"""Windows' system media controls: the media keys on the keyboard and on headsets, the volume flyout and the lock screen card.

The program shows itself to Windows through a MediaPlayer that plays nothing. Its command manager is switched off, so the
buttons come to this program and not to the MediaPlayer, and its SystemMediaTransportControls carry the state: playing,
paused or stopped, title, artist, album and cover, position and length, shuffle and repeat. The other way, through
SystemMediaTransportControlsInterop.GetForWindow, is not open to a Python program: the winrt packages project only
GetForCurrentView, which belongs to the packaged app kind, and have nothing that turns the interface pointer an interop call
returns into an object. A MediaPlayer needs no window handle either, so a frameless window that is made again does not
lose the session.

Windows reports button presses and seek requests on a thread of its own. They are handed to the main thread with a queued
signal, and nothing in Qt is touched from the other one.

When Windows will not give a session (no WinRT, a Windows N edition without the media features, the packages missing) the
four media keys are taken with RegisterHotKey instead, on the main thread's message queue, and the program says so once.
"""
from __future__ import annotations

import ctypes
import datetime
import functools
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, QTimer, Qt, Signal
from PySide6.QtMultimedia import QMediaPlayer

import plat_paths
from applog import log
from player import PlayMode, decode_art, extract_art

_SEEK_JUMP_MS = 1500        # a position jump this big between two ticks is a seek, not playback
_TIMELINE_EVERY = 10.0      # seconds between position updates while playing; Windows runs the clock between them
_COVER_SIZE = 600           # the longest side of the picture handed to Windows
_KEEP_COVERS = 3            # picture files kept in the cache folder; Windows reads one a moment after it is set
_WM_HOTKEY = 0x0312
_MOD_NOREPEAT = 0x4000
_HOTKEY_BASE = 0x4143       # ids of the four hotkeys, in the range an application may use
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_HOTKEYS = (                # virtual key, command, how the log names it
    (0xB3, "toggle", "play/pause"),
    (0xB0, "next", "next track"),
    (0xB1, "prev", "previous track"),
    (0xB2, "stop", "stop"),
)
_MODE_NAMES = {PlayMode.SEQUENTIAL: "NONE", PlayMode.REPEAT_ALL: "LIST", PlayMode.REPEAT_ONE: "TRACK"}
_MODE_OF_NAME = {name: mode for mode, name in _MODE_NAMES.items()}
_UNSET = object()


class _Msg(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_uint32), ("x", ctypes.c_int32), ("y", ctypes.c_int32)]


def _guard(fn):
    @functools.wraps(fn)
    def run(*a):
        try:
            return fn(*a)
        except Exception:
            log.exception("media session %s", fn.__name__)
    return run


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


def _pos_ms(p) -> int:
    return p._vgm.position_ms if p._vgm_active else p.position_ms


def _dur_ms(p) -> int:
    return p._vgm.duration_ms if p._vgm_active else p.duration_ms


def _state(p) -> str:
    if p.current is None:
        return "Stopped"
    if p.is_playing:
        return "Playing"
    if p._dsp_active:
        return "Paused" if p._dsp.paused else "Stopped"
    if p._fur_active:
        return "Paused" if p._fur_resume_frac > 0 else "Stopped"
    if p._vgm_active:
        return "Paused"
    return "Paused" if p._media.playbackState() == QMediaPlayer.PlaybackState.PausedState else "Stopped"


def _can_next(p) -> bool:
    if not p.queue:
        return False
    if p.mode == PlayMode.REPEAT_ONE:
        return len(p.queue) > 1
    return p.index < 0 or p._next_queue_idx() >= 0


def _load_winrt() -> SimpleNamespace:
    """The few classes of the winrt packages this module uses. Importing them is what fails where there is no WinRT."""
    from winrt.windows.foundation import Uri
    from winrt.windows.media import (MediaPlaybackAutoRepeatMode, MediaPlaybackStatus, MediaPlaybackType,
                                     SystemMediaTransportControlsButton, SystemMediaTransportControlsTimelineProperties)
    from winrt.windows.media.playback import MediaPlayer
    from winrt.windows.storage.streams import RandomAccessStreamReference
    return SimpleNamespace(Uri=Uri, Repeat=MediaPlaybackAutoRepeatMode, Status=MediaPlaybackStatus,
                           Type=MediaPlaybackType, Button=SystemMediaTransportControlsButton,
                           Timeline=SystemMediaTransportControlsTimelineProperties, MediaPlayer=MediaPlayer,
                           StreamReference=RandomAccessStreamReference)


