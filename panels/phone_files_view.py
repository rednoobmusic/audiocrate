"""The Files view of an open iPhone: the media area and the file sharing folder of every app, as in a file manager.

It lists, plays and copies in worker threads, so a slow phone never stops the window. All reading and copying is
done by integrations/phone_files.py. This view only holds where the person is, and does nothing while hidden.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QItemSelectionModel, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QSizePolicy,
                               QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget)

import phone_files as pf
from applog import log
from library_widgets import Scheme, draw_glyph

OBJ, KIND, RIGHT, DROPPABLE = (Qt.UserRole + i for i in range(4))
ROW = 26
OPEN_FIRST = "Open the phone first."
NOT_MUSIC = "That is not a music file. Use Copy to computer to take it off the phone."


def _plural(n: int, word: str) -> str:
    return "%d %s" % (n, word if n == 1 else word + "s")


class _Elide(QLabel):
    """One line of plain text that ends in an ellipsis instead of widening its parent."""

    def __init__(self):
        super().__init__()
        self._full = ""
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)

    def set_full(self, text: str):
        self._full = text
        self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(10, self.width())))


class _Bar(QWidget):
    """A thin progress bar: a fraction, or a moving piece while the end is not known."""

    def __init__(self, sc: Scheme):
        super().__init__()
        self.sc, self.frac = sc, 0.0
        self.setFixedHeight(3)
        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self.update)

    def set(self, frac: float):
        self.frac = frac
        self._sync()
        self.update()

    def _sync(self):
        if self.frac < 0 and self.isVisible():
            self._tick.start()
        else:
            self._tick.stop()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._tick.stop()

    def paintEvent(self, _):
        p, w, h = QPainter(self), self.width(), self.height()
        p.fillRect(0, 0, w, h, self.sc.line)
        if self.frac < 0:
            seg = max(24, w // 4)
            x = int((time.perf_counter() * 0.9 % 1.0) * (w + seg)) - seg
            p.fillRect(max(0, x), 0, min(w, x + seg) - max(0, x), h, self.sc.accent)
        else:
            p.fillRect(0, 0, int(w * min(1.0, self.frac)), h, self.sc.accent)


class _Strip(QWidget):
    """What a copy is doing: a line of text, a bar and Cancel. When it ends it keeps its sentence until Dismiss."""
    pressed = Signal()

    def __init__(self, sc: Scheme):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 5, 8, 6)
        v.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.text = _Elide()
        self.button = QPushButton("Cancel")
        self.button.clicked.connect(self.pressed)
        row.addWidget(self.text, 1)
        row.addWidget(self.button)
        v.addLayout(row)
        self.bar = _Bar(sc)
        v.addWidget(self.bar)
        self.hide()

    def begin(self, text: str, frac: float = 0.0):
        self.setToolTip("")
        self.button.setText("Cancel")
        self.button.setEnabled(True)
        self.bar.show()
        self.set(text, frac)
        self.show()

    def set(self, text: str, frac: float):
        self.text.set_full(text)
        self.bar.set(frac)

    def finish(self, text: str, tip: str = ""):
        self.button.setText("Dismiss")
        self.bar.hide()
        self.text.set_full(text)
        self.setToolTip(tip)
        self.show()


class _State(QWidget):
    """The page for loading, empty and error: a title, a line of help and perhaps one button."""
    action = Signal()

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(6)
        self.title, self.hint = QLabel(), QLabel()
        f = QFont(self.title.font())
        f.setBold(True)
        self.title.setFont(f)
        self.hint.setObjectName("track_sub")
        for lab in (self.title, self.hint):
            lab.setAlignment(Qt.AlignCenter)
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.PlainText)
        self.button = QPushButton()
        self.button.clicked.connect(self.action)
        v.addStretch(1)
        v.addWidget(self.title)
        v.addWidget(self.hint)
        v.addWidget(self.button, 0, Qt.AlignHCenter)
        v.addStretch(1)

    def show_message(self, title: str, hint: str = "", button: str = ""):
        self.title.setText(title)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.button.setText(button)
        self.button.setVisible(bool(button))


class _Crumbs(QWidget):
    """The path as clickable pieces. When it is too long for the width the first pieces become '...'."""
    picked = Signal(int)
    PAD, GAP = 6, 12

    def __init__(self, sc: Scheme):
        super().__init__()
        self.sc, self.labels, self._hits, self._hot = sc, [], [], -2
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_labels(self, labels: list[str]):
        self.labels = list(labels)
        self.setToolTip(" > ".join(labels))
        self.update()

    def sizeHint(self):
        return QSize(120, self.fontMetrics().height() + 12)

    def minimumSizeHint(self):
        return QSize(40, self.sizeHint().height())

    def _layout(self) -> list[tuple[int, QRect, str]]:
        """(index to go to or -1 for the current one, where, text). The '...' goes to the piece before the first shown."""
        fm, n, h = self.fontMetrics(), len(self.labels), self.height()
        if not n:
            return []
        widths = [fm.horizontalAdvance(t) + 2 * self.PAD for t in self.labels]
        lead = fm.horizontalAdvance("...") + 2 * self.PAD + self.GAP
        avail, first, used = self.width() - 8, n, 0
        while first > 0:
            w = widths[first - 1] + (self.GAP if first < n else 0)
            if first < n and used + w + (lead if first > 1 else 0) > avail:
                break
            used += w
            first -= 1
        out, x = [], 4
        if first > 0:
            out.append((first - 1, QRect(x, 0, lead - self.GAP, h), "..."))
            x += lead
        for i in range(first, n):
            w = widths[i] if i < n - 1 else min(widths[i], max(30, self.width() - 4 - x))
            text = self.labels[i] if w == widths[i] else fm.elidedText(self.labels[i], Qt.ElideMiddle, w - 2 * self.PAD)
            out.append((i if i < n - 1 else -1, QRect(x, 0, w, h), text))
            x += w + self.GAP
        return out

    def paintEvent(self, _):
        p, sc = QPainter(self), self.sc
        self._hits = self._layout()
        last = len(self._hits) - 1
        for k, (target, r, text) in enumerate(self._hits):
            hot = target >= 0 and target == self._hot
            f = QFont(self.font())
            f.setUnderline(hot)
            p.setFont(f)
            p.setPen(sc.fg if k == last or hot else sc.dim)
            p.drawText(r, Qt.AlignCenter, text)
            if k < last:
                draw_glyph(p, "right", QRectF(r.right() + 1, (self.height() - 10) / 2, self.GAP - 2, 10), sc.dimmer)

    def _at(self, pos) -> int:
        self._hits = self._layout()
        return next((t for t, r, _ in self._hits if t >= 0 and r.contains(pos)), -2)

    def mouseMoveEvent(self, e):
        hot = self._at(e.position().toPoint())
        if hot != self._hot:
            self._hot = hot
            self.setCursor(Qt.PointingHandCursor if hot >= 0 else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hot = -2
        self.update()

    def mouseReleaseEvent(self, e):
        t = self._at(e.position().toPoint())
        if t >= 0 and e.button() == Qt.LeftButton:
            self.picked.emit(t)


class _Rows(QStyledItemDelegate):
    """A row is an icon, a name and, on the right, a size or a line of detail that is dropped when there is no room."""

    def sizeHint(self, option, index):
        h = option.fontMetrics.height() + 10
        return QSize(0, max(ROW, h) if index.data(KIND) != "header" else max(22, h - 4))

    def paint(self, p, option, index):
        kind, right = index.data(KIND), index.data(RIGHT) or ""
        if kind == "header":
            p.save()
            f = QFont(option.font)
            f.setBold(True)
            p.setFont(f)
            c = QColor(option.palette.color(QPalette.Text))
            c.setAlpha(150)
            p.setPen(c)
            p.drawText(option.rect.adjusted(8, 6, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, index.data(Qt.DisplayRole))
            p.restore()
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        box = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget)
        fm = opt.fontMetrics
        rw = fm.horizontalAdvance(right) + 10 if right else 0
        if right and index.data(DROPPABLE) and option.rect.width() < 400:     # the same rows all alike
            right, rw = "", 0
        opt.text = fm.elidedText(opt.text, Qt.ElideMiddle, max(10, box.width() - rw))
        style.drawControl(QStyle.CE_ItemViewItem, opt, p, opt.widget)
        if right:
            p.save()
            c = QColor(opt.palette.color(QPalette.Text))
            c.setAlpha(150)
            p.setPen(c)
            p.drawText(QRect(box.right() - rw + 10, box.top(), rw - 10, box.height()),
                       Qt.AlignRight | Qt.AlignVCenter, right)
            p.restore()


class _List(QListWidget):
    opened = Signal()
    up = Signal()
    refresh = Signal()
    typed = Signal(str)

    def keyPressEvent(self, e):
        k, mods = e.key(), e.modifiers()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self.opened.emit()
        elif k == Qt.Key_Backspace:
            self.up.emit()
        elif k == Qt.Key_F5:
            self.refresh.emit()
        elif e.text() and e.text().isprintable() and not mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            self.typed.emit(e.text())
        else:
            super().keyPressEvent(e)


class _Job:
    def __init__(self, jid: int, kind: str, **extra):
        self.id, self.kind, self.extra = jid, kind, extra
        self.cancel = threading.Event()
        self.last = 0.0


class PhoneFilesView(QWidget):
    add_to_playlist = Signal(list)          # list[Path], files on the phone's mount
    play_now = Signal(list)                 # list[Path]
    library_changed = Signal(object)        # the folder on this computer that gained songs
    status = Signal(str)
    _places_read = Signal(int, object, str)  # request, (places, notes), an error sentence
    _dir_read = Signal(int, object, str)     # request, entries, an error sentence
    _progressed = Signal(int, int, int, str)
    _finished = Signal(int, object)

    def __init__(self, device_id: str, name: str, parent=None, run=None, gvfs=None):
        super().__init__(parent)
        self._udid, self._name = device_id, name or "iPhone"
        self._run, self._gvfs = run, gvfs               # the command runner and the FUSE root, for tests
        self._media: Path | None = None
        self._apps: Path | None = None
        self._apps_seen: Path | None = None             # where the listed apps are, once they are
        self._dirty = False                             # a mount was set that has not been read yet
        self._places: list | None = None
        self._notes: list[str] = []
        self._apps_pending = False
        self._place: pf.Place | None = None             # None is the top, where the places are
        self._cwd: Path | None = None
        self._entries: list | None = None
        self._pending = False
        self._error = ""
        self._gen_places = self._gen_dir = 0            # the newest request of each kind, older answers are dropped
        self._places_busy = False
        self._keep: set[str] = set()                    # names to select again after a refresh
        self._scroll_apps = False
        self._threads: list[threading.Thread] = []
        self._ids = itertools.count(1)
        self._job: _Job | None = None
        self._closed = False
        self._tracks = pf.PhoneTracks()
        self._dest = self._pick_home("Music")
        self._src_dir = Path.home()
        self._library_dir = Path.home() / "Music"
        self._icons: dict[str, QIcon] = {}
        self._targets: list = []
        self._menu_obj: QMenu | None = None
        self._counts = (0, 0)                           # what the list holds, and what the filter lets through

        self._list = _List()                            # made first, so that it is the one with the focus at the start
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter this folder")
        self._filter.setClearButtonEnabled(True)
        self._filter.setFocusPolicy(Qt.ClickFocus)       # the list has the focus, and typing there goes to the filter
        self._sc = Scheme(self, self._filter)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._crumbs = _Crumbs(self._sc)
        self._crumbs.picked.connect(self._crumb)
        root.addWidget(self._crumbs)
        row = QHBoxLayout()
        row.setContentsMargins(6, 0, 6, 4)
        row.addWidget(self._filter)
        root.addLayout(row)
        self._pages = QStackedWidget()
        root.addWidget(self._pages, 1)
        self.setFocusProxy(self._list)
        self._list.setFrameShape(QListWidget.NoFrame)
        self._list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setTextElideMode(Qt.ElideMiddle)
        self._list.setIconSize(QSize(16, 16))
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.setItemDelegate(_Rows(self._list))
        self._list.itemDoubleClicked.connect(self._open_item)
        self._list.customContextMenuRequested.connect(self._menu)
        self._list.opened.connect(self._open_current)
        self._list.up.connect(self._go_up)
        self._list.refresh.connect(self.refresh)
        self._list.typed.connect(self._type)
        self._list.itemSelectionChanged.connect(self._sync_count)
        self._state = _State()
        self._state.action.connect(self.refresh)
        self._pages.addWidget(self._list)
        self._pages.addWidget(self._state)
        self._count = QLabel()
        self._count.setObjectName("track_sub")
        self._count.setContentsMargins(8, 2, 8, 4)
        root.addWidget(self._count)
        self._strip = _Strip(self._sc)
        self._strip.pressed.connect(self._strip_pressed)
        root.addWidget(self._strip)

        self._late = self._timer(150, self._slow)
        self._filter_timer = self._timer(120, self._fill)
        self._tidy = self._timer(10000, self._strip.hide)
        self._restyle_timer = self._timer(0, self._restyle)
        self._filter.textChanged.connect(lambda _: self._filter_timer.start())
        self._filter.returnPressed.connect(self._open_first)
        self._filter.installEventFilter(self)
        self._places_read.connect(self._on_places)
        self._dir_read.connect(self._on_dir)
        self._progressed.connect(self._on_progress)
        self._finished.connect(self._on_finished)
        self._restyle()
        self._show_state(OPEN_FIRST)

    # small helpers

    @staticmethod
    def _pick_home(sub: str) -> Path:
        p = Path.home() / sub
        return p if p.is_dir() else Path.home()

    def _timer(self, ms: int, fn) -> QTimer:
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(fn)
        return t

    def _spawn(self, name: str, fn, *args):
        self._threads = [t for t in self._threads if t.is_alive()]
        t = threading.Thread(target=fn, args=args, name="phonefiles-" + name, daemon=True)
        self._threads.append(t)
        t.start()

    @staticmethod
    def _post(sig, *args):
        try:
            sig.emit(*args)
        except RuntimeError:                # the view was deleted while a thread was finishing
            pass

    def minimumSizeHint(self):
        return QSize(160, 120)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._count.setVisible(self.height() >= 190)

    # public

    def set_mount(self, media_path, apps_path=None):
        """The folders GVfs made for the phone. The apps folder may be None: it is looked for, and opened, when the
        view is first shown. Setting the same folders again changes nothing."""
        media = Path(media_path) if media_path else None
        apps = Path(apps_path) if apps_path else None
        if (media, apps) == (self._media, self._apps):
            return
        self._media, self._apps, self._apps_seen = media, apps, None
        self._gen_places += 1
        self._gen_dir += 1
        self._places, self._notes, self._place, self._cwd = None, [], None, None
        self._places_busy = False
        self._entries, self._pending, self._error = None, False, ""
        self._dirty = media is not None
        if media is None:
            self._show_state(OPEN_FIRST)
        elif self.isVisible():
            self._begin()

    def set_name(self, name: str):
        if (name or "iPhone") == self._name:
            return
        self._name = name or "iPhone"
        self._crumbs.set_labels([lab for lab, _ in self._crumb_model()])

    def set_library_folder(self, folder):
        """Where Add to library puts the songs, in a folder named after the phone."""
        self._library_dir = Path(folder)

    def busy(self) -> bool:
        return self._job is not None

    def apps_root(self) -> Path | None:
        """The apps folder on the mount, once it is known. Nothing is read to tell."""
        return self._apps or self._apps_seen

    def refresh(self):
        self._keep = {e.name for e in self._selected()}
        if self._media is None:
            return
        if self._place is None:
            self._request_places(quick=False)
        else:
            self._request_dir()

    def shutdown(self):
        """Stop what is running and wait for the threads. A read that is stuck in the file system cannot be told
        to stop, so the wait is short and those threads are left to end on their own."""
        self._closed = True
        for t in (self._late, self._filter_timer, self._tidy, self._restyle_timer):
            t.stop()
        if self._job is not None:
            self._job.cancel.set()
        for t in list(self._threads):
            t.join(3.0)
        self._threads = [t for t in self._threads if t.is_alive()]

    # look

    def changeEvent(self, e):
        if e.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange, QEvent.Type.FontChange,
                        QEvent.Type.ApplicationPaletteChange):
            self._restyle_timer.start()
        super().changeEvent(e)

    def _restyle(self):
        self._sc.refresh()
        self._icons = {}
        for kind in ("dir", "media", "app", "file", "audio", "link", "note"):
            self._icons[kind] = self._make_icon(kind)
        for i in range(self._list.count()):
            it = self._list.item(i)
            it.setIcon(self._icons.get(it.data(KIND), QIcon()))
        self._crumbs.update()
        self._strip.bar.update()
        self._list.viewport().update()

    def _make_icon(self, kind: str) -> QIcon:
        sc, dpr = self._sc, self.devicePixelRatioF()
        pm = QPixmap(int(16 * dpr), int(16 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        r = QRectF(0, 0, 16, 16)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(sc.dim, 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if kind in ("dir", "media"):
            draw_glyph(p, "folder", r, sc.folder, sc.folder_fill)
        elif kind == "audio":
            draw_glyph(p, "note", r, sc.accent)
        elif kind == "app":
            p.setBrush(sc.folder_fill)
            p.setPen(QPen(sc.folder, 1.3))
            p.drawRoundedRect(QRectF(2.5, 2.5, 11, 11), 3, 3)
            p.drawEllipse(QRectF(6, 6, 4, 4))
        elif kind == "note":
            draw_glyph(p, "disc", r, sc.dimmer)
        else:                               # a file, or a link which has an arrow on it
            path = QPainterPath()
            path.moveTo(3.5, 1.8)
            path.lineTo(9.5, 1.8)
            path.lineTo(12.5, 4.8)
            path.lineTo(12.5, 14.2)
            path.lineTo(3.5, 14.2)
            path.closeSubpath()
            p.drawPath(path)
            p.drawLine(QPointF(9.5, 1.8), QPointF(9.5, 4.8))
            p.drawLine(QPointF(9.5, 4.8), QPointF(12.5, 4.8))
            if kind == "link":
                p.setPen(QPen(sc.accent, 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.drawLine(QPointF(5.5, 11.5), QPointF(10, 7))
                p.drawLine(QPointF(6.8, 7), QPointF(10, 7))
                p.drawLine(QPointF(10, 7), QPointF(10, 10.2))
        p.end()
        return QIcon(pm)

    # showing and hiding

    def showEvent(self, e):
        super().showEvent(e)
        if self._dirty and self._media is not None:
            self._begin()
        elif self._pending and self._entries is None:
            self._slow()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._late.stop()
        self._filter_timer.stop()

    def eventFilter(self, obj, e):
        if obj is self._filter and e.type() == QEvent.Type.KeyPress:
            if e.key() == Qt.Key_Escape and self._filter.text():
                self._filter.clear()
                return True
            if e.key() == Qt.Key_Down and self._list.count():
                self._list.setFocus()
                return True
        return super().eventFilter(obj, e)

    # where the person is

    def _begin(self):
        self._dirty = False
        self._place = self._cwd = None
        self._request_places(quick=True)

    def _crumb_model(self) -> list:
        items = [(self._name, ("places", False))]
        pl = self._place
        if pl is None or self._cwd is None:
            return items
        if pl.kind == "media":
            items.append(("Media", ("dir", pl, pl.path)))
        else:
            items += [("Apps", ("places", True)), (pl.title, ("dir", pl, pl.path))]
        here = pl.path
        try:
            parts = self._cwd.relative_to(pl.path).parts
        except ValueError:
            parts = ()
        for part in parts:
            here = here / part
            items.append((part, ("dir", pl, here)))
        return items

    def _sync_crumbs(self):
        model = self._crumb_model()
        self._targets = [t for _, t in model]
        self._crumbs.set_labels([lab for lab, _ in model])

    def _crumb(self, i: int):
        if not 0 <= i < len(self._targets):
            return
        t = self._targets[i]
        if t[0] == "places":
            self._go_places(t[1])
        else:
            self._go(t[1], t[2])

    def _clear_filter(self):
        self._filter_timer.stop()
        self._filter.blockSignals(True)
        self._filter.clear()
        self._filter.blockSignals(False)

    def _go_places(self, scroll_apps: bool = False):
        self._place = self._cwd = None
        self._scroll_apps = scroll_apps
        self._clear_filter()
        self._gen_dir += 1                  # a folder still being read is no longer wanted
        self._pending = False
        self._sync_crumbs()
        if self._places is not None:
            self._entries, self._error = list(self._places), ""
            self._fill()
        else:
            self._request_places(quick=True)

    def _go(self, place: pf.Place, path: Path, select: str = ""):
        self._place, self._cwd = place, Path(path)
        self._keep = {select} if select else set()
        self._clear_filter()
        self._sync_crumbs()
        self._request_dir()

    def _go_up(self):
        if self._place is None or self._cwd is None:
            return
        if self._cwd == self._place.path:
            self._keep = {self._place.title}
            self._go_places()
        else:
            self._go(self._place, self._cwd.parent, select=self._cwd.name)

    # reading

    def _request_places(self, quick: bool):
        """Read the media area and the apps. The media area needs no reading, so with quick it shows at once and the
        apps join it when they are found, which can take a while when the apps folder has to be opened first."""
        if self._media is None:
            return
        self._error = ""
        self._sync_crumbs()
        if quick:
            self._places = None
            self._entries, self._notes = [pf.media_place(self._media)], []
            self._apps_pending = True
            self._pending = False
            self._fill()
        else:
            self._pending = True
            self._late.start()
        if self._places_busy:               # the answer to the request in flight will fill it in
            return
        self._gen_places += 1
        self._places_busy = True
        self._spawn("places", self._read_places, self._gen_places, self._media, self._apps)

    def _read_places(self, gen: int, media, apps):
        try:
            res = pf.list_places(self._udid, run=self._run, gvfs=self._gvfs, media=media, apps=apps)
            self._post(self._places_read, gen, res, "")
        except pf.PhoneError as e:
            self._post(self._places_read, gen, None, str(e))
        except OSError as e:
            self._post(self._places_read, gen, None, "The phone could not be read. %s" % pf.os_message(e))
        except Exception:
            log.exception("listing the places of %s", self._udid)
            self._post(self._places_read, gen, None, "Something went wrong while reading the phone.")

    def _request_dir(self):
        self._gen_dir += 1
        self._pending, self._error = True, ""
        self._late.start()
        self._spawn("list", self._read_dir, self._gen_dir, self._place, self._cwd)

    def _read_dir(self, gen: int, place: pf.Place, path: Path):
        try:
            res = pf.list_dir(path, root=place.path, media=place.kind == "media" and path == place.path,
                              run=self._run, gvfs=self._gvfs)
            self._post(self._dir_read, gen, res, "")
        except pf.PhoneError as e:
            self._post(self._dir_read, gen, None, str(e))
        except Exception:
            log.exception("listing %s", path)
            self._post(self._dir_read, gen, None, "Something went wrong while reading the phone.")

    def _on_places(self, gen: int, res, err: str):
        if gen != self._gen_places or self._closed:
            return
        self._places_busy = False
        self._apps_pending = False
        if err:
            self._places = None
        else:
            self._places, self._notes = res
            self._apps_seen = next((p.path.parent for p in self._places if p.kind == "app"), None)
            for note in self._notes:
                self.status.emit(note)
        if self._place is None:             # the top is showing: it shows what came
            self._late.stop()
            self._pending = False
            self._error = err
            self._entries = None if err else list(self._places)
            self._fill()

    def _on_dir(self, gen: int, res, err: str):
        if gen != self._gen_dir or self._closed:
            return
        self._late.stop()
        self._pending = False
        self._error = err
        self._entries = None if err else res
        self._fill()

    def _slow(self):
        """A read is taking a moment: say so, once there is nothing else to look at."""
        if self._pending and not self._closed:
            self._entries = None
            self._fill()

    # filling the list

    def _add(self, text: str, kind: str, obj=None, right: str = "", tip: str = "", droppable: bool = False,
             selectable: bool = True) -> QListWidgetItem:
        it = QListWidgetItem(self._icons.get(kind, QIcon()), text)
        it.setData(OBJ, obj)
        it.setData(KIND, kind)
        it.setData(RIGHT, right)
        it.setData(DROPPABLE, droppable)
        if tip:
            it.setToolTip(tip)
        if not selectable:
            it.setFlags(Qt.NoItemFlags)
        self._list.addItem(it)
        return it

    def _kind_of(self, e: pf.Entry) -> str:
        if e.link:
            return "link"
        if e.is_dir:
            return "dir"
        return "audio" if self._tracks.is_audio(e.name) else "file"

    def _tip_of(self, e: pf.Entry) -> str:
        if self._place is not None and self._place.kind == "media" and self._cwd == self._place.path:
            note = pf.MEDIA_NOTES.get(e.name)
            if note:
                return "%s\n%s" % (e.name, note)
        lines = [e.name]
        if e.link:
            lines.append("A link. It is shown but not followed.")
        elif not e.is_dir:
            lines.append(pf.format_size(e.size))
        if e.mtime:
            lines.append("Changed " + time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime)))
        return "\n".join(lines)

    def _fill(self):
        """Put what was read in the list, narrowed by the filter, or show why there is nothing to list."""
        if self._closed:
            return
        q = self._filter.text().strip().casefold()
        lst = self._list
        keep = self._keep or {e.name for e in self._selected()}
        self._keep = set()
        lst.setUpdatesEnabled(False)
        lst.clear()
        header = None
        total = shown = 0
        if self._error:
            lst.setUpdatesEnabled(True)
            self._show_state("The phone could not be read", self._error + " Check that the phone is unlocked and "
                             "plugged in.", "Try again")
            return
        if self._entries is None:
            lst.setUpdatesEnabled(True)
            self._show_state("Reading the phone" if self._place is None else "Reading the folder")
            return
        if self._place is None:
            apps = [p for p in self._entries if p.kind == "app"]
            total = len(apps)
            for p in self._entries:
                if p.kind == "media" and (not q or q in p.title.casefold()):
                    self._add(p.title, "media", p, p.subtitle, "%s\n%s" % (p.title, p.subtitle), droppable=True)
            matching = [p for p in apps if not q or q in p.title.casefold() or q in p.subtitle.casefold()]
            if matching or (not q and (self._notes or self._apps_pending)):
                header = self._add("Apps", "header", None, selectable=False)
            shown = len(matching)
            for p in matching:
                self._add(p.title, "app", p, p.subtitle, "%s\n%s" % (p.title, p.subtitle), droppable=True)
            if not q:
                for note in self._notes:
                    self._add("Could not open the apps. Double click to try again.", "note", note, tip=note)
                if self._apps_pending:
                    self._add("Looking for the apps", "note", None, selectable=False)
        else:
            total = len(self._entries)
            for e in self._entries:
                if q and q not in e.name.casefold():
                    continue
                kind = self._kind_of(e)
                self._add(e.name, kind, e, "" if e.is_dir or e.link else pf.format_size(e.size), self._tip_of(e))
                shown += 1
        again = None
        for i in range(lst.count()):
            it = lst.item(i)
            obj = it.data(OBJ)
            if it.flags() & Qt.ItemIsSelectable and (getattr(obj, "name", None) or getattr(obj, "title", None)) in keep:
                it.setSelected(True)
                again = again or it
        if lst.count() and not lst.currentItem():
            first = next((i for i in range(lst.count()) if lst.item(i).flags() & Qt.ItemIsSelectable), 0)
            lst.setCurrentRow(first, QItemSelectionModel.NoUpdate)
        lst.setUpdatesEnabled(True)
        if self._place is None and self._scroll_apps and header is not None:
            lst.scrollToItem(header, QAbstractItemView.PositionAtTop)
        elif again is not None:
            lst.scrollToItem(again)
        self._scroll_apps = False
        if lst.count() and (shown or self._place is None):
            self._pages.setCurrentWidget(lst)
        elif q:
            self._show_state("Nothing matches “%s”" % self._filter.text().strip(), "Clear the filter to see "
                             "everything in this folder.")
        elif self._place is not None and self._place.kind == "app" and self._cwd == self._place.path:
            self._show_state("This app does not share any files.", "Its shared folder is empty.")
        else:
            self._show_state("Nothing here.", "This folder is read only."
                             if self._place is not None and self._place.writable is False else "")
        self._counts = (total, shown)
        self._sync_count()

    def _show_state(self, title: str, hint: str = "", button: str = ""):
        self._state.show_message(title, hint, button)
        self._pages.setCurrentWidget(self._state)
        self._count.setText("")

    def _sync_count(self):
        if self._pages.currentWidget() is not self._list:
            return
        total, shown = self._counts
        word = "app" if self._place is None else "item"
        text = _plural(total, word) if total == shown else "%d of %s" % (shown, _plural(total, word))
        n = len(self._list.selectedItems())
        if n > 1:
            text += ", %d selected" % n
        if self._place is not None and self._place.writable is False:
            text += ", read only"
        self._count.setText(text)

    def _type(self, text: str):
        self._filter.setFocus()
        self._filter.setText(self._filter.text() + text)

    # what is selected

    def _selected(self) -> list:
        """The selected files and folders in the order of the list."""
        out = []
        for i in range(self._list.count()):
            it = self._list.item(i)
            obj = it.data(OBJ)
            if it.isSelected() and isinstance(obj, pf.Entry):
                out.append(obj)
        return out

    def _selected_places(self) -> list:
        return [it.data(OBJ) for i in range(self._list.count()) for it in [self._list.item(i)]
                if it.isSelected() and isinstance(it.data(OBJ), pf.Place)]

    # opening

    def _open_current(self):
        it = self._list.currentItem()
        if it is not None:
            self._open_item(it)

    def _open_first(self):
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it.flags() & Qt.ItemIsSelectable:
                self._open_item(it)
                return

    def _open_item(self, it: QListWidgetItem):
        if self._pending and self._entries is not None:
            return
        obj, kind = it.data(OBJ), it.data(KIND)
        if isinstance(obj, pf.Place):
            self._go(obj, obj.path)
        elif kind == "note" and isinstance(obj, str):
            self.refresh()
        elif kind == "dir":
            self._go(self._place, self._cwd / obj.name)
        elif kind == "audio":
            self._tracks_of("play", [obj])
        elif kind == "link":
            self.status.emit("%s is a link. Links are shown but not followed." % obj.name)
        elif kind == "file":
            self.status.emit(NOT_MUSIC)

    # playing

    def _tracks_of(self, what: str, entries: list):
        """Play or queue the songs in entries. Folders are searched in a thread, since a phone is slow to list."""
        if not entries:
            return
        if not any(e.is_dir for e in entries):
            self._deliver(what, [e.path for e in entries if not e.link and self._tracks.is_audio(e.name)], False,
                          entries)
            return
        self._start("find", "Looking for music", lambda job: self._tracks.collect(entries, job.cancel), what=what,
                    entries=entries)

    def _deliver(self, what: str, paths: list, cut: bool, entries: list):
        if not paths:
            if len(entries) == 1 and not entries[0].is_dir:
                self.status.emit(NOT_MUSIC)
            else:
                self.status.emit("There is no music in %s." % (entries[0].name if len(entries) == 1
                                                                else "what is selected"))
            return
        if what == "play":
            self.play_now.emit(paths)
            msg = "Playing " + (paths[0].name if len(paths) == 1 else _plural(len(paths), "song"))
        else:
            self.add_to_playlist.emit(paths)
            msg = "Added %s to the playlist." % _plural(len(paths), "song")
        self.status.emit(msg + (" Only the first %d were found." % pf.MAX_TRACKS if cut else ""))

    # copying

    def _start(self, kind: str, text: str, work, **extra) -> bool:
        if self._job is not None:
            self.status.emit("Wait for the copy that is running, or cancel it first.")
            return False
        job = self._job = _Job(next(self._ids), kind, **extra)
        self._strip.begin(text, -1 if kind == "find" else 0.0)
        self._tidy.stop()

        def run():
            try:
                res = work(job)
            except Exception:
                log.exception("phone files job %s", kind)
                res = ([], False) if kind == "find" else pf.CopyResult(errors=["Something went wrong while copying."])
            self._post(self._finished, job.id, res)
        self._spawn("job", run)
        return True

    def _progress_cb(self, job: _Job):
        def tick(done: int, total: int, name: str):
            now = time.monotonic()
            if now - job.last >= 0.1 or (total and done >= total):
                job.last = now
                self._post(self._progressed, job.id, done, total, name)
        return tick

    def _on_progress(self, jid: int, done: int, total: int, name: str):
        job = self._job
        if job is None or job.id != jid or self._closed:
            return
        verb = "Copying to the phone" if job.kind == "in" else "Copying"
        self._strip.set("%s %s" % (verb, name) if name else verb, done / total if total else -1)

    def _on_finished(self, jid: int, res):
        job = self._job
        if job is None or job.id != jid or self._closed:
            return
        self._job = None
        if job.kind == "find":
            self._strip.hide()
            paths, cut = res
            if job.cancel.is_set():
                self.status.emit("Cancelled.")
            else:
                self._deliver(job.extra["what"], paths, cut, job.extra["entries"])
            return
        text = res.summary()
        clean = not res.errors and not res.cancelled
        self._strip.finish(text, "\n".join(res.errors))
        if clean:
            self._tidy.start()
        self.status.emit(text)
        if job.kind == "in":
            pl = job.extra["place"]
            if res.read_only:
                pl.writable = False
            elif res.files:
                pl.writable = True
            if self._place is pl:
                self.refresh()
        elif res.files:
            self.library_changed.emit(res.dest)

    def _strip_pressed(self):
        if self._job is not None:
            self._job.cancel.set()
            self._strip.button.setEnabled(False)
            self._strip.set("Cancelling", -1)
        else:
            self._tidy.stop()
            self._strip.hide()
            self._strip.button.setEnabled(True)

    def _copy_to_computer(self, entries: list, library: bool = False):
        if not entries or self._place is None:
            return
        place = self._place
        if library:
            dest = self._library_dir / (re.sub(r'[\\/:*?"<>|]', "_", self._name).strip(" .") or "iPhone")
        else:
            d = QFileDialog.getExistingDirectory(self, "Copy to this computer", str(self._dest))
            if not d:
                return
            dest = self._dest = Path(d)

        def work(job):
            if library:
                try:
                    dest.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    return pf.CopyResult(dest=dest, errors=["The folder %s could not be made. %s"
                                                            % (dest.name, pf.os_message(e, "pc_write"))])
            return pf.copy_out(entries, dest, self._progress_cb(job), job.cancel, root=place.path,
                               only=self._tracks.is_audio if library else None)
        self._start("library" if library else "out", "Copying", work)

    def _ask_replace(self, names: list[str]):
        """'replace', 'skip' or None for cancel, when files of these names are in the folder already."""
        one = len(names) == 1
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Already on the phone")
        box.setText("%s in this folder already. Replace %s?" % (
            "%s is" % names[0] if one else "%d of these files are" % len(names), "it" if one else "them"))
        replace = box.addButton("Replace", QMessageBox.AcceptRole)
        skip = box.addButton("Skip %s" % ("it" if one else "them"), QMessageBox.ActionRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        got = box.clickedButton()
        box.deleteLater()
        return "replace" if got is replace else "skip" if got is skip else None

    def _copy_here(self):
        pl, folder = self._place, self._cwd
        if pl is None or pl.kind != "app" or folder is None:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Copy files to %s" % pl.title, str(self._src_dir))
        if not files:
            return
        paths = [Path(f) for f in files]
        self._src_dir = paths[0].parent
        have = {e.name for e in self._entries or [] if isinstance(e, pf.Entry)}
        clash = [p.name for p in paths if p.name in have]
        overwrite = False
        if clash:
            choice = self._ask_replace(clash)
            if choice is None:
                return
            overwrite = choice == "replace"
        self._start("in", "Copying to the phone",
                    lambda job: pf.copy_in(paths, folder, self._progress_cb(job), job.cancel, overwrite=overwrite,
                                           root=pl.path), place=pl)

    # menu

    def _menu(self, pos):
        it = self._list.itemAt(pos)
        if it is not None and it.flags() & Qt.ItemIsSelectable and not it.isSelected():
            self._list.clearSelection()
            it.setSelected(True)
            self._list.setCurrentItem(it, QItemSelectionModel.NoUpdate)
        elif it is None:
            self._list.clearSelection()
        if self._menu_obj is None:
            self._menu_obj = QMenu(self)            # one menu, filled again each time, so right clicks pile nothing up
        menu = self._menu_obj
        menu.clear()
        sel, places = self._selected(), self._selected_places()
        busy = self._job is not None
        if places and len(places) == 1:
            menu.addAction("\U000f0770  Open", lambda _=False, p=places[0]: self._go(p, p.path))
        if sel:
            if len(sel) == 1 and sel[0].is_dir:
                menu.addAction("\U000f0770  Open", lambda _=False, e=sel[0]: self._go(self._place, self._cwd / e.name))
            if any(e.is_dir or (not e.link and self._tracks.is_audio(e.name)) for e in sel):
                menu.addAction("\U000f040a  Play now", lambda: self._tracks_of("play", sel)).setEnabled(not busy)
                menu.addAction("\U000f0412  Add to playlist", lambda: self._tracks_of("add", sel)).setEnabled(not busy)
                menu.addAction("\U000f0331  Add to library",
                               lambda: self._copy_to_computer(sel, library=True)).setEnabled(not busy)
            menu.addAction("\U000f01da  Copy to computer...", lambda: self._copy_to_computer(sel)).setEnabled(not busy)
        pl = self._place
        if pl is not None and pl.kind == "app":
            text = "\U000f0552  Copy files here..." + (" (read only)" if pl.writable is False else "")
            menu.addAction(text, self._copy_here).setEnabled(not busy and pl.writable is not False)
        if menu.actions():
            menu.addSeparator()
        menu.addAction("\U000f0450  Refresh", self.refresh)
        menu.exec(self._list.viewport().mapToGlobal(pos))
