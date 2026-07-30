"""Per-user calendar state: the "not watching" marks and the change-detection
fields, replacing app/state.py's shared per-(endpoint,year,month) JSON files.

The two halves are keyed differently ON PURPOSE.

"Not watching" is a fact about a SHOW and lives in not_watching_shows, keyed by
(user, item_id) alone. Marking a series premiere means you are not watching that
show — so its episodes stop appearing on All Episodes, its next season premiere
arrives already marked, and none of it comes back next month. Keying the mark by
the view it happened to be made in made the toggle mean "hide this cell", which
is not what it says.

Change detection ("N new since you last looked") is genuinely per view, because
it is about one month of one endpoint's list, so calendar_view_state keeps its
(user, endpoint, year, month) key.

Both are rows rather than documents, which is what turns a single toggle into a
delta — an INSERT or DELETE of one item_id — instead of the whole-array
read-modify-write that loses updates when a user has two tabs open.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from . import db
from .config import DATA_DIR
from .endpoints import ENDPOINTS

logger = logging.getLogger(__name__)

# The same slug-safe transform app/state.py used to build its filenames, so a
# state_*.json name can be mapped back to its endpoint key on import.
# safe filename fragment -> endpoint key, e.g. "shows_new" -> "shows/new".
# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

async def not_watching_list(user_id: int) -> list[str]:
    """Every show this user has marked not-watching, oldest mark first.

    Not scoped to an endpoint or a month: a mark applies wherever that show turns
    up. The item ids are the calendar card's data-id, which the normalizer builds
    from the SHOW's ids on every show endpoint, so one list filters all of them.
    """
    rows = await db.fetch_all(
        "SELECT item_id FROM not_watching_shows WHERE user_id = ? "
        "ORDER BY created_at, item_id",
        (user_id,),
    )
    return [r["item_id"] for r in rows]


async def not_watching_ids(user_id: int) -> set[str]:
    """not_watching_list as a set, for the callers that only ever ask "is this
    one of them?"."""
    return set(await not_watching_list(user_id))


async def load_view_state(user_id: int, endpoint: str, year: int, month: int) -> dict:
    """The change-detection fields for one (endpoint, year, month): the previous
    visit's item count and show-id list, plus the history log.

    `last_show_ids` is None — not [] — when this view has never been recorded,
    because "no baseline yet" and "a baseline that happened to be empty" mean
    opposite things to the is-new diff.
    """
    row = await db.fetch_one(
        "SELECT last_count, last_show_ids_json, history_json FROM calendar_view_state "
        "WHERE user_id = ? AND endpoint = ? AND year = ? AND month = ?",
        (user_id, endpoint, int(year), int(month)),
    )
    return {
        "history": json.loads(row["history_json"]) if row and row["history_json"] else [],
        "last_count": row["last_count"] if row else None,
        "last_show_ids": json.loads(row["last_show_ids_json"]) if row and row["last_show_ids_json"] else None,
    }


async def load_state(user_id: int, endpoint: str, year: int, month: int) -> dict:
    """What the calendar page needs on load, in the shape app/state.load_state
    returned so the front end is unchanged.

    `notWatching` is the user's whole global set; the change-detection fields are
    read for this one (endpoint, year, month).
    """
    not_watching = await not_watching_list(user_id)
    view = await load_view_state(user_id, endpoint, year, month)
    return {
        "notWatching": not_watching,
        "history": view["history"],
        "lastCount": view["last_count"],
        "lastShowIds": view["last_show_ids"],
    }


# ---------------------------------------------------------------------------
# writes — deltas (a single toggle) and whole-document (the drop-in for POST)
# ---------------------------------------------------------------------------

async def set_not_watching(user_id: int, item_id: str, not_watching: bool) -> None:
    """Mark or unmark one show for this user, everywhere. A delta, so two open
    tabs cannot lose each other's marks the way a whole-array save did."""
    if not_watching:
        await db.execute(
            "INSERT INTO not_watching_shows (user_id, item_id, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, item_id) DO NOTHING",
            (user_id, str(item_id), db.now()),
        )
    else:
        await db.execute(
            "DELETE FROM not_watching_shows WHERE user_id = ? AND item_id = ?",
            (user_id, str(item_id)),
        )


async def set_view_state(user_id: int, endpoint: str, year: int, month: int, *,
                         last_count: int | None, last_show_ids: list | None,
                         history: list | None = None) -> None:
    """Write the change-detection fields for one (endpoint, year, month). When
    history is None the stored history is left as it was, so the "N new since you
    last looked" write does not have to re-send the whole history each time."""
    def _work(conn: db.Connection) -> None:
        if history is None:
            row = conn.execute(
                "SELECT history_json FROM calendar_view_state "
                "WHERE user_id = ? AND endpoint = ? AND year = ? AND month = ?",
                (user_id, endpoint, int(year), int(month)),
            ).fetchone()
            history_json = row["history_json"] if row else None
        else:
            history_json = json.dumps(list(history))
        conn.execute(
            "INSERT INTO calendar_view_state "
            "(user_id, endpoint, year, month, last_count, last_show_ids_json, history_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, endpoint, year, month) DO UPDATE SET "
            "last_count = excluded.last_count, last_show_ids_json = excluded.last_show_ids_json, "
            "history_json = excluded.history_json, updated_at = excluded.updated_at",
            (
                user_id, endpoint, int(year), int(month),
                None if last_count is None else int(last_count),
                None if last_show_ids is None else json.dumps(list(last_show_ids)),
                history_json, db.now(),
            ),
        )

    await db.transaction(_work)


