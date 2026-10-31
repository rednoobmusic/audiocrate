"""AudioCrate's own title bar, for a frameless main window.

The compositor draws its title bar in its own colours, so to match the theme the
window drops it and draws this one: icon, the menus, the title, and the window
buttons. Move and resize are handed back to the compositor through
QWindow.startSystemMove and startSystemResize, so snapping and tiling still work.

The same bar is used for the main window (with its menus), for dialogs
(decorate_dialog) and for floating docks (a TitleBar set as the dock's title bar
widget, see ui/main_window.py).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QMenu, QMenuBar, QSizePolicy, QWidget

HEIGHT = 34
show_line = True   # the thin line under the bar; the "Borders between panels" setting turns it off
aero = False       # the Frutiger Aero style adds a glass shine across the bar
style = "default"  # the style of the theme (core/theme_styles.py), which the bar paints a little of itself
accent = QColor("#8040c8")     # the theme's accent, for the lines of those styles
shade = 0          # the "Frame shading" setting, 0 to 100: a gradient from lighter to darker down the bar
_GRIP = 5        # resize strip along each edge, px
_CORNER = 10     # and the square at each corner
_DRAG_PX = 4     # how far a press must travel before it becomes a move


def _tinted(pm: QPixmap, color: QColor) -> QPixmap:
    """pm with everything that is drawn in it filled with color."""
    out = QPixmap(pm.size())
    out.setDevicePixelRatio(pm.devicePixelRatio())
    out.fill(Qt.transparent)
    q = QPainter(out)
    q.drawPixmap(0, 0, pm)
    q.setCompositionMode(QPainter.CompositionMode_SourceIn)
    q.fillRect(out.rect(), color)
    q.end()
    return out


def bar_background(pal) -> QColor:
    """The strip colour behind title bar buttons: a step off the window colour."""
    bg = QColor(pal.window().color())
    return bg.lighter(112) if bg.lightness() < 128 else bg.darker(106)


class _WinButton(QAbstractButton):
    def __init__(self, kind: str, parent: QWidget):
        super().__init__(parent)
        self._kind = kind
        self.setFixedSize(46, HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        fg = QColor(self.palette().windowText().color())
        if self.underMouse() or self.isDown():
            if self._kind == "close":
                p.fillRect(self.rect(), QColor("#c42b1c" if self.isDown() else "#e5484d"))
                fg = QColor("#ffffff")
            else:
                hover = QColor(fg)
                hover.setAlpha(46 if self.isDown() else 28)
                p.fillRect(self.rect(), hover)
        p.setPen(QPen(fg, 1))
        cx, cy = self.width() // 2, self.height() // 2
        if self._kind == "min":
            p.drawLine(cx - 5, cy, cx + 5, cy)
        elif self._kind == "max":
            if self.window().isMaximized():
                p.drawRect(cx - 5, cy - 3, 7, 7)
                p.drawLine(cx - 3, cy - 5, cx + 5, cy - 5)
                p.drawLine(cx + 5, cy - 5, cx + 5, cy + 3)
            else:
                p.drawRect(cx - 5, cy - 5, 10, 10)
        elif self._kind == "dock":                 # "restore": back into the window
            p.drawRect(cx - 5, cy - 3, 7, 7)
            p.drawLine(cx - 3, cy - 5, cx + 5, cy - 5)
            p.drawLine(cx + 5, cy - 5, cx + 5, cy + 3)
        else:
            p.setRenderHint(QPainter.Antialiasing)
            p.drawLine(cx - 5, cy - 5, cx + 5, cy + 5)
            p.drawLine(cx - 5, cy + 5, cx + 5, cy - 5)


class TitleBar(QWidget):
    def __init__(self, win: QWidget, menu: bool = True,
                 kinds: tuple = ("min", "max", "close"), outline: bool = False):
        super().__init__(win)
        self._win = win
        self._outline = outline                 # a panel's bar also draws the panel's top and sides
        self._press: QPoint | None = None
        self.setFixedHeight(HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addSpacing(HEIGHT)                  # the icon is painted here
        self.menubar = None
        if menu:
            # sized to its menus so no empty stretch of it swallows a press meant to drag
            self.menubar = QMenuBar(self)
            self.menubar.setNativeMenuBar(False)
            self.menubar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            self.menubar.setStyleSheet("QMenuBar { background: transparent; border: none; }")
            lay.addWidget(self.menubar, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        self._buttons = {}
        for kind in kinds:
            b = _WinButton(kind, self)
            self._buttons[kind] = b
            lay.addWidget(b)
        if "min" in self._buttons:
            self._buttons["min"].clicked.connect(win.showMinimized)
        if "max" in self._buttons:
            self._buttons["max"].clicked.connect(self._toggle_max)
        if "dock" in self._buttons:
            self._buttons["dock"].clicked.connect(self._dock)
        self._buttons["close"].clicked.connect(win.close)

        win.installEventFilter(self)
        win.windowTitleChanged.connect(lambda _: self.update())

    def _dock(self):
        # Not inside this bar's own mouse event: docking replaces this very widget, and
        # doing that while its press is still being handled is what left the mouse stuck.
        win = self._win
        QTimer.singleShot(0, lambda: win.setFloating(False))

    def _toggle_max(self):
        if "max" in self._buttons:
            self._win.showNormal() if self._win.isMaximized() else self._win.showMaximized()

    def eventFilter(self, obj, ev):
        if obj is self._win and ev.type() in (QEvent.WindowStateChange, QEvent.ActivationChange):
            self.update()
            if "max" in self._buttons:
                self._buttons["max"].update()      # its glyph follows the window state
        return False

    # drawing

    def paintEvent(self, _):
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), bar_background(pal))
        if shade and not aero:
            tone = QLinearGradient(0, 0, 0, self.height())
            tone.setColorAt(0.0, QColor(255, 255, 255, shade * 4 // 5))
            tone.setColorAt(1.0, QColor(0, 0, 0, shade * 11 // 10))
            p.fillRect(self.rect(), tone)
        if aero:
            glass = QLinearGradient(0, 0, 0, self.height())
            glass.setColorAt(0.0, QColor(255, 255, 255, 80))
            glass.setColorAt(0.5, QColor(255, 255, 255, 18))
            glass.setColorAt(0.5001, QColor(0, 0, 0, 0))
            glass.setColorAt(1.0, QColor(0, 0, 0, 50))
            p.fillRect(self.rect(), glass)
        fg = QColor(pal.windowText().color())
        self._style_touch(p)
        if show_line:
            line = QColor(fg)
            line.setAlpha(36)
            p.setPen(line)
            p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
            if self._outline:
                p.drawLine(0, 0, self.width(), 0)
                p.drawLine(0, 0, 0, self.height())
                p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())

        icon = self._win.windowIcon()
        if not icon.isNull():
            pm = icon.pixmap(18, 18)
            if bar_background(pal).lightness() > 150:       # the logo is white, so on a light bar it takes the text colour
                pm = _tinted(pm, QColor(pal.windowText().color()))
            p.drawPixmap(10, (self.height() - 18) // 2, pm)

        # the title goes in the space between the menus and the buttons
        left = (self.menubar.geometry().right() if self.menubar is not None else HEIGHT) + 12
        right = min(b.geometry().left() for b in self._buttons.values()) - 12
        if right > left:
            fm = self.fontMetrics()
            text = fm.elidedText(self._win.windowTitle(), Qt.ElideRight, right - left)
            fg.setAlpha(210 if self._win.isActiveWindow() else 120)
            p.setPen(fg)
            p.drawText(QRect(left, 0, right - left, self.height()), Qt.AlignCenter, text)

    def _style_touch(self, p: QPainter):
        """What the bar adds in a style of its own: a glow, scan lines, brushed metal, a chunky edge or paper."""
        w, h = self.width(), self.height()
        if style == "terminal":
            for y in range(2, h, 3):
                p.fillRect(0, y, w, 1, QColor(0, 0, 0, 70))
            p.fillRect(0, h - 1, w, 1, accent)
        elif style == "carbon":
            metal = QLinearGradient(0, 0, 0, h)
            metal.setColorAt(0.0, QColor(255, 255, 255, 46))
            metal.setColorAt(0.5, QColor(255, 255, 255, 6))
            metal.setColorAt(1.0, QColor(0, 0, 0, 70))
            p.fillRect(self.rect(), metal)
            p.fillRect(0, 0, w, 1, QColor(255, 255, 255, 60))
        elif style == "chiptune":
            p.fillRect(0, h - 3, w, 3, accent)
            p.fillRect(0, 0, w, 2, QColor(255, 255, 255, 40))
        elif style == "parchment":
            paper = QLinearGradient(0, 0, 0, h)
            paper.setColorAt(0.0, QColor(255, 255, 255, 80))
            paper.setColorAt(1.0, QColor(120, 80, 30, 36))
            p.fillRect(self.rect(), paper)

    # move, maximise, system menu

    # A panel's bar leaves the mouse to the dock itself: Qt drags the panel with the window, shows in the main window where
    # it would dock and docks it on release. A compositor move (startSystemMove) would take the pointer away, so a panel
    # moved that way could never be dropped into the layout.
    def mousePressEvent(self, e):
        if "dock" in self._buttons:
            e.ignore()
            return
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if "dock" in self._buttons:
            e.ignore()
            return
        # A move starts on the first drag, not the press, or the compositor takes
        # the press and a double click never arrives.
        if self._press is not None and e.buttons() & Qt.LeftButton:
            if (e.globalPosition().toPoint() - self._press).manhattanLength() > _DRAG_PX:
                self._press = None
                h = self._win.windowHandle()
                if h is not None:
                    h.startSystemMove()

    def mouseReleaseEvent(self, e):
        if "dock" in self._buttons:
            e.ignore()
            return
        self._press = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = None
            if "dock" in self._buttons:
                self._dock()
            else:
                self._toggle_max()

    def contextMenuEvent(self, e):
        if "dock" in self._buttons:
            self._dock_menu(e.globalPos())
            return
        h = self._win.windowHandle()
        if h is not None and hasattr(h, "showSystemMenu"):
            h.showSystemMenu(e.pos())

    def _dock_menu(self, pos: QPoint):
        """Where a floating panel can go back to, for when dragging it into the layout is not wanted."""
        win, window = self._win, self._win.parent()
        m = QMenu(self)
        m.addAction("Dock", self._dock)
        for label, area in (("Dock to left", Qt.LeftDockWidgetArea), ("Dock to right", Qt.RightDockWidgetArea),
                            ("Dock to top", Qt.TopDockWidgetArea), ("Dock to bottom", Qt.BottomDockWidgetArea)):
            m.addAction(label, lambda a=area: QTimer.singleShot(
                0, lambda: (win.setFloating(False), window.addDockWidget(a, win))))
        m.addSeparator()
        m.addAction("Close", win.close)
        m.exec(pos)


class _Grip(QWidget):
    """An invisible strip on one edge or corner that starts a system resize."""

    def __init__(self, win: QWidget, edges: Qt.Edge, cursor: Qt.CursorShape):
        super().__init__(win)
        self._win = win
        self.edges = edges
        self.setCursor(cursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            h = self._win.windowHandle()
            if h is not None:
                h.startSystemResize(self.edges)


class _Grips(QObject):
    def __init__(self, win: QWidget):
        super().__init__(win)
        self._win = win
        L, T, R, B = Qt.Edge.LeftEdge, Qt.Edge.TopEdge, Qt.Edge.RightEdge, Qt.Edge.BottomEdge
        C = Qt.CursorShape
        self._grips = [
            _Grip(win, T, C.SizeVerCursor), _Grip(win, B, C.SizeVerCursor),
            _Grip(win, L, C.SizeHorCursor), _Grip(win, R, C.SizeHorCursor),
            _Grip(win, T | L, C.SizeFDiagCursor), _Grip(win, B | R, C.SizeFDiagCursor),
            _Grip(win, T | R, C.SizeBDiagCursor), _Grip(win, B | L, C.SizeBDiagCursor),
        ]
        win.installEventFilter(self)
        self._place()

    def eventFilter(self, obj, ev):
        if obj is self._win and ev.type() in (
                QEvent.Resize, QEvent.Show, QEvent.WindowStateChange, QEvent.ChildAdded):
            self._place()
        return False

    def _place(self):
        w = self._win
        W, H = w.width(), w.height()
        on = w.isWindow() and not (w.isMaximized() or w.isFullScreen())
        g, c = _GRIP, _CORNER
        for grip in self._grips:
            e = grip.edges
            horiz = e & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge)
            vert = e & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge)
            x = 0 if e & Qt.Edge.LeftEdge else (W - c if e & Qt.Edge.RightEdge else c)
            y = 0 if e & Qt.Edge.TopEdge else (H - c if e & Qt.Edge.BottomEdge else c)
            if horiz and vert:
                grip.setGeometry(x, y, c, c)
            elif horiz:
                x = 0 if e & Qt.Edge.LeftEdge else W - g
                grip.setGeometry(x, c, g, max(0, H - 2 * c))
            else:
                y = 0 if e & Qt.Edge.TopEdge else H - g
                grip.setGeometry(c, y, max(0, W - 2 * c), g)
            grip.setVisible(on)
            if on:
                grip.raise_()


def install_grips(win: QWidget) -> _Grips:
    return _Grips(win)


class _DialogChrome(QObject):
    """Keeps a dialog's title bar sized to it."""

    def __init__(self, win: QWidget, bar: TitleBar):
        super().__init__(win)
        self._win, self._bar = win, bar
        win.installEventFilter(self)
        self._place()

    def eventFilter(self, obj, ev):
        if obj is self._win and ev.type() in (QEvent.Resize, QEvent.Show):
            self._place()
        return False

    def _place(self):
        self._bar.setGeometry(0, 0, self._win.width(), HEIGHT)
        self._bar.raise_()


def decorate_dialog(win: QWidget) -> None:
    """Give a dialog the themed title bar in place of the compositor's.

    Called as the dialog is first polished, which is before its native window
    exists, so the flag change does not make it blink. The bar is a child laid
    over a strip the dialog's layout leaves empty: the top contents margin grows
    by the bar's height and the layout simply starts below it.
    """
    if win.property("_chromed"):
        return
    win.setProperty("_chromed", True)
    win.setWindowFlag(Qt.FramelessWindowHint, True)
    m = win.contentsMargins()
    win.setContentsMargins(m.left(), m.top() + HEIGHT, m.right(), m.bottom())
    if win.testAttribute(Qt.WA_Resized):
        win.resize(win.width(), win.height() + HEIGHT)
    bar = TitleBar(win, menu=False, kinds=("close",))
    chrome = _DialogChrome(win, bar)
    bar.show()
    fixed = win.minimumSize() == win.maximumSize() and not win.minimumSize().isNull()
    if not fixed:
        install_grips(win)
