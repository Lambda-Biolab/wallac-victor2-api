"""Wallac 1420 instrument microservice -- Phase A (read-only).

Exposes the OEM MlrServ COM automation server as a REST/JSON API over the
libvirt NAT (doc 93/95). Read-only: /health, /instrument, /protocols.

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
MDB_SRC = r"C:\Program Files\Wallac1420\Data\Mlr3.mdb"
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
    def __init__(self, interval=1.0):
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
        while True:
            snap = {"ts": now_iso(), "error": None}
            try:
                srv = self._ensure()
                st = srv.GetState
                snap.update(
                    connected=bool(st.IsConnected),
                    state=str(st.GetStateText),
                    state_code=int(st.GetStateCode),
                    is_running=bool(st.IsRunning),
                    is_error=bool(st.IsError),
                    is_idle=bool(st.IsIdle),
                )
                try:
                    snap["target_temperature"] = float(srv.GetTargetTemperature)
                except Exception:  # noqa: BLE001
                    snap["target_temperature"] = None
            except Exception as exc:  # noqa: BLE001
                self._srv = None
                snap.update(connected=False, error=f"{type(exc).__name__}: {exc}")
            with _monitor_lock:
                snap["seq"] = _monitor.get("seq", 0) + 1
                _monitor.clear()
                _monitor.update(snap)
            time.sleep(self.interval)


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

        if not bool(srv.GetState.IsConnected):
            raise RuntimeError("instrument not connected")
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


def _grid_csv(wells, value):
    """8x12 plate grid CSV. value 'od' or 'raw' (meas_a). First label/repeat
    per well wins."""
    key = "od" if value == "od" else "meas_a"
    cell = {}
    for w in wells:
        addr = w.get("well") or ""
        if len(addr) >= 2 and addr[0].isalpha():
            row = addr[0].upper()
            try:
                col = int(addr[1:])
            except ValueError:
                continue
            if (row, col) not in cell:
                v = w.get(key)
                cell[(row, col)] = "" if v is None else v
    lines = ["row," + ",".join(str(c) for c in range(1, 13))]
    for row in "ABCDEFGH":
        lines.append(row + "," + ",".join(str(cell.get((row, c), "")) for c in range(1, 13)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# HTTP layer.
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "WallacAgent/0.1"

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
        return dict(p.split("=", 1) for p in qs.split("&") if "=" in p)

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
                    meta["state"] = _runs[run_id]["state"]
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
        self._send(
            503,
            {
                "error": "com_error",
                "detail": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            },
        )

    def _get_simple(self, path):
        w = self.server.worker
        if path == "/health":
            data = w.call(op_health)
            data["ok"] = data["instrument_connected"]
            data["ts"] = now_iso()
            self._send(200 if data["ok"] else 503, data)
        elif path == "/status":
            with _monitor_lock:
                snap = dict(_monitor)
            self._send(200 if snap.get("connected") else 503, snap)
        elif path == "/monitor":
            self._stream_monitor()
        elif path == "/instrument":
            self._send(200, w.call(op_instrument))
        else:  # /protocols
            protos = w.call(op_protocols("refresh=1" in self.path), timeout=40)
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
            wells = w.call(op_job_results(parts[1]), timeout=40)
            if parts[2] == "export":
                self._send_csv(200, self._job_csv(wells))
            else:
                self._send(200, {"assay_id": parts[1], "count": len(wells), "wells": wells})
        else:
            self._send(404, {"error": "not found", "path": "/" + "/".join(parts)})

    def _get_runs(self, parts):
        w = self.server.worker
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
            res = w.call(op_results, timeout=40)
            if parts[2] == "export":
                rows = ["well,counts,result_type,plate,plate_repeat"]
                for x in res.get("wells", []):
                    rows.append("{well},{counts},{result_type},{plate},{plate_repeat}".format(**x))
                self._send_csv(200, "\n".join(rows) + "\n")
            else:
                res["run"] = parts[1]
                self._send(200, res)
        else:
            self._send(404, {"error": "not found", "path": "/" + "/".join(parts)})

    def do_GET(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = path.strip("/").split("/")
        try:
            if path in ("/health", "/status", "/monitor", "/instrument", "/protocols"):
                self._get_simple(path)
            elif parts[0] == "jobs":
                self._get_jobs(parts)
            elif parts[0] == "runs":
                self._get_runs(parts)
            else:
                self._send(404, {"error": "not found", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)

    def _post_run(self):
        body = self._read_json()
        pid = body.get("protocol_id")
        if pid is None:
            self._send(400, {"error": "protocol_id required"})
            return
        dry = bool(body.get("dry_run", False))
        if not dry:
            active = _active_run_id()
            if active:
                self._send(409, {"error": "instrument busy", "active_run": active})
                return
        run_id = "r-" + uuid.uuid4().hex[:12]
        if not dry:
            with _runs_lock:
                _runs[run_id] = {
                    "run_id": run_id,
                    "protocol_id": int(pid),
                    "plate_id": body.get("plate_id"),
                    "state": "starting",
                    "started_at": now_iso(),
                    "ended_at": None,
                }
        res = self.server.worker.call(op_start_run(run_id, pid, dry), timeout=60)
        if dry:
            self._send(200, res)
        else:
            self._send(202, {"run_id": run_id, "state": "running", **res})

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
            self._send(401, {"error": "unauthorized"})
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = path.strip("/").split("/")
        try:
            if parts == ["runs"]:
                self._post_run()
            elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "abort":
                self._post_abort(parts[1])
            elif path == "/admin/reconnect":
                self._send(200, self.server.worker.reconnect())
            else:
                self._send(404, {"error": "not found", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)


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
