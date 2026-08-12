"""Per-user JSON export / restore of the whole tracker dataset.

The export is one user's complete distrakt data — every month, roster row,
watch-state row, per-season progress, movie watch and emoji mapping — as a single
document carrying a schema version. Restore is the inverse: REPLACE (not merge),
in one transaction, scoped to the requesting user.
"""
from __future__ import annotations

import json
import logging

from .. import db
from ..providers.base import Media, collect_ids, resolve_key
from . import store
from .store import IDENTITY_COLUMNS, MONTH_RECORD_COLUMNS, USER_RECORD_COLUMNS

logger = logging.getLogger(__name__)

# Bump only on an incompatible change to the exported shape. Restore refuses a
# version it doesn't understand rather than guessing at an older/newer layout.
# 2 added distrakt_prefs (the network->emoji map). 3 re-keyed the roster and the
# two caches onto (media, match_source, match_id) — see MIGRATION_18 in app/db.py.
# 4 split the single roster table into month records and user records — see
# MIGRATION_19. 5 gave every cached row and every sync cursor the SERVICE that
# reported it — see MIGRATION_22. Version 1, 2, 3 and 4 documents still restore;
# see _upgrade_legacy for what that takes and what it cannot carry across.
EXPORT_SCHEMA = 5
SUPPORTED_EXPORT_SCHEMAS = (1, 2, 3, 4, 5)

# The service every row in a pre-5 document came from. Nothing else has ever
# written the tracker's caches, so this is a statement of fact rather than a
# guess — the same one MIGRATION_22 writes into a live database, deliberately, so
# a backup and a database of the same vintage come out the same.
_LEGACY_SOURCE = "trakt"

# The tables that hold nothing but a cache of a provider's own answers. A legacy
# document's copies of these are DROPPED rather than migrated: their rows are
# keyed on the provider's id and carry no shared id to re-key from, and the next
# tracker load re-fetches them anyway.
_CACHE_TABLES = ("distrakt_show_progress", "distrakt_movie_watches")

# (table, columns-excluding-user_id). The export lists rows verbatim so an
# export -> restore round trip is an identity; restore always writes user_id from
# the session, never from the file.
_EXPORT_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("distrakt_months", ("month", "closed", "totals_refreshed_at", "movies_json", "created_at")),
    ("distrakt_month_records", MONTH_RECORD_COLUMNS),
    ("distrakt_user_seasons", USER_RECORD_COLUMNS),
    ("distrakt_prompt_dismissals", (*IDENTITY_COLUMNS, "season", "created_at")),
    ("distrakt_watch_state", ("cursors_json", "beacons_json")),
    ("distrakt_show_progress", (*IDENTITY_COLUMNS, "season", "source",
                                "watched_episodes_json", "trakt_id", "simkl_id")),
    ("distrakt_movie_watches", (*IDENTITY_COLUMNS, "source", "watched_at", "title",
                                "year", "trakt_id", "simkl_id")),
    # The emoji map travels with the backup: it is the only copy there is now
    # that nothing seeds it, so a restore that dropped it would lose work that
    # cannot be recovered from anywhere else.
    ("distrakt_prefs", ("network_emojis_json", "default_network_emoji", "updated_at")),
)


class RestoreError(ValueError):
    """A restore document that cannot be applied (unknown schema, wrong shape)."""


async def export_user_data(user_id: int) -> dict:
    """The requesting user's complete distrakt dataset as one JSON-able document.
    Contains no tokens and no other user's data."""
    doc: dict = {"schema": EXPORT_SCHEMA, "exported_at": db.now()}
    for table, cols in _EXPORT_TABLES:
        rows = await db.fetch_all(
            f"SELECT {', '.join(cols)} FROM {table} WHERE user_id = ?",
            (user_id,),
        )
        doc[table] = [{c: row[c] for c in cols} for row in rows]
    return doc


def _rekey_roster(doc: dict) -> list[dict]:
    """The `distrakt_shows` rows of a schema 1 or 2 document, re-keyed.

    Those documents pre-date the re-key: their rows name a title by Trakt's own
    id. Roster rows carry `tmdb` as well, so the identity waterfall can be run
    over what each row already holds — the same resolution MIGRATION_18 does to a
    live database, deliberately, so a backup and a database of the same vintage
    come out the same.

    A roster row that resolves to no shared id at all REFUSES the whole restore
    rather than being dropped or given an invented key. It is somebody's roster,
    and a partial restore that silently omitted rows would be worse than one that
    says what it cannot do.
    """
    rows = []
    unkeyable: list[str] = []
    for row in doc.get("distrakt_shows") or []:
        if not isinstance(row, dict):
            raise RestoreError("distrakt_shows rows must be objects")
        ids = collect_ids({"trakt": row.get("trakt_id"), "tmdb": row.get("tmdb"),
                           "slug": row.get("slug")})
        media = str(row.get("media") or Media.SHOW)
        key = resolve_key(media, ids)
        if key is None:
            unkeyable.append(str(row.get("title") or "an untitled entry"))
            continue
        rows.append({
            **{k: v for k, v in row.items()
               if k not in {"trakt_id", "tmdb", "slug", "source"}},
            "media": key.media,
            "match_source": key.match_source,
            "match_id": key.match_id,
            "trakt_id": ids.get("trakt"),
            "tmdb": ids.get("tmdb"),
            "slug": ids.get("slug") or "",
            "added_by": row.get("source") or "",
        })
    if unkeyable:
        raise RestoreError(
            f"{len(unkeyable)} row(s) in this backup name none of the shared ids "
            "the tracker files rows under, so they cannot be restored without "
            f"inventing an identity for them (first: {unkeyable[0]}). Nothing was "
            "changed."
        )
    return rows


