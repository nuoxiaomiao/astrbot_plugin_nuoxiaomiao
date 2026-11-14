"""Isolate AstrBot runtime paths for tests before importing astrbot or main."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ISOLATED_ROOT: str | None = None


def ensure_test_env() -> str:
    """Ensure repo root is importable and ASTRBOT_ROOT points to a temp dir."""
    global _ISOLATED_ROOT
    repo_str = str(_REPO_ROOT)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    if _ISOLATED_ROOT is None:
        existing = os.environ.get("ASTRBOT_ROOT", "").strip()
        if existing and Path(existing).resolve() != _REPO_ROOT.resolve():
            _ISOLATED_ROOT = existing
        else:
            tmp_dir = tempfile.mkdtemp(prefix="nuoxiaomiao_astrbot_test_")
            os.environ["ASTRBOT_ROOT"] = tmp_dir
            _ISOLATED_ROOT = tmp_dir
            atexit.register(lambda p=tmp_dir: shutil.rmtree(p, ignore_errors=True))
    return _ISOLATED_ROOT
