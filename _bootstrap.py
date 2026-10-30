"""Put core/, panels/, integrations/ and ui/ on sys.path so modules can import each other by flat name
(`from player import ...`). main.py imports this first; the check scripts do the same. On Windows it also picks
Qt's FFmpeg media backend, which has to be set before QtMultimedia is imported."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC_DIRS = ("core", "panels", "integrations", "ui")

for _d in _SRC_DIRS:
    _p = _ROOT / _d
    if _p.is_dir():
        sp = str(_p)
        if sp not in sys.path:
            sys.path.insert(0, sp)

if sys.platform == "win32":
    # The video sound tap (QAudioBufferOutput) exists only in the FFmpeg backend; the wheel also carries the Media Foundation one.
    os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"
