"""MPRIS2 for the desktop: media keys, playerctl, and the Plasma and GNOME media widgets.

QtDBus builds an adaptor's D-Bus interface from the Qt meta object, so the interfaces are
declared with ClassInfo, the properties with Property and the methods with Slot. A plain
Python @property or method is invisible to it.
"""
from __future__ import annotations

import ctypes
import functools
import os
from pathlib import Path

import shiboken6
from PySide6.QtCore import ClassInfo, Property, QMetaType, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtDBus import (
    QDBusAbstractAdaptor, QDBusArgument, QDBusConnection, QDBusMessage, QDBusObjectPath,
)
from PySide6.QtMultimedia import QMediaPlayer

from applog import log
from player import Player, PlayMode, PLAYABLE_EXTS, _ART_CACHE, _ART_LOCK, _FOLDER_COVER_CACHE

_NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
_ROOT_IFC = "org.mpris.MediaPlayer2"
_PLAYER_IFC = "org.mpris.MediaPlayer2.Player"
_SEEK_JUMP_MS = 1500        # a position jump this big between two ticks is a seek, not playback
_LOOP = {PlayMode.SEQUENTIAL: "None", PlayMode.REPEAT_ALL: "Playlist", PlayMode.REPEAT_ONE: "Track"}
_LOOP_MODE = {v: k for k, v in _LOOP.items()}

_i64_op = False             # QDBusArgument::operator<<(qlonglong) once found, None if there is none


def _i64(n: int):
    """A 64 bit D-Bus integer for a variant.

    PySide hands QtDBus a Python int as a 32 bit integer whenever it fits, and
    QDBusArgument's << only reaches the 16 bit overload, but MPRIS wants int64 for
    mpris:length. A QDBusArgument holding one qlonglong does marshal as 'x', so the
    operator is called on it directly. If that cannot be done the plain int goes out.
    """
    global _i64_op
    n = int(n)
    if _i64_op is False:
        try:
            _i64_op = ctypes.CDLL("libQt6DBus.so.6")._ZN13QDBusArgumentlsEx
            _i64_op.restype = ctypes.c_void_p
            _i64_op.argtypes = [ctypes.c_void_p, ctypes.c_longlong]
        except Exception:
            _i64_op = None
    if _i64_op is not None:
        try:
            a = QDBusArgument()
            _i64_op(shiboken6.getCppPointer(a)[0], n)
            if a.currentSignature() == "x":
                return a
        except Exception:
            pass
    return n


def _no_strings() -> QDBusArgument:
    """An empty 'as'. An empty Python list would not say what it is a list of."""
    a = QDBusArgument()
    a.beginArray(QMetaType(QMetaType.Type.QString.value))
    a.endArray()
    return a


_taper_exp = 0.0


def _taper() -> float:
    """The exponent of the volume slider's curve. MPRIS clients get the slider's scale, so
    their volume bar and the one in the window agree."""
    global _taper_exp
    if not _taper_exp:
        try:
            from panel_controls import _VOL_EXP
            _taper_exp = float(_VOL_EXP)
        except Exception:
            _taper_exp = 3.0
    return _taper_exp


def _pos_ms(p: Player) -> int:
    return p._vgm.position_ms if p._vgm_active else p.position_ms


def _dur_ms(p: Player) -> int:
    return p._vgm.duration_ms if p._vgm_active else p.duration_ms


def _status(p: Player) -> str:
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


def _can_next(p: Player) -> bool:
    if not p.queue:
        return False
    if p.mode == PlayMode.REPEAT_ONE:
        return len(p.queue) > 1
    return p.index < 0 or p._next_queue_idx() >= 0


def _cover_uri(path: Path) -> str | None:
    """The folder cover, when that is what the app shows for this track. Looks only at
    what the art code has already found; nothing is read or decoded here."""
    try:
        key = (str(path), path.stat().st_mtime_ns)
        with _ART_LOCK:
            pick = _FOLDER_COVER_CACHE.get(str(path.parent))
            shown = _ART_CACHE.get(key)
        if pick is not None and shown is not None and len(shown) == pick.stat().st_size:
            return pick.as_uri()
    except Exception:
        pass
    return None


def _guard(fn):
    @functools.wraps(fn)
    def run(*a):
        try:
            return fn(*a)
        except Exception:
            log.exception("MPRIS %s", fn.__name__)
    return run


@ClassInfo(**{"D-Bus Interface": _ROOT_IFC})
class _MPRISRoot(QDBusAbstractAdaptor):
    def __init__(self, svc: "MPRISService"):
        super().__init__(svc)
        self._svc = svc

    CanQuit = Property(bool, lambda s: True)
    CanRaise = Property(bool, lambda s: False)
    HasTrackList = Property(bool, lambda s: False)
    Identity = Property(str, lambda s: "AudioCrate")
    DesktopEntry = Property(str, lambda s: "audiocrate")
    SupportedUriSchemes = Property("QStringList", lambda s: ["file"])
    SupportedMimeTypes = Property("QStringList", lambda s: [])

    @Slot()
    @_guard
    def Quit(self):
        self._svc.quit()

    @Slot()
    def Raise(self):
        pass


