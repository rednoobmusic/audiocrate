"""Last.fm and Libre.fm scrobbling (the Audioscrobbler v2.0 protocol).

Reference: https://www.last.fm/api/scrobbling. Libre.fm implements the same API
at a different base URL. Ported from fooyin's C++ scrobbler plugin (GPLv3),
https://github.com/fooyin/fooyin: the same auth flow, played-threshold rule and
offline retry cache, adapted to this app's Player signals and to urllib, as in
lyrics_fetch.py.

Flow: request a token, the user approves it in the browser, exchange it for a
session key (stored locally, valid until revoked). Now-playing is pushed on
track change. A scrobble is queued once the track has been heard for 50% of its
duration (capped at 4 minutes, the standard Last.fm rule) and submitted in the
background, retried on failure so nothing is lost while offline.
"""
from __future__ import annotations

import hashlib
import json
import safe_write
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from player import Player
from played_tracker import PlayedThresholdTracker

_CACHE_DIR = Path.home() / ".config" / "audiocrate" / "scrobble_cache"
_UA = "AudioCrate/1.0"
_TIMEOUT = 10.0

_MIN_SCROBBLE_DELAY_MS = 5_000
_MIN_SCROBBLE_DELAY_ON_ERROR_MS = 30_000
_MAX_SCROBBLES_PER_REQUEST = 50

_NOW_PLAYING_REFRESH_MS = 180_000     # re-send "now playing" periodically for long tracks

# https://www.last.fm/api/errorcodes: the codes that mean the stored credentials
# or session are the problem, not a network or server hiccup
_AUTH_ERROR_CODES = {4, 9, 10, 13, 14, 15, 17, 26}

_ERROR_MESSAGES = {
    4:  "Authentication failed. Check the API key and secret",
    9:  "Session expired or was revoked. Reconnect",
    10: "Invalid API key",
    13: "Invalid method signature. Check the API secret",
    14: "Token not authorised. Approve it in the browser first",
    15: "This item is no longer available",
    17: "Login required",
    26: "API key suspended",
    29: "Rate limit exceeded. Try again later",
}


class ScrobbleAuthError(Exception):
    """The server rejected the credentials (bad or expired), as opposed to a
    network hiccup. Worth showing to the user at once instead of retrying forever."""


