"""Icon glyphs on Qt before 6.7.

The icons are glyphs of the bundled Nerd Font. Qt 6.7 and newer find them through font fallback. Older ones do not, and
draw an empty box where the icon belongs (Ubuntu 24.04 ships Qt 6.6). On those, a button or label whose text is only
icon glyphs is given the Nerd Font itself.
"""
from __future__ import annotations

from PySide6.QtCore import qVersion
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel

family = ""         # the Nerd Font's name, set by use() only where Qt needs it


def use(name: str) -> None:
    global family
    major, minor = (int(n) for n in qVersion().split(".")[:2])
    family = name if name and (major, minor) < (6, 7) else ""


def _only_icons(text: str) -> bool:
    t = text.strip()
    return bool(t) and all(0xE000 <= ord(c) <= 0xF8FF or ord(c) >= 0xF0000 for c in t)


def fix(w) -> None:
    if family and isinstance(w, (QAbstractButton, QLabel)) and w.font().family() != family and _only_icons(w.text()):
        f = QFont(w.font())
        f.setFamily(family)
        w.setFont(f)


def sweep() -> None:
    if family:
        for w in QApplication.allWidgets():
            fix(w)
