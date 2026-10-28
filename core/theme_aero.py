"""The Frutiger Aero style: glass title bars and buttons, a bright rim, sky blue accents.

An optional look next to the default one (Settings > Appearance > Style). It takes the
same inputs as theme.make_qss and works with the album art colours: the glass layers are
derived from the accent family of whatever palette is in use.
"""
from __future__ import annotations

from PySide6.QtGui import QColor

_QSS_TEMPLATE = """
/* Base */
QMainWindow, QWidget {{
    background: {bg};
    color: {fg};
    font-family: {text_font}"Segoe UI", "Noto Sans", "Open Sans", "Cantarell", "JetBrainsMonoNL Nerd Font Propo", "JetBrainsMono Nerd Font Propo", sans-serif;
    font-size: {ui_size}px;
}}
/* Dock panel body */
QDockWidget > QWidget {{
    background: {dock_bg};
    border: 1px solid {panel_border};
    border-top: none;
}}
/* Scroll areas and their viewports show the panel colour, not the solid bg */
QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
/* Labels, checkboxes and group boxes are transparent over the panel */
QLabel, QCheckBox, QGroupBox, QAbstractSpinBox {{
    background: transparent;
}}

/* Menu bar */
QMenuBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0    {glass_hi},
        stop:0.18 {glass_mid},
        stop:0.5  {glass_lo},
        stop:0.82 {glass_mid},
        stop:1    {glass_edge});
    border-bottom: 1px solid {panel_border};
    padding: 1px 4px;
    color: {fg};
}}
QMenuBar::item {{ padding: 4px 12px; border-radius: 4px; }}
QMenuBar::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent_l}, stop:1 {accent});
    color: #ffffff;
}}
QMenu {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:1 {glass_lo});
    border: 1px solid {border_hi};
    border-radius: 6px;
    padding: 4px;
    color: {fg};
}}
QMenu::item {{ padding: 5px 28px 5px 14px; border-radius: 4px; }}
QMenu::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent_l}, stop:1 {accent});
    color: #ffffff;
}}
QMenu::separator {{ height: 1px; background: {border_hi}; margin: 4px 10px; }}

/* Dock widgets */
QDockWidget {{
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
    color: {fg};
    font-size: 11px;
    font-weight: bold;
}}
QDockWidget::title {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0    {glass_hi},
        stop:0.18 {glass_mid},
        stop:0.5  {glass_lo},
        stop:0.82 {glass_mid},
        stop:1    {glass_edge});
    padding: 5px 10px;
    border-bottom: 1px solid {panel_border};
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    text-align: left;
}}
QDockWidget::close-button, QDockWidget::float-button {{
    background: transparent;
    border: none;
    padding: 2px;
    border-radius: 3px;
}}
QDockWidget::close-button:hover, QDockWidget::float-button:hover {{
    background: rgba(255,255,255,40);
}}
QFrame#dock_titlebar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0    {glass_hi},
        stop:0.18 {glass_mid},
        stop:0.5  {glass_lo},
        stop:0.82 {glass_mid},
        stop:1    {glass_edge});
    border: 1px solid {panel_border};
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QFrame#dock_titlebar QLabel {{ background: transparent; }}
QFrame#panel_frame {{
    background: {dock_bg};
    border: 1px solid {panel_border};
    border-top: none;
}}

/* Tab widget */
QTabWidget::pane {{
    border: 1px solid {panel_border};
    border-radius: 0 6px 6px 6px;
    background: {dock_bg};
    top: -1px;
}}
QTabBar::tab {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_mid}, stop:1 {glass_lo});
    color: {fg_dim};
    padding: 6px 16px;
    border: 1px solid {border};
    border-bottom: none;
    border-radius: 5px 5px 0 0;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.18 {glass_mid}, stop:0.5 {glass_lo},
        stop:0.82 {glass_mid}, stop:1 {glass_edge});
    color: {fg};
    border-color: {border_hi};
    border-bottom: 2px solid {accent};
}}
QTabBar::tab:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:1 {glass_lo});
    color: {fg};
}}

/* List & tree views */
QListWidget {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0   {glass_hi},
        stop:0.08 {glass_mid},
        stop:0.09 {glass_lo},
        stop:1   {bg2});
    border: 1px solid {border_hi};
    border-radius: 6px;
    outline: none;
    alternate-background-color: rgba(255, 255, 255, 6);
}}
QListWidget::item {{ padding: 5px 10px; border-radius: 4px; }}
QListWidget::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent_l}, stop:1 {accent});
    color: #ffffff;
    border: 1px solid {border_hi};
    border-radius: 4px;
}}
QListWidget::item:hover {{
    background: rgba(255, 255, 255, 12);
    border-radius: 4px;
}}

QTreeView, QTreeWidget {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0   {glass_hi},
        stop:0.08 {glass_mid},
        stop:0.09 {glass_lo},
        stop:1   {bg2});
    border: 1px solid {border_hi};
    border-radius: 6px;
    outline: none;
    alternate-background-color: rgba(255, 255, 255, 6);
}}
QTreeView::item {{ padding: 4px 6px; border-radius: 4px; }}
QTreeView::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent_l}, stop:1 {accent});
    color: #ffffff;
    border: 1px solid {border_hi};
    border-radius: 4px;
}}
QTreeView::item:hover {{
    background: rgba(255, 255, 255, 12);
    border-radius: 4px;
}}
QTreeView::branch:selected {{ background: transparent; }}

/* Buttons */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0    {btn_hi},
        stop:0.18 {btn_mid},
        stop:0.5  {btn_lo},
        stop:0.82 {btn_mid},
        stop:1    {glass_edge});
    color: {fg};
    border: 1px solid {border_hi};
    border-radius: 6px;
    padding: 5px 14px;
    min-width: 28px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent_ll}, stop:0.5 {accent_l}, stop:1 {accent});
    border-color: {accent_l};
    color: #ffffff;
}}
QPushButton:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent}, stop:1 {accent_d});
    border-color: {accent};
    color: #ffffff;
}}
QPushButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent_l}, stop:1 {accent});
    border-color: {accent};
    color: #ffffff;
}}
QPushButton:flat {{ background: transparent; border: none; color: {fg_dim}; }}
QPushButton:flat:hover {{ background: rgba(255,255,255,25); color: {fg}; border-radius: 4px; }}

/* Sliders */
QSlider {{ background: transparent; }}
QSlider::groove:horizontal {{
    height: 5px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.3 {glass_mid}, stop:1 {glass_lo});
    border: 1px solid {border_hi};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #ffffff, stop:0.4 {accent_l}, stop:1 {accent});
    border: 1px solid {accent};
    width: 16px; height: 16px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::handle:horizontal:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #ffffff, stop:0.3 {accent_ll}, stop:1 {accent_l});
}}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent_d}, stop:1 {accent_l});
    border-radius: 3px;
}}

/* Text inputs */
QLineEdit, QTextEdit {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.12 {glass_mid}, stop:0.13 {glass_lo}, stop:1 {dock_bg});
    border: 1px solid {border_hi};
    border-radius: 5px;
    padding: 5px 9px;
    color: {fg};
    selection-background-color: {sel};
}}
QLineEdit:focus, QTextEdit:focus {{
    border: 1px solid {accent};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.15 {glass_mid}, stop:1 {glass_lo});
}}

QSpinBox, QDoubleSpinBox, QComboBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_mid}, stop:1 {glass_lo});
    border: 1px solid {border_hi};
    border-radius: 5px;
    padding: 4px 8px;
    color: {fg};
    selection-background-color: {sel};
}}
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {accent};
}}
QComboBox::drop-down {{
    border: none;
    padding-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_lo}, stop:1 {dock_bg});
    border: 1px solid {border_hi};
    border-radius: 5px;
    selection-background-color: {sel};
    color: {fg};
}}

/* Scrollbars */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {glass_hi}, stop:0.5 {border_hi}, stop:1 {accent_d});
    border-radius: 5px;
    min-height: 24px;
    border: 1px solid {border_hi};
}}
QScrollBar::handle:vertical:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {glass_hi}, stop:0.4 {accent_l}, stop:1 {accent});
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.5 {border_hi}, stop:1 {accent_d});
    border-radius: 5px;
    min-width: 24px;
    border: 1px solid {border_hi};
}}
QScrollBar::handle:horizontal:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.4 {accent_l}, stop:1 {accent});
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* Checkboxes */
QCheckBox {{
    spacing: 8px;
    color: {fg};
    background: transparent;
}}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {border_hi};
    border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_mid}, stop:1 {glass_lo});
}}
QCheckBox::indicator:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.2 {accent_l}, stop:1 {accent});
    border-color: {accent};
}}
QCheckBox::indicator:hover {{ border-color: {accent_l}; }}

/* Splitter */
QSplitter::handle {{
    background: {panel_border};
    width: 1px;
    height: 1px;
    margin: 2px;
}}
QSplitter::handle:hover {{ background: {accent}; }}

/* Frame separators (HLine / VLine) */
QFrame[frameShape="4"] {{ /* HLine */
    border: none;
    border-top: 1px solid {border_hi};
    height: 1px;
    background: transparent;
}}
QFrame[frameShape="5"] {{ /* VLine */
    border: none;
    border-left: 1px solid {border_hi};
    width: 1px;
    background: transparent;
}}


/* Status bar */
QStatusBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.18 {glass_mid}, stop:0.5 {glass_lo},
        stop:0.82 {glass_mid}, stop:1 {glass_edge});
    color: {fg_dimmer};
    font-size: 11px;
    border-top: 1px solid {panel_border};
}}

/* Dialogs */
QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {glass_hi}, stop:0.1 {glass_mid}, stop:0.11 {glass_lo}, stop:1 {dock_bg});
    border: 1px solid {border_hi};
    border-radius: 8px;
}}
QDialogButtonBox QPushButton {{ min-width: 72px; }}

/* Form layout labels */
QFormLayout QLabel {{ color: {fg_dim}; background: transparent; }}

/* Group box */
QGroupBox {{
    border: 1px solid {border};
    border-radius: 6px;
    margin-top: 8px;
    padding-top: 6px;
    font-weight: bold;
    color: {fg_dim};
    background: transparent;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    background: transparent;
}}

/* Player-specific labels */
QLabel#track_title {{
    font-size: 15px;
    font-weight: bold;
    color: {fg};
}}
QLabel#track_sub    {{ font-size: 12px; color: {fg_dim}; }}
QLabel#time_label   {{ font-size: 12px; color: {fg_dim}; font-family: monospace; }}
QLabel#next_up_label {{
    font-size: 11px;
    color: {accent_l};
    font-style: italic;
}}

/* Scroll area */
QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{
    border: none;
    background: transparent;
}}

/* Tool bar */
QToolBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0    {glass_hi},
        stop:0.18 {glass_mid},
        stop:0.5  {glass_lo},
        stop:0.82 {glass_mid},
        stop:1    {glass_edge});
    border: none;
    spacing: 4px;
}}
{separator_rule}
QLabel#settings_section {{ color: {accent_ll}; font-weight: bold; }}
QLabel#settings_hint {{ color: {fg_dim}; placeholder-text-color: {fg_dim}; }}
QFrame#settings_rule {{ background: {border_hi}; border: none; min-height: 1px; max-height: 1px; }}
QPushButton#settings_nav {{ padding: 5px 4px; }}
QToolTip {{
    background: {glass_lo};
    color: {fg};
    border: 1px solid {accent};
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
}}
"""

