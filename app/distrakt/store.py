"""The tracker's record store: two tables, and the shape of what they hold.

ONE JOB — read and write `distrakt_months`, `distrakt_month_records`,
`distrakt_user_seasons` and `distrakt_prompt_dismissals`. No Trakt calls, no
bucketing decisions, no lifecycle. Deciding what a season's record BECOMES as its
life proceeds lives beside this module, not in it, so a store operation can be
tested against a database and nothing else.

MONTH FACTS AND VIEWER FACTS ARE DIFFERENT RECORDS, STORED SEPARATELY. "This
season premiered in July" is true of July for ever; "you are four episodes into
it" is true only right now and of no month at all. Keeping both on one row meant
the second could only be kept current by copying the row onto every month the
season was live in, and answering "what am I behind on?" meant unioning every
month and deduping — so a title's presence on today's list depended on which
month had last copied it forward.

  distrakt_months         the month-level state: whether the month is frozen,
                          when its totals were last refreshed, the films
                          snapshotted at freeze time. Keyed (user_id, month).
  distrakt_month_records  what a month announced or settled, keyed
                          (user_id, month, kind, identity, season). `kind` is IN
                          the key on purpose: a season that premiered AND was
                          settled in the same month holds two rows on it, which
                          is two different statements rather than a duplicate.
  distrakt_user_seasons   what the viewer is in the middle of, keyed
                          (user_id, identity, season) and belonging to no month.
                          keepup and catchup are two STATES of one row, so a
                          season finishing its run is an UPDATE.

A SEASON LIVES IN UP TO TWO OF THREE PLACES AT ONCE: the month it premiered in,
the viewer's list, and the month that settled it.

A PREMIERE RECORD CARRIES NO VIEWER PROGRESS. `watched` is meaningless on one and
neither written nor read there — it is a snapshot of the show as it premiered,
which is what makes it safe to keep for ever and safe to correct when a later
season lookup reports a different episode total.

WHAT NAMES A RECORD. A season is identified by (media, match_source, match_id) —
the shared id namespace the identity waterfall landed in, plus the id in it (see
app/providers/base.py). NOT by the id of whichever service happened to report it:
keying on that would make the same season arriving from a second service a second
record for ever, with nothing to say the two were the same. The service's own ids
travel on the record as ordinary attributes, because they are still what you need
to CALL that service — `record["ids"]["trakt"]` is how you ask Trakt about a
season, and `record["match_id"]` is how you find it here.

In memory a month is the same `doc` dict the renderers have always consumed —
    {month, closed, totals_refreshed_at, movies?, shows: [record, ...]}
— so load_month assembles that shape from distrakt_months plus that month's
records, and save_month writes it back.
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterable
from datetime import date
from enum import StrEnum

from .. import clock, db
from ..providers.base import ItemKey, Media, collect_ids, item_key, resolve_key

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class RecordKind(StrEnum):
    """What a stored record IS — the STORAGE vocabulary.

    A StrEnum, so the member IS the stored string: it goes into the `kind` column
    and out into the browser's JSON with no conversion at either boundary.

    Four kinds belong to a month and two belong to the viewer, and which table a
    kind lives in is not a second fact to remember — MONTH_KINDS and USER_KINDS
    below are derived from this set and are what every caller asks.

    Separate from Bucket, which is the RENDER vocabulary: what a SECTION of the
    page and of the Discord notices is called. The two are mapped once, in
    BUCKET_OF_KIND, and nowhere else.
    """
    SERIES_PREMIERE = "series_premiere"
    SEASON_PREMIERE = "season_premiere"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    KEEPUP = "keepup"
    CATCHUP = "catchup"


class Bucket(StrEnum):
    """The closed set of section names the browser and the Discord notices group
    by — the RENDER vocabulary.

    A StrEnum for the same reason RecordKind is: `row["bucket"]` reaches
    app/static/js/tracker/rows.js and the notice headers as a plain string with no
    conversion. These strings are read by shipped browser code, so they cannot
    change without changing that too.

    CATCHUP RENDERS AS "Cleanup", which is why this enum is not simply RecordKind
    under another name: what the viewer's own notices call that section is
    established and renaming it would be a user-visible change to something nobody
    asked to have renamed.
    """
    NEW = "new"
    RETURNING = "returning"
    KEEPUP = "keepup"
    CLEANUP = "cleanup"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


# The ONE mapping between the two vocabularies. Every renderer, every section
# header and every grouping in the browser resolves through this; a second copy of
# it anywhere is how a kind would come to render under two different headings.
BUCKET_OF_KIND: dict[RecordKind, Bucket] = {
    RecordKind.SERIES_PREMIERE: Bucket.NEW,
    RecordKind.SEASON_PREMIERE: Bucket.RETURNING,
    RecordKind.KEEPUP: Bucket.KEEPUP,
    RecordKind.CATCHUP: Bucket.CLEANUP,
    RecordKind.COMPLETED: Bucket.COMPLETED,
    RecordKind.ABANDONED: Bucket.ABANDONED,
}

# Which kinds each table may hold. Named as sets rather than restated at each
# query, because "is this a month record?" is asked by the lifecycle, the
# renderers and the two migrate verbs below, and a disagreement between two of
# them would file a record in the wrong table.
PREMIERE_KINDS = frozenset({RecordKind.SERIES_PREMIERE, RecordKind.SEASON_PREMIERE})
SETTLED_KINDS = frozenset({RecordKind.COMPLETED, RecordKind.ABANDONED})
MONTH_KINDS = PREMIERE_KINDS | SETTLED_KINDS
USER_KINDS = frozenset({RecordKind.KEEPUP, RecordKind.CATCHUP})


class MonthStanding(StrEnum):
    """Where a month stands relative to the calendar: over, in progress, or not
    yet begun.

    Named here with month_key because it is the same fact — a month is addressed
    by a "YYYY-MM" string, and everything that has to know whether that string is
    behind, around or ahead of today asks the same question of it. It was being
    asked with an inline comparison at each place that needed it, in slightly
    different words, which is how a month that was over came to be rendered as
    one still in progress.

    Not a stored value: nothing in either table holds it, because it changes
    without anything being written. It is derived from the key and the clock every
    time it is wanted.
    """
    PAST = "past"
    CURRENT = "current"
    FUTURE = "future"


def month_standing(month: str, today: date | None = None) -> MonthStanding:
    """Where `month` ("YYYY-MM") stands relative to `today`.

    Whole months, not days: a month is in progress from its 1st to its last, and
    the tracker's rules — what freezes, what previews, what a notice may say — all
    turn on the month rather than on where inside it we are.
    """
    today = today or clock.today()
    here = (today.year, today.month)
    there = parse_month_key(month)
    if there < here:
        return MonthStanding.PAST
    return MonthStanding.CURRENT if there == here else MonthStanding.FUTURE


def bucket_of_kind(kind: RecordKind | str) -> Bucket:
    """The section a record of `kind` renders under. The only reader of
    BUCKET_OF_KIND that callers outside this module should need."""
    return BUCKET_OF_KIND[RecordKind(kind)]


def premiere_kind(season: int | None) -> RecordKind:
    """Which premiere a season number is: a SERIES premiere (a first season) or a
    SEASON premiere (a later one).

    The two are separate record kinds rather than one kind sorted by season number
    when something renders. They are two distinct sections of the month's first
    notice, and deriving the split at every render is how the two come to
    disagree — so the split is decided ONCE, here, at the moment a record is made.
    """
    return RecordKind.SERIES_PREMIERE if int(season or 1) <= 1 else RecordKind.SEASON_PREMIERE


# The identity columns, in key order. Named once because both record tables, the
# dismissals table and every lookup in this package are built from them.
IDENTITY_COLUMNS = ("media", "match_source", "match_id")

# The columns that hold ids, and which ID_KEYS namespace each one holds. Both
# directions read from this one mapping: the column names carry the historical
# `_id` suffix that Trakt's did, while `ids` uses the namespace names an Item
# uses, and a second table of the same fact would drift.
ID_COLUMNS = {
    "trakt_id": "trakt", "simkl_id": "simkl", "tmdb": "tmdb",
    "tvdb": "tvdb", "imdb": "imdb", "mal": "mal", "slug": "slug",
}

# The fields both tables share, in insert order. Split out because the two record
# tables differ only at their ends — a month record adds `month` and
# `abandoned_form`, a user record adds `came_back` — and writing the common part
# twice is how one of them would quietly stop storing a field.
_SHARED_COLUMNS = (
    "kind", *IDENTITY_COLUMNS, "season", *ID_COLUMNS, "title", "network",
    "watched", "total", "cadence", "premiere", "finale",
    "started_airing", "finished_airing", "added_by", "created_at",
)

# Every distrakt_month_records column except user_id, in insert order.
#
# `watched_by_source` / `total_by_source` are JSON objects keyed by source, held
# only on a MONTH record: a month that froze while two services disagreed has to
# keep both numbers, because it will never be recomputed and a single number
# would claim one of them was the answer. A user record has no such need — it is
# recomputed on every load, from whatever the services say now. `watched` and
# `total` beside them keep holding one number, the primary source's, so every
# reader that asks for one still gets one.
MONTH_RECORD_COLUMNS = ("month", *_SHARED_COLUMNS, "abandoned_form",
                        "watched_by_source", "total_by_source")

# Every distrakt_user_seasons column except user_id, in insert order.
USER_RECORD_COLUMNS = (*_SHARED_COLUMNS, "came_back")

# Columns whose value is coerced on the way to the database. Everything else
# passes through as the caller stated it.
_INT_COLUMNS = frozenset(("season", "watched", "total"))
_BOOL_COLUMNS = frozenset(("started_airing", "finished_airing", "came_back"))
_TEXT_COLUMNS = frozenset(("title", "network", "added_by"))
# Columns whose value is a small mapping in a record and JSON text in the
# database. Named here rather than serialized at each call site so a record can
# be handed around as a record — every reader of `watched_by_source` gets a dict,
# and only the two functions below ever see the text form. An empty mapping is
# stored as NULL, which is what "nobody wrote a breakdown down" already looks
# like on every row written before these columns existed.
_JSON_COLUMNS = frozenset(("watched_by_source", "total_by_source"))

# Who put a record there. Only CALENDAR records mean "the calendar says you're
# watching this", which is why removing one marks the show turned away there and
# removing any other kind does not. "" is a record written before the column
# existed.
ADDED_BY_CALENDAR = "calendar"
ADDED_BY_HISTORY = "history"
ADDED_BY_MANUAL = "manual"
ADDED_BY_VALUES = (ADDED_BY_CALENDAR, ADDED_BY_HISTORY, ADDED_BY_MANUAL)

# What an in-place update may touch. The identity, the season, the month and the
# kind are excluded because changing one of them addresses a DIFFERENT record —
# moving a season between kinds is a migration, not an update, and has its own
# verbs below. `added_by` is excluded because it records who FIRST filed the
# record and a live-counts write must not turn a calendar record into a manual
# one. `created_at` is excluded for the same reason.
_UPDATABLE_MONTH_COLUMNS = frozenset(MONTH_RECORD_COLUMNS) - {
    "month", "kind", *IDENTITY_COLUMNS, "season", "added_by", "created_at"}
# The user set differs twice over. `kind` IS updatable here, because keepup and
# catchup are two states of one record rather than two records — a season
# finishing its run changes the record it already has. `came_back` is not: it is
# the marker for a season that turned out not to have been finished, and only the
# viewer acknowledging it clears it, so a routine counts refresh written back
# without the flag must not dismiss a marker nobody has read.
_UPDATABLE_USER_COLUMNS = frozenset(USER_RECORD_COLUMNS) - {
    *IDENTITY_COLUMNS, "season", "added_by", "created_at", "came_back"}


def _insert_sql(table: str, columns: tuple[str, ...]) -> str:
    return (f"INSERT INTO {table} (user_id, " + ", ".join(columns) + ") "
            "VALUES (" + ", ".join(["?"] * (1 + len(columns))) + ")")


_INSERT_MONTH_SQL = _insert_sql("distrakt_month_records", MONTH_RECORD_COLUMNS)
_INSERT_USER_SQL = _insert_sql("distrakt_user_seasons", USER_RECORD_COLUMNS)

# The WHERE clauses that address one record. Written once each because every
# single-record read, update and delete below shares one of them, and a mismatch
# between two of them would be a cross-record write.
_IDENTITY_MATCH = " AND ".join(f"{c} = ?" for c in IDENTITY_COLUMNS)
_MONTH_ROW_WHERE = f"WHERE user_id = ? AND month = ? AND kind = ? AND {_IDENTITY_MATCH} AND season = ?"
# Addresses one season of one user in EITHER table: it is the whole key of a user
# record, and the part of a month record's key that is not the month and the kind.
_SEASON_WHERE = f"WHERE user_id = ? AND {_IDENTITY_MATCH} AND season = ?"


class UnkeyableRecord(ValueError):
    """A record that names no shared id, so nothing can tell it apart from
    another title of the same name. Raised rather than stored under a made-up
    identity — see app/providers/base.py's MATCH_SOURCES."""


