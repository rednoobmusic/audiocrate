"""Shared support for native chiptune rendering: a process-wide lock, and a
size cap for the on-disk render caches.

One process-wide lock for native chiptune emulation.

libfurnace and libsidplayfp are both C/C++ emulator cores with process-global
state (Furnace keeps a singleton DivEngine; both carry static tables and logging
state). Rendering two tunes at once — which happens as soon as a background
prefetch overlaps the render the user is actually waiting for — corrupts the
heap and aborts with "free(): invalid pointer".

Serialising every native render costs nothing in practice: rendering runs at
40-60x realtime, so the queue never backs up, and the foreground render is what
the user is waiting on anyway.
"""
from __future__ import annotations

import threading
from pathlib import Path

RENDER_LOCK = threading.RLock()

# Rendered chiptunes are full uncompressed WAVs — a 20-subsong SID alone came to
# 42 MB in testing, and nothing ever deleted them. Cap the caches and drop the
# least recently used first.
CACHE_MAX_MB = 512


def prune_cache(directory: Path, max_mb: int = CACHE_MAX_MB):
    """Keep a render cache under max_mb, evicting least-recently-used first."""
    try:
        files = [(p.stat().st_atime, p.stat().st_size, p)
                 for p in directory.glob("*") if p.is_file()]
    except OSError:
        return
    total = sum(f[1] for f in files)
    limit = max_mb * 1024 * 1024
    if total <= limit:
        return
    for _atime, size, p in sorted(files):          # oldest access first
        # A render may already be gone because it was the sidecar of an earlier
        # eviction; still count its bytes as reclaimed, or the loop keeps
        # deleting past the limit.
        try:
            p.unlink(missing_ok=True)
            sidecar = p.with_suffix(".json")        # metadata next to a render
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass
        total -= size
        if total <= limit:
            break
