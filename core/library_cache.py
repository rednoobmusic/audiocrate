"""Persistent tag cache for the music library.

Reading tags with mutagen is slow and the library re-scans whenever a watched folder changes, so
tags are cached in a small SQLite DB keyed by path and mtime. A file is only re-read when its mtime
changes.

This is the single tag source for the library panel (tag-based search) and the playback queue
(Track construction), so a file's tags are read at most once.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from artists import join_artists, split_artists

_DB_PATH = Path.home() / ".config" / "audiocrate" / "library.db"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_warned_no_mutagen = False

# Bump this whenever a change makes previously cached rows wrong. A row is only
# re-read when the file's mtime moves, so a bad reader poisons the cache for the
# life of the files it read: the old tags guard wrote a filename and two empty
# strings for anything whose tags object looked empty, and every one of those
# rows would have been served for ever. Version 3 keeps every value of the artist tag
# and the album artist, where it kept only the first value of the artist.
_SCHEMA_VERSION = 3

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
        ver = _conn.execute("PRAGMA user_version").fetchone()[0]
        if ver != _SCHEMA_VERSION:
            # Everything is re-read on demand, so throwing the rows away costs
            # one slow pass and fixes rows no mtime will ever invalidate.
            _conn.execute("DROP TABLE IF EXISTS tracks")
            _conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION:d}")
            _log.info("tag cache cleared, re-reading (schema %s to %s)",
                      ver, _SCHEMA_VERSION)
        # artist is the line shown when the row was read; artists holds the tag's own values, one per line
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS tracks ("
            "  path TEXT PRIMARY KEY, mtime REAL,"
            "  title TEXT, artist TEXT, album TEXT, duration REAL,"
            "  artists TEXT, albumartist TEXT)"
        )
        _conn.commit()
    return _conn


def _first(f, key: str, default: str = "") -> str:
    """One tag value, or the default. Never raises.

    Each key is read on its own so that a container which objects to one of
    them still gives up the rest, and its duration.
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


def _values(f, key: str) -> list[str]:
    """Every value of a tag, empty ones left out. Never raises."""
    try:
        v = f.get(key)
        return [s for s in (str(x).strip() for x in ([v] if isinstance(v, str) else v or ())) if s]
    except Exception:
        return []


def _entry(title: str, raw: list, albumartist: str, album: str, duration: float) -> dict:
    """The tags of one file as everyone else sees them. raw is what the artist tag holds; the names
    are worked out from it each time, so changing how names are split needs no new read."""
    names = split_artists(raw, albumartist)
    return {"title": title, "artist": join_artists(names), "artists": names, "albumartist": albumartist,
            "album": album, "duration": duration, "raw": raw}


def _read_tags(path: Path) -> dict:
    """Read tags straight from the file (the slow path)."""
    title, raw, albumartist, album, duration = path.stem, [], "", "", 0.0
    try:
        from mutagen import File as MFile
        f = MFile(str(path), easy=True)
    except ImportError:
        # Without mutagen every file falls back to its filename with empty artist
        # and album, so say so once.
        global _warned_no_mutagen
        if not _warned_no_mutagen:
            _warned_no_mutagen = True
            _log.error("mutagen is not installed, so no tags can be read at all")
        f = None
    except Exception:
        _log.debug("mutagen could not open %s", path.name, exc_info=True)
        f = None

    if f is not None:
        # No `if f.tags` guard in front: a container can answer for a key while
        # its tags object still looks empty, and the guard threw the whole read
        # away when it did. The info panel reads these files the same way.
        title       = _first(f, "title", path.stem)
        raw         = _values(f, "artist")
        albumartist = _first(f, "albumartist").strip()
        album       = _first(f, "album")
        try:
            info = getattr(f, "info", None)
            if info is not None and getattr(info, "length", None):
                duration = float(info.length)
        except Exception:
            pass

    if duration <= 0.0:
        # mutagen only knows the containers it was written for, a small part of
        # what this plays, and a track with no length shows an empty Time column.
        # ffprobe measures anything ffmpeg can demux. It is a subprocess, but this
        # whole function is the slow path behind the cache and runs once per file.
        try:
            from ffmpeg_util import probe_duration
            duration = probe_duration(path)
        except Exception:
            pass
    return _entry(title, raw, albumartist, album, duration)