def month_key(year: int, month: int) -> str:
    """The key a month is stored and addressed under: zero-padded "YYYY-MM".

    Here rather than in whichever caller needed one first, because the format is
    a STORAGE fact — it is the key half of distrakt_months' identity, and
    _MONTH_RE above is the same fact stated as a validator.

    THE ZERO-PADDING IS LOAD-BEARING, not cosmetic: several callers compare month
    keys with `<` and `>=` to decide which months are past, and the backward walk
    below orders by the column. String comparison only agrees with chronological
    order while every key is the same width, so "2026-7" would sort after
    "2026-12" and silently mis-file a month.
    """
    return f"{int(year):04d}-{int(month):02d}"


def parse_month_key(month: str) -> tuple[int, int]:
    """month_key's inverse: "YYYY-MM" back to (year, month).

    Beside month_key because it is the same fact read the other way, and the
    format has exactly one owner in this module — _MONTH_RE states it, month_key
    writes it, this reads it. Without an inverse every caller sliced the string
    itself, which came to nine copies of `int(key[:4]), int(key[5:7])` plus one
    written a second way as `key.split("-")`.

    Validates rather than slicing blindly. A caller handing this a date, an empty
    string or a "2026-7" gets a ValueError naming the value, instead of a plausible
    (2026, 0) that goes wrong somewhere further along.
    """
    _validate_month(month)
    return int(month[:4]), int(month[5:7])


