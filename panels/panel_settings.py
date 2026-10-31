from __future__ import annotations
import json
import sys
import safe_write
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QAbstractButton,
    QStackedWidget, QButtonGroup, QGridLayout, QLabel, QSlider, QCheckBox,
    QScrollArea, QFrame,
    QSizePolicy, QPushButton, QFileDialog, QComboBox,
    QMessageBox, QFontComboBox, QLineEdit, QColorDialog, QPlainTextEdit,
)
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaDevices

from artists import DEFAULT_EXCEPTIONS, configure as configure_artists
from player import device_key
import anim
import plat_paths
import theme_presets
from theme_picker import ThemePicker

_CFG_FILE = plat_paths.config_dir() / "settings.json"

# Blank by default: Discord Rich Presence is opt-in, and each user supplies their
# own Discord application ID and art base URL in Settings.
_DEFAULT_DISCORD_CLIENT_ID    = ""
_DEFAULT_DISCORD_ART_BASE_URL = ""

_DEFAULTS = {
    "viz_art_opacity":   28,
    "viz_show_art":      True,
    "viz_peak_hold":     True,
    "viz_bar_count":     128,
    "viz_bar_gap":       2,
    "viz_corner_r":      3,
    "viz_fft_enabled":   True,
    "viz_fft_gain":      100,
    "osc_window_ms":     40,
    "osc_gain":          100,
    "osc_smooth":        1,
    "osc_line_width":    15,
    "osc_trigger":       True,
    "osc_fps":           60,           # 241 = unlimited
    "osc_vsync":         False,
    "pb_fade_steps":     2,
    "pb_fade_interval":  5,
    "lyr_font_size":     18,
    "art_color_theme":   False,
    "accent_color":      "#8040c8",
    "bg_color":          "",          # "" = auto (tinted from the accent)
    "text_color":        "",          # "" = auto (default foreground)
    "frame_shade":       0,
    "panel_borders":     True,
    "corner_radius":     4,           # px, buttons, fields, menus and tooltips of AudioCrate's own theme
    "theme_style":       "default",    # or "aero"
    "waterfall":         {},           # the Waterfall panel's own settings (panel_waterfall.DEFAULTS)
    "waterfall_presets": {},
    "crossfade_ms":      0,
    "video_dsp":         True,
    "video_delay_ms":    0,            # -200..200: positive plays the sound of videos later, negative holds the picture back
    "discord_rpc_enabled":  False,
    "discord_client_id":    _DEFAULT_DISCORD_CLIENT_ID,
    "discord_art_base_url": _DEFAULT_DISCORD_ART_BASE_URL,
    "discord_art_upload_cmd": "",
    "audio_device_id":   "",
    "docking_enabled":   True,
    "viz_auto_suspend":  True,
    "animations":        True,
    "lastfm_enabled":    False,
    "lastfm_api_key":    "",
    "lastfm_api_secret": "",
    "librefm_enabled":    False,
    "librefm_api_key":    "",
    "librefm_api_secret": "",
    "play_stats_enabled": False,
    "keep_output_open":  False,
    "text_font":         "",
    "ui_size":           13,
    "system_theme":      True,
    "custom_titlebar":   True,
    "rgb_enabled":       False,
    "rgb_style":         "Spectrum",   # Spectrum, Pulse or Steady
    "rgb_keyboard":      True,
    "rgb_emblem":        True,
    "rgb_mode_led":      False,
    "rgb_openrgb":       False,
    "rgb_openrgb_host":  "127.0.0.1:6742",
    "rgb_openrgb_exclude": "predator, acer",
    "rgb_brightness":    100,
    "rgb_fps":           12,
    "tour_seen":         0,            # the version of the first run tour the user has been through (ui/tour_dialog.py)
    "debug_logs":        False,        # DEBUG lines in the log file (applog.set_detailed)
    "artist_split_commas": True,        # "A, B" in an artist tag is two artists
    "artist_split_amp":  True,         # and so is "A & B"
    "artist_split_slash": False,        # "A/B" is one name unless this is on (ID3v2.3 lists several artists that way)
    "artist_exceptions": "\n".join(DEFAULT_EXCEPTIONS),     # names that stay whole, one per line
}

_SECTIONS = ("Playback", "Visuals", "Appearance", "Connections", "System")
_RGB_STYLES = ("Spectrum", "Pulse", "Steady")


def _hslider(lo, hi, val, suffix="", tip="", fmt=None):
    sl  = QSlider(Qt.Horizontal)
    sl.setRange(lo, hi)
    sl.setValue(val)
    if tip:
        sl.setToolTip(tip)
    fmt = fmt or (lambda v: f"{v}{suffix}")
    lbl = QLabel(fmt(val))
    lbl.setMinimumWidth(52)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    sl.valueChanged.connect(lambda v: lbl.setText(fmt(v)))
    return sl, lbl


def _row(sl, lbl):
    h = QHBoxLayout()
    h.setSpacing(8)
    h.addWidget(sl, 1)
    h.addWidget(lbl)
    return h


_LABEL_W = 108  # shared label column width, so every tab lines up


_DISCORD_LINKS = (("Create an app in the Discord Developer Portal", "https://discord.com/developers/applications"),
                  ("Discord's Rich Presence guide", "https://docs.discord.com/developers/platform/rich-presence"))

_INLINE_MAX = 30    # a hint longer than this sits behind a ? button instead of under its setting


class _HelpPopup(QFrame):
    """The long explanation of a setting, in a small bubble that closes on the next click or on Esc."""

    def __init__(self, text: str, anchor: QWidget, links=()):
        super().__init__(anchor.window(), Qt.Popup)
        self.setObjectName("settings_help")
        self.setFrameShape(QFrame.StyledPanel)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAutoFillBackground(True)
        lbl = QLabel(plat_paths.breakable(text))
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.PlainText)
        lbl.setFixedWidth(300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        lay.addWidget(lbl)
        lbl.ensurePolished()
        link = "#8ab4f8" if lbl.palette().windowText().color().lightness() > 128 else "#1a5fb4"    # readable on dark and light
        for label, url in links:                    # pages that say more, opened in the browser
            a = QLabel(f'<a href="{url}"><span style="color:{link}">{label}</span></a>')
            a.setTextFormat(Qt.RichText)
            a.setWordWrap(True)
            a.setFixedWidth(300)
            a.setCursor(Qt.PointingHandCursor)
            a.setToolTip(url)
            a.linkActivated.connect(self._go)
            lay.addWidget(a)
        self.adjustSize()
        screen = anchor.screen().availableGeometry()
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        x = max(screen.left() + 4, min(below.x() - 12, screen.right() - self.width() - 4))
        y = below.y()
        if y + self.height() > screen.bottom():                    # no room under the button: above it
            y = anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4
        self.move(x, max(screen.top() + 4, y))

    def _go(self, url: str):
        QDesktopServices.openUrl(QUrl(url))
        self.close()


class _HelpButton(QAbstractButton):
    """A small ? that opens the long explanation of the setting next to it."""

    def __init__(self, text: str, parent=None, links=()):
        super().__init__(parent)
        self._text = text
        self._links = tuple(links)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setToolTip("What is this?")
        self.setAccessibleName("Help")
        self.setAccessibleDescription(text)
        self.clicked.connect(self._open)

    def _open(self):
        _HelpPopup(self._text, self, self._links).show()

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fg = QColor(self.palette().windowText().color())
        fg.setAlpha(230 if (self.underMouse() or self.hasFocus() or self.isDown()) else 140)
        p.setPen(QPen(fg, 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5))
        f = self.font()
        f.setBold(True)
        f.setPixelSize(11)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, "?")


class _Arrow(QWidget):
    """The triangle in front of a section that can be folded: down when it is open, right when it is closed."""

    def __init__(self, is_open: bool):
        super().__init__()
        self.open = is_open
        self.setFixedSize(10, 10)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fg = QColor(self.palette().windowText().color())
        fg.setAlpha(170)
        p.setPen(Qt.NoPen)
        p.setBrush(fg)
        pts = ((1, 2.5), (9, 2.5), (5, 7.5)) if self.open else ((2.5, 1), (2.5, 9), (7.5, 5))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))


