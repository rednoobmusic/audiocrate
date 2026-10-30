from __future__ import annotations
import threading
import time
import numpy as np

from PySide6.QtWidgets import QWidget, QMenu
from PySide6.QtCore import Qt, QRectF, QTimer, Signal
from PySide6.QtGui  import (
    QBrush, QGradient, QPainter, QColor, QLinearGradient, QPixmap,
)

import power
from analyzer import Analyzer, BANDS
from player   import Player, decode_art, extract_art

_LERP_S = 0.7      # seconds a colour change takes


class _ColorLerper:
    """Interpolates between two QColors over _LERP_S seconds of real time, so
    panels drawn at different frame rates fade at the same speed."""

    def __init__(self, color: QColor):
        self._cur = color
        self._tgt = color
        self._t0 = None           # when the current fade began; None when settled

    def set_target(self, color: QColor):
        self._cur = self._current()
        self._tgt = color
        self._t0 = time.monotonic()

    def step(self):
        """No-op: the colour follows the clock, not the frame count."""

    def _current(self) -> QColor:
        if self._t0 is None:
            return self._tgt
        t = (time.monotonic() - self._t0) / _LERP_S
        if t >= 1.0:
            self._t0 = None
            return self._tgt
        return QColor(
            int(self._cur.red()   + (self._tgt.red()   - self._cur.red())   * t),
            int(self._cur.green() + (self._tgt.green() - self._cur.green()) * t),
            int(self._cur.blue()  + (self._tgt.blue()  - self._cur.blue())  * t),
        )

    @property
    def color(self) -> QColor:
        return self._current()

    @property
    def animating(self) -> bool:
        return self._t0 is not None and time.monotonic() - self._t0 < _LERP_S


class _Cfg:
    art_opacity = 0.28
    show_art    = True
    peak_hold   = True
    peak_thresh = 0.025
    bar_count   = BANDS
    bar_gap     = 2
    corner_r    = 3
    fft_enabled = True
    fft_gain    = 100