def month_first_day(month: str) -> date:
    """The 1st of "YYYY-MM", for the callers that want a date rather than a pair.

    Several of them built `date(int(key[:4]), int(key[5:7]), 1)` inline. Named
    here so "the month starts on its first day" is stated once next to the format
    it is derived from.
    """
    year, month_number = parse_month_key(month)
    return date(year, month_number, 1)


def _validate_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    return month


def _validate_kind(kind, allowed: frozenset[RecordKind], where: str) -> RecordKind:
    """`kind` as a RecordKind, refused if it cannot live in `where`.

    Checked at the boundary rather than left to the primary key, because a kind
    in the wrong table is not a constraint violation — both tables would accept
    the string quite happily, and the record would simply never be found again by
    the reader that expected it in the other one.
    """
    try:
        resolved = RecordKind(kind)
    except ValueError:
        raise ValueError(
            f"{kind!r} is not one of the tracker's record kinds "
            f"({', '.join(sorted(RecordKind))})") from None
    if resolved not in allowed:
        raise ValueError(
            f"{resolved} is not a {where} kind; those are "
            f"{', '.join(sorted(allowed))}")
    return resolved


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
            "the tracker files records under."
        )
    return key


def row_params(rec: dict) -> tuple:
    """(media, match_source, match_id, season) for a record — the tuple every
    single-record statement in this module ends with."""
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
    """Build a full record from `show`, filling schema defaults.

    Resolves the identity here rather than at each call site, so a record that
    cannot be keyed is refused at the moment it is built and not two layers
    further on, where the caller no longer knows which title it came from.

    THE KIND IS REQUIRED AND IS NOT GUESSED. It decides which table the record
    lives in and, for a month record, is half of what addresses it — a default
    would file somebody's completed season as a premiere, and nothing downstream
    would ever notice. premiere_kind() is there for the callers that genuinely are
    making a premiere and want the series/season split made for them.
    """
    incoming = dict(show or {})
    key = record_key(incoming)
    kind = _validate_kind(incoming.get("kind"), frozenset(RecordKind), "stored")
    return {
        "media": key.media,
        "match_source": key.match_source,
        "match_id": key.match_id,
        "key": str(key),
        "season": int(incoming["season"]),
        "ids": collect_ids(incoming.get("ids") or {}),
        "title": str(incoming.get("title") or ""),
        "network": str(incoming.get("network") or ""),
        "kind": str(kind),
        "bucket": str(bucket_of_kind(kind)),
        "abandoned": kind is RecordKind.ABANDONED,
        "abandoned_form": incoming.get("abandoned_form"),
        "came_back": bool(incoming.get("came_back", False)),
        "watched": int(incoming.get("watched") or 0),
        "total": int(incoming.get("total") or 0),
        "cadence": incoming.get("cadence"),
        "premiere": incoming.get("premiere"),
        "finale": incoming.get("finale"),
        "started_airing": bool(incoming.get("started_airing", False)),
        "finished_airing": bool(incoming.get("finished_airing", False)),
        "added_by": str(incoming.get("added_by") or ""),
        "created_at": incoming.get("created_at"),
        # Carried through as mappings; only a MONTH record has columns for them,
        # and a user record simply drops them on the way to the database because
        # MONTH_RECORD_COLUMNS is what names them.
        "watched_by_source": dict(incoming.get("watched_by_source") or {}),
        "total_by_source": dict(incoming.get("total_by_source") or {}),
    }


