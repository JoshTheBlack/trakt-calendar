"""The tracker's row store: month documents in, month documents out.

ONE JOB — read and write `distrakt_months` and `distrakt_shows`. No Trakt calls,
no bucketing, no rollover decisions. The orchestration that reaches out to a
provider lives beside this module, not in it, so a store operation can be tested
against a database and nothing else.

Two storage shapes:
  - distrakt_months holds the month-level state (whether the month is frozen,
    when its totals were last refreshed, the movies snapshotted at freeze time),
    keyed (user_id, month).
  - distrakt_shows holds one row per tracked (user_id, month, title, season).

WHAT NAMES A ROW. A season is identified by (media, match_source, match_id) — the
shared id namespace the identity waterfall landed in, plus the id in it (see
app/providers/base.py). NOT by the id of whichever service happened to report it:
keying on that would make the same season arriving from a second service a second
row for ever, with nothing to say the two were the same. The service's own ids
travel on the row as ordinary attributes, because they are still what you need to
CALL that service — `record["ids"]["trakt"]` is how you ask Trakt about a season,
and `record["match_id"]` is how you find it here.

In memory a month is the same `doc` dict the renderers and the pure rollover
logic have always consumed —
    {month, closed, totals_refreshed_at, movies?, shows: [record, ...]}
— so load_month assembles that shape from the two tables and save_month writes it
back.
"""
from __future__ import annotations

import json
import re

from .. import db
from ..providers.base import ItemKey, Media, collect_ids, item_key, resolve_key

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# The identity columns, in key order. Named once because three tables and every
# lookup in this package are built from them.
IDENTITY_COLUMNS = ("media", "match_source", "match_id")

# The columns that hold ids, and which ID_KEYS namespace each one holds. Both
# directions read from this one mapping: the column names carry the historical
# `_id` suffix that Trakt's did, while `ids` uses the namespace names an Item
# uses, and a second table of the same fact would drift.
ID_COLUMNS = {
    "trakt_id": "trakt", "simkl_id": "simkl", "tmdb": "tmdb",
    "tvdb": "tvdb", "imdb": "imdb", "mal": "mal", "slug": "slug",
}

# The distrakt_shows columns beyond (user_id, month), in insert order. The bool
# columns are stored as 0/1; everything else passes through.
SHOW_COLUMNS = (
    *IDENTITY_COLUMNS, "season", *ID_COLUMNS, "title", "network",
    "abandoned", "abandoned_form", "watched", "total", "cadence", "premiere",
    "finale", "bucket", "started_airing", "finished_airing", "added_by",
)

# Keys allowed on a stored show record, with the type-coercion used on write.
_INT_FIELDS = ("season", "watched", "total")
_BOOL_FIELDS = ("abandoned",)

# Who put a row on the roster. Only CALENDAR rows are re-imported on every load
# of a preview month, and only they mean "the calendar says you're watching
# this" — which is why removing one marks the show not-watching there and
# removing any other kind does not. "" is a row written before the column
# existed; see routes.api_distrakt_remove.
ADDED_BY_CALENDAR = "calendar"
ADDED_BY_HISTORY = "history"
ADDED_BY_MANUAL = "manual"
ADDED_BY_VALUES = (ADDED_BY_CALENDAR, ADDED_BY_HISTORY, ADDED_BY_MANUAL)

# Columns an in-place update is allowed to touch (identity keys excluded).
# `added_by` is excluded with them: it records who FIRST put the row on the
# roster and must not be rewritten by the live-counts writer or by re-adding a
# show that is already there, or a calendar row could quietly become a manual one.
_UPDATABLE_COLUMNS = frozenset(SHOW_COLUMNS) - {*IDENTITY_COLUMNS, "season", "added_by"}
_BOOL_COLUMNS = frozenset(("abandoned", "started_airing", "finished_airing"))

_INSERT_SHOW_SQL = (
    "INSERT INTO distrakt_shows (user_id, month, " + ", ".join(SHOW_COLUMNS) + ") "
    "VALUES (" + ", ".join(["?"] * (2 + len(SHOW_COLUMNS))) + ")"
)

# The WHERE clause that addresses one season of one user's month. Written once
# because every single-row read, update and delete below shares it, and a
# mismatch between two of them would be a cross-row write.
_ROW_WHERE = (
    "WHERE user_id = ? AND month = ? AND "
    + " AND ".join(f"{c} = ?" for c in IDENTITY_COLUMNS)
    + " AND season = ?"
)


