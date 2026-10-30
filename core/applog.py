"""App-wide logging: a rotating file audiocrate.log in the config folder (INFO and up, three files of 2 MB) and
stderr (WARNING and up). Every line carries the name of the thread that wrote it. set_detailed(True) lets DEBUG lines
into the file as well, which Settings, System and AUDIOCRATE_LOG=debug do. Callers use `from applog import log`."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os

import plat_diag
import plat_paths

_LOG_FILE = plat_paths.config_dir() / "audiocrate.log"
_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


class Tail(logging.Handler):
    """The last lines in memory, formatted like the file, so a state snapshot and the Diagnostics window can show them
    without reading the disk. `seq` counts every line ever kept, which tells a reader whether anything is new."""

    def __init__(self, keep: int = 1000):
        super().__init__(logging.INFO)
        self.lines: collections.deque = collections.deque(maxlen=keep)
        self.seq = 0

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
            self.seq += 1
        except Exception:
            pass

    def last(self, n: int) -> list[str]:
        return list(self.lines)[-n:]


def _make_logger() -> logging.Logger:
    log = logging.getLogger("audiocrate")
    if log.handlers:                 # already configured (import is idempotent)
        return log
    log.setLevel(logging.DEBUG)
    log.propagate = False
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(_FORMAT))
        log.addHandler(fh)
    except Exception:
        pass
    tail.setFormatter(logging.Formatter(_FORMAT))
    log.addHandler(tail)
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("AudioCrate %(levelname)s: %(message)s"))
    log.addHandler(sh)
    return log


tail = Tail()
log = _make_logger()

_new_record = logging.getLogRecordFactory()


def _record(*args, **kw):
    """A thread Qt started and Python met only when it called in has no name (Dummy-3); its OS name is the Qt object name."""
    r = _new_record(*args, **kw)
    if r.threadName.startswith("Dummy-"):
        r.threadName = plat_diag.current_thread_name() or r.threadName
    return r


logging.setLogRecordFactory(_record)


def set_detailed(on: bool) -> None:
    """DEBUG lines into the log file and the in-memory tail (on), or INFO and above only (off). Takes effect at once.
    AUDIOCRATE_LOG=debug keeps it on whatever the setting says."""
    if os.environ.get("AUDIOCRATE_LOG", "").lower() == "debug":
        on = True
    level = logging.DEBUG if on else logging.INFO
    log.setLevel(level)
    for h in log.handlers:
        if isinstance(h, (logging.handlers.RotatingFileHandler, Tail)):
            h.setLevel(level)
