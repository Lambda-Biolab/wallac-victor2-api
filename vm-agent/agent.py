"""Wallac 1420 instrument microservice.

Exposes the OEM MlrServ COM automation server as a friendly REST/JSON API over
the libvirt NAT (doc 93/95). Highlights: POST /measure runs a protocol by name
and returns the deduped OD table; GET /docs lists every route. See also
/health, /instrument, /protocols, /runs, /jobs.

Lifecycle (doc 95): this agent MUST run as the interactive user 'lambda',
launched via launch_as_user.py, AND the OEM GUI (MlrMgr) must already be
running as 'lambda' so MlrServ is connected to the instrument. A COM client
in a different user context cannot attach (0x80080005).

COM is apartment-threaded: all COM access happens on one dedicated STA worker
thread; HTTP handler threads submit calls to it and block for the result.

Auth: optional bearer token read from TOKEN_FILE (no secret in code). If the
file is absent, auth is disabled (the NAT is host-only) and a warning logs.
"""

import contextlib
import json
import math
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

PROGID = "Wallac1420.Server"
RUNDEF_PROGID = "Wallac1420.Server.AssayRunDefinition"
# Coordinates abort with lid_watcher: set -> watcher clicks ABORT (cancel)
# instead of IGNORE (continue) on the next exception dialog.
ABORT_FLAG = r"C:\Users\Public\abort.flag"
# The instrument only honors a halt once it is fully into the measurement.
# Aborting earlier fails and can WEDGE the OEM state machine (doc 101), so we
# refuse an abort until the run is at least this old.
MIN_ABORT_AGE = 60.0
BIND_HOST = "0.0.0.0"  # VM has only the libvirt NAT NIC -> host-only
BIND_PORT = 8420
TOKEN_FILE = r"C:\Users\Public\agent_token.txt"  # readable by lambda token
CALL_TIMEOUT = 20.0

# Protocols are not in the COM API -- read from the Jet DB (doc 95).
MDB_SRC = (
    r"C:\Users\lambda\AppData\Local\VirtualStore\Program Files\Wallac\Wallac1420\Data\Mlr3.mdb"
)
MDB_COPY = r"C:\Users\Public\mlr3_agent_copy.mdb"
_PROT_SQL = (
    "SELECT p.AssayProtID, p.ProtName, p.ProtNumber, p.ProtVersion, "
    "p.FactoryPreset, g.GroupName "
    "FROM AssayProtocol p LEFT JOIN ProtocolGroup g "
    "ON p.ProtGroup = g.GroupID ORDER BY g.GroupName, p.ProtName"
)
_protocols_cache = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            tok = fh.read().strip()
            return tok or None
    except OSError:
        return None


# --------------------------------------------------------------------------
# COM worker: owns the dynamic-dispatch server object on a single STA thread.
# Connects lazily and reconnects on disconnect (Phase C reliability).
# --------------------------------------------------------------------------
_RECONNECT = object()  # sentinel: drop + recreate the COM object

# HRESULTs that mean "the server is gone" -> drop and reconnect.
_DISCONNECT_HR = {
    -2147417848,  # 0x80010108 RPC_E_DISCONNECTED
    -2147221507,  # 0x800401FD CO_E_OBJNOTCONNECTED
    -2147023174,  # 0x800706BA RPC server unavailable
    -2146959355,  # 0x80080005 CO_E_SERVER_EXEC_FAILURE
}


class ApiError(Exception):
    """An error with an HTTP status, a machine-readable code, and a human hint.

    Handlers raise this; the dispatcher turns it into a uniform JSON body
    ``{"error": code, "hint": ..., "detail": ...}``.
    """

    def __init__(self, status, code, hint, detail=None, extra=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.hint = hint
        self.detail = detail
        self.extra = extra or {}

    def payload(self):
        out = {"error": self.code, "hint": self.hint}
        if self.detail:
            out["detail"] = self.detail
        out.update(self.extra)
        return out


def _classify_exc(exc):
    """Translate a raw worker/COM exception into a friendly ``ApiError``.

    The OEM stack signals "not ready to measure" by returning a null assay
    object, which surfaces downstream as an AttributeError on ``None`` -- the
    opaque traceback users were seeing. We turn the common cases into an
    actionable hint and keep the raw text under ``detail``.
    """
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, TimeoutError):
        return ApiError(
            504,
            "com_timeout",
            "the instrument did not respond in time; check MlrMgr is running, "
            "then POST /admin/reconnect and retry",
        )
    msg = f"{type(exc).__name__}: {exc}"
    low = msg.lower()
    if "nonetype" in low and ("getjobid" in low or "getassayid" in low or "newassay" in low):
        return ApiError(
            409,
            "instrument_not_ready",
            "the reader refused to start the measurement -- close the lid, load "
            "a plate, clear any error shown in MlrMgr, then retry",
            detail=msg,
        )
    if "not connected" in low:
        return ApiError(
            503,
            "instrument_not_connected",
            "MlrMgr is not connected to the reader; start the OEM GUI and wait "
            "for it to connect, then retry",
            detail=msg,
        )
    if "already running" in low:
        return ApiError(
            409,
            "instrument_busy",
            "the reader is already running a measurement; wait for it to finish",
            detail=msg,
        )
    hr = getattr(exc, "hresult", None)
    if hr is None:
        hr = getattr(exc, "winerror", None)
    if hr in _DISCONNECT_HR:
        return ApiError(
            503,
            "instrument_link_lost",
            "the COM link to the reader dropped; POST /admin/reconnect, then retry",
            detail=msg,
        )
    if hr is None:
        # Not a COM error at all (e.g. a bug or a bad-request value) -- don't
        # mislabel it as a 503 instrument fault.
        return ApiError(
            500,
            "internal_error",
            "unexpected server error -- see detail/trace",
            detail=msg,
        )
    return ApiError(
        503,
        "com_error",
        "unexpected instrument/COM error -- see detail (and the agent stderr log)",
        detail=msg,
    )


class ComWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._q = queue.Queue()
        self.ready = threading.Event()
        self.init_error = None
        self._srv = None

    def _ensure(self):
        if self._srv is None:
            import comtypes.client

            self._srv = comtypes.client.CreateObject(PROGID)
        return self._srv

    def _do_reconnect(self, fut):
        self._srv = None
        try:
            self._ensure()
            fut["result"] = {"reconnected": True}
        except Exception as exc:  # noqa: BLE001
            fut["error"] = exc

    def _exec(self, fn, fut):
        for attempt in (0, 1):
            try:
                fut["result"] = fn(self._ensure())
                return
            except Exception as exc:  # noqa: BLE001
                hr = getattr(exc, "hresult", None)
                if hr is None:
                    hr = getattr(exc, "winerror", None)
                if attempt == 0 and hr in _DISCONNECT_HR:
                    self._srv = None  # reconnect + retry once
                    continue
                fut["error"] = exc
                return

    def run(self):
        import comtypes

        comtypes.CoInitialize()
        try:
            self._ensure()
        except Exception as exc:  # noqa: BLE001
            self.init_error = f"{type(exc).__name__}: {exc}"
        self.ready.set()
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, fut = item
            if fn is _RECONNECT:
                self._do_reconnect(fut)
            else:
                self._exec(fn, fut)
            fut["done"].set()

    def _submit(self, fn, timeout):
        fut = {"done": threading.Event()}
        self._q.put((fn, fut))
        if not fut["done"].wait(timeout):
            raise TimeoutError("COM call timed out")
        if "error" in fut:
            raise fut["error"]
        return fut.get("result")

    def call(self, fn, timeout=CALL_TIMEOUT):
        return self._submit(fn, timeout)

    def reconnect(self, timeout=60):
        return self._submit(_RECONNECT, timeout)


# --------------------------------------------------------------------------
# Real-time monitor: a dedicated STA thread with its OWN COM connection polls
# instrument state continuously (independent of the command worker, so a long
# op like abort never freezes monitoring). Latest snapshot in _monitor.
# --------------------------------------------------------------------------
_monitor = {"seq": 0, "ts": None, "connected": False}
_monitor_lock = threading.Lock()


