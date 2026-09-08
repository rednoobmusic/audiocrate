"""Lightweight app-wide logging.

Failures that used to vanish into bare `except: pass` now go here: a rotating
log file at ~/.config/audiocrate/audiocrate.log (INFO+) plus stderr (WARNING+),
so real problems are diagnosable instead of silent.

Usage:
    from applog import log
    try:
        ...
    except Exception:
        log.exception("decoding %s", path)   # full traceback to the log file
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_FILE = Path.home() / ".config" / "audiocrate" / "audiocrate.log"


def _make_logger() -> logging.Logger:
    log = logging.getLogger("audiocrate")
    if log.handlers:                 # already configured (import is idempotent)
        return log
    log.setLevel(logging.DEBUG)
    log.propagate = False
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        log.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("AudioCrate %(levelname)s: %(message)s"))
    log.addHandler(sh)
    return log


log = _make_logger()
