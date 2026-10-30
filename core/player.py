from __future__ import annotations
import random
from pathlib import Path
from typing import Optional

_GME_EXTS = {".vgm", ".vgz", ".nsf", ".nsfe", ".spc", ".gbs", ".ay", ".gym",
             ".hes", ".kss", ".sap"}  # all libgme formats
_FUR_EXTS = {".fur"}      # Furnace modules, played by FurPlayer (libfurnace_wrap)
import cli_render         # zxtune/openmpt render-to-WAV for AY/YM/VGM + tracker modules
import sid_player         # C64 SID via libsidplayfp (scanned first, then played)
from artists import split_artists

# SID playback is on by default; AUDIOCRATE_SID=0 turns it off. The rapid-switch
# crash happened just as often with SID off: it came from the .fur streaming path,
# see _play_idx.
import os as _os
import time as _time
SID_ENABLED = _os.environ.get("AUDIOCRATE_SID", "1") != "0"
_SID_EXTS = sid_player.EXTS if SID_ENABLED else set()

from PySide6.QtCore    import QObject, Signal, QUrl, QTimer
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices, QAudioDevice

_FADE_STEPS    = 2
_FADE_INTERVAL = 5

# Shortest gap between two attempts to move off a failing output. A sink that
# has lost its device reports the failure on every tick, so without this a
# replacement that does not work either is rebuilt a hundred times a second.
_RECOVER_GAP = 5.0


def device_key(dev: QAudioDevice) -> str:
    """A QAudioDevice id as something the settings file can hold.

    id() is a QByteArray, and json refuses bytes. It refuses the whole file,
    not just this key, so a raw id would throw away every other setting saved
    in the same write.

    latin-1 maps each byte to one codepoint and back, so the round trip is
    lossless whatever Qt puts in there, and a real device id is ASCII, which
    leaves the file readable.
    """
    return dev.id().data().decode("latin-1")


def set_fade_params(steps: int, interval_ms: int):
    global _FADE_STEPS, _FADE_INTERVAL
    _FADE_STEPS    = max(1, steps)
    _FADE_INTERVAL = max(1, interval_ms)

# Container/codec coverage mirrors what ffmpeg's demuxers actually advertise
# (generated from `ffmpeg -demuxers`, minus subtitle/image/raw-stream formats and
# the tracker/chiptune extensions handled by the dedicated engines below).
VIDEO_EXTS = {
    ".3g2", ".3gp", ".amv", ".asf", ".avi", ".bik", ".bmv", ".cdxl", ".daud",
    ".dav", ".dif", ".divx", ".dv", ".f4v", ".flv", ".ifv", ".imx", ".ismv",
    ".ivf", ".kux", ".lvf", ".m2ts", ".m2v", ".m4v", ".mj2", ".mk3d", ".mkv",
    ".moflex", ".mov", ".mp4", ".mpeg", ".mpg", ".mpv", ".mts", ".mvi", ".mxf",
    ".mxg", ".nut", ".ogv", ".ogx", ".pdv", ".plm", ".psp", ".rm", ".rmvb",
    ".roq", ".sga", ".smk", ".ts", ".ty", ".ty+", ".usm", ".vb",
    ".viv", ".vob", ".webm", ".wmv", ".xmv", ".yop",
}

_FFMPEG_AUDIO_EXTS = {
    # mainstream
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".spx", ".m4a", ".m4b", ".aac",
    ".wav", ".w64", ".wma", ".ape", ".mka", ".mks", ".aiff", ".aif", ".aifc",
    ".wv", ".mpc", ".tta", ".tak", ".shn", ".ac3", ".eac3", ".ec3", ".ac4",
    ".dts", ".dtshd", ".thd", ".mlp", ".dsf", ".mp1", ".mp2", ".mpa",
    ".m2a", ".au", ".snd", ".caf", ".voc", ".amr", ".awb", ".gsm", ".ra",
    ".3ga", ".8svx", ".qoa", ".rka", ".osq", ".bonk", ".cdg", ".dss", ".mac",
    # raw-stream / alias extensions ffmpeg already decodes (parity with VLC)
    ".a52", ".adt", ".adts", ".amb", ".mpga", ".qcp", ".xa",
    # speech / telephony / bluetooth codecs
    ".sbc", ".msbc", ".aptx", ".aptxhd", ".lc3", ".dfpwm", ".sln", ".nsp",
    # broadcast / studio
    ".ircam", ".sf", ".nist", ".sph", ".paf", ".pvf", ".avr", ".sds", ".vqf",
    ".wsd", ".laf", ".iamf", ".tco", ".rso", ".sbg", ".son", ".adp",
    # audiobook / DRM-free proprietary
    ".aa", ".aa3", ".aax", ".oma", ".omg", ".binka", ".ivr", ".vt",
    # game-console / engine audio
    ".adx", ".ast", ".brstm", ".bcstm", ".bfstm", ".genh", ".hca", ".fsb",
    ".vag", ".xvag", ".svag", ".msf", ".mtaf", ".mca", ".rsd", ".rco", ".rcv",
    ".apl", ".ads", ".afc", ".aea", ".aix", ".dtk", ".dtm", ".fap", ".fwse",
    ".sdr2", ".sdx", ".sfx", ".sfx2", ".ss2", ".st26", ".stp", ".svs", ".vpk",
    ".vqe", ".vql", ".xl", ".adf",
}
AUDIO_EXTS = (_FFMPEG_AUDIO_EXTS
              | {".fur"}             # Furnace modules (libfurnace, into the engine)
              | _SID_EXTS            # C64 SID via libsidplayfp
              | _GME_EXTS            # chiptunes via libgme
              | cli_render.EXTS)     # AY/YM/ZX + tracker modules

# Everything the app accepts from a drop, a folder scan or a file dialog, video included.
PLAYABLE_EXTS = AUDIO_EXTS | VIDEO_EXTS


_PRELOAD_LEAD_S = 60        # the next song is decoded this many seconds before the current one ends


class PlayMode:
    SEQUENTIAL = 0
    REPEAT_ALL = 1
    REPEAT_ONE = 2

MODE_NAMES = {
    PlayMode.SEQUENTIAL: "SEQ",
    PlayMode.REPEAT_ALL: "REP",
    PlayMode.REPEAT_ONE: "RP1",
}


# Names that mean a cover, best first. A folder of loose downloads or a Windows Media Player folder holds other pictures and
# thumbnails too; an image that is not named like one of these is never taken for the cover.
_COVER_STEMS = ("cover", "folder", "front", "albumart", "album", "artwork", "art", "thumb")
_COVER_EXTS  = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_COVER_SMALL = ("small", "thumb", "icon")        # Windows Media Player writes AlbumArtSmall.jpg and AlbumArt_{id}_Small.jpg


def _cover_rank(p: Path):
    """Sort key of an image next to a track, or None when its name says it is not a cover."""
    stem = p.stem.lower()
    for i, name in enumerate(_COVER_STEMS):
        if stem == name or (stem.startswith(name) and not stem[len(name)].isalpha()) or (name == "albumart" and stem.startswith(name)):
            small = any(s in stem for s in _COVER_SMALL)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            return (small, i, -size, stem)
    return None

# Several panels (now-playing, visualizer, palette theming, metadata) ask for the
# same track's art on every track change, so it is cached: the file is opened and
# the picture decoded once, not three or four times.
from collections import OrderedDict
import threading as _threading

_ART_CACHE: OrderedDict = OrderedDict()          # (path, mtime) -> bytes | None
_ART_CACHE_MAX = 64
_ART_CACHE_BYTES = 16 << 20                      # and no more than this many bytes of them
_FOLDER_COVER_CACHE: OrderedDict = OrderedDict() # folder path -> Path | None
_FOLDER_COVER_MAX = 256
_ART_LOCK = _threading.Lock()


