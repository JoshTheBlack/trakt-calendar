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

from .. import db
from ..config import DATA_DIR
from ..metadata import rows as title_rows
from ..providers.base import MATCH_SOURCES, item_key, parse_item_key
from ..endpoints import ENDPOINTS

logger = logging.getLogger(__name__)

# The same slug-safe transform app/state.py used to build its filenames, so a
# state_*.json name can be mapped back to its endpoint key on import.
# safe filename fragment -> endpoint key, e.g. "shows_new" -> "shows/new".
# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

# THE ID NAMESPACES A CARD'S OWN IDENTITY HAS EVER BEEN DRAWN FROM. A source's
# record id is its slug where it has one, so these are the spellings a mark may
# have been stored under before mark keys existed.
#
# SLUG-SHAPED NAMESPACES ONLY, AND NUMERIC SERVICE IDS DELIBERATELY EXCLUDED. A
# card was never identified by a bare tmdb or tvdb number, and admitting them
# would let a mark match the wrong title outright: one real account has a mark
# literally spelled `1670` — the show's slug — and tmdb 1670 is a different
# programme entirely. A false match here HIDES something the viewer never
# marked, which is the one failure that leaves no trace on the page.
_LEGACY_ID_NAMESPACES = ("slug", "simkl_slug", "trakt_slug", "traktslug",
                         "tvdbslug", "mdlslug")


def marked(marks, item) -> bool:
    """Whether `item` is one of this viewer's not-watching marks.

    TWO SPELLINGS ARE ACCEPTED AND THAT IS NOT SLOPPINESS. A mark is stored under
    whatever the card sent when it was made, and cards used to send the WINNING
    SOURCE's id — which moves when the viewer changes whose description they
    read. One real account held 909 marks of which 800 named a title some
    currently-stored row can identify, `1670` and `1670-2023` among them: the
    same show, marked twice, under two services' spellings.

    So new marks are stored under `Item.mark_key`, which no preference moves, and
    the legacy id is still honoured. The remaining 85 name titles nothing
    currently stores, so they cannot be rewritten in advance — they are matched
    here, and they go out through `spellings_of` when somebody unmarks one.

    THIS PARAGRAPH USED TO CLAIM A LEGACY MARK WAS "re-saved under the stable key
    the next time somebody toggles one", AND NOTHING DID THAT. The write deleted
    the mark key alone, so every one of those 909 marks was permanently stuck on:
    the card read as marked, the unmark named a row that did not exist, and the
    redraw showed it still marked. It survived because the browser used to write
    the class itself and only a reload disagreed. `set_not_watching` is the
    mirror of this rule now, which is what makes the claim true.

    ONE IMPLEMENTATION, because this is a rule every surface has to agree on: the
    calendar grid, the day fragment, the stats card, the share page and the
    tracker's import all ask it, and a copy that asked only half the question
    would make a show hidden on one screen and visible on the next.

    THE TITLE STORE'S IDS COUNT AS WELL AS THE CARD'S (`known_ids`). A card
    carries the ids of the services its viewer admits, and a mark filed under
    another service's slug — Simkl's, from before mark keys — named a title the
    card no longer spelled once that service was switched off: four October
    premieres a real account had hidden came back on its TMDB-only calendar. The
    store holds every service's ids for the title whoever is admitted, so a mark
    holds whichever services the viewer is reading.
    """
    return marked_by_ids(marks, mark_key=item.mark_key, ids={**item.known_ids, **item.ids},
                         display_id=item.id, media=item.media)


def key_spellings(media, ids) -> set[str]:
    """Every item key a title known by `ids` could be filed under — one per shared
    id it carries, not only the one the waterfall picks today.

    A TITLE'S KEY CAN MOVE, AND A MARK MUST NOT MOVE WITH IT. The key is
    `resolve_key`'s first match in MATCH_SOURCES, and a Simkl calendar entry often
    arrives with no tmdb id: Recipe of the Year was keyed `show:tvdb:482228`.
    Enrichment later merges the ids Simkl's own record states into the stored
    title, and the same card then keys as `show:tmdb:…`. A mark pressed on the
    first key stopped matching the card under the second, and the redraw after a
    mark found no card to redraw — measured on a real October, on the Simkl
    titles whose enrichment landed while the page was open.
    """
    ids = ids or {}
    return {item_key(str(media), name, str(ids[name])) for name in MATCH_SOURCES
            if ids.get(name) not in (None, "", 0)}


