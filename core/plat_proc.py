"""Child programs: how they are started, watched and ended.

Linux does what the program always did. A child gets a session of its own, so a process group; it is lowered with
nice; a group is ended with signals (interrupt, terminate, kill); the Online service lets a small shell watch for the
program's death.
Windows has no process groups to signal. A child gets a console-less process group, below normal priority when it
should be low, and a Job Object that holds it and everything it starts. Ending the job ends them all, and so does
closing its last handle, which the program's own death does. The job is made right after the child is, so a
grandchild started in the few microseconds before that is not in it.
"""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

import plat_paths

log = logging.getLogger("audiocrate.proc")

INT = signal.SIGINT
TERM = signal.SIGTERM
KILL = getattr(signal, "SIGKILL", signal.SIGTERM)       # Windows has none: ending the job is the last step there
LADDER = (INT, TERM, KILL)

_NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW
_NEW_GROUP = 0x00000200                 # CREATE_NEW_PROCESS_GROUP
_BELOW_NORMAL = 0x00004000              # BELOW_NORMAL_PRIORITY_CLASS
_KILL_ON_CLOSE = 0x2000                 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
_CTRL_BREAK = 1
_DENIED = 5                             # ERROR_ACCESS_DENIED
_WAIT_TIMEOUT = 0x102


def popen_kwargs(low: bool = False, group: bool = True) -> dict:
    """Keyword arguments for subprocess.Popen and run. group gives the child a group of its own that can be ended with
    everything it starts: a session on Linux, a process group on Windows. Windows never opens a console window for
    the child, and starts it below normal priority when low is set; Linux lowers the command itself (low_priority)."""
    if sys.platform != "win32":
        return {"start_new_session": True} if group else {}
    return {"creationflags": _NO_WINDOW | (_NEW_GROUP if group else 0) | (_BELOW_NORMAL if low else 0)}


def low_priority(args) -> list:
    """The command run under nice -n 10 where there is a nice, so a conversion does not disturb playback. On Windows
    the priority class is set when the child is made (popen_kwargs)."""
    if sys.platform != "win32" and shutil.which("nice"):
        return ["nice", "-n", "10"] + list(args)
    return list(args)


def lower_priority(p) -> None:
    """An already running child one step below this program on Linux, so the audio engine matters more."""
    if hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, p.pid, min(19, os.getpriority(os.PRIO_PROCESS, 0) + 10))
        except OSError:
            pass


def which(name: str) -> str | None:
    """Where a helper program is. Windows looks in the tools folder beside the program first, then on PATH. A program
    that comes with DLLs of its own (zxtune123) sits in a folder of that name inside the tools folder, so that they
    stay together."""
    if sys.platform == "win32":
        folder = plat_paths.tools_dir()
        for where in [folder, folder / name] if folder is not None else []:
            for candidate in (name, name + ".exe"):
                if (where / candidate).is_file():
                    return str(where / candidate)
    return shutil.which(name)


# the environment of a child

_ENV_KEEP = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
    "DESKTOP_SESSION", "KDE_SESSION_VERSION", "KDE_FULL_SESSION", "GNOME_DESKTOP_SESSION_ID",
    "DBUS_SESSION_BUS_ADDRESS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}

# Without SYSTEMROOT a Python program cannot open a socket, and the browser cookie readers need the profile folders.
_WINDOWS_KEEP = {
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}


def child_env(tmpdir: Path | None = None) -> dict:
    """A small environment for yt-dlp and friends.

    Only what they need to find programs, a proxy and the desktop's keyring, so a
    bundled library path or a stray Python variable of ours cannot break them.
    """
    if sys.platform == "win32":
        return _windows_env(tmpdir)
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.startswith("LC_")}
    path = [p for p in env.get("PATH", "").split(":") if p]
    for extra in (Path.home() / ".deno" / "bin", Path.home() / ".local" / "bin"):
        if str(extra) not in path:
            path.append(str(extra))
    env["PATH"] = ":".join(path) or "/usr/bin:/bin"
    if os.environ.get("LD_LIBRARY_PATH_ORIG"):          # set by a PyInstaller bundle
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
    env.update(PYTHONWARNINGS="ignore", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PYINSTALLER_RESET_ENVIRONMENT="1")
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def _windows_env(tmpdir) -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() in _WINDOWS_KEEP}
    path = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    tools = plat_paths.tools_dir()
    if tools is not None and tools.is_dir() and str(tools) not in path:
        path.insert(0, str(tools))
    deno = Path.home() / ".deno" / "bin"
    if str(deno) not in path:
        path.append(str(deno))
    env["PATH"] = os.pathsep.join(path)
    env.update(PYTHONWARNINGS="ignore", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               PYINSTALLER_RESET_ENVIRONMENT="1")
    if tmpdir is not None:
        env["TEMP"] = env["TMP"] = str(tmpdir)
    return env


