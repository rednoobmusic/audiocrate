"""The media session of the desktop: MPRIS on Linux, the system media controls on Windows.

make_session(player, window) gives the main window one object with the same members whichever system it runs on:
open_files and remote_changed signals, refresh(), wrote() and shutdown().
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Signal

from applog import log

if sys.platform != "win32":
    from mpris import MPRISService


class NullSession(QObject):
    """What the window gets when a session could not be built at all: it answers every call and does nothing."""

    open_files = Signal(list)
    remote_changed = Signal()

    def refresh(self):
        pass

    def wrote(self):
        pass

    def shutdown(self):
        pass


def make_session(player, window):
    if sys.platform != "win32":
        return MPRISService(player, window)
    try:
        from smtc import SmtcSession
        return SmtcSession(player, window)
    except Exception:
        log.exception("media session: could not start, the media keys and the system controls are off")
        return NullSession(window)
