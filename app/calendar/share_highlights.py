"""The shows a shared month's preview picture features, chosen by its owner.

THE CARD'S ALONE. A highlight changes which titles the share card
(app/calendar/share_routes.py's `assemble_card`) draws and nothing else: the
share page, its count and every other surface are untouched. It is a GUARANTEE,
never a narrowing — every featured title the view shows is drawn, and the
card's own ranking fills the tiles left (`share_routes.tile_candidates`) — so
the card draws as many tiles as it ever did.

ONLY THE PREMIERES CARDS (`CALENDARS`): a highlight is a choice among a month's
premieres, which is what Series and Season Premieres list. All Episodes' card and
the films card draw as they always did.

ONE ROW PER (owner, year, month, title), `share_highlights` (app/db.py), keyed by
the title store's key, so a re-key moves a highlight with its title
(app/metadata/holders.py repoints it). AT MOST `MAX_HIGHLIGHTS` A MONTH, decided
here where the write is made, inside its transaction — two tabs pressing at once
cannot make eleven.

WHICH TITLES MAY BE FEATURED IS THE CALLER'S TO SAY (`eligible`): this module
knows a month and a set of keys, never what a month premiered. A stored highlight
whose title is no longer among them goes at the next write rather than holding a
place nobody can see to clear.
"""
from __future__ import annotations

import json
from collections.abc import Collection
from typing import NamedTuple

from .. import db
from . import share_card

# One per tile: a card draws at most this many, so an eleventh would be a choice
# the card could not honour.
MAX_HIGHLIGHTS = share_card.MAX_TILES

# The calendars whose card draws them (endpoints.ENDPOINTS' keys).
CALENDARS = frozenset({"shows/new", "shows/premieres"})


class Written(NamedTuple):
    """What a write left: the month's featured titles in the order chosen, and
    whether the write was refused because the month already had its fill."""
    featured: tuple[str, ...]
    full: bool


def _drop_ineligible(conn: db.Connection, user_id: int, year: int, month: int,
                     eligible: Collection[str]) -> int:
    """Delete the month's highlights whose title is not in `eligible`."""
    return conn.execute(
        "DELETE FROM share_highlights WHERE user_id = ? AND year = ? AND month = ? "
        "AND title_key NOT IN (SELECT value FROM json_each(?))",
        (user_id, year, month, json.dumps(sorted({str(key) for key in eligible})))).rowcount


def _featured(conn: db.Connection, user_id: int, year: int, month: int) -> tuple[str, ...]:
    return tuple(str(row["title_key"]) for row in conn.execute(
        "SELECT title_key FROM share_highlights WHERE user_id = ? AND year = ? AND month = ? "
        "ORDER BY position, created_at", (user_id, year, month)))


async def for_month(user_id: int, year: int, month: int) -> tuple[str, ...]:
    """The month's featured titles, in the order they were chosen."""
    return await db.run(lambda conn: _featured(conn, user_id, year, month))


async def for_card(user_id: int, endpoint_key: str, year: int, month: int) -> frozenset[str]:
    """What a card of `endpoint_key` for that month must feature: the month's
    highlights on a premieres card, nothing on any other."""
    if endpoint_key not in CALENDARS:
        return frozenset()
    return frozenset(await for_month(user_id, year, month))


async def feature(user_id: int, year: int, month: int, title_key: str, on: bool, *,
                  eligible: Collection[str]) -> Written:
    """Feature `title_key` on the month's card, or stop featuring it, and drop
    any highlight whose title is not in `eligible`. Refused — nothing written but
    the dropping — when featuring would make more than `MAX_HIGHLIGHTS`."""
    def _work(conn: db.Connection) -> Written:
        _drop_ineligible(conn, user_id, year, month, eligible)
        held = _featured(conn, user_id, year, month)
        if not on:
            conn.execute("DELETE FROM share_highlights WHERE user_id = ? AND year = ? "
                         "AND month = ? AND title_key = ?", (user_id, year, month, title_key))
        elif title_key not in held:
            if len(held) >= MAX_HIGHLIGHTS:
                return Written(held, True)
            conn.execute(
                "INSERT INTO share_highlights (user_id, year, month, title_key, position, "
                "created_at) VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM "
                "share_highlights WHERE user_id = ? AND year = ? AND month = ?), ?)",
                (user_id, year, month, title_key, user_id, year, month, db.now()))
        return Written(_featured(conn, user_id, year, month), False)

    return await db.transaction(_work)


async def keep_only(user_id: int, year: int, month: int, eligible: Collection[str]) -> int:
    """Drop the month's highlights whose title is not in `eligible` — a show
    taken off the month — and say how many went."""
    return await db.transaction(
        lambda conn: _drop_ineligible(conn, user_id, year, month, eligible))
