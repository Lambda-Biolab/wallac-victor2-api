"""Phase 0 read-only COM probe for the Wallac 1420 instrument server.

Connects to the OEM MlrServ COM automation server (ProgID Wallac1420.Server,
doc 94) and reads identity/state/capability via the real interface graph
discovered from the TypeLib (doc 95): IInstrumentServer -> IState / IOptions.
Proves programmatic control and reports whether the instrument is connected.

Read-only: no assay, no register I/O. Output appended to PROBE_OUT (and
printed) line-by-line so partial progress is visible if a COM call blocks.

Lifecycle (doc 94): MlrServ must already be running (launch it directly via
PsExec -i 1 first); cold COM auto-launch fails 0x80080005. Run this probe in
the interactive session:  PsExec -i 1 -d C:\\Windows\\py.exe C:\\install\\probe.py
"""

import datetime
import traceback

PROGID = "Wallac1420.Server"
# Public is writable by the UAC-filtered standard token the probe runs under
# when launched as the interactive user (doc 95).
PROBE_OUT = r"C:\Users\Public\probe_out.txt"


def log(msg):
    line = f"{datetime.datetime.now().isoformat()} {msg}"
    with open(PROBE_OUT, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        print(line)
    except Exception:
        pass


def getval(obj, name):
    # comtypes dynamic dispatch invokes a no-arg method on attribute access
    # and returns the value directly (doc 95).
    try:
        return repr(getattr(obj, name))
    except Exception as exc:
        return f"EXC {exc!r}"


def main():
    open(PROBE_OUT, "w").close()
    log("=== Phase 0 probe start ===")
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    log(f"comtypes {comtypes.__version__}")

    try:
        srv = comtypes.client.CreateObject(PROGID)
        log(f"CreateObject OK: {srv!r}")
    except Exception:
        log("CreateObject FAILED\n" + traceback.format_exc())
        return 3

    # Identity (from the instrument -- real values mean it's connected).
    log("GetInstrumentModel = {}".format(getval(srv, "GetInstrumentModel")))
    log(
        "GetInstrumentSerialNumber = {}".format(
            getval(srv, "GetInstrumentSerialNumber")
        )
    )
    log("GetTargetTemperature = {}".format(getval(srv, "GetTargetTemperature")))
    log("GetPlateHeating = {}".format(getval(srv, "GetPlateHeating")))
    log("GetLastError = {}".format(getval(srv, "GetLastError")))
    log("GetLastErrorText = {}".format(getval(srv, "GetLastErrorText")))

    # Instrument connection/state (IState) -- the key signal.
    try:
        st = srv.GetState  # dynamic dispatch returns the IState object
        log(f"GetState -> {st!r}")
        for m in (
            "IsConnected",
            "IsIdle",
            "IsRunning",
            "IsError",
            "IsWaiting",
            "IsLoaded",
            "GetStateText",
            "GetStateCode",
        ):
            log(f"State.{m} = {getval(st, m)}")
    except Exception:
        log("GetState FAILED\n" + traceback.format_exc())

    # Capabilities (IOptions).
    try:
        op = srv.GetOptions
        for m in (
            "IsValid",
            "IsBarcodeReader",
            "IsTRFluorometer",
            "IsPromptFluorometer",
            "IsPhotometer",
            "IsLuminometer",
            "IsTempControl",
            "IsDispenser",
        ):
            log(f"Options.{m} = {getval(op, m)}")
    except Exception as exc:
        log(f"GetOptions EXC {exc!r}")

    log("=== Phase 0 probe end ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
