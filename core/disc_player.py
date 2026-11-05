"""Discs and disc images, played by libvlc: DVD-Video with its menus, titles and chapters, Blu-ray without copy
protection, video CDs, ISO images and the folders of a disc.

Qt's media player cannot open a DVD, and mpv dropped the menus in its last versions, while VLC's engine still has
libdvdnav behind it. libvlc is not part of AudioCrate: it is loaded when VLC is installed (the libvlc package) and
the feature says so when it is not, so the program does not grow by it. The picture comes out as frames in memory
and is painted by VideoStage, like any other video, and the sound is played by libvlc itself.

DiscPlayer has the parts of Player's surface that VideoStage uses, so that the strip over the picture, the keys and
the menu work for a disc in the same way.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage

NO_VLC = ("Playing discs needs VLC's engine. Install VLC (the libvlc package on Linux) and try again. "
          "On Windows, VLC is installed in its usual folder.")
DISC_EXTS = (".iso", ".img", ".udf")

# libvlc_state_t
_OPENING, _BUFFERING, _PLAYING, _PAUSED, _STOPPED, _ENDED, _ERROR = 1, 2, 3, 4, 5, 6, 7
# libvlc_navigate_mode_t
NAV = {"activate": 0, "up": 1, "down": 2, "left": 3, "right": 4, "menu": 5}
_TITLE_MENU = 1             # libvlc_title_menu


class _TrackDesc(ctypes.Structure):
    pass


_TrackDesc._fields_ = [("id", ctypes.c_int), ("name", ctypes.c_char_p), ("next", ctypes.POINTER(_TrackDesc))]


class _Title(ctypes.Structure):
    _fields_ = [("duration", ctypes.c_int64), ("name", ctypes.c_char_p), ("flags", ctypes.c_uint)]


class _Chapter(ctypes.Structure):
    _fields_ = [("offset", ctypes.c_int64), ("duration", ctypes.c_int64), ("name", ctypes.c_char_p)]


class _VideoTrack(ctypes.Structure):
    _fields_ = [("height", ctypes.c_uint), ("width", ctypes.c_uint), ("sar_num", ctypes.c_uint), ("sar_den", ctypes.c_uint),
                ("fps_num", ctypes.c_uint), ("fps_den", ctypes.c_uint)]


class _MediaTrack(ctypes.Structure):
    _fields_ = [("codec", ctypes.c_uint32), ("original", ctypes.c_uint32), ("id", ctypes.c_int), ("type", ctypes.c_int),
                ("profile", ctypes.c_int), ("level", ctypes.c_int), ("detail", ctypes.c_void_p),
                ("bitrate", ctypes.c_uint), ("language", ctypes.c_char_p), ("description", ctypes.c_char_p)]


_LOCK = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_UNLOCK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_DISPLAY = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_SETUP = ctypes.CFUNCTYPE(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
                          ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint))
_CLEANUP = ctypes.CFUNCTYPE(None, ctypes.c_void_p)

_lib = None


def _candidates() -> list:
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramW6432")]
        return [Path(r) / "VideoLAN" / "VLC" / "libvlc.dll" for r in roots if r]
    found = ctypes.util.find_library("vlc")
    return [found] if found else ["libvlc.so.5"]


def _load():
    for c in _candidates():
        try:
            if isinstance(c, Path):
                if not c.is_file():
                    continue
                os.environ.setdefault("VLC_PLUGIN_PATH", str(c.parent / "plugins"))
                os.add_dll_directory(str(c.parent))
            lib = ctypes.CDLL(str(c))
        except (OSError, AttributeError):
            continue
        return lib
    return None


def _protos(L):
    P, I, U, V, S = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, None, ctypes.c_char_p
    spec = {
        "libvlc_new": (P, I, ctypes.POINTER(S)), "libvlc_release": (V, P),
        "libvlc_media_new_location": (P, P, S), "libvlc_media_release": (V, P),
        "libvlc_media_tracks_get": (U, P, ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_MediaTrack)))),
        "libvlc_media_tracks_release": (V, ctypes.POINTER(ctypes.POINTER(_MediaTrack)), U),
        "libvlc_media_player_new": (P, P), "libvlc_media_player_release": (V, P), "libvlc_media_player_set_media": (V, P, P),
        "libvlc_media_player_play": (I, P), "libvlc_media_player_pause": (V, P), "libvlc_media_player_set_pause": (V, P, I),
        "libvlc_media_player_stop": (V, P), "libvlc_media_player_get_state": (I, P),
        "libvlc_media_player_get_time": (ctypes.c_int64, P), "libvlc_media_player_set_time": (V, P, ctypes.c_int64),
        "libvlc_media_player_get_length": (ctypes.c_int64, P),
        "libvlc_media_player_get_position": (ctypes.c_float, P), "libvlc_media_player_set_position": (V, P, ctypes.c_float),
        "libvlc_media_player_set_rate": (I, P, ctypes.c_float), "libvlc_media_player_get_rate": (ctypes.c_float, P),
        "libvlc_media_player_next_frame": (V, P),
        "libvlc_audio_get_volume": (I, P), "libvlc_audio_set_volume": (I, P, I), "libvlc_audio_set_mute": (V, P, I),
        "libvlc_audio_get_track_description": (ctypes.POINTER(_TrackDesc), P), "libvlc_audio_get_track": (I, P),
        "libvlc_audio_set_track": (I, P, I),
        "libvlc_video_get_spu_description": (ctypes.POINTER(_TrackDesc), P), "libvlc_video_get_spu": (I, P),
        "libvlc_video_set_spu": (I, P, I),
        "libvlc_track_description_list_release": (V, ctypes.POINTER(_TrackDesc)),
        "libvlc_media_player_get_full_title_descriptions": (I, P, ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_Title)))),
        "libvlc_title_descriptions_release": (V, ctypes.POINTER(ctypes.POINTER(_Title)), U),
        "libvlc_media_player_get_full_chapter_descriptions": (I, P, I, ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_Chapter)))),
        "libvlc_chapter_descriptions_release": (V, ctypes.POINTER(ctypes.POINTER(_Chapter)), U),
        "libvlc_media_player_get_title": (I, P), "libvlc_media_player_set_title": (V, P, I),
        "libvlc_media_player_get_chapter": (I, P), "libvlc_media_player_set_chapter": (V, P, I),
        "libvlc_media_player_next_chapter": (V, P), "libvlc_media_player_previous_chapter": (V, P),
        "libvlc_media_player_navigate": (V, P, U),
        "libvlc_video_set_callbacks": (V, P, _LOCK, _UNLOCK, _DISPLAY, P),
        "libvlc_video_set_format_callbacks": (V, P, _SETUP, _CLEANUP),
    }
    for name, (res, *args) in spec.items():
        fn = getattr(L, name)
        fn.restype, fn.argtypes = res, args


def library():
    """libvlc, loaded on first use, or None when VLC is not installed."""
    global _lib
    if _lib is None:
        lib = _load()
        try:
            if lib is not None:
                _protos(lib)
        except AttributeError:              # a libvlc from the 2.x days
            lib = None
        _lib = lib or False
    return _lib or None


def available() -> bool:
    return library() is not None


def sniff_image(path: Path) -> str:
    """Whether a disc image holds a DVD or a Blu-ray, by the folder names near its start: "dvd" or "bluray"."""
    try:
        with open(path, "rb") as f:
            head = f.read(4 << 20)
    except OSError:
        return "dvd"
    return "bluray" if b"BDMV" in head and b"VIDEO_TS" not in head else "dvd"


def mrl_for(source: str) -> tuple[str, str]:
    """(the address libvlc plays, '') for a drive (/dev/sr0), a disc image, or the folder of a disc; ('', why) when it
    is none of those."""
    p = Path(source)
    if str(source).startswith("/dev/") or (sys.platform == "win32" and len(str(source)) <= 3 and str(source)[1:2] == ":"):
        return "dvd://" + str(source), ""
    if p.is_dir():
        for sub, scheme in (("VIDEO_TS", "dvd"), ("BDMV", "bluray")):
            if (p / sub).is_dir():
                return "%s://%s" % (scheme, p.as_posix()), ""
        if p.name.upper() == "VIDEO_TS":
            return "dvd://" + p.parent.as_posix(), ""
        if p.name.upper() == "BDMV":
            return "bluray://" + p.parent.as_posix(), ""
        return "", "That folder is not the folder of a DVD or a Blu-ray disc. It has no VIDEO_TS or BDMV folder in it."
    if p.is_file():
        return "%s://%s" % (sniff_image(p), p.as_posix()), ""
    return "", "That is not there any more."


class _Current:
    """What VideoStage asks of the thing that is playing."""
    is_video = True
    is_disc = True

    def __init__(self, path: Path):
        self.path = path
        self.title = path.stem or path.name
        self.artist = self.album = ""


def _names(L, head) -> list[tuple[int, str]]:
    out, node = [], head
    while node:
        d = node.contents
        out.append((d.id, (d.name or b"").decode("utf-8", "replace")))
        node = d.next
    return out


class DiscPlayer(QObject):
    track_changed = Signal(object)
    state_changed = Signal(bool)
    position_changed = Signal(float, int, int)
    video_tracks_changed = Signal()
    subtitle_text = Signal(str)             # never: libvlc draws the subtitles in the picture
    rate_changed = Signal(float)
    volume_changed = Signal(float)
    ab_changed = Signal()
    frame = Signal(object)                  # a QImage of the picture
    ratio_changed = Signal(float)           # the shape the picture has on a screen (width / height), when the disc says
    disc_changed = Signal()                 # the titles or chapters changed
    ended = Signal()
    error = Signal(str)
    _from_vlc = Signal(object)              # a frame, from libvlc's thread to this one

    def __init__(self, parent=None, args=()):
        super().__init__(parent)
        self._args = list(args)             # more command line options for libvlc, for a test
        self._inst = self._mp = self._media = None
        self._buf = None
        self._size = (0, 0)
        self._queued = 0
        self._volume = 1.0
        self._muted = False
        self._rate = 1.0
        self._ab = [None, None]
        self._playing = False
        self._last_state = None
        self._titles: list = []
        self._title_key = (-1, -1)
        self._ratio_tries = 0
        self.ratio = 0.0
        self.current: _Current | None = None
        self._from_vlc.connect(self._deliver, Qt.QueuedConnection)
        self._timer = QTimer(self, interval=100)
        self._timer.timeout.connect(self._tick)
        # libvlc keeps calling these, so they live as long as the player
        self._cb = (_LOCK(self._lock), _UNLOCK(self._unlock), _DISPLAY(self._display), _SETUP(self._setup), _CLEANUP(lambda o: None))

    # ------------------------------------------------------------------ libvlc's side, on its own threads

    def _setup(self, opaque, chroma, width, height, pitches, lines):
        ctypes.memmove(chroma, b"RV32", 4)
        w, h = width[0], height[0]
        pitches[0], lines[0] = w * 4, h
        self._size = (w, h)
        self._buf = ctypes.create_string_buffer(w * h * 4)
        return 1

    def _lock(self, opaque, planes):
        planes[0] = ctypes.addressof(self._buf)
        return None

    def _unlock(self, opaque, picture, planes):
        pass

    def _display(self, opaque, picture):
        w, h = self._size
        if self._queued > 1 or not w:       # the window has not kept up: this picture is dropped
            return
        self._queued += 1
        self._from_vlc.emit(QImage(self._buf, w, h, w * 4, QImage.Format_RGB32).copy())

    # ------------------------------------------------------------------ the GUI thread

    def _deliver(self, img):
        self._queued = max(0, self._queued - 1)
        self.frame.emit(img)

    def open(self, source: str) -> str:
        """Start a disc, an image or a disc folder. Returns '' or a sentence saying why it did not start."""
        L = library()
        if L is None:
            return NO_VLC
        mrl, why = mrl_for(source)
        return why or self.open_mrl(mrl, Path(source))

    def open_mrl(self, mrl: str, path: Path) -> str:
        """Play any address libvlc takes (a file, a stream). `path` is the name it is shown under."""
        L = library()
        if L is None:
            return NO_VLC
        self.close()
        argv = [b"--quiet", b"--no-osd", b"--no-video-title-show", b"--no-snapshot-preview", b"--no-xlib"] + [a.encode() for a in self._args]
        arr = (ctypes.c_char_p * len(argv))(*argv)
        self._inst = L.libvlc_new(len(argv), arr)
        if not self._inst:
            return "VLC's engine could not be started."
        self._mp = L.libvlc_media_player_new(self._inst)
        lock, unlock, display, setup, cleanup = self._cb
        L.libvlc_video_set_callbacks(self._mp, lock, unlock, display, None)
        L.libvlc_video_set_format_callbacks(self._mp, setup, cleanup)
        self._media = L.libvlc_media_new_location(self._inst, mrl.encode())
        L.libvlc_media_player_set_media(self._mp, self._media)
        L.libvlc_audio_set_volume(self._mp, int(self._volume * 100))
        L.libvlc_audio_set_mute(self._mp, int(self._muted))
        self.current = _Current(path)
        self.ratio, self._ratio_tries, self._title_key, self._titles = 0.0, 0, (-1, -1), []
        self._ab = [None, None]
        self.track_changed.emit(self.current)
        if L.libvlc_media_player_play(self._mp) != 0:
            self.close()
            return "The disc could not be started."
        self._timer.start()
        return ""

    def close(self):
        L = library()
        self._timer.stop()
        if L is None or self._mp is None:
            return
        mp, media, inst = self._mp, self._media, self._inst
        self._mp = self._media = self._inst = None
        L.libvlc_media_player_stop(mp)
        L.libvlc_media_player_release(mp)
        if media:
            L.libvlc_media_release(media)
        L.libvlc_release(inst)
        self._set_playing(False)
        self.current = None

    def _state(self) -> int:
        return library().libvlc_media_player_get_state(self._mp) if self._mp else _STOPPED

    def _set_playing(self, on: bool):
        if on != self._playing:
            self._playing = on
            self.state_changed.emit(on)

    def _tick(self):
        L = library()
        st = self._state()
        self._set_playing(st in (_OPENING, _BUFFERING, _PLAYING))
        if st == _ERROR and self._last_state != _ERROR:
            self.error.emit("The disc could not be played. A DVD with copy protection needs libdvdcss.")
        if st == _ENDED and self._last_state != _ENDED:
            self.ended.emit()
        self._last_state = st
        if st not in (_PLAYING, _PAUSED, _BUFFERING):
            return
        ms, dur = max(int(L.libvlc_media_player_get_time(self._mp)), 0), max(int(L.libvlc_media_player_get_length(self._mp)), 0)
        frac = ms / dur if dur else max(float(L.libvlc_media_player_get_position(self._mp)), 0.0)
        self.position_changed.emit(min(frac, 1.0), ms, dur)
        if self._ab[1] is not None and ms >= self._ab[1]:
            self.seek_ms(self._ab[0])
        key = (L.libvlc_media_player_get_title(self._mp), L.libvlc_media_player_get_chapter(self._mp))
        if key != self._title_key:
            self._title_key = key
            self._titles = self._read_titles()
            self.disc_changed.emit()
            self.video_tracks_changed.emit()
        if not self.ratio and self._ratio_tries < 40 and st == _PLAYING:
            self._ratio_tries += 1
            self._read_ratio()

    def _read_ratio(self):
        L = library()
        tracks = ctypes.POINTER(ctypes.POINTER(_MediaTrack))()
        n = L.libvlc_media_tracks_get(self._media, ctypes.byref(tracks))
        try:
            for i in range(n):
                t = tracks[i].contents
                if t.type == 1 and t.detail:                    # libvlc_track_video
                    v = ctypes.cast(t.detail, ctypes.POINTER(_VideoTrack)).contents
                    if v.width and v.height and v.sar_num and v.sar_den:
                        self.ratio = v.width * v.sar_num / (v.height * v.sar_den)
                        self.ratio_changed.emit(self.ratio)
                        return
        finally:
            if n:
                L.libvlc_media_tracks_release(tracks, n)

    # ------------------------------------------------------------------ what Player has, as far as VideoStage asks

    @property
    def is_playing(self) -> bool:
        return self._playing

    def toggle(self):
        L = library()
        if self._mp is None:
            return
        if self._state() in (_STOPPED, _ENDED):
            L.libvlc_media_player_stop(self._mp)
            L.libvlc_media_player_play(self._mp)
        else:
            L.libvlc_media_player_pause(self._mp)

    def pause(self):
        if self._mp is not None and self._playing:
            library().libvlc_media_player_set_pause(self._mp, 1)

    @property
    def position_ms(self) -> int:
        return max(int(library().libvlc_media_player_get_time(self._mp)), 0) if self._mp else 0

    @property
    def duration_ms(self) -> int:
        return max(int(library().libvlc_media_player_get_length(self._mp)), 0) if self._mp else 0

    def seek(self, frac: float):
        if self._mp is None:
            return
        dur = self.duration_ms
        if dur:
            self.seek_ms(int(max(0.0, min(1.0, frac)) * dur))
        else:
            library().libvlc_media_player_set_position(self._mp, max(0.0, min(1.0, frac)))

    def seek_ms(self, ms: int):
        if self._mp is not None:
            library().libvlc_media_player_set_time(self._mp, max(0, int(ms)))

    def seek_delta(self, seconds: int):
        self.seek_ms(self.position_ms + seconds * 1000)

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, v: float):
        self._volume = max(0.0, min(1.0, float(v)))
        if self._mp is not None:
            library().libvlc_audio_set_volume(self._mp, int(round(self._volume * 100)))
        self.volume_changed.emit(self._volume)

    def toggle_mute(self):
        self._muted = not self._muted
        if self._mp is not None:
            library().libvlc_audio_set_mute(self._mp, int(self._muted))

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def playback_rate(self) -> float:
        return self._rate

    def set_playback_rate(self, rate: float):
        self._rate = max(0.25, min(4.0, float(rate)))
        if self._mp is not None:
            library().libvlc_media_player_set_rate(self._mp, self._rate)
        self.rate_changed.emit(self._rate)

    def frame_step(self, direction: int):
        if self._mp is None or direction < 0:           # libvlc steps forward only
            return
        if self._playing:
            self.pause()
        library().libvlc_media_player_next_frame(self._mp)

    def prev(self):
        if self._mp is not None:
            library().libvlc_media_player_previous_chapter(self._mp)

    def next(self):
        if self._mp is not None:
            library().libvlc_media_player_next_chapter(self._mp)

    @property
    def ab_loop(self) -> tuple:
        return tuple(self._ab)

    def ab_toggle(self):
        if self._ab[0] is None:
            self._ab = [self.position_ms, None]
        elif self._ab[1] is None:
            self._ab[1] = max(self.position_ms, self._ab[0] + 500)
        else:
            self._ab = [None, None]
        self.ab_changed.emit()

    def _tracks(self, which: str) -> list[tuple[int, str]]:
        if self._mp is None:
            return []
        L = library()
        head = (L.libvlc_audio_get_track_description if which == "audio" else L.libvlc_video_get_spu_description)(self._mp)
        try:
            return [(i, n) for i, n in _names(L, head) if i >= 0]       # -1 is the entry that turns it off
        finally:
            if head:
                L.libvlc_track_description_list_release(head)

    def audio_tracks(self) -> list[str]:
        names = [n for _, n in self._tracks("audio")]
        return names if len(names) > 1 else []

    @property
    def audio_track(self) -> int:
        cur = library().libvlc_audio_get_track(self._mp) if self._mp else -1
        return next((k for k, (i, _) in enumerate(self._tracks("audio")) if i == cur), 0)

    def set_audio_track(self, k: int):
        ids = self._tracks("audio")
        if self._mp is not None and 0 <= k < len(ids):
            library().libvlc_audio_set_track(self._mp, ids[k][0])

    def subtitle_tracks(self) -> list[str]:
        return [n for _, n in self._tracks("subs")]

    @property
    def subtitle_track(self) -> int:
        cur = library().libvlc_video_get_spu(self._mp) if self._mp else -1
        return next((k for k, (i, _) in enumerate(self._tracks("subs")) if i == cur), -1)

    def set_subtitle_track(self, k: int):
        ids = self._tracks("subs")
        if self._mp is not None:
            library().libvlc_video_set_spu(self._mp, ids[k][0] if 0 <= k < len(ids) else -1)

    def video_info(self) -> list[tuple[str, str]]:
        out = []
        if self.current is not None:
            out.append(("Disc", self.current.title))
        t = self.title_index
        if self._titles and 0 <= t < len(self._titles):
            out.append(("Title", self._titles[t][0]))
        w, h = self._size
        if w:
            out.append(("Picture", "%d x %d" % (w, h)))
        if self.ratio:
            out.append(("Aspect ratio", "%.2f : 1" % self.ratio))
        return out

    # ------------------------------------------------------------------ the disc

    def _read_titles(self) -> list:
        L = library()
        arr = ctypes.POINTER(ctypes.POINTER(_Title))()
        n = L.libvlc_media_player_get_full_title_descriptions(self._mp, ctypes.byref(arr))
        out = []
        if n > 0:
            for i in range(n):
                t = arr[i].contents
                out.append(((t.name or b"").decode("utf-8", "replace") or "Title %d" % i, t.duration // 1000, bool(t.flags & _TITLE_MENU)))
            L.libvlc_title_descriptions_release(arr, n)
        return out

    def titles(self) -> list[tuple[str, int, bool]]:
        """[(name, seconds, is_a_menu)] of the disc."""
        return list(self._titles)

    @property
    def title_index(self) -> int:
        return library().libvlc_media_player_get_title(self._mp) if self._mp else -1

    def set_title(self, i: int):
        if self._mp is not None:
            library().libvlc_media_player_set_title(self._mp, i)

    def chapters(self) -> list[tuple[str, int]]:
        """[(name, seconds)] of the title that plays."""
        if self._mp is None:
            return []
        L = library()
        arr = ctypes.POINTER(ctypes.POINTER(_Chapter))()
        n = L.libvlc_media_player_get_full_chapter_descriptions(self._mp, -1, ctypes.byref(arr))
        out = []
        if n > 0:
            for i in range(n):
                c = arr[i].contents
                out.append(((c.name or b"").decode("utf-8", "replace") or "Chapter %d" % (i + 1), c.offset // 1000))
            L.libvlc_chapter_descriptions_release(arr, n)
        return out

    @property
    def chapter_index(self) -> int:
        return library().libvlc_media_player_get_chapter(self._mp) if self._mp else -1

    def set_chapter(self, i: int):
        if self._mp is not None:
            library().libvlc_media_player_set_chapter(self._mp, i)

    @property
    def in_menu(self) -> bool:
        """Is a menu of the disc on the screen? Its arrows, Enter and the menu key then steer the menu."""
        t = self.title_index
        return 0 <= t < len(self._titles) and self._titles[t][2]

    def navigate(self, where: str):
        """Up, down, left, right, activate (the highlighted button) or menu (the menu of the disc)."""
        if self._mp is not None and where in NAV:
            library().libvlc_media_player_navigate(self._mp, NAV[where])
