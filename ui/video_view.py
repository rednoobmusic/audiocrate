"""The widget that shows the picture of a video.

QVideoWidget lives in QtMultimediaWidgets, which some distributions ship as a part of PySide6 of its own that is easy to
miss (python3-pyside6.qtmultimediawidgets on Debian and Ubuntu). Without it the picture is painted from the frames of a
QVideoSink instead, which asks a little more of the processor and behaves the same.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
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
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def videoSink(self) -> QVideoSink:
        return self._sink

    def setAspectRatioMode(self, mode):
        self._mode = mode
        self.update()

    def _on_frame(self, frame):
        self._image = frame.toImage() if frame.isValid() else QImage()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if not self._image.isNull():
            area = QRect(QPoint(0, 0), self._image.size().scaled(self.size(), self._mode))
            area.moveCenter(self.rect().center())
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(area, self._image)


def make_video_widget() -> QWidget:
    return QVideoWidget() if QVideoWidget is not None else _VideoView()
