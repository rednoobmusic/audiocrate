from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QPainter, QTextLayout, QTextOption
from PySide6.QtWidgets import QLabel, QSizePolicy


class SteadyLabel(QLabel):
    """A word-wrapped QLabel for text that changes with the song. A QLabel with word wrap asks for the width of its
    longest word and for the height of the glyphs it happens to use, so a title with one long word, or in a script
    that falls back to another font, moved the whole window. This one asks for no width and for one line of its own
    font, whatever it says, and a word wider than the label is broken where it must be instead of running off the
    edge. Text that wraps at spaces is drawn by QLabel itself."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)          # makes the size policy ask for the height by the width
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setTextFormat(Qt.PlainText)

    def _line(self) -> int:
        self.ensurePolished()
        return self.fontMetrics().size(0, "Ag").height()

    def _too_wide(self, width: int) -> bool:
        """Some word is wider than the label, which QLabel would let run past its edge."""
        self.ensurePolished()
        r = self.fontMetrics().boundingRect(0, 0, max(1, width), 100000, int(Qt.TextWordWrap.value), self.text())
        return r.width() > width

    def _lines(self, width: int):
        lay = QTextLayout(self.text().replace("\n", " "), self.font())
        opt = QTextOption(self.alignment() & Qt.AlignHorizontal_Mask)
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        lay.setTextOption(opt)
        lead, y = self.fontMetrics().leading(), 0.0
        lay.beginLayout()
        while (ln := lay.createLine()).isValid():
            ln.setLineWidth(max(1, width))
            y += lead
            ln.setPosition(QPointF(0, y))
            y += ln.height()
        lay.endLayout()
        return lay, max(float(self._line()), y)

    def heightForWidth(self, w: int) -> int:
        if self._too_wide(w):
            return int(self._lines(w)[1] + 0.999)
        return super().heightForWidth(w)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._line())

    def sizeHint(self) -> QSize:
        return QSize(0, self._line())

    def paintEvent(self, e):
        if not self._too_wide(self.width()):
            return super().paintEvent(e)
        lay, h = self._lines(self.width())
        room = self.height() - h
        top = 0 if self.alignment() & Qt.AlignTop else room if self.alignment() & Qt.AlignBottom else room / 2
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        lay.draw(p, QPointF(0, max(0.0, top)))
