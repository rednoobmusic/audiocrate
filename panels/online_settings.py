"""The first-run notice and the settings page of the Online panel. Both are inline pages, not dialogs.

The wording of the notice lives here once, so the notice and Settings always agree. Bump
NOTICE_VERSION when the text changes in a way people should read again.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFontInfo, QPalette
from PySide6.QtWidgets import (QBoxLayout, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from browser_login import IDLE, LOGIN_URLS, WAITING
from library_widgets import PillButton, Scheme
from online_providers import ACCOUNT_NOTE, PROVIDERS
from online_service import BROWSERS, human_bytes
from online_widgets import Background, ElidedLabel, IconButton

NOTICE_VERSION = "1"
NOTICE_TITLE = "About Online"
NOTICE = (
    "Online fetches the audio from the site behind a link and plays it here. It uses yt-dlp, a separate program, "
    "and nothing goes through a server of ours.",
    "YouTube, X and most other sites say in their terms that you may not download from them except through their "
    "own apps. Some courts have ruled that getting around YouTube's download protection is unlawful, and the rules "
    "differ by country. You are responsible for what you fetch, so keep to music you have the right to hear and keep.",
    "If you sign in with your browser's cookies, the site sees these requests as coming from your account, and "
    "accounts used this way are sometimes limited or banned. A spare account is safer.",
    "Sites change without warning, so Online can stop working until yt-dlp is updated. The settings page, behind "
    "the gear button, has an Update button. Spotify, Apple Music and other copy-protected services are not supported.",
)
NOTICE_MORE = (
    "Fetching shows the site your IP address. Thumbnails come from the site's image servers. AudioCrate keeps a "
    "cache of what it fetched, which you can cap and clear on this page.",
    "Cookies are kept in a private folder inside your AudioCrate settings, and only the lines for the site they "
    "belong to. For Any link, a cookies file may hold at most two sites. AudioCrate never asks for or stores a password.",
)

BROWSER_NAMES = {"firefox": "Firefox", "chrome": "Chrome", "chromium": "Chromium", "brave": "Brave",
                 "vivaldi": "Vivaldi", "edge": "Edge", "opera": "Opera"}
AUDIO_FORMATS = (("Original, no conversion", "best"), ("Opus", "opus"), ("M4A (AAC)", "m4a"), ("MP3", "mp3"))
VIDEO_HEIGHTS = (360, 480, 720, 1080, 1440, 2160)
SIGNIN_NOTE = ("Opens your browser. Afterwards AudioCrate uses that browser's cookies for requests to this site "
               "only. It never sees your password.")
SIGNIN_OPTIONAL = {"x": "Optional. "}
SIGNIN_NO_YTDLP = "Browser sign in needs yt-dlp to read the cookies. Get it under Tools, further down."


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("settings_hint")
    lbl.setWordWrap(True)
    lbl.setForegroundRole(QPalette.PlaceholderText)          # dim under the system look too
    return lbl


def _section(form: QFormLayout, text: str):
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0 if form.rowCount() == 0 else 10, 0, 0)
    v.setSpacing(4)
    hdr = QLabel(text)
    hdr.setObjectName("settings_section")
    f = hdr.font()
    f.setBold(True)
    hdr.setFont(f)
    rule = QFrame()
    rule.setObjectName("settings_rule")
    rule.setFrameShape(QFrame.HLine)
    v.addWidget(hdr)
    v.addWidget(rule)
    form.addRow(box)


def _page_form(inner: QWidget) -> QFormLayout:
    form = QFormLayout(inner)
    form.setContentsMargins(12, 10, 12, 14)
    form.setHorizontalSpacing(10)
    form.setVerticalSpacing(8)
    form.setRowWrapPolicy(QFormLayout.WrapAllRows)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return form


def _scroll(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setWidget(inner)
    return sa


def _paragraphs(texts, small: bool = False) -> list[QLabel]:
    out = []
    for t in texts:
        lbl = QLabel(t)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if small:
            f = lbl.font()
            f.setPixelSize(max(9, round(QFontInfo(f).pixelSize() * 0.93)))
            lbl.setFont(f)
        out.append(lbl)
    return out


class NoticePage(QWidget):
    """The words about what Online does, shown before the first fetch.

    The buttons stay at the foot of the page while it is tall enough to read the text and see them. On
    a short panel they come after the text instead, and the page scrolls to them.
    """
    accepted = Signal(bool)         # True when the box "Do not show this again" is ticked
    declined = Signal()
    PIN_FROM = 380                  # page height from which the buttons are pinned

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        inner = QWidget()
        self._col = QVBoxLayout(inner)
        self._col.setContentsMargins(14, 12, 14, 12)
        self._col.setSpacing(8)
        head = QLabel(NOTICE_TITLE)
        head.setObjectName("settings_section")
        f = head.font()
        f.setBold(True)
        head.setFont(f)
        self._col.addWidget(head)
        for lbl in _paragraphs(NOTICE, small=True):
            self._col.addWidget(lbl)
        self._tail = QWidget()                      # holds the buttons wherever they are
        tl = QVBoxLayout(self._tail)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        self.remember = QCheckBox("Do not show this again")
        self.remember.setChecked(True)
        tl.addWidget(self.remember)
        self._row = QBoxLayout(QBoxLayout.LeftToRight)
        self._row.setSpacing(6)
        self.no = PillButton(sc, "Not now")
        self.yes = PillButton(sc, "I understand", primary=True)
        self.no.clicked.connect(self.declined)
        self.yes.clicked.connect(lambda: self.accepted.emit(self.remember.isChecked()))
        self._row.addWidget(self.no, 1)
        self._row.addWidget(self.yes, 1)
        tl.addLayout(self._row)
        self._scroll = _scroll(inner)
        self._lay.addWidget(self._scroll, 1)
        self._col.addWidget(self._tail)
        self._col.addStretch(1)
        self._pinned = False

    def _pin(self, on: bool):
        if on == self._pinned:
            return
        self._pinned = on
        if on:
            self._lay.addWidget(self._tail)
            self._tail.setContentsMargins(12, 6, 12, 10)
        else:
            self._col.insertWidget(self._col.count() - 1, self._tail)
            self._tail.setContentsMargins(0, 0, 0, 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._pin(self.height() >= self.PIN_FROM)
        want = QBoxLayout.TopToBottom if self.width() < 290 else QBoxLayout.LeftToRight
        if self._row.direction() != want:
            self._row.setDirection(want)

    def painted(self) -> list:
        return [self.no, self.yes]


class _Account(QWidget):
    """One site's sign in: where its cookies come from, a check, and a way to forget them."""

    def __init__(self, prov, page: "SettingsPage"):
        super().__init__()
        self.prov, self.page = prov, page
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        top = QHBoxLayout()
        name = QLabel(prov.name)
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        top.addWidget(name)
        top.addStretch(1)
        v.addLayout(top)
        self.state = _hint("")
        v.addWidget(self.state)
        self.login = page.login if page.login is not None and prov.cookie_domains and prov.id in LOGIN_URLS else None
        self.can_login = True
        if self.login is not None:
            srow = QHBoxLayout()
            srow.setSpacing(6)
            self.signin_btn = QPushButton()
            self.signin_btn.setMinimumWidth(1)              # the label shortens to fit instead of clipping
            self.done_btn = QPushButton("Done")
            self.cancel_btn = QPushButton("Cancel")
            for b in (self.signin_btn, self.done_btn, self.cancel_btn):
                srow.addWidget(b)
            srow.addStretch(1)
            v.addLayout(srow)
            self.note_ok = SIGNIN_OPTIONAL.get(prov.id, "") + SIGNIN_NOTE
            self.signin_note = _hint(self.note_ok)
            v.addWidget(self.signin_note)
            self.signin_btn.clicked.connect(lambda: self.login.start(prov.id))
            self.done_btn.clicked.connect(lambda: self.login.finish(prov.id))
            self.cancel_btn.clicked.connect(lambda: self.login.cancel(prov.id))
            self.login.changed.connect(self._login_changed)
            self.login.browser_changed.connect(self._relabel)
        self.browser = QComboBox()
        self.profile = QLineEdit()
        self.profile.setPlaceholderText("Profile, if you have more than one")
        if prov.cookie_domains:
            self.browser.addItem("No browser", "")
            for key in BROWSERS:
                self.browser.addItem(BROWSER_NAMES.get(key, key.title()), key)
            self.browser.setToolTip("Use the cookies of a browser you are already signed in with")
            self.browser.currentIndexChanged.connect(self._browser_changed)
            self.profile.editingFinished.connect(self._profile_changed)
            v.addWidget(self.browser)
            v.addWidget(self.profile)
        row = self._file_row = QBoxLayout(QBoxLayout.LeftToRight)
        row.setSpacing(6)
        self.file_btn = QPushButton("Cookies file")
        self.file_btn.setToolTip("Choose a cookies.txt file exported from your browser")
        self.check_btn = QPushButton("Check")
        self.forget_btn = QPushButton("Forget")
        for b in (self.file_btn, self.check_btn, self.forget_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(_hint(prov.login_note))
        self.file_btn.clicked.connect(self._pick_file)
        self.check_btn.clicked.connect(lambda: page.check_requested.emit(prov.id))
        self.forget_btn.clicked.connect(self._forget)

    def refresh(self):
        src = self.page.svc.cookie_source(self.prov.id)
        self.browser.blockSignals(True)
        self.profile.blockSignals(True)
        self.browser.setCurrentIndex(max(0, self.browser.findData(src["browser"])) if src["kind"] == "browser" else 0)
        self.profile.setText(src["profile"] if src["kind"] == "browser" else "")
        self.browser.blockSignals(False)
        self.profile.blockSignals(False)
        self.profile.setVisible(src["kind"] == "browser")
        if src["kind"] == "browser":
            who = BROWSER_NAMES.get(src["browser"], src["browser"])
            self.state.setText(f"Using the cookies of {who}" + (f", profile {Path(src['profile']).name}." if src["profile"] else "."))
        elif src["kind"] == "file":
            self.state.setText("Using a cookies file.")
        else:
            self.state.setText("Not signed in.")
        signed = src["kind"] != "none"
        self.check_btn.setEnabled(signed)
        self.forget_btn.setEnabled(signed)
        if self.login is not None:
            step = self.login.state(self.prov.id)
            if step != IDLE:
                self.state.setText(self.login.text(self.prov.id))
            self.signin_btn.setVisible(step == IDLE)
            self.signin_btn.setEnabled(self.can_login)
            self.done_btn.setVisible(step != IDLE)
            self.cancel_btn.setVisible(step != IDLE)
            self.done_btn.setEnabled(step == WAITING)
            self.cancel_btn.setEnabled(step == WAITING)
            self._relabel()

    def say(self, text: str):
        self.state.setText(text)

    def set_support(self, ok: bool):
        """Browser sign in reads the cookies through yt-dlp, so it is off while yt-dlp is missing."""
        if self.login is not None:
            self.can_login = ok
            self.signin_btn.setEnabled(ok)
            self.signin_note.setText(self.note_ok if ok else SIGNIN_NO_YTDLP)

    def _login_changed(self, pid: str):
        if pid != self.prov.id:
            return
        self.refresh()
        text = self.login.text(pid)
        if text and self.login.state(pid) == IDLE:
            self.say(text)

    def _relabel(self):
        """The button names the browser when it is known and gives way to shorter words when the row is narrow."""
        if self.login is None:
            return
        b = self.login.browser()
        signed = self.login.signed_in(self.prov.id) and self.page.svc.cookie_source(self.prov.id)["kind"] != "none"
        full = ("Sign in again" if signed else f"Sign in with {b.name}" if b.key else "Sign in with your browser")
        btn = self.signin_btn
        fm = btn.fontMetrics()
        pad = btn.sizeHint().width() - fm.horizontalAdvance(btn.text())
        room = self.width() - 2
        text = next((t for t in (full, "Sign in with browser", "Sign in") if fm.horizontalAdvance(t) + pad <= room), "Sign in")
        if btn.text() != text:
            btn.setText(text)
        btn.setToolTip(f"{full}. Opens the site's sign in page in your default browser")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relabel()
        QTimer.singleShot(0, self, self._restack)           # not during the resize: the page would keep the old height

    def _restack(self):
        """The three buttons go one under the other when they do not fit in a row."""
        need = sum(b.sizeHint().width() for b in (self.file_btn, self.check_btn, self.forget_btn)) + 2 * self._file_row.spacing()
        want = QBoxLayout.TopToBottom if need > self.width() else QBoxLayout.LeftToRight
        if self._file_row.direction() != want:
            self._file_row.setDirection(want)
        form = self.parentWidget().layout() if self.parentWidget() is not None else None
        if form is not None and self.height() < self.heightForWidth(self.width()):
            form.invalidate()                               # the form keeps the old height of this row otherwise

    def _apply(self, kind: str, **kw):
        self.page.set_source(self.prov.id, kind, **kw)

    def _browser_changed(self, _i: int):
        key = self.browser.currentData()
        if key:
            self._apply("browser", browser=key, profile=self.profile.text().strip())
        else:
            self._apply("none")

    def _profile_changed(self):
        key = self.browser.currentData()
        if key:
            self._apply("browser", browser=key, profile=self.profile.text().strip())

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a cookies file", str(Path.home()),
                                              "Cookies (*.txt);;All files (*)")
        if path:
            self._apply("file", file=path)

    def _forget(self):
        self.page.forget_login(self.prov.id)


