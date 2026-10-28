"""Put core/, panels/, integrations/ and ui/ on sys.path so modules can import each other by flat name
(`from player import ...`). main.py imports this first; the check scripts do the same."""
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
