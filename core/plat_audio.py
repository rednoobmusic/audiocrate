"""Priority and timing for the audio thread and the program.

Linux asks for SCHED_FIFO for the feed thread, as the engine always did, and needs nothing else.
Windows has three things a player has to ask for, or the 10 ms feed tick slips when the machine is busy or the window is
minimised: the Multimedia Class Scheduler for the feed thread (MMCSS, task "Pro Audio"), an opt-out from the power
throttling that Windows 10 and 11 apply to background programs (EcoQoS, and from Windows 11 ignoring timer
resolution requests of a minimised window), and a timer tick of 1 ms. The last two are held while sound plays and
let go a few seconds after it stops (Hold), not for the whole life of the program.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import sys

_RT_PRIO = 60                   # SCHED_FIFO priority of the audio thread (PipeWire's own thread is 83)
APP_USER_MODEL_ID = "AudioCrate"        # the taskbar and the media keys group windows and sessions by this name
_THROTTLE_VERSION = 1
_THROTTLE_SPEED = 0x1           # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
_THROTTLE_TIMER = 0x4           # PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
_PROCESS_POWER_THROTTLING = 4   # PROCESS_INFORMATION_CLASS


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


class _Throttling(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("control_mask", ctypes.c_uint32), ("state_mask", ctypes.c_uint32)]


def go_realtime() -> str | None:
    """Raise the calling thread for audio and say what was done, in words for the log; None where it is not allowed.

    Linux: SCHED_FIFO for the thread, so a busy machine cannot keep it off the
    CPU while the sink runs dry. Qt asks for its highest priority when a thread
    starts, which is above the usual rtprio limit (98 for the audio group), so
    that request fails and the thread stays an ordinary one; this asks for one
    the limit allows.
    Windows: the thread joins the "Pro Audio" task of the Multimedia Class Scheduler, which keeps it running on
    time under load. Qt already starts it at the highest thread priority."""
    if sys.platform == "win32":
        return _pro_audio()
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(_RT_PRIO))
    except (AttributeError, OSError):
        return None
    return f"real-time (SCHED_FIFO {_RT_PRIO})"


def _pro_audio() -> str | None:
    try:
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        join = _declare(avrt, "AvSetMmThreadCharacteristicsW", ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p)
    except (OSError, AttributeError):
        return None
    index = ctypes.c_uint32(0)              # the task index starts at 0 and the call fills it in
    if not join("Pro Audio", ctypes.byref(index)):
        return None
    return "scheduled as Pro Audio (MMCSS)"


def power_opt_out(on: bool = True) -> bool:
    """Windows: tell the system this program must not be slowed down as a background program and must get the timer
    resolution it asks for even when its window is minimised. on=False hands both back to the system, which manages
    them as it does for any program. False where the system does not know the setting (before Windows 10 1709) or
    refuses. Linux has nothing to opt out of."""
    if sys.platform != "win32":
        return False
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        me = _declare(k, "GetCurrentProcess", ctypes.c_void_p)()
        set_info = _declare(k, "SetProcessInformation", ctypes.c_int32, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
                            ctypes.c_uint32)
    except (OSError, AttributeError):
        return False
    state = _Throttling(_THROTTLE_VERSION, (_THROTTLE_SPEED | _THROTTLE_TIMER) if on else 0, 0)    # controlled, and switched off
    return bool(set_info(me, _PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)))


@contextlib.contextmanager
def timer_resolution(ms: int = 1):
    """Windows keeps its timers on a coarse tick (about 15.6 ms) unless a program asks for finer. While the block
    runs the tick is ms milliseconds, and the request is taken back when it ends (timeBeginPeriod and timeEndPeriod
    come in pairs). Yields whether the request was accepted. Linux timers are not coarse."""
    winmm, accepted = None, False
    if sys.platform == "win32":
        try:
            winmm = ctypes.WinDLL("winmm")
            begin = _declare(winmm, "timeBeginPeriod", ctypes.c_uint32, ctypes.c_uint32)
            accepted = begin(ms) == 0
        except (OSError, AttributeError):
            winmm = None
    try:
        yield accepted
    finally:
        if accepted:
            _declare(winmm, "timeEndPeriod", ctypes.c_uint32, ctypes.c_uint32)(ms)


class Hold:
    """What a program that plays sound needs from Windows, taken while it plays and given back when it does not: the 1 ms
    timer tick and the opt-out from background throttling. Both cost the whole machine (more timer interrupts, a core that
    never slows down), which is a lot to ask of a player that sits idle or paused in the background all day.
    acquire() and release() may be called any number of times, in any order."""

    def __init__(self, ms: int = 1):
        self._ms = ms
        self._stack = None

    @property
    def held(self) -> bool:
        return self._stack is not None

    def acquire(self):
        if self._stack is not None:
            return
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(timer_resolution(self._ms))
        power_opt_out(True)

    def release(self):
        if self._stack is None:
            return
        self._stack.close()
        self._stack = None
        power_opt_out(False)


def app_identity(name: str = APP_USER_MODEL_ID) -> bool:
    """Windows: give the program an application id of its own, before any window exists. The taskbar groups the
    program's windows under it, and the system media controls and the volume flyout show the program by it. Linux names
    the program through audio_identity.py."""
    if sys.platform != "win32":
        return False
    try:
        shell = ctypes.WinDLL("shell32")
        return _declare(shell, "SetCurrentProcessExplicitAppUserModelID", ctypes.c_int32, ctypes.c_wchar_p)(name) == 0
    except (OSError, AttributeError):
        return False
