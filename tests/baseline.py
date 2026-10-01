"""
The package as of the pinned pre-overhaul commit, importable next to the current one.

`load_baseline()` extracts `src/proto_converter` at `BASELINE_COMMIT` from git into a
temporary directory as the package `proto_converter_974baa1`, so tests and the
benchmark can run the old and new converters side by side in one process. Returns
None when git or the commit is unavailable (e.g. an sdist), so callers can skip.
"""

import atexit
import importlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Optional

BASELINE_COMMIT = "974baa1"
BASELINE_PACKAGE = f"proto_converter_{BASELINE_COMMIT}"

_REPO = Path(__file__).resolve().parents[1]
_loaded: Optional[ModuleType] = None


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args], check=True, capture_output=True, text=True
    ).stdout


def load_baseline() -> Optional[ModuleType]:
    global _loaded
    if _loaded is not None:
        return _loaded
    try:
        files = _git("ls-tree", "-r", "--name-only", BASELINE_COMMIT, "src/proto_converter").split()
    except (OSError, subprocess.CalledProcessError):
        return None

    root = Path(tempfile.mkdtemp(prefix="proto_converter_baseline_"))
    atexit.register(shutil.rmtree, root, True)
    package = root / BASELINE_PACKAGE
    package.mkdir()
    for name in files:
        source = _git("show", f"{BASELINE_COMMIT}:{name}")
        source = re.sub(r"\bproto_converter\b(?=\.|\s+import)", BASELINE_PACKAGE, source)
        (package / Path(name).name).write_text(source, encoding="utf-8")

    sys.path.insert(0, str(root))
    _loaded = importlib.import_module(BASELINE_PACKAGE)
    return _loaded
