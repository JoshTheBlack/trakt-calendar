"""Each service's COLUMN GROUP in the title store's rows, spelled once.

A title, a season and an episode are each one row (app/metadata/tables.py), and
what each service says about it sits in that row as a group of columns named
`{source}_{column}`. WHICH COLUMNS A SERVICE HAS IS ITS OWN PACKAGE'S
DECLARATION (app/providers/base.py's `StoreColumns`) — a service has only the
columns it can state — so nothing here lists a service by name: a column is
named for a service only when the service declares it, and a reader asking a
service for a column it does not have is handed that column's empty value
(`EMPTY`), the value a group with nothing to say holds. That is not another
service's word standing in: it is the same "said nothing" the service's own
empty column would read as.

THE FRAGMENTS ARE BUILT FROM CONSTANTS — the registry's names and declarations,
the table names, the aliases a caller passes — never from input.
"""
from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .. import providers
from ..providers.base import Source
from . import tables

# WHAT EACH COLUMN HOLDS WHERE NOTHING WAS SAID — the default its declaration
# gives it in every service's group, and what a reader is handed for a column
# the service's group does not have. One name means one thing in all three
# tables, so one map serves them; tests/metadata/test_groups.py holds it to the
# schema's own defaults.
EMPTY: dict[str, Any] = {
    # a title's group
    "id": None, "ids_json": "{}", "detail_url": "", "title": "", "title_fold": "",
    "year": "", "network": "", "country": "", "language": "", "certification": "",
    "status": "", "overview": "", "poster": "", "runtime": None, "genres_json": "[]",
    "ratings_json": "{}", "first_aired": "", "season_count": None, "videos_json": "[]",
    "release_types_json": "{}", "enriched": 0, "fetched_at": None, "stale_after": 0,
    "failed_at": None, "fail_count": 0, "cast_json": "[]", "homepage": "",
    "keywords_json": "[]", "show_type": "", "facts_known": 0, "logo": "",
    "collection_json": "{}", "next_episode_json": "{}", "popularity": None,
    "anime_type": "",
    # a season's
    "episode_count": None, "premiere": "", "air_dates_json": "[]", "name": "", "named": 0,
    # an episode's listing and description
    "air_ts": None, "date_only": 0, "calendars": 0, "searched": 0, "unstated": 0,
    "label": "", "listed_at": None, "repeats_json": "[]", "entry": None,
    "episode_type": "", "rating": None, "votes": None,
}

# THE WORDS A GROUP HOLDS: what a service said about a title, a season or an
# episode, as opposed to what files the group (its own id, the ids it stated,
# its page's address, its folded title) and the clocks and bookkeeping it is kept
# by (`NOT_WORDS`) — an episode's listing included, whose own clock is
# `listed_at`. A service's storage ceiling is about every word
# (app/metadata/rows.py's `ceiling_clock`), and tests/metadata/test_retention.py
# fails on a column of the three tables that neither these nor `IDENTITY` name,
# because a word nobody listed is a word the ceiling cannot see left behind.
WORDS: dict[str, tuple[str, ...]] = {
    tables.TITLES: ("title", "year", "network", "country", "language", "certification",
                    "status", "overview", "poster", "runtime", "genres_json", "ratings_json",
                    "anime_type", "release_types_json", "keywords_json", "first_aired",
                    "homepage", "season_count", "cast_json", "videos_json", "logo",
                    "collection_json", "next_episode_json", "show_type", "popularity"),
    tables.SEASONS: ("name", "overview", "episode_count", "premiere", "air_dates_json",
                     "poster", "cast_json", "videos_json"),
    tables.EPISODES: ("title", "overview", "first_aired", "episode_type", "runtime", "rating",
                      "votes"),
}
NOT_WORDS: dict[str, tuple[str, ...]] = {
    # `facts_known` says whether two of the words are an answer (rows.facts_values);
    # it is not itself something the service said.
    tables.TITLES: ("id", "ids_json", "detail_url", "title_fold", "enriched", "fetched_at",
                    "stale_after", "failed_at", "fail_count", "facts_known"),
    tables.SEASONS: ("fetched_at", "stale_after", "failed_at", "fail_count", "id", "named"),
    tables.EPISODES: ("fetched_at", "stale_after", "air_ts", "date_only", "calendars",
                      "searched", "unstated", "label", "listed_at", "repeats_json", "entry"),
}
# THE COLUMNS THAT SAY WHICH ROW IT IS, and belong to no service.
IDENTITY: dict[str, tuple[str, ...]] = {
    tables.TITLES: ("title_key", "media", "ids_json", "created_at", "last_viewed_at",
                    "refreshed_at"),
    tables.SEASONS: ("title_key", "season"),
    tables.EPISODES: ("title_key", "unit", "media", "season", "number"),
}
# AN EPISODE ROW'S LISTING COLUMNS, as one service's group holds them.
LISTING = ("air_ts", "date_only", "calendars", "searched", "unstated", "label", "listed_at",
           "repeats_json", "entry")

# THE COLUMN WHOSE VALUE SAYS A SERVICE HAS A GROUP HERE AT ALL: a title's own id
# for the service (NULL where it has none), a season's or an episode
# description's clock (0 for a listing's stub nothing has described), and an
# episode listing's first instant.
PRESENT = {tables.TITLES: "id", tables.SEASONS: "fetched_at", tables.EPISODES: "fetched_at"}
LISTED = "air_ts"


