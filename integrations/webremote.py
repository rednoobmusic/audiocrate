"""Mobile web remote — serves a phone-sized AudioCrate UI over HTTP.

Design notes:

* The HTTP server runs on its own daemon thread. `Player` lives on the GUI
  thread and is NOT thread-safe, so nothing here touches it directly:
    - reads come from `_snap`, a plain dict rebuilt on the GUI thread by a
      QTimer and swapped under a lock;
    - writes are emitted as `command` signals, which Qt queues onto the GUI
      thread (the same trick MPRIS relies on for its property setters).
  The only shared state crossing threads is that dict and a couple of ints.

* Library browsing reads `library.db` directly with a private read-only
  connection per thread. It's WAL, so concurrent reads are free and we never
  have to bother the GUI thread to list 400 tracks.

* Auth is a PIN plus a signed session cookie (HMAC-SHA256 over an expiry, i.e.
  the same construction as a JWT minus the ceremony). The PIN itself is stored
  as PBKDF2, never in the clear, and the signing secret is per-install — rotate
  it and every device is logged out. Cookies are HttpOnly + SameSite=Lax, and
  Secure whenever the request arrived over TLS.

Off by default. Enable with AUDIOCRATE_WEB=1 (or "web_remote": true in
settings.json). First run prints a generated PIN to the log.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from applog import log
from player import Player, PlayMode, extract_art

_CFG_DIR   = Path.home() / ".config" / "audiocrate"
_CFG_FILE  = _CFG_DIR / "webremote.json"
_DB_PATH   = _CFG_DIR / "library.db"
_WEBUI_DIR = Path(__file__).resolve().parent / "webui"
_WAVE_DIR  = Path.home() / ".cache" / "audiocrate" / "webwaveform"

WAVE_POINTS = 240              # bars in the phone's waveform seek bar

DEFAULT_PORT = 8770
TOKEN_TTL    = 90 * 24 * 3600      # "remember me" — 90 days
PBKDF2_ITERS = 200_000
SNAPSHOT_MS  = 400                 # GUI-thread state refresh
MAX_FAILS    = 5                   # per IP before lockout
LOCKOUT_SEC  = 300

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".map":  "application/json",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}


# ── config / crypto ───────────────────────────────────────────────────────────

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def _hash_pin(pin: str, salt: bytes) -> str:
    return _b64e(hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERS))


def load_config() -> dict:
    """Read (or create) the remote's config. Generates a PIN on first run."""
    cfg: dict = {}
    try:
        cfg = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    dirty = False
    if not cfg.get("secret"):
        cfg["secret"] = _b64e(secrets.token_bytes(32)); dirty = True
    if not cfg.get("salt"):
        cfg["salt"] = _b64e(secrets.token_bytes(16)); dirty = True
    if not cfg.get("pin_hash"):
        pin = f"{secrets.randbelow(10**6):06d}"
        cfg["pin_hash"] = _hash_pin(pin, _b64d(cfg["salt"]))
        cfg["_generated_pin"] = pin          # surfaced once, then dropped
        dirty = True
    cfg.setdefault("port", DEFAULT_PORT)
    cfg.setdefault("bind", "127.0.0.1")      # tunnel-only by default; "0.0.0.0" for LAN
    if dirty:
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    try:
        _CFG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(_CFG_FILE)
    except Exception:
        log.exception("failed to save %s", _CFG_FILE)


def set_pin(pin: str):
    """Change the PIN and invalidate every existing session."""
    cfg = load_config()
    cfg["salt"]     = _b64e(secrets.token_bytes(16))
    cfg["secret"]   = _b64e(secrets.token_bytes(32))   # rotate → logs out all devices
    cfg["pin_hash"] = _hash_pin(pin, _b64d(cfg["salt"]))
    save_config(cfg)


# ── token ─────────────────────────────────────────────────────────────────────

def _sign(payload: bytes, secret: bytes) -> str:
    return _b64e(hmac.new(secret, payload, hashlib.sha256).digest())


