from __future__ import annotations
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QSlider, QStackedWidget, QSizePolicy, QComboBox,
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QFontMetrics
from player import Player, PlayMode, _SID_EXTS

# Nerd Font MD codepoints — verified directly from font cmap
_IC_PREV    = "\U000f04ae"   # nf-md-skip_previous
_IC_PLAY    = "\U000f040a"   # nf-md-play
_IC_PAUSE   = "\U000f03e4"   # nf-md-pause
_IC_STOP    = "\U000f04db"   # nf-md-stop
_IC_NEXT    = "\U000f04ad"   # nf-md-skip_next
_IC_SHUFFLE = "\U000f049d"   # nf-md-shuffle
_IC_WAVE    = "\U000f147d"   # nf-md-waveform
_IC_VOL     = "\U000f057e"   # nf-md-volume_high
_IC_TRIM    = "\U000f0a6b"   # nf-md-scissors_cutting
_IC_WAND    = "\U000f0068"   # nf-md-auto_fix (Enhance DSP magic wand)

_MODE_CYCLE  = [PlayMode.SEQUENTIAL, PlayMode.REPEAT_ALL, PlayMode.REPEAT_ONE]
_MODE_LABELS = {
    PlayMode.SEQUENTIAL: "\U000f04ad",   # nf-md-skip_next (play through)
    PlayMode.REPEAT_ALL: "\U000f0456",   # nf-md-repeat
    PlayMode.REPEAT_ONE: "\U000f0458",   # nf-md-repeat_once
}
from waveform_bar import WaveformBar