class UnkeyableRecord(ValueError):
    """A record that names no shared id, so nothing can tell it apart from
    another title of the same name. Raised rather than stored under a made-up
    identity — see app/providers/base.py's MATCH_SOURCES."""


def month_key(year: int, month: int) -> str:
    """The key a month is stored and addressed under: zero-padded "YYYY-MM".

    Here rather than in whichever caller needed one first, because the format is
    a STORAGE fact — it is the key half of distrakt_months' identity, and
    _MONTH_RE above is the same fact stated as a validator. It was spelled out
    inline in the routes and again in the rollover logic, which is two places to
    get the padding wrong.

    THE ZERO-PADDING IS LOAD-BEARING, not cosmetic: several callers compare month
    keys with `<` and `>=` to decide which months are past. String comparison
    only agrees with chronological order while every key is the same width, so
    "2026-7" would sort after "2026-12" and silently mis-file a month.
    """
    return f"{int(year):04d}-{int(month):02d}"


def _validate_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    return month


def record_key(rec: dict) -> ItemKey:
    """The identity of a stored (or about-to-be-stored) record.

    Takes the triple when the record already carries one and otherwise runs the
    waterfall over its `ids`, so a caller building a record from a provider's
    payload does not have to resolve the key itself — and cannot resolve it a
    second, different way.
    """
    if rec.get("match_source") and rec.get("match_id"):
        return ItemKey(str(rec.get("media") or Media.SHOW),
                       str(rec["match_source"]), str(rec["match_id"]))
    key = resolve_key(rec.get("media") or Media.SHOW, rec.get("ids") or {})
    if key is None:
        raise UnkeyableRecord(
            f"{rec.get('title') or 'This title'} carries none of the shared ids "
            "the tracker files rows under."
        )
    return key


def row_params(rec: dict) -> tuple:
    """(media, match_source, match_id, season) for a record — the tuple every
    single-row statement in this module ends with."""
    key = record_key(rec)
    return (key.media, key.match_source, key.match_id, int(rec["season"]))


def new_month_doc(month: str) -> dict:
    """A fresh, empty month document (not persisted until save_month)."""
    return {
        "month": _validate_month(month),
        "closed": False,
        "totals_refreshed_at": None,
        "shows": [],
    }


def normalize_show(show: dict) -> dict:
    """Build a full show record from `show`, filling schema defaults.

    Resolves the identity here rather than at each call site, so a record that
    cannot be keyed is refused at the moment it is built and not two layers
    further on, where the caller no longer knows which title it came from.
    """
    incoming = dict(show or {})
    key = record_key(incoming)
    return {
        "media": key.media,
        "match_source": key.match_source,
        "match_id": key.match_id,
        "key": str(key),
        "season": int(incoming["season"]),
        "ids": collect_ids(incoming.get("ids") or {}),
        "title": str(incoming.get("title") or ""),
        "network": str(incoming.get("network") or ""),
        "abandoned": bool(incoming.get("abandoned", False)),
        "abandoned_form": incoming.get("abandoned_form"),
        "watched": int(incoming.get("watched") or 0),
        "total": int(incoming.get("total") or 0),
        "cadence": incoming.get("cadence"),
        "premiere": incoming.get("premiere"),
        "finale": incoming.get("finale"),
        "bucket": incoming.get("bucket"),
        "added_by": str(incoming.get("added_by") or ""),
    }


def _coerce_update(fields: dict) -> dict:
    """Coerce the subset of updatable COLUMNS present in `fields` for an in-place
    update, dropping identity keys and anything not a real column.

    `ids` is expanded into its columns: a row learning an id it did not have when
    it was written is the one way an identity can be improved later, and the
    caller states it the same way it states one on an insert.
    """
    out = {}
    for column, id_key in ID_COLUMNS.items():
        if id_key in (fields or {}).get("ids", {}):
            out[column] = fields["ids"][id_key]
    for k, v in (fields or {}).items():
        if k not in _UPDATABLE_COLUMNS:
            continue  # identity key or not a stored column
        if k in _INT_FIELDS:
            out[k] = int(v) if v is not None else 0
        elif k in _BOOL_FIELDS:
            out[k] = bool(v)
        else:
            out[k] = v
    return out


def _show_params(user_id: int, month: str, rec: dict) -> tuple:
    """Positional values for _INSERT_SHOW_SQL from a record dict."""
    ids = rec.get("ids") or {}
    return (
        user_id, month,
        *row_params(rec),
        *(ids.get(id_key) for id_key in ID_COLUMNS.values()),
        str(rec.get("title") or ""),
        str(rec.get("network") or ""),
        1 if rec.get("abandoned") else 0,
        rec.get("abandoned_form"),
        int(rec.get("watched") or 0),
        int(rec.get("total") or 0),
        rec.get("cadence"),
        rec.get("premiere"),
        rec.get("finale"),
        rec.get("bucket"),
        1 if rec.get("started_airing") else 0,
        1 if rec.get("finished_airing") else 0,
        str(rec.get("added_by") or ""),
    )