def _premiered_in(row: dict) -> bool:
    """Whether a legacy row's "M/D" premiere date falls in the month it sits on.

    Read here rather than through discord_fmt so this stays a pure document
    transform with no feature module behind it; the format is two integers either
    side of a slash and a row that does not carry one simply did not premiere in
    a month anybody can name.
    """
    premiere = str(row.get("premiere") or "")
    month = str(row.get("month") or "")
    try:
        premiere_month = int(premiere.split("/", 1)[0])
        return premiere_month == int(month[5:7])
    except (IndexError, ValueError):
        return False


def _settled_kind(row: dict) -> str | None:
    """The verdict a legacy row recorded, or None if its month settled nothing.

    The `abandoned` flag and the frozen `bucket` are both read because either
    alone is enough: the flag is what the viewer pressed and the bucket is what
    the month wrote down when it froze, and a row carrying one without the other
    is still a row about giving something up.
    """
    if row.get("abandoned") or row.get("bucket") == store.RecordKind.ABANDONED:
        return str(store.RecordKind.ABANDONED)
    if row.get("bucket") == store.RecordKind.COMPLETED:
        return str(store.RecordKind.COMPLETED)
    return None


def _split_roster(rows: list[dict], stamp: int) -> tuple[list[dict], list[dict]]:
    """A schema-3 document's single roster list as (month records, user records).

    The same classification MIGRATION_19 applies to a live database, so a backup
    and a database of the same vintage come out the same. It is written twice —
    once in SQL there, once here — because a document is not a database and
    neither form can call the other; when the rule changes, both change.

    A row can produce TWO records: one that premiered in its month AND was settled
    there gets a premiere record and a verdict, which is two statements about that
    month rather than a duplicate.
    """
    month_records: list[dict] = []
    # Keyed by (identity, season) with the latest month winning, because the same
    # season had a copy on every month it was live in and the most recent one
    # carries the most recent counts.
    listed: dict[tuple, dict] = {}
    settled_seasons = {
        (r.get("media"), r.get("match_source"), r.get("match_id"), r.get("season"))
        for r in rows if _settled_kind(r)
    }
    for row in rows:
        season_key = (row.get("media"), row.get("match_source"),
                      row.get("match_id"), row.get("season"))
        # `created_at` is new in this shape. A legacy row never recorded when it
        # was written, so it is stamped with the moment the backup was TAKEN
        # rather than the moment of the restore: that is the closest thing the
        # document knows to when the row existed.
        shared = {"created_at": stamp,
                  **{k: v for k, v in row.items() if k not in {"bucket", "abandoned"}}}
        if _premiered_in(row):
            # A premiere record carries no viewer progress: it is a snapshot of
            # the show as it premiered, which is what makes it safe to keep.
            month_records.append({**shared, "watched": 0, "abandoned_form": None,
                                  "kind": str(store.premiere_kind(row.get("season")))})
        kind = _settled_kind(row)
        if kind:
            month_records.append({**shared, "kind": kind})
        elif season_key not in settled_seasons:
            # Giving up on a season in March is a statement about the season, so a
            # copy of it left on February must not put it back on the list.
            previous = listed.get(season_key)
            if previous is None or str(row.get("month")) >= str(previous.get("month")):
                listed[season_key] = row
    user_records = [
        {"created_at": stamp}
        | {k: v for k, v in row.items() if k not in {"bucket", "abandoned", "month",
                                                     "abandoned_form"}}
        | {"kind": str(store.RecordKind.CATCHUP if row.get("finished_airing")
                       or row.get("cadence") == "b" else store.RecordKind.KEEPUP),
           "came_back": 0}
        for row in listed.values()
    ]
    return month_records, user_records