class SettingsPage(QWidget):
    """Accounts, what is fetched and kept, and the tools Online depends on. Every control writes through at once."""
    done = Signal()
    check_requested = Signal(str)           # provider id
    update_requested = Signal()
    clear_cache_requested = Signal()
    note = Signal(str, int)                 # a line for the status bar

    def __init__(self, svc, bg: Background, sc: Scheme, login=None, parent=None):
        super().__init__(parent)
        self.svc, self.bg, self.sc, self.login = svc, bg, sc, login
        self._busy_update = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(8, 6, 8, 6)
        head.setSpacing(2)
        self._back = IconButton(sc, "back")
        self._back.setToolTip("Back to the list")
        self._back.setAccessibleName("Back")
        self._back.clicked.connect(self.done)
        self._title = ElidedLabel(sc, bold=True)
        self._title.setText("Online settings")
        head.addWidget(self._back)
        head.addWidget(self._title, 1)
        hw = QWidget()
        hw.setLayout(head)
        lay.addWidget(hw)
        inner = QWidget()
        form = _page_form(inner)
        self._build(form)
        self._scroll_area = _scroll(inner)
        lay.addWidget(self._scroll_area, 1)

    # layout

    def _build(self, form: QFormLayout):
        _section(form, "Accounts")
        self.accounts = {}
        for prov in PROVIDERS.values():
            acc = _Account(prov, self)
            self.accounts[prov.id] = acc
            form.addRow(acc)
        form.addRow(_hint(ACCOUNT_NOTE))

        _section(form, "What to fetch")
        self.audio_fmt = QComboBox()
        for label, key in AUDIO_FORMATS:
            self.audio_fmt.addItem(label, key)
        self.audio_fmt.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)     # its longest name must not set the page width
        self.audio_fmt.setMinimumContentsLength(8)
        self.audio_fmt.currentIndexChanged.connect(lambda _i: self._put("audio_format", self.audio_fmt.currentData()))
        form.addRow("Audio format", self.audio_fmt)
        self.video_h = QComboBox()
        for h in VIDEO_HEIGHTS:
            self.video_h.addItem(f"{h}p", h)
        self.video_h.currentIndexChanged.connect(lambda _i: self._put("video_height", self.video_h.currentData()))
        form.addRow("Video quality", self.video_h)
        form.addRow(_hint("Video sound uses the EQ, Enhance and DSP (Settings, Playback)."))
        self.max_min = QSpinBox()
        self.max_min.setRange(0, 1440)
        self.max_min.setSuffix(" min")
        self.max_min.setSpecialValueText("No limit")
        self.max_min.setKeyboardTracking(False)             # typed digits count when Enter is pressed, not one by one
        self.max_min.setToolTip("Audio is kept in memory while it plays, so longer items are not played from a link. Saving has no limit")
        self.max_min.valueChanged.connect(lambda v: self._put("max_minutes", v))
        form.addRow("Longest audio", self.max_min)
        self.square = QCheckBox("Square cover art")           # the longer wording made the page wider than a 240 px panel
        self.square.setToolTip("Crop cover art to a square")
        self.square.toggled.connect(lambda on: self._put("square_art", on))
        form.addRow(self.square)
        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 8)
        self.concurrent.setKeyboardTracking(False)
        self.concurrent.valueChanged.connect(lambda v: self._put("concurrent", v))
        form.addRow("Fetches at once", self.concurrent)
        self.rate = QLineEdit()
        self.rate.setPlaceholderText("No limit, or 500K or 2M")
        self.rate.editingFinished.connect(lambda: self._put("rate_limit", self.rate.text().strip()))
        form.addRow("Speed limit", self.rate)

        _section(form, "Storage")
        row = QHBoxLayout()
        row.setSpacing(6)
        self.save_dir = QLineEdit()
        self.save_dir.setReadOnly(True)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row.addWidget(self.save_dir, 1)
        row.addWidget(browse)
        form.addRow("Save folder", row)
        self.cache_limit = QSpinBox()
        self.cache_limit.setRange(64, 1_000_000)
        self.cache_limit.setSingleStep(256)
        self.cache_limit.setSuffix(" MB")
        self.cache_limit.editingFinished.connect(lambda: self._put("cache_max_mb", self.cache_limit.value()))
        form.addRow("Cache limit", self.cache_limit)
        crow = QHBoxLayout()
        crow.setSpacing(6)
        self.cache_use = _hint("")
        self.clear_btn = QPushButton("Clear cache")
        self.clear_btn.clicked.connect(self.clear_cache_requested)
        crow.addWidget(self.cache_use, 1)
        crow.addWidget(self.clear_btn)
        form.addRow(crow)

        _section(form, "Tools")
        self.ytdlp = _hint("")
        form.addRow("yt-dlp", self.ytdlp)
        urow = QHBoxLayout()
        urow.setSpacing(6)
        self.update_btn = QPushButton("Update yt-dlp")
        self.update_btn.clicked.connect(self.update_requested)
        self.channel = QComboBox()
        self.channel.addItem("Nightly builds", "nightly")
        self.channel.addItem("Stable releases", "stable")
        self.channel.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.channel.setMinimumContentsLength(6)
        self.channel.setToolTip("Which yt-dlp builds Update installs. Nightly gets fixes first.")
        self.channel.currentIndexChanged.connect(lambda _i: self._put("channel", self.channel.currentData()))
        urow.addWidget(self.update_btn)
        urow.addWidget(self.channel, 1)
        form.addRow(urow)
        self.update_note = _hint("")
        form.addRow(self.update_note)
        self.ffmpeg = _hint("")
        form.addRow("ffmpeg", self.ffmpeg)
        self.js = _hint("")
        form.addRow("JavaScript", self.js)
        self.gdl = _hint("")
        form.addRow("gallery-dl", self.gdl)

        _section(form, NOTICE_TITLE)
        for lbl in _paragraphs(NOTICE + NOTICE_MORE):
            form.addRow(lbl)

    # reading

    def refresh(self):
        s = self.svc.settings()
        for w in (self.audio_fmt, self.video_h, self.max_min, self.square, self.concurrent, self.cache_limit, self.channel,
                  self.rate):
            w.blockSignals(True)
        self.audio_fmt.setCurrentIndex(max(0, self.audio_fmt.findData(s.get("audio_format", "best"))))
        h = int(s.get("video_height", 1080))
        self.video_h.setCurrentIndex(min(range(len(VIDEO_HEIGHTS)), key=lambda i: abs(VIDEO_HEIGHTS[i] - h)))
        self.max_min.setValue(int(s.get("max_minutes", 180)))
        self.square.setChecked(bool(s.get("square_art", True)))
        self.concurrent.setValue(int(s.get("concurrent", 4)))
        self.cache_limit.setValue(int(s.get("cache_max_mb", 2048)))
        self.channel.setCurrentIndex(max(0, self.channel.findData(s.get("channel", "nightly"))))
        self.rate.setText(str(s.get("rate_limit", "")))
        for w in (self.audio_fmt, self.video_h, self.max_min, self.square, self.concurrent, self.cache_limit, self.channel,
                  self.rate):
            w.blockSignals(False)
        self.save_dir.setText(str(s.get("save_dir", "")))
        self.save_dir.setToolTip(str(s.get("save_dir", "")))
        if self.login is not None:
            self.login.refresh_browser()
        for acc in self.accounts.values():
            acc.refresh()
        self.refresh_tools()
        self.refresh_cache()

    def refresh_tools(self):
        t = self.svc.tools()
        y = t.get("ytdlp", {})
        if y.get("checking"):
            self.ytdlp.setText("Checking.")
        elif y.get("ok"):
            who = ("from your system" if y.get("owned_by_system") and not y.get("owned")
                   else "kept up to date by AudioCrate" if y.get("owned") else "")
            age = y.get("age_days")
            bits = [y.get("version", ""), f"{age} days old" if age is not None else "", who]
            text = ", ".join(b for b in bits if b) + "."
            if y.get("stale"):
                text += " It is getting old. Updating often fixes links that stopped working."
            self.ytdlp.setText(text)
        else:
            self.ytdlp.setText(y.get("note") or "yt-dlp was not found.")
        for acc in self.accounts.values():
            acc.set_support(bool(y.get("ok") or y.get("checking")))
        self.update_btn.setText("Update yt-dlp" if y.get("ok") else "Get yt-dlp")
        self.update_btn.setEnabled(not self._busy_update)
        f, j, g = t.get("ffmpeg", {}), t.get("js", {}), t.get("gallery_dl", {})
        self.ffmpeg.setText(f"Version {f['version']}." if f.get("ok") else (f.get("note") or "Checking."))
        self.js.setText(f"{j.get('name', '')} {j.get('version', '')}.".replace("  ", " ") if j.get("ok") else (j.get("note") or "Checking."))
        self.gdl.setText(f"Version {g['version']}. Used for X profiles and bookmarks." if g.get("ok") else (g.get("note") or "Checking."))

    def refresh_cache(self):
        self.cache_use.setText("Measuring the cache.")
        self.bg.run(self.svc.cache_info, self._cache_info)

    def _cache_info(self, info, err):
        if err is not None or not isinstance(info, dict):
            self.cache_use.setText("The cache could not be measured.")
            return
        n = info.get("files", 0)
        self.cache_use.setText(f"Using {human_bytes(info.get('bytes', 0))} for {n} item{'' if n == 1 else 's'}.")

    # writing

    def _put(self, key: str, value):
        try:
            self.svc.set_setting(key, value)
        except ValueError as e:
            self.note.emit(str(e), 6000)
            self.refresh()

    def _browse(self):
        start = self.save_dir.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Save folder", start)
        if path:
            self._put("save_dir", path)
            self.save_dir.setText(path)
            self.save_dir.setToolTip(path)

    def set_source(self, pid: str, kind: str, **kw):
        """Choose where a site's cookies come from. The service reads and copies files, so it runs off the GUI thread."""
        def work():
            self.svc.set_cookie_source(pid, kind, **kw)
        self.bg.run(work, lambda _r, err: self._source_done(pid, err))

    def _source_done(self, pid: str, err):
        acc = self.accounts.get(pid)
        if acc is None:
            return
        acc.refresh()
        if isinstance(err, ValueError):
            acc.say(str(err))
        elif err is not None:
            acc.say("That did not work.")

    def forget_login(self, pid: str):
        self.set_source(pid, "none")

    # results the panel hands back

    def show_check(self, pid: str, text: str):
        acc = self.accounts.get(pid)
        if acc is not None:
            acc.say(text)

    def set_check_busy(self, pid: str, busy: bool):
        acc = self.accounts.get(pid)
        if acc is not None:
            acc.check_btn.setEnabled(not busy)
            if busy:
                acc.say("Checking.")

    def set_update_state(self, busy: bool, text: str = ""):
        self._busy_update = busy
        self.update_btn.setEnabled(not busy)
        self.update_note.setText("Getting yt-dlp." if busy else text)

    def show_section(self, which: str, pid: str = ""):
        """Scroll to Accounts (at the site's row) or to Tools."""
        target = self.accounts.get(pid) if which == "accounts" and pid in self.accounts else (
            self.ytdlp if which == "tools" else None)
        self._scroll_area.verticalScrollBar().setValue(0)
        if target is not None:
            self._scroll_area.ensureWidgetVisible(target, 0, 20)

    def painted(self) -> list:
        return [self._back, self._title]

