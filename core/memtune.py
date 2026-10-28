"""Keeps the resident size down: a bounded source line cache, glibc allocator limits, and a way to hand freed memory back.

early() runs first in main.py, before PySide6 or numpy are imported. trim() is called after something big was freed (a song
swap, a library rebuild).

PySide6 reads the source of every module imported after it, to see whether it mentions PySide6 (shibokensupport/feature.py),
and linecache keeps all of it: about 700 files and 40 MB of strings that nothing ever asks for again. The cache below
keeps the newest few thousand lines and forgets the rest while the imports are still running, so the freed space is reused
by the next import instead of leaving holes.

glibc raises its mmap threshold every time it frees a large block, up to 32 MB, after which blocks of that size come from
the arenas and are not given back when they are freed. A fixed threshold keeps every array above 1 MB in its own mapping.
"""
from __future__ import annotations

import ctypes
import linecache
import os
import time

_M_TRIM_THRESHOLD, _M_MMAP_THRESHOLD, _M_ARENA_MAX = -1, -3, -8
_MMAP_THRESHOLD = 1 << 20
_TRIM_THRESHOLD = 4 << 20
_ARENA_MAX = 2
_LINES = 8000                       # source lines the line cache keeps
_MIN_GAP = 20.0                     # seconds between two trims

_libc = None
_last_trim = 0.0


class _Lines(dict):
    """linecache.cache that holds at most _LINES lines of source in all, dropping the oldest files first."""

    def __init__(self, other=()):
        super().__init__()
        self._n = 0
        for k, v in dict(other).items():
            self[k] = v

    @staticmethod
    def _size(v) -> int:
        return len(v[2]) if len(v) == 4 else 0

    def __setitem__(self, k, v):
        old = dict.get(self, k)
        if old is not None:
            self._n -= self._size(old)
        super().__setitem__(k, v)
        self._n += self._size(v)
        while self._n > _LINES and len(self) > 1:
            try:
                oldest = next(iter(self))
            except (RuntimeError, StopIteration):     # another thread changed it meanwhile
                break
            if oldest == k:
                break
            self.pop(oldest, None)

    def pop(self, k, *default):
        old = dict.get(self, k)
        if old is not None:
            self._n -= self._size(old)
        return super().pop(k, *default)

    def __delitem__(self, k):
        self.pop(k)

    def clear(self):
        self._n = 0
        super().clear()


def _lib():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def early() -> None:
    """Call before the heavy imports. AUDIOCRATE_NO_MEMTUNE=1 turns it off."""
    if os.environ.get("AUDIOCRATE_NO_MEMTUNE"):
        return
    linecache.cache = _Lines(linecache.cache)
    try:
        m = _lib().mallopt
        m(_M_MMAP_THRESHOLD, _MMAP_THRESHOLD)
        m(_M_TRIM_THRESHOLD, _TRIM_THRESHOLD)
        m(_M_ARENA_MAX, _ARENA_MAX)
    except (AttributeError, OSError):
        pass


def trim(force: bool = False) -> bool:
    """Gives free heap pages back to the system (malloc_trim). Skipped when called again within _MIN_GAP seconds, unless
    forced. True when it ran."""
    global _last_trim
    now = time.monotonic()
    if not force and now - _last_trim < _MIN_GAP:
        return False
    _last_trim = now
    try:
        _lib().malloc_trim(0)
    except (AttributeError, OSError):
        return False
    return True