def _split_legacy_roster(doc: dict, upgraded: dict) -> None:
    """The pre-4 half of the upgrade, applied in place to `upgraded`.

    Each step is applied only to the documents that need it, so a schema-3 backup
    is split without being re-keyed and a schema-1 one gets both.

    The two CACHE tables cannot be re-keyed (they never recorded a shared id for
    anything) and are dropped from a pre-3 document, with the sync cursor cleared
    so the next sync re-seeds them from the start of the current month. That is a
    re-fetch, not a loss: films in months that were already FROZEN live on the
    month row, which this carries across untouched.
    """
    schema = doc.get("schema")
    rows = list(doc.get("distrakt_shows") or [])
    if schema < 3:
        rows = _rekey_roster(doc)
        for table in _CACHE_TABLES:
            upgraded.pop(table, None)
        if doc.get("distrakt_watch_state"):
            upgraded["distrakt_watch_state"] = [
                {**dict(row), "last_synced": None} for row in doc["distrakt_watch_state"]
                if isinstance(row, dict)
            ]
    for row in rows:
        if not isinstance(row, dict):
            raise RestoreError("distrakt_shows rows must be objects")
    upgraded.pop("distrakt_shows", None)
    month_records, user_records = _split_roster(rows, int(doc.get("exported_at") or db.now()))
    upgraded["distrakt_month_records"] = month_records
    upgraded["distrakt_user_seasons"] = user_records
    logger.info(
        "Restoring a schema-%s tracker backup: %d roster row(s) became %d month "
        "record(s) and %d user record(s).",
        schema, len(rows), len(month_records), len(user_records),
    )


def _name_the_source(upgraded: dict) -> None:
    """The pre-5 half: every cached row and both stored blobs gain their service.

    A pre-5 document's rows are all Trakt's, so this NAMES what was already true
    rather than choosing anything. It is done here as well as in MIGRATION_22
    because a document is not a database and neither form can call the other;
    when the rule changes, both change.
    """
    for table in _CACHE_TABLES:
        rows = upgraded.get(table)
        if isinstance(rows, list):
            upgraded[table] = [
                {**row, "source": row.get("source") or _LEGACY_SOURCE}
                for row in rows if isinstance(row, dict)
            ]
    states = upgraded.get("distrakt_watch_state")
    if isinstance(states, list):
        upgraded["distrakt_watch_state"] = [
            {"cursors_json": _nest(row.get("last_synced")),
             "beacons_json": _nest(_parsed(row.get("beacons_json")))}
            for row in states if isinstance(row, dict)
        ]


def _parsed(document):
    """A stored JSON blob read back, or None if it is absent or unreadable.

    Unreadable reads as absent for the same reason the loader treats it that way:
    a beacon is a cache of a service's "something changed" marker, and the cost of
    dropping one is a single extra call after the restore.
    """
    if not document:
        return None
    try:
        return json.loads(document)
    except (TypeError, ValueError):
        return None


def _nest(value) -> str | None:
    """One value as the {source: value} document the column now holds."""
    return None if value is None else json.dumps({_LEGACY_SOURCE: value})


def _upgrade_legacy(doc: dict) -> dict:
    """A schema 1, 2, 3 or 4 document read into the current shape.

    Each half is applied only to the documents that need it, and they are
    separate because they are separate changes: a schema-4 backup already holds
    the two record tables and only wants its cached rows named.
    """
    schema = doc.get("schema")
    upgraded = dict(doc)
    if schema < 4:
        _split_legacy_roster(doc, upgraded)
    if schema < 5:
        _name_the_source(upgraded)
    return upgraded


async def restore_user_data(user_id: int, doc: dict) -> None:
    """Replace `user_id`'s distrakt data with the document's, in one transaction.

    REPLACE, not merge: the user's existing rows in the file's tables are deleted
    and the file's inserted. Any `user_id` present in the file is IGNORED — every
    row is written under the session user. Refuses an unknown schema version.

    A table the document does not carry at all is LEFT ALONE rather than emptied,
    which is what lets an older export restore onto a newer schema: a version-1
    backup predates the emoji map and says nothing about it, and reading that
    silence as "delete my map" would destroy data the file never claimed to
    describe.
    """
    if not isinstance(doc, dict):
        raise RestoreError("restore document must be an object")
    schema = doc.get("schema")
    if schema not in SUPPORTED_EXPORT_SCHEMAS:
        raise RestoreError(f"unsupported distrakt export schema: {schema!r}")
    if schema < EXPORT_SCHEMA:
        doc = _upgrade_legacy(doc)
    payload: dict[str, list] = {}
    for table, _cols in _EXPORT_TABLES:
        if table not in doc:
            continue
        rows = doc.get(table)
        if not isinstance(rows, list):
            raise RestoreError(f"{table} must be a list")
        payload[table] = rows

    def _work(conn: db.Connection) -> None:
        for table in payload:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        for table, cols in _EXPORT_TABLES:
            if table not in payload:
                continue
            collist = ", ".join(("user_id", *cols))
            placeholders = ", ".join(["?"] * (1 + len(cols)))
            sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"
            for row in payload[table]:
                if not isinstance(row, dict):
                    raise RestoreError(f"{table} rows must be objects")
                conn.execute(sql, [user_id, *((row.get(c)) for c in cols)])

    await db.transaction(_work)