# Deep blue-green base like a Windows 7 taskbar, sky blue accents, glassy whites.
_PALETTE = {
    "bg":        "#071018",
    "bg2":       "#0c1a28",
    "bg3":       "#122034",
    "border":    "#183650",
    "accent":    "#1a9fd4",
    "accent_l":  "#45c0f0",
    "accent_ll": "#90deff",
    "accent_d":  "#0d6a96",
    "fg":        "#e0f4ff",
    "fg_dim":    "#7ab8d8",
    "fg_dimmer": "#3d6880",
    "sel":       "rgba(26, 159, 212, 60)",
}


def _rgba(color: str, alpha: int) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _glass(p: dict) -> None:
    """The glass layers, from the colours the rest of the palette already has."""
    p["border_hi"]  = _rgba(p["accent_l"], 100)     # the bright rim
    p["glass_hi"]   = "rgba(255, 255, 255, 90)"     # the shine along the top
    p["glass_mid"]  = _rgba(p["accent_l"], 35)
    p["glass_lo"]   = _rgba(p["bg"], 230)           # the deep middle
    p["glass_edge"] = _rgba(p["bg2"], 200)
    p["dock_bg"]    = p["bg2"]
    p["btn_hi"]     = "rgba(255, 255, 255, 65)"
    p["btn_mid"]    = _rgba(p["accent_d"], 160)
    p["btn_lo"]     = _rgba(p["bg2"], 230)