def _api_error_from_http(exc: "urllib.error.HTTPError") -> tuple[int, str] | None:
    """Extract (code, message) from a Last.fm/Libre.fm JSON error body, if any.
    These APIs return error details in the body even on non-2xx responses."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return None
    if "error" in body:
        code = int(body["error"])
        return code, body.get("message", _ERROR_MESSAGES.get(code, "Unknown API error"))
    return None


class _AudioScrobblerService:
    """One Audioscrobbler-protocol endpoint (Last.fm or Libre.fm)."""

    name: str = ""
    api_url: str = ""
    auth_url: str = ""

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.username = ""
        self.session_key = ""

    @property
    def is_authenticated(self) -> bool:
        return bool(self.username and self.session_key)

    # session persistence
    def _session_file(self) -> Path:
        return Path.home() / ".config" / "audiocrate" / f"scrobble_{self.name.lower()}.json"

    def save_session(self):
        try:
            f = self._session_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            safe_write.write_text(f, json.dumps({
                "username": self.username, "session_key": self.session_key,
            }))
        except Exception:
            pass

    def load_session(self):
        try:
            d = json.loads(self._session_file().read_text(encoding="utf-8"))
            self.username = d.get("username", "")
            self.session_key = d.get("session_key", "")
        except Exception:
            pass

    def logout(self):
        self.username = ""
        self.session_key = ""
        try:
            self._session_file().unlink(missing_ok=True)
        except Exception:
            pass

    # signing (MD5 of sorted key+value pairs + shared secret)
    def _sign(self, params: dict) -> str:
        data = "".join(k + v for k, v in sorted(params.items())) + self.api_secret
        return hashlib.md5(data.encode("utf-8")).hexdigest()

    def _post(self, params: dict) -> dict:
        """Signed POST to the API endpoint. Raises on network/HTTP failure."""
        full = dict(params)
        full["api_key"] = self.api_key
        if self.session_key:
            full["sk"] = self.session_key
        full["api_sig"] = self._sign(full)
        full["format"] = "json"
        body = urllib.parse.urlencode(full).encode("utf-8")
        req = urllib.request.Request(
            self.api_url, data=body, method="POST",
            headers={"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._send(req)

    def _send(self, req: "urllib.request.Request") -> dict:
        """Run a request. Credential errors (bad key, secret or session) raise
        ScrobbleAuthError so callers can tell them from a transient network or
        server problem, which is worth retrying."""
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            info = _api_error_from_http(e)
            if info is not None:
                code, message = info
                if code in _AUTH_ERROR_CODES:
                    raise ScrobbleAuthError(message) from e
                raise RuntimeError(message) from e
            raise

    def _get_token(self) -> str:
        params = {"method": "auth.gettoken", "api_key": self.api_key}
        params["api_sig"] = self._sign(params)
        params["format"] = "json"
        url = self.api_url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        data = self._send(req)
        return data.get("token", "")

    def begin_auth(self) -> str:
        """Fetch a token, open the approval page in the browser, and return the
        token (kept for the follow-up exchange once the user has approved it)."""
        token = self._get_token()
        if not token:
            raise RuntimeError("Could not get an auth token from the server")
        auth_url = self.auth_url + "?" + urllib.parse.urlencode(
            {"api_key": self.api_key, "token": token})
        webbrowser.open(auth_url)
        return token

    def exchange_token(self, token: str):
        """Exchange an approved token for a session key. Raises ScrobbleAuthError
        for a rejected/unapproved token, or another Exception on network failure."""
        params = {"method": "auth.getsession", "token": token}
        params["api_key"] = self.api_key
        params["api_sig"] = self._sign(params)
        params["format"] = "json"
        url = self.api_url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        data = self._send(req)
        session = data.get("session")
        if not session or "name" not in session or "key" not in session:
            raise ScrobbleAuthError(data.get("message", "Malformed session response"))
        self.username = session["name"]
        self.session_key = session["key"]
        self.save_session()

    # playback reporting
    def update_now_playing(self, artist: str, title: str, album: str, duration_ms: int, albumartist: str = ""):
        params = {
            "method": "track.updateNowPlaying",
            "artist": artist, "track": title,
        }
        if album:
            params["album"] = album
        if albumartist:
            params["albumArtist"] = albumartist
        if duration_ms > 0:
            params["duration"] = str(duration_ms // 1000)
        self._post(params)

    def scrobble_batch(self, items: list[dict]) -> dict:
        """items: list of {artist, title, album, duration_ms, timestamp}. Raises on
        network or HTTP failure, otherwise returns the parsed JSON response. The
        caller checks it for an 'error' key: Last.fm reports API errors with HTTP 200."""
        params = {"method": "track.scrobble"}
        for i, item in enumerate(items):
            params[f"artist[{i}]"] = item["artist"]
            params[f"track[{i}]"] = item["title"]
            params[f"timestamp[{i}]"] = str(item["timestamp"])
            if item.get("album"):
                params[f"album[{i}]"] = item["album"]
            if item.get("albumartist"):
                params[f"albumArtist[{i}]"] = item["albumartist"]
        return self._post(params)


class LastFmService(_AudioScrobblerService):
    name = "LastFM"
    api_url = "https://ws.audioscrobbler.com/2.0/"
    auth_url = "https://www.last.fm/api/auth/"


class LibreFmService(_AudioScrobblerService):
    name = "LibreFM"
    api_url = "https://libre.fm/2.0/"
    auth_url = "https://libre.fm/api/auth/"


class _ScrobbleCache:
    """A JSON-backed queue of not-yet-confirmed scrobbles for one service, so
    nothing is lost if the app closes (or the network is down) before a
    pending scrobble is submitted."""

    def __init__(self, service_name: str):
        self._path = _CACHE_DIR / f"{service_name.lower()}.json"
        self._items: list[dict] = []
        self._load()

    def _load(self):
        try:
            self._items = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._items = []

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            safe_write.write_text(self._path, json.dumps(self._items))
        except Exception:
            pass

    def add(self, artist: str, title: str, album: str, timestamp: int, albumartist: str = ""):
        self._items.append({
            "artist": artist, "title": title, "album": album, "timestamp": timestamp,
            "albumartist": albumartist,
        })
        self._save()

    def count(self) -> int:
        return len(self._items)

    def peek_batch(self, n: int) -> list[dict]:
        return self._items[:n]

    def remove(self, items: list[dict]):
        for item in items:
            try:
                self._items.remove(item)
            except ValueError:
                pass
        self._save()


class ScrobbleController(QObject):
    """Wires one or more Audioscrobbler-protocol services to the Player and
    drives the played-threshold / now-playing / retry logic for each."""

    auth_status_changed = Signal(str, bool, str)   # (service_name, ok, message)

    def __init__(self, player: Player, parent: QObject | None = None):
        super().__init__(parent)
        self._player = player
        self._services: dict[str, _AudioScrobblerService] = {
            "LastFM": LastFmService("", ""),
            "LibreFM": LibreFmService("", ""),
        }
        self._enabled: dict[str, bool] = {n: False for n in self._services}
        self._caches: dict[str, _ScrobbleCache] = {
            n: _ScrobbleCache(n) for n in self._services
        }
        self._submitting: set[str] = set()
        self._submit_error: dict[str, bool] = {n: False for n in self._services}
        self._auth_failed: dict[str, bool] = {n: False for n in self._services}
        self._pending_token: dict[str, str] = {}

        self._session_start_ms = 0
        self._tracker = PlayedThresholdTracker()

        self._submit_timer = QTimer(self)
        self._submit_timer.setSingleShot(True)
        self._submit_timer.timeout.connect(self._try_submit_all)

        self._now_playing_timer = QTimer(self)
        self._now_playing_timer.setInterval(_NOW_PLAYING_REFRESH_MS)
        self._now_playing_timer.timeout.connect(self._push_now_playing)

        player.track_changed.connect(self._on_track_changed)
        player.position_changed.connect(self._on_position)
        player.state_changed.connect(self._on_state_changed)

        for name in self._services:
            self._services[name].load_session()

    # configuration (from Settings)
    def configure(self, name: str, *, enabled: bool, api_key: str, api_secret: str):
        svc = self._services[name]
        changed = api_key.strip() != svc.api_key or api_secret.strip() != svc.api_secret
        svc.api_key = api_key.strip()
        svc.api_secret = api_secret.strip()
        self._enabled[name] = bool(enabled) and bool(svc.api_key) and bool(svc.api_secret)
        if changed:
            self._auth_failed[name] = False   # new credentials deserve a fresh try
        if self._enabled[name] and not self._auth_failed[name] and self._caches[name].count():
            self._schedule_submit(name, immediate=True)

    def is_authenticated(self, name: str) -> bool:
        return self._services[name].is_authenticated

    def username(self, name: str) -> str:
        return self._services[name].username

    def connect_service(self, name: str):
        """Begin the browser auth flow for `name`. Result arrives via
        auth_status_changed. Runs the network calls off the GUI thread."""
        svc = self._services[name]
        if not svc.api_key or not svc.api_secret:
            self.auth_status_changed.emit(name, False, "Set an API key and secret first")
            return

        def _work():
            try:
                token = svc.begin_auth()
                self._pending_token[name] = token
            except ScrobbleAuthError as e:
                self.auth_status_changed.emit(name, False, str(e))
                return
            except Exception as e:
                self.auth_status_changed.emit(name, False, f"Could not start auth: {e}")
                return
            # Last.fm and Libre.fm have no callback for desktop apps, so poll until the
            # user has approved the token in the browser, then exchange it. A
            # ScrobbleAuthError other than "not yet approved" (code 14) means the
            # credentials are bad, and waiting will not fix that.
            for _ in range(60):   # up to ~2 minutes
                time.sleep(2.0)
                try:
                    svc.exchange_token(token)
                    self._auth_failed[name] = False
                    self.auth_status_changed.emit(name, True, svc.username)
                    if self._caches[name].count():
                        QTimer.singleShot(0, lambda: self._schedule_submit(name, immediate=True))
                    return
                except ScrobbleAuthError as e:
                    if "not authori" not in str(e).lower():
                        self.auth_status_changed.emit(name, False, str(e))
                        return
                    continue
                except Exception:
                    continue   # transient network error, keep polling
            self.auth_status_changed.emit(name, False, "Timed out waiting for browser approval")

        threading.Thread(target=_work, daemon=True).start()

    def _report_auth_failure(self, name: str, message: str):
        """Report a credential failure found while reporting playback (not in the
        initial connect flow). Emitted once, since _auth_failed stops further
        attempts until the user reconfigures or reconnects."""
        if self._auth_failed.get(name):
            return   # already reported; avoid duplicate popups from a race
        self._auth_failed[name] = True
        self.auth_status_changed.emit(name, False, message)

    def disconnect_service(self, name: str):
        self._services[name].logout()
        self._auth_failed[name] = False
        self.auth_status_changed.emit(name, False, "")

    def shutdown(self):
        self._submit_timer.stop()
        self._now_playing_timer.stop()

    # playback tracking (the played-threshold rule)
    def _on_track_changed(self, track):
        self._session_start_ms = int(time.time() * 1000)
        self._tracker.reset()
        self._push_now_playing()
        if self._player.is_playing:
            self._now_playing_timer.start()
        else:
            self._now_playing_timer.stop()

    def _on_state_changed(self, playing: bool):
        if playing:
            self._now_playing_timer.start()
        else:
            self._now_playing_timer.stop()

    def _on_position(self, _frac: float, pos_ms: int, dur_ms: int):
        just_played, _delta_ms = self._tracker.update(pos_ms, dur_ms)
        if just_played:
            self._queue_scrobble()

    def _queue_scrobble(self):
        track = self._player.current
        if track is None or not track.artist or not track.title:
            return   # Last.fm requires both artist and title
        ts = self._session_start_ms // 1000
        for name, enabled in self._enabled.items():
            if not enabled:
                continue
            self._caches[name].add(track.artist, track.title, track.album, ts, track.albumartist)
            self._schedule_submit(name, immediate=True)

    def _push_now_playing(self):
        track = self._player.current
        if track is None or not track.artist or not track.title or not self._player.is_playing:
            return
        for name, enabled in self._enabled.items():
            svc = self._services[name]
            if not enabled or not svc.is_authenticated or self._auth_failed[name]:
                continue
            artist, title, album, dur = track.artist, track.title, track.album, self._player.duration_ms
            albumartist = track.albumartist

            def _work(svc=svc, artist=artist, title=title, album=album, dur=dur, name=name, aa=albumartist):
                try:
                    svc.update_now_playing(artist, title, album, dur, aa)
                except ScrobbleAuthError as e:
                    self._report_auth_failure(name, str(e))
                except Exception:
                    pass   # now-playing is best-effort; never queued/retried

            threading.Thread(target=_work, daemon=True).start()

    # submission with retry/backoff (mirrors fooyin's doDelayedSubmit)
    def _schedule_submit(self, name: str, immediate: bool = False):
        if name in self._submitting or self._auth_failed[name]:
            return
        delay = 0 if (immediate and not self._submit_error[name]) else (
            _MIN_SCROBBLE_DELAY_ON_ERROR_MS if self._submit_error[name] else _MIN_SCROBBLE_DELAY_MS
        )
        if delay == 0:
            self._submit(name)
        elif not self._submit_timer.isActive():
            self._submit_timer.start(delay)

    def _try_submit_all(self):
        for name, enabled in self._enabled.items():
            if enabled and self._caches[name].count():
                self._submit(name)

    def _submit(self, name: str):
        svc = self._services[name]
        cache = self._caches[name]
        if not svc.is_authenticated or cache.count() == 0 or name in self._submitting:
            return
        batch = cache.peek_batch(_MAX_SCROBBLES_PER_REQUEST)
        self._submitting.add(name)

        def _work():
            error = False
            auth_failed = False
            try:
                resp = svc.scrobble_batch(batch)
                if "error" in resp:
                    error = True
                else:
                    cache.remove(batch)
            except ScrobbleAuthError as e:
                error = True
                auth_failed = True
                self._report_auth_failure(name, str(e))
            except Exception:
                error = True
            self._submitting.discard(name)
            self._submit_error[name] = error
            # retrying an auth failure only hammers the API; configure() and a
            # successful connect_service() clear _auth_failed and resume
            if auth_failed:
                self._auth_failed[name] = True
            elif error:
                QTimer.singleShot(0, lambda: self._schedule_submit(name))
            elif cache.count():
                QTimer.singleShot(0, lambda: self._schedule_submit(name, immediate=True))

        threading.Thread(target=_work, daemon=True).start()