@functools.cache
def sources() -> tuple[str, ...]:
    """Every service with a group, in the declared order — the order a row's
    groups are laid out and visited in: each registered `Source`. Fixed for the
    life of the process, as the schema built from it is."""
    registered = providers.registered()
    return tuple(str(source) for source in Source if source in registered)


@functools.cache
def names(table: str, source: str) -> frozenset[str]:
    """The columns `source`'s group holds in `table`, as its package declares —
    asked of every column a read touches, so answered from the declaration once
    (a package's declaration is fixed for the life of the process, and the
    schema is built from it)."""
    declared = providers.registered()[Source(str(source))].capabilities.store_columns
    return {tables.TITLES: declared.titles, tables.SEASONS: declared.seasons,
            tables.EPISODES: declared.episodes}[table]


def has(table: str, source: str, name: str) -> bool:
    return name in names(table, source)


def literal(value: Any) -> str:
    """A constant as SQL — only ever EMPTY's values, never input."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def column(table: str, source: str, name: str, alias: str = "") -> str:
    """SQL for `source`'s `name` on a row of `table` aliased `alias`: its own
    column, or the column's empty value where the service has none."""
    if not has(table, source, name):
        return literal(EMPTY[name])
    return f"{alias + '.' if alias else ''}{source}_{name}"


def present(table: str, source: str, alias: str = "", *, listed: bool = False) -> str:
    """SQL: `source` has a group on this row (`listed`: a listing, on an episode
    row)."""
    name = LISTED if listed else PRESENT[table]
    if not has(table, source, name):
        return "0"
    return f"{alias + '.' if alias else ''}{source}_{name} IS NOT NULL"


def absent(table: str, source: str, alias: str = "", *, listed: bool = False) -> str:
    """SQL: `source` has no group on this row (`listed`: no listing)."""
    name = LISTED if listed else PRESENT[table]
    if not has(table, source, name):
        return "1"
    return f"{alias + '.' if alias else ''}{source}_{name} IS NULL"


def select(table: str, source: str, wanted: Iterable[str], alias: str = "") -> str:
    """A SELECT list naming each of `wanted` as `source`'s group states it, under
    the column's own name."""
    return ", ".join(f"{column(table, source, name, alias)} AS {name}" for name in wanted)


def of(row: Mapping[str, Any], table: str, source: str, wanted: Iterable[str]) -> dict:
    """{name: value} for each of `wanted`, read off a whole row of `table` as
    `source`'s group states it — EMPTY's value for a column it does not have."""
    return {name: (row[f"{source}_{name}"] if has(table, source, name) else EMPTY[name])
            for name in wanted}


def every(table: str, source: str) -> tuple[str, ...]:
    """`source`'s group in `table`, in a fixed order — what a writer spells a
    whole group as."""
    return tuple(sorted(names(table, source)))


# `held.x` is a group's stored `x` and `given.x` the written one, in a template
# `spelled` turns into a service's own columns — so each writer states its rule
# once, in the column's own name, for whichever service it is writing.
_TEMPLATE = re.compile(r"\b(held|given)\.([a-z_]+)\b")


def spelled(template: str, table: str, source: str, *, given: str = "excluded") -> str:
    """`template` with `held.x` as `table.{source}_x` and `given.x` as
    `{given}.{source}_x`."""
    return _TEMPLATE.sub(
        lambda m: f"{table if m[1] == 'held' else given}.{source}_{m[2]}", template)


def changing(conn, table: str, keys: tuple[str, ...], given_sql: str,
             sets: Iterable[tuple[str, str]], source: str, params: Mapping[str, Any]
             ) -> set[int]:
    """The positions (`at`) of the rows `given_sql` reads — a SELECT naming each
    column as `table` names it, `{source}_x` for a group's — whose upsert by
    `sets` ((column, template) each: the very templates the upsert is spelled
    from) would change the stored row: one not there yet, or one a set would
    leave other than it is. READ ONLY, in one aggregated row: what a writer asks
    before its lock, so a write that would change nothing is never sent.

    A WRITE THAT CHANGES NOTHING STILL HOLDS THE LOCK, and a calendar's refill
    used to write its whole span every time it ran: MEASURED on a copy of dev, a
    week of All Episodes stored back as it was held the lock 0.77–0.82 s alone
    and 1.07–1.31 s beside month reads — the title groups 0.38–0.84 s of it, the
    episode rows 0.19–0.53 s."""
    (found,) = conn.execute(
        f"SELECT json_group_array(at) FROM ({changing_sql(table, keys, given_sql, sets, source)})",
        params).fetchone()
    return set(json.loads(found))


def changing_sql(table: str, keys: tuple[str, ...], given_sql: str,
                 sets: Iterable[tuple[str, str]], source: str) -> str:
    """`changing`'s question as a SELECT of `at`, for a caller asking it of
    several services in one statement."""
    join = " AND ".join(f"held.{key} = given.{key}" for key in keys)
    differs = " OR ".join(
        f"({spelled(template, 'held', source, given='given')}) IS NOT held.{source}_{name}"
        for name, template in sets) or "0"
    return (f"SELECT given.at AS at FROM ({given_sql}) AS given "
            f"LEFT JOIN {table} AS held ON {join} WHERE held.{keys[0]} IS NULL OR {differs}")


def loads(text: Any, default: Any) -> Any:
    """A JSON column's value, or `default` for one that does not parse to the
    same kind of thing."""
    try:
        value = json.loads(text) if text not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default
