"""The widget that shows the picture of a video.

QVideoWidget lives in QtMultimediaWidgets, which some distributions ship as a part of PySide6 of its own that is easy to
miss (python3-pyside6.qtmultimediawidgets on Debian and Ubuntu). Without it the picture is painted from the frames of a
QVideoSink instead, which asks a little more of the processor and behaves the same.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QVideoSink
from PySide6.QtWidgets import QWidget

try:
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:
    QVideoWidget = None


class _VideoView(QWidget):
    plain = True                    # hand the media player the sink, see VideoAudio._route_video

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._image = QImage()
        self._mode = Qt.KeepAspectRatio
        self._ratio = None          # a width to height ratio to show the picture in, whatever its own is
        self._stale = False         # frames came while the widget was hidden and were not converted
        self.native_ratio = None    # the shape a disc says its picture has, when the frames do not (anamorphic video)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMouseTracking(True)

    def videoSink(self) -> QVideoSink:
        return self._sink

    def setAspectRatioMode(self, mode):
        self._mode = mode
        self.update()

    def set_aspect(self, key):
        """"fit", "fill", "stretch", or a ratio as a number."""
        self._ratio = key if isinstance(key, float) else None
        self._mode = {"fill": Qt.KeepAspectRatioByExpanding, "stretch": Qt.IgnoreAspectRatio}.get(key, Qt.KeepAspectRatio)
        self.update()

    def current_image(self):
        """The picture on show as a QImage, None when there is none."""
        frame = self._sink.videoFrame()
        img = frame.toImage() if frame.isValid() else self._image
        return None if img.isNull() else img

    def show_image(self, img):
        """A picture from a player that is not behind the sink (the disc player)."""
        self._image = img
        self.update()

    def _on_frame(self, frame):
        if not self.isVisible():
            self._stale = True      # nobody sees it: no conversion of the frame
            return
        self._image = frame.toImage() if frame.isValid() else QImage()
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self._stale:
            self._stale = False
            self._on_frame(self._sink.videoFrame())

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if not self._image.isNull():
            ratio = self._ratio or self.native_ratio
            src = QSize(int(ratio * 1000), 1000) if ratio else self._image.size()
            area = QRect(QPoint(0, 0), src.scaled(self.size(), self._mode))
            area.moveCenter(self.rect().center())
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(area, self._image)


PaintedVideo = _VideoView


def make_video_widget() -> QWidget:
    return QVideoWidget() if QVideoWidget is not None else _VideoView()