class Monitor(threading.Thread):
    def __init__(self, interval=0.1):
        super().__init__(daemon=True)
        self.interval = interval
        self._srv = None

    def _ensure(self):
        if self._srv is None:
            import comtypes.client

            self._srv = comtypes.client.CreateObject(PROGID)
        return self._srv

    def run(self):
        import comtypes

        comtypes.CoInitialize()
        cycle = 0
        while True:
            cycle += 1
            snap = {"ts": now_iso(), "error": None, "live_wells": []}
            try:
                srv = self._ensure()
                self._snapshot_state(srv, snap, cycle)
                self._snapshot_live_wells(srv, snap)
            except Exception as exc:  # noqa: BLE001
                self._srv = None
                snap.update(connected=False, error=f"{type(exc).__name__}: {exc}")
            with _monitor_lock:
                snap["seq"] = _monitor.get("seq", 0) + 1
                _monitor.clear()
                _monitor.update(snap)
            time.sleep(self.interval)

    def _snapshot_state(self, srv, snap: dict, cycle: int) -> None:
        """Read instrument state + target temperature (throttled)."""
        st = srv.GetState
        snap.update(
            connected=bool(st.IsConnected),
            state=str(st.GetStateText),
            state_code=int(st.GetStateCode),
            is_running=bool(st.IsRunning),
            is_error=bool(st.IsError),
            is_idle=bool(st.IsIdle),
        )
        # Temperature changes slowly; only poll every ~1s (every 10th
        # cycle at 100ms) to keep the tight poll loop focused on
        # GetLiveResult.
        if cycle % 10 == 0:
            try:
                snap["target_temperature"] = float(srv.GetTargetTemperature)
            except Exception:  # noqa: BLE001
                with _monitor_lock:
                    snap["target_temperature"] = _monitor.get("target_temperature")
        else:
            with _monitor_lock:
                snap["target_temperature"] = _monitor.get("target_temperature")

        # Reconcile run state: if the instrument is idle but a run is
        # still marked "running", the run has completed. Transition it
        # so stale runs don't block new ones with 409 Conflict.
        if snap.get("is_idle") and not snap.get("is_running"):
            with _runs_lock:
                for rid, r in _runs.items():
                    if r.get("state") not in ("starting", "running"):
                        continue
                    assay = _assays.get(rid)
                    if assay is not None:
                        try:
                            if bool(assay.IsMeasured) or not bool(assay.IsRunning):
                                r["state"] = "completed"
                                r["ended_at"] = now_iso()
                        except Exception:  # noqa: BLE001
                            pass
                    else:
                        # No COM assay object — instrument is idle,
                        # so the run must have finished or been lost.
                        r["state"] = "completed"
                        r["ended_at"] = now_iso()

    def _snapshot_live_wells(self, srv, snap: dict) -> None:
        """Poll live results and merge with previous run's accumulation.

        The live buffer only holds the MOST RECENT well measured, so we
        accumulate across polls and reset when the assay_id changes
        (= new run started).
        """
        try:
            live = srv.GetLiveResult
            _ = live.Top
            current_wells = self._read_live_buffer(live)
            if current_wells:
                self._merge_live_wells(snap, current_wells)
            else:
                with _monitor_lock:
                    snap["live_wells"] = _monitor.get("live_wells", [])
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _read_live_buffer(live) -> list:
        """Iterate the COM GetLiveResult buffer, returning wells as dicts."""
        current_wells = []
        while bool(live.IsValid):
            current_wells.append(
                {
                    "well": str(live.GetWellAddress),
                    "counts": int(live.GetCounts),
                    "result_type": int(live.GetResultType),
                    "plate": int(live.GetPlateIndex),
                    "plate_repeat": int(live.GetPlateRepeatIndex),
                    "assay_id": int(live.GetAssayID),
                }
            )
            if not bool(live.Next):
                break
        return current_wells

    @staticmethod
    def _merge_live_wells(snap: dict, current_wells: list) -> None:
        """Merge current poll's wells with the previous accumulated snapshot.

        - New assay_id → reset accumulation (start of a new run)
        - Same assay_id + is_running → dedup-merge (avoid double-counting)
        - Otherwise → use current poll as-is
        """
        latest = current_wells[0]
        latest_assay = latest.get("assay_id", 0)
        with _monitor_lock:
            prev = list(_monitor.get("live_wells", []))
        prev_assay = prev[0].get("assay_id", 0) if prev else 0

        if latest_assay != prev_assay:
            # New run started; reset accumulation
            snap["live_wells"] = current_wells
        elif snap.get("is_running"):
            # Same run; merge new wells (dedup by well name)
            seen = {w["well"] for w in prev}
            merged = list(prev)
            for w in current_wells:
                if w["well"] not in seen:
                    merged.append(w)
                    seen.add(w["well"])
            snap["live_wells"] = merged
        else:
            snap["live_wells"] = current_wells


# --------------------------------------------------------------------------
# COM read operations (run on the worker thread; dynamic dispatch -> values).
# --------------------------------------------------------------------------
def op_health(srv):
    st = srv.GetState
    return {
        "instrument_connected": bool(st.IsConnected),
        "state": str(st.GetStateText),
        "state_code": int(st.GetStateCode),
        "is_running": bool(st.IsRunning),
        "is_error": bool(st.IsError),
        "is_idle": bool(st.IsIdle),
    }


def op_instrument(srv):
    st = srv.GetState
    connected = bool(st.IsConnected)
    out = {
        "connected": connected,
        "target_temperature": float(srv.GetTargetTemperature),
        "plate_heating": bool(srv.GetPlateHeating),
    }
    # Identity/options read the live link -- only safe when connected.
    if connected:
        out["serial"] = int(srv.GetInstrumentSerialNumber)
        out["model"] = int(srv.GetInstrumentModel)
        op = srv.GetOptions
        out["technologies"] = {
            "tr_fluorometer": bool(op.IsTRFluorometer),
            "prompt_fluorometer": bool(op.IsPromptFluorometer),
            "photometer": bool(op.IsPhotometer),
            "luminometer": bool(op.IsLuminometer),
            "barcode_reader": bool(op.IsBarcodeReader),
            "temp_control": bool(op.IsTempControl),
            "dispenser": bool(op.IsDispenser),
        }
    return out


def _read_protocols():
    # Runs on the COM worker (STA). Opens the Jet DB read-only; if the live
    # file is locked exclusively, falls back to a copy.
    import comtypes.client

    eng = comtypes.client.CreateObject("DAO.DBEngine.36")
    try:
        db = eng.OpenDatabase(MDB_SRC, False, True)
    except Exception:
        shutil.copy(MDB_SRC, MDB_COPY)
        db = eng.OpenDatabase(MDB_COPY, False, True)
    try:
        rs = db.OpenRecordset(_PROT_SQL)
        out = []
        while not rs.EOF:
            f = rs.Fields
            num = f.Item("ProtNumber").Value
            grp = f.Item("GroupName").Value
            out.append(
                {
                    "id": int(f.Item("AssayProtID").Value),
                    "name": str(f.Item("ProtName").Value),
                    "number": None if num is None else int(num),
                    "version": int(f.Item("ProtVersion").Value),
                    "group": None if grp is None else str(grp),
                    "factory_preset": bool(f.Item("FactoryPreset").Value),
                }
            )
            rs.MoveNext()
        rs.Close()
        return out
    finally:
        db.Close()


def op_protocols(refresh):
    def _op(_srv):
        global _protocols_cache
        if refresh or _protocols_cache is None:
            _protocols_cache = _read_protocols()
        return _protocols_cache

    return _op


def _resolve_protocol(spec, worker):
    """Resolve a protocol given as numeric id OR name (case-insensitive: exact
    match, then unique substring). Returns the full protocol record
    (``{id, name, group, factory_preset, ...}``).

    Raises ``ApiError`` (404 not found / 409 ambiguous) with candidates, so the
    caller never has to memorize the magic integer ids.
    """
    protos = worker.call(op_protocols(False), timeout=40)
    s = str(spec).strip()
    if s.isdigit():
        pid = int(s)
        for p in protos:
            if p["id"] == pid:
                return p
        # Protocol not in cache — may be a newly-generated protocol.
        # Refresh the cache from the MDB and try once more.
        protos = worker.call(op_protocols(True), timeout=40)
        for p in protos:
            if p["id"] == pid:
                return p
        raise ApiError(
            404,
            "protocol_not_found",
            "no protocol has that id; GET /protocols to list them",
            extra={"requested": spec},
        )
    low = s.lower()
    exact = [p for p in protos if p["name"].lower() == low]
    if len(exact) == 1:
        return exact[0]
    cands = exact if len(exact) > 1 else [p for p in protos if low in p["name"].lower()]
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise ApiError(
            409,
            "protocol_ambiguous",
            "that name matches several protocols; use a more specific name or the numeric id",
            extra={"candidates": [{"id": p["id"], "name": p["name"]} for p in cands[:12]]},
        )
    raise ApiError(
        404,
        "protocol_not_found",
        "no protocol matches that name; GET /protocols?q=... to search",
        extra={"requested": spec},
    )


# --------------------------------------------------------------------------
# Run management (Phase B). IAssay COM objects live ONLY on the worker
# thread (_assays); HTTP-visible metadata lives in _runs (guarded by lock).
# --------------------------------------------------------------------------
_runs = {}
_assays = {}
_runs_lock = threading.Lock()


def _active_run_id():
    with _runs_lock:
        for rid, r in _runs.items():
            if r["state"] in ("starting", "running"):
                return rid
    return None