def make_token(secret: bytes, ttl: int = TOKEN_TTL) -> str:
    body = json.dumps({"exp": int(time.time()) + ttl, "v": 1},
                      separators=(",", ":")).encode()
    return f"{_b64e(body)}.{_sign(body, secret)}"


def check_token(token: str, secret: bytes) -> bool:
    try:
        b64body, sig = token.split(".", 1)
        body = _b64d(b64body)
        if not hmac.compare_digest(sig, _sign(body, secret)):
            return False
        return json.loads(body).get("exp", 0) > time.time()
    except Exception:
        return False


# ── the Qt-side service ───────────────────────────────────────────────────────

class WebRemote(QObject):
    """Owns the HTTP server, the state snapshot and the command queue."""

    command = Signal(str, object)          # (action, value) — queued to GUI thread

    def __init__(self, player: Player, window=None, parent: QObject | None = None):
        super().__init__(parent)
        self._player = player
        self._window = window
        self._cfg    = load_config()
        self._secret = _b64d(self._cfg["secret"])
        self._salt   = _b64d(self._cfg["salt"])
        self._lock   = threading.Lock()
        self._snap: dict = {}
        self._queue_snap: dict = {"items": [], "index": -1, "upcoming": []}
        self._queue_dirty = True
        self._fails: dict[str, list] = {}   # ip -> [count, locked_until]
        self._tls   = threading.local()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._err   = ""            # set before the first _refresh() below

        # Commands arrive on the HTTP thread; Qt hands them to the GUI thread.
        self.command.connect(self._apply, Qt.QueuedConnection)

        self._timer = QTimer(self)
        self._timer.setInterval(SNAPSHOT_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

        try:
            player.error.connect(self._on_error)
            # Rebuilding the queue view walks the play order (and may rebuild the
            # shuffle list), so it only ever happens on the GUI thread — flagged
            # here, done in _refresh.
            player.queue_changed.connect(self._mark_queue_dirty)
            player.order_changed.connect(self._mark_queue_dirty)
            player.track_changed.connect(lambda _t: self._mark_queue_dirty())
        except Exception:
            log.exception("web remote: could not connect player signals")

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        bind = os.environ.get("AUDIOCRATE_WEB_BIND", self._cfg.get("bind", "127.0.0.1"))
        port = int(os.environ.get("AUDIOCRATE_WEB_PORT", self._cfg.get("port", DEFAULT_PORT)))
        try:
            httpd = ThreadingHTTPServer((bind, port), _Handler)
        except OSError as e:
            log.error("web remote could not bind %s:%d — %s", bind, port, e)
            return False
        httpd.daemon_threads = True
        httpd.remote = self                                    # handler reaches us here
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="web-remote", daemon=True)
        self._thread.start()
        log.info("web remote listening on http://%s:%d", bind, port)
        pin = self._cfg.pop("_generated_pin", None)
        if pin:
            log.warning("web remote PIN generated: %s  (change it in Settings)", pin)
            print(f"\n  AudioCrate web remote PIN: {pin}\n", flush=True)
        return True

    def stop(self):
        self._timer.stop()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    # ── state snapshot (GUI thread) ───────────────────────────────────────────

    def _on_error(self, msg: str):
        self._err = msg or ""

    def _mark_queue_dirty(self):
        self._queue_dirty = True

    def _refresh(self):
        p = self._player
        try:
            t   = p.current
            dur = p.duration_ms
            sub_n = p.subsong_count
            snap = {
                "playing":   bool(p.is_playing),
                "has_track": t is not None,
                "title":     (t.title or t.path.stem) if t else "",
                "artist":    (t.artist or "") if t else "",
                "album":     (t.album or "") if t else "",
                "path":      str(t.path) if t else "",
                "ext":       (t.path.suffix.lower().lstrip(".") if t else ""),
                "index":     p.index,
                "pos_ms":    p.position_ms,
                "dur_ms":    dur,
                "volume":    round(float(p.volume), 3),
                "shuffle":   bool(p.shuffle),
                "mode":      int(p.mode),
                "mode_name": MODE_LABEL.get(int(p.mode), "SEQ"),
                "queue_len": len(p.queue),
                "enhance":   bool(p.enhance),
                "sleep":     bool(p.stop_after_current),
                "subsongs":  sub_n,
                "subsong":   p.current_subsong if sub_n > 1 else 0,
                "art":       _art_key(t.path) if t else "",
                "error":     self._err,
                "ts":        int(time.time() * 1000),
            }
        except Exception:
            log.exception("snapshot failed")
            return

        qsnap = None
        if self._queue_dirty:
            self._queue_dirty = False
            qsnap = self._build_queue()

        with self._lock:
            self._snap = snap
            if qsnap is not None:
                self._queue_snap = qsnap

    def _build_queue(self, limit: int = 500) -> dict:
        """Queue contents + play order. GUI thread only — `upcoming()` can
        rebuild the shuffle list as a side effect."""
        p = self._player
        try:
            tracks = list(p.queue)[:limit]
            up     = p.upcoming(limit)
            idx    = p.index
        except Exception:
            log.exception("queue snapshot failed")
            return {"items": [], "index": -1, "upcoming": []}
        items = [{
            "i": i,
            "path":   str(t.path),
            "title":  t.title or t.path.stem,
            "artist": t.artist or "",
            "album":  t.album or "",
            "art":    _art_key(t.path),
            "ext":    t.path.suffix.lower().lstrip("."),
        } for i, t in enumerate(tracks)]
        return {"items": items, "index": idx, "upcoming": up}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def queue_view(self) -> dict:
        with self._lock:
            return dict(self._queue_snap)

    # ── commands (GUI thread) ─────────────────────────────────────────────────

    def _apply(self, action: str, value):
        p = self._window._player if self._window else self._player
        try:
            if   action == "play":     p.play()
            elif action == "pause":    p.pause()
            elif action == "toggle":   p.toggle()
            elif action == "stop":     p.stop()
            elif action == "next":     p.next()
            elif action == "prev":     p.prev()
            elif action == "seek":     p.seek(max(0.0, min(1.0, float(value))))
            elif action == "seek_delta": p.seek_delta(int(value))
            elif action == "volume":   p.volume = max(0.0, min(1.0, float(value)))
            elif action == "shuffle":  p.shuffle = bool(value)
            elif action == "mode":     p.mode = int(value) % 3
            elif action == "play_index":
                i = int(value)
                if 0 <= i < len(p.queue):
                    p.play_track(i)
            elif action == "remove":
                i = int(value)
                if 0 <= i < len(p.queue):
                    p.remove(i)
            elif action == "clear":    p.clear()
            elif action == "subsong":  p.set_subsong(int(value))
            elif action == "enhance":  p.set_enhance(bool(value))
            elif action == "sleep":    p.set_stop_after_current(bool(value))
            elif action == "enqueue":
                self._enqueue([Path(x) for x in (value or [])], play=False)
            elif action == "enqueue_play":
                self._enqueue([Path(x) for x in (value or [])], play=True)
            else:
                log.warning("web remote: unknown action %r", action)
        except Exception:
            log.exception("web remote command %r failed", action)

    def _enqueue(self, paths: list, play: bool):
        """Add to the queue, going through the playlist widget when there is one
        so the desktop list stays in sync (and the session gets saved)."""
        paths = [p for p in paths if p.exists()]
        if not paths:
            return
        pl = getattr(self._window, "_playlist", None)
        if pl is not None and hasattr(pl, "add_paths"):
            pl.add_paths(paths)
        else:
            self._player.add_many(paths)
        if play:
            # The queue is unique by path, so these tracks may already be in it
            # and nothing was appended. Find where the first one actually sits
            # rather than assuming it landed at the old queue length — that
            # index is out of range whenever everything was already queued.
            target = paths[0]
            for i, t in enumerate(self._player.queue):
                if t.path == target:
                    self._player.play_track(i)
                    return
            log.warning("enqueue_play: %s not found in queue after add", target)

    # ── auth helpers (HTTP thread) ────────────────────────────────────────────

    def verify_pin(self, ip: str, pin: str) -> bool:
        now = time.time()
        with self._lock:
            rec = self._fails.get(ip)
            if rec and rec[1] > now:
                return False
        ok = hmac.compare_digest(_hash_pin(pin or "", self._salt),
                                 self._cfg.get("pin_hash", ""))
        with self._lock:
            if ok:
                self._fails.pop(ip, None)
            else:
                rec = self._fails.setdefault(ip, [0, 0.0])
                rec[0] += 1
                if rec[0] >= MAX_FAILS:
                    rec[1] = now + LOCKOUT_SEC
                    rec[0] = 0
                    log.warning("web remote: %s locked out for %ds", ip, LOCKOUT_SEC)
        return ok

    def locked_out(self, ip: str) -> int:
        with self._lock:
            rec = self._fails.get(ip)
        return max(0, int(rec[1] - time.time())) if rec else 0

    def valid_token(self, token: str) -> bool:
        return check_token(token, self._secret)

    def new_token(self) -> str:
        return make_token(self._secret)

    # ── library (HTTP thread, private sqlite conn) ─────────────────────────────

    def _db(self) -> sqlite3.Connection | None:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            if not _DB_PATH.exists():
                return None
            conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._tls.conn = conn
        return conn

    def library(self, q: str = "", limit: int = 200, offset: int = 0,
                album: str = "", artist: str = "") -> dict:
        conn = self._db()
        if conn is None:
            return {"items": [], "total": 0}
        where, args = [], []
        if q:
            where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR path LIKE ?)")
            args += [f"%{q}%"] * 4
        if album:
            where.append("album = ?"); args.append(album)
        if artist:
            where.append("artist = ?"); args.append(artist)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM tracks{sql_where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT path, title, artist, album, duration FROM tracks{sql_where}"
            f" ORDER BY artist='' , artist, album, title LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        return {"total": total, "items": [{
            "path":   r["path"],
            "title":  r["title"] or Path(r["path"]).stem,
            "artist": r["artist"] or "",
            "album":  r["album"] or "",
            "dur":    int((r["duration"] or 0) * 1000),
            "art":    _art_key(Path(r["path"])),
        } for r in rows]}

    def groups(self, kind: str, q: str = "") -> dict:
        """Album or artist list for the Library screen's drill-down."""
        conn = self._db()
        if conn is None:
            return {"items": []}
        col = "album" if kind == "albums" else "artist"
        args: list = []
        filt = f"WHERE {col} != ''"
        if q:
            filt += f" AND {col} LIKE ?"
            args.append(f"%{q}%")
        rows = conn.execute(
            f"SELECT {col} AS name, COUNT(*) AS n, MIN(artist) AS artist,"
            f" MIN(path) AS sample FROM tracks {filt} GROUP BY {col}"
            f" ORDER BY name LIMIT 500", args).fetchall()
        # `sample` is one member track — its cover stands in for the group.
        return {"items": [{
            "name":   r["name"],
            "count":  r["n"],
            "artist": r["artist"] or "",
            "path":   r["sample"],
            "art":    _art_key(Path(r["sample"])),
        } for r in rows]}

    def _sessions(self) -> list[dict]:
        """The desktop's saved playlist tabs — a list of {name, paths}, exactly
        as PlaylistPanel.save_session() writes it."""
        try:
            data = json.loads((_CFG_DIR / "playlists.json").read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict) and isinstance(d.get("paths"), list)]

    def playlists(self) -> dict:
        return {"items": [{"name": str(d.get("name") or "Playlist"),
                           "count": len(d["paths"])} for d in self._sessions()]}

    def playlist_paths(self, name: str) -> list[str]:
        for d in self._sessions():
            if str(d.get("name") or "") == name:
                return [str(p) for p in d["paths"]]
        return []

    def playlist_tracks(self, name: str) -> dict:
        """Full contents of one saved playlist, for browsing before queueing.

        Tags come from the shared library cache in one batched lookup, so
        opening a 60-track playlist doesn't re-parse 60 files.
        """
        paths = [Path(p) for p in self.playlist_paths(name)]
        if not paths:
            return {"name": name, "items": []}
        try:
            import library_cache
            tags = library_cache.get_many([p for p in paths if p.exists()])
        except Exception:
            log.exception("playlist tag lookup failed")
            tags = {}
        items = []
        for p in paths:
            t = tags.get(p) or {}
            exists = p.exists()
            items.append({
                "path":    str(p),
                "title":   (t.get("title") or p.stem),
                "artist":  t.get("artist") or "",
                "album":   t.get("album") or "",
                "dur":     int((t.get("duration") or 0) * 1000),
                "art":     _art_key(p) if exists else "",
                # Missing files stay listed and greyed, matching how the desktop
                # keeps unavailable tracks rather than silently dropping them.
                "missing": not exists,
            })
        return {"name": name, "items": items}

    # ── waveform ──────────────────────────────────────────────────────────────

    def waveform(self, path: Path, key: str, n: int = WAVE_POINTS) -> list[int] | None:
        """RMS envelope for the waveform seek bar, as n ints 0-100.

        Reuses the desktop's `_compute_waveform` so both bars are built the same
        way. A full decode costs a second or two, so results are cached on disk
        under the (path, mtime) key and are good until the file changes.
        ffmpeg can't decode chiptunes, so those return None and the phone falls
        back to the plain bar.
        """
        if not key:
            key = _art_key(path)
        cache = _WAVE_DIR / f"{key}-{n}.json"
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

        try:
            from waveform_bar import _compute_waveform
            rms = _compute_waveform(path, n)
        except Exception:
            log.exception("waveform decode failed for %s", path)
            return None
        peaks = [int(max(0.0, min(1.0, float(v))) * 100) for v in rms]
        if not any(peaks):
            return None                     # silence or a failed decode

        try:
            _WAVE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(peaks))
            tmp.replace(cache)              # same dir → atomic, unlike /tmp
        except Exception:
            log.exception("could not cache waveform to %s", cache)
        return peaks

    def theme(self) -> dict:
        """Mirror the desktop accent so the phone matches whatever theme is set."""
        accent, bg = "#8040c8", "#0e0c14"
        try:
            s = json.loads((_CFG_DIR / "settings.json").read_text(encoding="utf-8"))
            accent = s.get("accent") or accent
            bg     = s.get("bg") or bg
        except Exception:
            pass
        return {"accent": accent, "bg": bg}


