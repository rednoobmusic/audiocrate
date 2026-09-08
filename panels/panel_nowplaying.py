from __future__ import annotations
import threading
from pathlib import Path

try:
    from shiboken6 import isValid as _qt_valid
except ImportError:
    def _qt_valid(obj) -> bool:
        return obj is not None

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy, QMenu, QStackedWidget
from PySide6.QtCore    import Qt, Signal
from PySide6.QtGui     import QPixmap, QPainter, QPainterPath, QColor, QMouseEvent
from PySide6.QtMultimediaWidgets import QVideoWidget

from player import Player, extract_art, VIDEO_EXTS


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
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm: QPixmap | None = None
        self._scaled: QPixmap | None = None   # cached per-size scale of _pm
        self._scaled_key = None
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(80, 80)

    def set_pixmap(self, pm: QPixmap | None):
        if pm is not None and not pm.isNull() and max(pm.width(), pm.height()) > 1024:
            # keep a display-sized copy, not the full-resolution original
            pm = pm.scaled(1024, 1024, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._pm = pm
        self._scaled = None
        self._scaled_key = None
        self.update()

    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(0, 0, W, H, self.palette().base())

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

        p.end()


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
