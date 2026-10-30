"""The Online panel: search YouTube, paste a link from YouTube, X or anywhere yt-dlp reaches, and play,
queue or save what comes back.

Fetching is done by the OnlineService, which runs yt-dlp as a child process. Everything here is
signals and painting: no service call waits for an answer, and anything that touches the disk in a
way that can be slow (the cache, cookie files) runs on a worker thread.
"""
from __future__ import annotations

import os
import random
import time
import urllib.parse
from pathlib import Path

from PySide6.QtCore import QEvent, QItemSelectionModel, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QDesktopServices, QFontMetrics, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLineEdit, QMenu, QMessageBox, QScrollArea, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from browser_login import BrowserLogin
from library_views import fmt_duration
from library_widgets import PillButton, ProgressLine, Scheme, Segmented, StatusLine, _Button, scaled_font
from online_channel import History, Target, item_target, link_target
from online_channel_page import ChannelPage, PlaylistPage
from online_providers import PROVIDERS, is_url, normalize_url, provider_for_url
from online_service import ERROR_ACTIONS, OnlineService, human_bytes, memory_cost_mb
from online_settings import NOTICE_VERSION, NoticePage, SettingsPage
from online_widgets import (CONTAINERS, LONG_S, Background, Bar, ChipBar, DownloadStrip, IconButton, ProviderButton, ResultHeader,
                            ResultsModel, ResultsView, Skeleton, StateView, ThumbLoader, WrapLabel, draw_icon, item_key,
                            plural, wrap_lines)

BATCH_CAP = 25          # entries taken by Play all, Add all, Save all and by a long selection in one go
BATCH_GAP = 4           # seconds between starts that the service keeps in a big batch (OnlineService._pace)
SHOW_MORE = {"search": (25, 100), "songs": (25, 50), "feed": (50, 200), "link": (50, 500)}   # step, most
DONE_SHOWN = 6.0        # seconds a finished fetch stays in the strip
ACTIVE = ("queued", "running", "postprocessing")

WELCOME, RESULTS, STATE, LOADING, NOTICE, SETTINGS, CHANNEL, PLAYLIST = range(8)     # stack pages; the notice and the settings sit over the others

_PLACEHOLDER = {"x": "Paste a link to an X post", "link": "Paste a link"}      # the rest come from the provider table
_ERR_TITLE = {
    "no_ytdlp": "yt-dlp is missing", "no_ffmpeg": "ffmpeg is missing", "network": "No connection",
    "login_required": "Sign in needed", "age_restricted": "Age check", "geo_blocked": "Not available here",
    "private": "This is private", "unavailable": "Not available", "rate_limited": "Too many requests",
    "unsupported_url": "Cannot open this link", "no_media": "Nothing to play here", "timeout": "No answer",
    "disk_full": "Disk is full", "too_large": "Too big", "failed": "That did not work",
    "write_failed": "Could not save", "refused": "The site said no",
}
_ERR_ICON = {"login_required": "lock", "age_restricted": "lock", "private": "lock", "unsupported_url": "link",
             "no_media": "link"}
_ACTION = {"sign_in": "Sign in", "update": "Update", "retry": "Try again"}


def placeholder(prov) -> str:
    if prov.id in _PLACEHOLDER:
        return _PLACEHOLDER[prov.id]
    return f"Search {prov.name} or paste a link" if prov.search_prefix else f"Paste a link to {prov.name}"


def intro() -> str:
    """The one sentence on the first page, from the sites the provider table lists."""
    names = [p.name for p in PROVIDERS.values() if p.hosts]
    sites = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    search = next((p.name for p in PROVIDERS.values() if p.search_prefix), "")
    return (f"Paste a {sites} link" + (f", or search {search}" if search else "") + ". It plays here through your EQ and "
            "visualizers, with tags and cover art, and you can keep it in your library.")


def action_label(code: str) -> str:
    """The one button that goes with an error code: Sign in, Update, Try again, or nothing."""
    if code == "no_ytdlp":
        return "Get yt-dlp"
    return _ACTION.get(ERROR_ACTIONS.get(code, ""), "")


def _eta(n: int) -> str:
    """A rough time for n items, from the pause the service keeps between starts in a big batch."""
    if n < 5:
        return ""
    s = n * BATCH_GAP
    return f"about {round(s / 10) * 10} s" if s < 90 else f"about {round(s / 60)} min"


def _songs_url(query: str) -> str:
    return "https://music.youtube.com/search?q=" + urllib.parse.quote(query, safe="") + "#songs"


def play_next_into(panel, paths) -> None:
    """Put tracks right after the one that is playing: in the playlist playback comes from and in the
    player's queue. A track that is already in that playlist is moved, not added again, and the tracks
    keep the order they were given in. A sort that is showing becomes the order of the list (as when a
    row is dragged), so the tracks land after the playing one as it is on screen. One Undo takes it all back.
    With nothing playing from a playlist they are added to the end of the one in front, as Add does.
    In shuffle mode the player still picks the next track itself."""
    want = list(dict.fromkeys(Path(p) for p in paths))
    if not want:
        return
    cur, lst = panel._player.current, panel._source
    if cur is None or lst is None or not lst.has_path(cur.path):
        panel.add_paths(want)
        return
    want = [p for p in want if p != cur.path]
    fresh = [p for p in want if not lst.has_path(p)]
    marks = len(lst._history)
    added = lst.add_paths(fresh) if fresh else []
    if added:
        panel._feed_queue(lst, added)
    rows = [lst.m.by_path[p] for p in want if lst.has_path(p)]
    playing = lst.m.by_path[cur.path]

    def place():
        m = lst.m
        m.commit()
        picked = set(rows)
        rest = [r for r in m.rows if r not in picked]
        i = rest.index(playing) + 1
        rest[i:i] = rows
        m.rows = rest
        m.finish()

    if rows:
        lst._arrange(place)
    if len(lst._history) > marks + 1:
        del lst._history[marks + 1:]               # the add and the move are one step to undo


class _Chain:
    """Hands finished fetches over in the order they were asked for, whatever order they end in.

    One chain is one action: Add all, Play next on several rows, a single click. With together set,
    nothing is handed over until every fetch of the chain has ended.
    """

    def __init__(self, deliver, together: bool = False):
        self.deliver, self.together, self.slots = deliver, together, []

    def slot(self, how: str, token: int = 0) -> dict:
        s = {"how": how, "token": token, "path": None, "done": False}
        self.slots.append(s)
        return s

    def resolve(self, slot: dict, path):
        slot["path"], slot["done"] = path, True
        if self.together and not all(s["done"] for s in self.slots):
            return
        ready = []
        while self.slots and self.slots[0]["done"]:
            ready.append(self.slots.pop(0))
        if ready:
            self.deliver(ready)


# the first page

class _Canvas(QWidget):
    """A page drawn on the list colour, so it matches the lists next to it."""

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc = sc

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), self.sc.base)


class _ActionRow(_Button):
    """A clickable card for the first page: a glyph, a title and a few words of help."""

    def __init__(self, sc: Scheme, glyph: str, title: str, hint: str = "", clickable: bool = True, parent=None):
        super().__init__(sc, parent)
        self.glyph, self.title, self.hint, self._lines, self.clickable = glyph, title, hint, [], clickable
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(title)
        self.setFixedHeight(44)
        if not clickable:
            self.setCursor(Qt.ArrowCursor)
            self.setFocusPolicy(Qt.NoFocus)

    def _small(self):
        return scaled_font(self.font(), 0.88)

    def _relayout(self):
        fm = QFontMetrics(self._small())
        self._lines = wrap_lines(fm, self.hint, max(20, self.width() - 56), 3)
        h = 20 + self.fontMetrics().height() + (len(self._lines) * fm.height() + 2 if self._lines else 0)
        h = max(44 if self.glyph else 34, h)
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.clickable and (self.underMouse() or self.isDown())
        p.setPen(Qt.NoPen)
        p.setBrush(sc.hover if hot else sc.well)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        x = 10
        if self.glyph:
            p.setBrush(sc.sel if hot else sc.well_hover)
            p.drawEllipse(QRectF(10, (self.height() - 28) / 2, 28, 28))
            draw_icon(p, self.glyph, QRectF(17, (self.height() - 14) / 2, 14, 14), sc.fg if hot else sc.dim)
            x = 48
        fm, small = self.fontMetrics(), QFontMetrics(self._small())
        w = self.width() - x - 8
        total = fm.height() + (len(self._lines) * small.height() + 2 if self._lines else 0)
        y = (self.height() - total) // 2
        p.setPen(sc.fg)
        p.drawText(QRect(x, y, w, fm.height()), Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(self.title, Qt.ElideRight, w))
        p.setFont(self._small())
        p.setPen(sc.dim)
        for i, ln in enumerate(self._lines):
            p.drawText(QRect(x, y + fm.height() + 2 + i * small.height(), w, small.height()), Qt.AlignVCenter | Qt.AlignLeft, ln)


