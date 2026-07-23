"""Import-time regression tests for the vm-agent Windows ctypes scripts.

`launch_as_user.py` and `lid_watcher.py` reference `ctypes.wintypes.*` at
module scope, but `import ctypes` does NOT auto-bind the `wintypes`
submodule (PREPR blocker). The fix adds an explicit `import ctypes.wintypes`.

These checks run on Linux without invoking any Windows APIs:
- launch_as_user defines its STARTUPINFOW / PROCESS_INFORMATION ctypes
  Structures at module scope, so a plain import exercises the wintypes
  references and would AttributeError without the fix.
- lid_watcher still touches two Windows-only ctypes accessors at module
  scope -- `ctypes.windll` (lazy bound-function accessor) and
  `ctypes.WINFUNCTYPE` -- so a plain import is impossible on Linux. We stub
  both with no-ops that accept argtypes/restype assignment (no Win32 call is
  ever made). exec'ing the module then runs the SendMessageW.argtypes /
  .restype and EnumProc = WINFUNCTYPE(...) body -- purely ctypes metadata
  setup -- which fails without the wintypes import.
"""

import ctypes
import importlib.util
import pathlib
import types

_VM_AGENT = pathlib.Path(__file__).resolve().parent.parent / "vm-agent"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _VM_AGENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_launch_as_user_imports_and_defines_wintypes_structures():
    """Import must succeed and the Structures must use ctypes.wintypes types."""
    mod = _load("launch_as_user")

    # STARTUPINFOW.cb is ctypes.wintypes.DWORD == c_ulong.
    cb_name, cb_type = mod.STARTUPINFOW._fields_[0]
    assert cb_name == "cb"
    assert cb_type is ctypes.wintypes.DWORD  # equals ctypes.c_ulong

    # PROCESS_INFORMATION fields all derive from wintypes (HANDLE/DWORD).
    pi_names = [n for n, _ in mod.PROCESS_INFORMATION._fields_]
    assert pi_names == ["hProcess", "hThread", "dwProcessId", "dwThreadId"]


def test_lid_watcher_module_body_resolves_wintypes(monkeypatch):
    """The module-level wintypes references must resolve on Linux.

    We bypass the Windows-only `ctypes.windll` accessor (line: u =
    ctypes.windll.user32) with a no-call dummy so that the subsequent
    argtypes / restype / WINFUNCTYPE setup using ctypes.wintypes types
    executes. Before the fix this raised AttributeError on
    `ctypes.wintypes.HWND`.
    """

    class _StubFunc:
        """Accepts .argtypes / .restype assignment; never called."""

    class _StubLib:
        def __init__(self):
            self._cache = {}

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return self._cache.setdefault(name, _StubFunc())

    # Linux has neither ctypes.windll nor ctypes.WINFUNCTYPE; install
    # no-ops so the module-level ctypes metadata setup runs without invoking
    # any Win32 function. cfg.argtypes/restype just store values.
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=_StubLib()), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *a, **k: object(), raising=False)

    mod = _load("lid_watcher")

    # SendMessageW.argtypes was assigned from ctypes.wintypes.* references.
    assert mod.u.SendMessageW.argtypes[0] is ctypes.wintypes.HWND
    assert mod.u.SendMessageW.restype is ctypes.wintypes.LPARAM
    # EnumProc = ctypes.WINFUNCTYPE(...) executed using the wintypes types.
    assert mod.EnumProc is not None


def test_both_sources_explicitly_import_wintypes_submodule():
    """Static regression guard: both scripts must import the wintypes submodule."""
    for fname in ("launch_as_user.py", "lid_watcher.py"):
        src = (_VM_AGENT / fname).read_text(encoding="utf-8")
        assert "import ctypes.wintypes" in src, fname
