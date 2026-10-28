"""The first run tour: a few pictures of the real app, each with a title and two short sentences.

The slides are in tour/slides.json beside this module, the pictures next to it (see tools/make_tour_images.py,
which writes them). A picture is read when its slide is first shown, on a worker thread, never at startup.
A missing or unreadable picture leaves that slide with its text only; a missing slides.json leaves no
slides at all, which the caller checks (TourDialog.slides) before showing anything.

The dialog is application modal so the app's global playback keys (Space, N, the arrows) do not act on the
player while it is open. It does not stop the music.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

TOUR_VERSION = 1          # the settings key tour_seen holds the last version shown; raise it to show a new tour again
SIZE = QSize(700, 520)    # the size it opens at; it shrinks to about 360 x 420
_RATIO = 16 / 9


def default_slides_dir() -> Path:
    """ui/tour, found from this module so it works from a source tree, an install and a frozen build."""
    return Path(__file__).resolve().parent / "tour"


def load_slides(folder) -> list[dict]:
    """The slides of slides.json as dicts with id, title, text, image and alt, or [] when it is missing or broken."""
    try:
        data = json.loads((Path(folder) / "slides.json").read_text(encoding="utf-8"))
        raw = data["slides"]
    except Exception:
        return []
    out = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        slide = {k: str(s.get(k) or "") for k in ("id", "title", "text", "image", "alt")}
        slide["image"] = Path(slide["image"]).name        # a file beside slides.json, nothing else
        if slide["title"] or slide["text"]:
            out.append(slide)
    return out


def _read(path: Path) -> QImage:
    """Decode one picture (a null image when it cannot be). Called off the GUI thread."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    return reader.read()


class _Loaded(QObject):
    done = Signal(int, QImage)


class _Job(QRunnable):
    def __init__(self, index: int, path: Path, sink: _Loaded):
        super().__init__()
        self._index, self._path, self._sink = index, path, sink

    def run(self):
        img = _read(self._path)
        try:
            self._sink.done.emit(self._index, img)
        except RuntimeError:        # the dialog was closed and deleted while this was decoding
            pass


def _ring(widget: QWidget, painter: QPainter, radius: float):
    """The keyboard focus outline: the themes style the buttons but none of them draws one."""
    pen = QPen(widget.palette().windowText().color(), 2)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(widget.rect().adjusted(1, 1, -1, -1), radius, radius)


