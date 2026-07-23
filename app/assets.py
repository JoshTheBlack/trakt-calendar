"""Static-asset cache busting.

Its own module rather than a constant in app/main.py because every page needs the
token, including the ones whose routes live in app/auth_routes.py and
app/admin_routes.py — and those are imported BY main, so reaching back into it
would be a circular import.

The token is the newest mtime across the files browsers cache, recomputed once
per server start. That means a deploy invalidates them and a running server does
not, which is what a long-lived cache header wants.
"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Every stylesheet and script served to a browser. A file missing from this list
# can be edited without the browser ever noticing, so add new ones here.
_CACHED_ASSETS = (
    "static/css/style.css",
    "static/css/auth.css",
    "static/css/distrakt.css",
    "static/css/share.css",
    "static/js/app.js",
    "static/js/distrakt.js",
    "static/js/nav.js",
    "static/js/plex-auth.js",
    "static/js/share.js",
)

# Falls back to a constant when the files can't be stat'd — a wrong-but-stable
# token is better than one that changes per request and defeats caching entirely.
_FALLBACK = "1"


def _compute() -> str:
    try:
        return str(int(max((BASE_DIR / name).stat().st_mtime for name in _CACHED_ASSETS)))
    except OSError:
        return _FALLBACK


ASSET_VERSION = _compute()
