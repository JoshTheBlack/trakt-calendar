"""What a season row of a board is called and what it counts.

PURE: it is handed what the services already said and answers with the label to
print and the facts to store. No database, no HTTP, no provider.

WHAT A SHOW ROW'S SEASON CONTROL OFFERS — every season a service lists, greyed
where the board already holds it, with one suggested — is the same rule every
season picker in the app keeps, and lives with them in
app/providers/season_choices.py.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# The one spelling of a season everywhere a picker draws one.
from ..providers.season_choices import SPECIALS, season_word


def row_label(title: str | None, season: int | None, season_name: str | None = None) -> str:
    """WHAT A BOARD ROW IS CALLED — the row itself, its details heading and both
    exports, so the four cannot come to call one row two things.

    The whole show is its title. A season is "Show — Season N" ("Show — Specials"
    for 0), and a season with a name of its own is "Show — <name>" (True Detective
    — Night Country): an anthology's seasons are known by their names, not their
    numbers. The details panel's rule for what counts as a name is the service's
    (`SeasonsAnswer`'s `name`: a title that only restates the number is dropped
    there), so a stored name is used as it is.
    """
    part = season_part(season, season_name)
    return f"{title or 'Untitled'} — {part}" if part else (title or "Untitled")


def season_part(season: int | None, season_name: str | None = None) -> str:
    """The half of `row_label` after the dash: the season's name, else "Season
    N", and "" for the whole show. On its own because the board row draws the two
    halves apart — a long show name may be cut short there, and the season is the
    half that must survive it."""
    if season is None:
        return ""
    return season_name or season_word(season)


def season_facts(listed: Iterable[Mapping[str, Any]], season: int | None) -> dict[str, Any]:
    """What a row that IS `season` of a show (None: the whole show) counts and is
    called, out of one service's season list — the three tier_items columns that
    describe what the row is rather than which title it is.

    A ROW'S COUNTS DESCRIBE THE ROW. A season row has no season count at all and
    its own episode total; the whole show has the list's seasons and their
    episodes summed. Whatever the list does not say comes back None, never the
    other shape's number: a season row that showed the show's "5S 38E" beside
    "Season 3" read as the season having 38 episodes. Specials are not in a
    service's season list, so the whole show's count is its numbered seasons.
    """
    by_season: dict[int, Mapping[str, Any]] = {}
    for entry in listed:
        try:
            by_season[int(entry["season"])] = entry
        except (KeyError, TypeError, ValueError):
            continue
    if season is None:
        numbered = [entry for number, entry in by_season.items() if number != SPECIALS]
        episodes = sum(int(entry.get("episode_count") or 0) for entry in numbered)
        return {"season_count": len(numbered) or None, "episode_count": episodes or None,
                "season_name": None}
    entry = by_season.get(season) or {}
    return {"season_count": None,
            "episode_count": int(entry.get("episode_count") or 0) or None,
            "season_name": str(entry.get("name") or "").strip() or None}
