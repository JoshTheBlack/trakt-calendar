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

# The app's whole stylesheet. ONE file so every page can ship an identical
# <head>, which is what a boosted navigation — it swaps <body> only — needs in
# order to land styled. style.css's own header explains the bargain.
STYLESHEET = "static/css/style.css"

# On every page too, from the head macro rather than from any page's list below:
# boosting IS htmx, so a page without it is a one-way door out of a boosted
# session. BOOST is what makes the rest of a boosted arrival work — a swap
# replaces the body's children only, so the page arrived at has neither its own
# <body> classes nor its own scripts until that file gives them to it.
HTMX = "static/js/htmx.min.js"
BOOST = "static/js/boost.js"

# What each page loads, and IN WHAT ORDER, keyed by the page's own name. The
# lists live here rather than in the templates for the reason the stylesheet does:
# a template that spelled out its own script filenames would be a second copy of
# this list, and adding a file would half-work until somebody edited both.
#
# THEY ARE ORDINARY SCRIPTS, NOT MODULES, and that is what makes the order matter.
# The templates call these functions from onclick attributes, which reach names in
# the global scope only — so a module would have to publish every one of them onto
# window by hand. Instead a page's files share one global scope and run in the
# order below (defer preserves it), which means:
#   - a page's boot.js goes LAST. It is the one file that runs an init rather than
#     only declaring things, and by then everything it calls has to exist.
#   - a file's top-level `let`/`const` is visible to the whole page, so a name may
#     be declared in exactly ONE of a page's files. Two of them declaring the same
#     one is a SyntaxError that stops the second file dead.
# tests/test_page_head.py holds both of those to account.
#
# THE SAME LISTS ARE SHIPPED TO THE BROWSER, because a boosted navigation swaps
# the body and never the <head>: the page arrived at has to fetch its own scripts,
# and static/js/boost.js reads this map to know which. That is also why a name may
# not be shared across two PAGES' lists — visit both in one session and the second
# page's copy would be a re-declaration. Only ONE list, though: the head macro
# serializes this dict, so the browser's copy cannot drift from it.
PAGE_SCRIPTS = {
    "calendar": (
        "static/js/ui.js",
        "static/js/nav.js",
        "static/js/calendar/view.js",
        "static/js/calendar/layout.js",
        "static/js/calendar/arr-buttons.js",
        "static/js/calendar/season-tiles.js",
        "static/js/calendar/details-modal.js",
        "static/js/calendar/cert-picker.js",
        "static/js/calendar/filters.js",
        "static/js/calendar/settings.js",
        "static/js/calendar/settings-encryption.js",
        "static/js/calendar/settings-trakt.js",
        "static/js/calendar/share-links.js",
        "static/js/calendar/easter-egg.js",
        "static/js/calendar/boot.js",
    ),
    "tracker": (
        "static/js/ui.js",
        "static/js/nav.js",
        "static/js/tracker/month.js",
        "static/js/tracker/rows.js",
        "static/js/tracker/actions.js",
        "static/js/tracker/post.js",
        "static/js/tracker/details.js",
        "static/js/tracker/add.js",
        "static/js/tracker/emoji.js",
        "static/js/tracker/backup.js",
        "static/js/tracker/backfill.js",
        "static/js/tracker/boot.js",
    ),
    "rankings": (
        # SortableJS first: ranker/dnd.js hands it containers, and `typeof
        # Sortable === 'undefined'` is its check for the file having failed to
        # load, not for it not having run yet.
        "static/js/sortable.min.js",
        "static/js/ui.js",
        "static/js/nav.js",
        "static/js/ranker/core.js",
        "static/js/ranker/ask.js",
        "static/js/ranker/dnd.js",
        "static/js/ranker/save.js",
        "static/js/ranker/rows.js",
        "static/js/ranker/tiers.js",
        "static/js/ranker/boards.js",
        "static/js/ranker/search.js",
        "static/js/ranker/sources.js",
        "static/js/ranker/selection.js",
        "static/js/ranker/export.js",
        "static/js/ranker/export-preview.js",
        "static/js/ranker/export-image.js",
        "static/js/ranker/backup.js",
        "static/js/ranker/boot.js",
    ),
    "admin": ("static/js/ui.js", "static/js/nav.js"),
    "account": ("static/js/ui.js", "static/js/nav.js", "static/js/plex-auth.js"),
    "pick": ("static/js/nav.js",),
    "sign-in": ("static/js/plex-auth.js",),
    "register": ("static/js/plex-auth.js",),
    "share": ("static/js/ui.js", "static/js/share.js"),
}

# Every stylesheet and script served to a browser: the pages' own scripts, plus
# the three files no page lists (the stylesheet, and the two the head macro puts
# on every page itself).
# url() refuses a path that is not in here, which is what makes a `?v=` token a
# promise rather than decoration: a script tag for an untracked file fails the
# first time the page renders, instead of quietly serving a stale copy for as long
# as a browser's cache holds it.
_CACHED_ASSETS = (STYLESHEET, HTMX, BOOST) + tuple(
    dict.fromkeys(name for scripts in PAGE_SCRIPTS.values() for name in scripts))

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
    """The browser-facing URL for a cache-busted asset, e.g. "static/js/ui.js".

    Raises KeyError for anything not in _CACHED_ASSETS, so a page cannot serve a
    file the token does not cover. That combination — a `?v=` on a file whose
    mtime never reaches the token — is the failure worth refusing: it looks
    busted and never busts.
    """
    if name not in _CACHED_ASSETS:
        raise KeyError(
            f"{name} is not a tracked asset; add it to the page that loads it in "
            "assets.PAGE_SCRIPTS")
    return f"/{name}?v={ASSET_VERSION}"


def page_script_urls() -> dict[str, list[str]]:
    """PAGE_SCRIPTS as browser URLs, for static/js/boost.js to load from.

    Serialized into every page's <head>, because a boosted navigation swaps the
    body and leaves the head alone: the page arrived at has to fetch its own
    scripts, and it can only do that if it knows what they are. Built from the
    one dict above rather than restated, so the browser's copy cannot drift.
    """
    return {page: [url(name) for name in names] for page, names in PAGE_SCRIPTS.items()}


def scripts(page: str) -> tuple[str, ...]:
    """What `page` loads, in load order — see PAGE_SCRIPTS.

    Raises KeyError for a page that has no entry, rather than rendering a page
    with no scripts at all: every one of these pages is inert without them, and a
    silently empty tuple would look like a JavaScript bug rather than a typo here.
    """
    if page not in PAGE_SCRIPTS:
        raise KeyError(f"no scripts declared for page {page!r} in assets.PAGE_SCRIPTS")
    return PAGE_SCRIPTS[page]


def font_url(name: str) -> str:
    """The URL for a preloaded font — no token, for the reason FONT_PRELOADS gives."""
    return f"/{name}"