def _column_value(column: str, rec: dict):
    """One column's stored value from a record dict.

    One mapping for both tables and for both the insert and the update path, so
    "how is `watched` coerced?" has a single answer rather than one per statement.
    """
    if column in ID_COLUMNS:
        return (rec.get("ids") or {}).get(ID_COLUMNS[column])
    if column in _BOOL_COLUMNS:
        return 1 if rec.get(column) else 0
    if column in _INT_COLUMNS:
        return int(rec.get(column) or 0)
    if column in _TEXT_COLUMNS:
        return str(rec.get(column) or "")
    if column == "kind":
        return str(rec["kind"])
    if column in _JSON_COLUMNS:
        value = rec.get(column)
        return json.dumps(value) if value else None
    return rec.get(column)


def _insert_params(user_id: int, columns: tuple[str, ...], rec: dict) -> list:
    return [user_id, *(_column_value(c, rec) for c in columns)]


def _coerce_update(fields: dict, updatable: frozenset[str]) -> dict:
    """The subset of `updatable` present in `fields`, coerced for an in-place
    update. Anything that is not an updatable column is dropped.

    `ids` is expanded into its columns: a record learning an id it did not have
    when it was written is the one way an identity can be improved later, and the
    caller states it the same way it states one on an insert.
    """
    out: dict = {}
    for column, id_key in ID_COLUMNS.items():
        if column in updatable and id_key in (fields or {}).get("ids", {}):
            out[column] = fields["ids"][id_key]
    for name in (fields or {}):
        if name in updatable and name not in ID_COLUMNS:
            out[name] = _column_value(name, fields)
    return out


def _differences(existing, updates: dict) -> dict:
    """The subset of `updates` whose value the stored row does not already hold.

    EVERY LOAD RE-WRITES WHAT IT READ, otherwise. The live pass hands the same
    counts and dates back for every record on the page whether or not anything
    moved, so an unconditional update turned a plain read of a month into one
    write per row — a page of fifty seasons cost fifty pointless UPDATEs, every
    time anybody looked at it.

    A bool compares equal to the 0/1 SQLite stores it as, so no coercion is
    needed here; a column genuinely changing type would show up as a difference,
    which is the safe direction to be wrong in.
    """
    return {name: value for name, value in updates.items()
            if existing[name] != value}


def _stored_json(document) -> dict:
    """A JSON column back as a mapping, or an empty one.

    Unreadable reads as empty rather than raising: these columns hold a
    BREAKDOWN of numbers the row also carries in full, so the cost of not
    understanding one is a row that shows a single number, and refusing to render
    somebody's month over it would be far worse.
    """
    if not document:
        return {}
    try:
        parsed = json.loads(document)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def row_to_record(row) -> dict:
    """A stored row from either table back into the record shape the renderers
    consume.

    One reader for both tables because the shape they hand out is the same one:
    the columns they do not share are read only when the row actually has them, so
    a month record carries `month` and `abandoned_form` while a user record
    carries `came_back`, and neither has to pretend to the other's fields.

    `key` and `bucket` are DERIVED rather than stored. `key` is the flat form of
    the three identity columns beside it and is what a client names a record by;
    `bucket` is what section the record renders under, which BUCKET_OF_KIND
    already answers from `kind`. Storing either would be a second copy of a fact
    that can go stale.
    """
    columns = set(row.keys())
    kind = RecordKind(row["kind"])
    ids = collect_ids({id_key: row[column] for column, id_key in ID_COLUMNS.items()})
    rec = {
        "media": row["media"],
        "match_source": row["match_source"],
        "match_id": row["match_id"],
        "key": item_key(row["media"], row["match_source"], row["match_id"]),
        "season": row["season"],
        "ids": ids,
        "title": row["title"] or "",
        "network": row["network"] or "",
        "kind": str(kind),
        "bucket": str(BUCKET_OF_KIND[kind]),
        # The manual "I gave up on this" the renderers ask about. Derived from the
        # kind, which IS that statement now that giving up moves the record onto
        # the month rather than setting a flag on it.
        "abandoned": kind is RecordKind.ABANDONED,
        "watched": row["watched"],
        "total": row["total"],
        "cadence": row["cadence"],
        "premiere": row["premiere"],
        "finale": row["finale"],
        "started_airing": bool(row["started_airing"]),
        "finished_airing": bool(row["finished_airing"]),
        "added_by": row["added_by"] or "",
        "created_at": row["created_at"],
    }
    if "month" in columns:
        rec["month"] = row["month"]
    # WHAT EACH SOURCE SAID, on a month that recorded it. A month frozen while
    # two services disagreed keeps both numbers here and can still render the
    # disagreement years later; a month frozen with one service linked has one
    # entry, which reads back identically to a single number. NULL — every row
    # written before the columns existed — reads as "no breakdown was written
    # down", not as "the sources agreed on nothing".
    for column in ("watched_by_source", "total_by_source"):
        if column in columns:
            rec[column] = _stored_json(row[column])
    if "abandoned_form" in columns:
        rec["abandoned_form"] = row["abandoned_form"]
    if "came_back" in columns:
        rec["came_back"] = bool(row["came_back"])
    return rec


