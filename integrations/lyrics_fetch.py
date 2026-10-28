"""Lyrics lookup on LRCLIB (https://lrclib.net), which needs no API key.

Returns LRC text, time-synced when available and plain otherwise. Results are
cached on disk. The calls block on the network, so run them off the GUI thread.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

_CACHE_DIR = Path.home() / ".config" / "audiocrate" / "lyrics"
_API = "https://lrclib.net"
_UA = "AudioCrate/1.0"
_TIMEOUT = 6.0


def _cache_key(artist: str, title: str, album: str, duration: int) -> Path:
    raw = f"{artist}\x1f{title}\x1f{album}\x1f{duration}".lower()
    return _CACHE_DIR / (hashlib.sha1(raw.encode("utf-8")).hexdigest() + ".lrc")


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _best(rec: dict) -> str:
    if not isinstance(rec, dict):
        return ""
    return (rec.get("syncedLyrics") or rec.get("plainLyrics") or "").strip()


def _lookup(artist: str, title: str, album: str, duration: int) -> str:
    # exact lookup first, it is the one that can match on duration
    q = {"track_name": title, "artist_name": artist}
    if album:
        q["album_name"] = album
    if duration:
        q["duration"] = int(duration)
    try:
        lrc = _best(_get(f"{_API}/api/get?" + urllib.parse.urlencode(q)))
    except Exception:
        lrc = ""
    # otherwise search and take the first synced hit
    if not lrc:
        sq = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
        results = _get(f"{_API}/api/search?" + sq)
        if isinstance(results, list):
            synced = next((r for r in results if r.get("syncedLyrics")), None)
            lrc = _best(synced or (results[0] if results else {}))
    return lrc


def fetch(artist: str, title: str, album: str = "", duration: int = 0, also=()) -> tuple[str, str | None]:
    """(LRC text, error) for a track. Text is "" when nothing was found; error is
    None on a definitive answer and "network" when LRCLIB could not be reached,
    so the UI can tell "no lyrics exist" from "offline". Cached on disk.

    artist is the name as the file's tag has it, which also keys the cache. If it finds
    nothing, each of `also` (other ways to name the artists) is tried."""
    if not title:
        return "", None
    key = _cache_key(artist, title, album, int(duration or 0))
    try:
        if key.exists():
            return key.read_text(encoding="utf-8"), None
    except Exception:
        pass

    lrc = ""
    try:
        for who in dict.fromkeys([artist, *filter(None, also)]):
            lrc = _lookup(who, title, album, duration)
            if lrc:
                break
    except Exception:
        # DNS failure, timeout or HTTP 5xx: not the same as "no lyrics exist"
        return "", "network"

    if lrc:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            key.write_text(lrc, encoding="utf-8")
        except Exception:
            pass
    return lrc, None
