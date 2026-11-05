"""A video with its controls.

The picture is drawn by video_view.PaintedVideo, an ordinary widget, so the strip of controls, the subtitles and the short
line that answers a key (volume, speed) are ordinary child widgets over it. The strip shows while the pointer moves and goes
away while the video plays and the pointer rests. (QVideoWidget would not do: it draws in a window of its own that covers
every widget laid over it, and the windows that could be raised above it cannot be moved between the main window and a
floating panel on Wayland.)

Fullscreen moves the stage into a window of its own and back. Everything the strip does is also in the menu of the right mouse
button, and the keys work with or without the strip: F fullscreen, [ ] = speed, V subtitles, B audio track, A aspect ratio,
G H subtitle delay, Shift+S a screenshot, . and , a frame at a time while paused. The app-wide keys (space, arrows, M and the
rest) are in MainWindow._handle_media_key, which asks handle_key first.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
                               QSlider, QStackedWidget, QToolTip, QVBoxLayout, QWidget)

import subtitles
from panel_controls import _ClickSlider, _vol_to_slider
from player import VIDEO_EXTS
from video_view import PaintedVideo

_ICONS = {
    "prev": "\U000f04ae", "play": "\U000f040a", "pause": "\U000f03e4", "next": "\U000f04ad",
    "vol": "\U000f057e", "vol_mid": "\U000f0580", "vol_low": "\U000f057f", "mute": "\U000f0581",
    "audio": "\U000f075a", "subs": "\U000f0a16", "shot": "\U000f0100",
    "more": "\U000f01d9", "full": "\U000f0293", "unfull": "\U000f0294",
}
_SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0, 4.0)
_ASPECTS = (("Fit", "fit"), ("Fill the window, cropping the edges", "fill"), ("Stretch", "stretch"), None,
            ("16:9", 16 / 9), ("4:3", 4 / 3), ("21:9", 21 / 9), ("16:10", 16 / 10), ("1:1", 1.0))
_IDLE_MS = 2500                 # the strip goes away this long after the pointer stopped, while the video plays
_OSD_MS = 1400
_SUB_STEP = 100                 # ms per press of the subtitle delay keys
_BAR_QSS = """
QFrame#video_bar { background: rgba(16, 16, 22, 225); border: none; }
QFrame#video_bar QLabel { color: #e6e6ee; background: transparent; }
QFrame#video_bar QPushButton { background: transparent; border: none; border-radius: 4px; color: #e6e6ee; padding: 0; }
QFrame#video_bar QPushButton:hover { background: rgba(255, 255, 255, 36); }
QFrame#video_bar QPushButton:pressed { background: rgba(255, 255, 255, 64); }
QFrame#video_bar QPushButton:checked { background: ACCENT; color: #ffffff; }
QFrame#video_bar QSlider::groove:horizontal { height: 4px; background: #3a3a48; border-radius: 2px; }
QFrame#video_bar QSlider::sub-page:horizontal { background: ACCENT; border-radius: 2px; }
QFrame#video_bar QSlider::handle:horizontal { width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; background: #ffffff; }
"""


def _clock(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _speed_text(rate: float) -> str:
    return f"{rate:.2f}".rstrip("0").rstrip(".") + "×"


class _SeekSlider(_ClickSlider):
    """The seek line, which names the time under the pointer."""

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setRange(0, 1000)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.duration = lambda: 0

    def mouseMoveEvent(self, e):
        ratio = max(0.0, min(1.0, e.position().x() / max(1, self.width())))
        QToolTip.showText(e.globalPosition().toPoint(), _clock(int(ratio * self.duration())), self)
        super().mouseMoveEvent(e)


class _Caption(QWidget):
    """Text on a dark rounded box the size of the text. The pointer goes through it to the picture."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._text = ""
        self._made = None
        self.hide()

    def set_text(self, text: str, px: int, max_w: int):
        if (text, px, max_w) == self._made:
            return
        self._made = (text, px, max_w)
        font = self.font()
        font.setPixelSize(px)
        font.setBold(True)
        self.setFont(font)
        box = QFontMetrics(font).boundingRect(QRect(0, 0, max_w, 10000), Qt.AlignCenter | Qt.TextWordWrap, text)
        pad = max(6, px // 3)
        self._text = text
        self.resize(box.width() + 2 * pad, box.height() + 2 * pad)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 175))
        p.drawRoundedRect(self.rect(), 6, 6)
        p.setPen(Qt.white)
        p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self._text)


