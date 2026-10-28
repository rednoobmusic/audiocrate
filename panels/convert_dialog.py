"""Convert files to other formats: pick files, a format and a quality, press Convert.

The window is not modal, so the music keeps playing. The work is done by integrations/converter.py; this file only
collects the choices, runs a Converter and shows how it is going. Reading what is in a file, walking folders and
asking the file system about a drive all happen on worker threads, and the converter reports from its own threads,
so nothing here waits for the disk.

    ConvertDialog(parent, paths=None).show()      # or open_convert(parent, paths) to reuse a window that is open
"""
from __future__ import annotations

import os
import weakref
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import converter as cv
from library_widgets import Scheme, mix

STATE_ROLE, PCT_ROLE, JOB_ROLE = Qt.UserRole, Qt.UserRole + 1, Qt.UserRole + 2
WIDE = 980                      # from this width the files and the settings sit side by side
PRESETS = ('{filename}', '{artist} - {title}', '{track} {title}', '{albumartist}/{album}/{track} {title}')
SAMPLE = {'title': 'Song title', 'artist': 'Artist', 'albumartist': 'Artist', 'album': 'Album', 'genre': 'Genre',
          'track': 3, 'disc': 1, 'year': 2020}
STATE_TEXT = {cv.WAITING: 'Waiting', cv.RENDERING: 'Rendering', cv.ENCODING: 'Converting', cv.DONE: 'Done',
              cv.SKIPPED: 'Skipped', cv.FAILED: 'Failed', cv.CANCELLED: 'Cancelled'}
HINT_TEXT = {'rate': "Keep each file's sample rate, or convert every file to one rate.",
             'trim': 'Cuts silence from the start and end of each file.'}


class _Ring:
    """The keyboard focus outline. The themes style the buttons but none of them draws one."""

    def _ring_init(self):
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
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(self.palette().windowText().color(), 2 if isinstance(self, QPushButton) else 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 5, 5)