class VisualizerWidget(QWidget):
    _art_ready      = Signal(object)   # QImage or None, decoded off the GUI thread
    open_settings   = Signal()   # context menu "Settings" item

    def __init__(self, analyzer: Analyzer, player: Player, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent)     # paintEvent fills the whole widget
        self._rested = False                       # the last frame drawn was the one for silence
        self._analyzer = analyzer
        self._player   = player
        self._art: QPixmap | None = None
        self._art_scaled: QPixmap | None = None   # cached per-size scale of _art
        self._art_scaled_key = None
        self._bar_brushes: list = []              # per-bar gradient brush, sized by the bar it fills
        self._bar_colors_key = None
        self._cfg = _Cfg()
        self._default_accent = QColor("#8040c8")  # app accent (user-customisable)
        self._lerp1 = _ColorLerper(self._default_accent.lighter(140))
        self._lerp2 = _ColorLerper(self._default_accent.darker(190))
        self._system_accent = False   # follow OS highlight on system theme
        self._has_art = False         # an album-art palette currently overrides
        self.setMinimumHeight(100)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx_menu)

        self._art_ready.connect(self._apply_art)

        player.track_changed.connect(
            lambda t: threading.Thread(
                target=self._load_art, args=(t.path,), daemon=True
            ).start()
        )

        self._suspended = False    # set by the window when minimized/unfocused
        self._tick_timer = QTimer(self)
        self._tick_timer.setTimerType(Qt.PreciseTimer)   # coarse timers stutter
        self._tick_timer.setInterval(power.frame_ms(28))
        self._tick_timer.timeout.connect(self._tick)
        power.watch().changed.connect(
            lambda _: self._tick_timer.setInterval(power.frame_ms(28)))
        # Runs only while visible and not suspended (see _sync_timer).

    # run only when visible and not suspended (no FFT or redraw when hidden)

    def set_suspended(self, suspended: bool):
        self._suspended = bool(suspended)
        self._sync_timer()

    def _sync_timer(self):
        run = self.isVisible() and not self._suspended
        if run and not self._tick_timer.isActive():
            self._tick_timer.start()
        elif not run and self._tick_timer.isActive():
            self._tick_timer.stop()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_timer()

    def _load_art(self, path):
        data = extract_art(path)
        self._art_ready.emit((path, decode_art(data) if data else None))

    def _apply_art(self, got):
        path, img = got
        cur = self._player.current
        if cur is None or cur.path != path:
            return                  # a slow read for a song already skipped
        self._art = QPixmap.fromImage(img) if img is not None else None
        self._art_scaled = None
        self._art_scaled_key = None
        self.update()

    def _scaled_art(self, W: int, H: int) -> QPixmap | None:
        """Scale the art to the widget once per size change, not every frame."""
        if self._art is None or self._art.isNull():
            return None
        key = (W, H, self._art.cacheKey())
        if self._art_scaled_key != key:
            self._art_scaled = self._art.scaled(
                W, H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._art_scaled_key = key
        return self._art_scaled

    def _tick(self):
        if self._cfg.fft_enabled:
            self._analyzer.update(self._player.position_ms, self._player.is_playing)
        self._lerp1.step()
        self._lerp2.step()
        rest = self._at_rest()
        if rest and self._rested:
            return                  # nothing plays and the bars have fallen: the frame on screen is the frame
        self._rested = rest
        self.update()

    def _at_rest(self) -> bool:
        a, gain = self._analyzer, self._cfg.fft_gain / 100.0
        return (not self._player.is_playing and not self._lerp1.animating and not self._lerp2.animating
                and float(a.bands.max()) * gain < 0.003 and float(a.peaks.max()) * gain <= self._cfg.peak_thresh)

    def set_accent(self, palette: dict | None):
        if palette:
            self._has_art = True
            self._lerp1.set_target(QColor(palette.get("accent_l", palette["accent"])))
            self._lerp2.set_target(QColor(palette.get("accent_d", palette["accent"])))
        else:
            self._has_art = False
            c1, c2 = self._idle_accents()
            self._lerp1.set_target(c1)
            self._lerp2.set_target(c2)

    def set_system_accent(self, on: bool):
        """On the system theme, the no-art colour follows the OS highlight."""
        self._system_accent = bool(on)
        if not self._has_art:
            c1, c2 = self._idle_accents()
            self._lerp1.set_target(c1)
            self._lerp2.set_target(c2)

    def set_default_accent(self, color):
        """The app-theme no-art colour (user-customisable purple replacement)."""
        self._default_accent = QColor(color)
        if not self._has_art and not self._system_accent:
            c1, c2 = self._idle_accents()
            self._lerp1.set_target(c1)
            self._lerp2.set_target(c2)

    def _idle_accents(self) -> tuple[QColor, QColor]:
        if self._system_accent:
            h = self.palette().highlight().color()
            if h.isValid():
                return h, h.darker(220)
        return self._default_accent.lighter(140), self._default_accent.darker(190)

    def apply_config(self, d: dict):
        if "fft_enabled" in d: self._cfg.fft_enabled  = d["fft_enabled"]
        if "fft_gain"    in d: self._cfg.fft_gain      = d["fft_gain"]
        if "art_opacity" in d: self._cfg.art_opacity   = d["art_opacity"]
        if "show_art"    in d: self._cfg.show_art      = d["show_art"]
        if "bar_count"   in d: self._cfg.bar_count     = min(BANDS, d["bar_count"])
        if "bar_gap"     in d: self._cfg.bar_gap       = d["bar_gap"]
        if "corner_r"    in d: self._cfg.corner_r      = d["corner_r"]
        if "peak_hold"   in d: self._cfg.peak_hold     = d["peak_hold"]
        self.update()

    def _ctx_menu(self, pos):
        m = QMenu(self)
        m.addAction("Settings…", self.open_settings.emit)
        m.addAction("Clear art", lambda: setattr(self, "_art", None) or self.update())
        m.exec(self.mapToGlobal(pos))

    def paintEvent(self, _):
        cfg   = self._cfg
        n     = cfg.bar_count
        gain  = cfg.fft_gain / 100.0
        bands = np.clip(self._analyzer.bands[:n] * gain, 0.0, 1.0)
        peaks = np.clip(self._analyzer.peaks[:n] * gain, 0.0, 1.0)

        p    = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        p.fillRect(0, 0, W, H, self.palette().window())

        scaled = self._scaled_art(W, H) if cfg.show_art else None
        if scaled is not None:
            dx = (W - scaled.width())  // 2
            dy = (H - scaled.height()) // 2
            p.setOpacity(cfg.art_opacity)
            p.drawPixmap(dx, dy, scaled)
            p.setOpacity(1.0)

            if cfg.art_opacity < 1.0:
                bg   = self.palette().window().color()
                fade = int((1.0 - cfg.art_opacity) * 200)
                grad = QLinearGradient(0, 0, 0, H)
                grad.setColorAt(0.0, QColor(bg.red(), bg.green(), bg.blue(), fade // 2))
                grad.setColorAt(1.0, QColor(bg.red(), bg.green(), bg.blue(), fade))
                p.fillRect(0, 0, W, H, grad)

        if not cfg.fft_enabled:
            p.end()
            return

        gap  = float(cfg.bar_gap)
        step = W / n
        bw   = max(2.0, step - gap)
        cr   = float(cfg.corner_r)

        hi    = self._lerp1.color
        hi2   = self._lerp2.color
        pkclr = hi.lighter(160)

        # Per-bar colours depend only on the lerp state: rebuild the table when
        # the colours move (~700 ms per track change), not on every frame.
        ckey = (hi.rgba(), hi2.rgba(), n)
        if self._bar_colors_key != ckey:
            brushes = []
            for i in range(n):
                t  = i / max(n - 1, 1)
                r0 = int(hi.red()   * (1 - t) + hi2.red()   * t)
                g0 = int(hi.green() * (1 - t) + hi2.green() * t)
                b0 = int(hi.blue()  * (1 - t) + hi2.blue()  * t)
                top_clr = QColor(min(255, int(r0 * 1.3)),
                                 min(255, int(g0 * 1.3)),
                                 min(255, int(b0 * 1.3)), 235)
                bot_clr = QColor(r0 // 2, g0 // 2, b0 // 2, 150)
                # Bounding-box coordinates: the same brush fits a bar of any height,
                # so nothing is built per bar per frame.
                grad = QLinearGradient(0.0, 0.0, 0.0, 1.0)
                grad.setCoordinateMode(QGradient.ObjectBoundingMode)
                grad.setColorAt(0.0, top_clr)
                grad.setColorAt(1.0, bot_clr)
                brushes.append(QBrush(grad))
            self._bar_brushes = brushes
            self._bar_colors_key = ckey

        rounded = cr > 0.0
        if not rounded:
            p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(Qt.NoPen)

        brushes = self._bar_brushes
        for i in range(n):
            v  = float(bands[i])
            pk = float(peaks[i])
            bh = max(2.0, v * (H - 6))
            x  = i * step
            y  = H - bh

            if rounded:
                p.setBrush(brushes[i])
                p.drawRoundedRect(QRectF(x, y, bw, bh), cr, cr)
            else:
                p.fillRect(int(x), int(y), int(bw), int(bh) + 1, brushes[i])

            if cfg.peak_hold and pk > cfg.peak_thresh:
                py = max(1.0, H - pk * (H - 6) - 3)
                p.fillRect(int(x), int(py), int(bw), 2, pkclr)

        p.end()
