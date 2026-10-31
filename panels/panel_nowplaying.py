from __future__ import annotations
import math
import threading
from functools import lru_cache
from pathlib import Path

try:
    from shiboken6 import isValid as _qt_valid
except ImportError:
    def _qt_valid(obj) -> bool:
        return obj is not None

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy, QMenu, QSlider, QStackedWidget
from PySide6.QtCore    import Qt, Signal, QTimer, QPointF
from PySide6.QtGui     import (QPixmap, QPainter, QPainterPath, QColor, QMouseEvent,
                               QRadialGradient, QTransform)
from video_view import make_video_widget

from player import Player, decode_art, extract_art, VIDEO_EXTS
from steady_label import SteadyLabel
import anim


# Hover feel for the art. The web UI uses a spring transition; a widget that
# repaints itself has none, so each value walks a fraction of the remaining
# distance every frame. Rising and falling use different rates, like the
# stylesheet's glow (0.15s fade in, 0.6s fade out): arriving feels quicker
# than leaving.
_ART_TICK_MS  = 16
_ART_RATE_IN  = 0.34
_ART_RATE_OUT = 0.11
_ART_SETTLED  = 0.002     # closer than this and the value is just its target
_ART_LEAN_X   = 8.0       # the site's depth translate, cx * 8 and cy * 6
_ART_LEAN_Y   = 6.0
_ART_SCALE    = 1.04
_ART_GLOW_R   = 180.0     # matches the 180px circle the stylesheet paints
# The site tilts as well as leans: perspective(400px) rotateX(-cy*9) rotateY(cx*12).
_ART_TILT_X   = 9.0       # degrees at the top and bottom edges
_ART_TILT_Y   = 12.0      # degrees at the left and right edges
_ART_PERSP    = 400.0     # the same 400px perspective the stylesheet sets
# 400px is a viewing distance meant for windows a few hundred pixels across.
# Left fixed, the same tilt throws a corner half again as far on a large panel
# as on a small one, and the art has to shrink by a third to stay inside.
# Keeping the distance proportional to the art makes the tilt look the same at
# every size.
_ART_PERSP_MIN = 1.5      # multiples of the art's own width


def _perspective(side: float) -> float:
    return max(_ART_PERSP, side * _ART_PERSP_MIN)


def _hover_extent(side: float, w: float, h: float) -> float:
    """How far past the widget the art reaches at its worst hover position.

    Negative means it stays inside. Every extreme of the lean and tilt is tried
    against all four corners, because the worst case is a corner: both rotations
    push the perspective divisor the same way and magnify together, which a
    single axis estimate misses.
    """
    if side <= 0:
        return -1.0
    x, y = (w - side) / 2.0, (h - side) / 2.0
    worst = -1e9
    for cx in (-0.5, 0.5):
        for cy in (-0.5, 0.5):
            ox, oy = cx * _ART_LEAN_X, cy * _ART_LEAN_Y
            rx = math.radians(-cy * 2.0 * _ART_TILT_X)
            ry = math.radians(cx * 2.0 * _ART_TILT_Y)
            d = _perspective(side)
            sx, sy = math.sin(ry) / d, -math.sin(rx) / d
            cxr, cyr = math.cos(ry), math.cos(rx)
            for px, py in ((x, y), (x + side, y), (x, y + side), (x + side, y + side)):
                bx, by = px - w / 2.0, py - h / 2.0
                denom = 1.0 + bx * sx + by * sy
                if denom <= 0.05:          # past the vanishing point, unusable
                    return 1e9
                fx = bx * cxr / denom * _ART_SCALE + w / 2.0 + ox
                fy = by * cyr / denom * _ART_SCALE + h / 2.0 + oy
                worst = max(worst, -fx, fx - w, -fy, fy - h)
    return worst


@lru_cache(maxsize=32)
def _art_side(w: int, h: int) -> int:
    """The largest art side that still fits once the hover lean and tilt are applied.

    Found by bisection on the real transform: the tilt magnifies by a factor that
    depends on the size being solved for, and a closed form came out too generous.
    It runs once per widget size and the result is cached.
    """
    lo, hi = 0.0, float(min(w, h))
    if hi <= 0:
        return 0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if _hover_extent(mid, w, h) <= 0.0:
            lo = mid
        else:
            hi = mid
    return int(lo)


def _approach(cur: float, target: float, rate: float) -> float:
    """Step `cur` a fraction of the way to `target`, snapping once it is close.

    Without the snap a value approaches its target forever and the repaint timer
    never has a reason to stop.
    """
    nxt = cur + (target - cur) * rate
    return target if abs(target - nxt) < _ART_SETTLED else nxt


