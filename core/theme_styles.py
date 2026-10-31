"""Themes with a style of their own: a texture, gradients, bevels or glowing edges on top of the colours.

Each style is a function that returns more rules for the stylesheet theme.make_qss builds. They go after the plain
rules, so a selector written again here wins. The colours come from the same palette as the plain theme, and
core/theme_textures.py draws the patterns.
"""
from __future__ import annotations

import theme_textures as tex


def _v(top: str, bot: str) -> str:
    return f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {top}, stop:1 {bot})"


def _h(left: str, right: str) -> str:
    return f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {left}, stop:1 {right})"


def _tiled(url: str) -> str:
    """The background of the window and its panels: the pattern over whatever colour they have."""
    return (f"QMainWindow, QWidget {{ background-image: {url}; background-repeat: repeat-xy; }}\n"
            "QFrame#settings_help { background-image: none; }\n")


def terminal(p: dict, r: int) -> str:
    """Phosphor on black, square, with the lines of a tube across it."""
    return _tiled(tex.scanlines(100, p["accent"])) + f"""
QPushButton, QLineEdit, QTextEdit, QComboBox, QCheckBox::indicator, QMenu::item, QToolTip {{ border-radius: 0; }}
QPushButton {{ background: transparent; border: 1px solid {p['accent']}; color: {p['accent']}; }}
QPushButton:hover {{ background: {p['accent']}; border-color: {p['accent']}; color: {p['bg']}; }}
QPushButton:pressed {{ background: {p['accent_l']}; color: {p['bg']}; }}
QPushButton:checked {{ background: {p['accent']}; border-color: {p['accent']}; color: {p['bg']}; }}
QPushButton:checked:hover {{ background: {p['accent_l']}; border-color: {p['accent_l']}; color: {p['bg']}; }}
QPushButton:flat {{ background: transparent; border: none; color: {p['fg_dim']}; }}
QPushButton:flat:hover {{ background: transparent; color: {p['accent']}; }}
QTabBar::tab {{ background: transparent; border: 1px solid {p['border']}; border-bottom: none; color: {p['fg_dim']}; }}
QTabBar::tab:selected {{ background: {p['accent']}; border: 1px solid {p['accent']}; color: {p['bg']}; }}
QTabBar::tab:hover {{ color: {p['accent_l']}; background: transparent; }}
QTabBar::tab:selected:hover {{ color: {p['bg']}; background: {p['accent_l']}; }}
QTabWidget::pane {{ border: 1px solid {p['border']}; }}
QFrame#dock_titlebar {{ background: {p['bg']}; border: none; border-bottom: 1px solid {p['accent']}; }}
QFrame#dock_titlebar QLabel {{ color: {p['accent']}; background: transparent; }}
QSlider::groove:horizontal {{ height: 2px; background: {p['border']}; border-radius: 0; }}
QSlider::handle:horizontal {{ background: {p['accent']}; width: 8px; height: 14px; margin: -6px 0; border-radius: 0; }}
QSlider::handle:horizontal:hover {{ background: {p['accent_l']}; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 0; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {p['accent_d']}; border-radius: 0; }}
QCheckBox::indicator {{ background: transparent; }}
QMenu {{ border: 1px solid {p['accent']}; }}
QMenu::item:selected {{ background: {p['accent']}; color: {p['bg']}; }}
QListWidget::item:selected, QTreeView::item:selected {{ background: {p['accent']}; color: {p['bg']}; }}
QLabel#settings_section {{ color: {p['accent']}; }}
"""


def carbon(p: dict, r: int) -> str:
    """A carbon weave, brushed metal buttons and red lamps, like the front of a hi-fi component."""
    metal_hi, metal_lo, edge = "#4b4f56", "#272a2e", "#0a0b0c"
    return _tiled(tex.weave(78)) + f"""
QPushButton {{ background: {_v(metal_hi, metal_lo)}; border: 1px solid {edge}; border-top: 1px solid #70757d;
    color: #e8ebef; }}
QPushButton:hover {{ background: {_v('#5b6068', '#30343a')}; border-color: {p['accent']}; border-top-color: {p['accent_l']}; }}
QPushButton:pressed {{ background: {_v('#1f2124', '#34383d')}; }}
QPushButton:checked {{ background: {_v(p['accent_l'], p['accent_d'])}; border: 1px solid {edge}; border-top: 1px solid {p['accent_l']};
    color: #ffffff; }}
QPushButton:checked:hover {{ background: {_v(p['accent_l'], p['accent'])}; color: #ffffff; }}
QPushButton:flat {{ background: transparent; border: none; color: {p['fg_dim']}; }}
QTabWidget::pane {{ border: 1px solid {edge}; border-top: 1px solid #55595f; }}
QTabBar::tab {{ background: {_v('#363a40', '#1d1f22')}; border: 1px solid {edge}; border-top: 1px solid #5a5e65; border-bottom: none; }}
QTabBar::tab:selected {{ background: {_v('#484c53', '#2b2e33')}; color: #ffffff; border-bottom: 3px solid {p['accent']}; }}
QTabBar::tab:hover {{ background: {_v('#40444b', '#25282c')}; }}
QFrame#dock_titlebar {{ background: {_v('#40444a', '#1e2023')}; border: 1px solid {edge}; border-top: 1px solid #62666d; }}
QFrame#dock_titlebar QLabel {{ color: #d8dce1; background: transparent; }}
QLineEdit, QTextEdit, QComboBox {{ background: #0b0c0e; border: 1px solid {edge}; border-bottom: 1px solid #4a4e55; }}
QSlider::groove:horizontal {{ background: #050506; border: 1px solid #3c4046; height: 5px; }}
QSlider::handle:horizontal {{ background: {_v('#e1e5ea', '#80858c')}; border: 1px solid #111214; }}
QSlider::handle:horizontal:hover {{ background: {_v('#ffffff', '#9aa0a8')}; }}
QSlider::sub-page:horizontal {{ background: {_h(p['accent_d'], p['accent'])}; border-radius: 2px; }}
QMenu {{ border: 1px solid {edge}; border-top: 1px solid #5a5e65; }}
QMenu::item:selected {{ background: {_v(p['accent_l'], p['accent_d'])}; color: #ffffff; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {_v('#5a5f67', '#33373c')}; }}
QLabel#settings_section {{ color: {p['accent_l']}; }}
"""


