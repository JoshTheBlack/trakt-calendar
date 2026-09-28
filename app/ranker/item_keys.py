"""What a board row is called: one title, or one season of one.

A BOARD KEY IS AN ItemKey PLUS A SEASON. The triple the app keys a title on
(`providers.base.ItemKey`) is shared with the tracker and the calendar and does
not change; what a rankings board adds is which season of that title a row is,
because a board may hold a show whole AND any of its seasons as separate rows
(`tier_items.season`, NULL for the whole show, and the unique index over
IFNULL(season, -1) that says so).

THE SPELLING, and why each half of it is what it is:
  - the whole show is `show:tmdb:1234` — EXACTLY the ItemKey's flat form, so
    every key already stored (a removal's row, a deleted tier's `items_json`, a
    backup, a page still open in another tab) reads unchanged and names the
    same row it always did;
  - a season is `show:tmdb:1234:s2`. Letters, digits and colons only, the
    alphabet the app's other flat keys already travel in: it is an element id
    (a row's move popover), a quoted CSS attribute value (`[data-key="…"]`,
    escaped in static/js/ranker/dnd.js), a form field name (the bulk Move's
    `pick:<key>`) and a query-string value, and nothing in it needs escaping in
    any of those. `s` rather than a bare number so a season can never be read
    as part of an id — mal and imdb ids are opaque to this app.

ONE PARSE AND ONE FORMAT, HERE, AND NO RANKER MODULE USES THE SHARED ONES.
`providers.base.parse_item_key` splits at most twice and so reads
`show:tmdb:1234:s2` as the match_id "1234:s2" — it does not refuse it — and a
lookup by that id quietly matches nothing. tests/ranker/test_item_keys.py holds that
no module in this package calls the shared parser or formatter directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..providers.base import ItemKey, Media, parse_item_key

# The highest season number a key may name. No show has run anywhere near this
# many seasons; the bound exists so a key cannot carry an integer the database
# or a template has to think about.
MAX_SEASON = 999

# A season suffix at the END of a key. Canonical only — no leading zeros, no
# sign — so that formatting a parsed key gives back the string that was parsed,
# which is what lets every lookup in this package compare keys as strings.
_SEASON_SUFFIX = re.compile(r"^(?P<title>.+):s(?P<season>0|[1-9][0-9]{0,2})$")


@dataclass(frozen=True)
class BoardKey:
    """One board row's identity: a title, and which of its seasons (None for the
    whole title, and always None for a film)."""
    title: ItemKey
    season: int | None = None

    @property
    def media(self) -> str:
        return self.title.media

    @property
    def match_source(self) -> str:
        return self.title.match_source

    @property
    def match_id(self) -> str:
        return self.title.match_id

    def __str__(self) -> str:
        if self.season is None:
            return str(self.title)
        return f"{self.title}:s{self.season}"

    def whole(self) -> BoardKey:
        """The same title as a whole-show row."""
        return BoardKey(self.title)


def board_key(media: str, match_source: str, match_id: str,
              season: int | None = None) -> str:
    """The flat string a board row is named by — see the module docstring for
    the spelling."""
    return str(BoardKey(ItemKey(media, match_source, match_id), season))


def of_row(row: Any) -> BoardKey:
    """A tier_items or tier_item_removals row's own key, read off its columns.

    `season` is always read, never defaulted: a SELECT that leaves it out would
    be asking a whole-show-only question, and there are none of those left — so
    a missing column is a bug to find (IndexError), not a whole show.
    """
    return BoardKey(ItemKey(row["media"], row["match_source"], row["match_id"]),
                    row["season"])


def row_key(row: Any) -> str:
    """`of_row`, as the flat string."""
    return str(of_row(row))


def parse_board_key(value: Any) -> BoardKey:
    """Parse a flat board key, raising ValueError on anything that is not one.

    A key without a season suffix is the whole title, exactly as before seasons
    existed. A film never has a season, so `movie:…:s1` is refused rather than
    read as a film whose id happens to end in ":s1".
    """
    if not isinstance(value, str):
        raise ValueError("Item keys must be strings.")
    season = None
    if match := _SEASON_SUFFIX.match(value):
        value, season = match["title"], int(match["season"])
    title = parse_item_key(value)
    if season is not None and title.media != Media.SHOW:
        raise ValueError("Only a show has seasons.")
    return BoardKey(title, season)


def valid_season(value: Any, media: str) -> int | None:
    """A season as a row may store it: None, or a whole number from 0 (the
    specials) to MAX_SEASON on a show. Raises ValueError otherwise.

    The one rule for what `tier_items.season` may hold, used by every path that
    writes one — an add, a restore, a backup being read back, and a row being
    set to a season — so none of them can store a value the key cannot spell.
    """
    if value is None or value == "":
        return None
    # An int, or the digits of one (a form field is a string). Not int() on
    # anything: that would take 2.5 as season 2 and True as season 1.
    if isinstance(value, str) and value.strip().isdigit():
        season = int(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        season = value
    else:
        raise ValueError("A season must be a whole number.")
    if not 0 <= season <= MAX_SEASON:
        raise ValueError(f"A season must be between 0 and {MAX_SEASON}.")
    if media != Media.SHOW:
        raise ValueError("Only a show has seasons.")
    return season