def op_start_run(run_id, protocol_id, dry_run):
    """Build/configure the run definition; if not dry_run, start the assay
    (PHYSICAL: moves the carrier). Runs on the worker thread."""

    def _op(srv):
        import comtypes.client

        st = srv.GetState
        if not bool(st.IsConnected):
            raise RuntimeError("instrument not connected")
        # Defense-in-depth: refuse to start if the reader is physically already
        # measuring, even if the agent's _runs map was cleared (e.g. force-delete).
        if not dry_run and bool(st.IsRunning):
            raise RuntimeError("instrument is already running a measurement")
        try:
            from comtypes.gen import MlrServ as _M

            rd = comtypes.client.CreateObject(RUNDEF_PROGID, interface=_M.IAssayRunDefinition)
        except Exception:
            rd = comtypes.client.CreateObject(RUNDEF_PROGID)
        rd.ProtocolID = int(protocol_id)
        rd.LoadFirstPlate = True
        readback = int(rd.ProtocolID)
        if dry_run:
            return {"dry_run": True, "protocol_id": readback, "load_first_plate": True}
        assay = srv.NewAssay(rd)  # <-- starts the measurement
        _assays[run_id] = assay
        meta = {
            "job_id": int(assay.GetJobID),
            "assay_id": int(assay.GetAssayID),
            "protocol_id": int(assay.GetProtocolID),
        }
        with _runs_lock:
            _runs[run_id].update(state="running", **meta)
        return meta

    return _op


def op_run_state(run_id):
    def _op(srv):
        assay = _assays.get(run_id)
        if assay is None:
            return None
        measured = bool(assay.IsMeasured)
        info = {
            "is_running": bool(assay.IsRunning),
            "is_measured": measured,
            "is_ok": bool(assay.IsOk),
            "state_text": str(srv.GetState.GetStateText),
            "state_code": int(srv.GetState.GetStateCode),
        }
        with _runs_lock:
            r = _runs.get(run_id)
            if r and r["state"] == "running" and measured:
                r["state"] = "measured"
                r["ended_at"] = now_iso()
                # The OEM app assigns the real AssayID once it saves the row;
                # capture it so results resolve to the right persisted job.
                with contextlib.suppress(Exception):
                    aid = int(assay.GetAssayID)
                    if aid:
                        r["assay_id"] = aid
        return info

    return _op


def _request_stop(srv):
    with contextlib.suppress(Exception):
        cur = srv.GetCurrentAssay
        if cur is not None:
            srv.StopAssay(cur)
    with contextlib.suppress(Exception):
        _ = srv.Stop  # dynamic dispatch: attribute access invokes Stop()


def _wait_until_stopped(srv, tries=20):
    # Lid dialogs appear only every ~20s; hold on until the watcher ABORTs the
    # next one and the instrument leaves the running state (or we time out).
    for _ in range(tries):
        time.sleep(2)
        running = True
        with contextlib.suppress(Exception):
            running = bool(srv.GetState.IsRunning)
        if not running:
            return


def op_abort(run_id):
    def _op(srv):
        # Watcher (abort flag set) clicks ABORT on the next dialog; we trigger
        # the stop, wait for it to take effect, then report the real state.
        with contextlib.suppress(OSError):
            open(ABORT_FLAG, "w").close()
        _request_stop(srv)
        _wait_until_stopped(srv)
        with contextlib.suppress(OSError):
            os.remove(ABORT_FLAG)
        st = srv.GetState
        running = bool(st.IsRunning)
        with _runs_lock:
            if run_id in _runs:
                _runs[run_id]["state"] = "running" if running else "aborted"
                if not running:
                    _runs[run_id]["ended_at"] = now_iso()
        return {
            "ok": not running,
            "is_running": running,
            "state_text": str(st.GetStateText),
        }

    return _op


def op_results(srv):
    """Iterate ILive (GetLiveResult) for per-well counts. Returns empty when
    idle, so it is safe to call without a run."""
    live = srv.GetLiveResult
    wells = []
    try:
        _ = live.Top
        while bool(live.IsValid):
            wells.append(
                {
                    "well": str(live.GetWellAddress),
                    "counts": int(live.GetCounts),
                    "result_type": int(live.GetResultType),
                    "plate": int(live.GetPlateIndex),
                    "plate_repeat": int(live.GetPlateRepeatIndex),
                    "assay_id": int(live.GetAssayID),
                }
            )
            if not bool(live.Next):
                break
    except Exception as exc:  # noqa: BLE001
        return {"wells": wells, "note": f"live read stopped: {exc!r}"}
    return {"wells": wells}


# --------------------------------------------------------------------------
# Historical jobs/results from the live Jet DB. The OEM app runs as a
# standard-token user, so UAC virtualizes its writes into the per-user
# VirtualStore (doc 99) -- that copy is the authoritative results DB. We read
# a file-copy (it is locked by the running app).
# --------------------------------------------------------------------------
_RESULTS_VS = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\lambda\AppData\Local"),
    r"VirtualStore\Program Files\Wallac\Wallac1420\Data\Mlr3.mdb",
)
_RESULTS_REAL = r"C:\Program Files\Wallac\Wallac1420\Data\Mlr3.mdb"
_RESULTS_COPY = r"C:\Users\Public\results_snapshot.mdb"

_JOBS_SQL = (
    "SELECT a.AssayID, a.AssayProtID, p.ProtName, a.MeasBeginDate, "
    "a.MeasEndDate, a.WellsX, a.WellsY, a.Notes, a.Errors "
    "FROM AssayResult a LEFT JOIN AssayProtocol p "
    "ON a.AssayProtID = p.AssayProtID ORDER BY a.AssayID"
)
_JOB_RESULTS_SQL = (
    "SELECT w.Well, r.WellID, p.PlateNumber, r.LabelIndex, r.ResultType, "
    "r.RepeatNumber, r.MeasA, r.MeasB "
    "FROM ((PlateResult p INNER JOIN Result r ON p.PlateID = r.PlateID) "
    "INNER JOIN WellResult w ON (r.PlateID = w.PlateID "
    "AND r.WellID = w.WellID)) WHERE p.AssayID = {} "
    "ORDER BY p.PlateNumber, r.WellID, r.LabelIndex"
)


def _ival(v):
    return None if v is None else int(v)


def _open_results_db():
    import comtypes.client

    src = _RESULTS_VS if os.path.exists(_RESULTS_VS) else _RESULTS_REAL
    shutil.copy(src, _RESULTS_COPY)
    eng = comtypes.client.CreateObject("DAO.DBEngine.36")
    return eng.OpenDatabase(_RESULTS_COPY, False, True)


def op_jobs(_srv):
    db = _open_results_db()
    try:
        rs = db.OpenRecordset(_JOBS_SQL)
        out = []
        while not rs.EOF:
            f = rs.Fields
            grp = f.Item("ProtName").Value
            out.append(
                {
                    "assay_id": _ival(f.Item("AssayID").Value),
                    "protocol_id": _ival(f.Item("AssayProtID").Value),
                    "protocol_name": None if grp is None else str(grp),
                    "begin": str(f.Item("MeasBeginDate").Value)
                    if f.Item("MeasBeginDate").Value is not None
                    else None,
                    "end": str(f.Item("MeasEndDate").Value)
                    if f.Item("MeasEndDate").Value is not None
                    else None,
                    "wells_x": _ival(f.Item("WellsX").Value),
                    "wells_y": _ival(f.Item("WellsY").Value),
                    "notes": None if f.Item("Notes").Value is None else str(f.Item("Notes").Value),
                    "errors": None
                    if f.Item("Errors").Value is None
                    else str(f.Item("Errors").Value).strip(),
                }
            )
            rs.MoveNext()
        rs.Close()
        return out
    finally:
        db.Close()


def _parse_backgrounds(raw):
    """PlateResult.LabelPlateBackgrounds e.g. '1,0#313706,2;' ->
    {(label, result_type): (count, flashes)}."""
    out = {}
    for entry in str(raw or "").split(";"):
        entry = entry.strip()
        if "#" not in entry:
            continue
        left, right = entry.split("#", 1)
        try:
            label, rtype = (int(x) for x in left.split(",")[:2])
            cf = right.split(",")
            count = float(cf[0])
            flashes = float(cf[1]) if len(cf) > 1 else 1.0
            out[(label, rtype)] = (count, flashes)
        except (ValueError, IndexError):
            continue
    return out


def _od(meas_a, meas_b, bg):
    """Computed absorbance A = log10((bg_count/bg_flashes)/(signal/flashes)).
    Convenience only -- validate against an OEM-reported OD on a real plate."""
    if not bg or not meas_a or meas_a <= 0:
        return None
    bg_count, bg_flashes = bg
    flashes = meas_b if meas_b else 1
    if bg_count <= 0 or bg_flashes <= 0 or flashes <= 0:
        return None
    try:
        ratio = (bg_count / bg_flashes) / (meas_a / flashes)
        return round(math.log10(ratio), 4) if ratio > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def op_job_results(assay_id):
    def _op(_srv):
        db = _open_results_db()
        try:
            # per-plate, per-label backgrounds for OD computation.
            # Access fields by index (by-name is unreliable on joins).
            # cols: 0=PlateNumber, 1=LabelPlateBackgrounds
            bg_by_plate = {}
            rs = db.OpenRecordset(
                "SELECT PlateNumber, LabelPlateBackgrounds FROM PlateResult "
                f"WHERE AssayID = {int(assay_id)}"
            )
            while not rs.EOF:
                f = rs.Fields
                bg_by_plate[_ival(f.Item(0).Value)] = _parse_backgrounds(f.Item(1).Value)
                rs.MoveNext()
            rs.Close()

            # _JOB_RESULTS_SQL cols: 0=Well 1=WellID 2=PlateNumber 3=LabelIndex
            # 4=ResultType 5=RepeatNumber 6=MeasA 7=MeasB
            rs = db.OpenRecordset(_JOB_RESULTS_SQL.format(int(assay_id)))
            wells = []
            while not rs.EOF:
                f = rs.Fields
                plate = _ival(f.Item(2).Value)
                label = _ival(f.Item(3).Value)
                rtype = _ival(f.Item(4).Value)
                meas_a = _ival(f.Item(6).Value)
                meas_b = _ival(f.Item(7).Value)
                bg = bg_by_plate.get(plate, {}).get((label, rtype))
                wells.append(
                    {
                        "well": str(f.Item(0).Value),
                        "well_id": _ival(f.Item(1).Value),
                        "plate": plate,
                        "label": label,
                        "result_type": rtype,
                        "repeat": _ival(f.Item(5).Value),
                        "meas_a": meas_a,
                        "meas_b": meas_b,
                        "od": _od(meas_a, meas_b, bg),
                    }
                )
                rs.MoveNext()
            rs.Close()
            return wells
        finally:
            db.Close()

    return _op


