"""The shared page context: header nav flags, version, build label, and the
static-asset cache-busting token.

Its own module rather than a helper in app/main.py because every page carrying
the header needs these flags, including the ones whose routes live in
app/auth/routes.py, app/auth/admin_routes.py and app/ranker/routes.py — and those
are imported BY main, so reaching back into it would be a circular import.
Same reasoning as app/assets.py.

The header offers Calendar, Rankings and Admin on every page, so every page has
to know which of them this account may actually reach. Before this existed each
route decided that for itself and most of them didn't decide at all, which is
why the links were only present on the two pages that happened to pass the
flags. version and build were the same problem one level up: eight call sites
each restating the same lines. The static-asset token used to live here too; it
now reaches templates through app/assets.py directly, because the head macro is
the only thing that ever wanted it and a route context was one hop too many.

Distrakt is deliberately absent: its menu item is rendered for everyone and
revealed client-side, so that the HTML gives nothing away. See _nav.html.
"""
from __future__ import annotations

import os

from . import changelog

# THE PRODUCT'S NAME, IN ONE PLACE. It reached the browser from fifteen separate
# literals across templates, the ASGI app, the Plex handshake and the console
# banner — three of them spelling it three different ways ("Trakt New Shows",
# "New Shows", and the alt text on the logo), which is how a rename leaves a
# page behind. Templates get it as the `product` global (app/templating.py);
# Python callers import it from here.
#
# IT IS NOT THE NAME OF ANY IDENTIFIER, and that distinction is load-bearing.
# `distrakt`, TRAKT_DATA_DIR, the provider packages and the settings fields name
# the FEATURE or the BACKEND, and they are still correct. So is the tracker's
# "New Shows" category, which means "a series airing its very first season" and
# has never meant this app.
PRODUCT_NAME = "Distrakkl"

# Injected at Docker build time (GitHub Actions); "dev" for local runs.
_BUILD = os.environ.get("APP_BUILD", "dev").strip() or "dev"
_COMMIT = os.environ.get("APP_COMMIT", "").strip()
BUILD_LABEL = "dev" if _BUILD == "dev" else f"build {_BUILD}" + (f" · {_COMMIT[:7]}" if _COMMIT else "")


def page_context(user) -> dict:
    """The flags and chrome facts every rendered page needs.

    Merge into a page's context dict: `context.update(chrome.page_context(user))`.
    Accepts None so a route that renders the header for a caller without a
    session still gets a complete, all-false set rather than an undefined name.
    """
    return {
        "is_admin": bool(user and user.is_admin),
        "calendar_available": bool(user and user.calendar_approved),
        "ranker_available": bool(user and user.ranker_approved),
        "version": changelog.current_version(),
        "build": BUILD_LABEL,
    }
