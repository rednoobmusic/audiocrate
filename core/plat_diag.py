"""What the diagnostics (diag.py, diag_cli.py) ask of the system.

Linux: SIGUSR1 and SIGUSR2 are the triggers, and /proc answers the questions about processes.
Windows: two named events per process are the triggers. diag.py waits on them from its watcher thread (Trigger) and
`--debug-dump` sets them (send_trigger). GetProcessTimes, QueryFullProcessImageName and psapi answer the questions
about processes. A window that is stuck inside native code holds the interpreter lock, so nothing in Python can answer
an event; faulthandler can still write every thread's stack from its own thread after a timeout, which is what
freeze_dump arms. The events live in the Local namespace of the user's session and carry only the creator's access.
"""
from __future__ import annotations

import ctypes
import faulthandler
import os
import platform
import sys
import time
from pathlib import Path

WHICH = ("stacks", "snapshot")
FREEZE_S = 4.0                  # a window that has not beaten for this long gets a raw dump of every thread
_REARM_S = 1.0                  # the heartbeat comes much more often than that, the timer is only reset this often

_WAIT_TIMEOUT = 0x102
_QUERY_LIMITED = 0x1000         # PROCESS_QUERY_LIMITED_INFORMATION
_EVENT_MODIFY = 0x0002
_THREAD_QUERY_LIMITED = 0x0800
_SNAP_THREAD = 0x4
_NO_HANDLE = ctypes.c_void_p(-1).value
_PARENT_PROCESS = 0xFFFFFFFF    # ATTACH_PARENT_PROCESS
_armed = 0.0


def uses_events() -> bool:
    """Whether the triggers are named events (Windows) and not signals."""
    return sys.platform == "win32"


def event_name(pid: int, which: str) -> str:
    return f"Local\\AudioCrate-diag-{pid}-{which}"


def how_to(pid: int) -> str:
    """The sentence that tells a person how to ask for the dumps."""
    if uses_events():
        return "main.py --debug-dump (or AudioCrate.exe --debug-dump) writes stacks and a snapshot"
    return f"kill -USR1 {pid} writes stacks, kill -USR2 writes a snapshot"


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


# the trigger

