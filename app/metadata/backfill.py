"""Registering, once, every title the store and its holders already name.

THE IDENTITY TABLE STARTS EMPTY (migration 47 adds it and writes nothing — the
waterfall and the merge rules are code, and a second copy of them in a migration
would be a second answer to "which title is this"). So the first heartbeat after
the upgrade walks what is already stored and settles each title through the same
`identity.settle` every later arrival uses: each service's title rows, the
tracker's rows, the boards' rows, films watched, each season's watch progress,
the films a frozen month recorded, what the tracker wrote to a service's list.
No service is asked; this is local work.

IT IS ALSO WHERE THE STALE KEYS GO. Measured before this was written: 1,256 titles
on the development copy (83 on a production copy) carried ids that key them
differently from the key they were filed under — enrichment had learned the ids
and nothing moved the key. Settling a title against its own ids re-keys it, and
the airings and marks holding the old key move with it.

IN BATCHES, SO NO WRITER WAITS LONG. Each batch is its own transaction; a fill or
an add landing between two batches settles its own titles by the same rule, so
the order the two happen in makes no difference to where a title ends up. Done is
recorded in app_meta, and a pass that fails part-way is simply run again by the
next heartbeat — settling a title twice changes nothing the second time.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import db
from . import descriptions, identity, ids as id_rules, rows

logger = logging.getLogger(__name__)

MARKER = "title_store_registered"

# Titles settled per transaction. Small enough that a fill waiting on the write
# lock waits for one batch, not for the pass.
BATCH = 200

# The tracker's tables that keep shared ids in columns of their own, and the
# column each id space is in.
_TRACKER_TABLES = ("distrakt_user_seasons", "distrakt_month_records", "distrakt_unsettled_rows")
_TRACKER_IDS = {"trakt_id": "trakt", "simkl_id": "simkl", "tmdb": "tmdb", "tvdb": "tvdb",
                "imdb": "imdb", "mal": "mal", "trakt_slug": "trakt_slug",
                "simkl_slug": "simkl_slug"}


async def run_once() -> int:
    """Register everything stored, unless that has been done. Returns how many
    titles were settled (0 once done)."""
    if await db.get_meta(MARKER) == "1":
        return 0
    settled = await register_everything()
    await db.set_meta(MARKER, "1")
    return settled


WORDS_MARKER = "title_store_words_seeded"

# THE WORDS A TRACKER OR BOARD ROW STILL CARRIES A COPY OF, by table — what was
# drawn from the row itself until its readers moved to the store
# (app/metadata/labels.py). Read only while the columns exist.
_TRACKER_WORDS = ("title", "network")
_BOARD_WORDS = ("title", "network", "year", "runtime", "season_name", "episode_count")


async def seed_words_once() -> int:
    """Seed the store from every held row's own copy of its title's words, once,
    after the titles are registered (`run_once`). Returns how many rows gave
    words (0 once done, or before registration has run)."""
    if await db.get_meta(WORDS_MARKER) == "1" or await db.get_meta(MARKER) != "1":
        return 0
    seeded = await seed_held_words()
    await db.set_meta(WORDS_MARKER, "1")
    return seeded


async def seed_held_words(now: int | None = None) -> int:
    """Give the store every word a tracker or board row carries a copy of, where
    no service states one (descriptions.seed_words). Returns how many rows gave
    words.

    THE READERS MOVED BEFORE THE COPIES WENT. A tracker's and a board's rows draw
    their title's name, network, year and runtime — and a season row its own name
    and episode total — from the store now, with no fallback to the copy the row
    kept, so a title no service has described would draw nothing until one did:
    measured on a production copy, 26 of 55 tracker rows and 72 of 81 board rows.
    Seeding from the copies while they still exist means none draws a blank, and
    the first view of each still has its service describe it (the seed is not a
    description, and its words give way to the service's own).

    ONLY WHILE THE COLUMNS EXIST: once a migration has dropped a table's copies
    there is nothing to read, and this does nothing for that table.
    """
    ts = db.now() if now is None else now
    pending = await db.run(_held_words)
    for start in range(0, len(pending), BATCH):
        batch = pending[start:start + BATCH]

        def _work(conn, batch=batch):
            for media, ids, words in batch:
                descriptions.seed_words(conn, media, ids, now=ts, **words)

        await db.transaction(_work)
    logger.info("title store: seeded the words %d held row(s) carried", len(pending))
    return len(pending)


def _columns(conn: db.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _held_words(conn: db.Connection) -> list[tuple[str, dict, dict]]:
    """(media, ids, words) for every tracker and board row carrying a copy."""
    out: list[tuple[str, dict, dict]] = []
    if set(_TRACKER_WORDS) <= _columns(conn, "distrakt_user_seasons"):
        id_columns = ", ".join(_TRACKER_IDS)
        for row in conn.execute(f"SELECT media, match_source, match_id, title, network, "
                                f"{id_columns} FROM distrakt_user_seasons"):
            ids = {name: row[column] for column, name in _TRACKER_IDS.items()
                   if row[column] not in (None, "")}
            ids.setdefault(row["match_source"],
                           id_rules.typed(row["match_source"], row["match_id"]))
            out.append((str(row["media"]), ids,
                        {"title": row["title"] or "", "network": row["network"] or ""}))
    if set(_BOARD_WORDS) <= _columns(conn, "tier_items"):
        for row in conn.execute("SELECT media, match_source, match_id, season, tmdb, ids_json, "
                                + ", ".join(_BOARD_WORDS) + " FROM tier_items"):
            ids = {**_loads(row["ids_json"]),
                   row["match_source"]: id_rules.typed(row["match_source"], row["match_id"])}
            if row["tmdb"]:
                ids.setdefault("tmdb", row["tmdb"])
            out.append((str(row["media"]), ids, {
                "title": row["title"] or "", "network": row["network"] or "",
                "year": row["year"], "runtime": row["runtime"], "season": row["season"],
                # A SEASON ROW'S OWN name and count; a whole show's counts are its
                # description's (descriptions.seed_words says why).
                "season_name": (row["season_name"] or "") if row["season"] is not None else "",
                "episode_count": row["episode_count"] if row["season"] is not None else None}))
    return out


async def register_everything(now: int | None = None) -> int:
    """Settle every title any stored row names. Returns how many were settled."""
    ts = db.now() if now is None else now
    pending = await db.run(_gather)
    # Higher-ranked keys first: a tmdb title registered before the tvdb listing
    # that names it means the listing joins it rather than being merged into it
    # afterwards — the same end, with fewer rows moved twice.
    ordered = sorted(pending.items(), key=lambda item: (identity.rank(item[0]), item[0]))
    for start in range(0, len(ordered), BATCH):
        batch = ordered[start:start + BATCH]

        def _work(conn, batch=batch):
            for filed_under, (media, ids) in batch:
                identity.settle(conn, media, ids, filed_under=filed_under, now=ts)

        await db.transaction(_work)
    logger.info("title store: registered %d title(s) already held", len(ordered))
    return len(ordered)


def _gather(conn: db.Connection) -> dict[str, tuple[str, dict]]:
    """{key a row holds: (media, every id the rows holding it state)}."""
    out: dict[str, tuple[str, dict]] = {}

    def add(key: str, media: str, ids: dict) -> None:
        held = out.setdefault(str(key), (str(media), {}))[1]
        for name, value in ids.items():
            if value not in (None, ""):
                held.setdefault(name, value)

    # Each service's own group, in the declared order, so the first writer of an
    # id is the same service every time.
    title_rows = conn.execute(rows.title_groups_sql(("ids_json",))).fetchall()
    for row in sorted(title_rows, key=lambda r: rows.SOURCE_ORDER.get(str(r["source"]), 99)):
        add(row["title_key"], row["media"], _loads(row["ids_json"]))
    for table in _TRACKER_TABLES:
        columns = ", ".join(_TRACKER_IDS)
        for row in conn.execute(f"SELECT media, match_source, match_id, {columns} FROM {table}"):
            add(_key(row), row["media"],
                {name: row[column] for column, name in _TRACKER_IDS.items()})
    for row in conn.execute("SELECT media, match_source, match_id, tmdb, ids_json "
                            "FROM tier_items"):
        add(_key(row), row["media"], {**_loads(row["ids_json"]), "tmdb": row["tmdb"]})
    for table in ("distrakt_movie_watches", "distrakt_show_progress"):
        for row in conn.execute(f"SELECT media, match_source, match_id, trakt_id, simkl_id "
                                f"FROM {table}"):
            add(_key(row), row["media"], {"trakt": row["trakt_id"], "simkl": row["simkl_id"]})
    # A frozen month's films, each by the key and ids it froze with. An entry in
    # the shape written before films had keys names none, and is keyed later by
    # app/distrakt/film_snapshots.py.
    for (document,) in conn.execute("SELECT movies_json FROM distrakt_months "
                                    "WHERE json_valid(movies_json)"):
        films = json.loads(document)
        for film in films if isinstance(films, list) else ():
            key = film.get("key") if isinstance(film, dict) else None
            if isinstance(key, str) and key.count(":") == 2:
                ids = film.get("ids")
                add(key, key.split(":", 1)[0], ids if isinstance(ids, dict) else {})
    for row in conn.execute("SELECT media, match_source, match_id, ids_json "
                            "FROM distrakt_open_prompts"):
        add(_key(row), row["media"], _loads(row["ids_json"]))
    for row in conn.execute("SELECT DISTINCT match_source, match_id FROM watchlist_writes"):
        add(f"show:{row['match_source']}:{row['match_id']}", "show", {})
    return out


def _key(row) -> str:
    return f"{row['media']}:{row['match_source']}:{row['match_id']}"


def _loads(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