def _is_video(track) -> bool:
    """Is this track (of the player, or the disc the disc player has) a video?"""
    return track is not None and (getattr(track, "is_video", False) or track.path.suffix.lower() in VIDEO_EXTS)


class _Host(QWidget):
    """The window of the fullscreen stage. Closing it (Alt+F4) leaves fullscreen."""

    def __init__(self, parent, on_close):
        super().__init__(parent, Qt.Window)
        self._on_close = on_close
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, Qt.black)
        self.setPalette(pal)
        self.setWindowTitle("AudioCrate video")
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)

    def closeEvent(self, e):
        e.ignore()
        self._on_close()


class VideoStage(QWidget):
    fullscreen_changed = Signal(bool)
    volume_nudged = Signal(int)         # a wheel turn over the picture, in steps of the volume slider
    volume_set = Signal(int)            # the strip's volume slider, 0 to 100
    mute_toggled = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self._player = player
        self._aspect = "fit"
        self._cues = None               # the lines of a subtitle file
        self._sub_mode = None           # None, "embedded" or "file"
        self._sub_path: Path | None = None
        self._sub_files: list[Path] = []
        self._sub_delay = 0
        self._sub_text = ""
        self._fullscreen = False
        self._host = None               # the window of the fullscreen stage, made the first time
        self._home = None               # the stack the stage sits in while it is fullscreen, and its place there
        self._home_index = 0
        self._menus = 0                 # menus open from here: the strip stays while there is one
        self._dbl = False
        self.extra_menu = None          # a function that adds entries to the menu (the disc player's titles and chapters)
        self._dur = 0
        self._seeking = False
        self.setMinimumSize(160, 90)
        self.setMouseTracking(True)

        self._video = PaintedVideo(self)
        self._sub = _Caption(self)
        self._osd_box = _Caption(self)
        self._build_bar()
        self._video.installEventFilter(self)
        self._bar.installEventFilter(self)

        self._idle = QTimer(self, singleShot=True, interval=_IDLE_MS)
        self._idle.timeout.connect(self._go_quiet)
        self._osd_timer = QTimer(self, singleShot=True, interval=_OSD_MS)
        self._osd_timer.timeout.connect(self._osd_box.hide)
        self._click = QTimer(self, singleShot=True, interval=QApplication.doubleClickInterval())
        self._click.timeout.connect(lambda: self._player.toggle())

        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.position_changed.connect(self._on_position)
        player.video_tracks_changed.connect(self._fit_bar)
        player.subtitle_text.connect(self._on_embedded_text)
        player.rate_changed.connect(self._on_rate)
        player.volume_changed.connect(self._on_volume)
        player.ab_changed.connect(self._on_ab)
        self._on_volume(player.volume, quiet=True)
        self._on_state(player.is_playing)
        self._on_rate(player.playback_rate)
        self._on_ab()

    @property
    def video_widget(self) -> QWidget:
        """What the media player draws in."""
        return self._video

    @property
    def active(self) -> bool:
        return _is_video(self._player.current)

    # ------------------------------------------------------------------ the strip

    def _button(self, key: str, tip: str, slot=None, checkable=False) -> QPushButton:
        b = QPushButton(_ICONS.get(key, key))
        b.setToolTip(tip)
        b.setFixedSize(34, 30)
        b.setFocusPolicy(Qt.NoFocus)
        b.setCheckable(checkable)
        f = b.font()
        f.setPointSize(13)
        b.setFont(f)
        if slot is not None:
            b.clicked.connect(slot)
        return b

    def _build_bar(self):
        bar = self._bar = QFrame(self)
        bar.setObjectName("video_bar")
        bar.setMouseTracking(True)
        bar.setStyleSheet(_BAR_QSS.replace("ACCENT", self.palette().highlight().color().name()))
        box = QVBoxLayout(bar)
        box.setContentsMargins(10, 6, 10, 6)
        box.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._t_cur = QLabel("0:00")
        self._t_dur = QLabel("0:00")
        self._seek = _SeekSlider()
        self._seek.duration = lambda: self._dur
        self._seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self._seek.sliderReleased.connect(self._seek_release)
        self._seek.valueChanged.connect(lambda v: self._seeking and self._t_cur.setText(_clock(v / 1000 * self._dur)))
        top.addWidget(self._t_cur)
        top.addWidget(self._seek, 1)
        top.addWidget(self._t_dur)
        box.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._b_prev = self._button("prev", "Previous", self._player.prev)
        self._b_play = self._button("play", "Play or pause (space)", self._player.toggle)
        self._b_next = self._button("next", "Next", self._player.next)
        self._b_mute = self._button("vol", "Mute (M). The wheel over the picture changes the volume", lambda: self.mute_toggled.emit())
        self._vol = QSlider(Qt.Horizontal)
        self._vol.setRange(0, 100)
        self._vol.setFixedWidth(84)
        self._vol.setFocusPolicy(Qt.NoFocus)
        self._vol.valueChanged.connect(self.volume_set)
        self._b_ab = self._button("A-B", "Repeat a part: click at the start, again at the end, a third time to clear",
                                  self._player.ab_toggle, checkable=True)
        self._b_ab.setFixedWidth(44)
        self._b_speed = self._button("1×", "Playback speed ([ and ] change it, = sets it back)",
                                     lambda: self._popup(self._speed_menu(), self._b_speed))
        self._b_speed.setFixedWidth(50)
        self._b_audio = self._button("audio", "Audio track (B)", lambda: self._popup(self._audio_menu(), self._b_audio))
        self._b_subs = self._button("subs", "Subtitles (V)", lambda: self._popup(self._subs_menu(), self._b_subs))
        self._b_aspect = self._button("Fit", "Aspect ratio (A)", lambda: self._popup(self._aspect_menu(), self._b_aspect))
        self._b_aspect.setFixedWidth(52)
        self._b_shot = self._button("shot", "Save a picture of this frame (Shift+S)", self.screenshot)
        self._b_more = self._button("more", "More", lambda: self._popup(self._main_menu(), self._b_more))
        self._b_full = self._button("full", "Fullscreen (F or a double click)", lambda: self.set_fullscreen(not self._fullscreen))
        for w in (self._b_prev, self._b_play, self._b_next, self._b_mute, self._vol):
            row.addWidget(w)
        row.addStretch(1)
        for w in (self._b_ab, self._b_speed, self._b_audio, self._b_subs, self._b_aspect, self._b_shot, self._b_more, self._b_full):
            row.addWidget(w)
        box.addLayout(row)
        self._row = row
        # what goes first when the strip is narrow (it is all in the menu), first to go first
        self._optional = (self._b_shot, self._b_aspect, self._b_ab, self._b_speed, self._b_subs, self._b_audio, self._vol)
        self._always = (self._b_prev, self._b_play, self._b_next, self._b_mute, self._b_more, self._b_full)
        bar.hide()

    def add_button(self, text: str, tip: str, slot, width: int = 0) -> QPushButton:
        """A button of the owner of the stage (the disc player's menu, titles and chapters), left of the speed button."""
        b = self._button(text, tip, slot)
        b.setFixedWidth(width or max(34, b.fontMetrics().horizontalAdvance(text) + 18))
        self._row.insertWidget(self._row.indexOf(self._b_ab), b)
        self._always = self._always + (b,)
        self._fit_bar()
        return b

    def notice(self, text: str):
        """A line over the top left corner of the picture for a moment."""
        self._osd(text)

    def _fit_bar(self):
        """Hide the buttons that do not fit, the less used first, and the audio track button when there is only one."""
        for w in self._optional:
            w.setVisible(w is not self._b_audio or len(self._player.audio_tracks()) > 1)
        room = self.width() - 20

        def need():
            vis = [w for w in self._always + self._optional if w.isVisibleTo(self._bar)]
            return sum(w.minimumWidth() or w.sizeHint().width() for w in vis) + 4 * len(vis) + 24
        for w in self._optional:
            if need() <= room:
                break
            w.hide()

    def _seek_release(self):
        self._seeking = False
        self._player.seek(self._seek.value() / 1000.0)

    # ------------------------------------------------------------------ layout

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()

    def showEvent(self, e):
        super().showEvent(e)
        self.poke()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.setCursor(Qt.ArrowCursor)

    def _place(self):
        w, h = self.width(), self.height()
        self._video.setGeometry(0, 0, w, h)
        bh = self._bar.sizeHint().height()
        self._bar.setGeometry(0, h - bh, w, bh)
        self._fit_bar()
        px = max(15, h // 22)
        self._sub.set_text(self._sub_text or " ", px, int(w * 0.86))
        low = (bh + 8) if self._bar.isVisible() else 0
        self._sub.move((w - self._sub.width()) // 2, max(0, h - self._sub.height() - 14 - low))
        self._sub.setVisible(bool(self._sub_text))
        self._osd_box.move(14, 14)
        for ov in (self._sub, self._osd_box, self._bar):
            ov.raise_()

    def _osd(self, text: str):
        self._osd_box.set_text(text, max(15, self.height() // 24), int(self.width() * 0.7))
        self._osd_box.move(14, 14)
        self._osd_box.show()
        self._osd_box.raise_()
        self._osd_timer.start()

    # ------------------------------------------------------------------ the strip comes and goes

    def poke(self):
        """The pointer or a key moved: the strip shows, and goes again when things are quiet."""
        if not self.active:
            return
        if not self._bar.isVisible():
            self._bar.show()
            self._place()
        self.setCursor(Qt.ArrowCursor)
        self._idle.start()

    def _go_quiet(self):
        if not self._player.is_playing:
            return
        if self._menus or self._bar.underMouse() or self._seeking:
            self._idle.start()
            return
        self._bar.hide()
        self._place()
        if self._fullscreen:
            self.setCursor(Qt.BlankCursor)

    def _on_state(self, playing: bool):
        self._b_play.setText(_ICONS["pause" if playing else "play"])
        self.poke()

    # ------------------------------------------------------------------ the player's side

    def _on_track(self, track):
        if not _is_video(track):
            self.set_fullscreen(False)
            self._bar.hide()
            self._set_sub_text("")
            return
        self._aspect = "fit"
        self._video.set_aspect("fit")
        self._b_aspect.setText("Fit")
        self._cues = None
        self._sub_mode = None
        self._sub_delay = 0
        self._set_sub_text("")
        self._sub_files = subtitles.sidecars(track.path)
        if self._sub_files:
            self._set_subs("file", self._sub_files[0], quiet=True)
        self._fit_bar()
        self.poke()

    def _on_position(self, frac: float, ms: int, dur: int):
        self._dur = dur
        if not self._seeking:
            self._seek.blockSignals(True)
            self._seek.setValue(int(frac * 1000))
            self._seek.blockSignals(False)
            self._t_cur.setText(_clock(ms))
        self._t_dur.setText(_clock(dur))
        if self._sub_mode == "file" and self._cues is not None:
            self._set_sub_text(self._cues.at(ms - self._sub_delay))

    def _on_rate(self, rate: float):
        self._b_speed.setText(_speed_text(rate))
        if self.isVisible() and self.active:
            self._osd(f"Speed {_speed_text(rate)}")

    def _on_volume(self, vol: float, quiet=False):
        v = _vol_to_slider(vol)
        self._vol.blockSignals(True)
        self._vol.setValue(v)
        self._vol.blockSignals(False)
        self._b_mute.setText(_ICONS["mute" if v == 0 else "vol_low" if v < 34 else "vol_mid" if v < 67 else "vol"])
        if not quiet and self.isVisible() and self.active:
            self._osd("Muted" if v == 0 else f"Volume {v}%")

    def _on_ab(self):
        a, b = self._player.ab_loop
        self._b_ab.setChecked(b is not None)
        self._b_ab.setText("A-" if b is None and a is not None else "A-B")
        if self.isVisible() and self.active:
            self._osd("A-B repeat off" if a is None else f"A at {_clock(a)}" if b is None else f"Repeating {_clock(a)} to {_clock(b)}")

    # ------------------------------------------------------------------ subtitles

    def _set_sub_text(self, text: str):
        if text != self._sub_text:
            self._sub_text = text
            self._place()

    def _on_embedded_text(self, text: str):
        if self._sub_mode == "embedded":
            self._set_sub_text(text.strip())

    def _set_subs(self, mode, arg=None, quiet=False):
        """mode None (off), "embedded" (arg: the track) or "file" (arg: the path)."""
        self._set_sub_text("")
        name = "off"
        if mode == "file":
            try:
                self._cues = subtitles.load(arg)
            except OSError:
                self._osd("The subtitle file could not be read")
                return
            self._sub_path, name = arg, arg.name
            self._player.set_subtitle_track(-1)
        else:
            self._cues = None
            self._sub_path = None
            self._player.set_subtitle_track(arg if mode == "embedded" else -1)
            if mode == "embedded":
                name = self._player.subtitle_tracks()[arg]
        self._sub_mode = mode
        if mode == "file":
            self._on_position(0.0, self._player.position_ms, self._dur)
        if not quiet:
            self._osd(f"Subtitles: {name}")

    def _sub_choices(self):
        return ([(None, None)] + [("embedded", i) for i in range(len(self._player.subtitle_tracks()))]
                + [("file", p) for p in self._sub_files])

    def _current_sub(self):
        return (self._sub_mode, self._player.subtitle_track if self._sub_mode == "embedded" else self._sub_path)

    def cycle_subs(self):
        choices = self._sub_choices()
        cur = self._current_sub()
        i = choices.index(cur) if cur in choices else 0
        self._set_subs(*choices[(i + 1) % len(choices)])

    def _load_sub_file(self):
        cur = self._player.current
        start = str(cur.path.parent) if cur is not None and cur.path.parent.is_dir() else str(Path.home())
        exts = " ".join(f"*{e}" for e in subtitles.EXTS)
        self._menus += 1
        try:
            name, _ = QFileDialog.getOpenFileName(self, "Load subtitles", start, f"Subtitles ({exts});;All files (*)")
        finally:
            self._menus -= 1
        if name:
            p = Path(name)
            if p not in self._sub_files:
                self._sub_files.append(p)
            self._set_subs("file", p)

    def nudge_sub_delay(self, step: int):
        if self._sub_mode != "file":
            self._osd("The delay applies to subtitle files")
            return
        self._sub_delay += step
        self._on_position(0.0, self._player.position_ms, self._dur)
        self._osd(f"Subtitle delay {self._sub_delay / 1000:+.1f} s")

    # ------------------------------------------------------------------ menus

    def _popup(self, menu: QMenu, button: QWidget):
        self._menus += 1
        try:
            menu.exec(button.mapToGlobal(QPoint(0, -menu.sizeHint().height())))
        finally:
            self._menus -= 1
            self._idle.start()

    def _radio(self, menu: QMenu, text: str, checked: bool, slot) -> None:
        a = menu.addAction(text)
        a.setCheckable(True)
        a.setChecked(checked)
        a.triggered.connect(lambda _=False: slot())

    def _speed_menu(self, parent=None) -> QMenu:
        m = QMenu("Speed", parent or self)
        rate = self._player.playback_rate
        for s in sorted(set(_SPEEDS) | {round(rate, 2)}):
            self._radio(m, _speed_text(s), abs(rate - s) < 0.005, lambda s=s: self._player.set_playback_rate(s))
        m.addSeparator()
        m.addAction("Slower\t[", lambda: self.step_speed(-1))
        m.addAction("Faster\t]", lambda: self.step_speed(1))
        m.addAction("Normal speed\t=", lambda: self._player.set_playback_rate(1.0))
        return m

    def step_speed(self, direction: int):
        rate = self._player.playback_rate
        nxt = [s for s in _SPEEDS if s > rate + 0.005] if direction > 0 else [s for s in reversed(_SPEEDS) if s < rate - 0.005]
        if nxt:
            self._player.set_playback_rate(nxt[0])

    def _audio_menu(self, parent=None) -> QMenu:
        m = QMenu("Audio track", parent or self)
        names = self._player.audio_tracks()
        if not names:
            m.addAction("This video has one audio track").setEnabled(False)
        for i, n in enumerate(names):
            self._radio(m, n, i == self._player.audio_track, lambda i=i: self._set_audio(i))
        if names:
            m.addSeparator()
            m.addAction("Next track\tB", self.cycle_audio)
        return m

    def _set_audio(self, i: int):
        self._player.set_audio_track(i)
        names = self._player.audio_tracks()
        if i < len(names):
            self._osd(f"Audio: {names[i]}")

    def cycle_audio(self):
        names = self._player.audio_tracks()
        if len(names) > 1:
            self._set_audio((self._player.audio_track + 1) % len(names))

    def _subs_menu(self, parent=None) -> QMenu:
        m = QMenu("Subtitles", parent or self)
        cur = self._current_sub()
        self._radio(m, "Off", self._sub_mode is None, lambda: self._set_subs(None))
        for i, n in enumerate(self._player.subtitle_tracks()):
            self._radio(m, n, cur == ("embedded", i), lambda i=i: self._set_subs("embedded", i))
        for p in self._sub_files:
            self._radio(m, p.name, cur == ("file", p), lambda p=p: self._set_subs("file", p))
        m.addSeparator()
        m.addAction("Load a subtitle file...", self._load_sub_file)
        m.addAction("Next subtitles\tV", self.cycle_subs)
        later = m.addAction("Show them later\tH", lambda: self.nudge_sub_delay(_SUB_STEP))
        sooner = m.addAction("Show them sooner\tG", lambda: self.nudge_sub_delay(-_SUB_STEP))
        later.setEnabled(self._sub_mode == "file")
        sooner.setEnabled(self._sub_mode == "file")
        return m

    def _aspect_menu(self, parent=None) -> QMenu:
        m = QMenu("Aspect ratio", parent or self)
        for item in _ASPECTS:
            if item is None:
                m.addSeparator()
            else:
                self._radio(m, item[0], self._aspect == item[1], lambda k=item[1]: self.set_aspect(k))
        m.addAction("Next ratio\tA", self.cycle_aspect)
        return m

    def set_aspect(self, key):
        self._aspect = key
        self._video.set_aspect(key)
        label = next(t for t, k in (i for i in _ASPECTS if i) if k == key).split(",")[0]
        self._b_aspect.setText({"fit": "Fit", "fill": "Fill", "stretch": "Stretch"}.get(key, label))
        self._osd(f"Aspect ratio: {label}")

    def cycle_aspect(self):
        keys = [i[1] for i in _ASPECTS if i]
        self.set_aspect(keys[(keys.index(self._aspect) + 1) % len(keys)])

    def _main_menu(self) -> QMenu:
        m = QMenu(self)
        m.addAction("Pause\tSpace" if self._player.is_playing else "Play\tSpace", self._player.toggle)
        m.addSeparator()
        m.addMenu(self._speed_menu(m))
        m.addMenu(self._audio_menu(m))
        m.addMenu(self._subs_menu(m))
        m.addMenu(self._aspect_menu(m))
        m.addSeparator()
        a, _b = self._player.ab_loop
        m.addAction("Clear the A-B repeat" if a is not None else "Start an A-B repeat here", self._player.ab_toggle)
        m.addAction("Next frame\t.", lambda: self._player.frame_step(1))
        m.addAction("Previous frame\t,", lambda: self._player.frame_step(-1))
        m.addAction("Save a picture of this frame\tShift+S", self.screenshot)
        m.addAction("Media information...", self.show_info)
        if self.extra_menu is not None:
            m.addSeparator()
            self.extra_menu(m)
        m.addSeparator()
        m.addAction("Leave fullscreen\tF" if self._fullscreen else "Fullscreen\tF", lambda: self.set_fullscreen(not self._fullscreen))
        return m

    def _context_menu(self, pos: QPoint):
        self._menus += 1
        try:
            self._main_menu().exec(pos)
        finally:
            self._menus -= 1
            self._idle.start()

    def show_info(self):
        rows = self._player.video_info()
        if not rows:
            self._osd("No information from the media player")
            return
        tracks = self._player.audio_tracks()
        if tracks:
            rows.append(("Audio tracks", ", ".join(tracks)))
        subs = self._player.subtitle_tracks()
        if subs:
            rows.append(("Subtitle tracks", ", ".join(subs)))
        cur = self._player.current
        if cur is not None:
            rows.append(("File", str(cur.path)))
        self._menus += 1
        try:
            QMessageBox.information(self, "Media information", "\n".join(f"{k}: {v}" for k, v in rows))
        finally:
            self._menus -= 1

    # ------------------------------------------------------------------ screenshot

    def screenshot(self):
        img = self._video.current_image()
        if img is None:
            self._osd("There is no picture to save")
            return
        pictures = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation) or str(Path.home())
        folder = Path(pictures) / "AudioCrate"
        cur = self._player.current
        stem = cur.path.stem if cur is not None else "video"
        base = f"{stem} {_clock(self._player.position_ms).replace(':', '-')}"
        path, n = folder / f"{base}.png", 1
        try:
            folder.mkdir(parents=True, exist_ok=True)
            while path.exists():
                n += 1
                path = folder / f"{base} ({n}).png"
            ok = img.save(str(path), "PNG")
        except OSError:
            ok = False
        try:
            where = "~/" + folder.relative_to(Path.home()).as_posix()
        except ValueError:
            where = str(folder)
        self._osd(f"Saved {path.name} in {where}" if ok else "The picture could not be saved")

    # ------------------------------------------------------------------ fullscreen

    @property
    def fullscreen(self) -> bool:
        return self._fullscreen

    def set_fullscreen(self, on: bool):
        if on == self._fullscreen:
            return
        if on:
            home = self.parentWidget()
            if not isinstance(home, QStackedWidget) or not self.isVisible():
                return
            if self._host is None:
                self._host = _Host(self.window(), lambda: self.set_fullscreen(False))
            self._home, self._home_index = home, home.indexOf(self)
            home.removeWidget(self)
            self._host.layout().addWidget(self)
            self._fullscreen = True
            self._host.showFullScreen()
            self.show()
        else:
            self._fullscreen = False
            self._host.layout().removeWidget(self)
            self._host.hide()
            self._home.insertWidget(self._home_index, self)
            self._home.setCurrentWidget(self)
            self.setCursor(Qt.ArrowCursor)
            self.show()
        self._b_full.setText(_ICONS["unfull" if self._fullscreen else "full"])
        self.fullscreen_changed.emit(self._fullscreen)
        self.poke()

    # ------------------------------------------------------------------ pointer and keys

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj is self._bar:
            if t == QEvent.MouseMove:
                self._idle.start()
            return False
        if t == QEvent.MouseMove:
            self.poke()
        elif t == QEvent.MouseButtonPress and ev.button() == Qt.RightButton:
            self._context_menu(ev.globalPosition().toPoint())
            return True
        elif t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton:
            if self._dbl:
                self._dbl = False
            elif self.active:
                self._click.start()                 # a click pauses, unless it turns out to be the first of two
        elif t == QEvent.MouseButtonDblClick and ev.button() == Qt.LeftButton:
            self._click.stop()
            self._dbl = True
            self.set_fullscreen(not self._fullscreen)
            return True
        elif t == QEvent.Wheel and self.active:
            y = ev.angleDelta().y()
            if y:
                self.volume_nudged.emit(5 if y > 0 else -5)
            return True
        return False

    def handle_key(self, ev) -> bool:
        """The keys of the video. MainWindow calls this first, for every key press in the app."""
        if not self.active or not (self.isVisible() or self._fullscreen):
            return False
        K = Qt.Key
        k, mods = ev.key(), ev.modifiers() & ~Qt.KeypadModifier
        plain = mods == Qt.NoModifier
        if k == K.Key_Escape and self._fullscreen:
            self.set_fullscreen(False)
        elif k == K.Key_F11 or (plain and k == K.Key_F):
            self.set_fullscreen(not self._fullscreen)
        elif plain and k == K.Key_BracketLeft:
            self.step_speed(-1)
        elif plain and k == K.Key_BracketRight:
            self.step_speed(1)
        elif plain and k == K.Key_Equal:
            self._player.set_playback_rate(1.0)
        elif plain and k == K.Key_V:
            self.cycle_subs()
        elif plain and k == K.Key_B:
            self.cycle_audio()
        elif plain and k == K.Key_A:
            self.cycle_aspect()
        elif plain and k == K.Key_G:
            self.nudge_sub_delay(-_SUB_STEP)
        elif plain and k == K.Key_H:
            self.nudge_sub_delay(_SUB_STEP)
        elif mods == Qt.ShiftModifier and k == K.Key_S:
            self.screenshot()
        elif plain and k in (K.Key_Period, K.Key_Comma) and not self._player.is_playing:
            self._player.frame_step(1 if k == K.Key_Period else -1)
        else:
            return False
        self.poke()
        return True