# What a guard does: wait on its stdin, whose other end is this process. When it ends in any way, even
# SIGKILL or os._exit, the pipe closes and the guard ends the child's process group and removes the private
# folders it was given. If the child is already done the program says so and nothing is touched, since a
# finished group's number may belong to somebody else by then.
_GUARD = ('read -r x; [ "$x" = done ] && exit 0; g=$1; shift; kill -s KILL -- "-$g" 2>/dev/null; '
          '[ $# -gt 0 ] && rm -rf -- "$@"; exit 0')


class _Guard:
    """A small /bin/sh in a session of its own that outlives this process just long enough to clean up after it."""

    def __init__(self, pgid: int, private=None):
        args = ["/bin/sh", "-c", _GUARD, "sh", str(pgid)] + ([str(private)] if private else [])
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, env=child_env(), close_fds=True)

    def release(self) -> None:
        with self.lock:
            p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.stdin.write(b"done\n")
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            pass


# Windows: the Job Object

class _BasicLimits(ctypes.Structure):
    _fields_ = [("per_process_user_time", ctypes.c_int64), ("per_job_user_time", ctypes.c_int64),
                ("limit_flags", ctypes.c_uint32), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_process_limit", ctypes.c_uint32),
                ("affinity", ctypes.c_size_t), ("priority_class", ctypes.c_uint32),
                ("scheduling_class", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64), ("period_user", ctypes.c_int64),
                ("period_kernel", ctypes.c_int64), ("page_faults", ctypes.c_uint32),
                ("total_processes", ctypes.c_uint32), ("active_processes", ctypes.c_uint32),
                ("terminated_processes", ctypes.c_uint32)]


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