def _kind_filter(kinds: Iterable[RecordKind | str] | None) -> tuple[str, list[str]]:
    """An optional `AND kind IN (...)` clause and its parameters.

    Returns an empty clause for None rather than listing every kind, so "all of
    them" costs no query text and cannot fall behind a kind being added.
    """
    if kinds is None:
        return "", []
    values = [str(RecordKind(k)) for k in kinds]
    if not values:
        # An explicit empty selection is a caller asking for nothing, which is a
        # legitimate thing to ask for and must not silently mean "everything".
        return " AND 0", []
    return " AND kind IN (" + ", ".join(["?"] * len(values)) + ")", values


# ---------------------------------------------------------------------------
# the month row: whether it is frozen, when it refreshed, what films it kept
# ---------------------------------------------------------------------------

async def load_month(user_id: int, month: str) -> dict | None:
    """Return this user's stored month doc, or None if it has not been created.

    None — rather than an empty default — lets the callers distinguish an
    uninitialized month from an initialized-but-empty one.

    `shows` is every record the month holds, of every kind, in the order they
    were written. A reader that wants one kind asks month_records for it rather
    than filtering this, so the filter is stated once.
    """
    month = _validate_month(month)
    mrow = await db.fetch_one(
        "SELECT closed, totals_refreshed_at, movies_json FROM distrakt_months "
        "WHERE user_id = ? AND month = ?",
        (user_id, month),
    )
    if mrow is None:
        return None
    doc = {
        "month": month,
        "closed": bool(mrow["closed"]),
        "totals_refreshed_at": mrow["totals_refreshed_at"],
        "shows": await month_records(user_id, month),
    }
    # `movies` only exists on a frozen month (snapshotted at freeze time). Mirror
    # the file model, where the key was simply absent until then.
    if mrow["movies_json"] is not None:
        doc["movies"] = json.loads(mrow["movies_json"])
    return doc


async def save_month(user_id: int, doc: dict) -> None:
    """Persist a whole month doc for `user_id` in one transaction: upsert the
    month-level row, then replace the month's records with the doc's.

    Whole-doc replace matches how the freeze pass and the lazy init build a doc
    (from scratch, or by recomputing every record); the per-record verbs below
    write one record instead. Every record in `shows` must name a MONTH kind —
    the viewer's own list is not part of any month and is written through
    add_user_record.
    """
    month = _validate_month((doc or {}).get("month"))
    closed = 1 if doc.get("closed") else 0
    totals = doc.get("totals_refreshed_at")
    movies = doc.get("movies")
    movies_json = None if movies is None else json.dumps(movies)
    # Normalized before the transaction opens, so a record that cannot be keyed or
    # names no kind refuses the save without having deleted the month's records
    # first.
    records = [normalize_show(rec) for rec in (doc.get("shows") or [])]
    for rec in records:
        _validate_kind(rec["kind"], MONTH_KINDS, "month record")
    now = db.now()

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "INSERT INTO distrakt_months "
            "(user_id, month, closed, totals_refreshed_at, movies_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, month) DO UPDATE SET "
            "closed = excluded.closed, totals_refreshed_at = excluded.totals_refreshed_at, "
            "movies_json = excluded.movies_json",
            (user_id, month, closed, totals, movies_json, now),
        )
        conn.execute("DELETE FROM distrakt_month_records WHERE user_id = ? AND month = ?",
                     (user_id, month))
        for rec in records:
            conn.execute(_INSERT_MONTH_SQL, _insert_params(
                user_id, MONTH_RECORD_COLUMNS,
                {**rec, "month": month, "created_at": rec.get("created_at") or now}))

    await db.transaction(_work)


async def list_months(user_id: int) -> list[str]:
    """Sorted list of 'YYYY-MM' this user has a month row for."""
    rows = await db.fetch_all(
        "SELECT month FROM distrakt_months WHERE user_id = ? ORDER BY month",
        (user_id,),
    )
    return [r["month"] for r in rows]


async def months_with_shows(user_id: int) -> set[str]:
    """The months that actually HOLD something for this user.

    Distinct from list_months, which answers "has a month row", because the two
    diverge the moment a month is emptied: removing its last record leaves the
    month row behind, so the history backfill — which refuses to touch a month
    somebody is already keeping — saw a curated month where there was nothing at
    all, and could never refill it. What deserves protecting is the content, not
    the row.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT month FROM distrakt_month_records WHERE user_id = ?", (user_id,))
    return {r["month"] for r in rows}


async def set_month_movies(user_id: int, month: str, movies: list[dict]) -> None:
    """Replace just the films a CLOSED month snapshotted, leaving its records and
    its closed flag alone.

    Targeted rather than a save_month round trip because the caller (the history
    backfill) has learned about films for a month that was already frozen: the
    verdicts in it were settled and must not be rewritten, but the **Movies**
    section is a record of what was seen, and leaving it stale would make the
    month contradict the plan that just told the user what it found.
    """
    await db.execute(
        "UPDATE distrakt_months SET movies_json = ? WHERE user_id = ? AND month = ?",
        (json.dumps(list(movies or [])), user_id, _validate_month(month)),
    )


async def stamp_refreshed(user_id: int, month_key: str) -> None:
    """Record that a user's open month totals were just refreshed."""
    await db.execute(
        "UPDATE distrakt_months SET totals_refreshed_at = ? WHERE user_id = ? AND month = ?",
        (db.now(), user_id, _validate_month(month_key)),
    )


def frozen_shows(doc: dict) -> list[dict]:
    """LIVE SHOW SHAPE list for a CLOSED month, straight from the stored snapshot
    — NO Trakt calls. The freeze pass already persisted watched/total/cadence/
    premiere/finale/started_airing/finished_airing onto each record, so the
    discord_fmt renderers read them as-is."""
    out = []
    for rec in doc.get("shows") or []:
        show = dict(rec)
        show.setdefault("started_airing", False)
        show.setdefault("finished_airing", False)
        out.append(show)
    return out