class _Controls:
    """The session on Windows' own controls. post(kind, value) is called on a Windows thread."""

    def __init__(self, post):
        self._post = post
        self._mp = self._c = None
        self._tokens = []
        w = self._w = _load_winrt()
        self._buttons = {w.Button.PLAY: "play", w.Button.PAUSE: "pause", w.Button.STOP: "stop",
                         w.Button.NEXT: "next", w.Button.PREVIOUS: "prev"}
        try:
            self._mp = w.MediaPlayer()
            self._mp.command_manager.is_enabled = False
            c = self._c = self._mp.system_media_transport_controls
            c.is_enabled = True
            for event, handler in (("button_pressed", self._pressed), ("playback_position_change_requested", self._position),
                                   ("shuffle_enabled_change_requested", self._shuffle),
                                   ("auto_repeat_mode_change_requested", self._repeat)):
                self._tokens.append((getattr(c, "remove_" + event), getattr(c, "add_" + event)(handler)))
        except Exception:
            self.close()
            raise

    @_guard
    def _pressed(self, sender, args):
        name = self._buttons.get(args.button)
        if name:
            self._post("command", name)

    @_guard
    def _position(self, sender, args):
        self._post("seek", int(args.requested_playback_position / datetime.timedelta(milliseconds=1)))

    @_guard
    def _shuffle(self, sender, args):
        self._post("shuffle", bool(args.requested_shuffle_enabled))

    @_guard
    def _repeat(self, sender, args):
        mode = _MODE_OF_NAME.get(args.requested_auto_repeat_mode.name)
        if mode is not None:
            self._post("repeat", mode)

    def apply(self, now: dict, changed: dict):
        c, w = self._c, self._w
        if "status" in changed:
            c.playback_status = getattr(w.Status, now["status"].upper())
        for key, prop in (("play", "is_play_enabled"), ("pause", "is_pause_enabled"), ("stop", "is_stop_enabled"),
                          ("next", "is_next_enabled"), ("prev", "is_previous_enabled"), ("shuffle", "shuffle_enabled")):
            if key in changed:
                setattr(c, prop, now[key])
        if "repeat" in changed:
            c.auto_repeat_mode = getattr(w.Repeat, _MODE_NAMES.get(now["repeat"], "NONE"))
        if "track" in changed or ("cover" in changed and now["cover"] is None):
            self._show(now["track"], now["cover"])
        elif "cover" in changed:
            self._show_cover(now["cover"])

    def _show(self, track, cover):
        d = self._c.display_updater
        d.clear_all()
        if track is not None:
            d.type = self._w.Type.MUSIC
            m = d.music_properties
            m.title, m.artist, m.album_title, m.album_artist = track
            if cover:
                d.thumbnail = self._picture(cover)
        d.update()

    def _show_cover(self, cover):
        d = self._c.display_updater
        d.thumbnail = self._picture(cover)
        d.update()

    def _picture(self, path):
        return self._w.StreamReference.create_from_uri(self._w.Uri(Path(path).as_uri()))

    def timeline(self, pos_ms: int, dur_ms: int):
        t = self._w.Timeline()
        dur = max(0, dur_ms)
        t.start_time = t.min_seek_time = datetime.timedelta(0)
        t.end_time = t.max_seek_time = datetime.timedelta(milliseconds=dur)
        t.position = datetime.timedelta(milliseconds=min(max(0, pos_ms), dur))
        self._c.update_timeline_properties(t)

    def close(self):
        for remove, token in self._tokens:
            try:
                remove(token)
            except Exception:
                pass
        self._tokens = []
        try:
            if self._c is not None:
                self._c.playback_status = self._w.Status.CLOSED
                self._c.is_enabled = False
        except Exception:
            pass
        try:
            if self._mp is not None:
                self._mp.close()
        except Exception:
            pass
        self._mp = self._c = None