def _parse_well_addr(addr):
    """Parse a well address like 'A01' -> ('A', 1). Returns (row, col) or (None, 0)."""
    if len(addr) < 2 or not addr[0].isalpha():
        return None, 0
    try:
        return addr[0].upper(), int(addr[1:])
    except ValueError:
        return None, 0


def _grid_csv(wells, value):
    """8x12 plate grid CSV. value 'od' or 'raw' (meas_a). First label/repeat
    per well wins."""
    key = "od" if value == "od" else "meas_a"
    cell = {}
    for w in wells:
        row, col = _parse_well_addr(w.get("well") or "")
        if row is not None and (row, col) not in cell:
            v = w.get(key)
            cell[(row, col)] = "" if v is None else v
    lines = ["row," + ",".join(str(c) for c in range(1, 13))]
    for row in "ABCDEFGH":
        lines.append(row + "," + ",".join(str(cell.get((row, c), "")) for c in range(1, 13)))
    return "\n".join(lines) + "\n"


def _normalize_well(w):
    """Collapse a live OR persisted well row to a uniform {well, od, counts}.

    Live rows carry ``counts`` (and no od); persisted rows carry ``meas_a``
    (raw A/D) plus a computed ``od``.
    """
    counts = w.get("counts")
    if counts is None:
        counts = w.get("meas_a")
    return {
        "well": w.get("well"),
        "od": w.get("od"),
        "counts": counts,
        "result_type": w.get("result_type"),
    }


def _dedup_wells(wells):
    """One row per well address (OEM stores two ResultType rows per well).

    Prefer the primary measurement (ResultType 0) when available; fall back
    to the first row with a non-null od. Preserve first-seen order.
    """
    best = {}
    order = []
    for w in wells:
        nw = _normalize_well(w)
        addr = nw["well"]
        if addr is None:
            continue
        rtype = nw.get("result_type")
        cur = best.get(addr)
        if cur is None:
            best[addr] = nw
            order.append(addr)
        elif rtype == 0 and cur.get("result_type") != 0:
            # Prefer ResultType 0 (primary measurement) over secondary
            best[addr] = nw
        elif cur.get("od") is None and nw.get("od") is not None:
            best[addr] = nw
    return [best[a] for a in order]


def _grid_dict(wells, value):
    """Flat {well_address: value} map for shape=grid (value 'od' or 'raw')."""
    key = "od" if value == "od" else "counts"
    out = {}
    for w in wells:
        a = w.get("well")
        if a and a not in out:
            out[a] = w.get(key)
    return out


def _norm_grid_csv(wells, value):
    """8x12 plate-grid CSV from normalized {well,od,counts} rows."""
    key = "od" if value == "od" else "counts"
    cell = {}
    for w in wells:
        a = w.get("well") or ""
        if len(a) >= 2 and a[0].isalpha():
            try:
                col = int(a[1:])
            except ValueError:
                continue
            cell[(a[0].upper(), col)] = w.get(key)
    rows = ["row," + ",".join(str(c) for c in range(1, 13))]
    for row in "ABCDEFGH":
        rows.append(
            row
            + ","
            + ",".join(
                "" if cell.get((row, c)) is None else str(cell.get((row, c))) for c in range(1, 13)
            )
        )
    return "\n".join(rows) + "\n"


def _format_results(wells_raw, source, shape="list", value="od", dedup=True):
    """Shape a raw well list into the API response: deduped {well,od,counts}
    plus an optional flat grid map."""
    wells = _dedup_wells(wells_raw) if dedup else [_normalize_well(w) for w in wells_raw]
    out = {"source": source, "well_count": len(wells), "wells": wells}
    if shape == "grid":
        out["grid"] = _grid_dict(wells, value)
    return out


def _latest_assay_for(protocol_id, worker):
    """Persisted AssayID of the newest saved run of this protocol.

    The in-memory IAssay reports id 0 until the OEM app writes the row, so to
    fetch a just-finished run's results we look up the highest saved AssayID
    for the same protocol.
    """
    jobs = worker.call(op_jobs, timeout=40)
    mine = [
        j
        for j in jobs
        if j.get("protocol_id") == int(protocol_id) and j.get("assay_id") is not None
    ]
    return max((j["assay_id"] for j in mine), default=None)


# --------------------------------------------------------------------------
# MDB write operations (Stage 5: generated-protocol support).
#
# These endpoints expose raw MDB (Jet database) operations that the bridge's
# GeneratedProtocolManager calls via RemoteMdbClient. All write operations
# (backup, insert, delete) are guarded by _mdb_write_lock.
#
# Feature flag: WALLAC_ENABLE_PROTOCOL_AUTHORING=true must be set for any
# write operation. Read operations (GET) are always allowed.
# --------------------------------------------------------------------------

_mdb_write_lock = threading.Lock()
ENV_ENABLE_AUTHORING = "WALLAC_ENABLE_PROTOCOL_AUTHORING"
MDB_BACKUP_DIR = r"C:\Users\Public\mdb_backups"

# Columns that can be overridden via SQL UPDATE after cloning a template protocol.
# Binary/OLE Object fields (PlateMap, NormalizationInfo) are copied by the
# clone operation and should not appear here.


def _is_authoring_enabled():
    return os.environ.get(ENV_ENABLE_AUTHORING, "").lower() == "true"


def _check_authoring():
    if not _is_authoring_enabled():
        raise ApiError(
            403,
            "authoring_disabled",
            "set env " + ENV_ENABLE_AUTHORING + "=true on the vm-agent to enable",
        )


def _open_mdb_r():
    """Open MDB read-only (falls back to copy if locked)."""
    import comtypes.client

    eng = comtypes.client.CreateObject("DAO.DBEngine.36")
    try:
        return eng.OpenDatabase(MDB_SRC, False, True)
    except Exception:
        shutil.copy(MDB_SRC, MDB_COPY)
        return eng.OpenDatabase(MDB_COPY, False, True)


def _open_mdb_w():
    """Open MDB for writing (read-only=False)."""
    import comtypes.client

    eng = comtypes.client.CreateObject("DAO.DBEngine.36")
    return eng.OpenDatabase(MDB_SRC, False, False)


def _rs_to_dict(rs):
    """Convert a DAO recordset current row to a JSON-serializable dict.

    DAO returns Python types that json.dumps can't handle (datetime,
    Decimal, bytes). Convert them to strings/numbers.
    """
    import datetime
    import decimal

    row = {}
    for i in range(rs.Fields.Count):
        f = rs.Fields.Item(i)
        val = f.Value
        if isinstance(val, (datetime.datetime, datetime.date)):
            val = val.isoformat()
        elif isinstance(val, decimal.Decimal):
            val = float(val)
        elif isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        row[f.Name] = val
    return row


def _rs_to_dicts(rs):
    """Convert an entire DAO recordset to a list of dicts."""
    rows = []
    while not rs.EOF:
        rows.append(_rs_to_dict(rs))
        rs.MoveNext()
    return rows


def op_mdb_get_group_id(group_name):
    def _op(_srv):
        db = _open_mdb_r()
        try:
            rs = db.OpenRecordset(
                "SELECT GroupID FROM ProtocolGroup WHERE GroupName = '"
                + group_name.replace("'", "''")
                + "'"
            )
            if rs.EOF:
                return None
            gid = int(rs.Fields("GroupID").Value)
            rs.Close()
            return gid
        finally:
            db.Close()

    return _op


def op_mdb_get_protocol(assay_prot_id):
    def _op(_srv):
        db = _open_mdb_r()
        try:
            rs = db.OpenRecordset(
                "SELECT * FROM AssayProtocol WHERE AssayProtID = " + str(int(assay_prot_id))
            )
            if rs.EOF:
                return None
            row = _rs_to_dict(rs)
            rs.Close()
            return row
        finally:
            db.Close()

    return _op


