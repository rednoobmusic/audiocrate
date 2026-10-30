"""Year, track, disc, genre and album artist, for sorting and grouping the playlist.

library_cache keeps title, artist, album and length. The playlist also sorts and
groups on these five, so they are read the same way, once per file, and kept in a
small table of their own keyed by path and mtime. Everything here is safe to call
from a worker thread.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

import logs
import plat_fs
import plat_paths

_log = logs.get("tags")

_DB_PATH = plat_paths.config_dir() / "playlist_tags.db"
_SCHEMA = 1
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

NONE = ("", 0, 0, 0, "")     # album artist, year, track, disc, genre


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("CREATE TABLE IF NOT EXISTS extra ("
                  " path TEXT PRIMARY KEY, mtime REAL, albumartist TEXT,"
                  " year INTEGER, track INTEGER, disc INTEGER, genre TEXT)")
        if c.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA:
            c.execute("DELETE FROM extra")
            c.execute(f"PRAGMA user_version={_SCHEMA:d}")
        c.commit()
        _conn = c
    return _conn


def _first(f, key: str) -> str:
    try:
        v = f.get(key)
        return str(v[0]) if v else ""
    except Exception:
        return ""


def _int(text: str) -> int:
    """The first number in "3" or "3/12"."""
    m = re.search(r"\d+", text)
    return int(m.group()) if m else 0


def _year(text: str) -> int:
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
    return int(m.group(1)) if m else 0


def _read(path: Path) -> tuple:
    try:
        from mutagen import File
        f = File(str(path), easy=True)
    except Exception:
        _log.debug("mutagen could not open %s", path.name, exc_info=True)
        return NONE
    if f is None:
        return NONE
    date = _first(f, "date") or _first(f, "originaldate") or _first(f, "year")
    return (_first(f, "albumartist"), _year(date), _int(_first(f, "tracknumber")),
            _int(_first(f, "discnumber")), _first(f, "genre"))


def get_many(paths: list[Path]) -> dict[Path, tuple]:
    """(album artist, year, track, disc, genre) for each path. A file that cannot be
    read gets blanks and is not cached, so a drive that comes back is read then."""
    result: dict[Path, tuple] = {}
    stats: dict[str, float] = {}
    for p in paths:
        try:
            stats[plat_fs.path_key(p)] = p.stat().st_mtime
        except OSError:
            result[p] = NONE
    cached: dict[str, tuple] = {}
    keys = list(stats)
    with _lock:
        db = _db()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in db.execute("SELECT path, mtime, albumartist, year, track, disc, genre"
                                  f" FROM extra WHERE path IN ({marks})", chunk):
                cached[row[0]] = row[1:]
    fresh = []
    misses = 0
    for p in paths:
        key = plat_fs.path_key(p)
        if key not in stats:
            continue
        row = cached.get(key)
        if row and abs(row[0] - stats[key]) < 1e-6:
            result[p] = tuple(row[1:])
            continue
        vals = _read(p)
        result[p] = vals
        fresh.append((key, stats[key], *vals))
        misses += 1
        if misses % 16 == 0:
            time.sleep(0.001)         # let the GUI thread have the interpreter lock now and then
    if fresh:
        with _lock:
            db = _db()
            db.executemany("INSERT OR REPLACE INTO extra(path, mtime, albumartist, year, track, disc, genre)"
                           " VALUES (?,?,?,?,?,?,?)", fresh)
            db.commit()
    return result
