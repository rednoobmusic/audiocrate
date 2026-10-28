"""Commodore 64 SID playback support (libsidplayfp via libsid_wrap.so).

A SID file is a 6502 program, not a recording: it has subsongs, and nothing in
it says how long any of them last. So a tune is scanned once before it plays:
rendered to PCM, given a length, then written out as a plain WAV that the normal
DSP engine plays. The duration is known when playback starts, so the seek bar
and time display are right from the first frame.

The length comes from the first of these that gives an answer:

  1. The HVSC songlength database, per subsong, looked up by MD5 through
     libsidplayfp.
  2. Automatic detection (``loop_detect``), for tunes the database does not
     know: new releases, private exports, your own music.
  3. A default length, if detection is not conclusive.

Everything is cached on disk keyed by (file, mtime, size, subsong), so a tune is
scanned once and repeat plays start at once.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import loop_detect
from native_lock import RENDER_LOCK, prune_cache

EXTS = {".sid", ".psid", ".rsid"}

_RATE = 44100
_SCAN_S = 300            # how much to render while scanning for an ending
_DEFAULT_S = 180.0       # last resort if nothing can be determined
_CACHE = Path.home() / ".cache" / "audiocrate" / "sid"

# Where a Songlengths database may live. First hit wins; the setting in
# Settings > Chiptune overrides all of these.
_DB_CANDIDATES = (
    Path.home() / ".config" / "audiocrate" / "Songlengths.md5",
    Path.home() / ".local" / "share" / "audiocrate" / "Songlengths.md5",
    Path("/usr/share/hvsc/DOCUMENTS/Songlengths.md5"),
    Path.home() / "HVSC" / "DOCUMENTS" / "Songlengths.md5",
)

_lib = None
_db_override: Path | None = None


class _Meta(ctypes.Structure):
    _fields_ = [("title", ctypes.c_char * 64),
                ("author", ctypes.c_char * 64),
                ("released", ctypes.c_char * 64),
                ("subsongs", ctypes.c_int),
                ("start_song", ctypes.c_int),
                ("is_rsid", ctypes.c_int)]


def _load_lib():
    global _lib
    if _lib is not None:
        return _lib
    for cand in (Path(__file__).resolve().parent.parent / "libsid_wrap.so",
                 Path("libsid_wrap.so")):
        try:
            lib = ctypes.CDLL(str(cand))
        except OSError:
            continue
        lib.sw_read_meta.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Meta)]
        lib.sw_read_meta.restype = ctypes.c_int
        lib.sw_db_length.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        lib.sw_db_length.restype = ctypes.c_int
        lib.sw_render.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.POINTER(ctypes.c_int16),
                                  ctypes.c_int]
        lib.sw_render.restype = ctypes.c_int
        _lib = lib
        return _lib
    return None


def available() -> bool:
    """True when SID playback is usable (the wrapper library loaded)."""
    return _load_lib() is not None


def set_database(path: str | None):
    """Point at an HVSC Songlengths.md5 (from Settings). None clears it."""
    global _db_override
    _db_override = Path(path) if path else None


def database_path() -> Path | None:
    if _db_override and _db_override.is_file():
        return _db_override
    for c in _DB_CANDIDATES:
        if c.is_file():
            return c
    return None


@dataclass
class SidInfo:
    title: str
    author: str
    released: str
    subsongs: int
    start_song: int          # 1-based


def read_info(path: Path) -> SidInfo | None:
    """Header metadata. Falls back to parsing the PSID header if the lib is absent."""
    lib = _load_lib()
    if lib is not None:
        m = _Meta()
        if lib.sw_read_meta(str(path).encode(), ctypes.byref(m)) == 0:
            dec = lambda b: b.decode("latin-1", "replace").strip()
            return SidInfo(dec(m.title) or path.stem, dec(m.author), dec(m.released),
                           max(1, m.subsongs), max(1, m.start_song))
    try:
        d = path.read_bytes()
        if d[:4] not in (b"PSID", b"RSID"):
            return None
        songs, start = struct.unpack(">HH", d[14:18])
        dec = lambda o: d[o:o + 32].split(b"\0")[0].decode("latin-1", "replace")
        return SidInfo(dec(0x16) or path.stem, dec(0x36), dec(0x56),
                       max(1, songs), max(1, start))
    except Exception:
        return None


def _key(path: Path, subsong: int) -> str:
    try:
        st = path.stat()
        raw = f"{path}|{st.st_mtime_ns}|{st.st_size}|{subsong}"
    except OSError:
        raw = f"{path}|{subsong}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _db_length_s(path: Path, subsong: int) -> float | None:
    """Length from the HVSC database, if there is one and it knows this tune."""
    lib, db = _load_lib(), database_path()
    if lib is None or db is None:
        return None
    ms = lib.sw_db_length(str(db).encode(), str(path).encode(), int(subsong))
    return ms / 1000.0 if ms and ms > 0 else None


def _render_pcm(path: Path, subsong: int, seconds: int) -> np.ndarray | None:
    """Render a subsong to interleaved stereo int16.

    Serialised against every other native chiptune render, see native_lock.
    """
    lib = _load_lib()
    if lib is None:
        return None
    frames = _RATE * seconds
    buf = (ctypes.c_int16 * (frames * 2))()
    with RENDER_LOCK:
        n = lib.sw_render(str(path).encode(), int(subsong), _RATE,
                          seconds * 1000, buf, frames)
    if n <= 0:
        return None
    return np.ctypeslib.as_array(buf, shape=(frames * 2,))[:n * 2].reshape(-1, 2).copy()


class StreamReader:
    """Pull a subsong a chunk at a time, for DspEngine.load_stream().

    Only usable when the length is already known (a database hit, or an earlier
    scan). Without it there is nothing for the seek bar and no way to know where
    to stop.
    """

    def __init__(self, path: Path, subsong: int, rate: int = _RATE):
        lib = _load_lib()
        self._lib = lib
        self._h = None
        if lib is None:
            return
        lib.sw_stream_open.restype = ctypes.c_void_p
        lib.sw_stream_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.sw_stream_read.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_int16), ctypes.c_int]
        lib.sw_stream_close.argtypes = [ctypes.c_void_p]
        with RENDER_LOCK:
            self._h = lib.sw_stream_open(str(path).encode(), int(subsong), int(rate))

    def ok(self) -> bool:
        return bool(self._h)

    def __call__(self, frames: int):
        if not self._h:
            return None
        buf = (ctypes.c_int16 * (frames * 2))()
        # Held per chunk, not for the whole tune: another render can interleave
        # between chunks without either corrupting the other.
        with RENDER_LOCK:
            n = self._lib.sw_stream_read(self._h, buf, frames)
        if n <= 0:
            return None
        return np.ctypeslib.as_array(buf, shape=(frames * 2,))[:n * 2].reshape(-1, 2).copy()

    def close(self):
        if self._h:
            with RENDER_LOCK:
                self._lib.sw_stream_close(self._h)
            self._h = None


def known_length(path: Path, subsong: int = 1) -> float | None:
    """Length without rendering anything: the database, or an earlier cached scan."""
    ms = _db_length_s(path, subsong)
    if ms:
        return ms
    meta = _CACHE / f"{_key(path, subsong)}.json"
    try:
        return float(json.loads(meta.read_text())["length"])
    except Exception:
        return None


def scan(path: Path, subsong: int = 1) -> tuple[str, float, str]:
    """Prepare a subsong for playback: render it, decide its length, cache a WAV.

    Returns (wav_path, length_seconds, source). source is "database", "detected",
    "detected~" (a loop with a weak match), "default", or "error" with an empty
    path when nothing could be rendered; a cached scan returns the source it was
    stored with.
    """
    _CACHE.mkdir(parents=True, exist_ok=True)
    k = _key(path, subsong)
    wav, meta = _CACHE / f"{k}.wav", _CACHE / f"{k}.json"
    if wav.exists() and meta.exists() and wav.stat().st_size > 1024:
        try:
            d = json.loads(meta.read_text())
            return str(wav), float(d["length"]), d.get("source", "cached")
        except Exception:
            pass

    # Ask the database first: then only the length it gives has to be rendered,
    # not the whole scan window.
    length = _db_length_s(path, subsong)
    source = "database"
    if length is not None:
        pcm = _render_pcm(path, subsong, int(min(_SCAN_S, length + 1)) + 1)
        if pcm is None or len(pcm) == 0:
            return "", 0.0, "error"
    else:
        pcm = _render_pcm(path, subsong, _SCAN_S)
        if pcm is None or len(pcm) == 0:
            return "", 0.0, "error"
        # no database entry, so find the end from the audio itself
        mono = pcm.mean(axis=1).astype(np.float32) / 32768.0
        r = loop_detect.analyze(mono, _RATE, loops=2)
        if r.reason == "full":
            length, source = _DEFAULT_S, "default"
        elif r.reason == "loop" and r.confidence < loop_detect._STRONG_MATCH:
            # a plausible loop still beats an arbitrary default, but say so
            length, source = r.length_s, "detected~"
        else:
            length, source = r.length_s, "detected"

    length = max(1.0, min(length, _SCAN_S))
    frames = int(length * _RATE)
    out = pcm[:frames]
    # short fade so a tune cut at a loop point does not end on a click
    fade = min(len(out), int(0.05 * _RATE))
    if fade > 0:
        ramp = np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
        out = out.copy()
        out[-fade:] = (out[-fade:].astype(np.float32) * ramp).astype(np.int16)

    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(out.tobytes())
    try:
        meta.write_text(json.dumps({"length": length, "source": source,
                                    "subsong": subsong}))
    except OSError:
        pass
    prune_cache(_CACHE)
    return str(wav), length, source
