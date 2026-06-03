"""Auto-dismiss the Victor2 'LID OPEN ERROR' dialog (faulty lid sensor).

The reader's lid interlock falsely reports OPEN even when closed, so the OEM
stack pops a modal 'Wallac 1420 Exception' with Abort/Retry/Ignore that stalls
every measurement. This watcher (run as the interactive user 'lambda', e.g.
from start-stack.bat) polls for that dialog and clicks **Ignore**.

NOTE: the message body is owner-drawn, so cross-process GetWindowText can't
read 'LID OPEN' to match on text. We instead target the recoverable-error
signature -- a 'Wallac 1420 Exception' dialog with Abort+Retry+Ignore buttons
-- and click Ignore. Every action is logged (C:\\Users\\Public\\lid_watcher.log)
so the bypass is auditable. (A more precise fix is to handle
IInstrumentEvents.OnError's Action out-param; see doc 100.)
"""

import contextlib
import ctypes
import os
import time
from ctypes import wintypes

u = ctypes.windll.user32
u.SendMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
u.SendMessageW.restype = wintypes.LPARAM

BM_CLICK = 0x00F5
DIALOG_TITLE = "Wallac 1420 Exception"
ARI = {"abort", "retry", "ignore"}
LOG = r"C:\Users\Public\lid_watcher.log"
# When the agent wants to abort a run it drops this flag; we then click
# ABORT (cancel) instead of IGNORE (continue) on the next exception dialog.
ABORT_FLAG = r"C:\Users\Public\abort.flag"

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def win_text(h):
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, buf, 512)
    return buf.value


def win_class(h):
    buf = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(h, buf, 64)
    return buf.value


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def inspect_dialog(hdlg):
    """Return (button_label -> hwnd dict, body_text)."""
    buttons = {}
    body = []

    def cb(h, _l):
        t = win_text(h).strip()
        if win_class(h) == "Button":
            buttons[t.lower().replace("&", "")] = h
        elif t:
            body.append(t)
        return True

    u.EnumChildWindows(hdlg, EnumProc(cb), 0)
    return buttons, " ".join(body)


def scan():
    dialogs = []

    def cb(h, _l):
        if u.IsWindowVisible(h) and win_text(h) == DIALOG_TITLE:
            dialogs.append(h)
        return True

    u.EnumWindows(EnumProc(cb), 0)
    aborting = os.path.exists(ABORT_FLAG)
    for hdlg in dialogs:
        buttons, body = inspect_dialog(hdlg)
        if not ARI.issubset(buttons):
            continue
        if aborting and "abort" in buttons:
            u.SendMessageW(buttons["abort"], BM_CLICK, 0, 0)
            with contextlib.suppress(OSError):
                os.remove(ABORT_FLAG)
            log(f"ABORTED exception (abort flag set) hwnd={hdlg}")
        else:
            u.SendMessageW(buttons["ignore"], BM_CLICK, 0, 0)
            log(f"auto-Ignored A/R/I exception (hwnd={hdlg} body={body[:120]!r})")


def main():
    log("lid_watcher started")
    while True:
        try:
            scan()
        except Exception as exc:  # noqa: BLE001
            log(f"scan error: {exc!r}")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