# The history log shows the last few loads of a view, so it stays short enough
# to read at a glance in the corner of the stats bar.
HISTORY_LIMIT = 3


def _relative_day_label(day: str, today: date) -> str:
    """"Today" / "Yesterday" / "Jul 5" for a history entry's YYYY-MM-DD stamp."""
    try:
        when = date.fromisoformat(day)
    except (TypeError, ValueError):
        return "Today"
    if when == today:
        return "Today"
    if when == today - timedelta(days=1):
        return "Yesterday"
    return f"{when:%b} {when.day}"


async def resolve_view(user_id: int, endpoint: str, year: int, month: int, *,
                       show_ids: list[str], total: int, now: datetime) -> dict:
    """Diff this load of a view against the last one, then COMMIT it as the new
    baseline. Returns what the page needs to render: the set of show ids that
    weren't here last time, the "since last run" delta line, and the history log.

    A read-then-commit, deliberately in one place: whoever produces the cards
    also decides what counts as new, so the diff can never be run against a
    baseline a second request already overwrote. `show_ids` must be the SERVER's
    full list for the view — committing anything narrower (a partially rendered
    page, one day of a lazily loaded month) would make every id it omits look new
    on the next visit.

    A view with no stored baseline marks NOTHING new: a month being looked at for
    the first time is not a month where every show just appeared.

    `now` is the viewer's local time, so the history stamps read as the times
    they were actually looking at it.
    """
    prior = await load_view_state(user_id, endpoint, year, month)
    last_show_ids = prior["last_show_ids"]
    new_ids = set(show_ids) - set(last_show_ids) if isinstance(last_show_ids, list) else set()

    history = [dict(entry) for entry in prior["history"] if isinstance(entry, dict)]
    # One entry per CHANGE, not per load: reloading a view that hasn't moved
    # would otherwise push the three useful lines out of the log immediately.
    if not history or history[-1].get("count") != total:
        history.append({
            "time": f"{now.hour}:{now.minute:02d}",
            "count": total,
            "date": now.date().isoformat(),
        })
        history = history[-HISTORY_LIMIT:]

    await set_view_state(user_id, endpoint, year, month,
                         last_count=total, last_show_ids=list(show_ids), history=history)

    last_count = prior["last_count"]
    if last_count is None:
        delta = {"text": "(Initial Tracking)", "kind": "none"}
    elif total > last_count:
        delta = {"text": f"📈 (+{total - last_count} since last run)", "kind": "up"}
    elif total < last_count:
        delta = {"text": f"📉 (-{last_count - total} since last run)", "kind": "down"}
    else:
        delta = {"text": "✅ Perfect Match", "kind": "same"}

    today = now.date()
    return {
        "new_ids": new_ids,
        "delta": delta,
        # Newest first, which is the order the log is read in.
        "history": [
            {"label": _relative_day_label(entry.get("date"), today),
             "time": entry.get("time", ""),
             "count": entry.get("count", 0)}
            for entry in reversed(history)
        ],
    }


async def save_state(user_id: int, endpoint: str, year: int, month: int, payload: dict) -> None:
    """Write a whole state document for one (endpoint, year, month).

    The not-watching marks in it are ADDED to the user's global set rather than
    replacing it, because the payload only ever describes one view: a document
    listing July's Series Premieres says nothing about a show marked in August,
    and treating its absence as an unmark would delete marks the sender never
    saw. Unmarking is set_not_watching's job, which names the one show it means.
    """
    not_watching = [str(x) for x in (payload.get("notWatching") or [])]
    history = list(payload.get("history") or [])
    last_count = payload.get("lastCount")
    last_show_ids = payload.get("lastShowIds")

    def _work(conn: db.Connection) -> None:
        now = db.now()
        for item_id in not_watching:
            conn.execute(
                "INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id, item_id) DO NOTHING",
                (user_id, item_id, now),
            )
        conn.execute(
            "INSERT INTO calendar_view_state "
            "(user_id, endpoint, year, month, last_count, last_show_ids_json, history_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, endpoint, year, month) DO UPDATE SET "
            "last_count = excluded.last_count, last_show_ids_json = excluded.last_show_ids_json, "
            "history_json = excluded.history_json, updated_at = excluded.updated_at",
            (
                user_id, endpoint, int(year), int(month),
                None if last_count is None else int(last_count),
                None if last_show_ids is None else json.dumps(list(last_show_ids)),
                json.dumps(history), now,
            ),
        )

    await db.transaction(_work)

