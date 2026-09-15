from __future__ import annotations
import json
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QLabel, QSlider, QCheckBox,
    QScrollArea, QFrame,
    QSizePolicy, QPushButton, QFileDialog, QComboBox,
    QMessageBox, QFontComboBox, QLineEdit, QColorDialog,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtMultimedia import QMediaDevices

from player import device_key

_CFG_FILE = Path.home() / ".config" / "audiocrate" / "settings.json"

# Blank by default — Discord Rich Presence is opt-in, and each user supplies
# their own Discord application ID + art base URL in Settings. (No personal
# domain or app ID is shipped to the public.)
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
    "osc_fps":           60,
    "pb_fade_steps":     2,
    "pb_fade_interval":  5,
    "lyr_font_size":     18,
    "art_color_theme":   False,
    "accent_color":      "#8040c8",
    "bg_color":          "",          # "" = auto (tinted from the accent)
    "text_color":        "",          # "" = auto (default foreground)
    "frame_shade":       0,
    "normalize":         False,
    "crossfade_ms":      0,
    "discord_rpc_enabled":  False,
    "discord_client_id":    _DEFAULT_DISCORD_CLIENT_ID,
    "discord_art_base_url": _DEFAULT_DISCORD_ART_BASE_URL,
    "discord_art_upload_cmd": "",
    "audio_device_id":   "",
    "docking_enabled":   True,
    "viz_auto_suspend":  True,
    "lastfm_enabled":    False,
    "lastfm_api_key":    "",
    "lastfm_api_secret": "",
    "librefm_enabled":    False,
    "librefm_api_key":    "",
    "librefm_api_secret": "",
    "play_stats_enabled": False,
    "text_font":         "",
    "ui_size":           13,
    "system_theme":      True,
}


def _hslider(lo, hi, val, suffix="", tip=""):
    sl  = QSlider(Qt.Horizontal)
    sl.setRange(lo, hi)
    sl.setValue(val)
    if tip:
        sl.setToolTip(tip)
    lbl = QLabel(f"{val}{suffix}")
    lbl.setMinimumWidth(46)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    sl.valueChanged.connect(lambda v: lbl.setText(f"{v}{suffix}"))
    return sl, lbl


def _row(sl, lbl):
    h = QHBoxLayout()
    h.setSpacing(6)
    h.addWidget(sl, 1)
    h.addWidget(lbl)
    return h


def _contrast(hex_color: str) -> str:
    """Readable text colour (black/white) for a swatch of the given background."""
    from PySide6.QtGui import QColor
    c = QColor(hex_color)
    lum = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
    return "#000000" if lum > 0.55 else "#ffffff"