def marked_by_ids(marks, *, mark_key: str, ids, display_id="", media="") -> bool:
    """The rule itself, said without an `Item`.

    IT TAKES THE PIECES BECAUSE NOT EVERY CALLER HOLDS A CARD. The tracker's
    import holds an identity RECORD — an id map and a season, built from the
    card and deliberately carrying no display id — and it used to answer this
    question with a narrower copy of its own: the card's slug and its Trakt id,
    two of the six spellings below. What that cost, measured on a real instance:
    a viewer's Grey's Anatomy mark reads `grey-s-anatomy`, which is Trakt's slug,
    while the card resolved to Simkl's description and carried `greys-anatomy` —
    so the calendar hid the show and the import added it. Nocturne was marked
    under `lazarus`, a name that appears only as Simkl's `tvdbslug`, and the copy
    never looked in that namespace at all.

    So the docstring above was true as an intention and false as a fact, which
    is the exact failure a rule stated in one place and implemented in two
    produces. There is one implementation now and every surface calls it.
    """
    if mark_key and mark_key in marks:
        return True
    if display_id and str(display_id) in marks:
        return True
    # ANY KEY THE TITLE'S IDS SPELL, for a mark made before its key moved — see
    # key_spellings. `media` defaults to the mark key's own, which is what a
    # caller holding only a key and an id map has.
    if any(key in marks for key in
           key_spellings(media or str(mark_key).split(":", 1)[0], ids)):
        return True
    # EVERY SPELLING THIS CARD HAS EVER BEEN IDENTIFIED BY, not just the one it
    # carries today. Simkl's own slugs are not unique, so records moved from
    # being keyed by slug to being keyed by simkl id — and 118 marks on one real
    # September stopped matching overnight, every one of them a Simkl title
    # marked under the slug the card used to send. The stored mark is a fact
    # about a title; which spelling it happens to be written in is not.
    ids = ids or {}
    return any(str(ids[name]) in marks for name in _LEGACY_ID_NAMESPACES
               if ids.get(name) not in (None, ""))


def marked_keys(marks, items) -> set[str]:
    """The subset of `items` that is marked, as MARK KEYS.

    THE EXPANSION POINT. Everything downstream — the grid, the chips, the stats,
    the client's own bookkeeping — then asks the plain question `item.mark_key in
    keys`, so the legacy-id tolerance above is paid for once per month rather
    than restated at every membership test.
    """
    return {item.mark_key for item in items if marked(marks, item)}


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

async def spellings_of(mark_key: str) -> set[str]:
    """Every id a mark for this title may have been STORED under, including the
    mark key itself.

    THE MIRROR OF `marked_by_ids`, AND IT EXISTS BECAUSE A TOLERANT READ NEEDS A
    TOLERANT WRITE. That function accepts a mark written in any of
    `_LEGACY_ID_NAMESPACES`; unmarking used to delete the mark key alone, so a
    show marked before mark keys existed read as marked, was asked to be
    unmarked, and stayed marked forever — the DELETE named a row that was never
    there. Measured on one real account: 909 of its 1,038 marks were in a legacy
    spelling, which is the majority of every mark it held.

    THE STORED ROWS FOR THE TITLE are what know which spellings it has ever
    answered to, so no card has to carry them (app/metadata/rows.py's id_maps).
    They are found by the title key OR by the id the mark key names, because a
    mark can predate the key its title has now. Every key those ids spell is
    cleared too, so a mark made under the title's earlier key comes off.

    A SPELLING TWO TITLES SHARE UNMARKS BOTH, and that is the honest answer
    rather than a flaw. 21 of that account's legacy marks are slugs naming more
    than one title (`lovesick` is two different 2026 shows), so the single stored
    mark was ALREADY hiding both — the read cannot tell them apart either. The
    ambiguity is in the old data, and clearing it is what lets the next mark be
    written under a key that has none.
    """
    try:
        key = parse_item_key(mark_key)
    except ValueError:
        key = None
    maps = await title_rows.id_maps(
        str(mark_key), key.media if key else None,
        (key.match_source, key.match_id) if key else None)
    found = {str(mark_key)}
    for media, ids in maps:
        found.update(str(ids[name]) for name in _LEGACY_ID_NAMESPACES
                     if ids.get(name) not in (None, ""))
        found.update(key_spellings(media, ids))
    return found


async def set_not_watching(user_id: int, item_id: str, not_watching: bool) -> None:
    """Mark or unmark one show for this user, everywhere. A delta, so two open
    tabs cannot lose each other's marks the way a whole-array save did.

    MARKING WRITES THE MARK KEY ALONE — the stable identity no viewer preference
    moves — while UNMARKING clears every spelling the read would have accepted
    (`spellings_of`). The asymmetry is the point: one new fact goes in under one
    name, and an old fact goes out under all of the names it might be filed
    under, so toggling a title off and on again leaves exactly one row.
    """
    if not_watching:
        await db.execute(
            "INSERT INTO not_watching_shows (user_id, item_id, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, item_id) DO NOTHING",
            (user_id, str(item_id), db.now()),
        )
        return
    spellings = await spellings_of(item_id)
    await db.execute(
        "DELETE FROM not_watching_shows WHERE user_id = ? AND item_id IN "
        f"({', '.join('?' * len(spellings))})",
        (user_id, *sorted(spellings)),
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