class _Button(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._ring = False

    def focusInEvent(self, e):
        self._ring = e.reason() != Qt.MouseFocusReason
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._ring = False
        super().focusOutEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._ring and self.hasFocus():
            p = QPainter(self)
            _ring(self, p, 5)


class _Dot(QAbstractButton):
    """One position marker, a real button: click to jump there."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current = False
        self.setFixedSize(22, 22)
        self.setFocusPolicy(Qt.TabFocus)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fg = QColor(self.palette().windowText().color())
        c = QPoint(self.width() // 2, self.height() // 2)
        if self.current:
            p.setPen(Qt.NoPen)
            p.setBrush(fg)
            p.drawEllipse(c, 5, 5)
        else:
            fg.setAlpha(150 if self.underMouse() else 90)
            p.setPen(QPen(fg, 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, 4, 4)
        if self.hasFocus():
            _ring(self, p, 11)

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()


class _Picture(QWidget):
    """The slide's picture, drawn as large as fits at 16:9 and smooth scaled for the screen's pixel density."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: QImage | None = None
        self._scaled: tuple | None = None       # (pixel size, dpr, image)
        self.setMinimumSize(96, 54)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_image(self, img: QImage | None):
        self._src = img if img is not None and not img.isNull() else None
        self._scaled = None
        self.update()

    def _target(self) -> QRect:
        w, h = self.width(), self.height()
        tw, th = (w, round(w / _RATIO)) if w / max(h, 1) < _RATIO else (round(h * _RATIO), h)
        return QRect((w - tw) // 2, (h - th) // 2, tw, th)

    def paintEvent(self, _):
        t = self._target()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        edge = QColor(self.palette().windowText().color())
        edge.setAlpha(45)
        clip = QPainterPath()
        clip.addRoundedRect(t, 6, 6)
        if self._src is not None:
            dpr = self.devicePixelRatioF()
            px = QSize(round(t.width() * dpr), round(t.height() * dpr))
            if self._scaled is None or self._scaled[:2] != (px, dpr):
                img = self._src.scaled(px, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                img.setDevicePixelRatio(dpr)
                self._scaled = (px, dpr, img)
            p.setClipPath(clip)
            p.drawImage(t.topLeft(), self._scaled[2])
            p.setClipping(False)
        p.setPen(QPen(edge, 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(clip)


class TourDialog(QDialog):
    def __init__(self, parent=None, slides_dir=None):
        super().__init__(parent)
        self.setWindowTitle("AudioCrate tour")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self._dir = Path(slides_dir) if slides_dir is not None else default_slides_dir()
        self.slides = load_slides(self._dir)
        self._images: dict[int, QImage | None] = {}      # slides whose picture has come back (None: unreadable)
        self._asked: set[int] = set()
        self._index = 0
        self._sink = _Loaded(self)
        self._sink.done.connect(self._on_loaded)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 14)
        root.setSpacing(10)
        root.addStretch(0)                      # the two stretches centre the text when a slide has no picture
        self._pic = _Picture()
        root.addWidget(self._pic, 1)
        self._title = QLabel()
        self._title.setObjectName("tour_title")
        self._text = QLabel()
        self._text.setObjectName("tour_text")
        self._title_px = 0
        for lab in (self._title, self._text):
            lab.setStyleSheet("background: transparent;")   # the app sheet gives labels the window colour, a dialog has its own
            lab.setTextFormat(Qt.PlainText)
            lab.setWordWrap(True)
            lab.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            root.addWidget(lab)
        root.addStretch(0)
        self._stretches = (0, root.count() - 1)

        dots = QHBoxLayout()
        dots.setSpacing(0)
        dots.addStretch(1)
        self._dots = []
        for i, s in enumerate(self.slides):
            d = _Dot()
            d.setAccessibleName(f"Slide {i + 1} of {len(self.slides)}")
            d.setToolTip(s["title"] or d.accessibleName())
            d.clicked.connect(lambda _=False, i=i: self.go(i))
            dots.addWidget(d)
            self._dots.append(d)
        dots.addStretch(1)
        root.addLayout(dots)
        for d in self._dots:
            d.setVisible(len(self._dots) > 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._skip = _Button("Skip")
        self._back = _Button("Back")
        self._next = _Button("Next")
        self._next.setDefault(True)
        self._skip.clicked.connect(self.reject)
        self._back.clicked.connect(self.back)
        self._next.clicked.connect(self.next)
        row.addWidget(self._skip)
        row.addStretch(1)
        row.addWidget(self._back)
        row.addWidget(self._next)
        root.addLayout(row)

        # shortcuts rather than key events: a focused button would take the arrows to move focus
        for key, fn in ((Qt.Key_Left, self.back), (Qt.Key_Right, self.next_slide)):
            QShortcut(QKeySequence(key), self, activated=fn)
        order = [self._next, self._back, self._skip, *self._dots]
        for a, b in zip(order, order[1:]):
            QWidget.setTabOrder(a, b)

        self.resize(SIZE)
        self.go(0)
        self._next.setFocus()

    # position

    @property
    def index(self) -> int:
        return self._index

    def go(self, i: int):
        if not self.slides:
            return
        i = max(0, min(len(self.slides) - 1, i))
        self._index = i
        s = self.slides[i]
        self._title.setText(s["title"])
        self._text.setText(s["text"])
        self._pic.setAccessibleName(s["title"])
        self._pic.setAccessibleDescription(s["alt"])
        last = i == len(self.slides) - 1
        self._back.setEnabled(i > 0)
        self._skip.setVisible(not last)
        self._next.setText("Done" if last else "Next")
        for n, d in enumerate(self._dots):
            d.current = n == i
            d.update()
        self._show_picture()
        self._want_picture(i)

    def back(self):
        self.go(self._index - 1)

    def next(self):
        """Next, and Done on the last slide."""
        if self._index >= len(self.slides) - 1:
            self.accept()
        else:
            self.go(self._index + 1)

    def next_slide(self):
        """The right arrow: on, but never closes the dialog."""
        self.go(self._index + 1)

    # pictures

    def _want_picture(self, i: int):
        if i in self._asked:
            return
        self._asked.add(i)
        name = self.slides[i]["image"]
        if not name:
            self._images[i] = None
            self._show_picture()
            return
        QThreadPool.globalInstance().start(_Job(i, self._dir / name, self._sink))

    def _on_loaded(self, i: int, img: QImage):
        self._images[i] = None if img.isNull() else img
        if i == self._index:
            self._show_picture()

    def _show_picture(self):
        """The current slide's picture, an empty frame while it is being read, no frame when it cannot be."""
        i = self._index
        have = i in self._images
        img = self._images.get(i)
        self._pic.set_image(img)
        shown = not have or img is not None
        self._pic.setVisible(shown)
        root = self.layout()
        for slot in self._stretches:
            root.setStretch(slot, 0 if shown else 1)

    # the longest slide decides how tall the text area is, so the picture does not jump between slides

    def _fit_text(self):
        # the title is the text size plus half, bold; a sheet rule because the app's own sheet sets every font size
        px = round(self._text.fontInfo().pixelSize() * 1.5)
        if px != self._title_px:
            self._title_px = px
            self._title.setStyleSheet(f"background: transparent; font-size: {px}px; font-weight: bold;")
        for lab, key in ((self._title, "title"), (self._text, "text")):
            width = max(lab.contentsRect().width(), 1)
            flags = Qt.TextWordWrap | Qt.AlignHCenter
            tall = max((lab.fontMetrics().boundingRect(QRect(0, 0, width, 10_000), flags, s[key]).height()
                        for s in self.slides), default=0)
            lab.setMinimumHeight(tall + 2)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_text()

    def showEvent(self, e):
        super().showEvent(e)
        self._fit_text()
