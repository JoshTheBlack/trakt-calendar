"""Static-asset cache busting, and the one list of what every page's <head> loads.

Its own module rather than a constant in app/main.py because every page needs the
token, including the ones whose routes live in app/auth/routes.py and
app/auth/admin_routes.py — and those are imported BY main, so reaching back into it
would be a circular import.

The token is the newest mtime across the files browsers cache, recomputed once
per server start. That means a deploy invalidates them and a running server does
not, which is what a long-lived cache header wants.

Templates reach this through the base template rather than writing asset paths of
their own — see templates/_base.html and app/templating.py.
"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# The app's whole stylesheet. ONE file so every page can ship an identical
# <head>, which is what a boosted navigation — it swaps <body> only — needs in
# order to land styled. style.css's own header explains the bargain.
STYLESHEET = "static/css/style.css"

# Boosting IS htmx, so a page without it is a one-way door out of a boosted
# session. BOOST is what makes the rest of a boosted arrival work — a swap
# replaces the body's children only, so the page arrived at has neither its own
# <body> classes nor a DOMContentLoaded to start from until that file gives them
# to it.
HTMX = "static/js/htmx.min.js"
BOOST = "static/js/boost.js"

# htmx's json-enc extension, vendored from the same project's community
# extension repository.
#
# INERT UNTIL MARKUP ASKS FOR IT. The extension acts only on an element carrying
# `hx-json-enc`, so loading it changes no request until one does — and every
# mutating request this app makes has to be application/json, because
# app/authz.py's request_shape_guard refuses anything else before routing.
JSON_ENC = "static/js/htmx-json-enc.js"

# WHAT A PAGE LOADS: ONE OF TWO LISTS, and which one is decided by who can reach
# the page. The lists live here rather than in a template for the reason the
# stylesheet does: a template that spelled out filenames would be a second copy,
# and adding a file would half-work until somebody edited both.
#
# NOT ONE LIST PER PAGE, because a boosted navigation swaps the body and never the
# <head>. With a list per page, whatever the session's first page loaded was all a
# later page had, so the page arrived at had to fetch its own scripts at runtime —
# and then which files were running, and in what order they had run, depended on
# the route somebody had taken through the app. Every page a signed-in session
# moves between loads APP_SCRIPTS, so a boosted arrival lands with every script it
# needs already running and nothing is fetched.
#
# BUT NOT ONE LIST FOR EVERY PAGE EITHER. The pages a stranger lands on — signing
# in, registering, a shared calendar — would then carry the rankings board's drag
# library and the calendar's easter egg for nothing. MEASURED on the sign-in page
# over HTTP/2 in Chromium on DevTools' Fast 3G profile, cold: the whole list cost
# 356ms of first paint and 350ms before the form could be used, against the four
# files that page loaded when every page had its own list; the public list costs
# about 90ms of each. So those pages load PUBLIC_SCRIPTS, and
# templates/_base.html makes that safe: a page on this list does not boost, so a
# session that started on one never swaps its way into a page expecting the other
# list — it arrives there by a full load, which brings that page's own <head>.
#
# THEY ARE ORDINARY SCRIPTS, NOT MODULES. No template calls into them any more —
# the Content-Security-Policy runs no script written in markup (app/csp.py), so
# every control is a data- attribute a file listens for — but the files still
# call EACH OTHER by name (a row's nudge schedules the board's save, the export
# raises ui.js's toast), and that is what a module would have to turn into
# imports and exports. So every file on a page shares ONE global scope, which
# means:
#   - a name may be declared at top level in exactly ONE file. A second
#     `let`/`const` of it is a SyntaxError that stops that file dead; a second
#     `function` is legal and silently hands the name to whichever file runs
#     later. tests/kernel/test_page_head.py holds that to account.
#   - a file runs on every page of its list, so a listener has to ask whether
#     what it acts on is on screen, and must not assume its own page is the one
#     loaded.
#
# THE ORDER THAT MATTERS is the vendored half, and it is short. htmx first,
# because the extension registers itself against the global htmx publishes as it
# executes, and boost.js sets htmx's config at its top level. No first-party file
# calls into another as it loads — each declares, or binds a listener, and every
# call between files happens on an event, after all of them have run — so their
# order among themselves is not load-bearing. `defer` preserves this order all
# the same, which keeps it one fact rather than a thing to reason about.
PUBLIC_SCRIPTS = (
    HTMX,
    JSON_ENC,
    BOOST,
    # The details panel and the select resync on a shared calendar, and the
    # sign-in button waiting for htmx.
    "static/js/ui.js",
    # Signing in and registering with Plex.
    "static/js/plex-auth.js",
    # Signing in with a passkey, and adding one from the account page (which loads
    # this list too, as APP_SCRIPTS begins with it).
    "static/js/passkey.js",
    # A shared calendar's poster-only cards open their panel away from the edge
    # the same way the owner's calendar does.
    "static/js/calendar/layout.js",
)

APP_SCRIPTS = PUBLIC_SCRIPTS + (
    # SortableJS 1.15.6 (MIT), vendored rather than fetched from a CDN so the
    # board has no third-party origin to trust or to be offline without.
    # Integrity of the file as committed:
    # sha384-HZZ/fukV+9G8gwTNjN7zQDG0Sp7MsZy5DDN6VfY3Be7V9dvQpEpR2jF2HlyFUUjU
    # ranker/dnd.js's `typeof Sortable === 'undefined'` is its check for this
    # file having failed to load.
    "static/js/sortable.min.js",
    # THE CALENDAR. NO view.js: marking a show is a button posting itself,
    # answered with every card for that title on this month redrawn and the
    # counts it moves (templates/_marked_cards.html). NO source-swap.js: flipping
    # a card between the services that described it is a radio group and one
    # stylesheet rule (templates/_card.html, style.css's `.flip`). NO SEARCH
    # SCRIPT: the panel's every request is an `hx-get` on the control that makes
    # it, and WHERE A JUMP LANDS is the stylesheet's — the server marks the anchor
    # card's day `id="jump-target"`, the browser's own fragment scrolling reaches
    # it and `scroll-margin-block` keeps it clear of the sticky header. NO
    # SETTINGS, SHARE, FILTERS OR BADGE SCRIPT, here or anywhere: all of them are
    # fragments the server draws whose every control posts itself.
    # The Konami code answers on every page of this list — see its own header.
    "static/js/calendar/easter-egg.js",
    "static/js/calendar/boot.js",
    # THE TRACKER HAS NOTHING OF ITS OWN. The month, the rows, the row actions,
    # the add flow, the emoji editor, the backfill, the copy blocks and the tab
    # strip are all markup the server draws, and scrolling a title too wide for
    # its cell is a stylesheet rule (style.css's `.distrakt-title`, which reads
    # its own width in container query units rather than being told it).
    #
    # THE RANKINGS BOARD: WHAT IS LEFT IS THE POINTER AND THE FILE. The drag and
    # its coalesced save, the modifier-key ways of picking titles, the preview's
    # zoom and pan, and a download that has to be a POST. Every other action on
    # the board is markup that posts itself and is answered with the board
    # redrawn.
    "static/js/ranker/core.js",
    "static/js/ranker/dnd.js",
    "static/js/ranker/save.js",
    "static/js/ranker/rows.js",
    "static/js/ranker/search.js",
    "static/js/ranker/selection.js",
    "static/js/ranker/export.js",
    "static/js/ranker/export-preview.js",
    "static/js/ranker/boot.js",
)

# Every stylesheet and script served to a browser. APP_SCRIPTS includes the
# public list, so it is every script there is.
# url() refuses a path that is not in here, which is what makes a `?v=` token a
# promise rather than decoration: a script tag for an untracked file fails the
# first time the page renders, instead of quietly serving a stale copy for as long
# as a browser's cache holds it.
_CACHED_ASSETS = (STYLESHEET, *APP_SCRIPTS)

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
        raise KeyError(f"{name} is not a tracked asset; add it to assets.APP_SCRIPTS")
    return f"/{name}?v={ASSET_VERSION}"


def font_url(name: str) -> str:
    """The URL for a preloaded font — no token, for the reason FONT_PRELOADS gives."""
    return f"/{name}"
