"""The shared header's template context.

Its own module rather than a helper in app/main.py because every page carrying
the header needs these flags, including the ones whose routes live in
app/auth_routes.py, app/admin_routes.py and app/ranker_routes.py — and those are
imported BY main, so reaching back into it would be a circular import. Same
reasoning as app/assets.py.

The header offers Calendar, Rankings and Admin on every page, so every page has
to know which of them this account may actually reach. Before this existed each
route decided that for itself and most of them didn't decide at all, which is why
the links were only present on the two pages that happened to pass the flags.

Distrakt is deliberately absent: its menu item is rendered for everyone and
revealed client-side, so that the HTML gives nothing away. See _nav.html.
"""
from __future__ import annotations


def nav_context(user) -> dict:
    """The flags _nav.html's navbar() macro gates its menu items on.

    Merge into a page's context dict: `context.update(nav.nav_context(user))`.
    Accepts None so a route that renders the header for a caller without a
    session still gets a complete, all-false set rather than an undefined name.
    """
    return {
        "is_admin": bool(user and user.is_admin),
        "calendar_available": bool(user and user.calendar_approved),
        "ranker_available": bool(user and user.ranker_approved),
    }
