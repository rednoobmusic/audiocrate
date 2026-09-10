"""Persistent tag cache for the music library.

Reading tags with mutagen is slow, and the library re-scans whenever a watched
folder changes — so tags are cached in a small SQLite DB keyed by path + mtime.
A file is only re-read when its mtime changes; otherwise lookups are instant.

This is the single tag source for both the library panel (tag-based search) and
the playback queue (Track construction), so a file's tags are read at most once.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_DB_PATH = Path.home() / ".config" / "audiocrate" / "library.db"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_warned_no_mutagen = False

# Bump this whenever a change makes previously cached rows wrong. A row is only
# re-read when the file's mtime moves, so a bad reader poisons the cache for the
# life of the files it read: the tags guard removed in ded2ab7 wrote a filename
# and two empty strings for anything whose tags object looked empty, and every
# one of those rows would have been served for ever.
_SCHEMA_VERSION = 2

import logs

_log = logs.get("tags")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        # Pragmas must run before any DDL opens a transaction, or WAL silently
        # fails to take effect.
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS tracks ("
            "  path TEXT PRIMARY KEY, mtime REAL,"
            "  title TEXT, artist TEXT, album TEXT, duration REAL)"
        )
        ver = _conn.execute("PRAGMA user_version").fetchone()[0]
        if ver != _SCHEMA_VERSION:
            # Everything is re-read on demand, so throwing the rows away costs
            # one slow pass and fixes rows no mtime will ever invalidate.
            _conn.execute("DELETE FROM tracks")
            _conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION:d}")
            _log.info("tag cache cleared, re-reading (schema %s to %s)",
                      ver, _SCHEMA_VERSION)
        _conn.commit()
    return _conn


def _first(f, key: str, default: str = "") -> str:
    """One tag value, or the default. Never raises.

    Each key is read on its own so that a container which objects to one of
    them still gives up the rest, and still gives up its duration. All four used
    to sit under a single try, so a single awkward key lost the lot.
    """
    try:
        v = f.get(key)
    except Exception:
        return default
    if not v:
        return default
    try:
        return str(v[0]) or default
    except Exception:
        return default


def _read_tags(path: Path) -> dict:
    """Read tags straight from the file (the slow path)."""
    title, artist, album, duration = path.stem, "", "", 0.0
    try:
        from mutagen import File as MFile
        f = MFile(str(path), easy=True)
    except ImportError:
        # Without mutagen every file falls back to its own filename, which is
        # what a title column full of filenames and empty artist and album
        # columns actually looks like. Worth saying once rather than never.
        global _warned_no_mutagen
        if not _warned_no_mutagen:
            _warned_no_mutagen = True
            _log.error("mutagen is not installed, so no tags can be read at all")
        f = None
    except Exception:
        _log.debug("mutagen could not open %s", path.name, exc_info=True)
        f = None

    if f is not None:
        # Read straight off it, with no "if f.tags" in front. The info panel
        # reads these same files that way and shows tags this did not, because
        # a container can answer for a key while its tags object still looks
        # empty, and the guard threw the whole read away when it did. A FLAC
        # landed here with nothing but its filename, which is why everything
        # ended up crammed in the title column.
        title  = _first(f, "title", path.stem)
        artist = _first(f, "artist")
        album  = _first(f, "album")
        try:
            info = getattr(f, "info", None)
            if info is not None and getattr(info, "length", None):
                duration = float(info.length)
        except Exception:
            pass

    if duration <= 0.0:
        # mutagen only knows the containers it was written for, which is a small
        # part of what this plays, and a track with no length shows an empty
        # Time column. ffprobe measures anything ffmpeg can demux. It is a
        # subprocess, but this whole function is the slow path behind the cache
        # and runs once per file.
        try:
            from ffmpeg_util import probe_duration
            duration = probe_duration(path)
        except Exception:
            pass
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def get_tags(path: Path) -> dict:
    """Tags for one file — cached, re-read only when the file's mtime changes."""
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _read_tags(p)

    key = str(p)
    with _lock:
        row = _db().execute(
            "SELECT mtime, title, artist, album, duration FROM tracks WHERE path=?",
            (key,),
        ).fetchone()
        if row and abs(row[0] - mtime) < 1e-6:
            return {"title": row[1], "artist": row[2], "album": row[3], "duration": row[4]}

    tags = _read_tags(p)            # read outside the lock (mutagen I/O is slow)
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO tracks(path, mtime, title, artist, album, duration)"
            " VALUES (?,?,?,?,?,?)",
            (key, mtime, tags["title"], tags["artist"], tags["album"], tags["duration"]),
        )
        _db().commit()
    return tags


def get_many(paths: list[Path]) -> dict[Path, dict]:
    """Tags for many files (used by the library scan). Safe to call off-thread.

    Batched: one chunked SELECT for the whole list and a single commit for all
    new rows, instead of a query + fsync per file (which made the first scan of
    a large library take minutes).
    """
    result: dict[Path, dict] = {}
    stats: dict[str, float] = {}
    for p in paths:
        try:
            stats[str(p)] = p.stat().st_mtime
        except OSError:
            result[p] = _read_tags(p)   # unreadable → slow path, not cached

    # 1) one SELECT per 500 paths, under the lock
    cached: dict[str, tuple] = {}
    keys = list(stats.keys())
    with _lock:
        db = _db()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in db.execute(
                f"SELECT path, mtime, title, artist, album, duration"
                f" FROM tracks WHERE path IN ({marks})", chunk):
                cached[row[0]] = row[1:]

    # 2) mutagen-read only the misses, outside the lock
    fresh: list[tuple] = []
    for p in paths:
        key = str(p)
        if key not in stats:
            continue                    # already handled as unreadable
        row = cached.get(key)
        if row and abs(row[0] - stats[key]) < 1e-6:
            result[p] = {"title": row[1], "artist": row[2],
                         "album": row[3], "duration": row[4]}
        else:
            tags = _read_tags(p)
            result[p] = tags
            fresh.append((key, stats[key], tags["title"], tags["artist"],
                          tags["album"], tags["duration"]))

    # 3) one INSERT batch + one commit for everything new
    if fresh:
        with _lock:
            db = _db()
            db.executemany(
                "INSERT OR REPLACE INTO tracks(path, mtime, title, artist, album, duration)"
                " VALUES (?,?,?,?,?,?)", fresh)
            db.commit()
    return result


def remember_duration(path: Path, seconds: float) -> None:
    """Keep a length that only the player could work out.

    A chiptune carries no duration and ffmpeg cannot decode one either, so
    neither mutagen nor ffprobe has anything to say about a .fur or a .sid. The
    players measure them to draw a seek bar, and that answer is worth keeping:
    without it the Time column stays empty for exactly the formats this is for.

    Only fills a gap. A length already read from the file is the better one.
    """
    if seconds is None or seconds <= 0.0:
        return
    key = str(Path(path))
    with _lock:
        db = _db()
        row = db.execute("SELECT duration FROM tracks WHERE path=?", (key,)).fetchone()
        if row is None or (row[0] or 0.0) > 0.0:
            return
        db.execute("UPDATE tracks SET duration=? WHERE path=?", (float(seconds), key))
        db.commit()


def forget(path: Path):
    """Drop a cached entry (e.g. after a file is deleted/renamed)."""
    try:
        with _lock:
            _db().execute("DELETE FROM tracks WHERE path=?", (str(path),))
            _db().commit()
    except Exception:
        pass
