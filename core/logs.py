"""Readable logs, and a crash that says what it was doing.

A segfault in a Qt application prints "Segmentation fault" and nothing else,
which is true and useless. faulthandler is in the standard library and turns
that into the Python stack of every thread at the moment it died, which is the
difference between a guess and a fix.

Everything here is stdlib. Colour goes to a terminal and never to a file or a
pipe, so a redirected log stays plain text.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
import time
from pathlib import Path

LOG_DIR   = Path.home() / ".config" / "audiocrate"
CRASH_LOG = LOG_DIR / "crash.log"
RUN_LOG   = LOG_DIR / "audiocrate.log"

# The subsystems worth telling apart at a glance. A logger named for one of
# these gets its colour, so the eye can group a run without reading it.
_SUBSYSTEMS = {
    "audio":   "\033[36m",   # cyan
    "dsp":     "\033[36m",
    "player":  "\033[36m",
    "vgm":     "\033[35m",   # magenta for the chiptune side
    "fur":     "\033[35m",
    "sid":     "\033[35m",
    "ui":      "\033[34m",   # blue
    "panel":   "\033[34m",
    "fx":      "\033[34m",
    "library": "\033[32m",   # green
    "tags":    "\033[32m",
    "qt":      "\033[90m",   # grey, it is rarely ours
}

_LEVEL_COLOUR = {
    logging.DEBUG:    "\033[90m",        # grey
    logging.INFO:     "",                # whatever the terminal already is
    logging.WARNING:  "\033[33m",        # yellow
    logging.ERROR:    "\033[31m",        # red
    logging.CRITICAL: "\033[1;97;41m",   # white on red
}

_RESET = "\033[0m"
_DIM   = "\033[2m"


def _colour_ok(stream) -> bool:
    """Colour a terminal, never a file, a pipe or a NO_COLOR environment."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("AUDIOCRATE_COLOR") == "0":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class _Formatter(logging.Formatter):
    """Fixed width columns, so a run reads down rather than across.

    time  level  subsystem  message
    """

    def __init__(self, colour: bool):
        super().__init__()
        self._colour = colour

    def format(self, record: logging.LogRecord) -> str:
        when = time.strftime("%H:%M:%S", time.localtime(record.created))
        ms   = int(record.msecs)
        head = record.name.split(".")[0][:9]
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)

        stamp = f"{when}.{ms:03d}"
        if not self._colour:
            return f"{stamp}  {record.levelname:<8} {head:<9} {text}"

        lc = _LEVEL_COLOUR.get(record.levelno, "")
        sc = _SUBSYSTEMS.get(head, "")
        # INFO has no colour of its own, so it gets no escape at all rather
        # than an opening code it never opened and a reset for nothing.
        level = f"{lc}{record.levelname:<8}{_RESET}" if lc else f"{record.levelname:<8}"
        body  = f"{lc}{text}{_RESET}" if lc else text
        return (f"{_DIM}{stamp}{_RESET}  {level} "
                f"{sc}{head:<9}{_RESET} {body}")


def _install_qt_bridge() -> None:
    """Send Qt's own messages through logging instead of straight to stderr.

    Qt prints things like the ffmpeg backend banner with no timestamp and no
    level, which then sits in the middle of everything else looking unrelated.
    """
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    log = logging.getLogger("qt")
    levels = {
        QtMsgType.QtDebugMsg:    logging.DEBUG,
        QtMsgType.QtInfoMsg:     logging.INFO,
        QtMsgType.QtWarningMsg:  logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg:    logging.CRITICAL,
    }

    def handler(mode, context, message):
        log.log(levels.get(mode, logging.INFO), message.strip())

    qInstallMessageHandler(handler)


def _install_excepthook() -> None:
    """An unhandled exception should land in the log, not only on stderr.

    Qt swallows exceptions raised inside slots on some builds, so anything that
    does escape is worth keeping.
    """
    log = logging.getLogger("ui")
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = hook


def setup(level: str | None = None) -> logging.Logger:
    """Turn on logging and crash reporting. Call once, as early as possible.

    AUDIOCRATE_LOG sets the level (debug, info, warning, error). AUDIOCRATE_COLOR=0
    or NO_COLOR turns colour off. Nothing here raises: a logger that cannot write
    is not a reason to refuse to start.
    """
    name = (level or os.environ.get("AUDIOCRATE_LOG") or "info").upper()
    lvl = getattr(logging, name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(lvl)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_Formatter(_colour_ok(sys.stderr)))
    root.addHandler(console)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # One run per file rather than an ever growing one. The previous run is
        # the one you want after a crash, so it is kept alongside.
        if RUN_LOG.exists():
            RUN_LOG.replace(RUN_LOG.with_suffix(".log.1"))
        f = logging.FileHandler(RUN_LOG, mode="w", encoding="utf-8")
        f.setFormatter(_Formatter(colour=False))
        root.addHandler(f)
    except OSError:
        pass          # a read only home is not worth refusing to start over

    # The important part. A segfault prints the Python stack of every thread
    # instead of the one word the shell gives you.
    #
    # faulthandler takes one destination, not several, so the choice is between
    # the terminal and a file. A terminal gets it directly, since that is where
    # you are already looking. Started from a desktop entry there is no terminal
    # to print to, so it goes to a file that outlives the process instead.
    crash_file = None
    if not _colour_ok(sys.stderr):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            crash_file = open(CRASH_LOG, "a", encoding="utf-8", buffering=1)
            crash_file.write(
                f"\n=== run started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except OSError:
            crash_file = None
    faulthandler.enable(file=crash_file or sys.stderr, all_threads=True)

    # SIGUSR1 dumps every thread without killing anything, which is what you
    # want when it has not crashed but has stopped responding:
    #     kill -USR1 $(pgrep -f 'python.*main.py')
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1,
                                  file=crash_file or sys.stderr, all_threads=True)
        except (OSError, ValueError):
            pass

    _install_qt_bridge()
    _install_excepthook()

    log = logging.getLogger("ui")
    where = CRASH_LOG if crash_file else "stderr"
    log.debug("logging at %s, crash traces to %s", name, where)
    return log


def get(name: str) -> logging.Logger:
    """A logger named for a subsystem, which is what picks its colour."""
    return logging.getLogger(name)