# ---------------------------------------------------------------------------
# month records — what a month announced, and what it settled
# ---------------------------------------------------------------------------

async def month_records(user_id: int, month: str,
                        kinds: Iterable[RecordKind | str] | None = None) -> list[dict]:
    """One month's records, optionally narrowed to some kinds, in write order.

    The kinds filter is here rather than in each caller because "which records
    does this month's page show?" is asked three times with three different
    answers (a month not begun, one in progress, one that is over), and each of
    those is a list of kinds — not a different query.
    """
    clause, params = _kind_filter(kinds)
    rows = await db.fetch_all(
        "SELECT * FROM distrakt_month_records WHERE user_id = ? AND month = ?"
        + clause + " ORDER BY rowid",
        (user_id, _validate_month(month), *params),
    )
    return [row_to_record(r) for r in rows]


async def find_month_record(user_id: int, month: str, kind: RecordKind | str,
                            key: ItemKey, season: int) -> dict | None:
    """One month record, or None if that month holds no record of that kind for
    that season."""
    row = await db.fetch_one(
        f"SELECT * FROM distrakt_month_records {_MONTH_ROW_WHERE}",
        (user_id, _validate_month(month),
         str(_validate_kind(kind, MONTH_KINDS, "month record")),
         key.media, key.match_source, key.match_id, int(season)),
    )
    return None if row is None else row_to_record(row)


async def add_month_record(user_id: int, month: str, record: dict) -> dict:
    """Upsert one record onto `user_id`'s `month`, creating the month row if
    needed. Returns the stored record.

    Addressed by (month, kind, identity, season): a record that is not there is
    inserted whole, and one that is gets the updatable fields `record` carries
    written over it, so this doubles as the live-counts writer. Writing a
    DIFFERENT kind for the same season adds a second record rather than replacing
    the first — that is the premiered-and-settled-in-one-month case, and it is
    what having `kind` in the key is for.
    """
    month = _validate_month(month)
    kind = _validate_kind(record.get("kind"), MONTH_KINDS, "month record")
    key_params = row_params(record)
    address = (user_id, month, str(kind), *key_params)
    now = db.now()

    def _work(conn: db.Connection) -> dict:
        conn.execute(
            "INSERT INTO distrakt_months "
            "(user_id, month, closed, totals_refreshed_at, movies_json, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) ON CONFLICT(user_id, month) DO NOTHING",
            (user_id, month, now),
        )
        existing = conn.execute(
            f"SELECT * FROM distrakt_month_records {_MONTH_ROW_WHERE}", address,
        ).fetchone()
        if existing is None:
            conn.execute(_INSERT_MONTH_SQL, _insert_params(
                user_id, MONTH_RECORD_COLUMNS,
                {**normalize_show(record), "month": month,
                 "created_at": record.get("created_at") or now}))
        else:
            updates = _differences(
                existing, _coerce_update(record, _UPDATABLE_MONTH_COLUMNS))
            if updates:
                set_clause = ", ".join(f"{c} = ?" for c in updates)
                conn.execute(
                    f"UPDATE distrakt_month_records SET {set_clause} {_MONTH_ROW_WHERE}",
                    [*updates.values(), *address])
        return row_to_record(conn.execute(
            f"SELECT * FROM distrakt_month_records {_MONTH_ROW_WHERE}", address).fetchone())

    return await db.transaction(_work)


async def remove_month_record(user_id: int, month: str, kind: RecordKind | str,
                              key: ItemKey, season: int) -> bool:
    """Delete one record from one month. True if there was one to delete."""
    result = await db.execute(
        f"DELETE FROM distrakt_month_records {_MONTH_ROW_WHERE}",
        (user_id, _validate_month(month),
         str(_validate_kind(kind, MONTH_KINDS, "month record")),
         key.media, key.match_source, key.match_id, int(season)),
    )
    return result.rowcount > 0


async def walk_settled(user_id: int, *,
                       before: str | None = None) -> AsyncIterator[tuple[str, list[dict]]]:
    """This user's months that settled something, NEWEST FIRST, each with its
    completed and abandoned records. Yields (month, records).

    A GENERATOR because the caller stops as soon as it finds what it is looking
    for. When an episode is seen for a season nothing currently holds, the search
    walks back one month at a time until it matches or the data runs out — and the
    match is usually one or two months back, so reading every month first would
    load a viewing life to answer a question about last month.

    `before` excludes that month and everything after it, which is how the search
    skips the months it has already asked about by other means.

    Ordered by the month KEY, which is why month_key's zero-padding is
    load-bearing: string order is chronological order only while every key is the
    same width.
    """
    clause = " AND month < ?" if before else ""
    params: tuple = (user_id, _validate_month(before)) if before else (user_id,)
    kinds = [str(k) for k in sorted(SETTLED_KINDS)]
    months = await db.fetch_all(
        "SELECT DISTINCT month FROM distrakt_month_records WHERE user_id = ?"
        + clause
        + " AND kind IN (" + ", ".join(["?"] * len(kinds)) + ")"
        + " ORDER BY month DESC",
        (*params, *kinds),
    )
    for row in months:
        yield row["month"], await month_records(user_id, row["month"], SETTLED_KINDS)


# ---------------------------------------------------------------------------
# user records — what the viewer is in the middle of, on no month at all
# ---------------------------------------------------------------------------

