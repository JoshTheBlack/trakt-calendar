"""What a feature's row is CALLED and COUNTS, read from the title store in the
same query that reads the row.

A TRACKER ROW AND A BOARD ROW KEEP NO WORDS OF THEIR OWN. Each used to carry a
copy of its title's name, network, year and counts, filed on the day it arrived
and repaired by passes that went looking for what it lacked; the copies drifted
from the store that had the answer all along. They are drawn from the store now,
and these are the columns a feature's own SELECT adds to do it — one query, the
row and its words together, so no reader can be written that has the row and
not the words.

WHICH SERVICE'S WORDS. Every service that describes the title has a group on its
row, and they are read in the declared order (`Source`: TMDB, Trakt, Simkl), each
field from the first service that states one. With ONE refinement, which the
tracker's own arrival has always made: THE SERVICE THAT HOLDS THE ROW'S SEASON
NAMES IT. The ids on a title join two catalogues' shows whose seasons need not
line up — Simkl's anthology "Monster" carries the ids of Trakt's one-season
"DAHMER – Monster" — so the season a row is about may be described only by a
later service, and it is that service's name for the title the row is drawn
with (app/providers/detail_source.py's `for_season` is the same rule for the
panel).

EMPTY IS NOT A FALLBACK TO ANYTHING. A title no service has described yet draws
no name until one has — and every surface that draws one queues exactly that
description (app/metadata/refresh.py) — because a stale copy kept beside the
store is what this module replaced.

THE FRAGMENTS ARE BUILT FROM CONSTANTS — the table names, the declared order,
the alias a caller names its own table by — never from input, which is what
makes building SQL here safe.
"""
from __future__ import annotations

from . import groups, tables

# A text column with something in it, as SQL.
_NOT_EMPTY = "{} != ''"
_NOT_NULL = "{} IS NOT NULL"


def _key(alias: str) -> str:
    """The title key a row holding (media, match_source, match_id) is filed by —
    the shared waterfall's flat form, what `titles.title_key` holds."""
    return f"{alias}.media || ':' || {alias}.match_source || ':' || {alias}.match_id"


def _first(field: str, test: str, *, holds_season: str | None = None) -> str:
    """CASE over a title row aliased `t`: `field` from the first service, in
    the declared order, whose group has it and states it (`test`) — and, where
    `holds_season` names a season row aliased `s`, whose group holds that
    season. NULL where none does."""
    arms = []
    for source in groups.sources():
        if not groups.has(tables.TITLES, source, field):
            continue
        column = f"t.{source}_{field}"
        condition = f"t.{source}_id IS NOT NULL AND {test.format(column)}"
        if holds_season is not None:
            if not (groups.has(tables.SEASONS, source, "episode_count")
                    and groups.has(tables.SEASONS, source, "air_dates_json")):
                continue
            # A season the service HOLDS: described, and listing at least one
            # episode (app/metadata/descriptions.py's season_described).
            condition += (f" AND s.{source}_episode_count > 0 AND "
                          f"json_array_length(s.{source}_air_dates_json) = s.{source}_episode_count")
        arms.append(f"WHEN {condition} THEN {column}")
    return f"CASE {' '.join(arms)} END" if arms else "NULL"


def _text(field: str, key: str, season: str | None) -> str:
    general = (f"(SELECT {_first(field, _NOT_EMPTY)} FROM {tables.TITLES} t "
               f"WHERE t.title_key = {key})")
    if season is None:
        return f"COALESCE({general}, '')"
    holder = (f"(SELECT {_first(field, _NOT_EMPTY, holds_season='s')} FROM {tables.TITLES} t "
              f"JOIN {tables.SEASONS} s ON s.title_key = t.title_key AND s.season = {season} "
              f"WHERE t.title_key = {key})")
    return f"COALESCE({holder}, {general}, '')"


def tracker_columns(alias: str) -> str:
    """`label_title` and `label_network` for a tracker row aliased `alias` — what
    the row is called and who carries it, by the rules in the module docstring."""
    key, season = _key(alias), f"{alias}.season"
    return (f"{_text('title', key, season)} AS label_title, "
            f"{_text('network', key, season)} AS label_network")


def _season_first(field: str, test: str) -> str:
    """CASE over a season row aliased `s` (and its title's row `t`): `field`
    from the first service, in the declared order, whose season group states
    it."""
    arms = [f"WHEN s.{source}_fetched_at IS NOT NULL AND {test.format(f's.{source}_{field}')} "
            f"THEN s.{source}_{field}"
            for source in groups.sources() if groups.has(tables.SEASONS, source, field)]
    return f"CASE {' '.join(arms)} END" if arms else "NULL"


def board_columns(alias: str) -> str:
    """Everything a board row draws about its title, for a row aliased `alias`:
    `label_title`, `label_network`, `label_year`, `label_runtime`,
    `label_season_count`, and — for a row that is one season — that season's
    own `label_season_name` and `label_episode_count`; for a whole show, its
    episode count is every numbered season's, from the first service whose
    description vouches for its season list (`listing_service` below).

    A ROW'S COUNTS DESCRIBE THE ROW. A season row counts its own episodes and no
    seasons (the board's reader drops `label_season_count` for it); the whole
    show counts its numbered seasons and their episodes — specials are not a
    season of the count. Whatever nothing states is NULL, never the other shape's
    number: a season row that showed the show's "5S 38E" beside "Season 3" read
    as the season having 38 episodes."""
    key, season = _key(alias), f"{alias}.season"
    title_row = f"FROM {tables.TITLES} t WHERE t.title_key = {key}"
    season_row = f"FROM {tables.SEASONS} s WHERE s.title_key = {key} AND s.season = {season}"
    # THE SERVICE WHOSE SEASON GROUPS ARE A WHOLE LIST: the first, in the declared
    # order, whose description states how many seasons the title has and whose
    # stored seasons number exactly that (descriptions.listed_seasons says why
    # the rows alone are not a list) — its numbered seasons' episodes summed.
    arms = []
    for source in groups.sources():
        if not (groups.has(tables.TITLES, source, "season_count")
                and groups.has(tables.SEASONS, source, "episode_count")):
            continue
        listed = (f"FROM {tables.SEASONS} s WHERE s.title_key = t.title_key AND s.season > 0 "
                  f"AND s.{source}_episode_count > 0")
        arms.append(f"WHEN t.{source}_id IS NOT NULL AND t.{source}_season_count IS NOT NULL "
                    f"AND t.{source}_season_count = (SELECT COUNT(*) {listed}) "
                    f"THEN (SELECT SUM(s.{source}_episode_count) {listed})")
    whole_show_episodes = (f"(SELECT CASE {' '.join(arms)} END {title_row})" if arms else "NULL")
    return ", ".join((
        f"{_text('title', key, season)} AS label_title",
        f"{_text('network', key, season)} AS label_network",
        f"(SELECT {_first('year', _NOT_EMPTY)} {title_row}) AS label_year",
        f"(SELECT {_first('runtime', _NOT_NULL)} {title_row}) AS label_runtime",
        f"(SELECT {_first('season_count', _NOT_NULL)} {title_row}) AS label_season_count",
        f"CASE WHEN {season} IS NULL THEN NULL ELSE "
        f"(SELECT {_season_first('name', _NOT_EMPTY)} {season_row}) END AS label_season_name",
        f"CASE WHEN {season} IS NULL THEN {whole_show_episodes} ELSE "
        f"(SELECT {_season_first('episode_count', _NOT_NULL)} {season_row}) END "
        "AS label_episode_count",
    ))
