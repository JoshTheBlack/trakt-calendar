"""The Simkl id-spelling quirk sync.py and search.py both correct for, stated
once instead of twice.

Simkl writes its own id as `simkl_id` on some payloads (search hits, sync
entries) and `simkl` on others, and app.providers.base.collect_ids only
recognises the second — it filters against ID_KEYS, which spells this app's
own name for Simkl's id as `simkl`.

CALENDAR.PY DOES NOT USE THIS, ON PURPOSE. Its own `_simkl_ids` keeps a
narrower, hand-picked set of namespaces (simkl, slug, tmdb, imdb, mal) than
`normalize` below passes through — this one also keeps `tvdb` and `trakt` when
a payload happens to carry them, and nothing has measured whether Simkl's
calendar CDN files ever do. Converging the two would be a real, unverified
change to what a calendar Record's `ids` can hold, not a refactor, so
calendar.py keeps its own copy until somebody has actually looked.

Package-internal (the underscore names the MODULE, not any name inside it —
see CLAUDE.md's convention for this).
"""
from __future__ import annotations

from ..base import collect_ids


def normalize(raw: dict) -> dict:
    """`raw`, Simkl's own ids block off any payload in this package,
    corrected onto ID_KEYS's spelling and filtered to it."""
    mapped = dict(raw)
    if mapped.get("simkl") in (None, "") and mapped.get("simkl_id") not in (None, ""):
        mapped["simkl"] = mapped["simkl_id"]
    # THE SLUG IS THIS SERVICE'S, AND IS NAMED SO. Trakt and Simkl both call a
    # title's readable name `slug` and do not agree on it — Simkl writes
    # `the-traitors` where Trakt writes `the-traitors-2023` — so an unnamespaced
    # one collides in any id map holding both, and whichever service wrote last
    # decided what a link built from it pointed at.
    if mapped.get("slug") not in (None, ""):
        mapped["simkl_slug"] = mapped["slug"]
    return collect_ids(mapped)