async def user_records(user_id: int,
                       kinds: Iterable[RecordKind | str] | None = None) -> list[dict]:
    """Everything this viewer is in the middle of, optionally narrowed to keepup
    or catchup, in write order.

    THIS IS THE VIEWER'S OWN LIST, read from one table. What somebody is behind on
    is a fact about the person and true of no particular month, so it is stored as
    one and not derived by unioning every month they have ever had.
    """
    clause, params = _kind_filter(kinds)
    rows = await db.fetch_all(
        "SELECT * FROM distrakt_user_seasons WHERE user_id = ?" + clause + " ORDER BY rowid",
        (user_id, *params),
    )
    return [row_to_record(r) for r in rows]


async def find_user_record(user_id: int, key: ItemKey, season: int) -> dict | None:
    """This viewer's record for one season, or None if it is not on their list.

    No month is named because there is none to name: a season is on the list at
    most once, whichever month it premiered in and whichever month is on screen.
    """
    row = await db.fetch_one(
        f"SELECT * FROM distrakt_user_seasons {_SEASON_WHERE}",
        (user_id, key.media, key.match_source, key.match_id, int(season)),
    )
    return None if row is None else row_to_record(row)


async def add_user_record(user_id: int, record: dict) -> dict:
    """Upsert one season onto the viewer's list. Returns the stored record.

    Addressed by (identity, season) alone, so keepup and catchup can never both
    hold the same season: a record already on the list is UPDATED, which includes
    its kind changing when the season finishes airing.

    `came_back` is written on an INSERT and left alone on an update — see
    set_came_back for why only the viewer clears it.
    """
    _validate_kind(record.get("kind"), USER_KINDS, "user record")
    key_params = row_params(record)
    address = (user_id, *key_params)
    now = db.now()

    def _work(conn: db.Connection) -> dict:
        existing = conn.execute(
            f"SELECT * FROM distrakt_user_seasons {_SEASON_WHERE}", address,
        ).fetchone()
        if existing is None:
            conn.execute(_INSERT_USER_SQL, _insert_params(
                user_id, USER_RECORD_COLUMNS,
                {**normalize_show(record), "created_at": record.get("created_at") or now}))
        else:
            updates = _differences(
                existing, _coerce_update(record, _UPDATABLE_USER_COLUMNS))
            if updates:
                set_clause = ", ".join(f"{c} = ?" for c in updates)
                conn.execute(
                    f"UPDATE distrakt_user_seasons SET {set_clause} {_SEASON_WHERE}",
                    [*updates.values(), *address])
        return row_to_record(conn.execute(
            f"SELECT * FROM distrakt_user_seasons {_SEASON_WHERE}", address).fetchone())

    return await db.transaction(_work)


async def set_user_kind(user_id: int, key: ItemKey, season: int,
                        kind: RecordKind | str) -> bool:
    """Move a season between keepup and catchup. True if it was on the list.

    Its own verb rather than an add_user_record call carrying only a kind, because
    this is the one transition that happens with no new information: the season
    lookup made for every listed season on every load already reports the last
    episode's air date, and a date in the past means the season has finished
    airing. Nothing extra is fetched to decide it.
    """
    result = await db.execute(
        f"UPDATE distrakt_user_seasons SET kind = ? {_SEASON_WHERE}",
        (str(_validate_kind(kind, USER_KINDS, "user record")),
         user_id, key.media, key.match_source, key.match_id, int(season)),
    )
    return result.rowcount > 0


async def set_came_back(user_id: int, key: ItemKey, season: int, came_back: bool) -> bool:
    """Set or clear the marker that says this season had been finished and grew.
    True if the season was on the viewer's list.

    SET AS THE RECORD IS RE-ADDED AND BEFORE THE OLD MONTH RECORD GOES, because
    that completed record is what proved the season was ever finished and it is
    about to stop existing — "completed in July" has turned out not to be true, so
    the month no longer settled it. The flag is the only thing that survives the
    move, and it is what the page draws the marker from.

    CLEARED BY THE VIEWER PRESSING THE ACKNOWLEDGE CONTROL and by nothing else —
    not by time, not by the next load. That is why it is a stored flag with its
    own verb rather than something computed from the record's dates.
    """
    result = await db.execute(
        f"UPDATE distrakt_user_seasons SET came_back = ? {_SEASON_WHERE}",
        (1 if came_back else 0,
         user_id, key.media, key.match_source, key.match_id, int(season)),
    )
    return result.rowcount > 0


async def remove_user_record(user_id: int, key: ItemKey, season: int) -> bool:
    """Take one season off the viewer's list. True if it was there."""
    result = await db.execute(
        f"DELETE FROM distrakt_user_seasons {_SEASON_WHERE}",
        (user_id, key.media, key.match_source, key.match_id, int(season)),
    )
    return result.rowcount > 0


# ---------------------------------------------------------------------------
# moving a season between the two tables
# ---------------------------------------------------------------------------