MODE_LABEL = {
    PlayMode.SEQUENTIAL: "SEQ",
    PlayMode.REPEAT_ALL: "REP",
    PlayMode.REPEAT_ONE: "RP1",
}


_THUMB_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_THUMB_MAX  = 192
_THUMB_LOCK = threading.Lock()
_ALLOWED_SIZES = (96, 128, 256, 640)


def _thumbnail(data: bytes, size: int, key: str) -> tuple[bytes, str]:
    """Scale cover art down before it goes over the wire.

    Embedded covers are routinely 500 kB+; a 44 px list row does not need that,
    and a library screen would pull 20 of them. Scaled variants are cached by
    (art key, size) so the decode happens once per cover.
    """
    ck = (key or hashlib.sha1(data).hexdigest()[:16], size)
    with _THUMB_LOCK:
        hit = _THUMB_CACHE.get(ck)
        if hit is not None:
            _THUMB_CACHE.move_to_end(ck)
            return hit, "image/jpeg"

    try:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage
        img = QImage()
        if not img.loadFromData(data):
            return data, _sniff_mime(data)
        if max(img.width(), img.height()) > size:
            img = img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # Default-construct: QBuffer then owns its byte array. Passing
        # QBuffer(QByteArray()) hands it a temporary that PySide6 frees straight
        # away, and the first write segfaults.
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        ok = img.save(buf, "JPEG", 82)
        out = bytes(buf.data())
        buf.close()
        if not ok or not out:
            return data, _sniff_mime(data)
    except Exception:
        log.exception("thumbnail failed")
        return data, _sniff_mime(data)

    with _THUMB_LOCK:
        _THUMB_CACHE[ck] = out
        while len(_THUMB_CACHE) > _THUMB_MAX:
            _THUMB_CACHE.popitem(last=False)
    return out, "image/jpeg"