def op_mdb_find_protocol_by_name(name):
    def _op(_srv):
        db = _open_mdb_r()
        try:
            rs = db.OpenRecordset(
                "SELECT * FROM AssayProtocol WHERE ProtName = '" + name.replace("'", "''") + "'"
            )
            if rs.EOF:
                return None
            row = _rs_to_dict(rs)
            rs.Close()
            return row
        finally:
            db.Close()

    return _op


def op_mdb_get_max_protocol_id():
    def _op(_srv):
        db = _open_mdb_r()
        try:
            rs = db.OpenRecordset("SELECT MAX(AssayProtID) AS MaxID FROM AssayProtocol")
            if rs.EOF:
                return 0
            val = rs.Fields("MaxID").Value
            rs.Close()
            return int(val) if val is not None else 0
        finally:
            db.Close()

    return _op


def _mdb_column_names(db, template_id):
    """Get column names from the template AssayProtocol row."""
    rs = db.OpenRecordset("SELECT * FROM AssayProtocol WHERE AssayProtID = " + str(template_id))
    if rs.EOF:
        raise RuntimeError(f"Template protocol {template_id} not found")
    names = [rs.Fields.Item(i).Name for i in range(rs.Fields.Count)]
    rs.Close()
    return names


def _build_clone_sql(col_names, new_id, template_id):
    """Build INSERT INTO ... SELECT to clone a template row with a new ID."""
    select_parts = [str(new_id) if cn == "AssayProtID" else cn for cn in col_names]
    return (
        "INSERT INTO AssayProtocol ("
        + ", ".join(col_names)
        + ") SELECT "
        + ", ".join(select_parts)
        + " FROM AssayProtocol WHERE AssayProtID = "
        + str(template_id)
    )


def _sql_literal(val):
    """Convert a Python value to a Jet SQL literal."""
    if isinstance(val, bool):
        return str(-1 if val else 0)
    if val is None:
        return "NULL"
    if isinstance(val, (int, float)):
        return str(val)
    return "'" + str(val).replace("'", "''") + "'"


def _build_override_set_clauses(protocol_row, overridable_cols):
    """Build SET clauses for the UPDATE that overrides cloned fields."""
    clauses = []
    for key in overridable_cols:
        if key not in protocol_row:
            continue
        clauses.append(f"{key} = {_sql_literal(protocol_row[key])}")
    return clauses


def _execute_or_raise(db, sql, label):
    """Execute SQL, wrapping failures in a RuntimeError with context."""
    try:
        db.Execute(sql)
    except Exception as exc:
        raise RuntimeError(f"{label} failed: {exc}") from exc


def op_mdb_insert_protocol(protocol_row):
    """Insert a new AssayProtocol row by cloning a template via SQL.

    Approach: INSERT INTO ... SELECT clones the template row including
    binary fields (PlateMap, NormalizationInfo) that can't be set via
    DAO AppendChunk on a NULL OLE Object field. Then SQL UPDATE
    overrides the specific fields (AssayProtID, ProtName, etc.).

    Args:
        protocol_row: dict with keys:
            - AssayProtID (int): new protocol ID
            - _template_id (int): ID of the template to clone
            - ProtName (str): new protocol name
            - ProtNumber (int): protocol number
            - ProtVersion (int): protocol version
            - FactoryPreset (bool/int): factory preset flag
            - ProtGroup (int): protocol group ID
            - optional: LastRunDate, RunCount, CreatedTime, LastEditedTime
    """
    _OVERRIDABLE_COLS = frozenset(
        {
            "ProtName",
            "ProtNumber",
            "ProtVersion",
            "FactoryPreset",
            "ProtGroup",
            "LastRunDate",
            "RunCount",
            "CreatedTime",
            "LastEditedTime",
        }
    )

    def _op(_srv):
        db = _open_mdb_w()
        try:
            template_id = int(protocol_row.get("_template_id", 0))
            new_id = int(protocol_row["AssayProtID"])

            if template_id <= 0:
                raise RuntimeError("_template_id is required for protocol cloning")

            col_names = _mdb_column_names(db, template_id)
            _execute_or_raise(db, _build_clone_sql(col_names, new_id, template_id), "Clone INSERT")

            set_clauses = _build_override_set_clauses(protocol_row, _OVERRIDABLE_COLS)
            if set_clauses:
                update_sql = (
                    "UPDATE AssayProtocol SET "
                    + ", ".join(set_clauses)
                    + " WHERE AssayProtID = "
                    + str(new_id)
                )
                _execute_or_raise(db, update_sql, "Override UPDATE")

            return new_id
        finally:
            db.Close()

    return _op


def op_mdb_delete_protocol(assay_prot_id):
    def _op(_srv):
        db = _open_mdb_w()
        try:
            db.Execute("DELETE FROM AssayProtocol WHERE AssayProtID = " + str(int(assay_prot_id)))
            return True
        except Exception:
            return False
        finally:
            db.Close()

    return _op


def op_mdb_update_plate_map(assay_prot_id, plate_map):
    """Overwrite the PlateMap OLE Object field on a protocol row.

    plate_map: list of 108 ints (12-byte header + 96-byte 8x12 grid).
    Writes the entire blob via DAO AppendChunk. The field is cleared first
    so AppendChunk replaces rather than concatenates.

    args:
        assay_prot_id: int protocol ID
        plate_map: list[int] of 108 bytes
    returns:
        dict {"protocol_id": int, "bytes_written": int}
    """
    import array

    def _op(_srv):
        if len(plate_map) != 108:
            raise ApiError(
                400,
                "invalid_plate_map",
                f"plate_map must be 108 bytes (12 header + 96 grid); got {len(plate_map)}",
            )
        blob = array.array("B", [int(b) & 0xFF for b in plate_map])
        db = _open_mdb_w()
        try:
            rs = db.OpenRecordset(
                "SELECT PlateMap FROM AssayProtocol WHERE AssayProtID = " + str(int(assay_prot_id))
            )
            if rs.EOF:
                raise ApiError(404, "protocol_not_found", f"AssayProtID {assay_prot_id} not found")
            rs.Edit()
            # Overwrite (not append): set the OLE Object field to empty first
            # so AppendChunk starts from a clean state.
            fld = rs.Fields("PlateMap")
            with contextlib.suppress(Exception):
                # may not support empty-byte assignment; AppendChunk still replaces
                fld.Value = b""
            fld.AppendChunk(blob)
            rs.Update()
            rs.Close()
            return {
                "protocol_id": int(assay_prot_id),
                "bytes_written": len(blob),
            }
        finally:
            db.Close()

    return _op


def op_mdb_backup(name):
    def _op(_srv):
        if not os.path.exists(MDB_BACKUP_DIR):
            os.makedirs(MDB_BACKUP_DIR)
        backup_path = os.path.join(MDB_BACKUP_DIR, name)
        shutil.copy2(MDB_SRC, backup_path)
        return backup_path

    return _op


def op_mdb_query(sql):
    def _op(_srv):
        stripped = sql.strip().upper()
        if not stripped.startswith("SELECT"):
            raise ApiError(400, "invalid_query", "only SELECT queries are allowed")
        db = _open_mdb_r()
        try:
            rs = db.OpenRecordset(sql)
            rows = _rs_to_dicts(rs)
            rs.Close()
            return rows
        finally:
            db.Close()

    return _op


def op_mdb_ensure_group(group_name, group_id):
    """Insert a ProtocolGroup row if it doesn't exist. Returns the GroupID."""

    def _op(_srv):
        # Check if it already exists (by name or ID)
        db = _open_mdb_r()
        try:
            rs = db.OpenRecordset(
                "SELECT GroupID FROM ProtocolGroup WHERE GroupName = '"
                + group_name.replace("'", "''")
                + "' OR GroupID = "
                + str(int(group_id))
            )
            if not rs.EOF:
                gid = int(rs.Fields("GroupID").Value)
                rs.Close()
                return gid  # already exists
            rs.Close()
        finally:
            db.Close()

        # Insert new group via SQL INSERT (more reliable than AddNew for Jet)
        db = _open_mdb_w()
        try:
            sql = (
                "INSERT INTO ProtocolGroup (GroupID, GroupName, LeftMostChild, "
                "RightSibling, Parent, FactoryPreset, UserLevel, Notes) "
                "VALUES ("
                + str(int(group_id))
                + ", '"
                + group_name.replace("'", "''")
                + "', 0, 0, 1, 0, 1, "
                "'eLabFTW generated protocols')"
            )
            db.Execute(sql)
            return int(group_id)
        finally:
            db.Close()

    return _op


