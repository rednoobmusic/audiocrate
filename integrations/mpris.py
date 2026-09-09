from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtDBus import (
    QDBusConnection, QDBusAbstractAdaptor, QDBusMessage,
    QDBusObjectPath,
)

from player import Player, PlayMode


def _track_id(idx: int) -> QDBusObjectPath:
    return QDBusObjectPath(f"/org/audiocrate/track/{max(0, idx)}")


def _mpris_meta(player: Player) -> dict:
    t = player.current
    if t is None:
        return {"mpris:trackid": _track_id(-1)}
    meta: dict = {
        "mpris:trackid": _track_id(player.index),
        "xesam:title":   t.title or t.path.stem,
        "xesam:artist":  [t.artist] if t.artist else [],
        "xesam:album":   t.album or "",
        "xesam:url":     t.path.as_uri(),
    }
    dur = player.duration_ms
    if dur > 0:
        meta["mpris:length"] = dur * 1000
    return meta


class _MPRISRoot(QDBusAbstractAdaptor):
    """org.mpris.MediaPlayer2 root interface."""

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.setAutoRelaySignals(False)

    @property
    def CanQuit(self) -> bool:         return True
    @property
    def CanRaise(self) -> bool:        return False
    @property
    def HasTrackList(self) -> bool:    return False
    @property
    def Identity(self) -> str:         return "AudioCrate"
    @property
    def DesktopEntry(self) -> str:     return "audiocrate"
    @property
    def SupportedUriSchemes(self):     return ["file"]
    @property
    def SupportedMimeTypes(self):      return []

    def Quit(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def Raise(self): pass


class _MPRISPlayer(QDBusAbstractAdaptor):
    """org.mpris.MediaPlayer2.Player interface."""

    def __init__(self, parent: QObject, player: Player):
        super().__init__(parent)
        self.setAutoRelaySignals(False)
        self._player = player

    # properties

    @property
    def PlaybackStatus(self) -> str:
        p = self._player
        if not p.current:
            return "Stopped"
        return "Playing" if p.is_playing else "Paused"

    @property
    def LoopStatus(self) -> str:
        m = self._player.mode
        if m == PlayMode.REPEAT_ONE: return "Track"
        if m == PlayMode.REPEAT_ALL: return "Playlist"
        return "None"

    @LoopStatus.setter
    def LoopStatus(self, val: str):
        if val == "Track":    self._player.mode = PlayMode.REPEAT_ONE
        elif val == "Playlist": self._player.mode = PlayMode.REPEAT_ALL
        else:                 self._player.mode = PlayMode.SEQUENTIAL

    @property
    def Shuffle(self) -> bool:
        return self._player.shuffle

    @Shuffle.setter
    def Shuffle(self, val: bool):
        self._player.shuffle = val

    @property
    def Metadata(self) -> dict:
        return _mpris_meta(self._player)

    @property
    def Volume(self) -> float:
        return self._player.volume

    @Volume.setter
    def Volume(self, val: float):
        self._player.volume = max(0.0, min(1.0, val))

    @property
    def Position(self) -> int:
        return self._player.position_ms * 1000

    @property
    def MinimumRate(self) -> float: return 1.0
    @property
    def MaximumRate(self) -> float: return 1.0
    @property
    def Rate(self) -> float:        return 1.0
    @Rate.setter
    def Rate(self, _): pass

    @property
    def CanGoNext(self) -> bool:     return len(self._player.queue) > 1
    @property
    def CanGoPrevious(self) -> bool: return len(self._player.queue) > 1
    @property
    def CanPlay(self) -> bool:       return bool(self._player.queue)
    @property
    def CanPause(self) -> bool:      return bool(self._player.current)
    @property
    def CanSeek(self) -> bool:       return bool(self._player.current)
    @property
    def CanControl(self) -> bool:    return True

    # methods

    def Next(self):          self._player.next()
    def Previous(self):      self._player.prev()
    def Pause(self):         self._player.pause()
    def PlayPause(self):     self._player.toggle()
    def Stop(self):          self._player.stop()
    def Play(self):          self._player.play()

    def Seek(self, offset_us: int):
        self._player.seek_delta(offset_us // 1_000_000)

    def SetPosition(self, track_id: QDBusObjectPath, pos_us: int):
        self._player.seek(pos_us / max(1, self._player.duration_ms * 1000))

    def OpenUri(self, uri: str):
        pass


class MPRISService(QObject):
    """Registers the MPRIS2 D-Bus service and emits PropertiesChanged."""

    _BUS_NAME   = "org.mpris.MediaPlayer2.audiocrate"
    _OBJECT     = "/org/mpris/MediaPlayer2"
    _PLAYER_IFC = "org.mpris.MediaPlayer2.Player"
    _PROPS_IFC  = "org.freedesktop.DBus.Properties"

    def __init__(self, player: Player, parent: QObject | None = None):
        super().__init__(parent)
        self._player = player
        self._root   = _MPRISRoot(self)
        self._padap  = _MPRISPlayer(self, player)
        self._bus    = QDBusConnection.sessionBus()

        if not self._bus.isConnected():
            return
        self._bus.registerService(self._BUS_NAME)
        self._bus.registerObject(
            self._OBJECT, self,
            QDBusConnection.RegisterOption.ExportAdaptors,
        )

        player.state_changed.connect(self._on_state)
        player.track_changed.connect(self._on_track)
        player.position_changed.connect(self._on_position)

    def _emit_props(self, changed: dict):
        msg = QDBusMessage.createSignal(
            self._OBJECT, self._PROPS_IFC, "PropertiesChanged"
        )
        msg.setArguments([self._PLAYER_IFC, changed, []])
        self._bus.send(msg)

    def _on_state(self, playing: bool):
        self._emit_props({"PlaybackStatus": self._padap.PlaybackStatus})

    def _on_track(self, _track):
        self._emit_props({
            "Metadata":       self._padap.Metadata,
            "PlaybackStatus": self._padap.PlaybackStatus,
        })

    def _on_position(self, frac: float, pos_ms: int, dur_ms: int):
        if not self._bus.isConnected():
            return
        prev = getattr(self, "_prev_pos_ms", -1)
        self._prev_pos_ms = pos_ms
        if prev >= 0 and abs(pos_ms - prev) > 2000:
            msg = QDBusMessage.createSignal(
                self._OBJECT, self._PLAYER_IFC, "Seeked"
            )
            msg.setArguments([pos_ms * 1000])
            self._bus.send(msg)
