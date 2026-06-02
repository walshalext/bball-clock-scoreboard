"""Persistent operator configuration — stored in config.json beside server.py.

Only operator-level defaults live here: game structure and display preferences.
Per-game state (scores, fouls, rosters, running clocks) is never written here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state import _default_periods

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

_DEFAULTS: dict[str, Any] = {
    "periods":      [p.to_dict() for p in _default_periods()],
    "timeout_mode": "remaining",
    "timeout_max":  3,
}


def load() -> dict[str, Any]:
    """Load config.json, merging with built-in defaults for any missing keys."""
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **data}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