class SettingsWidget(QWidget):
    visualizer_changed   = Signal(dict)
    oscilloscope_changed = Signal(dict)
    playback_changed     = Signal(dict)
    nowplaying_changed   = Signal(dict)
    lyrics_changed       = Signal(dict)
    appearance_changed   = Signal(dict)
    theme_style_changed  = Signal(dict)   # accent / background / text / frame-shade
    font_changed         = Signal(str)   # emits font family name
    ui_size_changed      = Signal(int)   # emits px size
    system_theme_changed = Signal(bool)
    normalize_changed    = Signal(bool)
    crossfade_changed    = Signal(int)    # crossfade length in ms (0 = off)
    discord_changed      = Signal(dict)
    layout_export        = Signal(object)
    layout_import        = Signal(object)
    config_export        = Signal(object)   # Path
    config_import        = Signal(object)   # Path
    audio_device_changed = Signal(object)
    docking_changed      = Signal(bool)
    viz_auto_suspend_changed = Signal(bool)
    scrobble_config_changed = Signal(str, dict)   # (service name, {enabled, api_key, api_secret})
    scrobble_connect        = Signal(str)         # service name — start browser auth
    scrobble_disconnect     = Signal(str)         # service name — forget session
    play_stats_changed      = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = self._load()
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
            _CFG_FILE.write_text(json.dumps(self._cfg, indent=2), encoding="utf-8")
        except Exception:
            from applog import log
            log.exception("failed to save settings to %s", _CFG_FILE)

    def get(self, key, default=None):
        return self._cfg.get(key, default)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_playback(),     "Playback")
        self._tabs.addTab(self._tab_visualizer(),   "Visualizer")
        self._tabs.addTab(self._tab_oscilloscope(), "Oscilloscope")
        self._tabs.addTab(self._tab_lyrics(),       "Lyrics")
        self._tabs.addTab(self._tab_appearance(),   "Appearance")
        self._tabs.addTab(self._tab_audio(),        "Audio")
        self._tabs.addTab(self._tab_layout(),       "Layout")
        self._tabs.addTab(self._tab_scrobbling(),   "Scrobbling")
        self._tabs.addTab(self._tab_stats(),        "Stats")
        root.addWidget(self._tabs)

    def _scroll_wrap(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidget(inner)
        return sa

    def _tab_playback(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        c    = self._cfg

        self._normalize_chk = QCheckBox("Normalize volume (ReplayGain / peak)")
        self._normalize_chk.setChecked(c["normalize"])
        self._normalize_chk.setToolTip(
            "Levels tracks to a consistent loudness with a single gain that\n"
            "preserves each song's dynamics (quiet stays quiet). Uses embedded\n"
            "ReplayGain/R128 tags when present, otherwise the track's true peak."
        )
        form.addRow("", self._normalize_chk)

        self._normalize_chk.toggled.connect(self._apply_normalize)

        # Crossfade between consecutive audio tracks (0 = off → gapless).
        self._xfade_sl, self._xfade_lbl = _hslider(0, 12, c.get("crossfade_ms", 0) // 1000, " s")
        self._xfade_sl.setToolTip("Overlap the end of one track with the start of the next.")
        self._xfade_sl.valueChanged.connect(
            lambda v: self._xfade_lbl.setText("Off" if v == 0 else f"{v} s"))
        self._xfade_lbl.setText("Off" if self._xfade_sl.value() == 0 else f"{self._xfade_sl.value()} s")
        self._xfade_sl.sliderReleased.connect(self._apply_crossfade)
        form.addRow("Crossfade", _row(self._xfade_sl, self._xfade_lbl))

        form.addRow(QLabel(""))  # spacer
        disc_hdr = QLabel("Discord Rich Presence")
        disc_hdr.setStyleSheet("font-weight: bold;")
        form.addRow(disc_hdr)

        self._discord_enable = QCheckBox("Show what I'm playing on Discord")
        self._discord_enable.setChecked(c.get("discord_rpc_enabled", False))
        self._discord_enable.setToolTip("Requires the Discord desktop app running. "
                                        "Album art needs the art URL below.")
        self._discord_enable.toggled.connect(lambda _: self._apply_discord())
        form.addRow("", self._discord_enable)

        self._discord_client_id = QLineEdit(c.get("discord_client_id", ""))
        self._discord_client_id.setPlaceholderText("Discord application client ID")
        self._discord_client_id.editingFinished.connect(self._apply_discord)
        form.addRow("App ID", self._discord_client_id)

        self._discord_art_base = QLineEdit(c.get("discord_art_base_url", ""))
        self._discord_art_base.setPlaceholderText("Public base URL for album art")
        self._discord_art_base.editingFinished.connect(self._apply_discord)
        form.addRow("Art URL", self._discord_art_base)

        self._discord_art_upload = QLineEdit(c.get("discord_art_upload_cmd", ""))
        self._discord_art_upload.setPlaceholderText("optional: command to publish art, {file} = local path")
        self._discord_art_upload.setToolTip(
            "If the Art URL is hosted elsewhere, this shell command uploads each art "
            "file there. {file} is replaced with the local path (e.g. an scp/rsync command).")
        self._discord_art_upload.editingFinished.connect(self._apply_discord)
        form.addRow("Art upload", self._discord_art_upload)

        return self._scroll_wrap(w)

    def _tab_visualizer(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        c    = self._cfg

        self._viz_enabled = QCheckBox("Enable FFT spectrum")
        self._viz_enabled.setChecked(c["viz_fft_enabled"])
        form.addRow("", self._viz_enabled)

        self._viz_gain_sl, self._viz_gain_lbl = _hslider(0, 400, c["viz_fft_gain"], "%",
            tip="Spectrum bar height. Above 100% boosts quiet material; lower it if bars clip the top.")
        form.addRow("Gain", _row(self._viz_gain_sl, self._viz_gain_lbl))

        self._viz_show_art = QCheckBox("Show album art background")
        self._viz_show_art.setChecked(c.get("viz_show_art", True))
        form.addRow("", self._viz_show_art)

        self._viz_opacity_sl, self._viz_opacity_lbl = _hslider(0, 100, c["viz_art_opacity"], "%",
            tip="Opacity of the album-art backdrop shown behind the spectrum bars.")
        form.addRow("Art opacity", _row(self._viz_opacity_sl, self._viz_opacity_lbl))

        self._viz_bars_sl, self._viz_bars_lbl = _hslider(8, 128, c["viz_bar_count"],
            tip="Number of frequency bars — more bars = finer detail, slightly more CPU.")
        form.addRow("Bar count", _row(self._viz_bars_sl, self._viz_bars_lbl))

        self._viz_gap_sl, self._viz_gap_lbl = _hslider(0, 8, c["viz_bar_gap"], "px",
            tip="Gap between spectrum bars, in pixels.")
        form.addRow("Bar gap", _row(self._viz_gap_sl, self._viz_gap_lbl))

        self._viz_corner_sl, self._viz_corner_lbl = _hslider(0, 10, c["viz_corner_r"], "px",
            tip="Corner rounding of each bar, in pixels (0 = square).")
        form.addRow("Corner radius", _row(self._viz_corner_sl, self._viz_corner_lbl))

        self._viz_peaks = QCheckBox("Show peak hold")
        self._viz_peaks.setChecked(c["viz_peak_hold"])
        form.addRow("", self._viz_peaks)

        # Connect signals after all widgets exist so _apply_visualizer can read them all
        self._viz_enabled.toggled.connect(lambda _: self._apply_visualizer())
        self._viz_gain_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_show_art.toggled.connect(lambda _: self._apply_visualizer())
        self._viz_opacity_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_bars_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_gap_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_corner_sl.valueChanged.connect(lambda _: self._apply_visualizer())
        self._viz_peaks.toggled.connect(lambda _: self._apply_visualizer())

        return self._scroll_wrap(w)

    def _tab_oscilloscope(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        c    = self._cfg

        self._osc_win_sl, self._osc_win_lbl = _hslider(5, 500, c["osc_window_ms"], "ms",
            tip="How much time the scope shows at once. Smaller = more zoomed-in on the waveform.")
        form.addRow("Window", _row(self._osc_win_sl, self._osc_win_lbl))

        self._osc_gain_sl, self._osc_gain_lbl = _hslider(10, 500, c["osc_gain"], "%",
            tip="Vertical scale of the trace. Raise it for quiet tracks, lower it if the trace clips.")
        form.addRow("Gain", _row(self._osc_gain_sl, self._osc_gain_lbl))

        self._osc_smooth_sl, self._osc_smooth_lbl = _hslider(0, 20, max(0, c["osc_smooth"]),
            tip="Averages this many frames to steady the trace. Higher = smoother but laggier.")
        form.addRow("Smoothing", _row(self._osc_smooth_sl, self._osc_smooth_lbl))

        self._osc_lw_sl, self._osc_lw_lbl = _hslider(5, 40, c["osc_line_width"],
            tip="Thickness of the scope trace (value ×0.1 pixels).")
        form.addRow("Line width ×0.1", _row(self._osc_lw_sl, self._osc_lw_lbl))

        self._osc_fps_sl, self._osc_fps_lbl = _hslider(15, 120, c.get("osc_fps", 60), " fps")
        form.addRow("Frame rate", _row(self._osc_fps_sl, self._osc_fps_lbl))

        self._osc_trigger = QCheckBox("Zero-crossing trigger (stabilise waveform)")
        self._osc_trigger.setChecked(c.get("osc_trigger", True))
        self._osc_trigger.setToolTip(
            "Aligns the trace to the first rising zero-crossing each frame so the\n"
            "waveform holds still instead of scrolling sideways."
        )
        form.addRow("", self._osc_trigger)

        self._osc_win_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_gain_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_smooth_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_lw_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_fps_sl.valueChanged.connect(lambda _: self._apply_oscilloscope())
        self._osc_trigger.toggled.connect(lambda _: self._apply_oscilloscope())

        return self._scroll_wrap(w)

    def _tab_lyrics(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        self._lyr_font_sl, self._lyr_font_lbl = _hslider(10, 36, self._cfg["lyr_font_size"], "pt")
        self._lyr_font_sl.valueChanged.connect(lambda _: self._apply_lyrics())
        form.addRow("Font size", _row(self._lyr_font_sl, self._lyr_font_lbl))

        return self._scroll_wrap(w)

    def _tab_appearance(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        self._swatch_updaters = {}   # key → fn that repaints a colour swatch

        def header(text, top_gap=True):
            if top_gap:
                form.addRow(QLabel(""))
            lbl = QLabel(text)
            lbl.setStyleSheet("font-weight: bold;")
            form.addRow(lbl)

        def hint(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("font-style: italic;")
            lbl.setWordWrap(True)
            form.addRow("", lbl)

        # Theme
        header("Theme", top_gap=False)
        self._system_theme_chk = QCheckBox("Use system style and colours")
        self._system_theme_chk.setChecked(self._cfg.get("system_theme", True))
        self._system_theme_chk.setToolTip(
            "When enabled, uses your OS theme instead of AudioCrate's own colours.")
        self._system_theme_chk.toggled.connect(self._apply_system_theme)
        form.addRow("", self._system_theme_chk)

        self._art_color_chk = QCheckBox("Derive theme from album art")
        self._art_color_chk.setChecked(self._cfg["art_color_theme"])
        self._art_color_chk.toggled.connect(lambda _: self._apply_appearance())
        form.addRow("", self._art_color_chk)
        hint("Falls back to the colours below when no art is available.")

        # Colours
        header("Colours")
        hint("Used by AudioCrate's own theme (when “Use system style” is off).")
        form.addRow("Accent",     self._color_row("accent_color", "Accent colour"))
        form.addRow("Background", self._color_row("bg_color", "Background colour", auto=True))
        form.addRow("Text",       self._color_row("text_color", "Text colour", auto=True))

        self._shade_sl, self._shade_lbl = _hslider(0, 100, self._cfg.get("frame_shade", 0), "%")
        self._shade_sl.setToolTip("Shades panel title bars with a gradient for depth.")
        self._shade_sl.valueChanged.connect(lambda v: self._shade_lbl.setText(f"{v}%"))
        self._shade_sl.sliderReleased.connect(self._apply_theme_style)
        form.addRow("Frame shading", _row(self._shade_sl, self._shade_lbl))

        # Text & size
        header("Text & size")
        self._font_combo = QFontComboBox()
        self._font_combo.blockSignals(True)
        self._font_combo.setFontFilters(QFontComboBox.FontFilter.AllFonts)
        saved = self._cfg.get("text_font", "")
        if saved:
            from PySide6.QtGui import QFont as _QFont
            self._font_combo.setCurrentFont(_QFont(saved))
        self._font_combo.blockSignals(False)
        self._font_combo.currentFontChanged.connect(self._apply_font)
        form.addRow("UI text font", self._font_combo)

        reset_font_btn = QPushButton("Reset to default")
        reset_font_btn.clicked.connect(self._reset_font)
        form.addRow("", reset_font_btn)

        # UI size as percentage: 100% = 13px (default), range 75%–200%
        _saved_pct = round(self._cfg.get("ui_size", 13) / 13 * 100)
        self._ui_size_sl, self._ui_size_lbl = _hslider(75, 200, _saved_pct, "%")
        self._ui_size_timer = None  # debounce timer, created lazily
        self._ui_size_sl.valueChanged.connect(self._on_ui_size_drag)
        self._ui_size_sl.sliderReleased.connect(self._commit_ui_size)
        form.addRow("UI size", _row(self._ui_size_sl, self._ui_size_lbl))

        return self._scroll_wrap(w)

    # colour pickers (accent / background / text)
    def _color_row(self, key: str, dialog_title: str, auto: bool = False):
        """A swatch button + reset, wired to cfg[key]. auto → empty value means
        'derive automatically' and the reset button restores that."""
        btn   = QPushButton(); btn.setFixedHeight(26)
        reset = QPushButton("Auto" if auto else "Reset")

        def update():
            val = self._cfg.get(key, "")
            if not val:
                btn.setText("Auto"); btn.setStyleSheet("")
            else:
                btn.setText(val)
                btn.setStyleSheet(
                    f"background:{val}; color:{_contrast(val)}; "
                    f"border:1px solid #00000080; border-radius:4px;")

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

    def _apply_theme_style(self):
        self._cfg["frame_shade"] = self._shade_sl.value()
        self.save()
        self.theme_style_changed.emit({
            "accent":      self._cfg.get("accent_color", "#8040c8"),
            "bg":          self._cfg.get("bg_color", ""),
            "text":        self._cfg.get("text_color", ""),
            "frame_shade": self._cfg["frame_shade"],
        })

    def _apply_system_theme(self, on: bool):
        self._cfg["system_theme"] = on
        self.save()
        self.system_theme_changed.emit(on)

    def _apply_font(self, font):
        name = font.family()
        # Don't allow selecting the Nerd Font — it would break glyphs
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
        self.save()
        self.font_changed.emit("")

    def _on_ui_size_drag(self):
        # Called on every tick — only update label, don't apply QSS yet
        pass  # label is wired inside _hslider already

    def _commit_ui_size(self):
        pct  = self._ui_size_sl.value()
        size = max(9, round(13 * pct / 100))
        self._cfg["ui_size"] = size
        self.save()
        self.ui_size_changed.emit(size)

    def _apply_ui_size(self, size: int):
        # Legacy path used by _reset_defaults — converts px → pct for the slider
        pct = round(size / 13 * 100)
        self._ui_size_sl.blockSignals(True)
        self._ui_size_sl.setValue(pct)
        self._ui_size_sl.blockSignals(False)
        self._ui_size_lbl.setText(f"{pct}%")
        self._cfg["ui_size"] = size
        self.save()
        self.ui_size_changed.emit(size)

    def _tab_audio(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        self._audio_combo = QComboBox()
        self._audio_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._populate_audio_devices()
        # No QMediaDevices instance here to keep the list live. Constructing one
        # starts Qt's PipeWire device monitor, which segfaults inside its own
        # registry callback on this PySide6. The Refresh button next to the box
        # re-reads the list, and the player recovers on its own when the output
        # it is using stops working. See Player._on_device_lost.
        self._audio_combo.currentIndexChanged.connect(self._on_audio_device_changed)
        form.addRow("Output device", self._audio_combo)

        refresh_btn = QPushButton("Refresh devices")
        refresh_btn.clicked.connect(self._populate_audio_devices)
        form.addRow(refresh_btn)

        return self._scroll_wrap(w)

    def _tab_layout(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        hint = QLabel("Save or restore the full panel layout (positions, sizes, floating state, visibility).")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-style: italic;")
        form.addRow(hint)

        self._docking_chk = QCheckBox("Allow docking (panels snap when dragged)")
        self._docking_chk.setChecked(self._cfg.get("docking_enabled", True))
        self._docking_chk.toggled.connect(self._apply_docking)
        form.addRow("", self._docking_chk)

        self._viz_auto_suspend_chk = QCheckBox("Auto-suspend visualizers")
        self._viz_auto_suspend_chk.setChecked(self._cfg.get("viz_auto_suspend", True))
        self._viz_auto_suspend_chk.toggled.connect(self._apply_viz_auto_suspend)
        form.addRow("", self._viz_auto_suspend_chk)

        exp_btn = QPushButton("Export layout…")
        exp_btn.clicked.connect(self._on_export_layout)
        form.addRow(exp_btn)

        imp_btn = QPushButton("Import layout…")
        imp_btn.clicked.connect(self._on_import_layout)
        form.addRow(imp_btn)

        form.addRow(QLabel(""))  # spacer

        cfg_exp_btn = QPushButton("Export all config…")
        cfg_exp_btn.setToolTip("Saves all settings + layout into a single .json file")
        cfg_exp_btn.clicked.connect(self._on_export_config)
        form.addRow(cfg_exp_btn)

        cfg_imp_btn = QPushButton("Import all config…")
        cfg_imp_btn.setToolTip("Restores all settings + layout from a previously exported file")
        cfg_imp_btn.clicked.connect(self._on_import_config)
        form.addRow(cfg_imp_btn)

        form.addRow(QLabel(""))  # spacer

        reset_btn = QPushButton("Reset all settings to defaults")
        reset_btn.clicked.connect(self._reset_defaults)
        form.addRow(reset_btn)

        return self._scroll_wrap(w)

    def _tab_scrobbling(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        hint = QLabel(
            "Scrobbles once a track has played 50% (or 4 minutes, whichever is "
            "sooner). Each service needs its own free API key from your account "
            "there — last.fm/api/account/create or libre.fm/api/account/create."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-style: italic;")
        form.addRow(hint)

        self._scrobble_widgets = {}
        for service, label in (("lastfm", "Last.fm"), ("librefm", "Libre.fm")):
            form.addRow(QLabel(""))  # spacer
            hdr = QLabel(label)
            hdr.setStyleSheet("font-weight: bold;")
            form.addRow(hdr)

            enable = QCheckBox(f"Scrobble to {label}")
            enable.setChecked(self._cfg.get(f"{service}_enabled", False))
            form.addRow("", enable)

            key = QLineEdit(self._cfg.get(f"{service}_api_key", ""))
            key.setPlaceholderText("API key")
            form.addRow("API key", key)

            secret = QLineEdit(self._cfg.get(f"{service}_api_secret", ""))
            secret.setPlaceholderText("API secret")
            secret.setEchoMode(QLineEdit.Password)
            form.addRow("API secret", secret)

            status = QLabel("Not connected")
            connect_btn = QPushButton("Connect…")
            disconnect_btn = QPushButton("Disconnect")
            disconnect_btn.setVisible(False)
            btn_row = QHBoxLayout()
            btn_row.addWidget(connect_btn)
            btn_row.addWidget(disconnect_btn)
            btn_row.addWidget(status, 1)
            form.addRow("", btn_row)

            self._scrobble_widgets[service] = {
                "enable": enable, "key": key, "secret": secret,
                "status": status, "connect": connect_btn, "disconnect": disconnect_btn,
            }

            enable.toggled.connect(lambda _, s=service: self._apply_scrobble_config(s))
            key.editingFinished.connect(lambda s=service: self._apply_scrobble_config(s))
            secret.editingFinished.connect(lambda s=service: self._apply_scrobble_config(s))
            connect_btn.clicked.connect(lambda _, s=service: self._on_scrobble_connect(s))
            disconnect_btn.clicked.connect(lambda _, s=service: self._on_scrobble_disconnect(s))

        return self._scroll_wrap(w)

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
        self._apply_scrobble_config(service)   # make sure key/secret are current first
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

    def _apply_viz_auto_suspend(self, on: bool):
        self._cfg["viz_auto_suspend"] = on
        self.save()
        self.viz_auto_suspend_changed.emit(on)

    def _tab_stats(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        hint = QLabel(
            "Tracks most-played songs/artists and total listening time, entirely "
            "locally (~/.config/audiocrate/play_stats.json) — nothing is ever "
            "sent anywhere. A track counts as \"played\" the same way scrobbling "
            "counts it: 50% listened (or 4 minutes), whichever is sooner."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-style: italic;")
        form.addRow(hint)

        self._stats_enable = QCheckBox("Track local listening stats")
        self._stats_enable.setChecked(self._cfg.get("play_stats_enabled", False))
        self._stats_enable.toggled.connect(self._apply_play_stats)
        form.addRow("", self._stats_enable)

        return self._scroll_wrap(w)

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
        })
        self.save()
        self.oscilloscope_changed.emit({
            "window_ms":  self._cfg["osc_window_ms"],
            "gain":       self._cfg["osc_gain"],
            "smooth":     self._cfg["osc_smooth"],
            "line_width": self._cfg["osc_line_width"] / 10.0,
            "trigger":    self._cfg["osc_trigger"],
            "fps":        self._cfg["osc_fps"],
        })

    def _apply_lyrics(self):
        self._cfg["lyr_font_size"] = self._lyr_font_sl.value()
        self.save()
        self.lyrics_changed.emit({"font_size": self._cfg["lyr_font_size"]})

    def _apply_appearance(self):
        self._cfg["art_color_theme"] = self._art_color_chk.isChecked()
        self.save()
        self.appearance_changed.emit({"art_color_theme": self._cfg["art_color_theme"]})

    def _apply_normalize(self, on: bool):
        self._cfg["normalize"] = on
        self.save()
        self.normalize_changed.emit(on)

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
            self, "Export Layout", str(Path.home() / "layout.json"), "JSON (*.json)"
        )
        if path:
            self.layout_export.emit(Path(path))

    def _on_import_layout(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Layout", str(Path.home()), "JSON (*.json)"
        )
        if path:
            self.layout_import.emit(Path(path))

    def _on_export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export All Config", str(Path.home() / "audiocrate_config.json"), "JSON (*.json)"
        )
        if path:
            self.config_export.emit(Path(path))

    def _on_import_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import All Config", str(Path.home()), "JSON (*.json)"
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
        self._cfg = dict(_DEFAULTS)
        self.save()
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
        # Lyrics
        self._lyr_font_sl.setValue(self._cfg["lyr_font_size"])
        # Appearance
        self._system_theme_chk.blockSignals(True)
        self._system_theme_chk.setChecked(self._cfg["system_theme"])
        self._system_theme_chk.blockSignals(False)
        self._art_color_chk.setChecked(self._cfg["art_color_theme"])
        # Force the theme to re-apply even when the checkbox value didn't change
        # (setChecked emits no signal if already in that state) — otherwise a reset
        # could leave a previously-applied art/app theme showing while the box
        # reads "system". Done before the other appearance applies below.
        self._apply_system_theme(self._cfg["system_theme"])
        self._apply_ui_size(self._cfg["ui_size"])
        self._font_combo.blockSignals(True)
        from PySide6.QtGui import QFont as _QFont
        self._font_combo.setCurrentFont(_QFont("JetBrains Mono"))
        self._font_combo.blockSignals(False)
        for update in self._swatch_updaters.values():
            update()
        self._shade_sl.setValue(self._cfg["frame_shade"])
        # Playback
        self._normalize_chk.setChecked(self._cfg["normalize"])
        self._xfade_sl.setValue(self._cfg["crossfade_ms"] // 1000)
        self._discord_enable.setChecked(self._cfg["discord_rpc_enabled"])
        self._discord_client_id.setText(self._cfg["discord_client_id"])
        self._discord_art_base.setText(self._cfg["discord_art_base_url"])
        self._discord_art_upload.setText(self._cfg.get("discord_art_upload_cmd", ""))
        # Emit signals to apply everything
        self._apply_visualizer()
        self._apply_oscilloscope()
        self._apply_lyrics()
        self._apply_appearance()
        self._apply_normalize(self._cfg["normalize"])
        self._apply_crossfade()
        self._apply_discord()
        self._apply_theme_style()
        self.font_changed.emit("")
        # ui_size_changed already emitted inside _apply_ui_size above