@ClassInfo(**{"D-Bus Interface": _PLAYER_IFC})
class _MPRISPlayer(QDBusAbstractAdaptor):
    Seeked = Signal("qlonglong")

    def __init__(self, svc: "MPRISService", player: Player):
        super().__init__(svc)
        self._svc = svc
        self._p = player

    # properties. Nothing here has a NOTIFY signal: those would be exported as D-Bus signals,
    # and the service sends PropertiesChanged itself.

    PlaybackStatus = Property(str, lambda s: _status(s._p))
    Metadata = Property("QVariantMap", lambda s: s._svc.metadata())
    Position = Property("qlonglong", lambda s: _pos_ms(s._p) * 1000)
    Rate = Property(float, lambda s: 1.0, lambda s, v: None)
    MinimumRate = Property(float, lambda s: 1.0)
    MaximumRate = Property(float, lambda s: 1.0)
    CanGoNext = Property(bool, lambda s: _can_next(s._p))
    CanGoPrevious = Property(bool, lambda s: bool(s._p.queue))
    CanPlay = Property(bool, lambda s: bool(s._p.queue))
    CanPause = Property(bool, lambda s: s._p.current is not None)
    CanSeek = Property(bool, lambda s: s._p.current is not None)
    CanControl = Property(bool, lambda s: True)

    def _get_loop(self) -> str:
        return _LOOP.get(self._p.mode, "None")

    @_guard
    def _set_loop(self, val: str):
        self._p.mode = _LOOP_MODE.get(val, PlayMode.SEQUENTIAL)
        self._svc.wrote()

    def _get_vol(self) -> float:
        return self._p.volume ** (1.0 / _taper())

    @_guard
    def _set_vol(self, val: float):
        self._p.volume = max(0.0, min(1.0, float(val))) ** _taper()
        self._svc.wrote()

    @_guard
    def _set_shuffle(self, val: bool):
        self._p.shuffle = bool(val)
        self._svc.wrote()

    LoopStatus = Property(str, _get_loop, _set_loop)
    Volume = Property(float, _get_vol, _set_vol)
    Shuffle = Property(bool, lambda s: s._p.shuffle, _set_shuffle)

    # methods

    @Slot()
    @_guard
    def Next(self):
        if self._p.index < 0:           # Player.next() raises with shuffle on and nothing selected
            self._p.play()
        else:
            self._p.next()

    @Slot()
    @_guard
    def Previous(self):
        self._p.prev()

    @Slot()
    @_guard
    def Pause(self):
        self._p.pause()

    @Slot()
    @_guard
    def PlayPause(self):
        self._p.toggle()

    @Slot()
    @_guard
    def Stop(self):
        self._p.stop()

    @Slot()
    @_guard
    def Play(self):
        self._p.play()

    @Slot("qlonglong")
    @_guard
    def Seek(self, offset_us):
        p = self._p
        dur = _dur_ms(p)
        if p.current is None or dur <= 0:
            return
        target = _pos_ms(p) + offset_us // 1000
        if target >= dur:
            p.next()
        else:
            self._svc.seek_ms(max(0, target))

    @Slot(QDBusObjectPath, "qlonglong")
    @_guard
    def SetPosition(self, track_id, pos_us):
        dur = _dur_ms(self._p)
        if track_id.path() != self._svc.track_id() or dur <= 0 or not 0 <= pos_us // 1000 <= dur:
            return
        self._svc.seek_ms(pos_us // 1000)

    @Slot(str)
    @_guard
    def OpenUri(self, uri):
        self._svc.open_uri(uri)


class MPRISService(QObject):
    """Exports the MPRIS2 object and sends PropertiesChanged and Seeked.

    open_files(list of str) is sent for an OpenUri that names a playable local file; the
    main window connects it to open_files(). remote_changed is sent after a client writes
    Volume, LoopStatus or Shuffle, so the controls can show it.
    """

    _BUS_NAME = "org.mpris.MediaPlayer2.audiocrate"      # the name actually taken, once registered
    _OBJECT   = "/org/mpris/MediaPlayer2"
    _PROPS_IFC = "org.freedesktop.DBus.Properties"

    open_files = Signal(list)
    remote_changed = Signal()

    def __init__(self, player: Player, parent: QObject | None = None):
        super().__init__(parent)
        self._player = player
        self._bus = QDBusConnection.sessionBus()
        self._live = False
        self._serial = 0                # a fresh track id for every track_changed
        self._prev_pos_ms = -1
        self._sent: dict = {}
        if not self._bus.isConnected():
            return

        self._root = _MPRISRoot(self)
        self._padap = _MPRISPlayer(self, player)
        # The object goes up before the name, so nobody that sees the name finds it empty.
        if not self._bus.registerObject(self._OBJECT, self, QDBusConnection.RegisterOption.ExportAdaptors):
            log.warning("MPRIS: could not register %s", self._OBJECT)
            return
        base = MPRISService._BUS_NAME
        for name in (base, f"{base}.instance{os.getpid()}"):     # the MPRIS name for a second instance
            if self._bus.registerService(name):
                self._BUS_NAME = name
                self._live = True
                break
        if not self._live:
            self._bus.unregisterObject(self._OBJECT)
            log.warning("MPRIS: no bus name available")
            return

        # Changes arrive in bursts (a track change is track, state and queue signals), so
        # they are collected and compared with what was last sent.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._flush)
        self._art_timer = QTimer(self)      # the cover is found a moment after the track starts
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(800)
        self._art_timer.timeout.connect(self.refresh)
        self._sent = self._snapshot()

        for sig in (player.state_changed, player.queue_changed, player.order_changed):
            sig.connect(self.refresh)
        player.track_changed.connect(self._on_track)
        player.position_changed.connect(self._on_position)

    # what the window and the adaptors call

    def track_id(self) -> str:
        return f"/org/audiocrate/track/{self._serial}" if self._player.current is not None else _NO_TRACK

    def quit(self):
        win = self.parent()
        if win is not None and hasattr(win, "close"):
            win.close()                 # the same path as File, Quit, so settings are saved
        else:
            from PySide6.QtWidgets import QApplication
            QApplication.quit()

    def open_uri(self, uri: str):
        u = QUrl(uri)
        if not u.isLocalFile():
            return
        path = Path(u.toLocalFile())
        if path.suffix.lower() in PLAYABLE_EXTS and path.is_file():
            self.open_files.emit([str(path)])

    def seek_ms(self, ms: int):
        p = self._player
        self._prev_pos_ms = -1          # the tick the seek causes must not count as a second Seeked
        p.seek(ms / _dur_ms(p))
        self._prev_pos_ms = _pos_ms(p)
        self._padap.Seeked.emit(self._prev_pos_ms * 1000)

    def metadata(self, plain: dict | None = None) -> dict:
        """Metadata as QtDBus wants it (a{sv}) from the plain values of _meta()."""
        m = self._meta() if plain is None else plain
        out: dict = {"mpris:trackid": QDBusObjectPath(m["id"])}
        if m.get("length"):
            out["mpris:length"] = _i64(m["length"])
        if m.get("artist"):
            out["xesam:artist"] = list(m.get("artists") or [m["artist"]])
        for key, name in (("title", "xesam:title"), ("album", "xesam:album"),
                          ("url", "xesam:url"), ("art", "mpris:artUrl")):
            if m.get(key):
                out[name] = m[key]
        return out

    def shutdown(self):
        if not self._live:
            return
        self._live = False
        self._timer.stop()
        self._art_timer.stop()
        self._bus.unregisterObject(self._OBJECT)
        self._bus.unregisterService(self._BUS_NAME)

    # change tracking

    def _meta(self) -> dict:
        p = self._player
        t = p.current
        if t is None:
            return {"id": _NO_TRACK}
        try:
            url = t.path.as_uri()
        except ValueError:
            url = ""
        return {"id": self.track_id(), "title": t.title or t.path.stem, "artist": t.artist, "artists": t.artists,
                "album": t.album, "url": url, "art": _cover_uri(t.path), "length": _dur_ms(p) * 1000}

    def _snapshot(self) -> dict:
        p = self._player
        return {
            "PlaybackStatus": _status(p),
            "LoopStatus": _LOOP.get(p.mode, "None"),
            "Shuffle": p.shuffle,
            "Volume": p.volume ** (1.0 / _taper()),
            "Metadata": self._meta(),
            "CanGoNext": _can_next(p),
            "CanGoPrevious": bool(p.queue),
            "CanPlay": bool(p.queue),
            "CanPause": p.current is not None,
            "CanSeek": p.current is not None,
        }

    def refresh(self):
        """Look for changes soon. The window calls this when the volume slider moves."""
        if self._live and not self._timer.isActive():
            self._timer.start()

    def wrote(self):
        self.remote_changed.emit()
        self.refresh()

    @_guard
    def _flush(self):
        if not self._live:
            return
        now = self._snapshot()
        changed = {k: v for k, v in now.items() if v != self._sent.get(k)}
        if not changed:
            return
        self._sent = now
        if "Metadata" in changed:
            changed["Metadata"] = self.metadata(changed["Metadata"])
        msg = QDBusMessage.createSignal(self._OBJECT, self._PROPS_IFC, "PropertiesChanged")
        msg.setArguments([_PLAYER_IFC, changed, _no_strings()])
        self._bus.send(msg)

    def _on_track(self, _track):
        if not self._live:
            return
        self._serial += 1
        self._prev_pos_ms = -1
        self.refresh()
        self._art_timer.start()

    def _on_position(self, _frac: float, pos_ms: int, dur_ms: int):
        # Called every 100 ms while playing, so it only looks. A length that turned up late
        # and a volume moved in the window are noticed here.
        if not self._live:
            return
        if (dur_ms * 1000 != (self._sent["Metadata"].get("length") or 0)
                or self._player.volume ** (1.0 / _taper()) != self._sent["Volume"]):
            self.refresh()
        prev, self._prev_pos_ms = self._prev_pos_ms, pos_ms
        if prev >= 0 and abs(pos_ms - prev) > _SEEK_JUMP_MS:
            self._padap.Seeked.emit(pos_ms * 1000)
