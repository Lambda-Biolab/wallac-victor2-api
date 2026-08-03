import pathlib
import sys

_repo_root = pathlib.Path(__file__).resolve().parent.parent

# mutmut copies the project into mutants/ for mutation testing.  The copy
# includes tests/ but not vm-agent/, so __file__ resolves to
#   <cwd>/mutants/tests/conftest.py -> _repo_root = <cwd>/mutants
# which has no vm-agent/.  Detect the missing marker, walk up for
# vm-agent access, and save the mutants dir for later insertion.
_in_mutmut = not (_repo_root / "vm-agent" / "agent.py").is_file()
_mutants_dir = _repo_root if _in_mutmut else None  # saved for later insertion
if _in_mutmut:
    _repo_root = _repo_root.parent  # walk to real project root

# bridge/ is a package; add repo root so `from bridge.*` works in tests.
sys.path.insert(0, str(_repo_root))

# In mutmut mode, insert mutants/ before repo root so `from bridge.*`
# imports the MUTATED trampoline-wrapped copy (not the real source).
# Must come AFTER the real repo root insert so it lands at position 0.
if _in_mutmut and _mutants_dir is not None:
    sys.path.insert(0, str(_mutants_dir))

# vm-agent/ is not a package; agent.py imports comtypes lazily so its pure
# helpers are importable on Linux for unit testing.
sys.path.insert(0, str(_repo_root / "vm-agent"))