def _sniff_mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def _art_key(path: Path) -> str:
    """Stable per-(file, mtime) id — lets the phone cache art forever."""
    try:
        mt = path.stat().st_mtime_ns
    except OSError:
        mt = 0
    return hashlib.sha1(f"{path}|{mt}".encode()).hexdigest()[:16]


# ── HTTP ──────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = "AudioCrate"
    sys_version    = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        log.debug("web %s - %s", self.address_string(), fmt % args)

    @property
    def remote(self) -> WebRemote:
        return self.server.remote

    def _client_ip(self) -> str:
        fwd = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0])

    def _is_tls(self) -> bool:
        return (self.headers.get("X-Forwarded-Proto", "").lower() == "https")

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _authed(self) -> bool:
        return self.remote.valid_token(self._cookie("ac_session"))

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200, extra: dict | None = None):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1 << 20:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path

        if path.startswith("/api/"):
            if path == "/api/session":
                return self._json({"authed": self._authed(),
                                   "lockout": self.remote.locked_out(self._client_ip())})
            if not self._authed():
                return self._json({"error": "auth"}, 401)
            return self._api_get(path, qs)

        return self._static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        body = self._read_json()

        if path == "/api/auth":
            ip = self._client_ip()
            wait = self.remote.locked_out(ip)
            if wait:
                return self._json({"error": "locked", "retry": wait}, 429)
            if not self.remote.verify_pin(ip, str(body.get("pin", ""))):
                return self._json({"error": "bad_pin",
                                   "lockout": self.remote.locked_out(ip)}, 401)
            tok = self.remote.new_token()
            flags = f"ac_session={tok}; Path=/; Max-Age={TOKEN_TTL}; HttpOnly; SameSite=Lax"
            if self._is_tls():
                flags += "; Secure"
            return self._json({"ok": True}, 200, {"Set-Cookie": flags})

        if path == "/api/logout":
            return self._json({"ok": True}, 200,
                              {"Set-Cookie": "ac_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})

        if not self._authed():
            return self._json({"error": "auth"}, 401)

        if path == "/api/cmd":
            action = str(body.get("action", ""))
            self.remote.command.emit(action, body.get("value"))
            return self._json({"ok": True})

        if path == "/api/playlist/play":
            paths = self.remote.playlist_paths(str(body.get("name", "")))
            if not paths:
                return self._json({"error": "empty"}, 404)
            self.remote.command.emit(
                "enqueue_play" if body.get("play", True) else "enqueue", paths)
            return self._json({"ok": True, "count": len(paths)})

        return self._json({"error": "not_found"}, 404)

    # -- API ---------------------------------------------------------------

    def _api_get(self, path: str, qs: dict):
        arg = lambda k, d="": (qs.get(k, [d])[0])
        if path == "/api/state":
            return self._json(self.remote.snapshot())
        if path == "/api/queue":
            return self._json(self.remote.queue_view())
        if path == "/api/library":
            return self._json(self.remote.library(
                q=arg("q"), album=arg("album"), artist=arg("artist"),
                limit=min(500, int(arg("limit", "200") or 200)),
                offset=max(0, int(arg("offset", "0") or 0))))
        if path == "/api/albums":
            return self._json(self.remote.groups("albums", arg("q")))
        if path == "/api/artists":
            return self._json(self.remote.groups("artists", arg("q")))
        if path == "/api/playlists":
            return self._json(self.remote.playlists())
        if path == "/api/playlist":
            return self._json(self.remote.playlist_tracks(arg("name")))
        if path == "/api/theme":
            return self._json(self.remote.theme())
        if path == "/api/art":
            return self._art(arg("p"), arg("k"), arg("s"))
        if path == "/api/waveform":
            return self._waveform(arg("p"), arg("k"))
        return self._json({"error": "not_found"}, 404)

    def _waveform(self, path_str: str, key: str):
        p = Path(path_str) if path_str else None
        if p is None or not p.exists():
            snap = self.remote.snapshot()
            p = Path(snap["path"]) if snap.get("path") else None
            key = key or (snap.get("art") or "")
        if p is None or not p.exists():
            return self._json({"error": "no_track"}, 404)
        peaks = self.remote.waveform(p, key)
        if peaks is None:
            # Chiptunes and anything ffmpeg can't decode — the phone falls back
            # to the plain seek bar rather than showing an empty waveform.
            return self._json({"error": "unavailable"}, 404)
        self._send(200, json.dumps({"peaks": peaks}).encode(), "application/json",
                   cache="public, max-age=31536000, immutable" if key else "no-store")

    def _art(self, path_str: str, key: str, size_str: str):
        """Cover art. Cached hard by `key`, which changes with the file's mtime."""
        p = Path(path_str) if path_str else None
        if p is None or not p.exists():
            snap = self.remote.snapshot()
            p = Path(snap["path"]) if snap.get("path") else None
        if p is None or not p.exists():
            return self._send(404, b"", "text/plain")
        data = extract_art(p)
        if not data:
            return self._send(404, b"", "text/plain")

        try:
            size = int(size_str or 0)
        except ValueError:
            size = 0
        if size in _ALLOWED_SIZES:                  # fixed set → bounded cache
            data, ctype = _thumbnail(data, size, key)
        else:
            ctype = _sniff_mime(data)

        cache = "public, max-age=31536000, immutable" if key else "no-store"
        self._send(200, data, ctype, cache=cache)

    # -- static ------------------------------------------------------------

    def _static(self, path: str):
        index = _WEBUI_DIR / "index.html"
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (_WEBUI_DIR / rel).resolve()
        try:
            target.relative_to(_WEBUI_DIR)            # no ../ escapes
        except ValueError:
            return self._send(403, b"forbidden", "text/plain")
        if not target.is_file():
            # SPA fallback: /library/album/Foo is a client route, not a file.
            # Anything that looks like a real asset still 404s honestly.
            if "." not in Path(rel).name and index.is_file():
                return self._send(200, index.read_bytes(), _MIME[".html"],
                                  cache="no-cache")
            if rel == "index.html":
                return self._send(503, b"web UI not built - run: cd webremote-ui"
                                        b" && npm install && npm run build",
                                  "text/plain")
            return self._send(404, b"not found", "text/plain")
        ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
        cache = "no-cache" if target.suffix == ".html" else "public, max-age=3600"
        self._send(200, target.read_bytes(), ctype, cache=cache)


def install(player: Player, window=None, parent: QObject | None = None) -> WebRemote | None:
    """Create and start the remote if enabled. Returns None when it's off."""
    enabled = os.environ.get("AUDIOCRATE_WEB", "").strip()
    if enabled == "":
        try:
            enabled = json.loads((_CFG_DIR / "settings.json").read_text(
                encoding="utf-8")).get("web_remote", False)
            enabled = "1" if enabled else "0"
        except Exception:
            enabled = "0"
    if enabled in ("0", "off", "false", "no"):
        return None
    remote = WebRemote(player, window, parent)
    return remote if remote.start() else None