def row_to_show(row) -> dict:
    """A distrakt_shows row back into the record shape the renderers consume.

    `key` is derived rather than stored: it is the flat form of the three columns
    beside it, and it is what a client names a row by, so computing it here means
    no caller has to know how to spell it.
    """
    ids = collect_ids({id_key: row[column] for column, id_key in ID_COLUMNS.items()})
    return {
        "media": row["media"],
        "match_source": row["match_source"],
        "match_id": row["match_id"],
        "key": item_key(row["media"], row["match_source"], row["match_id"]),
        "season": row["season"],
        "ids": ids,
        "title": row["title"] or "",
        "network": row["network"] or "",
        "abandoned": bool(row["abandoned"]),
        "abandoned_form": row["abandoned_form"],
        "watched": row["watched"],
        "total": row["total"],
        "cadence": row["cadence"],
        "premiere": row["premiere"],
        "finale": row["finale"],
        "bucket": row["bucket"],
        "started_airing": bool(row["started_airing"]),
        "finished_airing": bool(row["finished_airing"]),
        "added_by": row["added_by"] or "",
    }


async def load_month(user_id: int, month: str) -> dict | None:
    """Return this user's stored month doc, or None if it has not been created.

    None — rather than an empty default — lets the rollover logic distinguish an
    uninitialized month from an initialized-but-empty one.
    """
    month = _validate_month(month)
    mrow = await db.fetch_one(
        "SELECT closed, totals_refreshed_at, movies_json FROM distrakt_months "
        "WHERE user_id = ? AND month = ?",
        (user_id, month),
    )
    if mrow is None:
        return None
    rows = await db.fetch_all(
        "SELECT * FROM distrakt_shows WHERE user_id = ? AND month = ? ORDER BY rowid",
        (user_id, month),
    )
    doc = {
        "month": month,
        "closed": bool(mrow["closed"]),
        "totals_refreshed_at": mrow["totals_refreshed_at"],
        "shows": [row_to_show(r) for r in rows],
    }
    # `movies` only exists on a frozen month (snapshotted at freeze time). Mirror
    # the file model, where the key was simply absent until then.
    if mrow["movies_json"] is not None:
        doc["movies"] = json.loads(mrow["movies_json"])
    return doc


async def save_month(user_id: int, doc: dict) -> None:
    """Persist a whole month doc for `user_id` in one transaction: upsert the
    month-level row, then replace the month's show rows with the doc's.

    Whole-doc replace matches how the freeze pass and the lazy init build a doc
    (from scratch or by recomputing every record); the per-item mutators below
    write single rows instead.
    """
    month = _validate_month((doc or {}).get("month"))
    closed = 1 if doc.get("closed") else 0
    totals = doc.get("totals_refreshed_at")
    movies = doc.get("movies")
    movies_json = None if movies is None else json.dumps(movies)
    shows = doc.get("shows") or []

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "INSERT INTO distrakt_months "
            "(user_id, month, closed, totals_refreshed_at, movies_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, month) DO UPDATE SET "
            "closed = excluded.closed, totals_refreshed_at = excluded.totals_refreshed_at, "
            "movies_json = excluded.movies_json",
            (user_id, month, closed, totals, movies_json, db.now()),
        )
        conn.execute("DELETE FROM distrakt_shows WHERE user_id = ? AND month = ?", (user_id, month))
        for rec in shows:
            conn.execute(_INSERT_SHOW_SQL, _show_params(user_id, month, rec))

    await db.transaction(_work)