class _SectionHead(QWidget):
    """A section's title with an optional ? at the right. A foldable one has an arrow in front, shows or hides its
    body when it is clicked (or Space or Enter is pressed on it) and says so through `toggled`."""
    toggled = Signal(bool)

    def __init__(self, text: str, help_text: str = "", fold: bool | None = None, minor: bool = False, links=()):
        super().__init__()
        self._fold = fold is not None
        self._arrow = _Arrow(not fold) if self._fold else None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        if self._arrow is not None:
            lay.addWidget(self._arrow)
            self.setCursor(Qt.PointingHandCursor)
            self.setFocusPolicy(Qt.TabFocus)
            self.setAccessibleName(text)
        hdr = QLabel(text)
        if not minor:                               # a minor head is plain text, so it reads as part of the section above
            hdr.setObjectName("settings_section")
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
        hdr.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(hdr)
        lay.addStretch(1)
        if help_text:
            lay.addWidget(_HelpButton(help_text, links=links))

    def _flip(self):
        self._arrow.open = not self._arrow.open
        self._arrow.update()
        self.toggled.emit(self._arrow.open)

    def mousePressEvent(self, e):
        if self._fold and e.button() == Qt.LeftButton:
            self._flip()

    def keyPressEvent(self, e):
        if self._fold and e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._flip()
        else:
            super().keyPressEvent(e)


def _field(form, text, field, help="", links=()):
    """A labelled row; field is a widget or a layout. With help, a ? sits after the label."""
    lbl = QLabel(text)
    lbl.setMinimumWidth(_LABEL_W)
    if help:
        cell = QWidget()
        h = QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(lbl)
        h.addWidget(_HelpButton(help, links=links))
        h.addStretch(1)
        lbl = cell
    form.addRow(lbl, field)
    return lbl


def _new_form(w: QWidget, margins=(14, 12, 14, 14)) -> QFormLayout:
    form = QFormLayout(w)
    form.setContentsMargins(*margins)
    form.setHorizontalSpacing(10)
    form.setVerticalSpacing(8)
    form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return form


def _section(form, text, help="", fold=None, minor=False):
    """Heading with a thin rule under it; the object names are styled in theme.py. With fold (True: closed at first,
    False: open) the section can be folded: what is added to the form this returns lands in its body, so the caller
    carries on with `form = _section(...)`. Otherwise the form is returned as it was. A minor one is a plain
    "Advanced" row that belongs to the section above: no rule, no bold heading, no gap."""
    form = getattr(form, "_page_form", form)
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0 if (minor or form.rowCount() == 0) else 10, 0, 0)
    v.setSpacing(4)
    head = _SectionHead(text, help, fold, minor)
    v.addWidget(head)
    if not minor:
        rule = QFrame()
        rule.setObjectName("settings_rule")
        rule.setFrameShape(QFrame.HLine)
        v.addWidget(rule)
    form.addRow(box)
    if fold is None:
        return form
    body = QWidget()
    sub = _new_form(body, (0, 0, 0, 0))
    sub._page_form = form
    body.setVisible(not fold)
    head.toggled.connect(body.setVisible)
    form.addRow(body)
    return sub


def _hint(text):
    lbl = QLabel(plat_paths.breakable(text))
    lbl.setObjectName("settings_hint")
    lbl.setWordWrap(True)
    lbl.setForegroundRole(QPalette.PlaceholderText)   # dim under the system look too
    return lbl


def _check(chk, hint="", links=()):
    """A checkbox spanning the row. A short hint (or a label that changes, such as a status) sits tucked under it,
    a long one is behind a ? at the end of the row."""
    if not hint:
        return chk
    box = QWidget()
    if isinstance(hint, str) and (len(hint) > _INLINE_MAX or links):
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(chk, 1)
        h.addWidget(_HelpButton(hint, links=links))
        return box
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    v.addWidget(chk)
    v.addWidget(hint if isinstance(hint, QLabel) else _hint(hint))
    return box


def _contrast(hex_color: str) -> str:
    """Readable text colour (black/white) for a swatch of the given background."""
    from PySide6.QtGui import QColor
    c = QColor(hex_color)
    lum = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
    return "#000000" if lum > 0.55 else "#ffffff"


class _LiveLabel(QLabel):
    """A label that says when it is on screen and when it is not, so what feeds it can sleep while it is hidden."""
    shown = Signal()
    hidden = Signal()

    def showEvent(self, e):
        super().showEvent(e)
        self.shown.emit()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.hidden.emit()


