"""Ready-made looks for the theme picker in Settings.

A preset is only a way to fill in the settings that already make a theme (system_theme, theme_style, accent_color,
bg_color and text_color), so the colours can be tuned afterwards and nothing else has to know about presets.
"""
from __future__ import annotations

from typing import NamedTuple


class Preset(NamedTuple):
    key: str
    name: str
    accent: str = ""
    bg: str = ""
    text: str = ""
    kind: str = "own"       # "system" (the OS look), "aero" (the glass look) or "own" (AudioCrate's theme in these colours)


# Own themes in the first group keep what they always were: the purple default tints the whole window from its accent,
# so it names no background or text.
PRESETS = (
    Preset("system", "System", kind="system"),
    Preset("audiocrate", "AudioCrate", "#8040c8"),
    Preset("aero", "Frutiger Aero", kind="aero"),
    # dark
    Preset("midnight", "Midnight", "#4f8cff", "#0b1220", "#d6e2ff"),
    Preset("black", "Black", "#ff4d6d", "#000000", "#e8e8e8"),
    Preset("nord", "Nord", "#88c0d0", "#2e3440", "#d8dee9"),
    Preset("dracula", "Dracula", "#bd93f9", "#282a36", "#f8f8f2"),
    Preset("gruvbox", "Gruvbox", "#fe8019", "#282828", "#ebdbb2"),
    Preset("solarized-dark", "Solarized Dark", "#268bd2", "#002b36", "#93a1a1"),
    Preset("tokyo", "Tokyo Night", "#7aa2f7", "#1a1b26", "#c0caf5"),
    Preset("rose-pine", "Rosé Pine", "#eb6f92", "#191724", "#e0def4"),
    Preset("catppuccin", "Catppuccin", "#cba6f7", "#1e1e2e", "#cdd6f4"),
    Preset("forest", "Forest", "#4caf7d", "#0f1a14", "#d5e8d4"),
    Preset("crimson", "Crimson", "#e5484d", "#170c0f", "#f0d9dc"),
    # light
    Preset("light", "Light", "#0a64d8", "#f5f5f7", "#1d1d1f"),
    Preset("paper", "Paper", "#b4531a", "#f6f1e7", "#3b3228"),
    Preset("solarized-light", "Solarized Light", "#268bd2", "#fdf6e3", "#586e75"),
    Preset("latte", "Latte", "#8839ef", "#eff1f5", "#4c4f69"),
)
_BY_KEY = {p.key: p for p in PRESETS}


def preset(key: str) -> Preset | None:
    return _BY_KEY.get(key)


def settings_for(key: str) -> dict:
    """The settings a preset sets."""
    p = _BY_KEY[key]
    if p.kind == "system":
        return {"system_theme": True, "theme_style": "default"}
    if p.kind == "aero":
        return {"system_theme": False, "theme_style": "aero", "accent_color": "#8040c8", "bg_color": "", "text_color": ""}
    return {"system_theme": False, "theme_style": "default", "accent_color": p.accent, "bg_color": p.bg,
            "text_color": p.text}


def current(cfg: dict) -> str:
    """The key of the preset the settings add up to, or "custom" when they match none."""
    if cfg.get("system_theme", True):
        return "system"
    if cfg.get("theme_style") == "aero":
        return "aero"
    have = tuple((cfg.get(k) or "").lower() for k in ("accent_color", "bg_color", "text_color"))
    for p in PRESETS:
        if p.kind == "own" and have == (p.accent.lower(), p.bg.lower(), p.text.lower()):
            return p.key
    return "custom"