def _folder_cover(path: Path) -> bytes | None:
    """Fall back to a cover image sitting next to the track (cover.jpg, folder.png ...)."""
    try:
        folder = path.parent
        key = str(folder)
        with _ART_LOCK:
            if key in _FOLDER_COVER_CACHE:
                _FOLDER_COVER_CACHE.move_to_end(key)
                pick = _FOLDER_COVER_CACHE[key]
                return pick.read_bytes() if pick is not None else None
        ranked = []
        for p in folder.iterdir():
            if p.suffix.lower() in _COVER_EXTS and p.is_file() and (r := _cover_rank(p)) is not None:
                ranked.append((r, p))
        pick = min(ranked, key=lambda x: x[0])[1] if ranked else None
        with _ART_LOCK:
            _FOLDER_COVER_CACHE[key] = pick
            while len(_FOLDER_COVER_CACHE) > _FOLDER_COVER_MAX:
                _FOLDER_COVER_CACHE.popitem(last=False)
        return pick.read_bytes() if pick is not None else None
    except Exception:
        pass
    return None


def _decode_art(data: bytes):
    """Cover bytes to a display-sized QImage, or None.

    Meant for a worker thread. A QImage may be built off the GUI thread and a
    QPixmap may not, and decoding then smooth-scaling a large cover is tens of
    milliseconds that the GUI thread should not spend on a song change.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    # Pillow, not QImage.fromData: PySide holds the interpreter lock for the whole
    # of that call, tens of milliseconds on a big cover, and the GUI thread cannot
    # run any Python until it lets go. Pillow decodes and resizes without the lock,
    # and for a JPEG draft mode decodes at reduced size to begin with.
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (1024, 1024))
        im = im.convert("RGBA")
        im.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
        w, h = im.size
        return QImage(im.tobytes(), w, h, w * 4, QImage.Format_RGBA8888).copy()
    except Exception:
        img = QImage.fromData(data)
        if img.isNull():
            return None
        if max(img.width(), img.height()) > 1024:
            img = img.scaled(1024, 1024, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return img


_ART_IMG: "OrderedDict[bytes, object]" = None
_ART_IMG_LOCK = _threading.Lock()


def decode_art(data: bytes):
    """_decode_art, once per cover. The visualizer, the now playing panel and the
    palette code all ask for the same bytes within a moment of each other on a
    song change; the lock makes the later ones wait for the first and share it."""
    global _ART_IMG
    with _ART_IMG_LOCK:
        if _ART_IMG is None:
            _ART_IMG = OrderedDict()
        if data in _ART_IMG:
            _ART_IMG.move_to_end(data)
            return _ART_IMG[data]
        img = _decode_art(data)
        _ART_IMG[data] = img
        while len(_ART_IMG) > 3:
            _ART_IMG.popitem(last=False)
        return img


def extract_art(path: Path) -> bytes | None:
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), 0)
    with _ART_LOCK:
        if key in _ART_CACHE:
            _ART_CACHE.move_to_end(key)
            return _ART_CACHE[key]
    data = _extract_art_uncached(path)
    with _ART_LOCK:
        _ART_CACHE[key] = data
        while len(_ART_CACHE) > _ART_CACHE_MAX or (
                len(_ART_CACHE) > 1 and sum(len(v) for v in _ART_CACHE.values() if v) > _ART_CACHE_BYTES):
            _ART_CACHE.popitem(last=False)
    return data


def _front(pictures):
    """The picture to show out of [(type, bytes)]: the front cover when there is one, then an untyped one, and a back
    cover, a booklet page or a photo of the band only when nothing else is there. Files often store the back cover first."""
    pictures = [(t, d) for t, d in pictures if d]
    if not pictures:
        return None
    return min(enumerate(pictures), key=lambda x: (0 if x[1][0] == 3 else 1 if x[1][0] in (0, 1, 2) else 2, x[0]))[1][1]


def _wm_picture(raw: bytes):
    """(type, image bytes) from the WM_PICTURE structure a WMA file stores: type, size, mime and description in UTF-16, image."""
    try:
        kind = raw[0]
        size = int.from_bytes(raw[1:5], "little")
        i = 5
        for _ in range(2):                                   # mime type, then description, each ended by a UTF-16 NUL
            while i + 1 < len(raw) and raw[i:i + 2] != b"\0\0":
                i += 2
            i += 2
        data = bytes(raw[i:i + size])
        return kind, data
    except (IndexError, ValueError):
        return 0, b""


def _extract_art_uncached(path: Path) -> bytes | None:
    try:
        import base64
        from mutagen import File as MFile
        from mutagen.flac import Picture
        f = MFile(str(path))
        if f is None:
            return _folder_cover(path)

        if hasattr(f, "pictures") and f.pictures:
            return _front([(p.type, p.data) for p in f.pictures])

        tags = f.tags
        if tags is None:
            return _folder_cover(path)

        found = [(v.type, v.data) for k, v in tags.items() if k.startswith("APIC")]
        if found:
            return _front(found)

        if "covr" in tags and tags["covr"]:
            return bytes(tags["covr"][0])

        for key in ("metadata_block_picture", "METADATA_BLOCK_PICTURE"):
            if key in tags:
                vals = tags[key]
                found = []
                for raw in (vals if isinstance(vals, list) else [vals]):
                    try:
                        pic = Picture(base64.b64decode(raw))
                        found.append((pic.type, pic.data))
                    except Exception:
                        continue
                if found:
                    return _front(found)

        if "WM/Picture" in tags:
            vals = tags["WM/Picture"]
            found = [_wm_picture(bytes(getattr(v, "value", v))) for v in (vals if isinstance(vals, list) else [vals])]
            if data := _front(found):
                return data

        # APE tags (Monkey's Audio, WavPack, Musepack): "Cover Art (Front)" holds the file name, a NUL, the image
        found = []
        for key in tags.keys():
            if str(key).lower().startswith("cover art"):
                raw = bytes(getattr(tags[key], "value", tags[key]))
                found.append((3 if "front" in str(key).lower() else 0, raw.partition(b"\0")[2]))
        if found:
            return _front(found)

    except Exception:
        pass
    return _folder_cover(path)


def _tags(path: Path) -> dict:
    # shared persistent cache: a file's tags are read from disk at most once
    try:
        import library_cache
        t = library_cache.get_tags(path)
        return {"title": t["title"] or path.stem, "artist": t["artist"], "artists": t["artists"],
                "albumartist": t["albumartist"], "album": t["album"]}
    except Exception:
        return {"title": path.stem, "artist": "", "album": ""}


class Track:
    def __init__(self, path: Path, info: dict | None = None):
        self.path   = path
        if info is None:
            info = _tags(path)
        self.title  = info["title"] or path.stem
        self._set_artists(info)
        self.album  = info["album"]

    def _set_artists(self, info: dict):
        # artist is every name in one line, artists the names one by one
        self.artist = info["artist"]
        self.artists = info.get("artists") or split_artists([self.artist])
        self.albumartist = info.get("albumartist", "")

    def fill(self, info: dict):
        """Set the tags after the fact (see Player.add_many)."""
        self.title  = info["title"] or self.path.stem
        self._set_artists(info)
        self.album  = info["album"]
        self.pending = False

    pending = False        # True while only the file name is known

    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


class Player(QObject):
    track_changed      = Signal(object)
    state_changed      = Signal(bool)
    position_changed   = Signal(float, int, int)
    queue_changed      = Signal()
    order_changed      = Signal()                      # play order (mode/shuffle) changed
    sleep_stopped      = Signal()                      # stop-after-current fired (sleep timer)
    subsong_changed    = Signal(int, int)              # (current subsong, total) for multi-tune chiptunes
    chip_rendering     = Signal(bool)                  # True while a CLI render is in flight (loading state)
    chip_ready         = Signal(str)                   # rendered WAV path is ready (for the waveform bar)
    error              = Signal(str)                   # user-visible playback problem (shown in status bar)
    video_sound_changed = Signal(bool)                 # True: the sound of the video plays through our sink (the scope can read it)
    _chip_done         = Signal(int, object)           # (generation, wav or None), internal, worker to main thread
    _tags_filled       = Signal()                      # queue tags arrived from the background reader

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue:  list[Track] = []
        self._paths:  set[Path]   = set()   # O(1) dedupe for add()/add_many()
        self._idx:    int         = -1
        self._mode:   int         = PlayMode.SEQUENTIAL
        self._shuffle: list[int]  = []
        self._shuffle_on:   bool  = False
        self._trim_silence:   bool  = False
        self._enhance:        bool  = False
        self._stop_after_current: bool = False   # sleep-timer "end of track" mode
        self._chip_gen: int = 0                  # bumps each CLI render so stale ones are ignored
        self._fail_streak: int = 0               # consecutive unplayable tracks (guards skip loop)
        # SID subsong state (SIDs play as rendered WAVs, so the engine itself
        # knows nothing about subsongs; the player tracks which one is loaded)
        self._sid_path = None
        self._sid_sub = 0                        # 0-based, like the rest of the UI
        self._sid_count = 0
        self._sid_sub_pending = None             # subsong to use on the next load
        self._chip_prefetching = False           # a background chiptune render is running
        self._chip_foreground = False            # the user is waiting on a render right now
        self._chip_cancel = None                 # cancels the in-flight chip render
        self._prefetch_cancel = None             # cancels a background prefetch render
        self._chip_done.connect(self._on_chip_done)
        self._tags_filled.connect(self.queue_changed.emit)    # lists showing titles refresh

        # VGM/GME player for chiptune formats
        from vgm_player import VgmPlayer
        self._vgm = VgmPlayer(self)
        self._vgm.finished.connect(self._on_vgm_finished)
        self._vgm.position_changed.connect(self.position_changed)
        self._vgm.state_changed.connect(self.state_changed)
        self._vgm_active = False

        # Real-time DSP engine for plain audio (live, bypassable Enhance effect)
        from dsp_engine import DspEngine
        self._dsp = DspEngine(self)
        self._dsp.finished.connect(self._on_dsp_finished)
        self._dsp.position_changed.connect(self.position_changed)
        self._dsp.state_changed.connect(self.state_changed)
        self._dsp.advanced.connect(self._on_dsp_advanced)
        self._dsp_active = False
        self._vgm.set_stage(self._dsp)       # chiptunes share the EQ, DSP chain and Enhance

        # Furnace (.fur) module player via libfurnace_wrap (optional, needs the lib)
        self._fur = None
        self._fur_active = False
        self._fur_resume_frac = 0.0
        self._cur_device: QAudioDevice | None = None    # what the user chose
        # Seeded with what the engines already built themselves on, so the
        # first unrelated change to the device list (a webcam arriving, say)
        # is recognised as "already on it" instead of restarting the sink and
        # announcing a switch that did not happen.
        self._live_device: QAudioDevice | None = QMediaDevices.defaultAudioOutput()
        try:
            from fur_player import FurPlayer
            self._fur = FurPlayer(parent=self)
            self._fur.finished.connect(self._on_fur_finished)
            self._fur.position_changed.connect(self.position_changed)
        except Exception:
            self._fur = None

        self._base_vol:     float = 0.7
        self._fade_step:    int   = 0
        self._fade_start:   float = 0.0
        self._fade_end:     float = 0.0
        self._fade_cb               = None

        # QMediaPlayer is used for video only (audio goes through the DSP engine, chiptunes through VGM or fur).
        self._out    = QAudioOutput()
        self._media  = QMediaPlayer()
        self._media.setAudioOutput(self._out)
        self._out.setVolume(self._base_vol)
        # The sound of video goes through the DSP engine's stage (EQ, DSP chain, Enhance) on its own sink.
        from video_audio import VideoAudio
        self._vaudio = VideoAudio(self._media, self._out, self._dsp, parent=self)
        self._vaudio.active_changed.connect(self.video_sound_changed)
        self._vaudio.fell_back.connect(self._on_video_fell_back)

        self._next_timer = QTimer(self)      # see _set_engine_next
        self._next_timer.setSingleShot(True)
        self._next_timer.setInterval(1000)
        self._next_timer.timeout.connect(self._set_engine_next_now)
        # short fade on video play, pause and stop
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(_FADE_INTERVAL)
        self._fade_timer.timeout.connect(self._fade_tick)

        self._media.playbackStateChanged.connect(self._on_state)
        self._media.positionChanged.connect(self._on_pos)
        self._media.mediaStatusChanged.connect(self._on_status)

        # Recovery is driven by the sink failing, not by watching the hardware.
        #
        # Constructing a QMediaDevices instance, which is the documented way to
        # get audioOutputsChanged, starts Qt's PipeWire device monitor. On
        # PySide6 6.11 with Python 3.14 that monitor segfaulted inside its own
        # registry callback, on a PipeWire thread, seconds after the window was
        # up and with nothing playing:
        #
        #   pw_proxy_add_object_listener <- libpipewire-module-protocol-native
        #     <- libQt6Multimedia x4 <- protocol-native <- pw thread loop
        #
        # Nothing here can catch that, so the monitor is not started at all.
        # The engine says when its sink stops taking audio, which is the thing
        # actually worth knowing, and the outputs are enumerated once, then.
        self._dsp.device_lost.connect(self._on_output_gone)
        self._vgm.stalled.connect(self._on_output_gone)
        self._last_recover = 0.0
        self._lost_output = False        # an output went away and playback was paused; the next start settles on another

    # audio device

    def set_video_output(self, widget):
        self._vaudio.set_video_output(widget)       # the media player's output, unless the picture has to trail the sound

    def set_audio_device(self, device: QAudioDevice):
        """The device the user picked. Remembered even while it is unplugged.

        Kept apart from the one actually in use, so unplugging a chosen device
        falls back to the system default without forgetting the choice, and
        plugging it back in returns to it.
        """
        self._cur_device = device
        self._apply_device(device)

    def _apply_device(self, device: QAudioDevice):
        """Point every player at an output. Nothing here decides which one."""
        self._live_device = device
        self._lost_output = False
        self._out.setDevice(device)
        self._vaudio.set_audio_device(device)
        self._vgm.set_audio_device(device)
        self._dsp.set_audio_device(device)
        if self._fur is not None:
            self._fur._device = device   # applied on next .fur load

    def _on_device_lost(self):
        """The output stopped taking audio. Move to whatever is there now.

        Unplugging the device a sink is writing to raises nothing. The sink
        stops taking bytes and the feed loop keeps offering them to something
        that has gone, so playback stalls with no error anywhere. Another device
        only takes over once every player is pointed at it, because each one
        still holds the handle to the one that left.

        Rate limited, because a replacement that does not work either reports
        the same failure on the next tick and would be rebuilt on every one.
        """
        now = _time.monotonic()
        if now - self._last_recover < _RECOVER_GAP:
            return
        self._last_recover = now
        self._recover_device()

    def _pick_output(self) -> QAudioDevice:
        """The chosen output, or the default if the chosen one has gone. Null when there is no output at all."""
        live = QMediaDevices.audioOutputs()
        want = self._cur_device
        if want is not None and not any(d.id() == want.id() for d in live):
            want = None                          # the chosen one is gone for now
        return want if want is not None else QMediaDevices.defaultAudioOutput()

    def _recover_device(self):
        """Point everything at the chosen output, or the default if it has gone."""
        dev = self._pick_output()
        if dev.isNull():
            return                               # no outputs at all, nothing to move to
        if self._live_device is not None and self._live_device.id() == dev.id():
            return                               # already on it
        self._apply_device(dev)
        self.error.emit(f"Audio output changed to {dev.description()}")

    def _on_output_gone(self):
        """The output stopped taking audio, which is what unplugging it looks like. Pause, as if the pause button
        was pressed, instead of leaving the sound frozen.

        The sink stops taking bytes and says nothing, or says it once, so the feed loop keeps offering audio to
        something that has gone. Nothing is moved yet: Qt may still list the device that left, and the person who
        unplugged it may be about to plug something else in. The next thing that starts sound, Play or a track,
        calls _settle_output.
        """
        if self._lost_output:
            return
        self._lost_output = True
        self.pause()
        self.error.emit("Audio output disconnected, playback paused")

    def _settle_output(self) -> bool:
        """After an output was lost: point every player at the chosen output, or the default if that has gone too.
        Also when it is the output that was in use, whose stream is what died. False when there is no output at
        all, and then nothing starts."""
        if not self._lost_output:
            return True
        dev = self._pick_output()
        if dev.isNull():
            self.error.emit("No audio output found")
            return False
        moved = self._live_device is None or self._live_device.id() != dev.id()
        self._apply_device(dev)                  # clears _lost_output
        if moved:
            self.error.emit(f"Audio output changed to {dev.description()}")
        return True

    def _on_video_fell_back(self, why: str):
        """The sound of a video went back to the player's own output. When our sink failed, its device is the likely
        cause and the player's output faces the same one, so move everything to what works now. VideoAudio takes the
        sound back when the output changes, and tries again by itself a little later if it did not."""
        if self._vaudio.device_failed:
            self._on_device_lost()

    def set_keep_output_open(self, on: bool):
        """Settings: keep the sound output open while nothing plays, so it stays wired in a patchbay."""
        self._dsp.set_keep_open(on)

    def set_crossfade(self, ms: int):
        """Crossfade between consecutive audio tracks (0 is off, which is gapless)."""
        self._dsp.set_crossfade(ms)

    # sleep timer "end of track"

    def set_stop_after_current(self, on: bool):
        """When on, playback stops once the current track ends instead of
        advancing. Also disables gapless preload so the track actually finishes."""
        self._stop_after_current = bool(on)
        if on:
            self._dsp.clear_next()          # no gapless crossing into the next track
        else:
            self._set_engine_next()         # restore gapless preload

    @property
    def stop_after_current(self) -> bool:
        return self._stop_after_current

    def _consume_stop_after_current(self) -> bool:
        if self._stop_after_current:
            self._stop_after_current = False
            self.stop()
            self.sleep_stopped.emit()
            return True
        return False

    # Enhance DSP ("magic wand")

    def set_enhance(self, on: bool):
        # live wet/dry switch in the DSP engine's signal path, takes effect at once with no reload
        self._enhance = bool(on)
        self._dsp.set_enabled(self._enhance)

    @property
    def enhance(self) -> bool:
        return self._enhance

    @property
    def eq(self):
        """The user parametric EQ (lives in the DSP engine; edited by the EQ panel)."""
        return self._dsp.user_eq

    def output_window(self, n: int = 2048):
        """Recent post-EQ output samples for the EQ analyzer (None unless playing audio)."""
        if self._vaudio.active or (self._dsp_active and self._dsp.is_playing) or (self._vgm_active and self._vgm.is_playing):
            return self._dsp.output_window(n)
        return None

    def mono_window(self, position_ms: int, n: int):
        """Mono window at the play head from the engine's decode (scope/spectrum)."""
        if self._dsp_active:
            return self._dsp.mono_window(position_ms, n)
        if self._vaudio.active:
            # The picture clock is ahead of what is heard by the lag, so the sound at this moment is a little before it.
            lag = self._vaudio.lag_ms() or 0.0
            return self._vaudio.window(position_ms - lag, n)
        return None

    @property
    def video_sound_tapped(self) -> bool:
        """True while the sound of the video plays through our sink, which is when mono_window() can serve it."""
        return self._vaudio.active

    def video_sound_offset_ms(self):
        """How far the sound of the video trails the picture right now in ms (negative: leads it), None if not known."""
        return self._vaudio.offset_ms()

    def set_video_delay(self, ms: int):
        """Settings: video sound delay in ms, -200 to 200. Positive plays our sound later, negative holds the picture back."""
        self._vaudio.set_delay_ms(ms)

    def audio_tracks(self) -> list[str]:
        """Names of the audio tracks of the video that is loaded, [] when it has one or none."""
        from PySide6.QtMultimedia import QMediaMetaData
        K = QMediaMetaData.Key
        out = []
        for i, md in enumerate(self._media.audioTracks()):
            name = md.stringValue(K.Title) or md.stringValue(K.Language)
            out.append(name if name and name != "Unspecified" else f"Track {i + 1}")
        return out if len(out) > 1 else []

    @property
    def audio_track(self) -> int:
        return self._media.activeAudioTrack()

    def set_audio_track(self, i: int):
        """Switch the sound of the video to another of its audio tracks, keeping the picture running."""
        self._media.setActiveAudioTrack(int(i))
        self._vaudio.track_changed()     # QMediaPlayer does not say so itself

    @staticmethod
    def audio_devices() -> list[QAudioDevice]:
        return QMediaDevices.audioOutputs()

    # volume / fade

    def _set_out_volume(self, v: float):
        """The video output's volume, and the level of the video sound while our own sink plays it."""
        self._out.setVolume(v)
        self._vaudio.set_level(v)

    def set_video_dsp(self, on: bool):
        """Settings switch: send the sound of videos through the EQ, DSP chain and Enhance. Applies at once, also to a
        video that is playing."""
        self._vaudio.set_enabled(bool(on))

    def _start_fade(self, start: float, end: float, callback=None):
        self._fade_timer.stop()
        self._fade_step  = 0
        self._fade_start = start
        self._fade_end   = end
        self._fade_cb    = callback
        self._fade_timer.start()

    def _fade_tick(self):
        self._fade_step += 1
        t   = self._fade_step / _FADE_STEPS
        vol = self._fade_start + (self._fade_end - self._fade_start) * t
        self._set_out_volume(max(0.0, min(1.0, vol)))
        if self._fade_step >= _FADE_STEPS:
            self._fade_timer.stop()
            self._set_out_volume(self._fade_end)
            if self._fade_cb:
                cb, self._fade_cb = self._fade_cb, None
                cb()

    # state signals

    def _on_state(self, state):
        self.state_changed.emit(self._media.playbackState() ==
                                QMediaPlayer.PlaybackState.PlayingState)

    def _on_pos(self, pos_ms):
        dur = self._media.duration()
        frac = pos_ms / dur if dur > 0 else 0.0
        self.position_changed.emit(frac, pos_ms, dur)

    def _on_status(self, status):
        # _media handles video only; audio plays through the DSP engine.
        MS = QMediaPlayer.MediaStatus
        if status == MS.EndOfMedia:
            if self._mode == PlayMode.REPEAT_ONE:
                self._media.setPosition(0)
                self._vaudio.seek()
                self._media.play()
            elif not self._consume_stop_after_current():
                self.next()

    def _on_dsp_finished(self):
        if self._mode == PlayMode.REPEAT_ONE:
            self._dsp.play()
        elif not self._consume_stop_after_current():
            self.next()

    # play-order helper (used for gapless preload and Up Next)

    def _next_queue_idx(self) -> int:
        """Return the queue index that would play after current, or -1."""
        n = len(self._queue)
        if n == 0 or self._idx < 0:
            return -1
        if self._mode == PlayMode.REPEAT_ONE:
            return self._idx
        if self._shuffle_on:
            if not self._shuffle:
                self._rebuild_shuffle()
            try:
                pos = self._shuffle.index(self._idx)
                if self._mode != PlayMode.REPEAT_ALL and pos == len(self._shuffle) - 1:
                    return -1
                return self._shuffle[(pos + 1) % len(self._shuffle)]
            except ValueError:
                return -1
        else:
            if self._mode == PlayMode.REPEAT_ALL:
                return (self._idx + 1) % n
            elif self._idx < n - 1:
                return self._idx + 1
            return -1

    # playback control

    def _on_vgm_finished(self):
        self._vgm_active = False
        if not self._consume_stop_after_current():
            self.next()

    def _play_idx(self, idx: int):
        if not self._queue or not self._settle_output():
            return
        self._dsp.t_request = _time.monotonic()     # the engine logs how long until the first sample
        self._fade_timer.stop()
        self._fade_cb = None
        target = idx % len(self._queue)

        self._idx = target
        t = self._queue[self._idx]
        if t.pending:                       # the one track that plays must have its real tags now
            t.fill(_tags(t.path))
        # Unavailable file (unplugged drive, deleted outside the app): skip with a
        # status message instead of handing every engine a path that can't open.
        if not t.path.exists():
            self._skip_unplayable(t, reason="File missing")
            return
        ext = t.path.suffix.lower()

        # C64 SID: scan (render and find the length), then play through the DSP engine.
        # SIDs carry no duration, so the tune is scanned once before it plays and the
        # seek bar is real from the first sample.
        if SID_ENABLED and ext in _SID_EXTS and sid_player.available():
            self._stop_media()
            self._stop_vgm()
            self._stop_fur()
            self._stop_dsp()
            self._chip_gen += 1
            gen = self._chip_gen
            self._cancel_chip_render()
            # retire any live .fur stream before a SID engine starts: the two
            # native chiptune engines must never run at once
            if self._fur is not None:
                self._fur.stop_stream()
            info = sid_player.read_info(t.path)
            prev_sid_path   = self._sid_path
            self._sid_path  = t.path
            self._sid_count = info.subsongs if info else 1
            same_file = (prev_sid_path == t.path)
            if self._sid_sub_pending is not None:
                self._sid_sub = self._sid_sub_pending
                self._sid_sub_pending = None
            elif same_file:
                pass          # replaying the same tune: stay on the chosen subsong
            else:
                self._sid_sub = (info.start_song - 1) if info else 0
            self._sid_sub = max(0, min(self._sid_sub, self._sid_count - 1))
            # track_changed is emitted after load_stream below, not here. The waveform
            # bar reads the engine buffer on track_changed, and at this point that
            # buffer still holds the previous subsong, so it would cache the old
            # shape under the new subsong's key.

            # If the length is already known (database, or an earlier scan) there is
            # nothing to wait for: stream the tune straight into the engine and start
            # on the first chunk, milliseconds instead of the seconds a full render of
            # a long subsong takes.
            known = None
            try:
                known = sid_player.known_length(t.path, self._sid_sub + 1)
            except Exception:
                known = None
            if known:
                # Render at the engine's rate: load_stream appends frames to the
                # playback buffer verbatim (no resampling), so a 44.1 kHz render
                # fed to a 48 kHz engine plays 8.8% fast, a semitone and a half
                # sharp. The scan/WAV path is unaffected (ffmpeg resamples it).
                reader = sid_player.StreamReader(t.path, self._sid_sub + 1,
                                                 rate=self._dsp.fs)
                if reader.ok():
                    self._dsp_active = True
                    self._dsp.volume = self._base_vol
                    self._dsp.set_enabled(self._enhance)
                    self._dsp.load_stream(reader, int(known * 1000), str(t.path))
                    self._dsp.play()
                    self.track_changed.emit(t)      # buffer is reset by now
                    self.state_changed.emit(True)
                    self._fail_streak = 0
                    if self._sid_count > 1:
                        self.subsong_changed.emit(self._sid_sub, self._sid_count)
                    QTimer.singleShot(1500, self._prefetch_chip)
                    return

            # unknown length: scan first (renders once, then cached)
            self.track_changed.emit(t)
            self.state_changed.emit(False)
            self.chip_rendering.emit(True)
            self._chip_foreground = True

            def _work(p=t.path, g=gen, sub=self._sid_sub):
                wav = None
                try:
                    wav, _len, _src = sid_player.scan(p, sub + 1)   # lib is 1-based
                except Exception:
                    wav = None
                finally:
                    self._chip_foreground = False
                self._chip_done.emit(g, wav or None)

            _threading.Thread(target=_work, daemon=True).start()
            return
        self._sid_path = None

        # Furnace .fur modules: streamed from libfurnace into the DSP engine, or
        # rendered first when streaming is not possible. They play through the engine
        # like every other chiptune, so EQ and Enhance, the engine-driven visualizers,
        # the waveform bar and sample-accurate seek all apply from the first sample.
        # Playing natively first and switching later made the DSP audibly kick in a
        # moment into the song.
        if ext in _FUR_EXTS and self._fur is not None:
            self._stop_media()
            self._stop_vgm()
            self._stop_fur()
            self._stop_dsp()
            self._chip_gen += 1
            gen = self._chip_gen
            self._fur_resume_frac = 0.0
            self._cancel_chip_render()
            self.track_changed.emit(t)

            # Stream off the singleton engine: the duration arrives with the load in
            # about 20 ms and audio starts on the first chunk. (Streaming was once
            # unsafe because of an upstream memset typo that left garbage in
            # DivEngine::filePlayerBuf; fixed in our engine build, see
            # _furnace_wrapper_src/furnace-dev250-build-fixes.patch.)
            try:
                reader, dur_ms = self._fur.stream_reader(t.path, self._dsp.fs)
            except Exception:
                reader, dur_ms = None, 0
            if reader is not None and dur_ms > 0:
                self._dsp_active = True
                self._dsp.volume = self._base_vol
                self._dsp.set_enabled(self._enhance)
                self._dsp.load_stream(reader, dur_ms, str(t.path))
                self._dsp.play()
                self.track_changed.emit(t)
                self.state_changed.emit(True)
                self._fail_streak = 0
                QTimer.singleShot(1500, self._prefetch_chip)
                return

            # could not stream it: render, then play
            self.state_changed.emit(False)
            self.chip_rendering.emit(True)
            self._chip_foreground = True
            self._chip_cancel = _threading.Event()

            def _work(p=t.path, g=gen, cancel=self._chip_cancel):
                wav = None
                try:
                    wav = self._fur.render_cached(p, cancel=cancel)
                except Exception:
                    wav = None
                finally:
                    self._chip_foreground = False
                self._chip_done.emit(g, wav)

            _threading.Thread(target=_work, daemon=True).start()
            return
        self._stop_fur()

        # AY/YM/VGM and tracker modules: render to WAV (zxtune or openmpt), then play
        # through the DSP engine. Rendered playback has correct timing for .ay, handles
        # the AY and X1 VGM files libgme cannot, and gives the waveform, seek,
        # visualizers and DSP.
        if cli_render.supported(ext):
            self._stop_media()
            self._stop_vgm()
            self._stop_fur()
            self._stop_dsp()
            self._chip_gen += 1
            gen = self._chip_gen
            self._cancel_chip_render()
            self.track_changed.emit(t)
            self.state_changed.emit(False)
            self.chip_rendering.emit(True)
            path = t.path

            def _work(p=path, g=gen):
                wav = None
                try:
                    wav = cli_render.render(p)
                except Exception:
                    wav = None
                self._chip_done.emit(g, wav)

            _threading.Thread(target=_work, daemon=True).start()
            return

        # VGM/chiptune formats: libgme
        if ext in _GME_EXTS:
            self._stop_media()
            self._stop_dsp()
            self._dsp.volume = self._base_vol      # the stage applies the volume to chiptunes
            self._vgm.volume = self._base_vol
            if self._vgm.load(t.path):
                self._vgm_active = True
                self._vgm.play()
                self.track_changed.emit(t)
                self.subsong_changed.emit(self._vgm.current_track, self._vgm.track_count)
                self._fail_streak = 0
            else:
                self._skip_unplayable(t)
            return
        if self._vgm_active:
            self._vgm.stop()
            self._vgm_active = False

        # Video: QMediaPlayer; VideoAudio taps its sound into the DSP stage when that is on
        if ext in VIDEO_EXTS:
            self._stop_dsp()
            self._set_out_volume(0.0)
            self._media.setLoops(
                QMediaPlayer.Infinite if self._mode == PlayMode.REPEAT_ONE else 1
            )
            self._vaudio.start()
            self._media.setSource(QUrl.fromLocalFile(str(t.path)))
            self._media.play()
            self.track_changed.emit(t)
            self._set_out_volume(self._base_vol)
            self._fail_streak = 0
            return

        # Plain audio: real-time DSP engine (live Enhance)
        self._stop_media()
        self._dsp_active = True
        self._dsp.volume = self._base_vol
        self._dsp.set_enabled(self._enhance)
        self._dsp.load(t.path, trim=self._trim_silence)
        self._dsp.play()
        self.track_changed.emit(t)
        self._fail_streak = 0
        self._set_engine_next()
        QTimer.singleShot(1500, self._prefetch_chip)

    def _set_engine_next(self):
        # A preload decodes a whole track, so it waits until the track has played for a
        # second, otherwise clicking through a list would start one for every song passed.
        self._next_timer.start(1000)

    def _set_engine_next_now(self):
        """Preload the upcoming track into the DSP engine for gapless playback
        (only when it's a plain-audio track the engine can continue into)."""
        if not self._dsp_active:
            return
        if self._stop_after_current:
            self._dsp.clear_next()       # sleep timer: let the current track end
            return
        nxt = self._next_queue_idx()
        if nxt < 0:
            self._dsp.clear_next()
            return
        t = self._queue[nxt]
        ext = t.path.suffix.lower()
        if ext in _GME_EXTS or ext in _FUR_EXTS or ext in VIDEO_EXTS or cli_render.supported(ext):
            self._dsp.clear_next()   # can't gaplessly cross into non-engine formats
            return
        # The decoded song sits in memory until it plays, as large as the song is long. It is decoded a minute before it is
        # needed, not at the start of the one before, which halves what a player holds for most of every song and saves
        # hundreds of megabytes when one is an hour long. Decoding runs far faster than playing.
        left = (self.duration_ms - self.position_ms) / 1000.0
        if self.duration_ms > 0 and left > _PRELOAD_LEAD_S + 2:
            self._dsp.clear_next()
            self._next_timer.start(int((left - _PRELOAD_LEAD_S) * 1000))
            return
        self._dsp.set_next(t.path, nxt, trim=self._trim_silence)

    def _on_chip_done(self, gen: int, wav):
        """A CLI render finished. If it is still the current track, play the WAV
        through the DSP engine so the waveform, seek, visualizers and DSP all work."""
        if gen != self._chip_gen:
            return                       # user moved on while it was rendering
        self.chip_rendering.emit(False)
        if not wav:
            t = self._queue[self._idx] if 0 <= self._idx < len(self._queue) else None
            name = t.path.name if t else "track"
            if t is not None and t.path.suffix.lower() in _FUR_EXTS:
                hint = "unsupported or corrupt module"   # libfurnace is built in
            else:
                hint = cli_render.missing_hint()
            self.error.emit(f"Could not render {name}: {hint}")
            self._advance_past_failure()
            return
        self._fail_streak = 0
        self._stop_media()
        self._dsp_active = True
        self._dsp.volume = self._base_vol
        self._dsp.set_enabled(self._enhance)
        self._dsp.load(wav, trim=False)
        self._dsp.play()
        self.chip_ready.emit(wav)        # waveform bar loads the rendered WAV
        self.state_changed.emit(True)
        if self._sid_path is not None and self._sid_count > 1:
            self.subsong_changed.emit(self._sid_sub, self._sid_count)
        QTimer.singleShot(1500, self._prefetch_chip)

    def _cancel_chip_render(self):
        """Abandon renders nobody is waiting for any more.

        Rendering holds an exclusive lock and runs to completion, so without this
        clicking through a playlist of modules queues every skipped render ahead
        of the one actually wanted, and each click gets slower than the last. The
        C renderer polls a cancel flag, so a superseded render aborts in
        milliseconds and releases the lock.
        """
        for attr in ("_chip_cancel", "_prefetch_cancel"):
            ev = getattr(self, attr, None)
            if ev is not None:
                ev.set()
            setattr(self, attr, None)

    def _prefetch_chip(self):
        """Pre-render the next chiptune in play order into the render cache.

        Some chiptunes are emulated to a WAV before they can play (the scan pass for
        SID, the zxtune and openmpt renders). Doing that when the track is reached
        means a visible wait; doing it now means the cache is already warm and the
        next track starts immediately with its real duration.

        One at a time, and not while a foreground render is in flight.
        """
        # Never compete with a render the user is actually waiting for: the
        # native lock is exclusive, so a prefetch started now would delay it.
        if self._chip_prefetching or self._chip_foreground:
            return
        self._prefetch_cancel = _threading.Event()
        for idx in self.upcoming(2):
            if not 0 <= idx < len(self._queue):
                continue
            p = self._queue[idx].path
            ext = p.suffix.lower()
            if SID_ENABLED and ext in _SID_EXTS and sid_player.available():
                job = lambda q=p: sid_player.scan(q, 1)
            elif ext in _FUR_EXTS:
                # .fur streams instantly, so there is nothing to prefetch, and the C
                # side refuses singleton reloads while a stream is live.
                continue
            elif cli_render.supported(ext):
                job = lambda q=p: cli_render.render(q)
            else:
                continue

            def _work(fn=job):
                try:
                    fn()
                except Exception:
                    pass
                finally:
                    self._chip_prefetching = False

            self._chip_prefetching = True
            _threading.Thread(target=_work, daemon=True).start()
            return

    def _skip_unplayable(self, t: "Track", reason: str = "Can't play"):
        """A track failed to load: tell the user and move on instead of going silent."""
        self.error.emit(f"{reason}: {t.path.name}, skipping")
        self.state_changed.emit(False)
        self._advance_past_failure()

    def _advance_past_failure(self):
        self._fail_streak += 1
        if self._fail_streak >= max(1, len(self._queue)):
            # every track in the queue failed: stop instead of looping forever
            self._fail_streak = 0
            return
        QTimer.singleShot(0, self.next)

    def _on_dsp_advanced(self, idx: int):
        """The engine gaplessly continued into the preloaded next track."""
        if 0 <= idx < len(self._queue):
            self._idx = idx
            self.track_changed.emit(self._queue[idx])
            self.state_changed.emit(True)
            self._set_engine_next()

    def _stop_media(self):
        """Silence and release the QMediaPlayer (used for video)."""
        self._media.setLoops(1)
        self._vaudio.silence()
        self._media.stop()
        self._media.setSource(QUrl())
        self._vaudio.stop(drain=False)      # after the media is stopped, so the player's own output is never heard

    def _stop_dsp(self):
        if self._dsp_active or self._dsp.is_playing:
            self._dsp.stop()
        self._dsp_active = False

    def _stop_vgm(self):
        if self._vgm_active:
            self._vgm.stop()
        self._vgm_active = False

    def _stop_fur(self):
        if self._fur is not None and self._fur_active:
            self._fur.stop()
        self._fur_active = False

    def _on_fur_finished(self):
        self._fur_active = False
        if not self._consume_stop_after_current():
            self.next()

    def _rebuild_shuffle(self):
        self._shuffle = list(range(len(self._queue)))
        random.shuffle(self._shuffle)

    # public API

    def add(self, path: Path):
        if path in self._paths:
            return
        self._queue.append(Track(path))
        self._paths.add(path)
        if self._shuffle_on:
            self._rebuild_shuffle()

    def add_many(self, paths: list[Path]):
        # Bulk path: O(n) dedupe via the set, one batched tag lookup for all new
        # files (instead of one SQLite query per Track), and rebuild the shuffle
        # order once at the end rather than per-track.
        new = [p for p in paths if p not in self._paths]
        infos: dict = {}
        if len(new) > 1:
            try:
                import library_cache
                # Cached tags only. Reading the rest with mutagen here blocked the
                # GUI for seconds on a folder the app had not seen before.
                infos = library_cache.get_many(new, read_missing=False)
            except Exception:
                infos = {}
        later = []
        for p in new:
            info = infos.get(p)
            if info is None and len(new) > 1:
                info = {"title": "", "artist": "", "album": ""}      # the file name until read
                t = Track(p, info)
                t.pending = True
                later.append(t)
            else:
                t = Track(p, info)
            self._queue.append(t)
            self._paths.add(p)
        if new and self._shuffle_on:
            self._rebuild_shuffle()
        self.queue_changed.emit()
        if later:
            self._read_tags_later(later)

    def _read_tags_later(self, tracks: list):
        """Read the tags of tracks the cache did not know, off the GUI thread, and
        tell the UI as they arrive (at most about once a second, so a long list
        does not rebuild the lists it feeds over and over)."""
        def work():
            import library_cache
            last = 0.0
            for i in range(0, len(tracks), 100):
                chunk = tracks[i:i + 100]
                try:
                    infos = library_cache.get_many([t.path for t in chunk])
                except Exception:
                    continue
                for t in chunk:
                    if t.path in infos:
                        t.fill(infos[t.path])
                now = _time.monotonic()
                if now - last > 1.0:
                    last = now
                    self._tags_filled.emit()
            self._tags_filled.emit()
        _threading.Thread(target=work, name="queue-tags", daemon=True).start()

    def reorder(self, paths) -> bool:
        """Put the queue in this order, which is what the playlist shows after a sort
        or a drag, so that next and previous follow it. The track that is playing stays
        the current one. False (and nothing changes) if paths is not the same set of
        tracks as the queue."""
        by = {t.path: t for t in self._queue}
        new = [by[p] for p in paths if p in by]
        if len(new) != len(self._queue):
            return False
        cur = self.current
        self._queue[:] = new
        self._idx = new.index(cur) if cur in new else -1
        if self._shuffle_on:
            self._rebuild_shuffle()
        self.order_changed.emit()
        return True

    def play(self):
        if not self._settle_output():
            return
        if self._vgm_active:
            self._vgm.resume()
            return
        if self._fur_active:
            if not self._fur.is_playing():
                self._fur.play()
                if self._fur_resume_frac > 0:
                    self._fur.seek(self._fur_resume_frac)
                    self._fur_resume_frac = 0.0
                self.state_changed.emit(True)
            return
        if self._dsp_active:
            if self._dsp.is_playing:
                return
            if self._dsp.paused:
                self._dsp.play()
            elif 0 <= self._idx < len(self._queue):
                self._play_idx(self._idx)   # stopped or ended: reload the current track
            return
        state = self._media.playbackState()
        if state == QMediaPlayer.PlaybackState.PausedState:
            self._set_out_volume(0.0)
            self._media.play()
            self._vaudio.resume()
            self._start_fade(0.0, self._base_vol)
        elif state == QMediaPlayer.PlaybackState.StoppedState and 0 <= self._idx < len(self._queue):
            self._play_idx(self._idx)
        elif self._idx < 0 and self._queue:
            self._play_idx(0)

    def pause(self):
        if self._vgm_active:
            self._vgm.pause()
            return
        if self._fur_active:
            # FurPlayer has no native pause: remember the position and stop.
            self._fur_resume_frac = self._fur.position_frac
            self._fur.stop()
            self.state_changed.emit(False)
            return
        if self._dsp_active:
            self._dsp.pause()
            return
        cur = self._out.volume()
        if cur > 0.01:
            self._start_fade(cur, 0.0, self._pause_media)
        else:
            self._pause_media()

    def _pause_media(self):
        self._media.pause()
        self._vaudio.pause()

    def toggle(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        if self._vgm_active:
            self._vgm.stop()
            self._vgm_active = False
            return
        if self._fur_active:
            self._fur.stop()           # _fur_active stays set so play() restarts it
            self._fur_resume_frac = 0.0
            self.state_changed.emit(False)
            return
        if self._dsp_active:
            self._dsp.stop()   # _dsp_active stays set so play() reloads the track
            return
        cur = self._out.volume()
        def _do_stop():
            self._media.setLoops(1)
            self._media.stop()
            self._vaudio.stop()
        if cur > 0.01:
            self._start_fade(cur, 0.0, _do_stop)
        else:
            _do_stop()

    # subsongs (multi-tune chiptunes: NSF, SAP, GBS, AY, KSS, SID)
    @property
    def subsong_count(self) -> int:
        if self._sid_path is not None:
            return self._sid_count
        return self._vgm.track_count if self._vgm_active else 0

    @property
    def current_subsong(self) -> int:
        if self._sid_path is not None:
            return self._sid_sub
        return self._vgm.current_track if self._vgm_active else 0

    def subsong_titles(self) -> list[str]:
        """Labels for a subsong picker, or [] when the file has no subsongs."""
        n = self.subsong_count
        return [f"Subsong {i + 1}" for i in range(n)] if n > 1 else []

    def set_subsong(self, idx: int):
        """Switch to subsong `idx` of the current chiptune (no-op for other files)."""
        if not self._queue:
            return
        # SID: each subsong is its own scan+render, so reload the track with the
        # requested subsong pinned (the scan is cached, so repeats are instant).
        if self._sid_path is not None:
            idx = max(0, min(idx, self._sid_count - 1))
            if idx == self._sid_sub:
                return
            self._sid_sub_pending = idx
            self._play_idx(self._idx)
            return
        if not self._vgm_active:
            return
        count = self._vgm.track_count
        idx = max(0, min(idx, count - 1))
        if idx == self._vgm.current_track:
            return
        path = self._queue[self._idx].path
        self._vgm.stop()
        if self._vgm.load(path, idx):
            self._vgm.play()
            self.subsong_changed.emit(self._vgm.current_track, self._vgm.track_count)
            self.state_changed.emit(True)

    def next(self):
        if not self._queue:
            return
        n = len(self._queue)
        if self._shuffle_on:
            if not self._shuffle:
                self._rebuild_shuffle()
            try:
                pos = self._shuffle.index(self._idx)
            except ValueError:                  # nothing is selected yet: start at the front of the shuffle
                self._play_idx(self._shuffle[0])
                return
            if pos == n - 1 and self._mode != PlayMode.REPEAT_ALL:
                return
            self._play_idx(self._shuffle[(pos + 1) % n])
        elif self._mode == PlayMode.REPEAT_ALL or self._idx < n - 1:
            self._play_idx((self._idx + 1) % n)

    def prev(self):
        if not self._queue:
            return
        if self.position_ms > 3000:
            self.seek(0.0)
        elif self._shuffle_on:
            if not self._shuffle:
                self._rebuild_shuffle()
            try:
                pos = self._shuffle.index(self._idx)
                self._play_idx(self._shuffle[(pos - 1) % len(self._shuffle)])
            except ValueError:
                self._play_idx(self._shuffle[0])
        else:
            self._play_idx((self._idx - 1) % len(self._queue))

    def play_track(self, idx: int):
        self._play_idx(idx)

    def load_current(self, idx: int):
        """Select a track as the current one without playing it. The UI shows it and
        a later play() reloads it from the stopped state. Used to restore the last
        session's track at launch."""
        if not (0 <= idx < len(self._queue)):
            return
        self._idx = idx
        self.track_changed.emit(self._queue[idx])

    def remove(self, idx: int):
        if not 0 <= idx < len(self._queue):
            return
        if idx == self._idx:
            self.stop()
            self._idx = -1
        elif idx < self._idx:
            self._idx -= 1
        removed = self._queue.pop(idx)
        self._paths.discard(removed.path)
        if self._shuffle_on:
            self._rebuild_shuffle()
        self.queue_changed.emit()

    def rename_track(self, idx: int, new_path: Path):
        """Update a queued track's path in place (after a rename on disk), keeping
        the dedupe set in sync."""
        if not 0 <= idx < len(self._queue):
            return
        t = self._queue[idx]
        self._paths.discard(t.path)
        t.path  = new_path
        t.title = new_path.stem
        self._paths.add(new_path)

    def clear(self):
        self._fade_timer.stop()
        self._fade_cb = None
        self._stop_dsp()
        self._stop_vgm()
        self._stop_fur()
        self._set_out_volume(0.0)
        self._media.setLoops(1)
        self._media.stop()
        self._vaudio.stop(drain=False)
        self._queue.clear()
        self._paths.clear()
        self._idx = -1
        self.queue_changed.emit()

    def seek(self, frac: float):
        if self._vgm_active:
            self._vgm.seek(frac)
            return
        if self._fur_active:
            self._fur.seek(frac)
            return
        if self._dsp_active:
            self._dsp.seek(frac)
            self._set_engine_next()          # the time left changed, so when to decode the next song did
            return
        dur = self._media.duration()
        if dur > 0:
            self._media.setPosition(int(frac * dur))
            self._vaudio.seek()

    def seek_delta(self, seconds: int):
        if self._vgm_active:
            frac = (self._vgm.position_ms + seconds * 1000) / max(1, self._vgm.duration_ms)
            self._vgm.seek(max(0.0, min(1.0, frac)))
            return
        if self._fur_active:
            frac = (self._fur.position_ms + seconds * 1000) / max(1, self._fur.duration_ms)
            self._fur.seek(max(0.0, min(1.0, frac)))
            return
        if self._dsp_active:
            dur = max(1, self._dsp.duration_ms)
            frac = (self._dsp.position_ms + seconds * 1000) / dur
            self._dsp.seek(max(0.0, min(1.0, frac)))
            return
        self._media.setPosition(max(0, self._media.position() + seconds * 1000))
        self._vaudio.seek()

    # mode setter (updates loop flag on active player)

    @property
    def mode(self) -> int:
        return self._mode

    @mode.setter
    def mode(self, val: int):
        self._mode = val
        # Video (QMediaPlayer) uses native looping for repeat-one; audio repeat is
        # handled in _on_dsp_finished, chiptunes in _on_vgm_finished.
        self._media.setLoops(
            QMediaPlayer.Infinite if val == PlayMode.REPEAT_ONE else 1
        )
        self._set_engine_next()      # re-target gapless preload to the new order
        self.order_changed.emit()

    def upcoming(self, limit: int = 200) -> list[int]:
        """Queue indices that will play after the current one, in play order."""
        n = len(self._queue)
        if n == 0 or self._idx < 0:
            return []
        if self._mode == PlayMode.REPEAT_ONE:
            return [self._idx]
        if self._shuffle_on:
            if not self._shuffle:
                self._rebuild_shuffle()
            try:
                pos = self._shuffle.index(self._idx)
            except ValueError:
                return []
            rest = self._shuffle[pos + 1:]
            if self._mode == PlayMode.REPEAT_ALL:
                rest = rest + self._shuffle[:pos]
        else:
            rest = list(range(self._idx + 1, n))
            if self._mode == PlayMode.REPEAT_ALL:
                rest = rest + list(range(0, self._idx))
        return rest[:limit]

    # properties

    @property
    def is_playing(self) -> bool:
        if self._vgm_active:
            return self._vgm.is_playing
        if self._fur_active:
            return self._fur.is_playing()
        if self._dsp_active:
            return self._dsp.is_playing
        return self._media.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def position_frac(self) -> float:
        if self._fur_active:
            return self._fur.position_frac
        if self._dsp_active:
            dur = self._dsp.duration_ms
            return self._dsp.position_ms / dur if dur > 0 else 0.0
        dur = self._media.duration()
        return self._media.position() / dur if dur > 0 else 0.0

    @property
    def position_ms(self) -> int:
        if self._fur_active:
            return self._fur.position_ms
        if self._dsp_active:
            return self._dsp.position_ms
        return self._media.position()

    @property
    def sample_rate(self) -> int:
        """The rate of the audio the engine hands the visuals: the output device's."""
        return int(getattr(self._dsp, "fs", 44100))

    @property
    def duration_ms(self) -> int:
        if self._fur_active:
            return self._fur.duration_ms
        if self._dsp_active:
            return self._dsp.duration_ms
        return self._media.duration()

    @property
    def volume(self) -> float:
        return self._base_vol

    @volume.setter
    def volume(self, val: float):
        self._base_vol = max(0.0, min(1.0, val))
        self._set_out_volume(self._base_vol)
        self._vgm.volume = self._base_vol
        self._dsp.volume = self._base_vol
        if self._fur is not None:
            self._fur.volume = self._base_vol

    @property
    def current(self) -> Optional[Track]:
        if 0 <= self._idx < len(self._queue):
            return self._queue[self._idx]
        return None

    @property
    def queue(self) -> list[Track]:
        return self._queue

    @property
    def index(self) -> int:
        return self._idx

    @property
    def shuffle(self) -> bool:
        return self._shuffle_on

    @shuffle.setter
    def shuffle(self, val: bool):
        self._shuffle_on = val
        if val:
            self._rebuild_shuffle()
        self._set_engine_next()      # re-target gapless preload to the new order
        self.order_changed.emit()

    @property
    def shuffle_order(self) -> list[int]:
        return list(self._shuffle)

    @property
    def trim_silence(self) -> bool:
        return self._trim_silence

    @trim_silence.setter
    def trim_silence(self, val: bool):
        self._trim_silence = val
