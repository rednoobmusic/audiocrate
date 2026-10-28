_QSS_TEMPLATE = """
QMainWindow, QWidget {{
    background: {bg};
    color: {fg};
    font-family: {text_font}"JetBrainsMonoNL Nerd Font Propo", "JetBrainsMono Nerd Font Propo", "JetBrains Mono", monospace;
    font-size: {ui_size}px;
}}
QDockWidget {{
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
    color: {fg_dim};
    font-size: 11px;
    font-weight: bold;
}}
QDockWidget::title {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {title_top}, stop:1 {title_bot});
    padding: 4px 8px;
    border-bottom: 1px solid {panel_border};
}}
QDockWidget::close-button, QDockWidget::float-button {{
    background: transparent;
    border: none;
    padding: 2px;
}}
QFrame#dock_titlebar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {bar_top}, stop:1 {bar_bot});
    border: 1px solid {panel_border};
}}
QFrame#dock_titlebar QLabel {{ background: transparent; }}
QFrame#panel_frame {{
    background: transparent;
    border: 1px solid {panel_border};
    border-top: none;
}}
QTabWidget::pane {{
    border: 1px solid {panel_border};
    background: {bg2};
}}
QTabBar::tab {{
    background: {bg2};
    color: {fg_dimmer};
    padding: 5px 14px;
    border: 1px solid {panel_border};
    border-bottom: none;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {bg3};
    color: {fg};
    border-bottom: 2px solid {accent};
}}
QTabBar::tab:hover {{ background: {bg3}; color: {fg_dim}; }}
QListWidget {{
    background: {bg2};
    border: none;
    outline: none;
    alternate-background-color: {bg};
}}
QListWidget::item {{ padding: 4px 8px; border: none; }}
QListWidget::item:selected {{ background: {sel}; color: {fg}; }}
QListWidget::item:hover {{ background: {bg3}; }}
QTreeView, QTreeWidget {{
    background: {bg2};
    border: none;
    outline: none;
    alternate-background-color: {bg};
}}
QTreeView::item {{ padding: 3px 4px; }}
QTreeView::item:selected {{ background: {sel}; color: {fg}; }}
QTreeView::item:hover {{ background: {bg3}; }}
QPushButton {{
    background: {bg3};
    color: {fg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 5px 12px;
    min-width: 28px;
}}
QPushButton:hover {{ background: {accent_d}; border-color: {accent}; }}
QPushButton:pressed {{ background: {accent}; }}
QPushButton:checked {{ background: {accent}; border-color: {accent}; color: {bg}; }}
QPushButton:checked:hover {{ background: {accent_l}; border-color: {accent_l}; color: {bg}; }}
QPushButton:flat {{ background: transparent; border: none; color: {fg_dim}; }}
QPushButton:flat:hover {{ color: {fg}; background: {bg3}; }}
QSlider::groove:horizontal {{
    height: 4px;
    background: {bg3};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {accent};
    width: 14px; height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {accent_l}; }}
QSlider::sub-page:horizontal {{ background: {accent_d}; border-radius: 2px; }}
QLineEdit, QTextEdit {{
    background: {bg2};
    border: 1px solid {border};
    border-radius: 3px;
    padding: 4px 8px;
    color: {fg};
    selection-background-color: {sel};
}}
QLineEdit:focus, QTextEdit:focus {{ border-color: {accent}; }}
QScrollBar:vertical {{
    background: {bg};
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {border};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {accent_d}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {bg};
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {border};
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{ background: {accent_d}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QSplitter::handle {{ background: {panel_border}; }}
QMenuBar {{ background: {bg}; border-bottom: 1px solid {panel_border}; }}
{separator_rule}
QMenuBar::item {{ padding: 4px 10px; }}
QMenuBar::item:selected {{ background: {bg3}; }}
QMenu {{ background: {bg2}; border: 1px solid {border}; padding: 4px; }}
QMenu::item {{ padding: 5px 24px 5px 12px; border-radius: 3px; }}
QMenu::item:selected {{ background: {sel}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}
QLabel#settings_section {{ color: {accent_l}; font-weight: bold; }}
QLabel#settings_hint {{ color: {fg_dim}; placeholder-text-color: {fg_dim}; }}
QFrame#settings_rule {{ background: {border}; border: none; min-height: 1px; max-height: 1px; }}
QPushButton#settings_nav {{ padding: 5px 4px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    background: {bg2};
    border: 1px solid {border};
    border-radius: 3px;
}}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent_l}; }}
QComboBox {{
    background: {bg2};
    color: {fg};
    border: 1px solid {border};
    border-radius: 3px;
    padding: 3px 6px;
    selection-background-color: {sel};
}}
QComboBox:hover, QComboBox:focus, QComboBox:on {{ border-color: {accent}; }}
QComboBox::drop-down {{ width: 18px; }}
QComboBox QAbstractItemView {{
    background: {bg2};
    border: 1px solid {border};
    selection-background-color: {sel};
    outline: none;
}}
QLabel#track_title  {{ font-size: 15px; font-weight: bold; color: {fg}; }}
QLabel#track_sub    {{ font-size: 12px; color: {fg_dimmer}; }}
QLabel#time_label   {{ font-size: 12px; color: {fg_dim}; font-family: monospace; }}
QStatusBar {{ background: {bg}; color: {fg_dimmer}; font-size: 11px; }}
QToolBar {{ background: {bg}; border: none; spacing: 4px; }}
QDialog {{ background: {bg2}; }}
QToolTip {{
    background: {bg2};
    color: {fg};
    border: 1px solid {accent};
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
}}
"""