def _from_row(row: tuple) -> dict:
    """A cached row: (title, artist, album, duration, artists, albumartist)."""
    return _entry(row[0], (row[4] or "").split("\n") if row[4] else [], row[5] or "", row[2], row[3])


def _write(tags: dict) -> tuple:
    return (tags["title"], tags["artist"], tags["album"], tags["duration"], "\n".join(tags["raw"]),
            tags["albumartist"])


def get_tags(path: Path) -> dict:
    """Tags for one file, cached and re-read only when the file's mtime changes."""
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _read_tags(p)

    key = str(p)
    with _lock:
        row = _db().execute(
            "SELECT mtime, title, artist, album, duration, artists, albumartist FROM tracks WHERE path=?",
            (key,),
        ).fetchone()
        if row and abs(row[0] - mtime) < 1e-6:
            return _from_row(row[1:])

    tags = _read_tags(p)            # read outside the lock (mutagen I/O is slow)
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO tracks(path, mtime, title, artist, album, duration, artists, albumartist)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (key, mtime, *_write(tags)),
        )
        _db().commit()
    return tags


def get_many(paths: list[Path], read_missing: bool = True) -> dict[Path, dict]:
    """Tags for many files (used by the library scan). Safe to call off-thread.

    read_missing=False answers from the cache only and leaves out any file it
    does not hold (or whose mtime has moved), so a caller on the GUI thread never
    waits on mutagen.

    Batched: one chunked SELECT for the whole list and a single commit for all
    new rows, not a query and an fsync per file.
    """
    result: dict[Path, dict] = {}
    stats: dict[str, float] = {}
    for p in paths:
        try:
            stats[str(p)] = p.stat().st_mtime
        except OSError:
            if read_missing:
                result[p] = _read_tags(p)   # unreadable: slow path, not cached

    # one SELECT per 500 paths, under the lock
    cached: dict[str, tuple] = {}
    keys = list(stats.keys())
    with _lock:
        db = _db()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in db.execute(
                f"SELECT path, mtime, title, artist, album, duration, artists, albumartist"
                f" FROM tracks WHERE path IN ({marks})", chunk):
                cached[row[0]] = row[1:]

    # mutagen reads only the misses, outside the lock
    fresh: list[tuple] = []
    for p in paths:
        key = str(p)
        if key not in stats:
            continue                    # already handled as unreadable
        row = cached.get(key)
        if row and abs(row[0] - stats[key]) < 1e-6:
            result[p] = _from_row(row[1:])
        elif read_missing:
            tags = _read_tags(p)
            result[p] = tags
            fresh.append((key, stats[key], *_write(tags)))

    # one INSERT batch and one commit for everything new
    if fresh:
        with _lock:
            db = _db()
            db.executemany(
                "INSERT OR REPLACE INTO tracks(path, mtime, title, artist, album, duration, artists, albumartist)"
                " VALUES (?,?,?,?,?,?,?,?)", fresh)
            db.commit()
    return result


def remember_duration(path: Path, seconds: float) -> None:
    """Keep a length that only the player could work out.

    A chiptune carries no duration and ffmpeg cannot decode one either, so neither mutagen nor
    ffprobe has anything to say about a .fur or a .sid. The players measure them to draw a seek bar,
    and keeping that answer fills the Time column for exactly these formats.

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
    """Drop a cached entry, for instance after a file is deleted or renamed."""
    try:
        with _lock:
            _db().execute("DELETE FROM tracks WHERE path=?", (str(path),))
            _db().commit()
    except Exception:
        pass
