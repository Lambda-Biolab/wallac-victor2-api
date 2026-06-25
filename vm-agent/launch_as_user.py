"""Launch a program as the interactive console user from a SYSTEM context.

The OEM GUI (MlrMgr) must run as the logged-on user 'lambda' (HKCU / profile
/ security-login), not as SYSTEM -- so PsExec -i (which runs as SYSTEM) makes
it spawn MlrServ and exit (doc 95). When 'lambda' has no password, the
password-based options (PsExec -u, schtasks /rp) don't apply.

Run this helper as SYSTEM (PsExec -s -d py.exe launch_as_user.py). SYSTEM has
SeTcbPrivilege, so it can WTSQueryUserToken() the active console session and
CreateProcessAsUser() the target on winsta0\\default -- no password needed.

This is the unattended autostart mechanism for the instrument microservice.
"""

import ctypes
import datetime
import sys
from ctypes import wintypes

# Target: argv[1]=exe, argv[2:]=args. Defaults to the OEM GUI manager.
if len(sys.argv) > 1:
    APP = sys.argv[1]
    ARGS = sys.argv[2:]
    CWD = None
else:
    APP = r"C:\Program Files\Wallac\Wallac1420\Program\MlrMgr.exe"
    ARGS = []
    CWD = r"C:\Program Files\Wallac\Wallac1420\Program"
OUT = r"C:\install\launch_out.txt"

TOKEN_PRIMARY = 1
SECURITY_IMPERSONATION = 2
MAXIMUM_ALLOWED = 0x02000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
# Console apps (python.exe) abort at startup with no console / std handles
# when launched via CreateProcessAsUser; give them their own console.
CREATE_NEW_CONSOLE = 0x00000010


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def log(msg):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.datetime.now().isoformat()} {msg}\n")


def main():
    open(OUT, "w").close()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    env_dll = ctypes.WinDLL("userenv", use_last_error=True)

    sid = k32.WTSGetActiveConsoleSessionId()
    log(f"active console session id = {sid}")
    if sid == 0xFFFFFFFF:
        log("no active console session")
        return 1

    htok = wintypes.HANDLE()
    if not wts.WTSQueryUserToken(wintypes.DWORD(sid), ctypes.byref(htok)):
        log(f"WTSQueryUserToken FAILED err={ctypes.get_last_error()}")
        return 1
    log("got console user token")

    adv.DuplicateTokenEx.restype = wintypes.BOOL
    hdup = wintypes.HANDLE()
    if not adv.DuplicateTokenEx(
        htok,
        MAXIMUM_ALLOWED,
        None,
        SECURITY_IMPERSONATION,
        TOKEN_PRIMARY,
        ctypes.byref(hdup),
    ):
        log(f"DuplicateTokenEx FAILED err={ctypes.get_last_error()}")
        return 1

    env = ctypes.c_void_p()
    if not env_dll.CreateEnvironmentBlock(ctypes.byref(env), hdup, False):
        log(f"CreateEnvironmentBlock failed (continuing) err={ctypes.get_last_error()}")
        env = None

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "winsta0\\default"
    pi = PROCESS_INFORMATION()

    # lpCommandLine must be a mutable buffer; convention: argv0 = program.
    cmdline = f'"{APP}"'
    if ARGS:
        cmdline += " " + " ".join(f'"{a}"' for a in ARGS)
    cmd_buf = ctypes.create_unicode_buffer(cmdline)

    adv.CreateProcessAsUserW.restype = wintypes.BOOL
    ok = adv.CreateProcessAsUserW(
        hdup,
        APP,
        cmd_buf,
        None,
        None,
        False,
        CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_CONSOLE,
        env,
        CWD,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        log(f"CreateProcessAsUserW FAILED err={ctypes.get_last_error()}")
        return 1

    log(f"launched [{cmdline}] as console user, pid={pi.dwProcessId}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
