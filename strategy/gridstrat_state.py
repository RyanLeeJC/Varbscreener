"""
Atomic JSON persistence for ``strategy.gridstrat`` (legacy mark-ladder + paired limit engine).

``Varibot/grid_limits_reconcile.py`` only needs ``_default_state_path`` from ``gridstrat`` for paths;
load/save helpers live here so paired + legacy can share one file with explicit schema versioning.
"""

from __future__ import annotations

# =============================================================================
# USER / DEFAULT SETTINGS
# =============================================================================

# JSON pretty-print indent (2 is readable; set 0 for smaller files).
STATE_JSON_INDENT: int = 2


import json
import os
from typing import Any, Dict


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=int(STATE_JSON_INDENT), default=str)
    os.replace(tmp, path)