async def migrate_to_month(user_id: int, key: ItemKey, season: int, *, month: str,
                           kind: RecordKind | str,
                           abandoned_form: str | None = None,
                           by_source: dict | None = None) -> dict | None:
    """Move a season OFF the viewer's list and onto `month` as a completed or
    abandoned record. Returns the record written, or None if it was not on the
    list.

    ONE TRANSACTION, because the two halves are one statement: a season that had
    been removed from the list without its verdict being recorded would vanish
    from the tracker entirely, and one recorded twice would be settled on a month
    while still being listed as in progress.

    The record's fields come from the row that was on the list — its counts, its
    dates, who first filed it — because the verdict is about that season as the
    viewer last had it, and re-deriving any of it here would be a second opinion.

    `by_source` IS THE ONE THING THE LIST ROW CANNOT SUPPLY: what each service
    said, which only a MONTH record has columns for. It is passed in by the caller
    that had the live show in hand rather than re-fetched here, and it is what
    lets a month frozen while two services disagreed still render the
    disagreement years later. Absent, the record keeps its single number and
    reads back exactly as every record written before this existed.
    """
    settled = _validate_kind(kind, SETTLED_KINDS, "settled")
    month = _validate_month(month)
    address = (user_id, key.media, key.match_source, key.match_id, int(season))
    now = db.now()

    def _work(conn: db.Connection) -> dict | None:
        row = conn.execute(
            f"SELECT * FROM distrakt_user_seasons {_SEASON_WHERE}", address).fetchone()
        if row is None:
            return None
        record = {**row_to_record(row), **(by_source or {}), "kind": str(settled),
                  "month": month,
                  "abandoned_form": abandoned_form if settled is RecordKind.ABANDONED else None,
                  "created_at": now}
        conn.execute(
            "INSERT INTO distrakt_months "
            "(user_id, month, closed, totals_refreshed_at, movies_json, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) ON CONFLICT(user_id, month) DO NOTHING",
            (user_id, month, now),
        )
        conn.execute(f"DELETE FROM distrakt_month_records {_MONTH_ROW_WHERE}",
                     (user_id, month, str(settled), *address[1:]))
        conn.execute(_INSERT_MONTH_SQL,
                     _insert_params(user_id, MONTH_RECORD_COLUMNS, record))
        conn.execute(f"DELETE FROM distrakt_user_seasons {_SEASON_WHERE}", address)
        return row_to_record(conn.execute(
            f"SELECT * FROM distrakt_month_records {_MONTH_ROW_WHERE}",
            (user_id, month, str(settled), *address[1:])).fetchone())

    return await db.transaction(_work)


async def migrate_to_user(user_id: int, key: ItemKey, season: int, *, month: str,
                          from_kind: RecordKind | str, kind: RecordKind | str,
                          came_back: bool = False) -> dict | None:
    """Move a season OFF `month`'s settled record and back onto the viewer's list.
    Returns the record written, or None if that month held no such record.

    THE MONTH RECORD DOES NOT SURVIVE. A month records what it settled, and this
    one no longer settled it: un-abandoning says the viewer did not give up after
    all, and a season that turned out to have more episodes was never finished in
    that month. A record that has turned out to be false is worse than an absent
    one.

    `came_back` is written as part of this same transaction, which is the whole
    reason it is a parameter here rather than a separate call afterwards — the
    completed record it replaces is the only other thing that remembered the
    season had once been finished, and between the two writes there would be a
    moment when nothing did.
    """
    settled = _validate_kind(from_kind, SETTLED_KINDS, "settled")
    listed = _validate_kind(kind, USER_KINDS, "user record")
    month = _validate_month(month)
    address = (user_id, key.media, key.match_source, key.match_id, int(season))
    month_address = (user_id, month, str(settled), *address[1:])
    now = db.now()

    def _work(conn: db.Connection) -> dict | None:
        row = conn.execute(
            f"SELECT * FROM distrakt_month_records {_MONTH_ROW_WHERE}",
            month_address).fetchone()
        if row is None:
            return None
        record = {**row_to_record(row), "kind": str(listed),
                  "came_back": came_back, "created_at": now}
        conn.execute(f"DELETE FROM distrakt_user_seasons {_SEASON_WHERE}", address)
        conn.execute(_INSERT_USER_SQL,
                     _insert_params(user_id, USER_RECORD_COLUMNS, record))
        conn.execute(f"DELETE FROM distrakt_month_records {_MONTH_ROW_WHERE}", month_address)
        return row_to_record(conn.execute(
            f"SELECT * FROM distrakt_user_seasons {_SEASON_WHERE}", address).fetchone())

    return await db.transaction(_work)


async def remove_season_everywhere(user_id: int, key: ItemKey, season: int) -> list[str]:
    """Delete one season from the viewer's list AND from every month that holds a
    record of it. Returns the months that actually held one, sorted.

    The ✕ on a row, and the only thing that ever removes a record. It is deliberately
    blunt: a season can hold a premiere record on one month, a verdict on another
    and a row on the viewer's list all at once, so anything narrower would leave a
    copy behind and the row would come straight back on the next load with the ✕
    looking broken.
    """
    address = (user_id, key.media, key.match_source, key.match_id, int(season))

    def _work(conn: db.Connection) -> list[str]:
        months = sorted({
            r["month"] for r in conn.execute(
                f"SELECT DISTINCT month FROM distrakt_month_records {_SEASON_WHERE}",
                address).fetchall()
        })
        conn.execute(f"DELETE FROM distrakt_month_records {_SEASON_WHERE}", address)
        conn.execute(f"DELETE FROM distrakt_user_seasons {_SEASON_WHERE}", address)
        return months

    return await db.transaction(_work)


# ---------------------------------------------------------------------------
# the prompt for an episode nothing knows about
# ---------------------------------------------------------------------------

async def dismiss_prompt(user_id: int, key: ItemKey, season: int) -> None:
    """Record that the viewer declined to add a season the tracker has never
    heard of.

    Stored, because the prompt is DERIVED from the watch history on every load:
    without somewhere to record the refusal the same episode would raise it again
    on the very next one and the ✗ would do nothing. Per SEASON rather than per
    episode, or every further episode of the same season would ask again.
    """
    await db.execute(
        "INSERT INTO distrakt_prompt_dismissals "
        "(user_id, media, match_source, match_id, season, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, media, match_source, match_id, season) DO NOTHING",
        (user_id, key.media, key.match_source, key.match_id, int(season), db.now()),
    )


async def dismissed_prompts(user_id: int) -> set[tuple[str, int]]:
    """The (item key, season) pairs this viewer has already said no to, in the
    same shape the watch-history lookups are keyed by."""
    rows = await db.fetch_all(
        "SELECT media, match_source, match_id, season FROM distrakt_prompt_dismissals "
        "WHERE user_id = ?",
        (user_id,),
    )
    return {(item_key(r["media"], r["match_source"], r["match_id"]), int(r["season"]))
            for r in rows}
