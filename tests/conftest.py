import pathlib
import sys

_repo_root = pathlib.Path(__file__).resolve().parent.parent

# bridge/ is a package; add repo root so `from bridge.*` works in tests.
sys.path.insert(0, str(_repo_root))

# vm-agent/ is not a package; agent.py imports comtypes lazily so its pure
# helpers are importable on Linux for unit testing.
sys.path.insert(0, str(_repo_root / "vm-agent"))
