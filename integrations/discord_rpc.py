from __future__ import annotations
import time
import shlex
import hashlib
import threading
import subprocess
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from player import Player
from player import extract_art

try:
    from PIL import Image, ImageOps, ImageFilter
except Exception:
    Image = ImageOps = ImageFilter = None

# pypresence ≥4.6 wants an ActivityType enum (it does `activity_type.value`); passing
# the raw int 2 raises AttributeError and silently kills every update. Use the enum
# when available, else omit it (presence falls back to the default "Playing" type).
try:
    from pypresence import ActivityType as _ActivityType
    _LISTENING = _ActivityType.LISTENING
except Exception:
    _LISTENING = None


class DiscordRPCService(QObject):
    """
    Optional Discord Rich Presence bridge.
    Uses pypresence when available; otherwise stays inactive.
    """

    # Emitted from an upload worker when art lands on the server, so we re-publish
    # with the now-fetchable large_image (queued → runs on the main thread).
    _art_ready = Signal()

    def __init__(self, player: Player, parent: QObject | None = None):
        super().__init__(parent)
        self._player = player
        self._enabled = False
        self._client_id = ""
        self._art_upload_cmd = ""
        self._uploaded: set[str] = set()    # art filenames confirmed on the server
        self._uploading: set[str] = set()
        self._rpc = None
        self._connected = False
        self._last_sent = 0.0
        self._min_update_sec = 3.0
        self._last_payload_key = None
        self._play_anchor_ts: int | None = None
        self._art_base_url = ""
        self._art_cache_dir = Path.home() / ".cache" / "audiocrate" / "discord_art"
        self._art_cache_dir.mkdir(parents=True, exist_ok=True)
        self._art_url_cache: dict[str, str] = {}

        self._defer_timer = QTimer(self)
        self._defer_timer.setSingleShot(True)
        self._defer_timer.setTimerType(Qt.PreciseTimer)
        self._defer_timer.timeout.connect(lambda: self._publish(force=True))
        self._last_pos_ms = -1

        player.track_changed.connect(lambda _: self._publish(force=True))
        player.state_changed.connect(lambda _: self._publish(force=True))
        player.position_changed.connect(self._on_position)
        self._art_ready.connect(lambda: self._publish(force=True))

    def configure(self, *, enabled: bool, client_id: str, art_base_url: str = "",
                  art_upload_cmd: str = ""):
        cid = str(client_id or "").strip()
        base = str(art_base_url or "").strip().rstrip("/")
        changed = (enabled != self._enabled) or (cid != self._client_id) or (base != self._art_base_url)
        self._enabled = bool(enabled)
        self._client_id = cid
        self._art_base_url = base
        self._art_upload_cmd = str(art_upload_cmd or "").strip()
        if not changed:
            return
        if not self._enabled or not self._client_id:
            self._clear()
            return
        self._reconnect()
        self._publish(force=True)

    def _on_position(self, _frac: float, _pos_ms: int, _dur_ms: int):
        # Seek detection: only push when timeline anchor should change.
        prev = self._last_pos_ms
        self._last_pos_ms = _pos_ms
        if prev >= 0 and abs(_pos_ms - prev) > 1800:
            self._play_anchor_ts = int(time.time() - (_pos_ms / 1000.0))
            self._publish(force=True)
            return
        # Position-only updates are unnecessary unless we need to refresh after seek.

    def _reconnect(self):
        self._clear()
        try:
            from pypresence import Presence
            rpc = Presence(self._client_id)
            rpc.connect()
            self._rpc = rpc
            self._connected = True
        except Exception:
            self._rpc = None
            self._connected = False

    def _publish(self, force: bool = False):
        if not self._enabled or not self._client_id:
            return
        if not self._connected:
            self._reconnect()
            if not self._connected:
                return
        now = time.time()
        wait = self._min_update_sec - (now - self._last_sent)
        if wait > 0:
            if force and not self._defer_timer.isActive():
                self._defer_timer.start(max(1, int(wait * 1000)))
            return

        track = self._player.current
        if track is None:
            self._clear_presence_only()
            self._last_sent = now
            self._last_payload_key = None
            return
        if not self._player.is_playing:
            # Presence starts with active playback only.
            self._clear_presence_only()
            self._last_sent = now
            self._last_payload_key = None
            return

        details = track.title or track.path.stem
        state_bits = []
        if track.artist:
            state_bits.append(track.artist)
        if track.album:
            state_bits.append(track.album)
        suffix = track.path.suffix.lower().lstrip(".")
        if suffix:
            state_bits.append(suffix.upper())
        if not state_bits:
            state_bits.append("Local file")

        payload = {
            "details": details[:128],
            "state": " - ".join(state_bits)[:128],
        }
        if _LISTENING is not None:
            payload["activity_type"] = _LISTENING   # "Listening to …"
        art_url = self._external_art_url(track.path)
        if art_url:
            payload["large_image"] = art_url
            payload["large_text"] = (track.album or track.title or track.path.stem)[:128]

        # start + end give Discord a live "X:XX remaining" countdown bar.
        if self._play_anchor_ts is None or force:
            self._play_anchor_ts = int(time.time() - (self._player.position_ms / 1000.0))
        payload["start"] = self._play_anchor_ts
        dur_ms = self._player.duration_ms
        if dur_ms > 0:
            remaining_ms = dur_ms - self._player.position_ms
            payload["end"] = int(time.time() + max(0, remaining_ms / 1000.0))

        # Exclude "end" from dedup key — it changes every second by design.
        static_key = tuple((k, v) for k, v in sorted(payload.items()) if k != "end")
        if not force and static_key == self._last_payload_key:
            return
        self._last_payload_key = static_key

        try:
            self._rpc.update(**payload)
            self._last_sent = now
        except Exception:
            self._connected = False

    def _external_art_url(self, track_path: Path) -> str | None:
        if not self._art_base_url:
            return None
        key = str(track_path)
        cached = self._art_url_cache.get(key)
        if cached:
            return cached
        filename = self._write_art_file(track_path)
        if not filename:
            return None
        # If an upload command is set, only advertise the URL once the file is
        # confirmed on the server — otherwise Discord fetches a 404 and caches it.
        if self._art_upload_cmd and filename not in self._uploaded:
            self._start_upload(filename)
            return None
        url = f"{self._art_base_url}/{filename}"
        self._art_url_cache[key] = url
        return url

    def _start_upload(self, filename: str):
        if filename in self._uploading:
            return
        self._uploading.add(filename)
        local = self._art_cache_dir / filename
        cmd = self._art_upload_cmd.replace("{file}", shlex.quote(str(local)))

        def _run():
            ok = False
            try:
                r = subprocess.run(cmd, shell=True, timeout=45,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ok = (r.returncode == 0)
            except Exception:
                ok = False
            self._uploading.discard(filename)
            if ok:
                self._uploaded.add(filename)
                self._art_ready.emit()   # re-publish now that the art is fetchable

        threading.Thread(target=_run, daemon=True).start()

    def _write_art_file(self, track_path: Path) -> str | None:
        if Image is None:
            return None
        blob = extract_art(track_path)
        if not blob:
            return None
        digest = hashlib.sha1(blob).hexdigest()[:16]
        name = f"{digest}_v2.jpg"   # _v2 = centred-on-blur render; new URL busts Discord's cache
        out_path = self._art_cache_dir / name
        if out_path.exists():
            return name
        try:
            S = 1024
            with Image.open(BytesIO(blob)) as im:
                im = im.convert("RGB")
                # Show the WHOLE cover centred (don't crop non-square art). Fill the
                # square with a blurred cover of itself so there are no empty bars.
                bg = ImageOps.fit(im, (S, S), Image.Resampling.LANCZOS)
                bg = bg.filter(ImageFilter.GaussianBlur(24))
                fg = im.copy()
                fg.thumbnail((S, S), Image.Resampling.LANCZOS)   # fit inside, keep aspect
                bg.paste(fg, ((S - fg.width) // 2, (S - fg.height) // 2))
                bg.save(out_path, format="JPEG", quality=88, optimize=True)
            return name
        except Exception:
            return None

    def _clear_presence_only(self):
        if self._connected and self._rpc is not None:
            try:
                self._rpc.clear()
            except Exception:
                self._connected = False

    def _clear(self):
        self._last_sent = 0.0
        self._last_payload_key = None
        self._play_anchor_ts = None
        if self._rpc is not None:
            try:
                self._rpc.clear()
            except Exception:
                pass
            try:
                self._rpc.close()
            except Exception:
                pass
        self._rpc = None
        self._connected = False

    def shutdown(self):
        self._defer_timer.stop()
        self._clear()
