from __future__ import annotations
import numpy as np
try:
    from scipy.signal import filtfilt as _filtfilt
except Exception:                       # scipy is a hard dep, but degrade gracefully
    _filtfilt = None

from PySide6.QtWidgets import (
    QWidget, QDialog, QFormLayout, QDialogButtonBox, QSlider, QSpinBox,
    QCheckBox, QMenu,
)
from PySide6.QtCore    import Qt, QTimer, QPointF, Signal
from PySide6.QtGui     import QPainter, QColor, QPen, QPolygonF

from analyzer import Analyzer, _RATE
from player   import Player
from panel_visualizer import _ColorLerper

_DEFAULT_ACCENT = "#8040c8"
# Low-pass cutoff (Hz) for the trigger detector: lock to the bass/sub wave so the
# trace doesn't re-trigger on HF zero crossings riding on top (cf. the site scope).
_TRIG_LP_HZ = 60.0


class _Cfg:
    window_ms  = 40
    gain       = 100
    line_width = 1.5
    smooth     = 1     # 0 = off (raw trace); higher = softer
    trigger    = True
    fps        = 60


class _ConfigDialog(QDialog):
    def __init__(self, cfg: _Cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Oscilloscope Settings")
        self._cfg = cfg
        form = QFormLayout(self)

        self._win = QSpinBox()
        self._win.setRange(5, 500)
        self._win.setSuffix(" ms")
        self._win.setValue(cfg.window_ms)
        form.addRow("Window", self._win)

        self._gain = QSlider(Qt.Horizontal)
        self._gain.setRange(10, 500)
        self._gain.setValue(cfg.gain)
        form.addRow("Gain %", self._gain)

        self._smooth = QSpinBox()
        self._smooth.setRange(0, 20)
        self._smooth.setSuffix(" samples")
        self._smooth.setValue(max(0, cfg.smooth))
        form.addRow("Smoothing", self._smooth)

        self._lw = QSlider(Qt.Horizontal)
        self._lw.setRange(5, 40)
        self._lw.setValue(int(cfg.line_width * 10))
        form.addRow("Line width", self._lw)

        self._fps = QSpinBox()
        self._fps.setRange(15, 120)
        self._fps.setSuffix(" fps")
        self._fps.setValue(cfg.fps)
        form.addRow("Frame rate", self._fps)

        self._trig = QCheckBox("Zero-crossing trigger")
        self._trig.setChecked(cfg.trigger)
        form.addRow("", self._trig)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _apply(self):
        self._cfg.window_ms  = self._win.value()
        self._cfg.gain       = self._gain.value()
        self._cfg.smooth     = self._smooth.value()
        self._cfg.line_width = self._lw.value() / 10.0
        self._cfg.trigger    = self._trig.isChecked()
        self._cfg.fps        = self._fps.value()
        self.accept()


class OscilloscopeWidget(QWidget):
    """A lightweight time-domain scope.

    Capture path: pull a window of mono audio at the play head, optionally align
    it to the first rising zero-crossing (a classic scope trigger that keeps the
    trace from sliding sideways), normalise against a peak-follower envelope, and
    resample to one point per pixel. Render path: a single antialiased polyline —
    no per-frame image buffers.
    """

    config_changed = Signal(dict)   # popup-dialog edits → persisted via the Settings panel

    def __init__(self, analyzer: Analyzer, player: Player, parent=None):
        super().__init__(parent)
        self._analyzer = analyzer
        self._player   = player
        self._cfg      = _Cfg()
        self._wave     = np.zeros(0, dtype=np.float32)
        self._env      = 1e-6
        self._system_accent = False   # follow OS highlight when on system theme
        self._has_art       = False   # an album-art palette currently overrides
        self._default_accent = QColor(_DEFAULT_ACCENT)  # app accent (user-customisable)
        self._lerp     = _ColorLerper(QColor(self._default_accent))
        self.setMinimumHeight(80)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx_menu)
        self._suspended = False    # set by the window when minimized/unfocused
        self._tick_timer = QTimer(self)
        # PreciseTimer: the default coarse timer batches wakeups (±5%) and made
        # the trace visibly stutter between "fast" and "slow" stretches.
        self._tick_timer.setTimerType(Qt.PreciseTimer)
        self._tick_timer.setInterval(max(8, round(1000 / self._cfg.fps)))
        self._tick_timer.timeout.connect(self._tick)
        # Only runs while actually visible + focused (see _sync_timer / showEvent).

    # run only when visible & focused (perf)

    def set_suspended(self, suspended: bool):
        self._suspended = bool(suspended)
        self._sync_timer()

    def _sync_timer(self):
        run = self.isVisible() and not self._suspended
        if run and not self._tick_timer.isActive():
            self._tick_timer.start()
        elif not run and self._tick_timer.isActive():
            self._tick_timer.stop()
            self.update()   # paint one idle frame

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_timer()

    # accent colour

    def set_accent(self, palette: dict | None):
        if palette:
            self._has_art = True
            self._lerp.set_target(QColor(palette.get("accent", _DEFAULT_ACCENT)))
        else:
            self._has_art = False
            self._lerp.set_target(self._idle_accent())

    def set_system_accent(self, on: bool):
        """When enabled, the idle trace colour tracks the OS highlight colour."""
        self._system_accent = bool(on)
        if not self._has_art:
            self._lerp.set_target(self._idle_accent())

    def set_default_accent(self, color):
        """The app-theme idle colour (user-customisable purple replacement)."""
        self._default_accent = QColor(color)
        if not self._has_art and not self._system_accent:
            self._lerp.set_target(self._idle_accent())

    def _idle_accent(self) -> QColor:
        if self._system_accent:
            c = self.palette().highlight().color()
            if c.isValid():
                return c
        return QColor(self._default_accent)

    # capture

    def _decay(self):
        if len(self._wave):
            self._wave = self._wave * np.float32(0.88)
        self.update()

    def _tick(self):
        self._lerp.step()
        if not self._player.is_playing:
            self._decay()
            return

        W = max(2, self.width())
        n = max(2, int(self._cfg.window_ms / 1000.0 * _RATE))   # samples shown

        # Capture extra when triggering so the lock point can sit centred, with
        # pre-trigger data filling the left half — a scope shows context *before*
        # the trigger, not just after it.
        cap = 2 * n if self._cfg.trigger else n
        window = self._analyzer.get_audio_window(self._player.position_ms, cap)
        if (window is None or len(window) < n) and cap > n:
            # near end-of-track / live underrun: retry with just the display length
            window = self._analyzer.get_audio_window(self._player.position_ms, n)
        if window is None or len(window) < 2:
            self._decay()
            return

        # DC-block (centre vertically): matches the site's 5 Hz high-pass ahead of
        # its analyser — keeps a DC-offset chip waveform on-screen and lets the
        # zero-crossing search actually find crossings.
        x = (window - np.float32(np.mean(window))).astype(np.float32)

        L = len(x)
        n = min(n, L)
        start = 0
        if self._cfg.trigger and L > n:
            half = n // 2
            # Lock to the bass: low-pass (~60 Hz) before the crossing search so we
            # don't re-trigger on HF wiggles riding on a slow sub wave. Zero-phase
            # (filtfilt) so the detected crossing lands on the *true* bass crossing
            # in the raw trace — no group-delay offset.
            if _filtfilt is not None and L > 27:
                a  = 1.0 - float(np.exp(-2.0 * np.pi * _TRIG_LP_HZ / _RATE))
                sm = _filtfilt([a], [1.0, -(1.0 - a)], x).astype(np.float32)
            else:
                sm = x
            # Rising zero crossing; valid start positions keep the centred slice
            # in-bounds, so search indices i where start = i-half stays in [0, L-n].
            lo = max(1, half)
            hi = max(lo + 1, L - n + half + 1)
            hits = np.where((sm[lo - 1:hi - 1] < 0.0) & (sm[lo:hi] >= 0.0))[0]
            t = lo + int(hits[0]) if len(hits) else lo
            start = min(max(t - half, 0), L - n)

        raw = x[start:start + n]

        # Peak-follower envelope keeps amplitude readable across quiet/loud passages.
        peak = float(np.max(np.abs(raw))) if len(raw) else 0.0
        if peak > self._env:
            self._env = peak
        else:
            self._env *= 0.995
        norm = raw / self._env if self._env > 1e-6 else raw

        disp = np.interp(
            np.linspace(0, len(norm) - 1, W),
            np.arange(len(norm)),
            norm,
        ).astype(np.float32)

        k = int(self._cfg.smooth)
        if k > 0:
            kernel = np.ones(2 * k + 1, dtype=np.float32) / (2 * k + 1)
            disp   = np.convolve(disp, kernel, mode="same")

        # Cosine-taper the very ends (Tukey window) so the trace fades into the
        # edges instead of snapping there.
        taper = max(2, W // 24)
        if W > 2 * taper:
            ramp = ((1.0 - np.cos(np.pi * np.arange(taper) / taper)) * 0.5).astype(np.float32)
            disp[:taper]  *= ramp
            disp[-taper:] *= ramp[::-1]

        self._wave = disp
        self.update()

    # render

    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return

        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().base())

        wave = self._wave
        if len(wave) < 2:
            p.end()
            return

        p.setRenderHint(QPainter.Antialiasing, True)

        gain = self._cfg.gain / 100.0
        mid  = H * 0.5
        amp  = mid * 0.9 * gain

        w  = min(len(wave), W)
        xs = np.linspace(0.0, W - 1.0, w)
        ys = np.clip(mid - wave[:w] * amp, 0.0, H - 1.0)

        poly = QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys)])

        pen = QPen(self._lerp.color)
        pen.setWidthF(max(0.5, float(self._cfg.line_width)))
        pen.setCosmetic(True)
        p.setPen(pen)
        p.drawPolyline(poly)
        p.end()

    # config

    def apply_config(self, d: dict):
        if "window_ms"  in d: self._cfg.window_ms  = d["window_ms"]
        if "gain"       in d: self._cfg.gain       = d["gain"]
        if "smooth"     in d: self._cfg.smooth     = max(0, d["smooth"])
        if "line_width" in d: self._cfg.line_width = d["line_width"]
        if "trigger"    in d: self._cfg.trigger    = bool(d["trigger"])
        if "fps"        in d:
            self._cfg.fps = max(15, min(120, int(d["fps"])))
            self._tick_timer.setInterval(max(8, round(1000 / self._cfg.fps)))

    def _ctx_menu(self, pos):
        m = QMenu(self)
        m.addAction("Settings...", self._open_settings)
        m.exec(self.mapToGlobal(pos))

    def _open_settings(self):
        if _ConfigDialog(self._cfg, self).exec():
            # route through the Settings panel so it persists and both UIs match
            self._tick_timer.setInterval(max(8, round(1000 / self._cfg.fps)))
            self.config_changed.emit({
                "osc_window_ms":  self._cfg.window_ms,
                "osc_gain":       self._cfg.gain,
                "osc_smooth":     self._cfg.smooth,
                "osc_line_width": int(round(self._cfg.line_width * 10)),
                "osc_trigger":    self._cfg.trigger,
                "osc_fps":        self._cfg.fps,
            })