class SettingsWidget(QWidget):
    visualizer_changed   = Signal(dict)
    oscilloscope_changed = Signal(dict)
    waterfall_requested  = Signal()      # open the Waterfall panel's settings
    playback_changed     = Signal(dict)
    nowplaying_changed   = Signal(dict)
    lyrics_changed       = Signal(dict)
    appearance_changed   = Signal(dict)
    theme_style_changed  = Signal(dict)   # accent / background / text / frame-shade
    font_changed         = Signal(str)   # emits font family name
    ui_size_changed      = Signal(int)   # emits px size
    system_theme_changed = Signal(bool)
    crossfade_changed    = Signal(int)    # crossfade length in ms (0 = off)
    video_dsp_changed    = Signal(bool)   # videos go through the EQ, Enhance and DSP chain
    video_delay_changed  = Signal(int)    # video sound delay in ms, -200..200, sent while the slider moves
    discord_changed      = Signal(dict)
    layout_export        = Signal(object)
    layout_import        = Signal(object)
    config_export        = Signal(object)   # Path
    config_import        = Signal(object)   # Path
    audio_device_changed = Signal(object)
    keep_output_open_changed = Signal(bool)
    docking_changed      = Signal(bool)
    viz_auto_suspend_changed = Signal(bool)
    scrobble_config_changed = Signal(str, dict)   # (service name, {enabled, api_key, api_secret})
    scrobble_connect        = Signal(str)         # service name, start browser auth
    scrobble_disconnect     = Signal(str)         # service name, forget session
    play_stats_changed      = Signal(bool)
    artist_split_changed    = Signal()       # how artist names are split changed
    rgb_changed             = Signal(dict)   # lighting settings, unprefixed keys
    tour_requested          = Signal()       # the "Show the tour" button

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = self._load()
        self._vlag_source = None
        self._vlag_timer = QTimer(self)
        self._vlag_timer.setInterval(500)
        self._vlag_timer.timeout.connect(self._vlag_tick)
        self._build()

    def _load(self) -> dict:
        cfg = dict(_DEFAULTS)
        try:
            saved = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in saved.items() if k in cfg})
        except Exception:
            pass
        return cfg

    def save(self):
        try:
            _CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
            safe_write.write_text(_CFG_FILE, json.dumps(self._cfg, indent=2))
        except Exception:
            from applog import log
            log.exception("failed to save settings to %s", _CFG_FILE)

    def get(self, key, default=None):
        return self._cfg.get(key, default)

    def set(self, key, value):
        self._cfg[key] = value
        self.save()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Section buttons in a grid that never scrolls: 3 + 2 when narrow, one row when wide.
        # The pages live in _tabs (MainWindow calls _tabs.setCurrentIndex(1) for Visuals).
        self._tabs = QStackedWidget()
        pages = (self._tab_playback, self._tab_visuals, self._tab_appearance,
                 self._tab_connections, self._tab_system)
        nav = QWidget()
        self._nav = QGridLayout(nav)
        self._nav.setContentsMargins(10, 10, 10, 8)
        self._nav.setSpacing(4)
        self._nav_group = QButtonGroup(self)
        self._nav_btns = []
        for i, (name, build) in enumerate(zip(_SECTIONS, pages)):
            self._tabs.addWidget(build())
            b = QPushButton(name)
            b.setObjectName("settings_nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _, i=i: self._tabs.setCurrentIndex(i))
            self._nav_group.addButton(b, i)
            self._nav_btns.append(b)
        self._nav_wide = None
        self._place_nav(False)
        self._nav_btns[0].setChecked(True)
        self._tabs.currentChanged.connect(lambda i: self._nav_btns[i].setChecked(True))

        rule = QFrame()
        rule.setObjectName("settings_rule")
        rule.setFrameShape(QFrame.HLine)
        root.addWidget(nav)
        root.addWidget(rule)
        root.addWidget(self._tabs, 1)

    def _place_nav(self, wide: bool):
        if wide == self._nav_wide:
            return
        self._nav_wide = wide
        for b in self._nav_btns:
            self._nav.removeWidget(b)
        spans = ((6,) * 5,) if wide else ((10,) * 3, (15,) * 2)   # 30 columns split evenly
        it = iter(self._nav_btns)
        for r, row in enumerate(spans):
            col = 0
            for span in row:
                self._nav.addWidget(next(it), r, col, 1, span)
                col += span

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_nav(e.size().width() >= 520)

    def show_section(self, name: str):
        """Show a section by name: Playback, Visuals, Appearance, Connections or System."""
        names = [n.lower() for n in _SECTIONS]
        if name.lower() in names:
            self._tabs.setCurrentIndex(names.index(name.lower()))

    def _scroll_wrap(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        return sa

    def _page(self):
        """An empty tab page: a widget plus its form layout."""
        w = QWidget()
        return w, _new_form(w)

    def _tab_playback(self) -> QWidget:
        w, form = self._page()
        self._sec_volume(form)
        self._sec_output(form)
        self._sec_stats(form)
        return self._scroll_wrap(w)

    def _sec_volume(self, form):
        c = self._cfg
        _section(form, "Sound")

        # Crossfade between consecutive audio tracks (0 = off, gapless).
        self._xfade_sl, self._xfade_lbl = _hslider(
            0, 12, c.get("crossfade_ms", 0) // 1000,
            fmt=lambda v: "Off" if v == 0 else f"{v} s")
        self._xfade_sl.setToolTip("Overlap the end of one track with the start of the next.")
        self._xfade_sl.sliderReleased.connect(self._apply_crossfade)
        _field(form, "Crossfade", _row(self._xfade_sl, self._xfade_lbl))

        self._video_dsp_chk = QCheckBox("Use the EQ and DSP on video sound")
        self._video_dsp_chk.setChecked(c.get("video_dsp", True))
        self._video_dsp_chk.toggled.connect(self._apply_video_dsp)
        form.addRow(_check(self._video_dsp_chk,
            "Plays the sound of video files through the same equalizer, Enhance and DSP chain as music. "
            "The sound runs about 50 ms behind the picture, and the delay below moves it. If it cannot be "
            "kept in step, the video goes back to its own sound. Applies at once, also to a video that is playing."))

        delay = max(-200, min(200, int(c.get("video_delay_ms", 0))))
        self._vdelay_sl, self._vdelay_lbl = _hslider(
            -200, 200, delay, fmt=lambda v: "0 ms" if v == 0 else f"{v:+d} ms")
        self._vdelay_sl.setSingleStep(1)
        self._vdelay_sl.setPageStep(10)
        self._vdelay_sl.valueChanged.connect(self._apply_video_delay)
        self._vdelay_sl.sliderReleased.connect(self.save)
        _field(form, "Video sound delay", _row(self._vdelay_sl, self._vdelay_lbl),
               help="Moves the sound of videos against the picture. Positive plays the sound later, negative holds "
                    "the picture back instead, since the sound cannot come before it. Applies while the video plays. "
                    "The sound card has a delay of its own that is not counted.")
        self._vlag_lbl = _LiveLabel()
        self._vlag_lbl.setObjectName("settings_hint")
        self._vlag_lbl.setWordWrap(True)
        self._vlag_lbl.setForegroundRole(QPalette.PlaceholderText)
        self._vlag_lbl.shown.connect(self._vlag_start)
        self._vlag_lbl.hidden.connect(self._vlag_timer.stop)
        self._vlag_text(None)
        form.addRow(self._vlag_lbl)

    def _sec_output(self, form):
        _section(form, "Output device")

        self._audio_combo = QComboBox()
        self._audio_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._audio_combo.setMinimumContentsLength(12)
        self._audio_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._populate_audio_devices()
        # No QMediaDevices instance here to keep the list live. Constructing one
        # starts Qt's PipeWire device monitor, which segfaults inside its own
        # registry callback on this PySide6. The Refresh button next to the box
        # re-reads the list, and the player recovers on its own when the output
        # it is using stops working. See Player._on_device_lost.
        self._audio_combo.currentIndexChanged.connect(self._on_audio_device_changed)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setToolTip("Re-read the list of output devices")
        refresh_btn.clicked.connect(self._populate_audio_devices)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._audio_combo, 1)
        row.addWidget(refresh_btn)
        form.addRow(row)

        self._keep_open_chk = QCheckBox("Keep the output open")
        self._keep_open_chk.setChecked(self._cfg.get("keep_output_open", False))
        self._keep_open_chk.toggled.connect(self._apply_keep_output_open)
        if sys.platform != "win32":          # a patchbay is a Linux thing; Windows has nothing wired to a stream
            form.addRow(_check(self._keep_open_chk,
                "Leaves AudioCrate's output in the sound server when playback stops, pauses, skips or seeks, so what "
                "you wire to it in a patchbay such as Helvum stays wired. It opens at once and stays until you quit. "
                "A song you stop or skip keeps playing for a moment, because its queued sound cannot be thrown away."))

    def _sec_stats(self, form):
        _section(form, "Listening stats")

        self._stats_enable = QCheckBox("Track local listening stats")
        self._stats_enable.setChecked(self._cfg.get("play_stats_enabled", False))
        self._stats_enable.toggled.connect(self._apply_play_stats)
        form.addRow(_check(self._stats_enable,
            "Tracks most-played songs/artists and total listening time, entirely "
            f"locally ({plat_paths.shown(plat_paths.config_dir() / 'play_stats.json')}). Nothing is ever "
            "sent anywhere. A track counts as \"played\" the same way scrobbling "
            "counts it: 50% listened (or 4 minutes), whichever is sooner."))

    def _tab_visuals(self) -> QWidget:
        w, form = self._page()
        self._sec_spectrum(form)
        self._sec_oscilloscope(form)
        self._sec_waterfall(form)
        return self._scroll_wrap(w)

    def _sec_waterfall(self, form):
        _section(form, "Waterfall",
                 help="The spectrogram panel has its own settings: FFT, window, frequency axis, level range, "
                      "smoothing, colour map and direction. Right click it for the same.")
        btn = QPushButton("Waterfall settings\u2026")
        btn.clicked.connect(self.waterfall_requested.emit)
        form.addRow(btn)

    def set_waterfall(self, cfg: dict):
        self._cfg["waterfall"] = dict(cfg)
        self.save()

    def set_waterfall_presets(self, presets: dict):
        self._cfg["waterfall_presets"] = dict(presets)
        self.save()

    def _sec_spectrum(self, form):
        c = self._cfg
        _section(form, "Spectrum")

        self._viz_enabled = QCheckBox("Enable FFT spectrum")
        self._viz_enabled.setChecked(c["viz_fft_enabled"])
        form.addRow(self._viz_enabled)

        self._viz_gain_sl, self._viz_gain_lbl = _hslider(0, 400, c["viz_fft_gain"], "%",
            tip="Spectrum bar height. Above 100% boosts quiet material; lower it if bars clip the top.")
        _field(form, "Gain", _row(self._viz_gain_sl, self._viz_gain_lbl))

        self._viz_bars_sl, self._viz_bars_lbl = _hslider(8, 128, c["viz_bar_count"],
            tip="Number of frequency bars. More bars give finer detail at a slightly higher CPU cost.")
        _field(form, "Bar count", _row(self._viz_bars_sl, self._viz_bars_lbl))

        self._viz_gap_sl, self._viz_gap_lbl = _hslider(0, 8, c["viz_bar_gap"], " px",
            tip="Gap between spectrum bars, in pixels.")
        _field(form, "Bar gap", _row(self._viz_gap_sl, self._viz_gap_lbl))

        self._viz_corner_sl, self._viz_corner_lbl = _hslider(0, 10, c["viz_corner_r"], " px",
            tip="Corner rounding of each bar, in pixels (0 = square).")
        _field(form, "Corner radius", _row(self._viz_corner_sl, self._viz_corner_lbl))

        self._viz_peaks = QCheckBox("Show peak hold")
        self._viz_peaks.setChecked(c["viz_peak_hold"])
        form.addRow(self._viz_peaks)

        _section(form, "Album art backdrop")

        self._viz_show_art = QCheckBox("Show album art background")
        self._viz_show_art.setChecked(c.get("viz_show_art", True))
        form.addRow(self._viz_show_art)

        self._viz_opacity_sl, self._viz_opacity_lbl = _hslider(0, 100, c["viz_art_opacity"], "%",
            tip="Opacity of the album-art backdrop shown behind the spectrum bars.")
        _field(form, "Opacity", _row(self._viz_opacity_sl, self._viz_opacity_lbl))

        # Connect signals after all widgets exist so _apply_visualizer can read them all
        self._viz_enabled.toggled.connect(lambda _: self._apply_visualizer())
        self._viz_gain_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_show_art.toggled.connect(lambda _: self._apply_visualizer())
        self._viz_opacity_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_bars_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_gap_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_corner_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_peaks.toggled.connect(lambda _: self._apply_visualizer())

    def _sec_oscilloscope(self, form):
        c = self._cfg
        form = _section(form, "Oscilloscope", fold=True)

        self._osc_win_sl, self._osc_win_lbl = _hslider(5, 500, c["osc_window_ms"], " ms",
            tip="How much time the scope shows at once. Smaller = more zoomed-in on the waveform.")
        _field(form, "Window", _row(self._osc_win_sl, self._osc_win_lbl))

        self._osc_gain_sl, self._osc_gain_lbl = _hslider(10, 500, c["osc_gain"], "%",
            tip="Vertical scale of the trace. Raise it for quiet tracks, lower it if the trace clips.")
        _field(form, "Gain", _row(self._osc_gain_sl, self._osc_gain_lbl))

        self._osc_smooth_sl, self._osc_smooth_lbl = _hslider(0, 20, max(0, c["osc_smooth"]),
            tip="Averages this many frames to steady the trace. Higher = smoother but laggier.")
        _field(form, "Smoothing", _row(self._osc_smooth_sl, self._osc_smooth_lbl))

        self._osc_lw_sl, self._osc_lw_lbl = _hslider(5, 40, c["osc_line_width"],
            tip="Thickness of the scope trace, in pixels.",
            fmt=lambda v: f"{v / 10:g} px")
        _field(form, "Line width", _row(self._osc_lw_sl, self._osc_lw_lbl))

        self._osc_fps_sl, self._osc_fps_lbl = _hslider(
            15, 241, c.get("osc_fps", 60), tip="Frames per second. The far end is Unlimited, "
            "which draws as fast as the window can and uses a full core.",
            fmt=lambda v: "Unlimited" if v > 240 else f"{v} fps")
        _field(form, "Frame rate", _row(self._osc_fps_sl, self._osc_fps_lbl))

        self._osc_vsync = QCheckBox("V-Sync")
        self._osc_vsync.setChecked(c.get("osc_vsync", False))
        self._osc_vsync.setToolTip("Draw one frame per display refresh, in step with it, "
                                   "instead of on a timer.")
        form.addRow(_check(self._osc_vsync,
                           "Follows the display's refresh rate and ignores the frame rate above."))
        self._osc_fps_sl.setEnabled(not self._osc_vsync.isChecked())
        self._osc_vsync.toggled.connect(lambda on: self._osc_fps_sl.setEnabled(not on))

        self._osc_trigger = QCheckBox("Zero-crossing trigger")
        self._osc_trigger.setChecked(c.get("osc_trigger", True))
        self._osc_trigger.setToolTip(
            "Aligns the trace to the first rising zero-crossing each frame so the\n"
            "waveform holds still instead of scrolling sideways."
        )
        form.addRow(_check(self._osc_trigger,
                           "Holds the waveform still instead of scrolling sideways."))

        self._osc_win_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_gain_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_smooth_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_lw_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_fps_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_vsync.toggled.connect(lambda _: self._apply_oscilloscope())
        self._osc_trigger.toggled.connect(lambda _: self._apply_oscilloscope())

    def _tab_appearance(self) -> QWidget:
        w, form = self._page()
        self._swatch_updaters = {}   # key: fn that repaints a colour swatch

        _section(form, "Theme")
        self._theme_picker = ThemePicker()
        self._theme_picker.chosen.connect(self._choose_theme)
        form.addRow(self._theme_picker)

        self._art_color_chk = QCheckBox("Colours follow the album art")
        self._art_color_chk.setChecked(self._cfg["art_color_theme"])
        self._art_color_chk.toggled.connect(lambda _: self._apply_appearance())
        form.addRow(_check(self._art_color_chk,
                           "Takes the colours of the theme from the cover of the song that is playing and changes "
                           "them with it. A song without a cover uses the theme chosen above."))

        custom = theme_presets.current(self._cfg) == "custom"
        cust = _section(form, "Customize", minor=True, fold=not custom,
                        help="Change the colours of the theme yourself. Accent is the colour of the highlights, "
                             "Background and Text set the rest. Auto lets AudioCrate choose. Colours and corners "
                             "belong to AudioCrate's own themes, so they are grey while System is chosen.")
        own = []                                    # what only AudioCrate's own themes use
        for label, key, title, auto in (("Accent", "accent_color", "Accent colour", False),
                                        ("Background", "bg_color", "Background colour", True),
                                        ("Text", "text_color", "Text colour", True)):
            row = self._color_row(key, title, auto=auto)
            own.append(_field(cust, label, row))
            own += [row.itemAt(i).widget() for i in range(row.count())]

        self._radius_sl, self._radius_lbl = _hslider(0, 12, self._cfg.get("corner_radius", 4), " px")
        self._radius_timer = QTimer(self)           # restyling the window is not cheap, so a drag waits for a pause
        self._radius_timer.setSingleShot(True)
        self._radius_timer.setInterval(150)
        self._radius_timer.timeout.connect(self._apply_theme_style)
        self._radius_sl.valueChanged.connect(lambda _: self._radius_timer.start())
        own.append(_field(cust, "Corners", _row(self._radius_sl, self._radius_lbl),
                          help="How round the buttons, fields and menus are. 0 is square."))
        own.append(self._radius_sl)
        self._own_theme_widgets = own

        self._shade_sl, self._shade_lbl = _hslider(0, 100, self._cfg.get("frame_shade", 0), "%")
        self._shade_sl.setToolTip("Shades panel title bars with a gradient for depth.")
        self._shade_sl.sliderReleased.connect(self._apply_theme_style)
        _field(cust, "Frame shading", _row(self._shade_sl, self._shade_lbl))

        self._borders = QCheckBox("Borders between panels")
        self._borders.setChecked(self._cfg.get("panel_borders", True))
        self._borders.setToolTip("The lines around and between panels, tabs and title bars. "
                                 "Off gives a flatter look.")
        self._borders.toggled.connect(lambda _: self._apply_theme_style())
        cust.addRow(_check(self._borders, "Off gives a flatter look."))

        self._titlebar_chk = QCheckBox("Use AudioCrate title bars")
        self._titlebar_chk.setChecked(self._cfg.get("custom_titlebar", True))
        self._titlebar_chk.toggled.connect(self._apply_custom_titlebar)
        form.addRow(_check(self._titlebar_chk, "Restart to apply."))
        self._sync_theme_ui()

        form = _section(form, "Text")
        self._font_btn = QPushButton(self._font_name(self._cfg.get("text_font", "")))
        self._font_btn.setToolTip("Opens the system font dialog")
        self._font_btn.clicked.connect(self._pick_font)

        reset_font_btn = QPushButton("Reset")
        reset_font_btn.setToolTip("Back to the default font")
        reset_font_btn.clicked.connect(self._reset_font)
        font_row = QHBoxLayout()
        font_row.setSpacing(6)
        font_row.addWidget(self._font_btn, 1)
        font_row.addWidget(reset_font_btn)
        _field(form, "UI font", font_row)

        # UI size as percentage: 100% = 13px (default), range 75% to 200%
        _saved_pct = round(self._cfg.get("ui_size", 13) / 13 * 100)
        self._ui_size_sl, self._ui_size_lbl = _hslider(75, 200, _saved_pct, "%")
        # Applying restyles the whole window, so a drag waits for a pause; the keys, the wheel
        # and a click on the track all change the value without a release, and they apply too.
        self._ui_size_timer = QTimer(self)
        self._ui_size_timer.setSingleShot(True)
        self._ui_size_timer.setInterval(150)
        self._ui_size_timer.timeout.connect(self._commit_ui_size)
        self._ui_size_sl.valueChanged.connect(lambda _: self._ui_size_timer.start())
        self._ui_size_sl.sliderReleased.connect(self._commit_ui_size)
        _field(form, "UI size", _row(self._ui_size_sl, self._ui_size_lbl))

        self._lyr_font_sl, self._lyr_font_lbl = _hslider(10, 36, self._cfg["lyr_font_size"], " pt")
        self._lyr_font_sl.valueChanged.connect(lambda _: self._apply_lyrics())
        _field(form, "Lyrics size", _row(self._lyr_font_sl, self._lyr_font_lbl))

        return self._scroll_wrap(w)

    # colour pickers (accent / background / text)
    def _color_row(self, key: str, dialog_title: str, auto: bool = False):
        """A swatch button + reset, wired to cfg[key]. With auto, an empty value means
        'derive automatically' and the reset button restores that."""
        btn   = QPushButton()
        reset = QPushButton("Auto" if auto else "Reset")
        reset.setMinimumWidth(60)
        btn.setMinimumHeight(reset.sizeHint().height())

        def update():
            val = self._cfg.get(key, "")
            if not val:
                btn.setText("Auto"); btn.setStyleSheet("")
            else:
                btn.setText(val)
                btn.setStyleSheet(
                    f"background:{val}; color:{_contrast(val)}; "
                    f"border:1px solid #8c8c8caa; border-radius:4px;")

        def pick():
            from PySide6.QtGui import QColor
            cur = QColor(self._cfg.get(key) or _DEFAULTS.get(key) or "#8040c8")
            col = QColorDialog.getColor(cur, self, dialog_title)
            if col.isValid():
                self._cfg[key] = col.name()
                update(); self._apply_theme_style()

        def do_reset():
            self._cfg[key] = "" if auto else _DEFAULTS.get(key, "")
            update(); self._apply_theme_style()

        btn.clicked.connect(pick)
        reset.clicked.connect(do_reset)
        update()
        self._swatch_updaters[key] = update
        row = QHBoxLayout(); row.setSpacing(6)
        row.addWidget(btn, 1); row.addWidget(reset)
        return row

    def _sync_theme_ui(self):
        """The picker, the greyed rows and the sliders, from the settings: after a preset was chosen, a config was
        imported or the settings were reset."""
        self._theme_picker.select(theme_presets.current(self._cfg))
        self._apply_own_theme_enabled(not self._cfg["system_theme"])
        for update in self._swatch_updaters.values():
            update()
        self._radius_sl.blockSignals(True)
        self._radius_sl.setValue(self._cfg.get("corner_radius", 4))
        self._radius_sl.blockSignals(False)
        self._radius_lbl.setText(f"{self._radius_sl.value()} px")

    def sync_theme(self):
        """For MainWindow, after it has put other settings in place (an imported config)."""
        self._sync_theme_ui()

    def _choose_theme(self, key: str):
        self._cfg.update(theme_presets.settings_for(key))
        self.save()
        self._sync_theme_ui()
        self._apply_system_theme(self._cfg["system_theme"])
        self._apply_theme_style()

    def _apply_theme_style(self):
        self._cfg["frame_shade"] = self._shade_sl.value()
        self._cfg["panel_borders"] = self._borders.isChecked()
        self._cfg["corner_radius"] = self._radius_sl.value()
        self.save()
        self._theme_picker.select(theme_presets.current(self._cfg))
        self.theme_style_changed.emit({
            "accent":      self._cfg.get("accent_color", "#8040c8"),
            "bg":          self._cfg.get("bg_color", ""),
            "text":        self._cfg.get("text_color", ""),
            "frame_shade": self._cfg["frame_shade"],
            "borders":     self._cfg["panel_borders"],
            "style":       self._cfg["theme_style"],
            "radius":      self._cfg["corner_radius"],
        })

    def _apply_own_theme_enabled(self, on: bool):
        for w in getattr(self, "_own_theme_widgets", ()):
            w.setEnabled(on)

    def _apply_system_theme(self, on: bool):
        self._apply_own_theme_enabled(not on)
        self._cfg["system_theme"] = on
        self.save()
        self.system_theme_changed.emit(on)

    @staticmethod
    def _font_name(name: str) -> str:
        return name or "Default (JetBrains Mono)"

    def _pick_font(self):
        """The system's own font dialog where the platform has one (KDE does), Qt's otherwise."""
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QFontDialog
        start = QFont(self._cfg.get("text_font") or "JetBrains Mono")
        start.setPixelSize(int(self._cfg.get("ui_size", 13)))
        res = QFontDialog.getFont(start, self, "UI font")
        ok, font = res if isinstance(res[0], bool) else (res[1], res[0])
        if ok:
            self._apply_font(font)
            self._font_btn.setText(self._font_name(self._cfg.get("text_font", "")))
            px = font.pixelSize() if font.pixelSize() > 0 else round(font.pointSizeF() * self.logicalDpiY() / 72)
            px = max(10, min(26, px))               # the range of the UI size slider
            if px != self._cfg.get("ui_size", 13):
                self._apply_ui_size(px)

    def _apply_font(self, font):
        name = font.family()
        # the Nerd Font can't be the text font, it would break glyphs
        _nerd_prefixes = ("JetBrainsMonoNL Nerd", "JetBrainsMono Nerd")
        if any(name.startswith(p) for p in _nerd_prefixes):
            return
        if name == self._cfg.get("text_font"):
            return  # no change, skip redundant reapply
        self._cfg["text_font"] = name
        self.save()
        self.font_changed.emit(name)

    def _reset_font(self):
        self._cfg["text_font"] = ""
        self._font_btn.setText(self._font_name(""))
        self.save()
        self.font_changed.emit("")

    def _commit_ui_size(self):
        self._ui_size_timer.stop()
        pct  = self._ui_size_sl.value()
        size = max(9, round(13 * pct / 100))
        if size == self._cfg.get("ui_size", 13):
            return
        self._cfg["ui_size"] = size
        self.save()
        self.ui_size_changed.emit(size)

    def _apply_ui_size(self, size: int):
        # takes px, moves the slider in percent; used by the font dialog and the reset
        pct = round(size / 13 * 100)
        self._ui_size_sl.blockSignals(True)
        self._ui_size_sl.setValue(pct)
        self._ui_size_sl.blockSignals(False)
        self._ui_size_lbl.setText(f"{pct}%")
        self._cfg["ui_size"] = size
        self.save()
        self.ui_size_changed.emit(size)

    def _tab_system(self) -> QWidget:
        w, form = self._page()
        self._sec_panels(form)
        self._sec_artists(form)
        self._sec_tour(form)
        self._sec_logs(form)
        self._sec_lighting(form)
        self._sec_backup(form)
        return self._scroll_wrap(w)

    def _sec_panels(self, form):
        _section(form, "Panels")
        self._docking_chk = QCheckBox("Allow docking")
        self._docking_chk.setChecked(self._cfg.get("docking_enabled", True))
        self._docking_chk.toggled.connect(self._apply_docking)
        form.addRow(_check(self._docking_chk, "Panels snap together when dragged."))

        self._viz_auto_suspend_chk = QCheckBox("Auto-suspend visualizers")
        self._viz_auto_suspend_chk.setChecked(self._cfg.get("viz_auto_suspend", True))
        self._viz_auto_suspend_chk.toggled.connect(self._apply_viz_auto_suspend)
        form.addRow(_check(self._viz_auto_suspend_chk,
            "Stops drawing the visualizers while AudioCrate is minimized or not the active window, which saves "
            "processor time."))

        self._anim_chk = QCheckBox("Animations")
        self._anim_chk.setChecked(self._cfg.get("animations", True))
        self._anim_chk.toggled.connect(self._apply_animations)
        form.addRow(_check(self._anim_chk, "Short fades when a track changes. Off shows every change at once."))
        anim.set_enabled(self._anim_chk.isChecked())

    def _sec_artists(self, form):
        form = _section(form, "Artist names", fold=True,
                        help="A tag with several artists is split into separate names. The library, search and "
                             "statistics treat each name on its own.")
        self._split_commas = QCheckBox("Split at commas")
        self._split_commas.setChecked(self._cfg["artist_split_commas"])
        self._split_amp = QCheckBox("Split at &&")
        self._split_amp.setChecked(self._cfg["artist_split_amp"])
        self._split_slash = QCheckBox("Split at slashes")
        self._split_slash.setChecked(self._cfg["artist_split_slash"])
        form.addRow(self._split_commas)
        form.addRow(self._split_amp)
        form.addRow(_check(self._split_slash, "A tag is always split at ; and at feat., ft. and featuring. A tag that "
                           "holds several values is never split any further. Older ID3v2.3 tags list several "
                           "artists with a slash. It is off by default because some names, like AC/DC, have one."))
        keep_head = QWidget()
        kh = QHBoxLayout(keep_head)
        kh.setContentsMargins(0, 0, 0, 0)
        kh.setSpacing(4)
        kh.addWidget(QLabel("Names that stay whole"))
        kh.addWidget(_HelpButton("One name per line. The album artist of a file stays whole too."))
        kh.addStretch(1)
        form.addRow(keep_head)
        self._split_keep = QPlainTextEdit(self._cfg["artist_exceptions"])
        self._split_keep.setFixedHeight(96)
        form.addRow(self._split_keep)
        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.setInterval(600)
        self._split_timer.timeout.connect(self._apply_artist_split)
        self._split_commas.toggled.connect(self._apply_artist_split)
        self._split_amp.toggled.connect(self._apply_artist_split)
        self._split_slash.toggled.connect(self._apply_artist_split)
        self._split_keep.textChanged.connect(self._split_timer.start)      # not on every key

    def artist_options(self) -> dict:
        c = self._cfg
        return {"commas": c["artist_split_commas"], "amp": c["artist_split_amp"], "exceptions": c["artist_exceptions"],
                "slash": c["artist_split_slash"]}

    def show_artist_options(self):
        """Put the artist settings in the boxes, for a config that was imported or the defaults after a reset."""
        c = self._cfg
        for box, key in ((self._split_commas, "artist_split_commas"), (self._split_amp, "artist_split_amp"),
                         (self._split_slash, "artist_split_slash")):
            box.blockSignals(True)
            box.setChecked(c[key])
            box.blockSignals(False)
        self._split_keep.blockSignals(True)
        self._split_keep.setPlainText(c["artist_exceptions"])
        self._split_keep.blockSignals(False)

    def _apply_artist_split(self):
        self._split_timer.stop()
        self._cfg.update({"artist_split_commas": self._split_commas.isChecked(),
                          "artist_split_amp": self._split_amp.isChecked(),
                          "artist_split_slash": self._split_slash.isChecked(),
                          "artist_exceptions": self._split_keep.toPlainText()})
        self.save()
        configure_artists(**self.artist_options())
        self.artist_split_changed.emit()

    def _sec_tour(self, form):
        _section(form, "Tour")
        btn = QPushButton("Show the tour")
        btn.clicked.connect(self.tour_requested)
        form.addRow(self._button_row(btn))

    def _sec_logs(self, form):
        form = _section(form, "Logs", fold=True)
        self._debug_logs_chk = QCheckBox("Write detailed logs")
        self._debug_logs_chk.setChecked(self._cfg.get("debug_logs", False))
        self._debug_logs_chk.toggled.connect(self._apply_debug_logs)
        form.addRow(_check(self._debug_logs_chk, f"Adds debug lines to the log in {plat_paths.shown(plat_paths.config_dir())}. It applies at once. "
                           "Help, Diagnostics shows the log and can save a report."))

    def _apply_debug_logs(self, on: bool):
        from applog import set_detailed
        self._cfg["debug_logs"] = on
        self.save()
        set_detailed(on)

    def _sec_lighting(self, form):
        c = self._cfg
        form = _section(form, "Lighting", fold=True)

        self._rgb_enabled = QCheckBox("Enable lighting")
        self._rgb_enabled.setChecked(c["rgb_enabled"])
        predator = sys.platform.startswith("linux")      # the keyboard, emblem and mode LED are an Acer Predator controller, read through Linux
        form.addRow(_check(self._rgb_enabled,
            ("Lights the Predator keyboard, lid emblem and mode LED, and any OpenRGB "
             "device, from the album art colour and the spectrum.") if predator else
            "Lights your OpenRGB devices from the album art colour and the spectrum."))

        self._rgb_style = QComboBox()
        self._rgb_style.addItems(_RGB_STYLES)
        self._rgb_style.setCurrentText(c["rgb_style"])
        _field(form, "Style", self._rgb_style)

        self._rgb_bright_sl, self._rgb_bright_lbl = _hslider(5, 100, c["rgb_brightness"], "%")
        _field(form, "Brightness", _row(self._rgb_bright_sl, self._rgb_bright_lbl))

        self._rgb_fps_sl, self._rgb_fps_lbl = _hslider(2, 30, c["rgb_fps"], " fps")
        _field(form, "Update rate", _row(self._rgb_fps_sl, self._rgb_fps_lbl))

        self._rgb_keyboard = QCheckBox("Keyboard")
        self._rgb_emblem = QCheckBox("Lid emblem")
        self._rgb_mode_led = QCheckBox("Mode LED")
        for chk, key in ((self._rgb_keyboard, "rgb_keyboard"), (self._rgb_emblem, "rgb_emblem"), (self._rgb_mode_led, "rgb_mode_led")):
            chk.setChecked(c[key] and predator)
            if predator:
                form.addRow(chk)

        self._rgb_openrgb = QCheckBox("OpenRGB devices")
        self._rgb_openrgb.setChecked(c["rgb_openrgb"])
        form.addRow(_check(self._rgb_openrgb,
            "Starts OpenRGB's server itself if it is not already running (it scans "
            "your hardware when it starts). Devices whose name contains an excluded "
            "word are skipped."))
        self._rgb_host = QLineEdit(c["rgb_openrgb_host"])
        self._rgb_host.setPlaceholderText("host:port")
        _field(form, "Server", self._rgb_host)
        self._rgb_exclude = QLineEdit(c["rgb_openrgb_exclude"])
        self._rgb_exclude.setPlaceholderText("Words, comma separated")
        _field(form, "Skip", self._rgb_exclude)

        self._rgb_status = _hint("")
        self._rgb_status.setVisible(False)
        form.addRow(self._rgb_status)

        for chk in (self._rgb_enabled, self._rgb_keyboard, self._rgb_emblem,
                    self._rgb_mode_led, self._rgb_openrgb):
            chk.toggled.connect(lambda _: self._apply_rgb())
        self._rgb_style.currentIndexChanged.connect(lambda _: self._apply_rgb())
        self._rgb_bright_sl.valueChanged.connect(lambda _: self._apply_rgb())
        self._rgb_fps_sl.valueChanged.connect(lambda _: self._apply_rgb())
        self._rgb_host.editingFinished.connect(self._apply_rgb)
        self._rgb_exclude.editingFinished.connect(self._apply_rgb)

    def set_rgb_status(self, text: str):
        """Called by MainWindow with the lighting device status."""
        self._rgb_status.setText(text)
        self._rgb_status.setVisible(bool(text))

    def _sec_backup(self, form):
        form = _section(form, "Backup and reset", fold=True)
        exp_btn = QPushButton("Export\u2026")
        exp_btn.clicked.connect(self._on_export_layout)
        imp_btn = QPushButton("Import\u2026")
        imp_btn.clicked.connect(self._on_import_layout)
        _field(form, "Panel layout", self._button_row(exp_btn, imp_btn),
               help="Save or restore the full panel layout: positions, sizes, floating state and visibility.")

        cfg_exp_btn = QPushButton("Export\u2026")
        cfg_exp_btn.setToolTip("Saves all settings + layout into a single .json file")
        cfg_exp_btn.clicked.connect(self._on_export_config)
        cfg_imp_btn = QPushButton("Import\u2026")
        cfg_imp_btn.setToolTip("Restores all settings + layout from a previously exported file")
        cfg_imp_btn.clicked.connect(self._on_import_config)
        _field(form, "All settings", self._button_row(cfg_exp_btn, cfg_imp_btn),
               help="Settings and layout together, in one .json file.")

        reset_btn = QPushButton("Reset all settings")
        reset_btn.clicked.connect(self._reset_defaults)
        form.addRow(reset_btn)

    @staticmethod
    def _button_row(*buttons):
        row = QHBoxLayout()
        row.setSpacing(8)
        for b in buttons:
            row.addWidget(b, 1)
        return row

    def _tab_connections(self) -> QWidget:
        w, form = self._page()
        self._sec_discord(form)
        self._sec_scrobbling(form)
        return self._scroll_wrap(w)

    def _sec_discord(self, form):
        c = self._cfg
        _section(form, "Discord Rich Presence")

        self._discord_enable = QCheckBox("Show my music on Discord")
        self._discord_enable.setChecked(c.get("discord_rpc_enabled", False))
        self._discord_enable.toggled.connect(lambda _: self._apply_discord())
        form.addRow(_check(self._discord_enable,
                           "Needs the Discord desktop app running, and an application of your own made in "
                           "Discord's Developer Portal (its Application ID goes in App ID below). Album art needs "
                           "the Art URL under Advanced.", links=_DISCORD_LINKS))

        self._discord_client_id = QLineEdit(c.get("discord_client_id", ""))
        self._discord_client_id.setPlaceholderText("Application client ID")
        self._discord_client_id.editingFinished.connect(self._apply_discord)
        _field(form, "App ID", self._discord_client_id,
               help="Discord needs an application of your own to show your music. Make one in the Developer Portal "
                    "(New Application), then copy its Application ID from General Information and paste it here.",
               links=_DISCORD_LINKS)

        adv = _section(form, "Advanced", minor=True,
                       fold=not (c.get("discord_art_base_url") or c.get("discord_art_upload_cmd")),
                       help="Album art on Discord needs a public web address that serves the pictures (Art URL) and, "
                            "when that address is on another computer, a command that uploads each picture there "
                            "(Art upload). Most people can leave both empty.")

        self._discord_art_base = QLineEdit(c.get("discord_art_base_url", ""))
        self._discord_art_base.setPlaceholderText("Public base URL")
        self._discord_art_base.editingFinished.connect(self._apply_discord)
        _field(adv, "Art URL", self._discord_art_base, help="Public base URL that serves the album art.")

        self._discord_art_upload = QLineEdit(c.get("discord_art_upload_cmd", ""))
        self._discord_art_upload.setPlaceholderText("Command, optional")
        self._discord_art_upload.editingFinished.connect(self._apply_discord)
        _field(adv, "Art upload", self._discord_art_upload,
               help="If the Art URL is hosted elsewhere, this shell command uploads each art file there. {file} is "
                    "replaced with the local path (for example an scp or rsync command).")

    def _sec_scrobbling(self, form):
        _section(form, "Scrobbling",
                 help="Scrobbles once a track has played 50% (or 4 minutes, whichever is sooner). Each service needs "
                      "its own free API key from your account there: last.fm/api/account/create or "
                      "libre.fm/api/account/create.")

        self._scrobble_widgets = {}
        for service, label in (("lastfm", "Last.fm"), ("librefm", "Libre.fm")):
            on = self._cfg.get(f"{service}_enabled", False)
            sub = _section(form, label, fold=not on)

            enable = QCheckBox(f"Scrobble to {label}")
            enable.setChecked(on)
            status = _hint("Not connected")
            sub.addRow(_check(enable, status))

            key = QLineEdit(self._cfg.get(f"{service}_api_key", ""))
            key.setPlaceholderText("API key")
            _field(sub, "API key", key)

            secret = QLineEdit(self._cfg.get(f"{service}_api_secret", ""))
            secret.setPlaceholderText("API secret")
            secret.setEchoMode(QLineEdit.Password)
            _field(sub, "API secret", secret)

            connect_btn = QPushButton("Connect\u2026")
            disconnect_btn = QPushButton("Disconnect")
            disconnect_btn.setVisible(False)
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            btn_row.addWidget(connect_btn)
            btn_row.addWidget(disconnect_btn)
            btn_row.addStretch(1)
            _field(sub, "Account", btn_row)

            self._scrobble_widgets[service] = {
                "enable": enable, "key": key, "secret": secret,
                "status": status, "connect": connect_btn, "disconnect": disconnect_btn,
            }

            enable.toggled.connect(lambda _, s=service: self._apply_scrobble_config(s))
            key.editingFinished.connect(lambda s=service: self._apply_scrobble_config(s))
            secret.editingFinished.connect(lambda s=service: self._apply_scrobble_config(s))
            connect_btn.clicked.connect(lambda _, s=service: self._on_scrobble_connect(s))
            disconnect_btn.clicked.connect(lambda _, s=service: self._on_scrobble_disconnect(s))

    def _apply_scrobble_config(self, service: str):
        wd = self._scrobble_widgets[service]
        self._cfg[f"{service}_enabled"] = wd["enable"].isChecked()
        self._cfg[f"{service}_api_key"] = wd["key"].text().strip()
        self._cfg[f"{service}_api_secret"] = wd["secret"].text().strip()
        self.save()
        self.scrobble_config_changed.emit(service, {
            "enabled": self._cfg[f"{service}_enabled"],
            "api_key": self._cfg[f"{service}_api_key"],
            "api_secret": self._cfg[f"{service}_api_secret"],
        })

    def _on_scrobble_connect(self, service: str):
        self._apply_scrobble_config(service)   # the key and secret must be current before the browser step
        self._scrobble_widgets[service]["status"].setText("Connecting…")
        self.scrobble_connect.emit(service)

    def _on_scrobble_disconnect(self, service: str):
        self.scrobble_disconnect.emit(service)

    def set_scrobble_status(self, service: str, connected: bool, message: str):
        """Called by MainWindow when auth completes/fails/is undone."""
        wd = self._scrobble_widgets.get(service)
        if wd is None:
            return
        wd["status"].setText(f"Connected as {message}" if connected
                             else (message or "Not connected"))
        wd["connect"].setVisible(not connected)
        wd["disconnect"].setVisible(connected)

    def _apply_docking(self, on: bool):
        self._cfg["docking_enabled"] = on
        self.save()
        self.docking_changed.emit(on)

    def _apply_animations(self, on: bool):
        self._cfg["animations"] = on
        self.save()
        anim.set_enabled(on)

    def _apply_viz_auto_suspend(self, on: bool):
        self._cfg["viz_auto_suspend"] = on
        self.save()
        self.viz_auto_suspend_changed.emit(on)

    def _apply_custom_titlebar(self, on: bool):
        self._cfg["custom_titlebar"] = on
        self.save()

    def _apply_rgb(self):
        self._cfg.update({
            "rgb_enabled":         self._rgb_enabled.isChecked(),
            "rgb_style":           self._rgb_style.currentText(),
            "rgb_keyboard":        self._rgb_keyboard.isChecked(),
            "rgb_emblem":          self._rgb_emblem.isChecked(),
            "rgb_mode_led":        self._rgb_mode_led.isChecked(),
            "rgb_openrgb":         self._rgb_openrgb.isChecked(),
            "rgb_openrgb_host":    self._rgb_host.text().strip(),
            "rgb_openrgb_exclude": self._rgb_exclude.text().strip(),
            "rgb_brightness":      self._rgb_bright_sl.value(),
            "rgb_fps":             self._rgb_fps_sl.value(),
        })
        self.save()
        self.rgb_changed.emit({
            "enabled":         self._cfg["rgb_enabled"],
            "style":           self._cfg["rgb_style"],
            "keyboard":        self._cfg["rgb_keyboard"],
            "emblem":          self._cfg["rgb_emblem"],
            "mode_led":        self._cfg["rgb_mode_led"],
            "openrgb":         self._cfg["rgb_openrgb"],
            "openrgb_host":    self._cfg["rgb_openrgb_host"],
            "openrgb_exclude": self._cfg["rgb_openrgb_exclude"],
            "brightness":      self._cfg["rgb_brightness"],
            "fps":             self._cfg["rgb_fps"],
        })

    def _apply_keep_output_open(self, on: bool):
        self._cfg["keep_output_open"] = on
        self.save()
        self.keep_output_open_changed.emit(on)

    def _apply_play_stats(self, on: bool):
        self._cfg["play_stats_enabled"] = on
        self.save()
        self.play_stats_changed.emit(on)

    def _populate_audio_devices(self):
        self._audio_combo.blockSignals(True)
        self._audio_combo.clear()
        default   = QMediaDevices.defaultAudioOutput()
        saved_id  = self._cfg.get("audio_device_id", "")
        select_idx = 0
        self._audio_combo.addItem(
            f"System default ({default.description() if not default.isNull() else 'none'})",
            None,
        )
        for i, dev in enumerate(QMediaDevices.audioOutputs(), start=1):
            key = device_key(dev)
            self._audio_combo.addItem(dev.description(), key)
            if key == saved_id:
                select_idx = i
        self._audio_combo.setCurrentIndex(select_idx)
        self._audio_combo.blockSignals(False)

    def _on_audio_device_changed(self, idx: int):
        dev_id = self._audio_combo.itemData(idx)
        self._cfg["audio_device_id"] = dev_id if dev_id is not None else ""
        self.save()
        if dev_id is None:
            self.audio_device_changed.emit(QMediaDevices.defaultAudioOutput())
        else:
            for dev in QMediaDevices.audioOutputs():
                if device_key(dev) == dev_id:
                    self.audio_device_changed.emit(dev)
                    return

    def _apply_visualizer(self):
        show_art = self._viz_show_art.isChecked()
        self._cfg.update({
            "viz_fft_enabled": self._viz_enabled.isChecked(),
            "viz_fft_gain":    self._viz_gain_sl.value(),
            "viz_show_art":    show_art,
            "viz_art_opacity": self._viz_opacity_sl.value(),
            "viz_bar_count":   self._viz_bars_sl.value(),
            "viz_bar_gap":     self._viz_gap_sl.value(),
            "viz_corner_r":    self._viz_corner_sl.value(),
            "viz_peak_hold":   self._viz_peaks.isChecked(),
        })
        self.save()
        self.visualizer_changed.emit({
            "fft_enabled": self._cfg["viz_fft_enabled"],
            "fft_gain":    self._cfg["viz_fft_gain"],
            "show_art":    show_art,
            "art_opacity": self._cfg["viz_art_opacity"] / 100.0,
            "bar_count":   self._cfg["viz_bar_count"],
            "bar_gap":     self._cfg["viz_bar_gap"],
            "corner_r":    self._cfg["viz_corner_r"],
            "peak_hold":   self._cfg["viz_peak_hold"],
        })

    def _apply_oscilloscope(self):
        self._cfg.update({
            "osc_window_ms":  self._osc_win_sl.value(),
            "osc_gain":       self._osc_gain_sl.value(),
            "osc_smooth":     self._osc_smooth_sl.value(),
            "osc_line_width": self._osc_lw_sl.value(),
            "osc_trigger":    self._osc_trigger.isChecked(),
            "osc_fps":        self._osc_fps_sl.value(),
            "osc_vsync":      self._osc_vsync.isChecked(),
        })
        self.save()
        self.oscilloscope_changed.emit({
            "window_ms":  self._cfg["osc_window_ms"],
            "gain":       self._cfg["osc_gain"],
            "smooth":     self._cfg["osc_smooth"],
            "line_width": self._cfg["osc_line_width"] / 10.0,
            "trigger":    self._cfg["osc_trigger"],
            "fps":        self._cfg["osc_fps"],
            "vsync":      self._cfg["osc_vsync"],
        })

    def _apply_lyrics(self):
        self._cfg["lyr_font_size"] = self._lyr_font_sl.value()
        self.save()
        self.lyrics_changed.emit({"font_size": self._cfg["lyr_font_size"]})

    def _apply_appearance(self):
        self._cfg["art_color_theme"] = self._art_color_chk.isChecked()
        self.save()
        self.appearance_changed.emit({"art_color_theme": self._cfg["art_color_theme"]})

    def _apply_video_dsp(self, on: bool):
        self._cfg["video_dsp"] = on
        self.save()
        self.video_dsp_changed.emit(on)

    def _apply_video_delay(self, ms: int):
        self._cfg["video_delay_ms"] = int(ms)
        if not self._vdelay_sl.isSliderDown():
            self.save()
        self.video_delay_changed.emit(int(ms))
        self._vlag_tick()

    def set_video_lag_source(self, fn):
        """fn() -> how far the sound of the playing video trails the picture in ms (negative: leads it), or None when
        no video plays through our output. It is asked twice a second while the text under the slider is on screen."""
        self._vlag_source = fn
        if self._vlag_lbl.isVisible():
            self._vlag_start()

    def _vlag_start(self):
        self._vlag_tick()
        if self._vlag_source is not None:
            self._vlag_timer.start()

    def _vlag_tick(self):
        if not self._vlag_lbl.isVisible():
            return
        try:
            self._vlag_text(self._vlag_source() if self._vlag_source is not None else None)
        except Exception:
            self._vlag_text(None)

    def _vlag_text(self, ms):
        if ms is None:
            self._vlag_lbl.setText("Shows the offset while a video plays.")
        elif ms >= 0:
            self._vlag_lbl.setText(f"Now about {round(ms)} ms behind the picture.")
        else:
            self._vlag_lbl.setText(f"Now about {round(-ms)} ms ahead of the picture.")

    def _apply_crossfade(self):
        ms = self._xfade_sl.value() * 1000
        self._cfg["crossfade_ms"] = ms
        self.save()
        self.crossfade_changed.emit(ms)

    def _apply_discord(self):
        self._cfg["discord_rpc_enabled"] = self._discord_enable.isChecked()
        self._cfg["discord_client_id"] = self._discord_client_id.text().strip()
        self._cfg["discord_art_base_url"] = self._discord_art_base.text().strip().rstrip("/")
        self._cfg["discord_art_upload_cmd"] = self._discord_art_upload.text().strip()
        self.save()
        self.discord_changed.emit({
            "enabled":        self._cfg["discord_rpc_enabled"],
            "client_id":      self._cfg["discord_client_id"],
            "art_base_url":   self._cfg["discord_art_base_url"],
            "art_upload_cmd": self._cfg["discord_art_upload_cmd"],
        })

    def _on_export_layout(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export layout", str(Path.home() / "layout.json"), "JSON (*.json)"
        )
        if path:
            self.layout_export.emit(Path(path))

    def _on_import_layout(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import layout", str(Path.home()), "JSON (*.json)"
        )
        if path:
            self.layout_import.emit(Path(path))

    def _on_export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export all config", str(Path.home() / "audiocrate_config.json"), "JSON (*.json)"
        )
        if path:
            self.config_export.emit(Path(path))

    def _on_import_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import all config", str(Path.home()), "JSON (*.json)"
        )
        if path:
            self.config_import.emit(Path(path))

    def _reset_defaults(self):
        if QMessageBox.question(
            self, "Reset settings",
            "Reset all settings to their defaults?",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._cfg = dict(_DEFAULTS, tour_seen=self._cfg["tour_seen"])
        self.save()
        self._debug_logs_chk.setChecked(False)
        # Visualizer
        self._viz_enabled.setChecked(self._cfg["viz_fft_enabled"])
        self._viz_gain_sl.setValue(self._cfg["viz_fft_gain"])
        self._viz_show_art.setChecked(self._cfg["viz_show_art"])
        self._viz_opacity_sl.setValue(self._cfg["viz_art_opacity"])
        self._viz_bars_sl.setValue(self._cfg["viz_bar_count"])
        self._viz_gap_sl.setValue(self._cfg["viz_bar_gap"])
        self._viz_corner_sl.setValue(self._cfg["viz_corner_r"])
        self._viz_peaks.setChecked(self._cfg["viz_peak_hold"])
        # Oscilloscope
        self._osc_win_sl.setValue(self._cfg["osc_window_ms"])
        self._osc_gain_sl.setValue(self._cfg["osc_gain"])
        self._osc_smooth_sl.setValue(self._cfg["osc_smooth"])
        self._osc_lw_sl.setValue(self._cfg["osc_line_width"])
        self._osc_trigger.setChecked(self._cfg["osc_trigger"])
        self._osc_fps_sl.setValue(self._cfg["osc_fps"])
        self._osc_vsync.setChecked(self._cfg["osc_vsync"])
        # Lyrics
        self._lyr_font_sl.setValue(self._cfg["lyr_font_size"])
        # Appearance
        self._art_color_chk.setChecked(self._cfg["art_color_theme"])
        self._sync_theme_ui()
        # Force the theme to re-apply even when the checkbox value didn't change
        # (setChecked emits no signal if already in that state), otherwise a reset
        # could leave a previously-applied art/app theme showing while the box
        # reads "system". Done before the other appearance applies below.
        self._apply_system_theme(self._cfg["system_theme"])
        self._apply_ui_size(self._cfg["ui_size"])
        self._font_btn.setText(self._font_name(self._cfg["text_font"]))
        for update in self._swatch_updaters.values():
            update()
        self._shade_sl.setValue(self._cfg["frame_shade"])
        self._borders.setChecked(self._cfg["panel_borders"])
        self._anim_chk.setChecked(self._cfg["animations"])
        # Playback
        self._xfade_sl.setValue(self._cfg["crossfade_ms"] // 1000)
        self._video_dsp_chk.setChecked(self._cfg["video_dsp"])
        self._vdelay_sl.setValue(self._cfg["video_delay_ms"])
        self._discord_enable.setChecked(self._cfg["discord_rpc_enabled"])
        self._discord_client_id.setText(self._cfg["discord_client_id"])
        self._discord_art_base.setText(self._cfg["discord_art_base_url"])
        self._discord_art_upload.setText(self._cfg.get("discord_art_upload_cmd", ""))
        # Titlebar and lighting. Signals stay blocked so one widget's apply can't
        # overwrite another's default; _apply_rgb below publishes the lot.
        c = self._cfg
        rgb_widgets = (self._titlebar_chk, self._rgb_enabled, self._rgb_keyboard,
                       self._rgb_emblem, self._rgb_mode_led, self._rgb_openrgb,
                       self._rgb_style, self._rgb_bright_sl, self._rgb_fps_sl)
        for wd in rgb_widgets:
            wd.blockSignals(True)
        self._titlebar_chk.setChecked(c["custom_titlebar"])
        self._rgb_enabled.setChecked(c["rgb_enabled"])
        self._rgb_keyboard.setChecked(c["rgb_keyboard"])
        self._rgb_emblem.setChecked(c["rgb_emblem"])
        self._rgb_mode_led.setChecked(c["rgb_mode_led"])
        self._rgb_openrgb.setChecked(c["rgb_openrgb"])
        self._rgb_style.setCurrentText(c["rgb_style"])
        self._rgb_bright_sl.setValue(c["rgb_brightness"])
        self._rgb_bright_lbl.setText(f"{c['rgb_brightness']}%")
        self._rgb_fps_sl.setValue(c["rgb_fps"])
        self._rgb_fps_lbl.setText(f"{c['rgb_fps']} fps")
        for wd in rgb_widgets:
            wd.blockSignals(False)
        self._rgb_host.setText(c["rgb_openrgb_host"])
        self._rgb_exclude.setText(c["rgb_openrgb_exclude"])
        self._apply_visualizer()
        self._apply_oscilloscope()
        self._apply_lyrics()
        self._apply_appearance()
        self._apply_crossfade()
        self._apply_video_dsp(self._cfg["video_dsp"])
        self._apply_video_delay(self._cfg["video_delay_ms"])
        self._apply_discord()
        self._apply_rgb()
        self._apply_custom_titlebar(self._cfg["custom_titlebar"])
        self._apply_theme_style()
        self.show_artist_options()
        self._apply_artist_split()
        self.font_changed.emit("")
        # ui_size_changed already emitted inside _apply_ui_size above
