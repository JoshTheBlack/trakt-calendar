"""Static-asset cache busting, and the one list of what a page's <head> loads.

Its own module rather than a constant in app/main.py because every page needs the
token, including the ones whose routes live in app/auth_routes.py and
app/admin_routes.py — and those are imported BY main, so reaching back into it
would be a circular import.

The token is the newest mtime across the files browsers cache, recomputed once
per server start. That means a deploy invalidates them and a running server does
not, which is what a long-lived cache header wants.

Templates reach this through the head macro rather than writing asset paths of
their own — see templates/_head.html and app/templating.py.
"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Every stylesheet and script served to a browser. A file missing from this list
# can be edited without the browser ever noticing, so add new ones here.
# url() refuses a path that is not in it, which is what keeps that true: a script
# tag for an untracked file fails the first time the page renders, rather than
# quietly serving a stale copy for as long as a browser's cache holds it.
_CACHED_ASSETS = (
    "static/css/style.css",
    "static/js/app.js",
    "static/js/distrakt.js",
    "static/js/nav.js",
    "static/js/plex-auth.js",
    "static/js/share.js",
    "static/js/ranker.js",
    # Vendored third-party, so their mtime only moves when the pinned version does.
    "static/js/htmx.min.js",
    "static/js/sortable.min.js",
)

# The app's whole stylesheet. ONE file so every page can ship an identical
# <head>, which is what a boosted navigation — it swaps <body> only — needs in
# order to land styled. style.css's own header explains the bargain.
STYLESHEET = "static/css/style.css"

# Preloaded by every page: these are the faces the shared header and body text
# are set in, so every page waits on them and none should discover them late.
# Deliberately NOT in _CACHED_ASSETS and served without a token — a font file is
# immutable and a new weight is a new filename, so there is nothing to bust.
FONT_PRELOADS = (
    "static/fonts/bebas-neue-v16-latin-400.woff2",
    "static/fonts/inter-v20-latin-400.woff2",
    "static/fonts/inter-v20-latin-700.woff2",
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


def url(name: str) -> str:
    """The browser-facing URL for a cache-busted asset, e.g. "static/js/app.js".

    Raises KeyError for anything not in _CACHED_ASSETS, so a page cannot serve a
    file the token does not cover. That combination — a `?v=` on a file whose
    mtime never reaches the token — is the failure worth refusing: it looks
    busted and never busts.
    """
    if name not in _CACHED_ASSETS:
        raise KeyError(f"{name} is not in assets._CACHED_ASSETS; add it there first")
    return f"/{name}?v={ASSET_VERSION}"


def font_url(name: str) -> str:
    """The URL for a preloaded font — no token, for the reason FONT_PRELOADS gives."""
    return f"/{name}"