class _Welcome(QScrollArea):
    """The first page: what this is in one sentence, three ways to start, and the last searches."""
    paste = Signal()
    search = Signal()
    recent = Signal(str)
    clear_recent = Signal()
    PASTE = "Copy one from your browser, then paste it in the box above. YouTube, X and many other sites work."

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc = sc
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = _Canvas(sc)
        self._col = QVBoxLayout(inner)
        self._col.setContentsMargins(12, 14, 12, 12)
        self._col.setSpacing(7)
        title = WrapLabel(sc, 2, dim=False, bold=True, scale=1.0)
        title.setText("Play audio from links")
        self._intro = WrapLabel(sc, 6, dim=True, scale=0.95)
        self._intro.setText(intro())
        self._col.addWidget(title)
        self._col.addWidget(self._intro)
        self._col.addSpacing(4)
        self._rows = [
            _ActionRow(sc, "paste", "Paste a link", self.PASTE),
            _ActionRow(sc, "search", "Search YouTube", "Type a song or an artist and press Enter."),
            _ActionRow(sc, "save", "Drop a link on the window", "Drag a link from your browser onto AudioCrate.", clickable=False),
        ]
        self._rows[0].clicked.connect(self.paste)
        self._rows[1].clicked.connect(self.search)
        for r in self._rows:
            self._col.addWidget(r)
        self._note = WrapLabel(sc, 4, dim=True, scale=0.88)
        self._col.addWidget(self._note)
        self._recent_head = WrapLabel(sc, 1, dim=True, scale=0.88)
        self._recent_head.setText("Recent searches")
        self._col.addSpacing(6)
        self._col.addWidget(self._recent_head)
        self._recent_rows: list[_ActionRow] = []
        self._shown: list[str] | None = None
        self._clear = _ActionRow(sc, "close", "Clear recent searches")
        self._clear.clicked.connect(self.clear_recent)
        self._col.addStretch(1)
        self.setWidget(inner)
        self.set_recent([])

    def set_recent(self, searches: list[str]):
        searches = list(searches[:6])
        if searches == self._shown:
            return
        self._shown = searches
        for r in self._recent_rows:
            self._col.removeWidget(r)
            r.hide()
            r.deleteLater()
        self._recent_rows = []
        self._recent_head.setVisible(bool(searches))
        for text in searches:
            row = _ActionRow(self.sc, "search", text)
            row.clicked.connect(lambda _=False, t=text: self.recent.emit(t))
            self._recent_rows.append(row)
            self._col.insertWidget(self._col.count() - 1, row)
        self._col.removeWidget(self._clear)
        self._col.insertWidget(self._col.count() - 1, self._clear)
        self._clear.setVisible(bool(searches))

    def set_provider(self, prov):
        """The search card names the site in use, and goes away for a site that has no search."""
        row = self._rows[1]
        row.title = f"Search {prov.name}"
        row.setAccessibleName(row.title)
        row.setVisible(bool(prov.search_prefix))
        row.update()
        self._note.setText(prov.login_note)

    def painted(self) -> list:
        return [*self._rows, *self._recent_rows, self._clear]