def chiptune(p: dict, r: int) -> str:
    """Chunky square bevels and a dithered background, in the colours of an old console."""
    bevel = p["border"]
    return _tiled(tex.dither("#ffffff", 26)) + f"""
QPushButton, QLineEdit, QTextEdit, QComboBox, QCheckBox::indicator, QMenu::item, QToolTip {{ border-radius: 0; }}
QPushButton {{ background: {p['bg3']}; border: 2px outset {bevel}; padding: 4px 11px; }}
QPushButton:hover {{ background: {p['accent_d']}; border-color: {p['accent']}; }}
QPushButton:pressed {{ border-style: inset; background: {p['bg2']}; }}
QPushButton:checked {{ background: {p['accent']}; border: 2px inset {p['accent_d']}; color: {p['bg']}; }}
QPushButton:checked:hover {{ background: {p['accent_l']}; color: {p['bg']}; }}
QPushButton:flat {{ background: transparent; border: none; color: {p['fg_dim']}; }}
QTabWidget::pane {{ border: 2px solid {bevel}; }}
QTabBar::tab {{ background: {p['bg2']}; border: 2px outset {bevel}; border-bottom: none; padding: 4px 12px; }}
QTabBar::tab:selected {{ background: {p['accent']}; color: {p['bg']}; border-color: {p['accent_l']}; }}
QTabBar::tab:hover {{ background: {p['accent_d']}; }}
QFrame#dock_titlebar {{ background: {p['border']}; border: 2px outset {bevel}; border-bottom: 3px solid {p['accent']}; }}
QFrame#dock_titlebar QLabel {{ color: {p['fg']}; background: transparent; }}
QLineEdit, QTextEdit, QComboBox {{ border: 2px inset {bevel}; }}
QSlider::groove:horizontal {{ height: 6px; background: {p['bg']}; border: 2px inset {bevel}; border-radius: 0; }}
QSlider::handle:horizontal {{ background: {p['accent']}; width: 10px; height: 16px; margin: -7px 0; border: 2px outset {p['accent_l']};
    border-radius: 0; }}
QSlider::sub-page:horizontal {{ background: {p['accent_d']}; border-radius: 0; }}
QCheckBox::indicator {{ width: 12px; height: 12px; border: 2px inset {bevel}; }}
QCheckBox::indicator:checked {{ background: {p['accent']}; border: 2px outset {p['accent_l']}; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ border-radius: 0; background: {p['accent_d']}; border: 2px outset {bevel}; }}
QMenu {{ border: 2px outset {bevel}; }}
QMenu::item:selected {{ background: {p['accent']}; color: {p['bg']}; }}
QToolTip {{ border: 2px solid {p['accent']}; }}
QLabel#settings_section {{ color: {p['accent']}; }}
"""


def parchment(p: dict, r: int) -> str:
    """Warm paper with a grain, ink-brown lines and a rust accent."""
    return _tiled(tex.grain(32)) + f"""
QPushButton {{ background: {_v(p['bg2'], p['bg3'])}; border: 1px solid {p['border']}; border-bottom: 2px solid {p['border']}; }}
QPushButton:hover {{ background: {_v(p['bg2'], p['accent_d'])}; border-color: {p['accent']}; }}
QPushButton:pressed {{ background: {p['accent_d']}; border-bottom: 1px solid {p['border']}; }}
QPushButton:checked {{ background: {_v(p['accent_l'], p['accent'])}; border-color: {p['accent']}; color: #fff6e4; }}
QPushButton:checked:hover {{ background: {p['accent_l']}; color: #fff6e4; }}
QTabBar::tab {{ background: {_v(p['bg3'], p['bg'])}; border: 1px solid {p['border']}; border-bottom: none; }}
QTabBar::tab:selected {{ background: {_v(p['bg2'], p['bg'])}; border-top: 3px solid {p['accent']}; color: {p['fg']}; }}
QFrame#dock_titlebar {{ background: {_v(p['bg2'], p['bg3'])}; border: 1px solid {p['border']}; border-bottom: 2px solid {p['border']}; }}
QFrame#dock_titlebar QLabel {{ color: {p['fg']}; background: transparent; }}
QSlider::handle:horizontal {{ background: {_v(p['accent_l'], p['accent'])}; border: 1px solid {p['border']}; }}
QMenu {{ border: 1px solid {p['border']}; }}
QMenu::item:selected {{ background: {p['accent']}; color: #fff6e4; }}
QLabel#settings_section {{ color: {p['accent']}; }}
"""


STYLES = {"terminal": terminal, "carbon": carbon, "chiptune": chiptune, "parchment": parchment}


def overlay(style: str, p: dict, r: int) -> str:
    fn = STYLES.get(style)
    return fn(p, r) if fn else ""
