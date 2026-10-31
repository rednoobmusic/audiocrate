"""Small tiles with a pattern in them (scan lines, a weave, paper grain, a dither), written as PNG files in the
cache folder and given to a stylesheet as url() for background-image. Each is mostly transparent, so the colour of
the theme shows through it. The file name holds what the tile was made from, so a changed look makes a new file.
"""
from __future__ import annotations

import random

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter


def _tile(name: str, w: int, h: int, draw) -> str:
    """url("...") of the tile name, drawn by draw(painter) the first time; "none" when the file cannot be made."""
    try:
        import plat_paths
        folder = plat_paths.cache_dir() / "theme"
        f = folder / f"{name}.png"
        if not f.exists():
            folder.mkdir(parents=True, exist_ok=True)
            img = QImage(w, h, QImage.Format_ARGB32)
            img.fill(Qt.transparent)
            q = QPainter(img)
            draw(q)
            q.end()
            if not img.save(str(f)):
                return "none"
        return f'url("{f.as_posix()}")'
    except Exception:
        return "none"


def _rgba(color: str, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def scanlines(alpha: int = 90, glow: str = "") -> str:
    """A dark line in every third row, and with glow a faint line of that colour beside it, like the lines of a tube."""
    def draw(q):
        if glow:
            q.fillRect(0, 1, 4, 1, _rgba(glow, 26))
        q.fillRect(0, 2, 4, 1, QColor(0, 0, 0, alpha))
    return _tile(f"scan-{alpha}-{QColor(glow).name()[1:] if glow else 'dark'}", 4, 3, draw)


def weave(alpha: int = 46) -> str:
    """A woven pattern of small light and dark squares, as in carbon fibre."""
    def draw(q):
        for by in range(2):
            for bx in range(2):
                dark = (bx + by) % 2 == 0
                for i in range(4):
                    # each square fades along one direction, and the light ones the other way round
                    t = i / 3
                    a = int(alpha * (0.35 + 0.65 * t)) if dark else int(alpha * 0.45 * (1 - t))
                    c = QColor(0, 0, 0, a) if dark else QColor(255, 255, 255, a)
                    if dark:
                        q.fillRect(bx * 4, by * 4 + i, 4, 1, c)
                    else:
                        q.fillRect(bx * 4 + i, by * 4, 1, 4, c)
    return _tile(f"weave-{alpha}", 8, 8, draw)


def grain(alpha: int = 16, seed: int = 7) -> str:
    """Speckle like the tooth of paper."""
    def draw(q):
        rnd = random.Random(seed)
        for y in range(96):
            for x in range(96):
                v = rnd.random()
                if v < 0.30:
                    q.fillRect(x, y, 1, 1, QColor(0, 0, 0, int(alpha * rnd.random())))
                elif v < 0.46:
                    q.fillRect(x, y, 1, 1, QColor(255, 255, 255, int(alpha * 1.4 * rnd.random())))
    return _tile(f"grain-{alpha}-{seed}", 96, 96, draw)


def dither(color: str = "#ffffff", alpha: int = 14) -> str:
    """A checkerboard of two pixel squares."""
    def draw(q):
        c = _rgba(color, alpha)
        q.fillRect(0, 0, 2, 2, c)
        q.fillRect(2, 2, 2, 2, c)
    return _tile(f"dither-{QColor(color).name()[1:]}-{alpha}", 4, 4, draw)
