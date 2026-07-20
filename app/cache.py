"""Tiny TTL disk cache for Trakt detail responses (phase 2).

Detail lookups (cast, episodes) are expensive — one Trakt call per show — so we
cache the raw JSON on disk keyed by a hash of the request, with a configurable
TTL. This keeps the details modal + tile enrichment fast and well within Trakt's
rate limits on repeat views.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"


def _key_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def get(key: str, ttl_seconds: int):
    """Return the cached value for `key` if fresh, else None. ttl<=0 disables caching."""
    if ttl_seconds <= 0:
        return None
    path = _key_path(key)
    if not path.exists():
        return None
    try:
        if (time.time() - path.stat().st_mtime) > ttl_seconds:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def set(key: str, value) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _key_path(key).write_text(json.dumps(value), encoding="utf-8")
    except OSError:
        pass  # cache writes are best-effort
