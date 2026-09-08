"""Resolve the ffmpeg binary — bundled next to the executable takes priority."""
from __future__ import annotations
import shutil
from pathlib import Path
import sys

_resolved: str | None = None


def ffmpeg_path() -> str | None:
    """Return path to ffmpeg, or None if not available anywhere."""
    global _resolved
    if _resolved is not None:
        return _resolved

    # When running as PyInstaller bundle, sys.executable is inside the AppDir
    here = Path(sys.executable).parent
    for candidate in (here / "ffmpeg", here / "ffmpeg.exe"):
        if candidate.exists():
            _resolved = str(candidate)
            return _resolved

    # Also check next to this module and the project root (dev mode / folder layout)
    here2 = Path(__file__).parent
    for base in (here2, here2.parent):
        for candidate in (base / "ffmpeg", base / "ffmpeg.exe"):
            if candidate.exists():
                _resolved = str(candidate)
                return _resolved

    # Fall back to system PATH
    found = shutil.which("ffmpeg")
    if found:
        _resolved = found
        return _resolved

    _resolved = ""   # not found — cache the miss
    return None


def ffmpeg_available() -> bool:
    return bool(ffmpeg_path())
