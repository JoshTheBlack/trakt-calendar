"""Per-(endpoint, year, month) watch-state persistence (requirement E).

Mirrors the JSON schema the PHP version used so behaviour is unchanged:
{ notWatching: [], history: [], lastCount: N|null, lastShowIds: []|null }
but keyed per endpoint as well, so switching endpoints keeps independent state.
"""
from __future__ import annotations

import json
import re

from .config import DATA_DIR, _ensure_data_dir

_SAFE = re.compile(r"[^a-z0-9]+")


def _state_path(endpoint: str, year: int, month: int):
    safe = _SAFE.sub("_", endpoint.lower()).strip("_")
    return DATA_DIR / f"state_{safe}_{year}_{month}.json"


def load_state(endpoint: str, year: int, month: int) -> dict:
    _ensure_data_dir()
    path = _state_path(endpoint, year, month)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"notWatching": [], "history": [], "lastCount": None, "lastShowIds": None}


def save_state(endpoint: str, year: int, month: int, payload: dict) -> None:
    _ensure_data_dir()
    to_save = {
        "notWatching": list(payload.get("notWatching") or []),
        "history": list(payload.get("history") or []),
        "lastCount": int(payload["lastCount"]) if payload.get("lastCount") is not None else None,
        "lastShowIds": list(payload.get("lastShowIds") or []),
    }
    path = _state_path(endpoint, year, month)
    path.write_text(json.dumps(to_save, indent=2), encoding="utf-8")