# --------------------------------------------------------------------------
# HTTP layer.
# --------------------------------------------------------------------------
_DOCS = {
    "service": "Wallac 1420 agent",
    "version": "0.2",
    "auth": "Authorization: Bearer <token>  (token file: C:\\Users\\Public\\agent_token.txt)",
    "quickstart": 'POST /measure {"protocol":"Absorbance @ 600"} -> waits, returns the OD table',
    "well_object": {"well": "A01", "od": 0.07, "counts": 360671},
    "endpoints": [
        {"method": "GET", "path": "/health", "desc": "liveness + 'ready' flag"},
        {
            "method": "GET",
            "path": "/instrument",
            "desc": "serial, model, technologies, temperature",
        },
        {"method": "GET", "path": "/status", "desc": "latest monitor snapshot"},
        {"method": "GET", "path": "/monitor", "desc": "SSE stream of state (~1 Hz)"},
        {"method": "GET", "path": "/protocols?q=<text>", "desc": "list/search protocols"},
        {"method": "GET", "path": "/protocols/<name|id>", "desc": "resolve one protocol"},
        {
            "method": "POST",
            "path": "/measure",
            "desc": "run a protocol by name and (by default) wait for the OD table",
            "body": {
                "protocol": "<name|id>",
                "wait": True,
                "timeout": 600,
                "shape": "list|grid",
                "value": "od|raw",
                "dry_run": False,
            },
        },
        {
            "method": "POST",
            "path": "/runs",
            "desc": "start a run (no wait)",
            "body": {"protocol": "<name|id>", "dry_run": False},
        },
        {"method": "GET", "path": "/runs", "desc": "list run records"},
        {
            "method": "GET",
            "path": "/runs/<id>",
            "desc": "status; state in starting|running|measured|aborted|failed",
        },
        {
            "method": "GET",
            "path": "/runs/<id>/results?shape=&value=&dedup=",
            "desc": "results (live while running, persisted once measured)",
        },
        {"method": "GET", "path": "/runs/<id>/export?...", "desc": "results as CSV"},
        {"method": "POST", "path": "/runs/<id>/abort", "desc": "abort (only >=60s into a run)"},
        {
            "method": "DELETE",
            "path": "/runs/<id>?force=",
            "desc": "forget a finished/failed/stuck run record",
        },
        {"method": "GET", "path": "/jobs", "desc": "saved measurement history"},
        {"method": "GET", "path": "/jobs/<assay_id>/results", "desc": "persisted per-well results"},
        {"method": "POST", "path": "/admin/reconnect", "desc": "drop + recreate the COM link"},
        # --- MDB generated-protocol endpoints (Stage 5) ---
        {
            "method": "GET",
            "path": "/mdb/groups?name=<name>",
            "desc": "get ProtocolGroup ID by name (read-only)",
        },
        {
            "method": "GET",
            "path": "/mdb/protocols/<id>",
            "desc": "get full AssayProtocol row by AssayProtID (read-only)",
        },
        {
            "method": "GET",
            "path": "/mdb/protocols?name=<name>",
            "desc": "find AssayProtocol by exact ProtName (read-only)",
        },
        {
            "method": "GET",
            "path": "/mdb/max-protocol-id",
            "desc": "highest AssayProtID in the MDB (read-only)",
        },
        {
            "method": "POST",
            "path": "/mdb/protocols",
            "desc": "insert a new AssayProtocol row (requires authoring flag)",
            "body": {"AssayProtID": 2000001, "ProtName": "ELAB-Job-1-abc12345", "...": "..."},
        },
        {
            "method": "DELETE",
            "path": "/mdb/protocols/<id>",
            "desc": "delete an AssayProtocol by ID (requires authoring flag)",
        },
        {
            "method": "POST",
            "path": "/mdb/backup",
            "desc": "create a timestamped MDB backup (requires authoring flag)",
            "body": {"name": "mlr3_backup_1_1719400000.mdb"},
        },
        {
            "method": "POST",
            "path": "/mdb/query",
            "desc": "execute a SELECT query (read-only)",
            "body": {"sql": "SELECT * FROM AssayProtocol WHERE FactoryPreset = False"},
        },
        {
            "method": "POST",
            "path": "/mdb/groups",
            "desc": "create a ProtocolGroup if it doesn't exist (idempotent, requires authoring flag)",
            "body": {"name": "eLabFTW Generated", "group_id": 10001},
        },
    ],
}


