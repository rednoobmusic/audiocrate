"""Finds the ffmpeg binary. One bundled next to the executable wins over the system one."""
from __future__ import annotations
import os
import shutil
import subprocess
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

    # last, the system PATH
    found = shutil.which("ffmpeg")
    if found:
        _resolved = found
        return _resolved

    _resolved = ""   # not found: cache the miss
    return None


def ffmpeg_available() -> bool:
    return bool(ffmpeg_path())


def probe_duration(path: Path | str) -> float:
    """Seconds according to ffprobe, or 0.0 if it cannot say.

    mutagen knows the container formats it was written for and nothing else, so
    for most of the extensions this plays it returns no length at all. What
    ffmpeg can demux, ffprobe can measure, which is nearly all of them.

    A subprocess per file, so this belongs behind a cache and on the slow path.
    Chiptunes are not covered: ffmpeg cannot decode a .fur or a .sid either, and
    those are measured by the players that can.
    """
    try:
        exe = ffmpeg_path()
        if not exe:
            return 0.0
        folder, name = os.path.split(exe)
        probe = os.path.join(folder, name.replace("ffmpeg", "ffprobe"))
        r = subprocess.run(
            [probe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
        )
        return float(r.stdout.decode().strip())
    except Exception:
        return 0.0
