from __future__ import annotations
import io
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6.QtGui import QColor

# LRU-capped so a huge queued folder cannot keep every palette alive
_palette_cache: OrderedDict[str, dict[str, str] | None] = OrderedDict()
_PALETTE_CACHE_MAX = 512
_cache_lock = threading.Lock()


def _dominant(data: bytes) -> tuple[int, int, int] | None:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (96, 96))          # a JPEG decodes at reduced size
        img = img.convert("RGB").resize((48, 48), Image.LANCZOS)
        arr = np.array(img, dtype=np.float32).reshape(-1, 3)

        # Fast median-cut: repeatedly halve the widest colour channel
        buckets = [arr]
        while len(buckets) < 8:
            new_buckets = []
            for b in buckets:
                if len(b) == 0:
                    continue
                ch = int(np.argmax(b.max(axis=0) - b.min(axis=0)))
                b_sorted = b[b[:, ch].argsort()]
                mid = len(b_sorted) // 2
                new_buckets.append(b_sorted[:mid])
                new_buckets.append(b_sorted[mid:])
            buckets = [b for b in new_buckets if len(b) > 0]

        centres = np.array([b.mean(axis=0) for b in buckets if len(b) > 0])

        def saturation(c):
            r, g, b = c / 255.0
            mx, mn = max(r, g, b), min(r, g, b)
            return (mx - mn) / mx if mx > 1e-6 else 0.0

        best = max(centres, key=saturation)
        return int(best[0]), int(best[1]), int(best[2])
    except Exception:
        return None


def _derive_palette(r: int, g: int, b: int) -> dict[str, str]:
    base = QColor(r, g, b)
    h, s, v, _ = base.getHsvF()

    # Accent: vivid, mid-brightness
    s_acc = max(0.55, min(0.90, s))
    v_acc = max(0.55, min(0.80, v * 1.4))

    def _bg(v_target: float) -> str:
        c = QColor.fromHsvF(h, min(0.35, s * 0.5), v_target)
        return c.name()

    def _acc(s_scale=1.0, v_scale=1.0) -> str:
        c = QColor.fromHsvF(
            h,
            max(0.0, min(1.0, s_acc * s_scale)),
            max(0.0, min(1.0, v_acc * v_scale)),
        )
        return c.name()

    def _fg(v_target: float, s_tint: float = 0.08) -> str:
        # near-white with a faint hue tint
        c = QColor.fromHsvF(h, s_tint, v_target)
        return c.name()

    return {
        "bg":        _bg(0.10),
        "bg2":       _bg(0.14),
        "bg3":       _bg(0.19),
        "border":    _bg(0.26),
        "accent":    _acc(),
        "accent_l":  _acc(v_scale=1.25),
        "accent_d":  _acc(s_scale=0.75, v_scale=0.65),
        "fg":        _fg(0.92),
        "fg_dim":    _fg(0.65, 0.12),
        "fg_dimmer": _fg(0.42, 0.15),
        "sel":       _acc(s_scale=0.60, v_scale=0.45),
    }


def get_palette_for_path(
    path: Path,
    art_data: bytes | None,
    callback: Callable[[dict[str, str] | None], None],
):
    """Hand the palette for path to callback, from a worker thread, or at once on a cache hit. Cached by path."""
    key = str(path)
    with _cache_lock:
        if key in _palette_cache:
            _palette_cache.move_to_end(key)
            callback(_palette_cache[key])
            return

    def _run():
        palette = None
        if art_data:
            rgb = _dominant(art_data)
            if rgb:
                palette = _derive_palette(*rgb)
        with _cache_lock:
            _palette_cache[key] = palette
            while len(_palette_cache) > _PALETTE_CACHE_MAX:
                _palette_cache.popitem(last=False)
        callback(palette)

    threading.Thread(target=_run, daemon=True).start()