def make_qss(palette: dict | None, accent: str | None, bg: str | None, text: str | None,
             text_font: str | None, ui_size: int, borders: bool, default_accent: str) -> str:
    p = dict(_PALETTE)
    if palette:                                  # album art
        for k in ("bg", "bg2", "bg3", "border", "accent", "accent_l", "accent_d",
                  "fg", "fg_dim", "fg_dimmer"):
            if k in palette:
                p[k] = palette[k]
        p["accent_ll"] = palette.get("accent_l", p["accent_l"])
        p["sel"] = _rgba(p["accent"], 70)
    else:
        if accent and accent.lower() != default_accent.lower():
            base = QColor(accent)
            p["accent"], p["accent_l"] = base.name(), base.lighter(140).name()
            p["accent_ll"], p["accent_d"] = base.lighter(190).name(), base.darker(190).name()
            p["sel"] = _rgba(p["accent"], 70)
        if bg:
            b = QColor(bg)
            p["bg"], p["bg2"] = b.name(), b.lighter(112).name()
            p["bg3"], p["border"] = b.lighter(125).name(), b.lighter(150).name()
        if text:
            t = QColor(text)
            p["fg"], p["fg_dim"], p["fg_dimmer"] = t.name(), t.darker(135).name(), t.darker(185).name()
    _glass(p)
    p["panel_border"] = p["border_hi"] if borders else "transparent"
    p["separator_rule"] = "" if borders else "QMainWindow::separator {{ background: transparent; }}".format()
    p["text_font"] = f'"{text_font}", ' if text_font else ""
    p["ui_size"] = ui_size
    return _QSS_TEMPLATE.format(**p)
