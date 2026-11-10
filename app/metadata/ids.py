"""How the title store spells an id map: which ids it keeps, and as what type.

ONE SPELLING FOR EVERY DOOR. The same title reaches the store from a calendar
listing, a search result, a tracker's history and a backup, each carrying the ids
its service happened to state in the types it happened to use. Filed as they came,
one title would be two — Simkl's ids block carries tmdb as "315142" where Trakt's
carries 315142 — so every id map is put through `canonical` before anything keys
from it or stores it.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..providers.base import collect_ids

# The id spaces whose ids are numbers. It matters twice over in the store: the
# indexes on `titles` compare json_extract's answer, and SQLite never finds the
# text '366850' equal to the integer 366850.
NUMERIC = frozenset({"trakt", "simkl", "tmdb", "tvdb", "mal"})


def typed(name: str, value: Any) -> Any:
    """One id as the type its space is stored in: a number for a numeric space
    whenever it reads as one, as given otherwise."""
    if name in NUMERIC and isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def canonical(ids: Mapping[str, Any] | None) -> dict:
    """`ids` as every path files them: ID_KEYS only, numeric spaces as numbers,
    and WITHOUT the bare `slug`.

    THE BARE SLUG IS AMBIGUOUS BY DEFINITION — both services call their readable
    name `slug` and disagree about it (`the-rookie` against `the-rookie-2018`) —
    so it depended on which service the path happened to read. Each service's
    name travels namespaced (`trakt_slug`, `simkl_slug`), which is what every
    link is built from.
    """
    return {name: typed(name, value) for name, value in collect_ids(ids or {}).items()
            if name != "slug"}


def stored(ids: Mapping[str, Any] | None) -> str:
    """`canonical(ids)` as the text a title group's `ids_json` holds, its keys
    sorted — the one spelling a listing and a description both write.

    THE TEXT IS WHAT IS COMPARED. A writer asks before its lock whether its
    write would change the stored row (groups.changing), column by column as
    stored, so the same ids in another key order are a change: a description
    sorted its keys and a listing kept its service's order, and every fill after
    a description rewrote the group to say the same thing."""
    return json.dumps(canonical(ids), sort_keys=True)