class ChildTree:
    """A Job Object that holds a child and everything it starts (Windows). terminate() ends them all, and closing the
    last handle does the same, so they cannot outlive the program."""

    def __init__(self, pid: int):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, u32, i32, ptr = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p
        self._set = _declare(k, "SetInformationJobObject", i32, handle, i32, ptr, u32)
        self._query = _declare(k, "QueryInformationJobObject", i32, handle, i32, ptr, u32, ptr)
        self._terminate = _declare(k, "TerminateJobObject", i32, handle, u32)
        self._close = _declare(k, "CloseHandle", i32, handle)
        self.lock = threading.Lock()
        self.handle = _declare(k, "CreateJobObjectW", handle, ptr, ctypes.c_wchar_p)(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.basic.limit_flags = _KILL_ON_CLOSE
            if not self._set(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):     # JobObjectExtendedLimitInformation
                raise ctypes.WinError(ctypes.get_last_error())
            process = _declare(k, "OpenProcess", handle, u32, i32, u32)(0x0101, 0, pid)      # PROCESS_SET_QUOTA | PROCESS_TERMINATE
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not _declare(k, "AssignProcessToJobObject", i32, handle, handle)(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._close(process)
        except OSError:
            self.close()
            raise

    def active(self) -> int:
        """How many processes of the job are still running."""
        with self.lock:
            if not self.handle:
                return 0
            info = _Accounting()
            if not self._query(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):     # JobObjectBasicAccountingInformation
                return 0
            return info.active_processes

    def terminate(self) -> None:
        with self.lock:
            if self.handle:
                self._terminate(self.handle, 1)

    def close(self) -> None:
        """Let go of the job. Whatever is still in it ends."""
        with self.lock:
            handle, self.handle = self.handle, None
        if handle:
            self._close(handle)


def contain(p) -> None:
    """Windows: put the child in a Job Object of its own, kept on p.tree. Without one (the child is already gone, or
    an older Windows already has it in a job that does not allow another) the group calls end that one child only.
    Linux: nothing, the session the child was started in is its group."""
    if sys.platform != "win32":
        return
    p.tree = None
    try:
        p.tree = ChildTree(p.pid)
    except OSError as e:
        log.warning("could not put process %d in a job: %s", p.pid, e)
        return
    weakref.finalize(p, p.tree.close)


def watch(p, private=None) -> None:
    """Make sure the child's group is ended if this program disappears, and that private (a folder that goes with the
    child) is removed. Linux starts a small shell to do both. Windows has the job end the child; a private folder a
    kill of the program leaves behind is removed by the Online service's sweep at the next start."""
    if sys.platform == "win32":
        contain(p)
        return
    try:
        p.guard = _Guard(p.pid, private)
    except OSError:
        p.guard = None


def release(p) -> None:
    """The child is done with: the guard is told so (Linux), the job is closed (Windows)."""
    if sys.platform == "win32":
        tree, p.tree = getattr(p, "tree", None), None
        if tree is not None:
            tree.close()
        return
    guard, p.guard = getattr(p, "guard", None), None
    if guard is not None:
        guard.release()


# ending a child and what it started

def group_alive(p) -> bool:
    """True while the child or anything it started is still running."""
    if p.poll() is None:
        return True
    if sys.platform == "win32":
        tree = getattr(p, "tree", None)
        return tree is not None and tree.active() > 0
    try:
        os.killpg(p.pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


_console_lock = threading.Lock()


def _break(pid: int) -> bool:
    """Ctrl+Break to the group of a child on Windows, which is how a console program is asked to stop. Only a
    process that shares the child's console can send it, and a program without a window has none of its own, so it
    joins the child's for the moment. A program that has a console (run from a terminal) cannot join another, and
    False says the polite way is not open."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    attach = _declare(k, "AttachConsole", ctypes.c_int32, ctypes.c_uint32)
    detach = _declare(k, "FreeConsole", ctypes.c_int32)
    send = _declare(k, "GenerateConsoleCtrlEvent", ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32)
    with _console_lock:
        if not attach(pid):
            return False
        try:
            return bool(send(_CTRL_BREAK, pid))
        finally:
            detach()


def signal_group(p, sig) -> None:
    """Send sig to the child's whole group. Windows has two steps: INT asks politely (Ctrl+Break), anything else, or
    an INT that cannot be sent, ends the job."""
    if sys.platform == "win32":
        if sig == INT and _break(p.pid):
            return
        tree = getattr(p, "tree", None)
        if tree is not None:
            tree.terminate()
        else:
            p.terminate()
        return
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop(procs, sigs=LADDER, grace: float = 3.0) -> None:
    """Stop whole process groups, all at once. Ask first, then insist.

    SIGINT first because yt-dlp then leaves the bytes it already wrote, and the
    next run carries on from them. Blocks for as long as the grace periods.
    """
    procs = [p for p in procs if p is not None]
    for sig in sigs:
        live = [p for p in procs if group_alive(p)]
        if not live:
            break
        for p in live:
            signal_group(p, sig)
        end = time.monotonic() + (1.0 if sig == KILL else grace)
        while time.monotonic() < end and any(group_alive(p) for p in live):
            time.sleep(0.02)
    for p in procs:
        release(p)


def kill_tree(p) -> None:
    """Kill a child started with popen_kwargs() together with everything it started."""
    if p.poll() is not None:                # gone and reaped: its number may belong to someone else now
        return
    if sys.platform == "win32":
        tree = getattr(p, "tree", None)
        if tree is not None:
            tree.terminate()
            return
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        p.kill()
    except OSError:
        pass


def is_alive(pid: int) -> bool:
    """Whether a process with this number is running."""
    if sys.platform == "win32":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = _declare(k, "OpenProcess", ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32)
        wait = _declare(k, "WaitForSingleObject", ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
        close = _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)
        handle = open_process(0x00100000 | 0x1000, 0, pid)          # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == _DENIED               # it is there, and not ours to look at
        try:
            return wait(handle, 0) == _WAIT_TIMEOUT
        finally:
            close(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# reading the pipes of a child

class PipeReader:
    """Chunks from the pipes of a child with a timeout. select() works on pipes on Linux; on Windows it does not, so
    each pipe gets a thread that reads it and a queue hands the chunks over. wait() gives (pipe, chunk) pairs, and an
    empty chunk means that pipe has ended. Close the pipes themselves only when the child is gone."""

    def __init__(self, *pipes):
        self._win = sys.platform == "win32"
        if self._win:
            self._queue = queue.Queue()
            self._live = set(pipes)
            for pipe in pipes:
                threading.Thread(target=self._pump, args=(pipe,), daemon=True, name="pipe-reader").start()
        else:
            self._sel = selectors.DefaultSelector()
            for pipe in pipes:
                self._sel.register(pipe, selectors.EVENT_READ)

    def _pump(self, pipe) -> None:
        while True:
            try:
                chunk = os.read(pipe.fileno(), 65536)
            except (OSError, ValueError):
                chunk = b""
            self._queue.put((pipe, chunk))
            if not chunk:
                return

    @property
    def live(self) -> bool:
        """Whether any pipe has not ended yet."""
        return bool(self._live) if self._win else bool(self._sel.get_map())

    def wait(self, timeout: float) -> list:
        """What arrived within timeout seconds, [] when nothing did."""
        if not self._win:
            out = []
            for key, _ in self._sel.select(timeout):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    self._sel.unregister(key.fileobj)
                out.append((key.fileobj, chunk))
            return out
        try:
            item = self._queue.get(timeout=max(timeout, 0))
        except queue.Empty:
            return []
        out = [item]
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for pipe, chunk in out:
            if not chunk:
                self._live.discard(pipe)
        return out

    def close(self) -> None:
        if not self._win:
            self._sel.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