class _HotKeys(QAbstractNativeEventFilter):
    """The four media keys through RegisterHotKey. They are registered for the thread, not for a window: the main window
    can be made again (a frameless window is) and a window handle taken now would be gone then. Windows posts WM_HOTKEY to
    the thread's queue and Qt's event loop offers it to this filter before it would drop it as a message without a window."""

    def __init__(self, post):
        super().__init__()
        self._post = post
        self._ids = {}
        self._unregister = None
        app = QCoreApplication.instance()
        if app is None:
            log.warning("media keys: there is no application to deliver them to")
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            register = _declare(user32, "RegisterHotKey", ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_uint, ctypes.c_uint)
            self._unregister = _declare(user32, "UnregisterHotKey", ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        except (OSError, AttributeError) as e:
            log.warning("media keys: user32 is not usable (%s)", e)
            return
        held = []
        for n, (vk, command, label) in enumerate(_HOTKEYS):
            if register(None, _HOTKEY_BASE + n, _MOD_NOREPEAT, vk):
                self._ids[_HOTKEY_BASE + n] = command
            else:
                err = ctypes.get_last_error()
                held.append(f"{label} ({'held by another program' if err == _ERROR_HOTKEY_ALREADY_REGISTERED else f'error {err}'})")
        if held:
            log.warning("media keys: not taken: %s", ", ".join(held))
        if self._ids:
            app.installNativeEventFilter(self)

    @property
    def taken(self) -> int:
        return len(self._ids)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = _Msg.from_address(int(message))
            if msg.message == _WM_HOTKEY and msg.wParam in self._ids:
                self._post("command", self._ids[msg.wParam])
                return True, 0
        return False, 0

    def apply(self, now: dict, changed: dict):
        pass

    def timeline(self, pos_ms: int, dur_ms: int):
        pass

    def close(self):
        app = QCoreApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)
        for ident in self._ids:
            self._unregister(None, ident)
        self._ids = {}