class Trigger:
    """The two events of this process (Windows). wait() blocks for up to a timeout and says which one was set. They
    reset themselves when they are seen."""

    def __init__(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, u32, i32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        create = _declare(k, "CreateEventW", handle, ctypes.c_void_p, i32, i32, ctypes.c_wchar_p)
        self._wait = _declare(k, "WaitForMultipleObjects", u32, u32, ctypes.c_void_p, i32, u32)
        self._close = _declare(k, "CloseHandle", i32, handle)
        self._handles = []
        try:
            for which in WHICH:
                h = create(None, 0, 0, event_name(os.getpid(), which))
                if not h:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handles.append(h)
        except OSError:
            self.close()
            raise
        self._array = (ctypes.c_void_p * len(self._handles))(*self._handles)

    def wait(self, timeout: float) -> list[str]:
        """The names of the events that were set, [] when none was within timeout seconds."""
        got = self._wait(len(self._handles), self._array, 0, int(max(timeout, 0) * 1000))
        if got < len(self._handles):
            return [WHICH[got]]
        if got != _WAIT_TIMEOUT:                # the call failed: do not turn the caller's loop into a busy one
            time.sleep(max(timeout, 0))
        return []

    def close(self) -> None:
        handles, self._handles = self._handles, []
        for h in handles:
            self._close(h)


def send_trigger(pid: int, which: str) -> None:
    """Ask the AudioCrate with this pid for its dumps (which is 'stacks' or 'snapshot'). OSError when it cannot be
    reached: there is no such process, or it belongs to someone else."""
    if not uses_events():
        import signal
        os.kill(pid, signal.SIGUSR1 if which == "stacks" else signal.SIGUSR2)
        return
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    open_event = _declare(k, "OpenEventW", ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p)
    set_event = _declare(k, "SetEvent", ctypes.c_int32, ctypes.c_void_p)
    close = _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)
    handle = open_event(_EVENT_MODIFY, 0, event_name(pid, which))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not set_event(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


def freeze_dump(raw) -> None:
    """Windows: arrange for every thread's stack to be written to the open file raw if this is not called again within
    FREEZE_S seconds. The heartbeat of the window calls it, so a window stuck in native code, which no Python code can
    report, still leaves its stacks. Linux has the signal handler for that."""
    global _armed
    if sys.platform != "win32" or raw is None:
        return
    now = time.monotonic()
    if now - _armed >= _REARM_S:
        _armed = now
        try:
            if os.fstat(raw.fileno()).st_size > 1 << 20:        # dumps nobody collected: keep the file small
                os.ftruncate(raw.fileno(), 0)
        except (OSError, ValueError):
            pass
        faulthandler.dump_traceback_later(FREEZE_S, repeat=False, file=raw)


def freeze_dump_off() -> None:
    global _armed
    if sys.platform == "win32":
        _armed = 0.0
        faulthandler.cancel_dump_traceback_later()


def attach_console() -> bool:
    """A windowed Windows program has no console, so sys.stdout and sys.stderr are None and `--debug-dump` printed to
    nowhere. This joins the console of the program that started it (Command Prompt, PowerShell) and opens the two
    streams on it. True when it did. A program whose output is redirected keeps it, one started from Explorer has no
    console to join, and Linux has nothing to do."""
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return False
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    if not _declare(k, "AttachConsole", ctypes.c_int32, ctypes.c_uint32)(_PARENT_PROCESS):
        return False
    # CONOUT$ is opened by name so that Python writes to it with WriteConsoleW, which keeps names outside the code page
    try:
        console = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return False
    if sys.stdout is None:
        sys.stdout = console
    if sys.stderr is None:
        sys.stderr = console
    return True


# questions about processes

class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    @property
    def value(self) -> int:
        return (self.high << 32) | self.low


class _Counters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS_EX"""
    _fields_ = [("cb", ctypes.c_uint32), ("page_faults", ctypes.c_uint32), ("peak_working_set", ctypes.c_size_t),
                ("working_set", ctypes.c_size_t), ("peak_paged_pool", ctypes.c_size_t), ("paged_pool", ctypes.c_size_t),
                ("peak_non_paged_pool", ctypes.c_size_t), ("non_paged_pool", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t), ("private", ctypes.c_size_t)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("usage", ctypes.c_uint32), ("thread", ctypes.c_uint32),
                ("owner", ctypes.c_uint32), ("base_priority", ctypes.c_int32), ("delta_priority", ctypes.c_int32),
                ("flags", ctypes.c_uint32)]


def _open_process(k, pid: int):
    open_process = _declare(k, "OpenProcess", ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32)
    return open_process(_QUERY_LIMITED, 0, pid)


def proc_start(pid: int) -> int | None:
    """When the process started, as a number that is the same for the life of that process and differs for a later
    process with the same pid: clock ticks since boot on Linux, the creation time on Windows. None when there is no
    such process."""
    if sys.platform == "win32":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = _open_process(k, pid)
        if not handle:
            return None
        try:
            made, gone, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
            times = _declare(k, "GetProcessTimes", ctypes.c_int32, ctypes.c_void_p, *([ctypes.c_void_p] * 4))
            if not times(handle, ctypes.byref(made), ctypes.byref(gone), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return None if gone.value else made.value           # an exit time means it is only held open by others
        finally:
            _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)(handle)
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def looks_like_app(pid: int) -> bool:
    """Is this pid a process of this user running AudioCrate's main.py, its compiled main.pyc or a frozen build. Windows
    cannot read another process's command line without more than this is worth, so a Python or AudioCrate program
    passes, and the events say whether it really is one."""
    if sys.platform == "win32":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = _open_process(k, pid)
        if not handle:
            return False
        try:
            buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint32(1024)
            query = _declare(k, "QueryFullProcessImageNameW", ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.c_void_p)
            if not query(handle, 0, buf, ctypes.byref(size)):
                return False
            name = os.path.basename(buf.value.replace("\\", "/")).lower()
            return name.startswith(("python", "audiocrate"))
        finally:
            _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)(handle)
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return False
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    names = [os.path.basename(a.decode("utf-8", "replace")) for a in argv if a]
    if not names:
        return False
    return (names[0].startswith("python") and any(n in ("main.py", "main.pyc") for n in names[1:6])) or names[0].startswith("audiocrate")


# the numbers of the snapshot

def _vmem(key: str) -> int:
    """kB of one field of /proc/self/status."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith(key + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def memory() -> dict:
    """The program's size and cost: resident, swapped, locked, threads, CPU seconds, and what the system can say about
    shared pages and open files. Windows has no swap or sharing figures per process, so it reports the working set,
    its peak, the private commit, the threads and the handles."""
    if sys.platform == "win32":
        return _windows_memory()
    out = {"rss_mb": round(_vmem("VmRSS") / 1024, 1), "swap_mb": round(_vmem("VmSwap") / 1024, 1),
           "locked_mb": round(_vmem("VmLck") / 1024, 1), "thread_count": _vmem("Threads"),
           "cpu_seconds": round(time.process_time(), 1)}
    try:
        for line in Path("/proc/self/smaps_rollup").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            if key in ("Pss", "Private_Dirty", "Shared_Clean"):
                out[key.lower() + "_mb"] = round(int(rest.split()[0]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["open_files"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    return out


def _windows_memory() -> dict:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    me = _declare(k, "GetCurrentProcess", ctypes.c_void_p)()
    out = {"cpu_seconds": round(time.process_time(), 1)}
    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    info = _declare(psapi, "GetProcessMemoryInfo", ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
    if info(me, ctypes.byref(counters), counters.cb):
        out.update(rss_mb=round(counters.working_set / (1 << 20), 1), peak_rss_mb=round(counters.peak_working_set / (1 << 20), 1),
                   private_mb=round(counters.private / (1 << 20), 1))
    handles = ctypes.c_uint32()
    if _declare(k, "GetProcessHandleCount", ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)(me, ctypes.byref(handles)):
        out["open_files"] = handles.value
    ids = _thread_ids(k)
    if ids:
        out["thread_count"] = len(ids)
    return out


def _thread_ids(k) -> list[int]:
    """The ids of this process's threads (a Toolhelp snapshot of all of them, which is cheap enough for a dump)."""
    snapshot = _declare(k, "CreateToolhelp32Snapshot", ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
    first = _declare(k, "Thread32First", ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)
    nxt = _declare(k, "Thread32Next", ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)
    close = _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)
    snap = snapshot(_SNAP_THREAD, 0)
    if not snap or snap == _NO_HANDLE:
        return []
    ids, entry = [], _ThreadEntry()
    entry.size = ctypes.sizeof(entry)
    try:
        ok = first(snap, ctypes.byref(entry))
        while ok:
            if entry.owner == os.getpid():
                ids.append(entry.thread)
            ok = nxt(snap, ctypes.byref(entry))
    finally:
        close(snap)
    return ids


def thread_names() -> list[str]:
    """Every thread of the process by the name the OS has for it (Qt's threads show their object name here)."""
    if sys.platform == "win32":
        try:
            return sorted(_windows_thread_names())
        except (OSError, AttributeError):
            return []
    out = []
    for tid in os.listdir("/proc/self/task"):
        try:
            out.append(Path(f"/proc/self/task/{tid}/comm").read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return sorted(out)


def current_thread_name() -> str:
    """The name the OS has for the calling thread, '' when it has none. Qt gives its threads their object names."""
    if sys.platform == "win32":
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            return _thread_description(k, _declare(k, "GetCurrentThread", ctypes.c_void_p)())
        except (OSError, AttributeError):
            return ""
    try:
        return Path("/proc/thread-self/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _windows_thread_names() -> list[str]:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    open_thread = _declare(k, "OpenThread", ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32)
    close = _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)
    out = []
    for tid in _thread_ids(k):
        name = ""
        handle = open_thread(_THREAD_QUERY_LIMITED, 0, tid)
        if handle:
            try:
                name = _thread_description(k, handle)
            finally:
                close(handle)
        out.append(name or f"thread {tid}")
    return out


def _thread_description(k, handle) -> str:
    """The name a thread gave itself (SetThreadDescription, Windows 10 1607 and later), '' when it has none."""
    try:
        get = _declare(k, "GetThreadDescription", ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)
    except AttributeError:
        return ""
    text = ctypes.c_wchar_p()
    if get(handle, ctypes.byref(text)) < 0:                # an HRESULT: any value from 0 up is a success
        return ""
    name = text.value or ""
    _declare(k, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)(ctypes.cast(text, ctypes.c_void_p))
    return name


def system_info() -> dict:
    """The system and, on Linux, the distribution, for the top of a snapshot."""
    if sys.platform == "win32":
        return {"system": " ".join(filter(None, (platform.system(), platform.release(), platform.version(), platform.machine())))}
    d = {}
    u = os.uname()
    d["system"] = f"{u.sysname} {u.release} {u.machine}"
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                d["distribution"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return d