class Handler(BaseHTTPRequestHandler):
    server_version = "WallacAgent/0.2"

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = self.server.token
        if token is None:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + token

    def _send_csv(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _query(self):
        if "?" not in self.path:
            return {}
        qs = self.path.split("?", 1)[1]
        out = {}
        for p in qs.split("&"):
            if "=" in p:
                k, v = p.split("=", 1)
                out[unquote(k)] = unquote(v)
        return out

    def _run_view(self, run_id, refresh=True):
        with _runs_lock:
            meta = dict(_runs.get(run_id, {}))
        if not meta:
            return None
        if refresh and meta.get("state") in ("running", "starting"):
            live = self.server.worker.call(op_run_state(run_id))
            if live:
                meta["live"] = live
                with _runs_lock:
                    current = _runs.get(run_id)
                    if current:  # may have been DELETEd concurrently
                        meta["state"] = current["state"]
                        meta["assay_id"] = current.get("assay_id")
        meta["protocol"] = {"id": meta.get("protocol_id"), "name": meta.get("protocol_name")}
        return meta

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write(f"{now_iso()} - {fmt % args}\n")

    def _stream_monitor(self):
        """Server-Sent Events: push the live instrument snapshot on every
        monitor update (~1 Hz). Real-time device monitoring."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = -1
        try:
            while True:
                with _monitor_lock:
                    snap = dict(_monitor)
                if snap.get("seq") != last:
                    last = snap.get("seq")
                    self.wfile.write(("data: " + json.dumps(snap) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected

    def _com_error(self, exc):
        err = _classify_exc(exc)
        body = err.payload()
        if not isinstance(exc, ApiError):
            body["trace"] = traceback.format_exc()
        self._send(err.status, body)

    # --- MDB generated-protocol handlers (Stage 5) ---

    def _mdb_get_group(self):
        q = self._query()
        name = q.get("name", "")
        if not name:
            self._send(400, {"error": "name_required", "hint": "provide ?name=<GroupName>"})
            return
        gid = self.server.worker.call(op_mdb_get_group_id(unquote(name)), timeout=40)
        if gid is None:
            self._send(404, {"error": "group_not_found", "name": name})
        else:
            self._send(200, {"group_id": gid, "name": name})

    def _mdb_get_protocol(self, assay_prot_id):
        try:
            pid = int(assay_prot_id)
        except ValueError:
            self._send(400, {"error": "invalid_id", "hint": "AssayProtID must be an integer"})
            return
        row = self.server.worker.call(op_mdb_get_protocol(pid), timeout=40)
        if row is None:
            self._send(404, {"error": "protocol_not_found", "assay_prot_id": pid})
        else:
            self._send(200, row)

    def _mdb_find_protocol(self):
        q = self._query()
        name = q.get("name", "")
        if not name:
            self._send(400, {"error": "name_required", "hint": "provide ?name=<ProtName>"})
            return
        row = self.server.worker.call(op_mdb_find_protocol_by_name(unquote(name)), timeout=40)
        if row is None:
            self._send(404, {"error": "protocol_not_found", "name": name})
        else:
            self._send(200, row)

    def _mdb_get_max_id(self):
        max_id = self.server.worker.call(op_mdb_get_max_protocol_id(), timeout=40)
        self._send(200, {"max_assay_prot_id": max_id})

    def _mdb_insert_protocol(self):
        _check_authoring()
        body = self._read_json()
        if "AssayProtID" not in body or "ProtName" not in body:
            self._send(
                400, {"error": "missing_fields", "hint": "AssayProtID and ProtName are required"}
            )
            return
        with _mdb_write_lock:
            new_id = self.server.worker.call(op_mdb_insert_protocol(body), timeout=60)
        self._send(201, {"assay_prot_id": new_id, "created": True})

    def _mdb_delete_protocol(self, assay_prot_id):
        _check_authoring()
        try:
            pid = int(assay_prot_id)
        except ValueError:
            self._send(400, {"error": "invalid_id", "hint": "AssayProtID must be an integer"})
            return
        with _mdb_write_lock:
            deleted = self.server.worker.call(op_mdb_delete_protocol(pid), timeout=60)
        if deleted:
            self._send(200, {"assay_prot_id": pid, "deleted": True})
        else:
            self._send(404, {"error": "delete_failed", "assay_prot_id": pid})

    def _mdb_backup(self):
        _check_authoring()
        body = self._read_json()
        name = body.get("name", "")
        if not name:
            self._send(400, {"error": "name_required", "hint": "provide a backup filename"})
            return
        with _mdb_write_lock:
            backup_path = self.server.worker.call(op_mdb_backup(name), timeout=60)
        self._send(200, {"backup_path": backup_path, "created": True})

    def _mdb_query(self):
        body = self._read_json()
        sql = body.get("sql", "")
        if not sql:
            self._send(400, {"error": "sql_required", "hint": "provide a SELECT query"})
            return
        rows = self.server.worker.call(op_mdb_query(sql), timeout=40)
        self._send(200, {"count": len(rows), "rows": rows})

    def _mdb_ensure_group(self):
        _check_authoring()
        body = self._read_json()
        name = body.get("name", "")
        group_id = body.get("group_id")
        if not name or not group_id:
            self._send(400, {"error": "missing_fields", "hint": "name and group_id are required"})
            return
        with _mdb_write_lock:
            gid = self.server.worker.call(op_mdb_ensure_group(name, group_id), timeout=60)
        self._send(200, {"group_id": gid, "name": name, "created": True})

    def _get_simple(self, path):
        w = self.server.worker
        if path == "/health":
            self._send_health(w)
        elif path == "/status":
            self._send_status()
        elif path == "/monitor":
            self._stream_monitor()
        elif path == "/instrument":
            self._send(200, w.call(op_instrument))
        else:  # /protocols
            self._send_protocols(w)

    def _send_health(self, w):
        data = w.call(op_health)
        data["ready"] = bool(
            data["instrument_connected"] and not data["is_error"] and data.get("is_idle")
        )
        data["ok"] = data["instrument_connected"]
        data["ts"] = now_iso()
        self._send(200 if data["ok"] else 503, data)

    def _send_status(self):
        with _monitor_lock:
            snap = dict(_monitor)
        self._send(200 if snap.get("connected") else 503, snap)

    def _send_protocols(self, w):
        protos = w.call(op_protocols("refresh=1" in self.path), timeout=40)
        q = self._query().get("q")
        if q:
            ql = unquote(q).lower()
            protos = [p for p in protos if ql in p["name"].lower()]
        self._send(200, {"count": len(protos), "protocols": protos})

    def _job_csv(self, wells):
        q = self._query()
        if q.get("format") == "grid":
            return _grid_csv(wells, q.get("value", "raw"))
        rows = ["well,plate,label,result_type,repeat,meas_a,meas_b,od"]
        for w in wells:
            od = "" if w["od"] is None else w["od"]
            rows.append(
                "{},{},{},{},{},{},{},{}".format(
                    w["well"],
                    w["plate"],
                    w["label"],
                    w["result_type"],
                    w["repeat"],
                    w["meas_a"],
                    w["meas_b"],
                    od,
                )
            )
        return "\n".join(rows) + "\n"

    def _get_jobs(self, parts):
        w = self.server.worker
        if parts == ["jobs"]:
            jobs = w.call(op_jobs, timeout=40)
            self._send(200, {"count": len(jobs), "jobs": jobs})
        elif len(parts) == 2:
            jobs = w.call(op_jobs, timeout=40)
            match = [j for j in jobs if str(j["assay_id"]) == parts[1]]
            self._send(
                200 if match else 404,
                match[0] if match else {"error": "no such job", "job": parts[1]},
            )
        elif len(parts) == 3 and parts[2] in ("results", "export"):
            self._get_job_results(parts[1], parts[2] == "export")
        else:
            self._send(
                404, {"error": "not_found", "hint": "see GET /docs", "path": "/" + "/".join(parts)}
            )

    def _get_job_results(self, assay_id, export):
        """Serve persisted job results as JSON or CSV."""
        w = self.server.worker
        q = self._query()
        shape = "grid" if q.get("format") == "grid" else q.get("shape", "list")
        value = q.get("value", "od")
        dedup = q.get("dedup", "1") != "0"
        wells_raw = w.call(op_job_results(assay_id), timeout=40)
        if not dedup:  # full raw rows (all ResultType rows + meas fields)
            if export:
                self._send_csv(200, self._job_csv(wells_raw))
            else:
                self._send(200, {"assay_id": assay_id, "count": len(wells_raw), "wells": wells_raw})
            return
        out = _format_results(wells_raw, "persisted", shape=shape, value=value, dedup=True)
        out["assay_id"] = assay_id
        self._send_results(out, export, shape, value)

    def _get_runs(self, parts):
        if parts == ["runs"]:
            with _runs_lock:
                runs = list(_runs.values())
            self._send(200, {"count": len(runs), "runs": runs})
        elif len(parts) == 2:
            view = self._run_view(parts[1])
            self._send(
                200 if view else 404,
                view or {"error": "no such run", "run": parts[1]},
            )
        elif len(parts) == 3 and parts[2] in ("results", "export"):
            self._run_results(parts[1], export=(parts[2] == "export"))
        else:
            self._send(404, {"error": "not found", "path": "/" + "/".join(parts)})

    def _persisted_wells(self, protocol_id, assay_id):
        """(wells, source) from the persisted DB by assay id, falling back to
        the newest saved run of the protocol; or the live buffer if none."""
        w = self.server.worker
        aid = assay_id or (_latest_assay_for(protocol_id, w) or 0)
        if aid:
            return w.call(op_job_results(aid), timeout=40), "persisted"
        return w.call(op_results, timeout=40).get("wells", []), "live"

    def _run_wells(self, run_id, meta):
        """Resolve (wells_raw, source) for a run: persisted DB rows once
        measured/aborted, else live wells from the monitor thread (which
        has its own COM connection and is NOT blocked by the running assay)."""
        if meta.get("state") in ("measured", "aborted"):
            return self._persisted_wells(meta["protocol_id"], meta.get("assay_id") or 0)
        # Read live wells from the monitor snapshot (updated by the monitor
        # thread's own COM connection, which works during runs).
        with _monitor_lock:
            snap = dict(_monitor)
        live_wells = snap.get("live_wells", [])
        if live_wells:
            return live_wells, "live"
        # Monitor snapshot empty; try the worker thread as a fallback (it
        # can process COM calls during a run — NewAssay is non-blocking).
        w = self.server.worker
        worker_result = w.call(op_results, timeout=3).get("wells", [])
        if worker_result:
            return worker_result, "live"
        # May have transitioned to measured since the snapshot
        with _runs_lock:
            cur = _runs.get(run_id) or {}
            st2, aid2 = cur.get("state"), (cur.get("assay_id") or 0)
        if st2 in ("measured", "aborted"):
            return self._persisted_wells(meta["protocol_id"], aid2)
        return [], "live"

    def _run_results(self, run_id, export=False):
        """Results for a run. Streams the live buffer while running; once the
        run is measured/aborted, serves the authoritative persisted DB rows.
        Query: shape=list|grid, value=od|raw, dedup=1|0, format=grid (alias)."""
        q = self._query()
        shape = "grid" if q.get("format") == "grid" else q.get("shape", "list")
        value = q.get("value", "od")
        dedup = q.get("dedup", "1") != "0"
        with _runs_lock:
            meta = dict(_runs.get(run_id, {}))
        if not meta:
            raise ApiError(404, "run_not_found", "no run with that id; GET /runs to list")
        wells_raw, source = self._run_wells(run_id, meta)
        out = _format_results(wells_raw, source, shape=shape, value=value, dedup=dedup)
        out["run_id"] = run_id
        self._send_results(out, export, shape, value)

    def _send_results(self, out, export, shape, value):
        """Emit a formatted results dict as JSON, or CSV (flat or 8x12 grid)."""
        if not export:
            self._send(200, out)
        elif shape == "grid":
            self._send_csv(200, _norm_grid_csv(out["wells"], value))
        else:
            rows = ["well,od,counts"]
            for wl in out["wells"]:
                od = "" if wl.get("od") is None else wl["od"]
                ct = "" if wl.get("counts") is None else wl["counts"]
                rows.append("{},{},{}".format(wl.get("well"), od, ct))
            self._send_csv(200, "\n".join(rows) + "\n")

    def do_GET(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = path.strip("/").split("/")
        try:
            self._route_get(path, parts)
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)

    def _route_get(self, path, parts):
        """Dispatch a GET request to the appropriate handler."""
        if path in ("/", "/docs"):
            self._send(200, _DOCS)
        elif path in ("/health", "/status", "/monitor", "/instrument", "/protocols"):
            self._get_simple(path)
        elif parts[0] == "protocols" and len(parts) == 2:
            self._send(200, _resolve_protocol(unquote(parts[1]), self.server.worker))
        elif parts[0] == "jobs":
            self._get_jobs(parts)
        elif parts[0] == "runs":
            self._get_runs(parts)
        elif parts[0] == "mdb":
            self._route_mdb(parts)
        else:
            self._send(404, {"error": "not_found", "hint": "see GET /docs", "path": path})

    def _route_mdb(self, parts):
        """Dispatch /mdb/* GET requests."""
        if parts[1] == "groups":
            self._mdb_get_group()
        elif parts[1] == "protocols" and len(parts) == 2:
            self._mdb_find_protocol()
        elif parts[1] == "protocols" and len(parts) == 3:
            self._mdb_get_protocol(parts[2])
        elif len(parts) == 2 and parts[1] == "max-protocol-id":
            self._mdb_get_max_id()
        else:
            self._send(
                404,
                {
                    "error": "not_found",
                    "hint": "see GET /docs",
                    "path": "/mdb/" + "/".join(parts[1:]),
                },
            )

    def _begin_run(self, proto_spec, dry=False, plate_id=None):
        """Resolve the protocol (name or id), start the run, return
        (proto, run_id, res). On a non-dry start failure the run is marked
        'failed' (not left as 'starting') so it never blocks future runs.
        Raises ApiError on any problem."""
        w = self.server.worker
        proto = _resolve_protocol(proto_spec, w)
        run_id = "r-" + uuid.uuid4().hex[:12]
        if not dry:
            # Atomically re-check "busy" and reserve the slot under one lock so
            # two concurrent /measure calls cannot both pass the guard (TOCTOU).
            with _runs_lock:
                for rid, r in _runs.items():
                    if r["state"] in ("starting", "running"):
                        raise ApiError(
                            409,
                            "instrument_busy",
                            "a run is already in progress; wait for it, or abort/DELETE it first",
                            extra={"active_run": rid},
                        )
                _runs[run_id] = {
                    "run_id": run_id,
                    "protocol_id": proto["id"],
                    "protocol_name": proto["name"],
                    "plate_id": plate_id,
                    "assay_id": None,
                    "state": "starting",
                    "started_at": now_iso(),
                    "ended_at": None,
                }
        try:
            res = w.call(op_start_run(run_id, proto["id"], dry), timeout=60)
        except Exception as exc:  # noqa: BLE001
            if not dry:
                with _runs_lock:
                    if run_id in _runs:
                        _runs[run_id]["state"] = "failed"
                        _runs[run_id]["ended_at"] = now_iso()
            raise _classify_exc(exc) from exc
        return proto, run_id, res

    def _post_run(self):
        body = self._read_json()
        proto_spec = body.get("protocol", body.get("protocol_id"))
        if proto_spec is None:
            raise ApiError(
                400, "protocol_required", "provide 'protocol' (name or id) in the JSON body"
            )
        dry = bool(body.get("dry_run", False))
        proto, run_id, res = self._begin_run(proto_spec, dry, body.get("plate_id"))
        if dry:
            self._send(200, dict(res, protocol=proto))
        else:
            self._send(202, dict(res, run_id=run_id, state="running", protocol=proto))

    def _await_measured(self, run_id, timeout):
        """Block until the run reports measured; raise measure_timeout if not."""
        w = self.server.worker
        deadline = time.time() + timeout
        while time.time() < deadline:
            live = w.call(op_run_state(run_id), timeout=CALL_TIMEOUT)
            if live and live.get("is_measured"):
                return
            time.sleep(2.0)
        raise ApiError(
            504,
            "measure_timeout",
            "the run did not finish within the timeout; poll GET /runs/" + run_id + " for status",
            extra={"run_id": run_id},
        )

    def _resolve_run_assay(self, run_id, protocol_id):
        """This run's authoritative AssayID. The OEM updates the in-memory
        assay's GetAssayID shortly after IsMeasured, so poll for it before
        falling back to 'latest for protocol' (which could be a PRIOR run)."""
        w = self.server.worker
        for _ in range(8):
            with _runs_lock:
                aid = (_runs.get(run_id) or {}).get("assay_id") or 0
            if aid:
                return aid
            w.call(op_run_state(run_id), timeout=CALL_TIMEOUT)  # nudge GetAssayID
            time.sleep(1.0)
        return _latest_assay_for(protocol_id, w) or 0

    def _await_persisted_wells(self, aid):
        """Retry the persisted read until the OEM flushes the result rows
        (IsMeasured can flip a beat before the Jet DB write completes)."""
        if not aid:
            return []
        w = self.server.worker
        for _ in range(8):
            wells = w.call(op_job_results(aid), timeout=40)
            if wells:
                return wells
            time.sleep(2.0)
        return []

    def _post_measure(self):
        """One-shot: resolve protocol by name, start, (optionally) wait for the
        measurement to finish, and return the deduped, persisted OD table."""
        body = self._read_json()
        proto_spec = body.get("protocol", body.get("protocol_id"))
        if proto_spec is None:
            raise ApiError(
                400, "protocol_required", "provide 'protocol' (name or id) in the JSON body"
            )
        dry = bool(body.get("dry_run", False))
        wait = bool(body.get("wait", True))
        timeout = float(body.get("timeout", 600))
        shape = body.get("shape", "list")
        value = body.get("value", "od")
        proto, run_id, res = self._begin_run(proto_spec, dry, body.get("plate_id"))
        if dry:
            self._send(200, dict(res, protocol=proto))
            return
        if not wait:
            self._send(202, dict(res, run_id=run_id, state="running", protocol=proto))
            return
        self._await_measured(run_id, timeout)
        aid = self._resolve_run_assay(run_id, proto["id"])
        wells_raw = self._await_persisted_wells(aid)
        out = _format_results(wells_raw, "persisted", shape=shape, value=value, dedup=True)
        out.update(run_id=run_id, assay_id=(aid or None), protocol=proto, state="measured")
        if not wells_raw:
            out["note"] = (
                "measurement finished but results not flushed yet; "
                "GET /runs/" + run_id + "/results in a moment"
            )
        self._send(200, out)

    def _post_abort(self, run_id):
        with _runs_lock:
            meta = dict(_runs.get(run_id) or {})
        started = meta.get("started_at")
        if started:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
            if age < MIN_ABORT_AGE:
                self._send(
                    425,
                    {
                        "error": "too early to abort",
                        "detail": f"instrument only honors abort ~{MIN_ABORT_AGE:.0f}s "
                        "into a run (aborting earlier wedges it);"
                        f" wait {MIN_ABORT_AGE - age:.0f}s more",
                        "run": run_id,
                        "age_s": round(age, 1),
                    },
                )
                return
        res = self.server.worker.call(op_abort(run_id), timeout=50)
        res["run"] = run_id
        res["state"] = "aborted" if res.get("ok") else "running"
        self._send(200, res)

    def do_POST(self):
        if not self._authorized():
            self._send(
                401, {"error": "unauthorized", "hint": "send 'Authorization: Bearer <token>'"}
            )
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = path.strip("/").split("/")
        try:
            if path == "/measure":
                self._post_measure()
            elif parts == ["runs"]:
                self._post_run()
            elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "abort":
                self._post_abort(parts[1])
            elif path == "/admin/reconnect":
                self._send(200, self.server.worker.reconnect())
            elif path == "/mdb/protocols":
                self._mdb_insert_protocol()
            elif path == "/mdb/backup":
                self._mdb_backup()
            elif path == "/mdb/query":
                self._mdb_query()
            elif path == "/mdb/groups":
                self._mdb_ensure_group()
            else:
                self._send(404, {"error": "not_found", "hint": "see GET /docs", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)

    def do_DELETE(self):
        if not self._authorized():
            self._send(
                401, {"error": "unauthorized", "hint": "send 'Authorization: Bearer <token>'"}
            )
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = path.strip("/").split("/")
        try:
            if len(parts) == 2 and parts[0] == "runs":
                self._delete_run(parts[1])
            elif len(parts) == 3 and parts[0] == "mdb" and parts[1] == "protocols":
                self._mdb_delete_protocol(parts[2])
            else:
                self._send(404, {"error": "not_found", "hint": "see GET /docs", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)

    def do_PATCH(self):
        if not self._authorized():
            self._send(
                401, {"error": "unauthorized", "hint": "send 'Authorization: Bearer <token>'"}
            )
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = path.strip("/").split("/")
        try:
            if (
                len(parts) == 4
                and parts[0] == "mdb"
                and parts[1] == "protocols"
                and parts[3] == "plate_map"
            ):
                self._mdb_update_plate_map(parts[2])
            else:
                self._send(404, {"error": "not_found", "hint": "see GET /docs", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)

    def _mdb_update_plate_map(self, assay_prot_id):
        """PATCH /mdb/protocols/{id}/plate_map — overwrite the PlateMap blob.

        Body: {"plate_map": [108 ints]} (12-byte header + 96-byte 8x12 grid).
        """
        try:
            pid = int(assay_prot_id)
        except ValueError:
            raise ApiError(400, "invalid_id", "protocol id must be an integer") from None
        body = self._read_json()
        plate_map = body.get("plate_map")
        if not isinstance(plate_map, list):
            raise ApiError(400, "invalid_body", "plate_map must be a list of 108 ints")
        result = self.server.worker.call(op_mdb_update_plate_map(pid, plate_map), timeout=30)
        self._send(200, result)

    def _delete_run(self, run_id):
        """Forget a finished/failed/stuck run record (frees the 'busy' guard).
        Refuses a still-running run unless ?force=1."""
        force = self._query().get("force", "0") not in ("0", "", "false")
        with _runs_lock:
            meta = _runs.get(run_id)
            if not meta:
                raise ApiError(404, "run_not_found", "no run with that id")
            if meta.get("state") in ("starting", "running") and not force:
                raise ApiError(
                    409,
                    "run_active",
                    "run is active; abort it first, or pass ?force=1 to drop the record anyway",
                )
            _runs.pop(run_id, None)
            _assays.pop(run_id, None)
        self._send(200, {"deleted": run_id})


def main():
    worker = ComWorker()
    worker.start()
    worker.ready.wait(30)
    if worker.init_error:
        sys.stderr.write(f"COM init failed: {worker.init_error}\n")
        # Serve anyway so /health reports the failure rather than nothing.

    Monitor().start()  # real-time device monitoring (independent COM thread)

    httpd = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    httpd.worker = worker
    httpd.token = load_token()
    if httpd.token is None:
        sys.stderr.write(f"WARNING: no {TOKEN_FILE} -> auth DISABLED (NAT-only)\n")
    sys.stderr.write(f"{now_iso()} Wallac agent listening on {BIND_HOST}:{BIND_PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