async def months_with_shows(user_id: int) -> set[str]:
    """The months that actually HOLD something for this user.

    Distinct from list_months, which answers "has a month row", because the two
    diverge the moment a month is emptied: removing its last show leaves the
    month row behind, so the history backfill — which refuses to touch a month
    somebody is already keeping — saw a curated month where there was nothing at
    all, and could never refill it. What deserves protecting is the content, not
    the row.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT month FROM distrakt_shows WHERE user_id = ?", (user_id,))
    return {r["month"] for r in rows}


async def set_month_movies(user_id: int, month: str, movies: list[dict]) -> None:
    """Replace just the films a CLOSED month snapshotted, leaving its roster rows
    and its closed flag alone.

    Targeted rather than a save_month round trip because the caller (the history
    backfill) has learned about films for a month that was already frozen: the
    verdicts in it were settled and must not be rewritten, but the **Movies**
    section is a record of what was watched, and leaving it stale would make the
    month contradict the plan that just told the user what it found.
    """
    await db.execute(
        "UPDATE distrakt_months SET movies_json = ? WHERE user_id = ? AND month = ?",
        (json.dumps(list(movies or [])), user_id, _validate_month(month)),
    )


async def list_months(user_id: int) -> list[str]:
    """Sorted list of 'YYYY-MM' this user has a month row for."""
    rows = await db.fetch_all(
        "SELECT month FROM distrakt_months WHERE user_id = ? ORDER BY month",
        (user_id,),
    )
    return [r["month"] for r in rows]


async def add_show(user_id: int, month: str, show: dict) -> None:
    """Upsert a title+season into `user_id`'s `month`, creating the month row if
    needed. Keyed by (media, match_source, match_id, season): a new one is
    inserted as a full record; an existing one is updated in place with whatever
    updatable keys `show` carries (so this doubles as a live-counts writer)."""
    month = _validate_month(month)
    key_params = row_params(show)

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "INSERT INTO distrakt_months "
            "(user_id, month, closed, totals_refreshed_at, movies_json, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) ON CONFLICT(user_id, month) DO NOTHING",
            (user_id, month, db.now()),
        )
        existing = conn.execute(
            f"SELECT match_id FROM distrakt_shows {_ROW_WHERE}",
            (user_id, month, *key_params),
        ).fetchone()
        if existing is None:
            conn.execute(_INSERT_SHOW_SQL, _show_params(user_id, month, normalize_show(show)))
            return
        updates = _coerce_update(show)
        if not updates:
            return
        cols = list(updates)
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        params = [(1 if updates[c] else 0) if c in _BOOL_COLUMNS else updates[c] for c in cols]
        params += [user_id, month, *key_params]
        conn.execute(f"UPDATE distrakt_shows SET {set_clause} {_ROW_WHERE}", params)

    await db.transaction(_work)


async def set_abandoned(user_id: int, month: str, key: ItemKey, season: int,
                        abandoned: bool, abandoned_form: str | None = None) -> dict | None:
    """Toggle a title's abandoned flag. Returns the updated record, or None if the
    month or the title+season isn't present for this user.

    When abandoning, `abandoned_form` freezes the rendered inline form so the
    Discord line stays stable; un-abandoning clears it.
    """
    month = _validate_month(month)
    key_params = (key.media, key.match_source, key.match_id, int(season))

    def _work(conn: db.Connection) -> dict | None:
        row = conn.execute(
            f"SELECT match_id FROM distrakt_shows {_ROW_WHERE}",
            (user_id, month, *key_params),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            f"UPDATE distrakt_shows SET abandoned = ?, abandoned_form = ? {_ROW_WHERE}",
            (1 if abandoned else 0, abandoned_form if abandoned else None,
             user_id, month, *key_params),
        )
        updated = conn.execute(
            f"SELECT * FROM distrakt_shows {_ROW_WHERE}", (user_id, month, *key_params),
        ).fetchone()
        return row_to_show(updated)

    return await db.transaction(_work)


async def remove_show(user_id: int, month: str, key: ItemKey, season) -> bool:
    """Delete a title+season from a user's month. Returns True if a record was
    removed. Callers guard closed (frozen) months."""
    month = _validate_month(month)
    result = await db.execute(
        f"DELETE FROM distrakt_shows {_ROW_WHERE}",
        (user_id, month, key.media, key.match_source, key.match_id, int(season)),
    )
    return result.rowcount > 0


def frozen_shows(doc: dict) -> list[dict]:
    """LIVE SHOW SHAPE list for a CLOSED month, straight from the stored snapshot
    — NO Trakt calls. The freeze pass already persisted watched/total/cadence/
    premiere/finale/started_airing/finished_airing/bucket onto each record, so the
    discord_fmt renderers read them as-is."""
    out = []
    for rec in doc.get("shows") or []:
        show = dict(rec)
        show.setdefault("started_airing", False)
        show.setdefault("finished_airing", False)
        out.append(show)
    return out


async def stamp_refreshed(user_id: int, month_key: str) -> None:
    """Record that a user's open month totals were just refreshed."""
    await db.execute(
        "UPDATE distrakt_months SET totals_refreshed_at = ? WHERE user_id = ? AND month = ?",
        (db.now(), user_id, _validate_month(month_key)),
    )
