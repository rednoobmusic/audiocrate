from __future__ import annotations
import threading
from pathlib import Path

try:
    from shiboken6 import isValid as _qt_valid
except ImportError:
    def _qt_valid(obj) -> bool:
        return obj is not None

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy, QMenu, QStackedWidget
from PySide6.QtCore    import Qt, Signal, QTimer, QPointF
from PySide6.QtGui     import (QPixmap, QPainter, QPainterPath, QColor, QMouseEvent,
                               QRadialGradient)
from PySide6.QtMultimediaWidgets import QVideoWidget

from player import Player, extract_art, VIDEO_EXTS


# Hover feel for the art. The web UI reaches the same place with a spring
# transition; a widget that repaints itself has no transitions, so each value
# walks a fraction of the remaining distance every frame instead. Rising and
# falling use different rates for the same reason the stylesheet gives the glow
# a 0.15s fade in and a 0.6s fade out: arriving should feel quicker than leaving.
_ART_TICK_MS  = 16
_ART_RATE_IN  = 0.34
_ART_RATE_OUT = 0.11
_ART_SETTLED  = 0.002     # closer than this and the value is just its target
_ART_LEAN_X   = 8.0       # the site's depth translate, cx * 8 and cy * 6
_ART_LEAN_Y   = 6.0
_ART_SCALE    = 1.04
_ART_GLOW_R   = 180.0     # matches the 180px circle the stylesheet paints


def _approach(cur: float, target: float, rate: float) -> float:
    """Step `cur` a fraction of the way to `target`, snapping once it is close.

    Without the snap a value approaches its target forever and the repaint timer
    never has a reason to stop.
    """
    nxt = cur + (target - cur) * rate
    return target if abs(target - nxt) < _ART_SETTLED else nxt


class _MiniSeek(QWidget):
    seeked = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frac = 0.0
        self._dragging = False
        self.setFixedHeight(14)
        self.setCursor(Qt.PointingHandCursor)

    def set_position(self, frac: float):
        self._frac = max(0.0, min(1.0, frac))
        self.update()

    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0:
            return
        p   = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        r   = 3
        track_h = 4
        ty  = (H - track_h) // 2

        p.setBrush(pal.mid())
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, ty, W, track_h, r, r)

        fill_w = int(self._frac * W)
        if fill_w > 0:
            p.setBrush(pal.highlight())
            p.drawRoundedRect(0, ty, fill_w, track_h, r, r)

        cx = int(self._frac * (W - 1))
        p.setBrush(pal.highlightedText())
        p.drawEllipse(cx - 5, (H - 10) // 2, 10, 10)
        p.end()

    def _pos_frac(self, x) -> float:
        return max(0.0, min(1.0, float(x) / max(1, self.width() - 1)))

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.LeftButton:
            self._dragging = True
            f = self._pos_frac(e.position().x())
            self.set_position(f)
            self.seeked.emit(f)

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._dragging:
            f = self._pos_frac(e.position().x())
            self.set_position(f)
            self.seeked.emit(f)

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
        self._hover  = False
        self._glow   = 0.0                    # 0 away, 1 fully lit
        self._off_x  = 0.0
        self._off_y  = 0.0
        self._scale  = 1.0
        self._t_off_x = 0.0
        self._t_off_y = 0.0
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

    def set_accent(self, palette: dict | None):
        """Tint the glow with the colour taken from the album art."""
        if palette:
            self._accent = QColor(palette.get("accent", palette.get("accent_l", "#ffffff")))
        else:
            self._accent = None
        if self._glow > 0.0:
            self.update()

    # hover
    def enterEvent(self, e):
        self._hover = True
        self._anim.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._t_off_x = self._t_off_y = 0.0
        self._anim.start()
        super().leaveEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent):
        pos = e.position() if hasattr(e, "position") else QPointF(e.pos())
        self._cursor = pos
        W, H = self.width(), self.height()
        if W > 0 and H > 0:
            # -0.5 at one edge and +0.5 at the other, the same cx and cy the
            # site's parallax uses, so the art leans towards the pointer with it
            cx = pos.x() / W - 0.5
            cy = pos.y() / H - 0.5
            self._t_off_x = cx * _ART_LEAN_X
            self._t_off_y = cy * _ART_LEAN_Y
        if not self._anim.isActive():
            self._anim.start()
        self.update()          # the glow follows the pointer with no easing
        super().mouseMoveEvent(e)

    def _step(self):
        rate = _ART_RATE_IN if self._hover else _ART_RATE_OUT
        self._glow  = _approach(self._glow, 1.0 if self._hover else 0.0, rate)
        self._scale = _approach(self._scale, _ART_SCALE if self._hover else 1.0, rate)
        self._off_x = _approach(self._off_x, self._t_off_x, rate)
        self._off_y = _approach(self._off_y, self._t_off_y, rate)
        if not self._hover and self._glow == 0.0 and self._off_x == 0.0 \
                and self._off_y == 0.0 and self._scale == 1.0:
            self._anim.stop()   # fully back at rest, nothing left to repaint for
        self.update()

    def _art_rect(self):
        """Where the art sits before the hover transform, and how big."""
        W, H = self.width(), self.height()
        side = min(W, H) - 16
        return (W - side) // 2, (H - side) // 2, side

    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(0, 0, W, H, self.palette().base())

        # Lean and zoom about the middle of the widget, so the art moves as one
        # piece rather than sliding out of its own frame.
        if self._off_x or self._off_y or self._scale != 1.0:
            p.translate(W / 2.0 + self._off_x, H / 2.0 + self._off_y)
            p.scale(self._scale, self._scale)
            p.translate(-W / 2.0, -H / 2.0)

        if self._pm and not self._pm.isNull():
            side = min(W, H) - 16
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
            side = min(W, H) - 16
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
        self._np_video = QVideoWidget()
        self._np_video.setAspectRatioMode(Qt.KeepAspectRatio)
        self._np_video.setStyleSheet("background: black;")
        self._np_video.hide()

        self._media_stack = QStackedWidget()
        self._media_stack.addWidget(self._art)       # index 0
        self._media_stack.addWidget(self._np_video)  # index 1
        root.addWidget(self._media_stack, 1)

        info = QVBoxLayout()
        info.setSpacing(2)

        self._title = QLabel("No track loaded")
        self._title.setObjectName("track_title")
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setWordWrap(True)

        self._artist = QLabel("")
        self._artist.setObjectName("track_sub")
        self._artist.setAlignment(Qt.AlignCenter)
        self._artist.setWordWrap(True)

        self._album = QLabel("")
        self._album.setObjectName("track_sub")
        self._album.setAlignment(Qt.AlignCenter)
        self._album.setWordWrap(True)

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
    def video_widget(self) -> QVideoWidget:
        return self._np_video

    def _on_track(self, track):
        self._title.setText(track.title or track.path.stem)
        self._artist.setText(track.artist)
        self._album.setText(track.album)
        is_video = track.path.suffix.lower() in VIDEO_EXTS
        self._media_stack.setCurrentIndex(1 if is_video else 0)
        if not is_video:
            self._art.set_pixmap(None)
            threading.Thread(target=self._load_art, args=(track.path,), daemon=True).start()

    def _load_art(self, path: Path):
        data = extract_art(path)
        if _qt_valid(self):
            self._art_ready.emit(data)

    def _apply_art(self, data):
        if data:
            pm = QPixmap()
            pm.loadFromData(data)
            self._art.set_pixmap(pm if not pm.isNull() else None)
        else:
            self._art.set_pixmap(None)

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
