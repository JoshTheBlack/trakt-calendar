"""Trakt's own ids block, corrected onto the app's id vocabulary.

WHY THIS EXISTS AT ALL, when Trakt's spelling is otherwise the app's. One field
is not shared: both services call a title's readable name `slug`, and they do not
agree on what it is — Trakt writes `the-traitors-2023` where Simkl writes
`the-traitors`. Left unnamespaced the two collide in any id map holding both, and
the loser is silent: whichever service wrote last decided where a link built from
that field pointed. A tracker row knows a title by both services at once, so this
is the ordinary case rather than an exotic one.

The mirror of `app/providers/simkl/_ids.py`, deliberately — one module per source
that says how that source spells things, so "whose slug is this" has an answer at
the boundary rather than a guess three layers in.

Package-internal: the underscore names the MODULE, not any name inside it.
"""
from __future__ import annotations

from ..base import collect_ids


def normalize(raw: dict | None) -> dict:
    """`raw`, Trakt's own ids block off any payload in this package, corrected
    onto ID_KEYS's spelling and filtered to it.

    `slug` IS CARRIED THROUGH AS WELL AS NAMESPACED. The calendar builds a
    record's own id from it (see calendar.py's `Record.id`), where there is no
    ambiguity to resolve — a Trakt record's slug is Trakt's — and dropping it
    would change what those records are addressed by.
    """
    mapped = dict(raw or {})
    if mapped.get("slug") not in (None, ""):
        mapped["trakt_slug"] = mapped["slug"]
    return collect_ids(mapped)
