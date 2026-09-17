"""Static-asset cache busting, and the one list of what a page's <head> loads.

Its own module rather than a constant in app/main.py because every page needs the
token, including the ones whose routes live in app/auth/routes.py and
app/auth/admin_routes.py — and those are imported BY main, so reaching back into it
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

# htmx's json-enc extension, vendored from the same project's community
# extension repository and served on every page beside htmx itself.
#
# ON EVERY PAGE BECAUSE THE THING IT ENCODES IS ON EVERY PAGE. Every mutating
# request this app makes has to be application/json — app/authz.py's
# request_shape_guard refuses anything else before routing — and the controls
# that make those requests are spread across the calendar, the tracker, the
# board and the shared header. A per-page list would be a second answer to
# "which pages write?", and the first one to go stale would fail as a form that
# silently posts the wrong content type.
#
# INERT UNTIL MARKUP ASKS FOR IT. The extension acts only on an element carrying
# `hx-json-enc`, so loading it changes no request until one does.
JSON_ENC = "static/js/htmx-json-enc.js"

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
# tests/kernel/test_page_head.py holds both of those to account.
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
        "static/js/calendar/source-swap.js",
        "static/js/calendar/search.js",
        # NO SETTINGS, SHARE OR FILTERS SCRIPT, ON THIS PAGE OR ANY OTHER. All
        # three panels are fragments the server draws and every control in them
        # posts itself; the Settings panel opens from the site menu on every page
        # an administrator reaches, and what any of them still asks of a browser —
        # copying a value and closing an overlay — is ui.js's, which every page
        # with a header loads. NO BADGE SCRIPT EITHER: a card's filter offer is a
        # <details> in a named exclusive group with three radios that post
        # themselves, so opening, closing, keeping one offer open at a time and
        # storing the answer are all the browser's or the server's.
        "static/js/calendar/easter-egg.js",
        "static/js/calendar/boot.js",
    ),
    "tracker": (
        "static/js/ui.js",
        "static/js/nav.js",
        # ONE FILE, AND IT IS ONE THING THE BROWSER MUST DO: measuring whether a
        # title overflows the cell drawn for it. Everything else this page used to
        # run — the month, the rows, the row actions, the add flow, the emoji
        # editor, the backfill, the copy blocks and the tab strip — is markup the
        # server draws, and the page needs no init of its own any more. Reading
        # the chosen backup file into a JSON body is ui.js's `data-file-into`,
        # because four pages now do exactly that and it is one act.
        "static/js/tracker/rows.js",
        # The calendar's file, on this page too, because the Konami code is ONE
        # listener for both — see static/js/calendar/easter-egg.js. Loading it
        # twice is not a re-declaration: boost.js keys what is running on the
        # file's PATH, and a page that already has it does not fetch it again.
        "static/js/calendar/easter-egg.js",
    ),
    "rankings": (
        # SortableJS first: ranker/dnd.js hands it containers, and `typeof
        # Sortable === 'undefined'` is its check for the file having failed to
        # load, not for it not having run yet.
        "static/js/sortable.min.js",
        "static/js/ui.js",
        "static/js/nav.js",
        # WHAT IS LEFT IS THE POINTER AND THE FILE. The drag and its coalesced
        # save, the keyboard and modifier-key ways of moving a title, where a
        # menu opens, the preview's zoom and pan, and a download that has to be a
        # POST. Every other action on this page — the boards, the tiers, the
        # removal, the bulk move, the import, the seed, the export options and
        # the backup restore — is markup that posts itself and is answered with
        # the board redrawn, so five of this feature's files have no contents
        # left at all.
        "static/js/ranker/core.js",
        "static/js/ranker/dnd.js",
        "static/js/ranker/save.js",
        "static/js/ranker/rows.js",
        "static/js/ranker/tiers.js",
        "static/js/ranker/search.js",
        "static/js/ranker/selection.js",
        "static/js/ranker/export.js",
        "static/js/ranker/export-preview.js",
        "static/js/ranker/boot.js",
    ),
    "admin": ("static/js/ui.js", "static/js/nav.js"),
    "account": ("static/js/ui.js", "static/js/nav.js", "static/js/plex-auth.js"),
    # ui.js for `data-copy` alone: a generated key is copied into an environment
    # file, and only a browser can write the clipboard. Everything else on the page
    # posts itself.
    "recovery": ("static/js/ui.js",),
    # ui.js for its URL-state select resync alone: this page's endpoint
    # picker acts on `change` and is restored by the bfcache exactly like
    # the calendar's, and leaving one copy of that bug unfixed is how the
    # other two came to differ in the first place.
    "pick": ("static/js/ui.js", "static/js/nav.js"),
    "sign-in": ("static/js/plex-auth.js",),
    "register": ("static/js/plex-auth.js",),
    # ui.js alone: a card opens the details panel through its delegated
    # listener, so the page carries no opener of its own.
    "share": ("static/js/ui.js",),
}

# Every stylesheet and script served to a browser: the pages' own scripts, plus
# the four files no page lists (the stylesheet, and the three the head macro puts
# on every page itself).
# url() refuses a path that is not in here, which is what makes a `?v=` token a
# promise rather than decoration: a script tag for an untracked file fails the
# first time the page renders, instead of quietly serving a stale copy for as long
# as a browser's cache holds it.
_CACHED_ASSETS = (STYLESHEET, HTMX, JSON_ENC, BOOST) + tuple(
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