_DEFAULT_PALETTE = {
    "bg":        "#0e0c14",
    "bg2":       "#120f1c",
    "bg3":       "#1a1525",
    "border":    "#2e2248",
    "accent":    "#8040c8",
    "accent_l":  "#a060e8",
    "accent_d":  "#3d2870",
    "fg":        "#d4cce6",
    "fg_dim":    "#a08ec0",
    "fg_dimmer": "#605080",
    "sel":       "#2d1f55",
}

_DEFAULT_UI_SIZE = 13
DEFAULT_ACCENT = _DEFAULT_PALETTE["accent"]

from PySide6.QtGui import QColor


def _hsv(h: int, s: int, v: int) -> QColor:
    """HSV colour; a hue of -1 (achromatic, as QColor reports it) is taken as 0."""
    c = QColor()
    c.setHsv(max(0, h), max(0, min(255, s)), max(0, min(255, v)))
    return c


def make_qss(
    palette: dict[str, str] | None = None,
    text_font: str | None = None,
    ui_size: int | None = None,
    accent: str | None = None,
    bg: str | None = None,
    text: str | None = None,
    frame_shade: int = 0,
    borders: bool = True,
    style: str = "default",
) -> str:
    """Build the stylesheet for AudioCrate's own (non-system) theme.

    accent      base accent hex; the accent family and (unless bg is given) a
                dark tint of the whole background derive from it.
    bg          explicit base background hex; bg2/bg3/border derive from it.
    text        explicit foreground hex; dim variants derive from it.
    frame_shade 0-100 vertical gradient on panel title bars: lighter on top, darker below.
    borders     False makes the lines between panels (title underlines, tab frames,
                splitter handles, dock separators) transparent.
    palette     album-art palette (full dict); overrides accent/bg/text.
    style       "aero" for the Frutiger Aero look (theme_aero.py); frame_shade does not apply.
    """
    if style == "aero":
        import theme_aero
        return theme_aero.make_qss(palette, accent, bg, text, text_font,
                                   ui_size if ui_size is not None else _DEFAULT_UI_SIZE,
                                   borders, DEFAULT_ACCENT)
    merged = dict(_DEFAULT_PALETTE)
    if palette:
        merged.update(palette)
    else:
        has_accent = bool(accent) and accent.lower() != DEFAULT_ACCENT.lower()
        if has_accent:
            base = QColor(accent)
            merged["accent"]   = base.name()
            merged["accent_l"] = base.lighter(140).name()
            merged["accent_d"] = base.darker(190).name()
            merged["sel"]      = base.darker(300).name()
            merged["border"]   = base.darker(330).name()

        # An explicit background wins. Otherwise a custom accent also tints the
        # background (dark shades of the accent's hue), so it changes the whole
        # window and not just the highlights.
        if bg:
            bb = QColor(bg)
            h, s, v, _ = bb.getHsv()
            merged["bg"]     = bb.name()
            merged["bg2"]    = _hsv(h, s, min(255, v + 8)).name()
            merged["bg3"]    = _hsv(h, s, min(255, v + 18)).name()
            merged["border"] = _hsv(h, max(0, s - 10), min(255, v + 42)).name()
        elif has_accent:
            h, s, _, _ = QColor(accent).getHsv()
            s = min(s, 140)
            merged["bg"]     = _hsv(h, s, 16).name()
            merged["bg2"]    = _hsv(h, s, 24).name()
            merged["bg3"]    = _hsv(h, s, 34).name()
            merged["border"] = _hsv(h, s, 64).name()

        if text:
            tc = QColor(text)
            merged["fg"]        = tc.name()
            merged["fg_dim"]    = tc.darker(135).name()
            merged["fg_dimmer"] = tc.darker(185).name()

    cols = {k: QColor(v) for k, v in merged.items()}
    shade = max(0, min(100, int(frame_shade)))
    p = {k: c.name() for k, c in cols.items()}

    # frame shading: vertical gradient on the dock title bars, normal on top, darker below
    p["title_top"] = cols["bg2"].name()
    p["title_bot"] = cols["bg2"].darker(100 + shade * 2).name()
    # Every dock has its own title bar widget (ui/main_window.py), so QDockWidget::title above is never
    # drawn; the bar is the frame #dock_titlebar. Both ends are the window colour at 0, so nothing changes
    # until the slider moves.
    p["bar_top"] = cols["bg"].lighter(100 + shade * 2).name()
    p["bar_bot"] = cols["bg"].darker(100 + shade * 2).name()

    # The lines between panels: the dock title underline, tab frames, the splitter
    # handle, the menu bar underline and the strips Qt draws between docked panels.
    # Off, they keep their size and turn transparent, so nothing shifts.
    p["panel_border"] = p["border"] if borders else "transparent"
    p["separator_rule"] = "" if borders else "QMainWindow::separator {{ background: transparent; }}".format()

    # chosen font first; the bundled Nerd Font after it still supplies the icon glyphs
    tf = f'"{text_font}", ' if text_font else ""
    sz = ui_size if ui_size is not None else _DEFAULT_UI_SIZE
    return _QSS_TEMPLATE.format(**p, text_font=tf, ui_size=sz)


QSS = make_qss()


