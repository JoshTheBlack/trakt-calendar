"""Per-user JSON export / restore of the whole tracker dataset.

The export is one user's complete distrakt data — every month, roster row,
watch-state row, per-season progress, movie watch and emoji mapping — as a single
document carrying a schema version. Restore is the inverse: REPLACE (not merge),
in one transaction, scoped to the requesting user.
"""
from __future__ import annotations

import logging

from .. import db
from ..providers.base import Media, collect_ids, resolve_key
from .store import IDENTITY_COLUMNS, SHOW_COLUMNS

logger = logging.getLogger(__name__)

# Bump only on an incompatible change to the exported shape. Restore refuses a
# version it doesn't understand rather than guessing at an older/newer layout.
# 2 added distrakt_prefs (the network->emoji map). 3 re-keyed the roster and the
# two caches onto (media, match_source, match_id) — see MIGRATION_18 in app/db.py.
# Version 1 and 2 documents still restore; see _upgrade_legacy for what that
# takes and what it cannot carry across.
EXPORT_SCHEMA = 3
SUPPORTED_EXPORT_SCHEMAS = (1, 2, 3)

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
    ("distrakt_shows", ("month", *SHOW_COLUMNS)),
    ("distrakt_watch_state", ("last_synced", "beacons_json")),
    ("distrakt_show_progress", (*IDENTITY_COLUMNS, "season", "watched_episodes_json",
                                "trakt_id", "simkl_id")),
    ("distrakt_movie_watches", (*IDENTITY_COLUMNS, "watched_at", "title", "year",
                                "trakt_id", "simkl_id")),
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


def _upgrade_legacy(doc: dict) -> dict:
    """A schema 1 or 2 document read into the current shape.

    Those documents pre-date the re-key: their rows name a title by Trakt's own
    id. Roster rows carry `tmdb` as well, so the identity waterfall can be run
    over what each row already holds — the same resolution MIGRATION_18 does to a
    live database, deliberately, so a backup and a database of the same vintage
    come out the same.

    The two CACHE tables cannot be resolved that way (they never recorded a shared
    id for anything) and are dropped, with `last_synced` cleared so the next sync
    re-seeds them from the start of the current month. That is a re-fetch, not a
    loss: films in months that were already FROZEN live on the month row, which
    this carries across untouched.

    A roster row that resolves to no shared id at all REFUSES the whole restore
    rather than being dropped or given an invented key. It is somebody's roster,
    and a partial restore that silently omitted rows would be worse than one that
    says what it cannot do.
    """
    upgraded = dict(doc)
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
    upgraded["distrakt_shows"] = rows
    for table in _CACHE_TABLES:
        upgraded.pop(table, None)
    if doc.get("distrakt_watch_state"):
        upgraded["distrakt_watch_state"] = [
            {**dict(row), "last_synced": None} for row in doc["distrakt_watch_state"]
            if isinstance(row, dict)
        ]
    logger.info(
        "Restoring a schema-%s tracker backup: %d roster row(s) re-keyed onto "
        "shared ids; its cached progress and film watches are left to re-fetch.",
        doc.get("schema"), len(rows),
    )
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