class _Button(_Ring, QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._ring_init()


class _Check(_Ring, QCheckBox):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._ring_init()


class _Signals(QObject):
    found = Signal(object, int)         # the files to add, how many given files were not music
    described = Signal(str, object)
    fatmap = Signal(int, object)        # which check asked, {folder: whether it is on a FAT drive}
    updated = Signal(object)
    finished = Signal(object)


class _Task(QRunnable):
    """fn() on the thread pool; whatever it needs to say goes through the signals it holds."""

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self._fn()
        except RuntimeError:            # the dialog was deleted while this was reading
            pass
        except Exception:
            cv.log.exception('convert dialog task')


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName('settings_hint')
    lbl.setWordWrap(True)
    lbl.setForegroundRole(QPalette.PlaceholderText)         # dim under the system look too
    return lbl


def _heading(text: str) -> QWidget:
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(4)
    lbl = QLabel(text)
    lbl.setObjectName('settings_section')
    f = lbl.font()
    f.setBold(True)
    lbl.setFont(f)
    rule = QFrame()
    rule.setObjectName('settings_rule')
    rule.setFrameShape(QFrame.HLine)
    v.addWidget(lbl)
    v.addWidget(rule)
    return box


class _Flow(QWidget):
    """Cells in a grid with as many columns as fit, so a narrow window gets one long column."""

    def __init__(self, cell: int = 190, cols: int = 3, parent=None):
        super().__init__(parent)
        self._cells: list[QWidget] = []
        self._cell, self._cols, self._n = cell, cols, 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(8)

    def add(self, w: QWidget):
        self._cells.append(w)
        self.relayout()

    def _columns(self, shown: list) -> int:
        """As many columns as fit, judged by what the cells really need in the live font."""
        need = max([self._cell] + [w.minimumSizeHint().width() for w in shown])
        gap = self._grid.horizontalSpacing()
        return max(1, min(self._cols, (self.width() + gap) // (need + gap)))

    def _shown(self) -> list:
        return [w for w in self._cells if not (w.isHidden() and w.testAttribute(Qt.WA_WState_ExplicitShowHide))]

    def relayout(self):
        shown = self._shown()
        n = self._columns(shown)
        while self._grid.count():
            self._grid.takeAt(0)
        for i, w in enumerate(shown):
            self._grid.addWidget(w, i // n, i % n, Qt.AlignTop)
        for c in range(self._cols):
            self._grid.setColumnStretch(c, 1 if c < n else 0)
        self._n = n

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._columns(self._shown()) != self._n:
            self.relayout()


def _cell(text: str, field: QWidget) -> QWidget:
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(3)
    lbl = QLabel(text)
    lbl.setBuddy(field)
    v.addWidget(lbl)
    v.addWidget(field)
    return box


def _combo(*rows) -> QComboBox:
    cb = QComboBox()
    cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    cb.setMinimumContentsLength(10)
    for text, data in rows:
        cb.addItem(text, data)
    _widen_popup(cb)
    return cb


def _widen_popup(cb: QComboBox):
    cb.view().setMinimumWidth(cb.view().sizeHintForColumn(0) + 24)


def _icon(state: str, sc: Scheme, dpr: float) -> QIcon:
    """The little picture for a state, drawn in the colours of the live theme."""
    n = 16
    pm = QPixmap(int(n * dpr), int(n * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    fg, acc, base, on_acc = sc.fg, sc.accent, sc.base, sc.on_accent
    dim = mix(base, fg, 0.6)
    c, r = QPointF(n / 2, n / 2), 6.2
    ring = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
    pen = lambda col, w=1.4: QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setBrush(Qt.NoBrush)
    if state == cv.WAITING:
        p.setPen(pen(dim))
        p.drawEllipse(ring)
    elif state in (cv.RENDERING, cv.ENCODING):
        p.setPen(pen(acc))
        p.drawEllipse(ring)
        p.setPen(Qt.NoPen)
        p.setBrush(acc)
        p.drawPie(ring.adjusted(2, 2, -2, -2), 90 * 16, -250 * 16)
    elif state == cv.DONE:
        p.setPen(Qt.NoPen)
        p.setBrush(acc)
        p.drawEllipse(ring)
        p.setPen(pen(on_acc, 1.7))
        path = QPainterPath(QPointF(5, 8.3))
        path.lineTo(7.2, 10.5)
        path.lineTo(11, 5.8)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
    elif state == cv.SKIPPED:
        p.setPen(pen(dim))
        p.drawEllipse(ring)
        for dx in (-1.6, 1.8):
            path = QPainterPath(QPointF(c.x() + dx - 1.2, c.y() - 2.4))
            path.lineTo(c.x() + dx + 1.2, c.y())
            path.lineTo(c.x() + dx - 1.2, c.y() + 2.4)
            p.drawPath(path)
    elif state == cv.FAILED:
        p.setPen(Qt.NoPen)
        p.setBrush(fg)
        p.drawEllipse(ring)
        p.setPen(pen(base, 1.7))
        p.drawLine(QPointF(5.6, 5.6), QPointF(10.4, 10.4))
        p.drawLine(QPointF(10.4, 5.6), QPointF(5.6, 10.4))
    else:                                       # cancelled
        p.setPen(pen(dim))
        p.drawEllipse(ring)
        p.drawLine(QPointF(5.2, 10.8), QPointF(10.8, 5.2))
    p.end()
    return QIcon(pm)


class _StatusDelegate(QStyledItemDelegate):
    """The status text, with a thin line under it that grows as the file is converted."""

    def __init__(self, parent, accent):
        super().__init__(parent)
        self._accent = accent

    def initStyleOption(self, opt, idx):
        super().initStyleOption(opt, idx)
        opt.textElideMode = Qt.ElideRight

    def paint(self, p, opt, idx):
        super().paint(p, opt, idx)
        pct = idx.data(PCT_ROLE)
        if pct and 0 < pct < 100:
            r = opt.rect
            p.save()
            p.setPen(Qt.NoPen)
            p.setBrush(self._accent())
            p.drawRect(QRect(r.left() + 2, r.bottom() - 2, int((r.width() - 4) * pct / 100), 2))
            p.restore()


class _FileList(QTreeWidget):
    """The files to convert. Folders and files can be dropped on it."""
    dropped = Signal(list)
    remove_key = Signal()
    restyled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.accepting = True

    @staticmethod
    def _paths(e) -> list[str]:
        return [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]

    def dragEnterEvent(self, e):
        if self.accepting and self._paths(e):
            e.acceptProposedAction()
        else:
            e.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = self._paths(e)
        if self.accepting and paths:
            e.acceptProposedAction()
            self.dropped.emit(paths)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete and self.accepting:
            self.remove_key.emit()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.topLevelItemCount():
            p = QPainter(self.viewport())
            p.setPen(self.palette().color(QPalette.PlaceholderText))
            p.drawText(self.viewport().rect().adjusted(16, 16, -16, -16), Qt.AlignCenter | Qt.TextWordWrap,
                       'Drop files or folders here, or press Add files.')

    def changeEvent(self, e):
        if e.type() in (QEvent.PaletteChange, QEvent.StyleChange, QEvent.ApplicationPaletteChange):
            self.restyled.emit()
        super().changeEvent(e)


class _Body(QWidget):
    """The settings column. It follows the width of the scroll area and never asks for more."""

    def sizeHint(self):
        return QSize(100, super().sizeHint().height())


class ConvertDialog(QDialog):
    """Files in, format and quality, where the results go, Convert. `converter_factory` and `settings_file` are for
    tests: the factory gets on_update and on_done and returns something shaped like converter.Converter."""

    def __init__(self, parent=None, paths=None, converter_factory=None, settings_file=None):
        super().__init__(parent)
        self.setWindowTitle('Convert files')
        self.setMinimumSize(520, 460)
        self.resize(self._first_size())
        self.setAcceptDrops(True)
        self._factory = converter_factory or cv.Converter
        self._settings_file = Path(settings_file) if settings_file else None
        self._fmts = cv.formats()
        self._opts = cv.load_options(self._settings_file)
        self._quality_of = {self._opts.format: self._opts.quality}
        self._fat_auto = self._opts.fat is None
        self._fat_map: dict[str, bool] = {}
        self._fat_token = 0
        self._ratios = cv.load_ratios()
        self._conv = None
        self._running = False
        self._have: dict[str, QTreeWidgetItem] = {}
        self._info: dict[str, dict] = {}
        self._rows: dict[int, QTreeWidgetItem] = {}         # job id to its row, for the run on screen
        self._pct: dict[int, int] = {}
        self._pct_sum = 0
        self._icons: dict[str, QIcon] = {}
        self._pending = 0                                   # files whose details are still being read
        self._rejected = 0
        self._jobs: list = []
        self._cancelling = False
        self._last_stats: dict | None = None
        self._dir = str(Path.home())
        self._sig = _Signals(self)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_now)
        self._build()
        self._sc = Scheme(self._scroll.widget(), self._folder)      # a child, so there is no cycle through the window
        self._apply_options(self._opts)
        self._sig.found.connect(self._on_found)
        self._sig.described.connect(self._on_described)
        self._sig.fatmap.connect(self._on_fatmap)
        self._sig.updated.connect(self._on_update)
        self._sig.finished.connect(self._on_finished)
        QShortcut(QKeySequence.Open, self, self.add_files_dialog)
        if paths:
            self.add_paths(paths)
        self._refresh()

    @staticmethod
    def _first_size() -> QSize:
        """About 680 x 680, less on a small screen."""
        scr = QApplication.primaryScreen()
        room = scr.availableGeometry().size() * 0.9 if scr else QSize(680, 680)
        return QSize(max(520, min(680, room.width())), max(460, min(680, room.height())))

    def _build(self):
        me = weakref.proxy(self)                    # what a callback holds, so that the window can be freed
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        files = QWidget()
        fl = QVBoxLayout(files)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        title = QLabel('Files')
        title.setObjectName('settings_section')
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        bar.addWidget(title)
        bar.addStretch(1)
        self._b_add = _Button('Add &files...')
        self._b_dir = _Button('Add fol&der...')
        self._b_rem = _Button('&Remove')
        self._b_clr = _Button('C&lear')
        for b in (self._b_add, self._b_dir, self._b_rem, self._b_clr):
            b.setAutoDefault(False)
            bar.addWidget(b)
        fl.addLayout(bar)
        self._list = _FileList()
        self._list.setColumnCount(5)
        self._list.setHeaderLabels(['Name', 'Format', 'Length', 'Size', 'Status'])
        self._list.setRootIsDecorated(False)
        self._list.setUniformRowHeights(True)
        self._list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._list.setTextElideMode(Qt.ElideMiddle)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.setIconSize(QSize(16, 16))
        self._list.setItemDelegateForColumn(4, _StatusDelegate(self._list, lambda: me._sc.accent))
        self._list.setMinimumHeight(90)
        self._list.setAccessibleName('Files to convert')
        h = self._list.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.Interactive)
        h.resizeSection(4, 140)
        h.setMinimumSectionSize(40)
        fl.addWidget(self._list, 1)
        self._size_note = _hint('')
        self._size_note.setAccessibleName('Estimated size of the results')
        fl.addWidget(self._size_note)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setMinimumHeight(110)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.viewport().setFocusPolicy(Qt.NoFocus)
        body = _Body()
        self._scroll.setWidget(body)
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 14, 0)
        col.setSpacing(8)
        self._build_format(col)
        self._build_options(col)
        self._build_chip(col)
        self._build_destination(col)
        col.addStretch(1)

        self._split = QSplitter(Qt.Vertical)
        self._split.setChildrenCollapsible(False)
        self._split.addWidget(files)
        self._split.addWidget(self._scroll)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 1)
        self._split.setSizes([230, 300])
        root.addWidget(self._split, 1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 1000)
        self._progress.setAccessibleName('Overall progress')
        self._progress.hide()
        root.addWidget(self._progress)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._summary.setMinimumHeight(self._summary.fontMetrics().lineSpacing() + 2)
        root.addWidget(self._summary)
        row = QHBoxLayout()
        row.setSpacing(6)
        self._b_open = _Button('Open &folder')
        self._b_open.setAutoDefault(False)
        self._b_go = _Button('&Convert')
        self._b_go.setDefault(True)
        self._b_close = _Button('Close')
        self._b_close.setAutoDefault(False)
        row.addWidget(self._b_open)
        row.addStretch(1)
        row.addWidget(self._b_go)
        row.addWidget(self._b_close)
        root.addLayout(row)

        self._b_add.clicked.connect(self.add_files_dialog)
        self._b_dir.clicked.connect(self.add_folder_dialog)
        self._b_rem.clicked.connect(self.remove_selected)
        self._b_clr.clicked.connect(self.clear_files)
        self._b_go.clicked.connect(self.start)
        self._b_close.clicked.connect(self._close_or_cancel)
        self._b_open.clicked.connect(self.open_folder)
        self._list.dropped.connect(self.add_paths)
        self._list.remove_key.connect(self.remove_selected)
        self._list.restyled.connect(self._restyle)
        self._list.itemSelectionChanged.connect(self._refresh_buttons)
        self._list.customContextMenuRequested.connect(self._row_menu)
        self.setTabOrder(self._b_add, self._b_dir)

    def _build_format(self, col):
        col.addWidget(_heading('Format'))
        flow = _Flow(210, 2)
        self._format = _combo(*[(f.label, f.id) for f in self._fmts])
        self._format.setAccessibleName('Format')
        self._quality = _combo()
        self._quality.setAccessibleName('Quality')
        flow.add(_cell('Format', self._format))
        flow.add(_cell('Quality', self._quality))
        col.addWidget(flow)
        self._note = _hint('')
        col.addWidget(self._note)
        self._format.currentIndexChanged.connect(self._on_format)
        self._quality.currentIndexChanged.connect(self._on_quality)

    def _build_options(self, col):
        col.addWidget(_heading('Options'))
        cells = _Flow(160, 3)
        self._rate = _combo(('Keep', 0), ('44.1 kHz', 44100), ('48 kHz', 48000), ('96 kHz', 96000))
        self._channels = _combo(('Keep', 0), ('Stereo', 2), ('Mono', 1))
        self._normalise = _combo(('Off', 'off'), ('ReplayGain from the tags', 'replaygain'),
                                 ('Even out the loudness', 'loudnorm'))
        self._rate.setToolTip(HINT_TEXT['rate'])
        for name, w in (('Sample rate', self._rate), ('Channels', self._channels), ('Volume', self._normalise)):
            w.setAccessibleName(name)
            cells.add(_cell(name, w))
        col.addWidget(cells)
        checks = _Flow(200, 3)
        self._trim = _Check('Trim silence at the ends')
        self._trim.setToolTip(HINT_TEXT['trim'])
        self._tags = _Check('Keep tags')
        self._art = _Check('Keep cover art')
        self._dsp = _Check('Use my equalizer, DSP chain and Enhance')
        self._dsp.setToolTip("Applies the player's equalizer, DSP chain and Enhance settings to the converted files.")
        self._dsp.setVisible(cv.dsp_available())
        for w in (self._trim, self._tags, self._art, self._dsp):
            checks.add(w)
        col.addWidget(checks)
        for w in (self._rate, self._channels, self._normalise):
            w.currentIndexChanged.connect(self._changed)
        for w in (self._trim, self._tags, self._art, self._dsp):
            w.toggled.connect(self._changed)

    def _build_chip(self, col):
        self._chip = QWidget()
        v = QVBoxLayout(self._chip)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(_heading('Chip and tracker files'))
        self._chip_flow = flow = _Flow(160, 3)
        self._secs = QSpinBox()
        self._secs.setRange(5, 3600)
        self._secs.setSuffix(' s')
        self._fade = QDoubleSpinBox()
        self._fade.setRange(0, 60)
        self._fade.setDecimals(1)
        self._fade.setSingleStep(0.5)
        self._fade.setSuffix(' s')
        self._loops = QSpinBox()
        self._loops.setRange(1, 16)
        for name, w in (('Length if unknown', self._secs), ('Fade out at the end', self._fade),
                        ('Loop count', self._loops)):
            w.setAccessibleName(name)
            flow.add(_cell(name, w))
            w.valueChanged.connect(self._changed)
        self._songs = _combo(('First song', 'first'), ('All songs, one file each', 'all'), ('One song, by number', 'n'))
        self._songs.setAccessibleName('Songs in the file')
        self._song_no = QSpinBox()
        self._song_no.setRange(1, 999)
        self._song_no.setAccessibleName('Song number')
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(self._songs, 1)
        h.addWidget(self._song_no)
        self._songs_cell = _cell('Songs in the file', row)
        flow.add(self._songs_cell)
        self._songs_cell.hide()                 # after it is in the flow: a cell hidden before it is added stays hidden
        flow.relayout()
        self._songs.currentIndexChanged.connect(self._on_songs)
        self._song_no.valueChanged.connect(self._changed)
        v.addWidget(flow)
        self._chip_note = _hint('')
        v.addWidget(self._chip_note)
        self._chip.hide()
        col.addWidget(self._chip)

    def _on_songs(self, *_):
        self._song_no.setVisible(self._songs.currentData() == 'n')
        self._changed()

    def _chip_state(self):
        """Show what the files in the list can use: the choice of songs only for a file that holds several, and a length
        setting only when some file in the list takes it, so that no control is on that does nothing."""
        chips = [i for i in self._info.values() if i.get('kind') == 'chip']
        most = max([i.get('songs') or 1 for i in chips] or [1])
        many = most > 1
        if self._songs_cell.isHidden() == many:
            self._songs_cell.setVisible(many)
            self._chip_flow.relayout()
        self._song_no.setMaximum(max(most, 1) if many else 999)
        self._song_no.setVisible(many and self._songs.currentData() == 'n')
        uses = [i.get('uses') or {} for i in chips]
        loops, secs = any(u.get('loops') for u in uses), any(u.get('seconds') for u in uses)
        self._loops.setEnabled(loops)
        self._secs.setEnabled(secs)
        self._loops.setToolTip('' if loops else 'None of the tunes loop, so this has no effect.')
        self._secs.setToolTip('' if secs else 'Every tune has its own length, so this has no effect.')
        said = []
        if chips and not secs:
            said.append('Every tune has its own length.')
        if chips and not loops:
            said.append('None of them loop.')
        self._chip_note.setText('These files are played through once to make the audio. A tune with its own length uses '
                                'that length. ' + ' '.join(said))

    def _build_destination(self, col):
        col.addWidget(_heading('Destination'))
        self._where = _combo(('Next to the original files', 'src'), ('Folder', 'dir'))
        self._where.setAccessibleName('Destination')
        col.addWidget(self._where)
        self._folder_row = QWidget()
        row = QHBoxLayout(self._folder_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._folder = QLineEdit()
        self._folder.setPlaceholderText('Choose a folder')
        self._folder.setAccessibleName('Destination folder')
        self._b_browse = _Button('Browse...')
        self._b_browse.setAutoDefault(False)
        row.addWidget(self._folder, 1)
        row.addWidget(self._b_browse)
        self._folder_row.hide()
        col.addWidget(self._folder_row)
        self._template = QComboBox()
        self._template.setEditable(True)
        self._template.setInsertPolicy(QComboBox.NoInsert)
        self._template.addItems(PRESETS)
        self._template.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._template.setMinimumContentsLength(12)
        self._template.setAccessibleName('File names')
        self._template.setToolTip('Fields: ' + ' '.join('{%s}' % f for f in cv.FIELDS) +
                                  '. A slash starts a folder.')
        _widen_popup(self._template)
        col.addWidget(_cell('File names', self._template))
        self._example = _hint('')
        self._example.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self._example)
        flow = _Flow(230, 2)
        self._exists = _combo(('Add a number', 'number'), ('Skip', 'skip'), ('Replace', 'overwrite'))
        self._exists.setAccessibleName('If the file exists')
        flow.add(_cell('If the file exists', self._exists))
        self._fat = _Check('Safe names for FAT drives')
        self._fat.setToolTip('Removes characters that USB sticks, SD cards and some players cannot store. '
                             'Turns on by itself for those drives.')
        flow.add(self._fat)
        col.addWidget(flow)
        self._fat_note = _hint('')
        self._fat_note.hide()
        col.addWidget(self._fat_note)
        self._where.currentIndexChanged.connect(self._on_where)
        self._folder.textChanged.connect(self._changed)
        self._folder.editingFinished.connect(self._detect_fat)
        self._b_browse.clicked.connect(self.browse_folder)
        self._template.editTextChanged.connect(self._changed)
        self._exists.currentIndexChanged.connect(self._changed)
        self._fat.clicked.connect(self._on_fat_click)

    def _fmt(self) -> cv.Format | None:
        return cv.format_by_id(self._format.currentData()) if self._fmts else None

    def _apply_options(self, o: cv.Options):
        for w in self._widgets():
            w.blockSignals(True)
        i = self._format.findData(o.format)
        self._format.setCurrentIndex(i if i >= 0 else 0)
        self._fill_qualities(o.quality)
        for combo, v in ((self._rate, o.rate), (self._channels, o.channels), (self._normalise, o.normalise),
                         (self._exists, o.exists)):
            combo.setCurrentIndex(max(combo.findData(v), 0))
        self._trim.setChecked(o.trim)
        self._tags.setChecked(o.tags)
        self._art.setChecked(o.art)
        self._dsp.setChecked(o.dsp)
        self._template.setEditText(o.template)
        self._where.setCurrentIndex(1 if o.folder else 0)
        self._folder.setText(o.folder)
        self._secs.setValue(o.chip_seconds)
        self._songs.setCurrentIndex(max(self._songs.findData(o.subsong if isinstance(o.subsong, str) else 'n'), 0))
        self._song_no.setValue(o.subsong if isinstance(o.subsong, int) else 1)
        self._fade.setValue(o.chip_fade)
        self._loops.setValue(o.chip_loops)
        self._fat_auto = o.fat is None
        self._fat.setCheckState(Qt.Checked if o.fat else Qt.Unchecked)
        for w in self._widgets():
            w.blockSignals(False)
        self._sync_where()
        self._fat_note.setVisible(self._fat_auto and bool(self._fat_note.text()))
        self._show_format_state()

    def _widgets(self):
        return (self._format, self._quality, self._rate, self._channels, self._normalise, self._trim, self._tags,
                self._art, self._dsp, self._template, self._where, self._folder, self._exists,
                self._fat, self._secs, self._fade, self._loops, self._songs, self._song_no)

    def options(self) -> cv.Options:
        """What the controls say now, as converter Options."""
        fmt = self._fmt()
        o = cv.Options()
        o.format = fmt.id if fmt else self._opts.format
        o.quality = self._quality.currentData() or ''
        o.rate, o.channels = self._rate.currentData(), self._channels.currentData()
        o.normalise, o.exists = self._normalise.currentData(), self._exists.currentData()
        o.trim, o.tags, o.art, o.dsp = (self._trim.isChecked(), self._tags.isChecked(), self._art.isChecked(),
                                        self._dsp.isChecked() and cv.dsp_available())
        o.template = self._template.currentText().strip() or '{filename}'
        o.folder = os.path.expanduser(self._folder.text().strip()) if self._to_folder() else ''
        o.fat = None if self._fat_auto else self._fat.isChecked()
        o.chip_seconds, o.chip_fade, o.chip_loops = self._secs.value(), self._fade.value(), self._loops.value()
        o.subsong = self._song_no.value() if self._songs.currentData() == 'n' else self._songs.currentData()
        return o

    def _save_now(self):
        self._save_timer.stop()
        cv.save_options(self.options(), self._settings_file)

    def _changed(self, *_):
        """Any setting: the example and the buttons follow, and the file is written a moment later."""
        if self._running:
            return
        self._save_timer.start()
        self._refresh()

    def _fill_qualities(self, want: str = ''):
        fmt = self._fmt()
        cb = self._quality
        cb.blockSignals(True)
        cb.clear()
        if fmt:
            for q in fmt.qualities:
                cb.addItem(q.label, q.id)
            keep = want or self._quality_of.get(fmt.id) or fmt.default
            cb.setCurrentIndex(max(cb.findData(keep), cb.findData(fmt.default), 0))
            _widen_popup(cb)
        cb.setEnabled(bool(fmt) and cb.count() > 1)
        cb.blockSignals(False)

    def _show_format_state(self):
        fmt = self._fmt()
        self._note.setText(fmt.note if fmt else '')
        self._note.setVisible(bool(fmt and fmt.note))
        self._art.setEnabled(bool(fmt and fmt.art) and not self._running)
        self._tags.setEnabled(bool(fmt and fmt.tags) and not self._running)
        self._art.setToolTip('' if not fmt or fmt.art else '%s files cannot hold a cover picture.' % fmt.label)
        self._tags.setToolTip('' if not fmt or fmt.tags else '%s files cannot hold tags.' % fmt.label)

    def _on_format(self, *_):
        fmt = self._fmt()
        self._fill_qualities()
        self._show_format_state()
        self._changed()

    def _on_quality(self, *_):
        fmt = self._fmt()
        if fmt:
            self._quality_of[fmt.id] = self._quality.currentData()
        self._changed()

    def _to_folder(self) -> bool:
        return self._where.currentData() == 'dir'

    def _sync_where(self):
        folder = self._to_folder()
        self._folder_row.setVisible(folder)
        if folder and not self._folder.text().strip() and self.isVisible() and not self._running:
            self._folder.setFocus()
        self._detect_fat()

    def _on_where(self, *_):
        self._sync_where()
        self._changed()

    def _on_fat_click(self, on):
        self._fat_auto = False
        self._fat.setCheckState(Qt.Checked if self._fat.isChecked() else Qt.Unchecked)
        self._fat_note.hide()
        self._changed()

    def _folders_in_use(self) -> list[str]:
        """The folders the results will go to: the chosen one, or the folder of each file in the list."""
        if self._to_folder():
            f = os.path.expanduser(self._folder.text().strip())
            return [f] if f else []
        return sorted({str(Path(p).parent) for p in self.paths()})

    def _detect_fat(self):
        """Work out, for every folder the results go to, whether it is on a FAT drive or a removable one, and let the
        box show it. The mount table is read on a worker thread: a folder on a stalled network mount must not hold the
        window. The files next to their originals may sit on different drives, so the box can be on for some of them."""
        if not self._fat_auto:
            return
        folders = self._folders_in_use()
        self._fat_token += 1
        token, sig = self._fat_token, self._sig
        if not folders:
            self._fat_map = {}
            self._show_fat()
            return
        self._pool.start(_Task(lambda: sig.fatmap.emit(token, {f: cv.fat_default(Path(f)) for f in folders})))

    def _on_fatmap(self, token: int, found: dict):
        if token == self._fat_token and self._fat_auto:
            self._fat_map = found
            self._show_fat()

    def _fat_of(self, path: str) -> bool:
        folder = os.path.expanduser(self._folder.text().strip()) if self._to_folder() else str(Path(path).parent)
        return bool(self._fat_map.get(folder))

    def _fat_counts(self) -> tuple[int, int]:
        """(files that will get FAT names, files), by what the automatic choice found for their folders."""
        paths = self.paths()
        return sum(1 for p in paths if self._fat_of(p)), len(paths)

    def _show_fat(self):
        """The box and the sentence under it say what the automatic choice will do for the files in the list."""
        if not self._fat_auto:
            return
        n, total = self._fat_counts()
        state = Qt.Checked if total and n == total else Qt.PartiallyChecked if n else Qt.Unchecked
        self._fat.blockSignals(True)
        self._fat.setCheckState(state)
        self._fat.blockSignals(False)
        if not total:
            text = ''
        elif n == total:
            text = ('Automatic: this file goes to a FAT drive, so FAT names are used.' if total == 1 else
                    'Automatic: all %d files go to FAT drives, so FAT names are used.' % total)
        elif not n:
            text = ('Automatic: this file does not go to a FAT drive, so FAT names are not used.' if total == 1 else
                    'Automatic: no file goes to a FAT drive, so FAT names are not used.')
        else:
            text = 'Automatic: FAT names will be used for %d of the %d files.' % (n, total)
        self._fat_note.setText(text)
        self._fat_note.setVisible(bool(text))
        self._refresh()

    def browse_folder(self):
        start = self._folder.text().strip() or self._dir
        d = QFileDialog.getExistingDirectory(self, 'Choose a folder for the converted files', start)
        if d:
            self._dir = d
            self._where.setCurrentIndex(1)
            self._folder.setText(d)
            self._detect_fat()

    def add_files_dialog(self):
        if self._running:
            return
        a, v, c = cv.exts()
        pat = ' '.join('*' + e for e in sorted(a | v | c))
        paths, _ = QFileDialog.getOpenFileNames(self, 'Add files to convert', self._dir,
                                                'Audio, video and tracker files (%s);;All files (*)' % pat)
        if paths:
            self._dir = str(Path(paths[0]).parent)
            self.add_paths(paths)

    def add_folder_dialog(self):
        if self._running:
            return
        d = QFileDialog.getExistingDirectory(self, 'Add a folder of music', self._dir)
        if d:
            self._dir = str(Path(d).parent)
            self.add_paths([d])

    def add_paths(self, paths):
        """Add files, and the music inside folders. Reading folders and files happens in the background."""
        if self._running:
            return
        paths = [str(p) for p in paths]
        if not paths:
            return
        if self._last_stats is not None:                # the list shows the last run: new files start a new list
            self._list.clear()
            self._have.clear()
            self._info.clear()
            self._reset_run()
        self._pending += 1
        self._refresh()
        sig = self._sig

        def work():
            files = [p for p in paths if not os.path.isdir(p)]
            found = cv.expand(paths)
            rejected = len(files) - len({str(p) for p in found} & set(files))
            sig.found.emit(found, max(rejected, 0))
        self._pool.start(_Task(work))

    def _on_found(self, found, rejected: int):
        self._pending -= 1
        self._rejected += rejected
        new = [p for p in found if str(p) not in self._have]
        self._list.setUpdatesEnabled(False)
        sig = self._sig
        for p in new:
            p = Path(p)
            it = QTreeWidgetItem([cv.printable(p.name), p.suffix.lstrip('.').upper(), '', '', ''])
            it.setData(0, STATE_ROLE, cv.WAITING)
            it.setToolTip(0, str(p))
            it.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            it.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
            self._have[str(p)] = it
            self._list.addTopLevelItem(it)
            self._pending += 1

            def read(p=p):
                try:
                    info = cv.describe(p)
                except Exception:
                    info = {'path': p, 'size': 0, 'duration': None, 'kind': cv.kind_of(p), 'tags': cv.blank_tags(p)}
                sig.described.emit(str(p), info)
            self._pool.start(_Task(read))
        self._list.setUpdatesEnabled(True)
        self._detect_fat()
        self._refresh()

    def _on_described(self, path: str, info: dict):
        self._pending -= 1
        it = self._have.get(path)
        if it is not None:
            self._info[path] = info
            it.setText(2, cv.human_time(info.get('duration')))
            it.setText(3, cv.human_size(info.get('size') or 0))
            t = info.get('tags') or {}
            bits = [t.get('artist'), t.get('title')]
            it.setToolTip(0, '%s\n%s' % (path, ' - '.join(b for b in bits if b)) if any(bits) else path)
        self._refresh()

    def remove_selected(self):
        if self._running:
            return
        for it in self._list.selectedItems():
            path = next((k for k, v in self._have.items() if v is it), None)
            if path is not None:
                del self._have[path]
                self._info.pop(path, None)
            self._list.takeTopLevelItem(self._list.indexOfTopLevelItem(it))
        self._reset_run()
        self._detect_fat()
        self._refresh()

    def clear_files(self):
        if self._running:
            return
        self._list.clear()
        self._have.clear()
        self._info.clear()
        self._reset_run()
        self._detect_fat()
        self._refresh()

    def _reset_run(self):
        self._last_stats = None
        self._rows.clear()
        self._pct.clear()
        self._pct_sum = 0
        self._progress.hide()
        self._rejected = 0
        self._jobs = []

    def paths(self) -> list[str]:
        return [str(self._list.topLevelItem(i).toolTip(0).split('\n')[0]) for i in range(self._list.topLevelItemCount())]

    def _first_path(self) -> str | None:
        it = self._list.topLevelItem(0) if self._list.topLevelItemCount() else None
        return it.toolTip(0).split('\n')[0] if it else None

    def _problem(self) -> str:
        if not self._fmts:
            return 'ffmpeg is not installed, so files cannot be converted.'
        if not self._list.topLevelItemCount():
            return 'Add files to convert.'
        err = cv.template_error(self._template.currentText())
        if err:
            return err
        if self._to_folder() and not self._folder.text().strip():
            return 'Choose a folder for the converted files.'
        return ''

    def _example_text(self) -> str:
        fmt = self._fmt()
        if not fmt:
            return ''
        tpl = self._template.currentText()
        err = cv.template_error(tpl)
        if err:
            return err
        first = self._first_path()
        info = self._info.get(first) if first else None
        src, tags = (Path(first), dict(info['tags'])) if info else (Path('song.flac'), dict(SAMPLE))
        fat = self._fat_of(first) if self._fat_auto and first else self._fat.isChecked()
        try:
            return 'Example: ' + cv.example(tpl, tags, src, fmt.ext, fat)
        except Exception:
            return ''

    def _estimate_text(self) -> str:
        """The size the results should come to, before and during a run: bit rate times length for the lossy formats,
        the shares that finished files showed for the lossless ones. Files already done count at their real size."""
        fmt = self._fmt()
        if not fmt or not self._info or self._last_stats is not None:
            return ''
        opts = self._jobs[0].opts if self._jobs else self.options()
        q = cv.quality_of(fmt, opts.quality)
        if self._running:
            done = [j for j in self._jobs if j.state == cv.DONE]
            rest = [self._info[str(j.src)] for j in self._jobs if j.state not in cv.FINAL and str(j.src) in self._info]
            got = cv.estimate_size(fmt, q, opts, rest, self._ratios) if rest else (0, True)
            if got is None:
                return ''
            total = sum(j.bytes_out for j in done) + got[0]
            return 'Estimated size when finished: about %s, %d of %d files done.' % (
                cv.human_size(total), len(done), len(self._jobs))
        got = cv.estimate_size(fmt, q, opts, self._info.values(), self._ratios)
        if got is None:
            return ''
        return 'Estimated size of the results: about %s.%s' % (
            cv.human_size(got[0]), '' if got[1] else ' This is a guess until one file is done.')

    def _idle_summary(self) -> str:
        n = self._list.topLevelItemCount()
        if not n:
            return ''
        secs = sum(i.get('duration') or 0 for i in self._info.values())
        size = sum(i.get('size') or 0 for i in self._info.values())
        text = '%d file%s' % (n, '' if n == 1 else 's')
        if self._info:
            text += ', ' + ', '.join(x for x in (cv.human_time(secs) + ' long' if secs else '',
                                                 cv.human_size(size) if size else '') if x)
        text += '.'
        rej = self._rejected
        if rej:
            text += ' %d file%s could not be added because AudioCrate cannot convert %s.' % (
                rej, '' if rej == 1 else 's', 'it' if rej == 1 else 'them')
        return text

    def _refresh(self):
        """Everything that depends on the settings and the list: example, buttons, the line at the bottom."""
        has_chip = any(i.get('kind') == 'chip' for i in self._info.values())
        if self._chip.isHidden() == has_chip:
            self._chip.setVisible(has_chip)
        if has_chip:
            self._chip_state()
        self._size_note.setText(self._estimate_text())
        self._example.setText(self._example_text())
        if not self._running:
            if self._last_stats is None:
                self._summary.setText(self._problem() or self._idle_summary())
            self._refresh_buttons()

    def _refresh_buttons(self):
        run, n = self._running, self._list.topLevelItemCount()
        self._list.accepting = not run
        for b in (self._b_add, self._b_dir):
            b.setEnabled(not run)
        self._b_rem.setEnabled(not run and bool(self._list.selectedItems()))
        self._b_clr.setEnabled(not run and n > 0)
        self._b_go.setEnabled(not run and not self._problem())
        self._b_close.setText('Cancel' if run else 'Close')
        self._b_close.setEnabled(not (run and self._cancelling))
        self._b_open.setEnabled(not run and self._result_folder() is not None)
        self._scroll.setEnabled(not run)

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        """Convert every file in the list with the settings as they are."""
        if self._running or self._problem():
            return
        self._save_now()
        opts = self.options()
        self._reset_run()
        jobs = []
        for i in range(self._list.topLevelItemCount()):
            it = self._list.topLevelItem(i)
            job = cv.ConvertJob(it.toolTip(0).split('\n')[0], opts)
            it.setData(0, JOB_ROLE, job)
            self._rows[job.id] = it
            self._pct[job.id] = 0
            self._show_state(it, job)
            jobs.append(job)
        self._jobs = jobs
        self._running, self._cancelling = True, False
        self._progress.setValue(0)
        self._progress.show()
        self._summary.setText('Converting %d file%s.' % (len(jobs), '' if len(jobs) == 1 else 's'))
        self._size_note.setText(self._estimate_text())
        self._refresh_buttons()
        self._show_format_state()
        self._conv = self._factory(on_update=self._sig.updated.emit, on_done=self._sig.finished.emit)
        self._conv.start(jobs)

    def _icon_for(self, state: str) -> QIcon:
        ic = self._icons.get(state)
        if ic is None:
            ic = self._icons[state] = _icon(state, self._sc, self._list.devicePixelRatioF())
        return ic

    def _restyle(self):
        self._sc.refresh()
        self._icons.clear()
        for it in self._rows.values():
            it.setIcon(0, self._icon_for(it.data(0, STATE_ROLE) or cv.WAITING))

    def _show_state(self, it: QTreeWidgetItem, job):
        state = job.state
        it.setData(0, STATE_ROLE, state)
        it.setIcon(0, self._icon_for(state))
        if state == cv.ENCODING:
            text = '%d%%' % job.percent
        elif state in (cv.SKIPPED, cv.FAILED) and job.message:
            text = job.message
        else:
            text = STATE_TEXT.get(state, '')
        it.setText(4, text)
        info = self._info.get(str(job.src))
        if state == cv.DONE and job.bytes_out:
            it.setText(3, '%s to %s' % (cv.human_size(job.bytes_in), cv.human_size(job.bytes_out)))
        elif info is not None:
            it.setText(3, cv.human_size(info.get('size') or 0))
        it.setData(4, PCT_ROLE, job.percent if state in (cv.RENDERING, cv.ENCODING) else 0)
        tip = [job.message] if job.message and job.message != text else []
        if state == cv.DONE and job.dest:
            tip.append('Saved as %s' % job.dest)
        it.setToolTip(4, '\n'.join(tip))

    def _on_update(self, job):
        it = self._rows.get(job.id)
        if it is None:
            return
        self._show_state(it, job)
        if job.state == cv.DONE and str(job.src) in self._info:
            f = cv.format_by_id(job.opts.format)
            if f is not None:
                cv.note_ratio(self._ratios, f, cv.quality_of(f, job.opts.quality), self._info[str(job.src)], job.opts, job.bytes_out)
        now = 100 if job.state in cv.FINAL else job.percent
        self._pct_sum += now - self._pct[job.id]
        self._pct[job.id] = now
        if self._rows:
            self._progress.setValue(round(10 * self._pct_sum / len(self._rows)))
        if self._running:
            done = sum(1 for p in self._pct.values() if p >= 100)
            self._summary.setText('Converting. %d of %d finished.' % (done, len(self._rows)))
            self._size_note.setText(self._estimate_text())

    def _on_finished(self, stats: dict):
        self._running = False
        self._cancelling = False
        self._last_stats = stats
        self._conv = None
        cv.save_ratios(self._ratios)
        self._size_note.setText('')
        self._summary.setText(self.summary_text(stats))
        self._progress.setValue(round(10 * self._pct_sum / max(len(self._rows), 1)))
        self._refresh_buttons()
        self._show_format_state()
        self._sync_where()

    @staticmethod
    def summary_text(s: dict) -> str:
        """'12 converted in 48 s, 1 skipped, 0 failed, 84 MB to 31 MB'"""
        took = ' in ' + cv.elapsed_text(s['elapsed']) if s.get('elapsed') is not None else ''
        parts = ['%d converted%s' % (s.get('done', 0), took), '%d skipped' % s.get('skipped', 0), '%d failed' % s.get('failed', 0)]
        if s.get('cancelled'):
            parts.append('%d cancelled' % s['cancelled'])
        if s.get('done'):
            parts.append('%s to %s' % (cv.human_size(s.get('bytes_in', 0)), cv.human_size(s.get('bytes_out', 0))))
        return ', '.join(parts)

    def cancel(self, job=None):
        """Stop everything, or one file."""
        if self._conv is not None and self._running:
            if job is None:
                self._cancelling = True
                self._summary.setText('Cancelling.')
                self._refresh_buttons()
            self._conv.cancel(job)

    def _close_or_cancel(self):
        if self._running:
            self.cancel()
        else:
            self.close()

    def row_menu(self, it: QTreeWidgetItem) -> QMenu | None:
        """The menu for a row, not yet shown (None when it has nothing to offer)."""
        menu = QMenu(self)
        job = it.data(0, JOB_ROLE)
        if self._running:
            if job is not None and not job.finished:
                menu.addAction('Cancel this file', lambda: self.cancel(job))
        else:
            if job is not None and job.state == cv.DONE and job.dest:
                menu.addAction('Show in folder', lambda: self._open(job.dest.parent))
            if not self._list.selectedItems():
                it.setSelected(True)
            menu.addAction('Remove', self.remove_selected)
        if menu.isEmpty():
            menu.deleteLater()
            return None
        return menu

    def _row_menu(self, pos):
        it = self._list.itemAt(pos)
        menu = self.row_menu(it) if it is not None else None
        if menu is not None:
            menu.exec(self._list.viewport().mapToGlobal(pos))

    def _result_folder(self) -> Path | None:
        done = [j.dest.parent for j in self._jobs if j.state == cv.DONE and j.dest]
        if not done or self._running:
            return None
        if self._to_folder() and self._folder.text().strip():
            return Path(os.path.expanduser(self._folder.text().strip()))
        try:
            return Path(os.path.commonpath([str(d) for d in done]))
        except ValueError:
            return done[0]

    def open_folder(self):
        folder = self._result_folder()
        if folder is not None:
            self._open(folder)

    def _open(self, folder: Path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _confirm_stop(self) -> bool:
        n = sum(1 for j in self._jobs if not j.finished)
        return QMessageBox.question(
            self, 'Convert files', 'Stop converting? %d file%s not finished yet. Files that are done are kept.'
            % (n, ' is' if n == 1 else 's are'), QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def reject(self):
        if self._running:
            if not self._confirm_stop():
                return
            self.cancel()
        self._save_now()
        super().reject()

    def shutdown(self, wait: float = 3.0):
        """For the main window to call when the app quits: stop a running conversion and wait for it."""
        if self._conv is not None and self._running:
            self._conv.cancel()
            self._conv.wait(wait)
        self._save_now()
        self._pool.clear()
        self._pool.waitForDone(1500)

    def _follow_focus(self, _old, new):
        """Tab to a control that is scrolled out of sight brings it into view."""
        if new is not None and self._scroll.isAncestorOf(new):
            self._scroll.ensureWidgetVisible(new, 0, 24)

    def hideEvent(self, e):
        try:
            QApplication.instance().focusChanged.disconnect(self._follow_focus)
        except (RuntimeError, TypeError):
            pass
        super().hideEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        QApplication.instance().focusChanged.connect(self._follow_focus, Qt.UniqueConnection)
        self._icons.clear()
        self._restyle()
        self._fit()
        if not self._list.topLevelItemCount():
            self._b_add.setFocus()
        else:
            self._b_go.setFocus()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        o = Qt.Horizontal if self.width() >= WIDE else Qt.Vertical
        if self._split.orientation() != o:
            self._split.setOrientation(o)
            self._split.setSizes([520, 440] if o == Qt.Horizontal else [230, 300])

    def dragEnterEvent(self, e):
        if not self._running and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths and not self._running:
            e.acceptProposedAction()
            self.add_paths(paths)


_open_window: dict = {}


def open_convert(parent=None, paths=None) -> ConvertDialog:
    """Show the convert window, adding `paths`. A window that is open already is reused, so a conversion in progress is
    never started twice."""
    dlg = _open_window.get(id(parent))
    try:
        dlg.isVisible()
    except (AttributeError, RuntimeError):
        dlg = None
    if dlg is None:
        dlg = _open_window[id(parent)] = ConvertDialog(parent, paths)
    elif paths:
        dlg.add_paths(paths)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