class OnlineWidget(QWidget):
    add_to_playlist = Signal(list)          # list[Path]
    play_now = Signal(list)                 # list[Path]
    play_next = Signal(list)                # list[Path]
    library_changed = Signal(object)        # Path of a folder that gained files
    status = Signal(str, int)               # message, milliseconds (0 = stay)

    def __init__(self, player, parent=None, service: OnlineService | None = None):
        super().__init__(parent)
        self._player = player
        self._svc = service if service is not None else OnlineService(self)
        self._closed = False
        self._suspended = False
        self._built = False
        self._woke = False                   # set when the panel is first shown, which is when the tools are looked at
        self._tools_due = False              # set at the first search or link: the notes about the tools wait until then
        self._provider = PROVIDERS["youtube"]
        self._mode = "audio"
        self._chip = "search"
        self._page = WELCOME                 # what the stack shows
        self._content = WELCOME              # what it shows when nothing is laid over it
        self._overlay: int | None = None     # NOTICE or SETTINGS while one is up
        self._notice_under: int | None = None
        self._loading = False
        self._req: dict | None = None        # the list request in flight
        self._last: dict | None = None       # the one that gave the list on screen
        self._failed: dict | None = None     # the one that failed for want of a sign in
        self._aux: dict[int, tuple] = {}     # request id -> what to do with the answer (sign in checks, updates)
        self._items: list[dict] = []
        self._info: dict = {}
        self._views: list[dict] = []         # lists left behind by opening a playlist, for Back
        self._hist = History()               # the channel and playlist pages opened one from another
        self._base = (WELCOME, "youtube")    # the page and the site the first of them was opened from
        self._jobs: dict[str, dict] = {}     # job id -> its last state
        self._how: dict[str, tuple] = {}     # job id -> (what it was asked for, mode), for Try again
        self._want: dict[str, list] = {}     # job id -> the (chain, slot) pairs waiting for its file
        self._gone: set[str] = set()         # jobs dismissed from the strip
        self._done_at: dict[str, float] = {}
        self._play_token = 0
        self._saved_dirs: set = set()
        self._asked: dict[str, bool] = {}     # what was answered when a site asked before saving
        self._dismissed: set = set()
        self._tool_key = ""
        self._pending = None                 # what to do once the notice is accepted
        self._session_ok = False
        self._last_text = ""
        self._programmatic = False
        self._protected: list[str] = []
        self._keeper = None
        self._state_do = None
        self._accent = None
        self._system_accent = False
        self._default_accent = None

        self._restyle_timer = QTimer(self)
        self._restyle_timer.setSingleShot(True)
        self._restyle_timer.timeout.connect(self._restyle)
        self._strip_timer = QTimer(self)
        self._strip_timer.setInterval(1000)
        self._strip_timer.timeout.connect(self._strip_tick)
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.setInterval(400)
        self._saved_timer.timeout.connect(self._flush_saved)
        self._protect_timer = QTimer(self)
        self._protect_timer.setSingleShot(True)
        self._protect_timer.setInterval(250)
        self._protect_timer.timeout.connect(self._protect_refresh)
        self._bg = Background(self)
        self._thumbs = ThumbLoader(getattr(self._svc, "thumbs_dir", None), self)
        self._thumbs.ready.connect(self._repaint_list)
        self.setAcceptDrops(True)
        self.setMinimumSize(200, 140)

        self._build()
        self._wire()
        self._set_provider("youtube")
        self._show(WELCOME)
        self._built = True
        self._restyle_timer.start(0)

    # building

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._edit = QLineEdit()
        sc = self._sc = Scheme(self, self._edit)

        head = QVBoxLayout()
        head.setContentsMargins(8, 6, 8, 6)
        head.setSpacing(5)
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self._prov_btn = ProviderButton(sc)
        self._prov_btn.chosen.connect(self._set_provider)
        self._act_find = self._edit.addAction(QIcon(), QLineEdit.LeadingPosition)
        self._act_clear = self._edit.addAction(QIcon(), QLineEdit.TrailingPosition)
        self._act_clear.setVisible(False)
        self._act_clear.setToolTip("Clear")
        self._act_clear.triggered.connect(self._edit.clear)
        self._edit.textChanged.connect(self._on_text)
        self._edit.returnPressed.connect(self._go)
        self._edit.setMinimumWidth(60)
        for key, fn in ((Qt.Key_Escape, self._escape), (Qt.Key_Down, self._focus_list)):
            sh = QShortcut(QKeySequence(key), self._edit)
            sh.setContext(Qt.WidgetShortcut)
            sh.activated.connect(fn)
        self._go_btn = PillButton(sc, "Go", primary=True)
        self._go_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._go_btn.setFixedWidth(46)
        self._go_btn.clicked.connect(self._go_clicked)
        row1.addWidget(self._prov_btn)
        row1.addWidget(self._edit, 1)
        row1.addWidget(self._go_btn)
        head.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self._chips = ChipBar(sc)
        self._chips.chosen.connect(self._on_chip)
        self._chips.blocked.connect(lambda _id: self._open_settings("accounts", self._provider.id))
        self._seg = Segmented(sc, ["Audio", "Video"])
        self._seg.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._seg.changed.connect(self._on_mode)
        self._gear = IconButton(sc, "gear")
        self._gear.setToolTip("Online settings")
        self._gear.setAccessibleName("Online settings")
        self._gear.clicked.connect(self._toggle_settings)
        row2.addWidget(self._chips, 1)
        row2.addWidget(self._seg)
        row2.addWidget(self._gear)
        head.addLayout(row2)
        hw = QWidget()
        hw.setLayout(head)
        root.addWidget(hw)

        self._progress = ProgressLine(sc)
        root.addWidget(self._progress)

        self._tool_bar = Bar(sc)
        self._tool_bar.button.connect(self._tool_clicked)
        self._tool_bar.closed.connect(self._tool_dismissed)
        self._tool_bar.hide()
        root.addWidget(self._tool_bar)

        # the pages
        self._model = ResultsModel(self)
        self._list = ResultsView(sc, self._model, self._thumbs)
        self._list.on_enter = self._play_selected
        self._list.on_enter_add = lambda: self._act("add")
        self._list.doubleClicked.connect(self._row_activated)
        self._list.pressed_button.connect(self._row_button)
        self._list.customContextMenuRequested.connect(self._context)
        self._list.selectionModel().selectionChanged.connect(self._sel_changed)
        copy = QShortcut(QKeySequence.Copy, self._list)
        copy.setContext(Qt.WidgetShortcut)
        copy.activated.connect(self._copy_links)
        self._header = ResultHeader(sc)
        self._header.back.connect(self._go_back)
        self._header.play_all.connect(lambda: self._all("play"))
        self._header.add_all.connect(lambda: self._all("add"))
        self._header.save_all.connect(lambda: self._all("save"))
        self._header.hide()
        self._list.on_more = self._show_more
        self._list.on_escape = self._list_escape
        self._list.drag_paths = self._ready_paths
        self._list.clicked.connect(self._row_clicked)
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self._header)
        rl.addWidget(self._list, 1)

        self._welcome = _Welcome(sc)
        self._welcome.paste.connect(self._welcome_paste)
        self._welcome.search.connect(self._focus_edit)
        self._welcome.recent.connect(self._recent_clicked)
        self._welcome.clear_recent.connect(self._clear_recent)
        self._state = StateView(sc)
        self._state.action.connect(self._state_clicked)
        self._skeleton = Skeleton(sc)
        self._notice = NoticePage(sc)
        self._notice.accepted.connect(self._notice_accepted)
        self._notice.declined.connect(self._notice_declined)
        self._login = BrowserLogin(self._svc, self._bg.run, gate=self._gate, parent=self)
        self._login.check_started.connect(self._login_check)
        self._settings = SettingsPage(self._svc, self._bg, sc, self._login)
        self._settings.done.connect(self._close_settings)
        self._settings.check_requested.connect(self._check)
        self._settings.update_requested.connect(self._update)
        self._settings.clear_cache_requested.connect(self._clear_cache)
        self._settings.note.connect(self.status)
        self._chan = ChannelPage(sc, self._thumbs, self._svc)
        self._plist = PlaylistPage(sc, self._thumbs, self._svc)
        for pg in (self._chan, self._plist):
            pg.play_now.connect(lambda items: self._do("play", items))
            pg.shuffle.connect(self._shuffle)
            pg.play_next.connect(lambda items: self._do("next", items))
            pg.add_to_playlist.connect(lambda items: self._do("add", items))
            pg.save.connect(lambda items: self._do("save", items))
            pg.cancel_item.connect(self._cancel_item)
            pg.open_channel.connect(lambda link: self._open_link_page(link, "channel"))
            pg.open_playlist.connect(lambda link: self._open_link_page(link, "playlist"))
            pg.back.connect(self._page_back)
            pg.forward.connect(self._page_forward)
            pg.sign_in.connect(lambda pid: self._open_settings("accounts", pid))
            pg.update_tools.connect(lambda: (self._open_settings("tools"), self._update()))
            pg.shown.connect(self._pictured)
            pg.status.connect(self.status)
            pg.changed.connect(self._refresh_status)
        self._stack = QStackedWidget()
        for w in (self._welcome, results, self._state, self._skeleton, self._notice, self._settings, self._chan, self._plist):
            self._stack.addWidget(w)
        root.addWidget(self._stack, 1)

        self._strip = DownloadStrip(sc)
        self._strip.cancel.connect(lambda jid: self._svc.cancel(jid))
        self._strip.dismiss.connect(self._dismiss_job)
        self._strip.action.connect(self._job_action)
        self._strip.clear.connect(self._clear_finished)
        root.addWidget(self._strip)

        foot = QVBoxLayout()
        foot.setContentsMargins(8, 4, 8, 8)
        foot.setSpacing(5)
        self._status_line = StatusLine(sc)
        foot.addWidget(self._status_line)
        self._pills = QWidget()
        pl = QHBoxLayout(self._pills)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        self._b_play = PillButton(sc, "Play", primary=True)
        self._b_add = PillButton(sc, "Add")
        self._b_save = PillButton(sc, "Save")
        self._b_play.clicked.connect(self._play_selected)
        self._b_add.clicked.connect(lambda: self._act("add"))
        self._b_save.clicked.connect(lambda: self._act("save"))
        for b in (self._b_play, self._b_add, self._b_save):
            pl.addWidget(b, 1)
        foot.addWidget(self._pills)
        self._foot = QWidget()
        self._foot.setLayout(foot)
        root.addWidget(self._foot)

        focus = QShortcut(QKeySequence("Ctrl+L"), self)
        focus.setContext(Qt.WidgetWithChildrenShortcut)
        focus.activated.connect(self._focus_edit)
        back = QShortcut(QKeySequence("Alt+Left"), self)
        back.setContext(Qt.WidgetWithChildrenShortcut)
        back.activated.connect(self._go_back)
        fwd = QShortcut(QKeySequence("Alt+Right"), self)
        fwd.setContext(Qt.WidgetWithChildrenShortcut)
        fwd.activated.connect(self._page_forward)
        self._painted = [self._prov_btn, self._chips, self._seg, self._gear, self._go_btn, self._progress,
                         self._status_line, self._b_play, self._b_add, self._b_save, self._skeleton,
                         self._state, self._strip, self._header, self._tool_bar]
        self._sel_changed()
        self._apply_compact()

    def _wire(self):
        s = self._svc
        s.results_ready.connect(self._on_results)
        s.request_failed.connect(self._on_failed)
        s.job_updated.connect(self._on_job)
        s.login_changed.connect(self._on_login_changed)
        s.tools_changed.connect(self._on_tools)
        try:
            s.protect = self._protect_paths
        except AttributeError:
            pass
        sig = getattr(self._player, "track_changed", None)
        if sig is not None:
            sig.connect(self._on_track)
        sig = getattr(self._player, "queue_changed", None)
        if sig is not None:
            sig.connect(self._protect_timer.start)
        self._on_track(None)

    # theme

    def _restyle(self):
        """Re-read the live theme and repaint. Runs a tick after a style change, once the children are polished."""
        if self._closed:
            return
        sc = self._sc
        sc.refresh()
        self._act_find.setIcon(self._icon("search", sc.dimmer))
        self._act_clear.setIcon(self._icon("close", sc.dim))
        for w in (*self._painted, *self._header.painted(), *self._tool_bar.painted(),
                  *self._notice.painted(), *self._settings.painted(), *self._welcome.painted()):
            w.update()
        self._chan.restyle()
        self._plist.restyle()
        self._list.viewport().update()
        self._apply_compact()

    def _icon(self, name: str, color) -> QIcon:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(14 * dpr), int(14 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        draw_icon(p, name, QRectF(0, 0, 14, 14), color)
        p.end()
        return QIcon(pm)

    def changeEvent(self, e):
        if e.type() in (QEvent.PaletteChange, QEvent.StyleChange, QEvent.ApplicationPaletteChange, QEvent.FontChange):
            if self._built:
                self._restyle_timer.start(0)
        super().changeEvent(e)

    def set_accent(self, palette):
        """The album art colours, or None. The painted parts read their accent from the live style sheet."""
        self._accent = palette
        self._restyle_timer.start(0)

    def set_system_accent(self, on: bool):
        self._system_accent = bool(on)
        self._restyle_timer.start(0)

    def set_default_accent(self, color):
        self._default_accent = color
        self._restyle_timer.start(0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._built:
            self._apply_compact()

    def _apply_compact(self):
        w, h = self.width(), self.height()
        narrow = w < 340
        self._prov_btn.set_compact(w < 420)
        labels = ["\U000f0387", "\U000f0567"] if narrow else ["Audio", "Video"]
        if labels != self._seg.labels:
            self._seg.labels = labels
            self._seg.setFixedWidth(self._seg.minimumSizeHint().width() + (10 if narrow else 16))
            self._seg.update()
        self._pills.setVisible(h >= 330 and self._page == RESULTS)
        self._foot.setVisible(h >= 220)
        self._header.set_compact(h < 260)
        self._strip_update()

    # showing and hiding

    def showEvent(self, e):
        super().showEvent(e)
        if not self._woke:                   # the probes of yt-dlp, ffmpeg and the rest start on the first look, not at launch
            self._woke = True
            self._rebuild_chips()
            self._tools_banner()
        self._restyle_timer.start(0)
        self._sync_active()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_active()

    def set_suspended(self, suspended: bool):
        self._suspended = bool(suspended)
        self._sync_active()

    def _sync_active(self):
        """Timers and background loading run only while the panel can be seen."""
        on = self.isVisible() and not self._suspended and not self._closed
        self._skeleton.run(on and self._page == LOADING)
        self._progress.set(-1 if (on and self._loading) else None)
        self._chan.set_active(on)
        self._plist.set_active(on)
        if on:
            self._strip_timer_sync()
        else:
            self._strip_timer.stop()
            self._thumbs.drop_backlog()
            if self._saved_timer.isActive():
                self._saved_timer.stop()
                self._flush_saved()

    # the public side

    def open_link(self, url: str, play: bool = False) -> None:
        """Open a link as if it had been pasted. With play, what it holds starts playing as soon as it is known."""
        self._open(str(url or "").strip(), "play" if play else "show")

    def focus_search(self) -> None:
        """Put the cursor in the box, on the page that was showing."""
        if self._overlay is not None:
            self._close_overlay()
        self._focus_edit()

    def show_settings(self, section: str = "", provider_id: str = "") -> None:
        """Open the settings page: section is accounts or tools, provider_id picks the site's row."""
        self._open_settings(section, provider_id or self._provider.id)

    def set_keeper(self, fn) -> None:
        """fn() returns the paths of every playlist. Online files in them stay out of cache pruning, as do
        the ones in the player's queue and the one that is playing, which are read from the player."""
        self._keeper = fn
        self._protect_refresh()

    def now_playing_path(self) -> Path | None:
        try:
            cur = self._player.current
        except Exception:
            return None
        path = getattr(cur, "path", None) if cur is not None else None
        return Path(path) if path else None

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for t in (self._strip_timer, self._saved_timer, self._restyle_timer, self._protect_timer):
            t.stop()
        self._skeleton.run(False)
        self._progress.set(None)
        self._chan.shutdown()
        self._plist.shutdown()
        if self._saved_dirs:
            self._flush_saved()
        if self._req is not None:
            self._svc.cancel(self._req["id"])
        for rid in list(self._aux):
            self._svc.cancel(rid)
        for jid, st in list(self._jobs.items()):
            if st.get("state") in ACTIVE:
                self._svc.cancel(jid)
        self._thumbs.close()
        self._login.shutdown()
        self._svc.shutdown()

    # pages

    def _show(self, page: int):
        """Show a content page: the first one, the list, a message or the loading rows."""
        self._content = page
        if page == WELCOME:
            self._refresh_recent()
        if self._overlay is None:
            self._set_page(page)

    def _open_overlay(self, page: int):
        self._overlay = page
        self._set_page(page)

    def _close_overlay(self):
        self._overlay = None
        self._set_page(self._content)

    def _set_page(self, page: int):
        self._page = page
        self._stack.setCurrentIndex(page)
        self._pills.setVisible(self.height() >= 330 and page == RESULTS)
        self._sync_active()
        self._refresh_status()
        self._sel_changed()

    # the notice

    def _notice_ok(self) -> bool:
        return self._session_ok or self._svc.settings().get("notice_version") == NOTICE_VERSION

    def _gate(self, then) -> bool:
        """True when the notice has been accepted. Otherwise show it and run `then` once it is."""
        if self._notice_ok():
            return True
        self._pending = then
        if self._overlay != NOTICE:
            self._notice_under = self._overlay
        self._open_overlay(NOTICE)
        if not self.isVisible():
            self.status.emit("Open the Online panel to read a short notice before the first link.", 6000)
        return False

    def _leave_notice(self):
        under, self._notice_under = self._notice_under, None
        if under is None:
            self._close_overlay()
        else:
            self._open_overlay(under)

    def _notice_accepted(self, remember: bool):
        self._session_ok = True
        if remember:
            try:
                self._svc.set_setting("notice_version", NOTICE_VERSION)
            except ValueError:
                pass
        self._leave_notice()
        then, self._pending = self._pending, None
        if then is not None:
            then()

    def _notice_declined(self):
        self._pending = None
        self._leave_notice()

    # header controls

    def _set_provider(self, pid: str):
        prov = PROVIDERS.get(pid)
        if prov is None:
            return
        if prov is not self._provider:
            self._cancel_request()                   # an answer for the old site must not show up under the new one
        self._provider = prov
        want = 1 if prov.id == "x" else 0          # a post on X is a video, a song on YouTube is audio
        if (self._mode == "video") != bool(want):
            self._seg.set_current(want)
            self._on_mode(want)
        self._prov_btn.set_providers(PROVIDERS.values(), prov)
        self._edit.setPlaceholderText(placeholder(prov))
        self._chip = "search" if prov.search_prefix else "link"
        self._rebuild_chips()
        self._welcome.set_provider(prov)
        self._refresh_recent()
        self._refresh_status()
        self._tools_banner()

    def _recent(self) -> list[str]:
        try:
            return list(self._svc.settings().get("recent", []))
        except Exception:
            return []

    def _refresh_recent(self):
        self._welcome.set_recent(self._recent() if self._provider.search_prefix else [])

    def _clear_recent(self):
        try:
            self._svc.set_setting("recent", [])
        except ValueError:
            pass
        self._refresh_recent()

    def _signed_in(self, pid: str) -> bool:
        try:
            return self._svc.cookie_source(pid).get("kind", "none") != "none"
        except Exception:
            return False

    def _rebuild_chips(self):
        prov = self._provider
        gdl = self._svc.tools().get("gallery_dl", {}) if self._woke else {"checking": True}
        chips = []
        if prov.search_prefix:
            chips.append(("search", "Search", True, ""))
            if prov.id == "youtube":
                chips.append(("songs", "Songs", True, "Search YouTube Music for songs"))
        else:
            chips.append(("link", "Link", True, "Paste a link"))
        for f in prov.feeds:
            if f.id == "search":
                continue
            if prov.id == "x" and not gdl.get("ok") and not gdl.get("checking"):
                continue                            # the X lists come from gallery-dl
            ok = not f.needs_login or self._signed_in(prov.id)
            chips.append((f.id, f.label, ok, "" if ok else "Sign in to use this"))
        if len(chips) < 2:
            chips = []
        ids = [c[0] for c in chips]
        if chips and self._chip not in ids:
            self._chip = ids[0]
        self._chips.set_chips(chips, self._chip)

    def _on_chip(self, cid: str):
        self._chip = cid
        if cid in ("search", "songs", "link"):
            text = self._edit.text().strip()
            if text and not is_url(text):
                self._submit(text)
            else:
                self._focus_edit()
        else:
            self._browse(cid)

    def _on_mode(self, i: int):
        self._mode = "video" if i == 1 else "audio"
        self._seg.setToolTip("Video sound uses the EQ, Enhance and DSP (Settings, Playback)." if self._mode == "video"
                             else "Audio plays through the EQ, Enhance and DSP.")
        self._refresh_status()

    def _toggle_settings(self):
        if self._overlay == SETTINGS:
            self._close_settings()
        else:
            self._open_settings()

    def _on_text(self, text: str):
        self._act_clear.setVisible(bool(text))
        grew = len(text) - len(self._last_text)
        self._last_text = text
        # a paste puts a whole link in at once, typing adds one character at a time
        if not self._programmatic and grew >= 8 and is_url(text):
            QTimer.singleShot(0, self._go)

    def _set_text(self, text: str):
        self._programmatic = True
        try:
            self._edit.setText(text)
            self._edit.setCursorPosition(0)
        finally:
            self._programmatic = False

    def _escape(self):
        if self._edit.text():
            self._edit.clear()
        elif self._views or self._in_page():
            self._go_back()

    def _list_escape(self):
        """Escape in the list goes back to the list before, or else lets go of the selection."""
        if self._views or self._in_page():
            self._go_back()
        else:
            self._list.clearSelection()

    def _focus_edit(self):
        self._edit.setFocus()
        self._edit.selectAll()

    def _focus_list(self):
        if self._content in (CHANNEL, PLAYLIST) and self._overlay is None:
            (self._chan if self._content == CHANNEL else self._plist).focus_list()
        elif self._page == RESULTS and self._model.rowCount():
            if not self._list.currentIndex().isValid():
                self._list.setCurrentIndex(self._model.index(0))
            self._list.setFocus()

    def _go_clicked(self):
        if self._loading:
            self._cancel_request()
        else:
            self._go()

    def _go(self):
        self._submit(self._edit.text().strip())

    # asking

    def _first_ask(self):
        if not self._tools_due:
            self._tools_due = True
            self._tools_banner()

    def _submit(self, text: str):
        if text:
            self._first_ask()
        if not text:
            if self._chip not in ("search", "songs", "link"):
                self._browse(self._chip)
            else:
                self._cancel_request()
                self._drop_pages()
                self._show(WELCOME)
            return
        if is_url(text) or link_target(text) is not None:
            self._open(text, "show")
            return
        if not self._provider.search_prefix:
            self._cancel_request()
            self._message("link", "Paste a link", f"{self._provider.name} has no search. Paste a link to a post or a page.")
            return
        self._search(text, self._chip == "songs")

    def _search(self, text: str, songs: bool = False):
        if not self._gate(lambda: self._search(text, songs)):
            return
        pid = self._provider.id
        kind = "songs" if songs else "search"
        if songs:
            run = lambda n: self._svc.resolve(_songs_url(text), n)
        else:
            run = lambda n: self._svc.search(pid, text, n)
        self._begin(kind, text, run, SHOW_MORE[kind][0])

    def _browse(self, feed_id: str):
        feed = next((f for f in self._provider.feeds if f.id == feed_id), None)
        if feed is None or not self._gate(lambda: self._browse(feed_id)):
            return
        pid = self._provider.id
        self._begin("feed", feed.label, lambda n: self._svc.browse(pid, feed_id, n), SHOW_MORE["feed"][0])

    def _open(self, url: str, then: str = "show", keep_stack: bool = False):
        """Open a link: a video, a playlist, a channel, a post. then is show, play or add."""
        if not url or not self._gate(lambda: self._open(url, then, keep_stack)):
            return
        self._first_ask()
        target = link_target(url) if then == "show" else None
        if target is not None:                       # a YouTube channel or playlist, or an @handle, opens as a page
            self._set_text(url)
            self._open_page(target)
            return
        prov = provider_for_url(url)
        if prov.id != self._provider.id:
            self._set_provider(prov.id)
        self._set_text(url)
        self._begin("link", url, lambda n: self._svc.resolve(url, n), SHOW_MORE["link"][0], then=then,
                    keep_stack=keep_stack)

    def _begin(self, kind: str, label: str, run, limit: int, then: str = "show", keep_stack: bool = False,
               more: bool = False):
        """Start a list request and show that it is loading. The service answers by signal."""
        self._cancel_request(quiet=True)
        if not keep_stack and not more:
            self._views.clear()
            self._drop_pages()
        rid = run(limit)
        self._req = {"id": rid, "kind": kind, "label": label, "run": run, "limit": limit, "then": then, "more": more,
                     "scroll": self._list.verticalScrollBar().value() if more else 0}
        self._failed = None
        self._set_loading(True, keep_page=more)

    def _cancel_request(self, quiet: bool = False):
        req, self._req = self._req, None
        if req is not None:
            self._svc.cancel(req["id"])
        if self._loading:
            self._set_loading(False)
            if not quiet and self._content == LOADING:
                self._show(RESULTS if self._items else WELCOME)

    def _set_loading(self, on: bool, keep_page: bool = False):
        self._loading = on
        self._go_btn.setText("Stop" if on else "Go")
        if on and not keep_page:
            self._show(LOADING)
        self._sync_active()
        self._refresh_status()

    def _show_more(self):
        req = self._last
        if req is None or self._loading:
            return
        step, most = SHOW_MORE.get(req["kind"], (25, 100))
        limit = min(most, max(len(self._items), req["limit"]) + step)
        self._begin(req["kind"], req["label"], req["run"], limit, then="show", keep_stack=True, more=True)

    # answers

    def _on_results(self, rid: int, items: list, info: dict):
        aux = self._aux.pop(rid, None)
        if aux is not None:
            self._aux_done(aux, True, info)
            return
        req = self._req
        if req is None or req["id"] != rid:
            return
        self._req = None
        self._set_loading(False)
        self._last = req
        self._set_results(items, info, req)

    def _on_failed(self, rid: int, code: str, msg: str):
        aux = self._aux.pop(rid, None)
        if aux is not None:
            self._aux_done(aux, False, {"code": code, "message": msg})
            return
        req = self._req
        if req is None or req["id"] != rid:
            return
        self._req = None
        self._set_loading(False)
        if code == "cancelled":
            self._show(RESULTS if self._items else WELCOME)
            return
        if req.get("more"):                                  # a longer list that did not come: keep the one we have
            self.status.emit(msg, 6000)
            self._show(RESULTS)
            return
        if req["kind"] == "songs" and code in ("failed", "unsupported_url", "no_media", "unavailable"):
            self.status.emit("Songs search is not available right now. Showing video results.", 6000)
            self._chip = "search"
            self._chips.set_current("search")
            self._search(req["label"], False)
            return
        self._error(code, msg, req)

    def _error(self, code: str, msg: str, req: dict | None):
        act = ERROR_ACTIONS.get(code, "")
        pid = self._provider.id
        if act == "sign_in":
            self._state_do = lambda: self._open_settings("accounts", pid)
            self._failed = req
        elif act == "update":
            self._state_do = lambda: (self._open_settings("tools"), self._update())
        elif act == "retry" and req is not None:
            self._state_do = lambda: self._retry(req)
        else:
            self._state_do = None
        title = _ERR_TITLE.get(code, "That did not work")
        self._state.show_message(_ERR_ICON.get(code, "warn"), title, msg, action_label(code))
        self._show(STATE)
        if not self.isVisible():
            self.status.emit(f"{title}. {msg}", 8000)

    def _retry(self, req: dict):
        self._begin(req["kind"], req["label"], req["run"], req["limit"], then=req["then"])

    def _state_clicked(self):
        if self._state_do is not None:
            self._state_do()

    def _message(self, glyph: str, title: str, hint: str = "", button: str = "", do=None):
        self._state_do = do
        self._state.show_message(glyph, title, hint, button)
        self._show(STATE)

    def _set_results(self, items: list, info: dict, req: dict):
        if req["kind"] == "songs":                       # a flat song list names no artist, so say where it is from
            items = [dict(it, uploader=it.get("uploader") or "YouTube Music") for it in items]
        self._items, self._info = list(items), dict(info)
        self._model.set_items(items)                 # the "show more" row is added by the caller once the header is set
        for key in {item_key(it) for it in items}:
            self._refresh_mark(key)
        if not items:
            if req["kind"] in ("search", "songs"):
                self._message("search", f"No results for “{req.get('label', '')}”", "Try other words, or paste a link.")
            else:
                self._message("search", "Nothing in here", "This list is empty.")
            return
        self._update_header(req)
        self._model.set_more(self._can_show_more(req))
        self._show(RESULTS)
        self._list.verticalScrollBar().setValue(req.get("scroll", 0) if req.get("more") else 0)
        then = req.get("then", "show")
        if then in ("play", "add") and not req.get("more"):
            self._all(then)

    def _can_show_more(self, req: dict) -> bool:
        most = SHOW_MORE.get(req["kind"], (25, 100))[1]
        return bool(self._info.get("more")) and len(self._items) < most

    def _update_header(self, req: dict):
        info, items = self._info, self._items
        listy = req["kind"] in ("feed", "link") and (len(items) > 1 or info.get("kind") in CONTAINERS)
        self._header.setVisible(listy)
        if not listy:
            return
        n = int(info.get("count") or len(items))
        noun = "track" if all(it.get("kind") == "audio" for it in items) else "video"
        total = plural(n, noun)
        sub = total if n <= len(items) else f"Showing {len(items):,} of {total}"
        who = info.get("uploader") or ""
        if who and who not in (info.get("title") or ""):
            sub += f", {who}"
        take = min(BATCH_CAP, sum(1 for it in items if it.get("kind") not in CONTAINERS))
        capped = len(items) > BATCH_CAP
        if capped:
            sub += f". Play all, Add all and Save all take the first {BATCH_CAP}."
        eta = _eta(take)
        tip = ""
        if capped or eta:
            tip = f"Takes the first {take}" if capped else f"Takes {take}"
            tip += f", {eta}." if eta else "."
        title = info.get("title") or req.get("label") or "List"
        self._header.set_texts(title, sub, bool(self._views), {"play": tip, "add": tip, "save": tip})
        self._header.set_buttons(play=take > 0)

    def _snapshot(self) -> dict:
        return {"items": self._items, "info": self._info, "last": self._last, "provider": self._provider.id,
                "chip": self._chip, "text": self._edit.text(), "scroll": self._list.verticalScrollBar().value()}

    def _open_item(self, it: dict, url: str = ""):
        """Open a playlist or a channel, keeping the list we came from so Back can return to it."""
        target = url or it.get("url") or ""
        if not target:
            return
        page = item_target(it, url)
        if page is not None:
            self._open_page(page)
            return
        if self._content == RESULTS and self._items:
            self._views.append(self._snapshot())
        self._open(target, "show", keep_stack=True)

    def _go_back(self):
        if self._in_page():
            self._page_back()
            return
        if not self._views:
            return
        v = self._views.pop()
        self._cancel_request(quiet=True)
        self._last = v["last"]
        self._items, self._info = v["items"], v["info"]
        if v["provider"] != self._provider.id:
            self._set_provider(v["provider"])
        self._chip = v["chip"]
        self._rebuild_chips()
        self._set_text(v["text"])
        self._model.set_items(self._items, self._can_show_more(self._last))
        self._update_header(self._last)
        self._show(RESULTS)
        self._list.verticalScrollBar().setValue(v["scroll"])

    # channel and playlist pages

    def _in_page(self) -> bool:
        return self._content in (CHANNEL, PLAYLIST)

    def _open_page(self, target: Target):
        """Show a channel or a playlist. Back leads to the page it was opened from, then to the ones opened before it."""
        if not self._gate(lambda: self._open_page(target)):
            return
        if not self._in_page():
            self._cancel_request(quiet=True)           # a list still arriving must not land under the page
            shown = self._content if self._content in (WELCOME, RESULTS, STATE) else (RESULTS if self._items else WELCOME)
            self._base = (shown, self._provider.id)
            self._hist.clear()
        self._hist.push(target)
        self._show_target(target)

    def _open_link_page(self, link: str, kind: str):
        target = link_target(link) or item_target({"kind": kind, "url": link})     # another site's link does not say which it is
        if target is not None:
            self._open_page(target)

    def _show_target(self, target: Target):
        if target.provider != self._provider.id and target.provider in PROVIDERS:
            self._set_provider(target.provider)
        page, other = (self._plist, self._chan) if target.kind == "playlist" else (self._chan, self._plist)
        other.leave()
        page.set_nav(self._hist.can_forward)
        page.open(target)
        self._show(PLAYLIST if page is self._plist else CHANNEL)

    def _page_back(self):
        if not self._hist.back():
            return
        if self._hist.current is not None:
            self._show_target(self._hist.current)
            return
        self._chan.leave()
        self._plist.leave()
        shown, pid = self._base
        if pid != self._provider.id:
            self._set_provider(pid)
        self._show(shown)

    def _page_forward(self):
        if not self._hist.can_forward:
            return
        if not self._in_page():
            self._base = (self._content, self._provider.id)
        self._hist.forward()
        self._show_target(self._hist.current)

    def _drop_pages(self):
        """A search, a link or a feed starts over: the pages and their history are forgotten."""
        self._chan.leave()
        self._plist.leave()
        self._hist.clear()

    def _shuffle(self, items: list[dict]):
        items = [it for it in items if it.get("kind") not in CONTAINERS]
        random.shuffle(items)
        self._do("play", items)

    def _pictured(self, items: list):
        """Rows that came onto a page learn which of them are fetching, ready or saved."""
        for key in {item_key(it) for it in items}:
            self._refresh_mark(key)

    def _refresh_status(self):
        if self._closed or not hasattr(self, "_status_line"):
            return
        mode_note = "Video sound uses the EQ, Enhance and DSP (Settings, Playback)." if self._mode == "video" else ""
        text = mode_note
        if self._loading:
            req = self._req or {}
            text = {"search": "Searching...", "songs": "Searching songs...", "feed": f"Loading {req.get('label', '')}...",
                    "link": "Opening the link..."}.get(req.get("kind"), "Working...")
        elif self._page == RESULTS:
            n, info = len(self._items), self._info
            kind, label = (self._last or {}).get("kind"), (self._last or {}).get("label", "")
            if kind in ("search", "songs"):
                text = f"{plural(n, 'result')} for “{label}”"
            elif kind == "feed":
                text = f"{plural(n, 'video')}, {label}"
            else:
                text = plural(n, "item")
            if info.get("more"):
                text += ", more available"
            if mode_note:
                text = mode_note
        elif self._page in (CHANNEL, PLAYLIST):
            text = (self._chan if self._page == CHANNEL else self._plist).status_text()
        elif self._page in (SETTINGS, NOTICE):
            text = ""
        picked = len(self._rows()) if self._page == RESULTS and self._list.selectionModel().hasSelection() else 0
        self._status_line.set_texts(text, f"{picked:,} selected" if picked else "")

    def _repaint_list(self):
        self._list.viewport().update()

    # rows

    def _rows(self) -> list[int]:
        n = len(self._model.items)
        rows = sorted({i.row() for i in self._list.selectionModel().selectedRows() if i.row() < n})
        if not rows:
            cur = self._list.currentIndex()
            if cur.isValid() and cur.row() < n and self._list.hasFocus():
                rows = [cur.row()]
        return rows

    def _selected(self) -> list[dict]:
        return [self._model.items[r] for r in self._rows()]

    def _sel_changed(self, *_):
        picked = [it for it in self._selected() if it.get("kind") not in CONTAINERS]
        has = bool(self._list.selectionModel().hasSelection()) and bool(picked)
        for b in (self._b_play, self._b_add, self._b_save):
            b.setEnabled(has)
        self._refresh_status()

    def _row_clicked(self, ix):
        if self._model.is_more(ix.row()):
            self._show_more()

    def _row_activated(self, ix):
        if self._model.is_more(ix.row()):
            return
        it = self._model.items[ix.row()]
        if it.get("kind") in CONTAINERS:
            self._open_item(it)
        else:
            self._play_selected()

    def _row_button(self, name: str, row: int):
        if not 0 <= row < len(self._model.items):
            return
        it = self._model.items[row]
        if name in ("play", "add", "save"):
            self._do(name, [it])
        elif name == "cancel":
            self._cancel_item(it)
        elif name == "open":
            self._open_item(it)
        elif name == "who":
            self._open_item(it, it.get("uploader_url") or "")

    def _play_selected(self, *_):
        sel = self._selected()
        if len(sel) == 1 and sel[0].get("kind") in CONTAINERS:
            self._open_item(sel[0])
        else:
            self._do("play", sel)

    def _act(self, what: str, mode: str | None = None):
        self._do(what, self._selected(), mode)

    def _all(self, what: str):
        self._do(what, [it for it in self._items if it.get("kind") not in CONTAINERS])

    def _ready_paths(self, rows: list[int] | None = None) -> list[Path]:
        """The files of the rows (the selected ones by default) that have already been fetched, one for each row."""
        out = []
        for r in (self._rows() if rows is None else rows):
            for p in sorted(self._model.state(r).get("paths", ())):
                if Path(p).is_file():
                    out.append(Path(p))
                    break
        return out

    def _copy_links(self):
        links = [it.get("url") for it in self._selected() if it.get("url")]
        if links:
            QGuiApplication.clipboard().setText("\n".join(links))
            self.status.emit("Copied the link" + ("s" if len(links) > 1 else ""), 2000)

    # context menu

    def _context(self, pos):
        ix = self._list.indexAt(pos)
        if not ix.isValid():
            return
        sm = self._list.selectionModel()
        if not sm.isSelected(ix):
            sm.select(ix, QItemSelectionModel.ClearAndSelect)
        sm.setCurrentIndex(ix, QItemSelectionModel.NoUpdate)
        menu = self._menu_for(self._rows())
        if menu is not None:
            menu.exec(self._list.viewport().mapToGlobal(pos))

    def _menu_for(self, rows: list[int]) -> QMenu | None:
        items = [self._model.items[r] for r in rows if 0 <= r < len(self._model.items)]
        if not items:
            return None
        menu = QMenu(self)
        fetch = [it for it in items if it.get("kind") not in CONTAINERS]
        if fetch:
            menu.addAction("Play now", lambda: self._do("play", fetch))
            menu.addAction("Play next", lambda: self._do("next", fetch))
            menu.addAction("Add to playlist", lambda: self._do("add", fetch))
            menu.addSeparator()
            menu.addAction("Save to library", lambda: self._do("save", fetch))
            menu.addAction("Save as video", lambda: self._do("save", fetch, "video"))
        for it in items:
            if it.get("kind") in CONTAINERS:
                menu.addAction("Open", lambda it=it: self._open_item(it))
                break
        menu.addSeparator()
        one = items[0]
        if len(items) == 1 and one.get("url"):
            menu.addAction("Open in browser", lambda: self._browser(one["url"]))
        files = self._ready_paths(rows) if len(items) == 1 else []
        if files:
            menu.addAction("Show in file manager", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(files[0].parent))))
        menu.addAction("Copy link" if len(items) == 1 else "Copy links", self._copy_links)
        if len(items) == 1 and one.get("uploader_url"):
            menu.addAction("Open channel", lambda: self._open_item(one, one["uploader_url"]))
        return menu

    @staticmethod
    def _browser(url: str):
        try:
            QDesktopServices.openUrl(QUrl(normalize_url(url)))
        except ValueError:
            pass

    # acting on items

    def _do(self, what: str, items: list[dict], mode: str | None = None):
        """what is play, next, add or save. The first item of a play starts playing, the rest join the playlist."""
        items = [it for it in items if it.get("kind") not in CONTAINERS]
        if not items or not self._gate(lambda: self._do(what, items, mode)):
            return
        if what == "save":
            items = self._may_save(items)
        if not items:
            return
        if len(items) > BATCH_CAP:
            self.status.emit(f"Taking the first {BATCH_CAP} of {len(items):,}.", 6000)
            items = items[:BATCH_CAP]
        mode = mode or self._mode
        if mode == "video" and what in ("play", "next"):
            self.status.emit("Video sound uses the EQ, Enhance and DSP (Settings, Playback).", 5000)
        elif mode == "audio" and what != "save":
            for it in items:
                if (it.get("duration") or 0) > LONG_S:
                    self.status.emit(f"{it.get('title') or 'That item'} is {fmt_duration(it['duration'])} long. "
                                     f"Playing it keeps about {memory_cost_mb(it['duration']):,} MB in memory.", 7000)
                    break
        if what == "play" and not self.isVisible():
            self.status.emit(f"Fetching {items[0].get('title') or 'the link'}...", 4000)
        chain = _Chain(self._deliver, together=(what == "next"))
        token = 0
        if what == "play":
            self._play_token += 1
            token = self._play_token
        for i, it in enumerate(items):
            how = "add" if (what == "play" and i > 0) else what
            self._fetch(it, how, chain, mode, token if how == "play" else 0)
        eta = _eta(len(items))
        if len(items) > 1:
            self.status.emit(f"{'Saving' if what == 'save' else 'Fetching'} {len(items)} items"
                             + (f", {eta}" if eta else "") + ".", 5000)

    def _fetch(self, it: dict, how: str, chain: _Chain, mode: str, token: int = 0) -> str:
        dest = "library" if how == "save" else "cache"
        self._protect_refresh()                      # the prune that ends this fetch reads what was true when it began
        jid = self._svc.start_download(it, mode, dest, how == "play", how == "next")
        self._how[jid] = (how, mode)
        self._jobs.setdefault(jid, {"id": jid, "state": "queued", "percent": 0.0, "text": "Waiting", "item": it,
                                    "mode": mode, "dest": dest, "path": "", "error": "", "error_code": ""})
        self._want.setdefault(jid, []).append((chain, chain.slot(how, token)))
        self._refresh_mark(item_key(it))
        self._strip_update()
        return jid

    def _may_save(self, items: list[dict]) -> list[dict]:
        ok = []
        for it in items:
            prov = PROVIDERS.get(it.get("provider"))
            policy = getattr(prov, "save_policy", "ok")
            if policy == "no":
                self.status.emit(f"{prov.name} is for streaming only, so its music cannot be saved here.", 6000)
                continue
            if policy == "ask":
                if prov.id not in self._asked:
                    text = (f"{prov.name} licenses its music for streaming. AudioCrate keeps it in a cache for playback. "
                            "Save it only if the artist allows downloads. Save anyway?")
                    self._asked[prov.id] = QMessageBox.question(self, "Save to library", text) == QMessageBox.Yes
                if not self._asked[prov.id]:
                    continue
            ok.append(it)
        return ok

    def _cancel_item(self, it: dict):
        key = item_key(it)
        for jid, st in list(self._jobs.items()):
            if item_key(st["item"]) == key and st.get("state") in ACTIVE:
                self._svc.cancel(jid)

    def _deliver(self, slots: list[dict]):
        """Send finished fetches on, in the order they were asked for."""
        run: list = []
        kind = ""

        def flush():
            nonlocal run
            if run:
                {"add": self.add_to_playlist, "next": self.play_next}[kind].emit(run)
                run = []

        for s in slots:
            if s["path"] is None:
                continue
            how = s["how"]
            if how == "play":
                flush()
                if s["token"] == self._play_token:          # a newer Play now replaces this one
                    self.play_now.emit([s["path"]])
            elif how == "save":
                self._saved(s["path"])
            else:
                if how != kind:
                    flush()
                    kind = how
                run.append(s["path"])
        flush()

    def _saved(self, path: Path):
        self._saved_dirs.add(path.parent)
        if self.isVisible() and not self._suspended:
            self._saved_timer.start()
        else:
            self._flush_saved()

    def _flush_saved(self):
        dirs, self._saved_dirs = self._saved_dirs, set()
        for d in sorted(dirs):
            self.library_changed.emit(d)

    # jobs

    def _on_job(self, jid: str, st: dict):
        if jid not in self._how:                  # somebody else's job
            return
        self._jobs[jid] = st
        for old in [j for j, s in self._jobs.items() if s.get("state") not in ACTIVE][:-200]:
            self._jobs.pop(old, None)
            self._how.pop(old, None)
            self._done_at.pop(old, None)
        self._refresh_mark(item_key(st["item"]))
        if st.get("state") in ("done", "error", "cancelled"):
            self._job_ended(jid, st)
        self._strip_update()

    def _job_ended(self, jid: str, st: dict):
        path = None
        if st["state"] == "done":
            p = Path(st.get("path") or "")
            path = p if p.is_file() else None
            self._done_at[jid] = time.monotonic()
            self._protect_refresh()
            if path is None:
                self.status.emit(f"{(st.get('item') or {}).get('title') or 'That item'}: the fetched file is gone.", 6000)
        elif st["state"] == "cancelled":
            self._gone.add(jid)
        for chain, slot in self._want.pop(jid, []):
            chain.resolve(slot, path)
        if st["state"] == "error":
            title = (st.get("item") or {}).get("title") or "That item"
            self.status.emit(f"{title}: {st.get('error') or 'could not be fetched.'}", 8000)

    def _refresh_mark(self, key: tuple):
        busy, saved, ready, error, paths, quality = None, False, False, "", set(), ""
        for st in self._jobs.values():
            if item_key(st["item"]) != key:
                continue
            s = st.get("state")
            if s in ACTIVE:
                busy = st
            elif s == "done":
                error = ""
                if st.get("path"):
                    paths.add(str(st["path"]))
                saved = saved or st.get("dest") == "library"
                ready = ready or st.get("dest") != "library"
                quality = st.get("quality") or quality
            elif s == "error":
                error = st.get("error") or "Could not be fetched."
        fields = {}
        if busy is not None:
            fields.update(busy=True, percent=float(busy.get("percent") or 0.0), text=busy.get("text") or "Waiting",
                          indeterminate=bool(busy.get("indeterminate")))
        if saved or ready or error or paths:
            fields.update(saved=saved, ready=ready, error=error, paths=paths, quality=quality)
        for m in (self._model, self._chan.model, self._plist.model):
            m.mark(key, fields or None)

    def _job_action(self, jid: str, action: str):
        st = self._jobs.get(jid)
        if st is None:
            return
        if action == "sign_in":
            self._open_settings("accounts", (st.get("item") or {}).get("provider") or self._provider.id)
        elif action == "update":
            self._open_settings("tools")
            self._update()
        elif action == "retry":
            how, mode = self._how.get(jid, ("add", self._mode))
            self._gone.add(jid)
            self._do(how, [st["item"]], mode)

    def _dismiss_job(self, jid: str):
        self._gone.add(jid)
        self._strip_update()

    def _clear_finished(self):
        self._gone |= {j for j, s in self._jobs.items() if s.get("state") not in ACTIVE}
        self._svc.clear_finished()
        self._strip_update()

    def _strip_update(self):
        if self._closed or not hasattr(self, "_strip"):
            return
        shown = [dict(s) for j, s in self._jobs.items() if j not in self._gone]
        active = [s for s in shown if s.get("state") in ACTIVE]
        ended = [s for s in shown if s.get("state") not in ACTIVE]
        for s in ended:
            if s.get("state") == "done" and s.get("quality"):
                s["text"] = f"{s.get('text') or 'Done'}, {s['quality']}"
            if s.get("state") == "error":
                s["action"] = ERROR_ACTIONS.get(s.get("error_code", ""), "")
                s["action_label"] = action_label(s.get("error_code", ""))
        h = self.height()
        self._strip.set_jobs(active + ended, 0 if h < 200 else max(1, min(4, int(h * 0.25 // self._strip.row_h()))))
        self._strip_timer_sync()

    def _strip_timer_sync(self):
        pending = any(j in self._done_at and j not in self._gone for j in self._jobs)
        animating = any(s.get("state") in ACTIVE for s in self._jobs.values())
        if (pending or animating) and self.isVisible() and not self._suspended and not self._closed:
            if not self._strip_timer.isActive():
                self._strip_timer.start()
        else:
            self._strip_timer.stop()

    def _strip_tick(self):
        now = time.monotonic()
        for j, t in list(self._done_at.items()):
            if now - t > DONE_SHOWN:
                self._gone.add(j)
        self._strip_update()
        self._list.viewport().update()           # the pulse of a bar without a known length

    def _protect_paths(self) -> list[str]:
        """Called by the service from its own thread, so it only reads a list this thread keeps fresh."""
        return list(self._protected)

    def _protect_refresh(self):
        """What the cache must keep: the playing file, and every online file the player's queue or a playlist holds."""
        if self._closed:
            return
        p = self.now_playing_path()
        keep = [str(p)] if p else []
        root = getattr(self._svc, "media_dir", None)
        prefix = str(root) + os.sep if root is not None else ""
        try:
            held = [t.path for t in self._player.queue]
        except Exception:
            held = []
        try:
            held += list(self._keeper()) if self._keeper is not None else []
        except Exception:
            pass
        seen = set(keep)
        for q in held:
            q = str(q)
            if q not in seen and q.startswith(prefix):
                seen.add(q)
                keep.append(q)
        self._protected = keep

    def _on_track(self, _track):
        self._protect_refresh()
        for m in (self._model, self._chan.model, self._plist.model):
            m.set_playing(self._protected[0] if self._protected else "")

    # accounts, tools, settings

    def _open_settings(self, section: str = "", pid: str = ""):
        self._settings.refresh()
        self._open_overlay(SETTINGS)
        if section:
            self._settings.show_section(section, pid)

    def _close_settings(self):
        self._close_overlay()
        self._rebuild_chips()

    def _check(self, pid: str):
        if not self._gate(lambda: self._check(pid)):
            return
        rid = self._svc.check_login(pid)
        self._aux[rid] = ("login", pid)
        self._settings.set_check_busy(pid, True)

    def _login_check(self, pid: str, rid: int):
        self._aux[rid] = ("login", pid)             # the answer lands in the Accounts row like a press of Check
        self._settings.set_check_busy(pid, True)

    def _update(self):
        if not self._gate(self._update):
            return
        rid = self._svc.update_ytdlp()
        self._aux[rid] = ("update", "")
        self._settings.set_update_state(True)

    def _aux_done(self, aux: tuple, ok: bool, info: dict):
        what, pid = aux
        if what == "login":
            self._settings.set_check_busy(pid, False)
            if ok:
                text = info.get("note") or ("Signed in." if info.get("logged_in") else "Not signed in.")
            else:
                text = info.get("message") or "That did not work."
            text = self._login.note_check(pid, info.get("logged_in") if ok else None, text)
            self._settings.show_check(pid, text)
        elif what == "update":
            self._settings.set_update_state(False, info.get("message") or "")
            self._settings.refresh_tools()

    def _clear_cache(self):
        self._protect_refresh()
        keep = list(self._protected)
        self._settings.cache_use.setText("Clearing the cache.")
        self._bg.run(lambda: (self._svc.prune_cache(0, protect=keep), sum(1 for q in keep if os.path.isfile(q))), self._cache_cleared)

    def _cache_cleared(self, res, err):
        if err is None and isinstance(res, tuple):
            freed, kept = res
            text = f"Cleared {human_bytes(freed)} from the cache."
            if kept:
                text += f" Kept {plural(kept, 'file')} that {'is' if kept == 1 else 'are'} playing or in a playlist."
            self.status.emit(text, 6000)
        self._settings.refresh_cache()

    def _on_login_changed(self, pid: str):
        self._rebuild_chips()
        self._chan.login_changed(pid)
        self._plist.login_changed(pid)
        if self._overlay == SETTINGS:
            self._settings.refresh()
        # a list that failed for want of a sign in runs again now that there is one
        if self._failed is not None and pid == self._provider.id and self._signed_in(pid):
            req, self._failed = self._failed, None
            self._retry(req)

    def _on_tools(self):
        self._settings.refresh_tools()
        self._rebuild_chips()
        self._tools_banner()

    def _tools_banner(self):
        """One warning at a time, for the problems that make links stop working."""
        if not self._woke:
            return
        t = self._svc.tools()
        y, js = t.get("ytdlp", {}), t.get("js", {})
        if y.get("checking"):
            return
        key = msg = btn = ""
        if not y.get("ok"):
            key, msg, btn = "ytdlp", "yt-dlp was not found, so links cannot be opened.", "Get it"
        elif not self._tools_due:
            pass                             # a fresh install shows no warning about a feature nobody has used yet
        elif y.get("stale"):
            key, msg, btn = "stale", f"yt-dlp is {y.get('age_days')} days old. Updating often fixes broken links.", "Update"
        elif self._provider.id == "youtube" and not js.get("ok"):
            key, msg, btn = "js", "YouTube needs a JavaScript runtime. Install deno.", "Details"
        if key and key not in self._dismissed:
            self._tool_key = key
            self._tool_bar.show_message("warn", msg, [btn])
            self._tool_bar.show()
        else:
            self._tool_bar.hide()

    def _tool_clicked(self, _i: int):
        self._open_settings("tools")
        if self._tool_key in ("ytdlp", "stale"):
            self._update()

    def _tool_dismissed(self):
        self._dismissed.add(self._tool_key)
        self._tool_bar.hide()

    # the clipboard

    def _welcome_paste(self):
        text = (QGuiApplication.clipboard().text() or "").strip()
        if text and is_url(text):
            self._open(text, "show")
        else:
            self._focus_edit()

    def _recent_clicked(self, text: str):
        self._set_text(text)
        self._chip = "search"
        self._chips.set_current("search")
        self._submit(text)

    # dropping links on the panel

    @staticmethod
    def _dropped_link(mime) -> str:
        for u in mime.urls():
            if u.scheme() in ("http", "https"):
                return u.toString()
        text = (mime.text() or "").strip()
        return text if is_url(text) else ""

    def dragEnterEvent(self, e):
        if self._dropped_link(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._dropped_link(e.mimeData()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        url = self._dropped_link(e.mimeData())
        if url:
            e.acceptProposedAction()
            self.open_link(url)