class _ElidingLabel(QLabel):
    """A single-line label that truncates with '…' instead of forcing its
    container (and the whole window) wider when the text is long. The full
    text is preserved for the tooltip and re-elided on resize."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        # Ignored width: take whatever the layout offers, never demand more.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        if text:
            self.setText(text)

    def setText(self, text: str):
        self._full = text or ""
        self.setToolTip(self._full)
        self._relayout()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.ElideRight, max(0, self.width())))

    def minimumSizeHint(self) -> QSize:
        # Height only — width must not pin the window open.
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        return QSize(0, super().sizeHint().height())


_VOL_EXP = 3.0   # volume taper exponent — gives the slider much more travel at low volumes


def _slider_to_vol(v: int) -> float:
    return (max(0, min(100, v)) / 100.0) ** _VOL_EXP


def _vol_to_slider(vol: float) -> int:
    return int(round((max(0.0, min(1.0, vol)) ** (1.0 / _VOL_EXP)) * 100))


class _ClickSlider(QSlider):
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self.orientation() == Qt.Horizontal:
                ratio = e.position().x() / max(1, self.width())
            else:
                ratio = 1.0 - e.position().y() / max(1, self.height())
            val = int(self.minimum() + ratio * (self.maximum() - self.minimum()))
            self.setValue(max(self.minimum(), min(self.maximum(), val)))
            self.sliderReleased.emit()
        super().mousePressEvent(e)

def _fmt(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


class ControlsWidget(QWidget):
    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player = player
        self._cur_path = None
        self._build()

        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.position_changed.connect(self._on_position)
        # Waveform: .fur output is fed live by MainWindow; ffmpeg can't decode it.
        player.track_changed.connect(self._on_track_waveform)
        player.subsong_changed.connect(self._on_subsong_changed)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(6)

        self._title = _ElidingLabel("No track loaded")
        self._title.setObjectName("track_title")
        self._sub   = _ElidingLabel("")
        self._sub.setObjectName("track_sub")
        root.addWidget(self._title)
        root.addWidget(self._sub)

        # Subsong selector (multi-tune chiptunes: NSF/SAP/GBS/KSS/AY…). Hidden
        # unless the loaded file actually has more than one subsong.
        self._subsong_row = QWidget()
        ss = QHBoxLayout(self._subsong_row)
        ss.setContentsMargins(0, 0, 0, 0)
        ss.setSpacing(6)
        _ss_font = QFont()
        _ss_font.setPointSize(12)
        self._ss_prev = QPushButton("\U000f0141")   # nf-md-chevron_left
        self._ss_next = QPushButton("\U000f0142")   # nf-md-chevron_right
        for b in (self._ss_prev, self._ss_next):
            b.setFixedSize(26, 22)
            b.setFont(_ss_font)
        self._ss_prev.setToolTip("Previous subsong")
        self._ss_next.setToolTip("Next subsong")
        self._ss_label = QLabel("")
        self._ss_label.setObjectName("track_sub")
        # Direct picker as well as prev/next — stepping one at a time through a
        # 20-subsong SID is painful.
        self._ss_pick = QComboBox()
        self._ss_pick.setFixedHeight(22)
        self._ss_pick.setToolTip("Jump to a subsong")
        self._ss_pick.activated.connect(self._on_subsong_pick)
        self._ss_prev.clicked.connect(self._on_subsong_prev)
        self._ss_next.clicked.connect(self._on_subsong_next)
        ss.addWidget(self._ss_prev)
        ss.addWidget(self._ss_next)
        ss.addWidget(self._ss_pick)
        ss.addWidget(self._ss_label)
        ss.addStretch(1)
        self._subsong_row.setVisible(False)
        root.addWidget(self._subsong_row)

        seek_row = QHBoxLayout()
        self._time_cur = QLabel("0:00")
        self._time_cur.setObjectName("time_label")
        self._time_dur = QLabel("0:00")
        self._time_dur.setObjectName("time_label")

        self._seek = _ClickSlider(Qt.Horizontal)
        self._seek.setRange(0, 1000)
        self._seek.sliderPressed.connect(lambda: setattr(self, '_seeking', True))
        self._seek.sliderReleased.connect(self._on_seek_release)

        self._wf = WaveformBar()
        self._wf.seeked.connect(self._player.seek)

        self._seek_stack = QStackedWidget()
        self._seek_stack.addWidget(self._seek)  # 0 - plain slider
        self._seek_stack.addWidget(self._wf)    # 1 - waveform
        self._seek_stack.setMaximumHeight(48)

        _icon_font = QFont()
        _icon_font.setPointSize(12)

        self._wf_btn = QPushButton(_IC_WAVE)
        self._wf_btn.setCheckable(True)
        self._wf_btn.setFixedSize(34, 30)
        self._wf_btn.setFont(_icon_font)
        self._wf_btn.setToolTip("Toggle waveform seekbar  [WF]")
        self._wf_btn.toggled.connect(
            lambda on: self._seek_stack.setCurrentIndex(1 if on else 0)
        )

        seek_row.addWidget(self._time_cur)
        seek_row.addWidget(self._seek_stack, 1)
        seek_row.addWidget(self._time_dur)
        seek_row.addWidget(self._wf_btn)
        root.addLayout(seek_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        self._btn_prev = QPushButton(_IC_PREV)
        self._btn_prev.setToolTip("Previous track  [←]")
        self._btn_play = QPushButton(_IC_PLAY)
        self._btn_play.setToolTip("Play / Pause  [Space]")
        self._btn_stop = QPushButton(_IC_STOP)
        self._btn_stop.setToolTip("Stop  [S]")
        self._btn_next = QPushButton(_IC_NEXT)
        self._btn_next.setToolTip("Next track  [→]")
        self._btn_mode = QPushButton(_MODE_LABELS[self._player.mode])
        self._btn_mode.setToolTip("Cycle play mode: Sequential → Repeat All → Repeat One")
        self._btn_shf  = QPushButton(_IC_SHUFFLE)
        self._btn_shf.setCheckable(True)
        self._btn_shf.setToolTip("Shuffle")

        for b in (self._btn_prev, self._btn_play, self._btn_stop,
                  self._btn_next, self._btn_mode, self._btn_shf):
            b.setFixedHeight(30)
            b.setFont(_icon_font)

        self._btn_prev.setFixedWidth(40)
        self._btn_play.setFixedWidth(50)
        self._btn_stop.setFixedWidth(40)
        self._btn_next.setFixedWidth(40)
        self._btn_mode.setFixedWidth(40)
        self._btn_shf.setFixedWidth(40)

        self._btn_prev.clicked.connect(self._player.prev)
        self._btn_play.clicked.connect(self._player.toggle)
        self._btn_stop.clicked.connect(self._player.stop)
        self._btn_next.clicked.connect(self._player.next)
        self._btn_mode.clicked.connect(self._cycle_mode)
        self._btn_shf.toggled.connect(self._on_shuffle_toggle)

        vol_lbl = QLabel(_IC_VOL)
        vol_lbl.setFont(_icon_font)
        self._vol = QSlider(Qt.Horizontal)
        self._vol.setRange(0, 100)
        self._vol.setValue(_vol_to_slider(self._player.volume))
        self._vol.setFixedWidth(100)
        self._vol_pct = QLabel(f"{_vol_to_slider(self._player.volume)}%")
        self._vol_pct.setObjectName("time_label")
        self._vol_pct.setFixedWidth(36)
        self._vol.valueChanged.connect(self._on_volume_changed)

        self._trim = QPushButton(_IC_TRIM)
        self._trim.setCheckable(True)
        self._trim.setFixedSize(40, 30)
        self._trim.setFont(_icon_font)
        self._trim.setToolTip("Trim leading silence (requires ffmpeg)")
        self._trim.toggled.connect(
            lambda on: setattr(self._player, "trim_silence", on)
        )

        # Enhance DSP — the "magic wand": airs + brightens the sound.
        self._wand = QPushButton(_IC_WAND)
        self._wand.setCheckable(True)
        self._wand.setFixedSize(40, 30)
        self._wand.setFont(_icon_font)
        self._wand.toggled.connect(self._player.set_enhance)

        btn_row.addWidget(self._btn_prev)
        btn_row.addWidget(self._btn_play)
        btn_row.addWidget(self._btn_stop)
        btn_row.addWidget(self._btn_next)
        btn_row.addSpacing(12)
        btn_row.addWidget(self._btn_mode)
        btn_row.addWidget(self._btn_shf)
        btn_row.addSpacing(8)
        btn_row.addWidget(self._trim)
        btn_row.addWidget(self._wand)
        btn_row.addStretch()
        btn_row.addWidget(vol_lbl)
        btn_row.addWidget(self._vol)
        btn_row.addWidget(self._vol_pct)
        root.addLayout(btn_row)

        self._seeking = False

    @property
    def waveform_on(self) -> bool:
        return self._wf_btn.isChecked()

    def set_waveform_on(self, on: bool):
        self._wf_btn.setChecked(on)

    # Enhance DSP wand state (persisted across sessions)
    @property
    def enhance_on(self) -> bool:
        return self._wand.isChecked()

    def set_enhance_on(self, on: bool):
        if self._wand.isEnabled():
            self._wand.setChecked(on)

    def _on_track_waveform(self, track):
        # .fur, libgme chiptunes, and CLI-rendered formats (zxtune/openmpt) get their
        # waveform from MainWindow (live/pre-rendered/rendered-WAV); everything else is
        # ffmpeg-decoded here.
        import cli_render
        ext = track.path.suffix.lower()
        # .fur streams through the DSP engine → engine-buffer waveform.
        _skip = {".vgm", ".vgz", ".nsf", ".nsfe", ".spc", ".gbs",
                 ".ay", ".gym", ".hes", ".kss", ".sap"} | cli_render.EXTS
        if ext not in _skip:
            # Plain audio streams through the DSP engine, so read the shape from
            # its decode buffer rather than decoding the file a second time.
            eng = getattr(self._player, "_dsp", None)
            self._wf.load(track.path, engine=eng, key=self._wave_key(track.path))

    def _on_shuffle_toggle(self, on: bool):
        self._player.shuffle = on

    def _on_volume_changed(self, v: int):
        self._player.volume = _slider_to_vol(v)
        self._vol_pct.setText(f"{v}%")

    def _cycle_mode(self):
        cur = self._player.mode
        idx = _MODE_CYCLE.index(cur) if cur in _MODE_CYCLE else 0
        nxt = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
        self._player.mode = nxt
        self._btn_mode.setText(_MODE_LABELS[nxt])

    def _on_track(self, track):
        self._title.setText(track.title)
        sub = " / ".join(filter(None, [track.artist, track.album]))
        self._sub.setText(sub)
        self._cur_path = track.path
        self._subsong_row.setVisible(False)   # re-shown by _on_subsong_changed for multi-tune files

    # subsongs
    def _wave_key(self, path) -> str:
        """Cache key for the waveform: per-subsong when the file has subsongs."""
        n = self._player.subsong_count
        if n > 1:
            return f"{path}#sub{self._player.current_subsong}"
        return str(path)

    def _on_subsong_changed(self, cur: int, count: int):
        multi = count > 1
        self._subsong_row.setVisible(multi)
        if multi:
            self._ss_label.setText(f"{cur + 1} / {count}")
            self._ss_prev.setEnabled(cur > 0)
            self._ss_next.setEnabled(cur < count - 1)
            if self._ss_pick.count() != count:
                self._ss_pick.blockSignals(True)
                self._ss_pick.clear()
                self._ss_pick.addItems(self._player.subsong_titles()
                                       or [f"Subsong {i+1}" for i in range(count)])
                self._ss_pick.blockSignals(False)
            self._ss_pick.blockSignals(True)
            self._ss_pick.setCurrentIndex(cur)
            self._ss_pick.blockSignals(False)
            path = getattr(self, "_cur_path", None)
            if path is not None:
                if Path(path).suffix.lower() in _SID_EXTS:
                    # SID subsongs stream through the DSP engine — re-read the
                    # engine buffer under this subsong's own cache key.
                    eng = getattr(self._player, "_dsp", None)
                    self._wf.load(Path(path), engine=eng, key=self._wave_key(path))
                else:
                    self._wf.load_gme(path, cur)

    def _on_subsong_pick(self, idx: int):
        self._player.set_subsong(idx)

    def _on_subsong_prev(self):
        self._player.set_subsong(self._player.current_subsong - 1)

    def _on_subsong_next(self):
        self._player.set_subsong(self._player.current_subsong + 1)

    def _on_state(self, playing: bool):
        self._btn_play.setText(_IC_PAUSE if playing else _IC_PLAY)

    def _on_position(self, frac: float, cur: int, dur: int):
        if not self._seeking:
            self._seek.setValue(int(frac * 1000))
            if not self._wf._dragging:
                self._wf.set_position(frac)
        self._time_cur.setText(_fmt(cur))
        self._time_dur.setText(_fmt(dur))

    def _on_seek_release(self):
        self._seeking = False
        self._player.seek(self._seek.value() / 1000.0)

    # Keyboard shortcuts live in ONE place: MainWindow._handle_media_key (the
    # app-wide event filter). A second keymap here made keys like M do different
    # things depending on which widget had focus.