class _MiniSeek(QSlider):
    """The seek bar under the art. A real slider, so the style sheet themes it exactly
    like the one in Controls (the filled part and the handle take the accent colours)."""

    seeked = Signal(float)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 1000)
        self._dragging = False
        self.setFixedHeight(18)
        self.setCursor(Qt.PointingHandCursor)

    def set_position(self, frac: float):
        self.blockSignals(True)
        self.setValue(int(round(max(0.0, min(1.0, frac)) * 1000)))
        self.blockSignals(False)

    def _seek_to(self, e: QMouseEvent):
        f = max(0.0, min(1.0, float(e.position().x()) / max(1, self.width() - 1)))
        self.set_position(f)
        self.seeked.emit(f)

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.LeftButton:
            self._dragging = True
            self._seek_to(e)

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._dragging:
            self._seek_to(e)

    def mouseReleaseEvent(self, e: QMouseEvent):
        self._dragging = False


class _ArtLabel(QLabel):
    """Album art that lights up under the pointer and leans towards it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm: QPixmap | None = None
        self._scaled: QPixmap | None = None   # cached per-size scale of _pm
        self._scaled_key = None
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(80, 80)

        self._accent = None                   # set from the album-art palette
        self._cursor = QPointF(0.0, 0.0)      # pointer, in widget coordinates
        self._over_art = False   # pointer is on the art, not just in the widget
        self._glow   = 0.0                    # 0 away, 1 fully lit
        self._off_x  = 0.0
        self._off_y  = 0.0
        self._scale  = 1.0
        self._t_off_x = 0.0
        self._t_off_y = 0.0
        self._tilt_x = 0.0        # degrees, about the horizontal axis
        self._tilt_y = 0.0
        self._t_tilt_x = 0.0
        self._t_tilt_y = 0.0
        self.setMouseTracking(True)
        self._anim = QTimer(self)
        self._anim.setInterval(_ART_TICK_MS)
        self._anim.timeout.connect(self._step)

    def set_pixmap(self, pm: QPixmap | None):
        if pm is not None and not pm.isNull() and max(pm.width(), pm.height()) > 1024:
            # keep a display-sized copy, not the full-resolution original
            pm = pm.scaled(1024, 1024, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._pm = pm
        self._scaled = None
        self._scaled_key = None
        self.update()

    def look(self):
        """A function that paints the cover as it is now, for anim.hold."""
        pm, (x, y, w, h) = self._scaled, self._art_rect()

        def paint(p):
            if pm is not None and not pm.isNull():
                path = QPainterPath()
                path.addRoundedRect(x, y, w, h, 12, 12)
                p.setClipPath(path)
                p.drawPixmap(x, y, pm)
        return paint

    def set_accent(self, palette: dict | None):
        """Tint the glow with the colour taken from the album art."""
        if palette:
            self._accent = QColor(palette.get("accent", palette.get("accent_l", "#ffffff")))
        else:
            self._accent = None
        if self._glow > 0.0:
            self.update()

    def enterEvent(self, e):
        self._anim.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._over_art = False
        self._t_off_x = self._t_off_y = 0.0
        self._t_tilt_x = self._t_tilt_y = 0.0
        self._anim.start()
        super().leaveEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent):
        pos = e.position() if hasattr(e, "position") else QPointF(e.pos())
        self._cursor = pos
        x, y, w, h = self._art_rect()
        # Measured against the art, not the widget around it. Against the widget
        # the pointer drives the lean while it is still out on the empty margin,
        # and never reaches the full throw at the art's own edge, which is
        # exactly where the site's cx and cy reach theirs.
        inside = (w > 0 and h > 0
                  and x <= pos.x() <= x + w and y <= pos.y() <= y + h)
        self._over_art = inside
        if inside:
            cx = (pos.x() - x) / w - 0.5
            cy = (pos.y() - y) / h - 0.5
            self._t_off_x = cx * _ART_LEAN_X
            self._t_off_y = cy * _ART_LEAN_Y
            # rotateX takes -cy so the top edge falls away as the pointer rises
            self._t_tilt_x = -cy * 2.0 * _ART_TILT_X
            self._t_tilt_y = cx * 2.0 * _ART_TILT_Y
        else:
            self._t_off_x = self._t_off_y = 0.0
            self._t_tilt_x = self._t_tilt_y = 0.0
        if not self._anim.isActive():
            self._anim.start()
        self.update()          # the glow follows the pointer with no easing
        super().mouseMoveEvent(e)

    def _step(self):
        live = self._over_art
        rate = _ART_RATE_IN if live else _ART_RATE_OUT
        self._glow  = _approach(self._glow, 1.0 if live else 0.0, rate)
        self._scale = _approach(self._scale, _ART_SCALE if live else 1.0, rate)
        self._off_x = _approach(self._off_x, self._t_off_x, rate)
        self._off_y = _approach(self._off_y, self._t_off_y, rate)
        self._tilt_x = _approach(self._tilt_x, self._t_tilt_x, rate)
        self._tilt_y = _approach(self._tilt_y, self._t_tilt_y, rate)
        if not live and self._glow == 0.0 and self._off_x == 0.0 \
                and self._off_y == 0.0 and self._scale == 1.0 \
                and self._tilt_x == 0.0 and self._tilt_y == 0.0:
            self._anim.stop()   # fully back at rest, nothing left to repaint for
        self.update()

    def _art_rect(self):
        """Where the art is actually drawn: x, y, width, height.

        The widget expands to fill its dock and the art is centred inside it, so
        the two are rarely the same shape. A cover that is not square is smaller
        again, since it keeps its aspect.
        """
        W, H = self.width(), self.height()
        side = _art_side(W, H)
        if side <= 0:
            return 0, 0, 0, 0
        if self._scaled is not None and not self._scaled.isNull():
            w, h = self._scaled.width(), self._scaled.height()
        else:
            w = h = side
        return (W - w) // 2, (H - h) // 2, w, h

    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(0, 0, W, H, self.palette().base())

        # Lean, zoom and tilt about the middle of the widget, so the art moves
        # as one piece rather than sliding out of its own frame.
        if (self._off_x or self._off_y or self._scale != 1.0
                or self._tilt_x or self._tilt_y):
            p.translate(W / 2.0 + self._off_x, H / 2.0 + self._off_y)
            p.scale(self._scale, self._scale)
            if self._tilt_x or self._tilt_y:
                p.setTransform(self._tilt(), True)
            p.translate(-W / 2.0, -H / 2.0)

        if self._pm and not self._pm.isNull():
            side = _art_side(W, H)
            if self._scaled_key != side or self._scaled is None:
                self._scaled = self._pm.scaled(side, side, Qt.KeepAspectRatio,
                                               Qt.SmoothTransformation)
                self._scaled_key = side
            scaled = self._scaled
            x      = (W - scaled.width())  // 2
            y      = (H - scaled.height()) // 2

            path = QPainterPath()
            path.addRoundedRect(x, y, scaled.width(), scaled.height(), 12, 12)
            p.setClipPath(path)
            p.drawPixmap(x, y, scaled)
            self._paint_glow(p, path)
            p.setClipping(False)

            p.setPen(QColor(255, 255, 255, 18))
            p.drawRoundedRect(x, y, scaled.width(), scaled.height(), 12, 12)
        else:
            pal  = self.palette()
            side = _art_side(W, H)
            x    = (W - side) // 2
            y    = (H - side) // 2
            p.setBrush(pal.mid())
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(x, y, side, side, 12, 12)
            p.setPen(pal.midlight().color())
            f = self.font()
            f.setPixelSize(max(24, side // 3))   # fill the art box with the placeholder glyph
            p.setFont(f)
            p.drawText(x, y, side, side, Qt.AlignCenter, "\U000f0387")   # nf-md-music
            path = QPainterPath()
            path.addRoundedRect(x, y, side, side, 12, 12)
            p.setClipPath(path)
            self._paint_glow(p, path)
            p.setClipping(False)

        p.end()

    def _tilt(self) -> QTransform:
        """A rotateX and rotateY under perspective, as a projective transform.

        CSS perspective divides a point by 1 + z/d after rotating it, and that
        division is what the bottom row of a 3x3 transform does. Rotating about
        the vertical axis sends x to x*cos(ry) and z to x*sin(ry), so the pair
        lands in m11 and m13, and the horizontal axis fills m22 and m23 the same
        way. Qt then has a real tilt rather than a squash pretending to be one.
        """
        rx = math.radians(self._tilt_x)
        ry = math.radians(self._tilt_y)
        d = _perspective(_art_side(self.width(), self.height()))
        return QTransform(math.cos(ry), 0.0, math.sin(ry) / d,
                          0.0, math.cos(rx), -math.sin(rx) / d,
                          0.0, 0.0, 1.0)

    def _paint_glow(self, p: QPainter, clip: QPainterPath):
        """A soft pool of light under the pointer, clipped to the art itself."""
        if self._glow <= 0.0:
            return
        base = self._accent or self.palette().highlight().color()
        if not base.isValid():
            base = QColor(255, 255, 255)
        g = QRadialGradient(self._cursor, _ART_GLOW_R)
        # 14% at the middle and 5% halfway out are the stops the stylesheet uses.
        for stop, pct in ((0.0, 0.14), (0.5, 0.05), (1.0, 0.0)):
            c = QColor(base)
            c.setAlphaF(pct * self._glow)
            g.setColorAt(stop, c)
        p.fillPath(clip, g)


class NowPlayingWidget(QWidget):
    _art_ready = Signal(object)

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player = player
        self._build()
        self._art_ready.connect(self._apply_art)
        player.track_changed.connect(self._on_track)
        player.position_changed.connect(self._on_position)
        player.state_changed.connect(self._on_state)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self._art = _ArtLabel()
        self._np_video = make_video_widget()
        self._np_video.setAspectRatioMode(Qt.KeepAspectRatio)
        self._np_video.setStyleSheet("background: black;")
        self._np_video.hide()

        self._media_stack = QStackedWidget()
        self._media_stack.addWidget(self._art)       # index 0
        self._media_stack.addWidget(self._np_video)  # index 1
        root.addWidget(self._media_stack, 1)

        info = QVBoxLayout()
        info.setSpacing(2)

        self._title = SteadyLabel()
        self._title.setText("No track loaded")
        self._title.setObjectName("track_title")
        self._title.setAlignment(Qt.AlignCenter)

        self._artist = SteadyLabel()
        self._artist.setObjectName("track_sub")
        self._artist.setAlignment(Qt.AlignCenter)

        self._album = SteadyLabel()
        self._album.setObjectName("track_sub")
        self._album.setAlignment(Qt.AlignCenter)

        info.addWidget(self._title)
        info.addWidget(self._artist)
        info.addWidget(self._album)
        root.addLayout(info)

        self._seek = _MiniSeek()
        self._seek.seeked.connect(self._player.seek)
        root.addWidget(self._seek)

        self._time_row_widget = QWidget()
        time_row = QHBoxLayout(self._time_row_widget)
        time_row.setContentsMargins(0, 0, 0, 0)
        self._cur = QLabel("0:00")
        self._cur.setObjectName("time_label")
        self._dur = QLabel("0:00")
        self._dur.setObjectName("time_label")
        self._cur.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._dur.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        time_row.addWidget(self._cur)
        time_row.addStretch()
        time_row.addWidget(self._dur)
        root.addWidget(self._time_row_widget)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx_menu)

    def set_accent(self, palette: dict | None):
        """Pass the album-art colour down to the art's hover glow."""
        self._art.set_accent(palette)

    def _ctx_menu(self, pos):
        m = QMenu(self)
        visible = self._seek.isVisible()
        a = m.addAction("Hide seekbar" if visible else "Show seekbar")
        a.triggered.connect(lambda: self._set_seekbar_visible(not visible))
        m.exec(self.mapToGlobal(pos))

    def _set_seekbar_visible(self, visible: bool):
        self._seek.setVisible(visible)
        self._time_row_widget.setVisible(visible)

    @property
    def video_widget(self) -> QWidget:
        return self._np_video

    def _on_track(self, track):
        anim.set_text(self._title, track.title or track.path.stem)
        anim.set_text(self._artist, track.artist)
        anim.set_text(self._album, track.album)
        is_video = track.path.suffix.lower() in VIDEO_EXTS
        self._media_stack.setCurrentIndex(1 if is_video else 0)
        if not is_video:
            anim.hold(self._art, self._art.look())
            self._art.set_pixmap(None)
            threading.Thread(target=self._load_art, args=(track.path,), daemon=True).start()

    def _load_art(self, path: Path):
        data = extract_art(path)
        img = decode_art(data) if data else None
        if _qt_valid(self):
            self._art_ready.emit((path, img))

    def _apply_art(self, got):
        path, img = got
        cur = self._player.current
        if cur is None or cur.path != path:
            return                  # a slow read for a song already skipped must not cover the one playing now
        self._art.set_pixmap(QPixmap.fromImage(img) if img is not None else None)
        anim.release(self._art)

    def _on_position(self, frac: float, cur_ms: int, dur_ms: int):
        def fmt(ms):
            s = ms // 1000
            return f"{s // 60}:{s % 60:02d}"
        if not self._seek._dragging:
            self._seek.set_position(frac)
        self._cur.setText(fmt(cur_ms))
        self._dur.setText(fmt(dur_ms))

    def _on_state(self, playing: bool):
        pass
