"""Online lyrics lookup via LRCLIB (https://lrclib.net) — free, no API key.

Returns LRC text (time-synced when available, plain otherwise). Results are
cached on disk so a track is only fetched once, and every call is network-safe
to run off the main thread (the lyrics panel already loads in a worker thread).
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


def fetch(artist: str, title: str, album: str = "", duration: int = 0) -> tuple[str, str | None]:
    """(LRC text, error) for a track. Text is "" when nothing was found; error is
    None on a definitive answer and "network" when LRCLIB couldn't be reached —
    so the UI can tell "no lyrics exist" from "you're offline". Cached on disk."""
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
        # Precise lookup first (best chance of a duration-matched synced result).
        q = {"track_name": title, "artist_name": artist}
        if album:
            q["album_name"] = album
        if duration:
            q["duration"] = int(duration)
        try:
            lrc = _best(_get(f"{_API}/api/get?" + urllib.parse.urlencode(q)))
        except Exception:
            lrc = ""
        # Fall back to a fuzzy search and take the first synced hit.
        if not lrc:
            sq = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
            results = _get(f"{_API}/api/search?" + sq)
            if isinstance(results, list):
                synced = next((r for r in results if r.get("syncedLyrics")), None)
                lrc = _best(synced or (results[0] if results else {}))
    except Exception:
        # DNS failure, timeout, HTTP 5xx… — not the same as "no lyrics exist"
        return "", "network"

    if lrc:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            key.write_text(lrc, encoding="utf-8")
        except Exception:
            pass
    return lrc, None