class SmtcSession(QObject):
    """The same members as MPRISService: open_files (never sent, Windows has nothing that opens a file through the
    session), remote_changed (after Windows changed shuffle or repeat), refresh, wrote and shutdown."""

    open_files = Signal(list)
    remote_changed = Signal()
    _event = Signal(str, object)

    def __init__(self, player, parent: QObject | None = None):
        super().__init__(parent)
        self._player = player
        self._live = False
        self._serial = 0                # a new number for every track_changed, so a late picture is known to be old
        self._prev_pos_ms = -1
        self._sent: dict = {}
        self._sent_dur = -1
        self._timeline_at = 0.0
        self._cover = None
        self._covers: list[str] = []
        self._event.connect(self._on_event, Qt.QueuedConnection)
        self._backend = self._open_backend()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)         # a track change is a burst of signals; one look at the end of it
        self._timer.timeout.connect(self._flush)
        self._live = True
        for sig in (player.state_changed, player.queue_changed, player.order_changed):
            sig.connect(self.refresh)
        player.track_changed.connect(self._on_track)
        player.position_changed.connect(self._on_position)
        self._clean_cache()
        self.refresh()

    def _open_backend(self):
        try:
            backend = _Controls(self._event.emit)
            log.info("media session: system media controls ready")
            return backend
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        log.info("media keys: using the hotkey fallback")
        log.info("media session: system media controls are not available (%s)", reason)
        keys = _HotKeys(self._event.emit)
        if keys.taken:
            log.info("media keys: %d of %d keys taken", keys.taken, len(_HOTKEYS))
        else:
            log.warning("media keys: no key could be taken, so they stay with whoever has them")
        return keys

    # what the window calls

    def refresh(self):
        if self._live and not self._timer.isActive():
            self._timer.start()

    def wrote(self):
        self.remote_changed.emit()
        self.refresh()

    def shutdown(self):
        if not self._live:
            return
        self._live = False
        self._timer.stop()
        self._backend.close()
        for path in self._covers:
            self._drop(path)
        self._covers = []

    # change tracking

    def _snapshot(self) -> dict:
        p = self._player
        t = p.current
        return {
            "status": "Closed" if t is None and not p.queue else _state(p),
            "play": bool(p.queue),
            "pause": t is not None,
            "stop": t is not None,
            "next": _can_next(p),
            "prev": bool(p.queue),
            "shuffle": p.shuffle,
            "repeat": p.mode,
            "track": None if t is None else tuple(str(x or "") for x in (t.title or t.path.stem, t.artist, t.album, t.albumartist or t.artist)),
            "cover": self._cover,
        }

    @_guard
    def _flush(self):
        if not self._live:
            return
        now = self._snapshot()
        changed = {k: v for k, v in now.items() if v != self._sent.get(k, _UNSET)}
        if not changed:
            return
        self._sent = now
        self._backend.apply(now, changed)
        if "status" in changed or "track" in changed:
            self._push_timeline()

    def _push_timeline(self, pos: int | None = None, dur: int | None = None):
        p = self._player
        if pos is None:
            pos, dur = (0, 0) if p.current is None else (_pos_ms(p), _dur_ms(p))
        self._backend.timeline(pos, dur)
        self._sent_dur = dur
        self._timeline_at = time.monotonic()

    @_guard
    def _on_track(self, track):
        if not self._live:
            return
        self._serial += 1
        self._prev_pos_ms = -1
        self._cover = None
        self.refresh()
        threading.Thread(target=self._find_cover, args=(self._serial, track.path), daemon=True, name="smtc-cover").start()

    @_guard
    def _on_position(self, _frac: float, pos_ms: int, dur_ms: int):
        if not self._live:
            return
        prev, self._prev_pos_ms = self._prev_pos_ms, pos_ms
        jumped = prev >= 0 and abs(pos_ms - prev) > _SEEK_JUMP_MS
        late = dur_ms != self._sent_dur and self._player.current is not None
        stale = self._player.is_playing and time.monotonic() - self._timeline_at > _TIMELINE_EVERY
        if jumped or late or stale:
            self._push_timeline(pos_ms, dur_ms)

    # what arrives from Windows, on the main thread

    @_guard
    def _on_event(self, kind: str, value):
        p = self._player
        if kind == "command":
            self._command(value)
        elif kind == "seek":
            self._seek(int(value))
        elif kind == "shuffle":
            p.shuffle = bool(value)
            self.wrote()
        elif kind == "repeat":
            p.mode = int(value)
            self.wrote()
        elif kind == "cover":
            self._take_cover(*value)

    def _command(self, name: str):
        p = self._player
        if name == "play":
            p.play()
        elif name == "pause":
            p.pause()
        elif name == "toggle":
            p.toggle()
        elif name == "stop":
            p.stop()
        elif name == "next":
            p.play() if p.index < 0 else p.next()       # Player.next() raises with shuffle on and nothing selected
        elif name == "prev":
            p.prev()

    def _seek(self, ms: int):
        p = self._player
        dur = _dur_ms(p)
        if p.current is None or dur <= 0:
            return
        if ms >= dur:
            p.next()
            return
        self._prev_pos_ms = -1              # the tick the seek causes must not count as a second seek
        p.seek(max(0, ms) / dur)
        self._prev_pos_ms = _pos_ms(p)
        self._push_timeline()

    # the cover

    def _find_cover(self, serial: int, path):
        """On a thread of its own: reading the picture out of the file and writing a small PNG for Windows to read."""
        try:
            data = extract_art(path)
            img = decode_art(data) if data else None
            if img is None:
                return
            if max(img.width(), img.height()) > _COVER_SIZE:
                img = img.scaled(_COVER_SIZE, _COVER_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            folder = plat_paths.cache_dir() / "media"
            folder.mkdir(parents=True, exist_ok=True)
            out = folder / f"cover-{os.getpid()}-{serial}.png"
            if img.save(str(out), "PNG"):
                self._event.emit("cover", (serial, str(out)))
        except Exception:
            log.exception("media session: cover")

    def _take_cover(self, serial: int, path: str):
        if not self._live or serial != self._serial:
            self._drop(path)
            return
        self._cover = path
        self._covers.append(path)
        while len(self._covers) > _KEEP_COVERS:
            self._drop(self._covers.pop(0))
        self.refresh()

    @staticmethod
    def _drop(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _clean_cache():
        """Pictures left by a run that ended badly: anything a day old."""
        folder = plat_paths.cache_dir() / "media"
        try:
            for f in folder.glob("cover-*.png"):
                if time.time() - f.stat().st_mtime > 86400:
                    f.unlink()
        except OSError:
            pass
