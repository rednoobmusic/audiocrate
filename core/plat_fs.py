"""File system and memory calls that differ between Linux and Windows: replacing a file that something else has open,
long paths, a key for comparing paths, hidden files, free memory, the program's own size, flushing a volume, opening a
file that might be a pipe.
Linux does what the program did before these functions existed."""
from __future__ import annotations

import os
import sys
import time

_TRIES = 6                  # a replace that meets a sharing violation is tried this often ...
_PAUSE = 0.1                # ... with this many seconds between the tries, half a second in all
_NEAR_LIMIT = 240           # a path this long goes through the \\?\ form, MAX_PATH is 260 and a folder name's limit 248
_BUSY = (5, 32, 33)         # access denied, sharing violation, lock violation: what a virus scanner or indexer causes
_NOT_TAKEN = (1, 50, 87)    # invalid function, not supported, invalid parameter: a volume that does not take a flush


def long(path):
    """The path in the form that lifts Windows' 260 character limit. Linux gets the path back as it was."""
    if sys.platform != "win32":
        return path
    full = os.path.abspath(os.fspath(path))
    if full.startswith("\\\\?\\"):
        return full
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def fit(path):
    """The path in the extended-length form when it is near Windows' limit, as it is otherwise (Linux always)."""
    if sys.platform != "win32":
        return path
    return long(path) if len(os.path.abspath(path)) >= _NEAR_LIMIT else path


def _retrying(call, src, dst) -> None:
    """call(src, dst), tried again for a short while when Windows says another handle is open on one of the files."""
    src, dst = fit(src), fit(dst)
    for attempt in range(_TRIES):
        try:
            call(src, dst)
            return
        except PermissionError as e:
            if getattr(e, "winerror", None) not in _BUSY or attempt == _TRIES - 1:
                raise
            time.sleep(_PAUSE)


def replace(src, dst) -> None:
    """os.replace. On Windows a file that another handle has open (a reader, a virus scanner, the indexer) cannot be
    replaced for a moment, so the call is repeated for about half a second before the error is passed on."""
    if sys.platform != "win32":
        os.replace(src, dst)
        return
    _retrying(os.replace, src, dst)


def rename(src, dst) -> None:
    """os.rename with the same short wait as replace."""
    if sys.platform != "win32":
        os.rename(src, dst)
        return
    _retrying(os.rename, src, dst)


def path_key(path) -> str:
    """A string that two spellings of one path share, for dictionaries, databases and duplicate checks. Linux paths
    are case sensitive, so the path comes back as it is. Windows ignores case and takes both slashes: the key is
    lower case, with backslashes, dots resolved and no \\\\?\\ prefix."""
    text = os.fspath(path)
    if sys.platform != "win32":
        return text
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.normpath(text))


def is_hidden(entry) -> bool:
    """Whether a directory entry (os.DirEntry, Path or a path string) is hidden: a name that starts with a dot, and
    on Windows also a hidden or system file or folder, such as $RECYCLE.BIN."""
    name = entry.name if hasattr(entry, "name") else os.path.basename(entry)
    if name.startswith("."):
        return True
    if sys.platform != "win32":
        return False
    try:
        flags = (entry.stat() if hasattr(entry, "stat") else os.stat(entry)).st_file_attributes
    except OSError:
        return False
    return bool(flags & 0x6)                    # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def open_regular(path) -> int:
    """A descriptor for reading the file at path, for a caller that checks it is a regular file afterwards. Linux
    opens without blocking, since a plain open of a named pipe waits for a writer for ever; a Windows path cannot
    lead to a pipe."""
    if sys.platform != "win32":
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    return os.open(path, os.O_RDONLY | os.O_BINARY)


def file_id(path):
    """What a path leads to, as a value two paths of one file share, or None when it cannot be read. A Windows file
    system that gives no inode number (st_ino is 0) is told apart by the path instead."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if sys.platform == "win32" and not st.st_ino:
        return None, path_key(os.path.realpath(path))
    return st.st_dev, st.st_ino


def same_file(a, b) -> bool:
    """Whether two paths lead to one file."""
    ident = file_id(a)
    return ident is not None and ident == file_id(b)


def free_ram() -> int:
    """Bytes of memory a program can use now without swapping: MemAvailable on Linux, the available physical memory
    on Windows. 2 GiB when neither can be read."""
    if sys.platform == "win32":
        return _windows_free_ram()
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 2 << 30


def _windows_free_ram() -> int:
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32), ("total_phys", ctypes.c_uint64),
                    ("avail_phys", ctypes.c_uint64), ("total_page_file", ctypes.c_uint64),
                    ("avail_page_file", ctypes.c_uint64), ("total_virtual", ctypes.c_uint64),
                    ("avail_virtual", ctypes.c_uint64), ("avail_extended_virtual", ctypes.c_uint64)]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    try:
        if ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.avail_phys)
    except OSError:
        pass
    return 2 << 30


def rss_mb() -> float:
    """Resident size in MB (the working set on Windows), or 0.0 where it cannot be read."""
    if sys.platform == "win32":
        return _windows_rss_mb()
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def _windows_rss_mb() -> float:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("page_faults", ctypes.c_uint32), ("peak_working_set", ctypes.c_size_t),
                    ("working_set", ctypes.c_size_t), ("peak_paged_pool", ctypes.c_size_t),
                    ("paged_pool", ctypes.c_size_t), ("peak_non_paged_pool", ctypes.c_size_t),
                    ("non_paged_pool", ctypes.c_size_t), ("pagefile", ctypes.c_size_t),
                    ("peak_pagefile", ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), ctypes.c_uint32]
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.working_set / (1 << 20)
    except OSError:
        pass
    return 0.0


def sync_file(path) -> None:
    """Write out the data of a file that has been closed. Windows only flushes a handle that can write."""
    flags = os.O_RDWR | os.O_BINARY if sys.platform == "win32" else os.O_RDONLY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def flush_volume(path) -> None:
    """Write out everything pending for the volume the path is on. Linux syncs every file system, as os.sync does.
    Windows flushes the one volume when the person may open it for writing, which a standard user often may not for
    a fixed disk, and does nothing when not or when the volume does not take the call; any other failure raises OSError."""
    if sys.platform != "win32":
        os.sync()
        return
    import ctypes
    from ctypes import wintypes

    full = os.path.abspath(path)
    if full.startswith("\\\\?\\"):
        full = full[4:]
    drive = os.path.splitdrive(full)[0]
    if len(drive) != 2 or drive[1] != ":":
        return                                  # a network share has no volume to open
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW("\\\\.\\" + drive, 0xC0000000, 3, None, 3, 0, None)    # read and write, share both, existing
    if handle in (None, ctypes.c_void_p(-1).value):          # access denied is the usual reason
        return
    try:
        if not kernel32.FlushFileBuffers(handle) and ctypes.get_last_error() not in _NOT_TAKEN:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
