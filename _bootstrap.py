"""Make the grouped source folders importable with flat module names.

The source is organised into folders for navigation (core/, panels/,
integrations/, ui/), but modules still import each other by plain name
(`from player import ...`). Importing this module first puts those folders on
sys.path so the flat imports keep resolving — no per-module package prefixes.

main.py imports this before anything else; tests/tools can do the same.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC_DIRS = ("core", "panels", "integrations", "ui")

for _d in _SRC_DIRS:
    _p = _ROOT / _d
    if _p.is_dir():
        sp = str(_p)
        if sp not in sys.path:
            sys.path.insert(0, sp)
