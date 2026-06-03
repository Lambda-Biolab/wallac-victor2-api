import pathlib
import sys

# vm-agent/ is not a package; agent.py imports comtypes lazily so its pure
# helpers are importable on Linux for unit testing.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vm-agent"))
