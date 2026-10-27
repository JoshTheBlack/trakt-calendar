"""What a feature's row is CALLED and COUNTS, read from the title store in the
same query that reads the row.

A TRACKER ROW AND A BOARD ROW KEEP NO WORDS OF THEIR OWN. Each used to carry a
copy of its title's name, network, year and counts, filed on the day it arrived
and repaired by passes that went looking for what it lacked; the copies drifted
from the store that had the answer all along. They are drawn from the store now,
and these are the columns a feature's own SELECT adds to do it — one query, the
row and its words together, so no reader can be written that has the row and
not the words.

WHICH SERVICE'S WORDS. Every service that describes the title has a row of it,
and they are read in the declared order (`Source`: TMDB, Trakt, Simkl), each
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

from . import rows

# A service's rank in the declared order, as SQL over a title row aliased `t`.
# A row of a service nobody registered (none can be written) sorts last.
_RANK = ("CASE t.source "
         + " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in rows.SOURCE_ORDER.items())
         + f" ELSE {len(rows.SOURCE_ORDER)} END")

# A season row that is described and lists at least one episode — a season the
# service HOLDS (app/metadata/descriptions.py's season_described, with episodes).
_HOLDS_SEASON = ("s.episode_count > 0 "
                 "AND json_array_length(s.air_dates_json) = s.episode_count")


# A text column with something in it, as SQL.
_NOT_EMPTY = "{} != ''"


def _key(alias: str) -> str:
    """The title key a row holding (media, match_source, match_id) is filed by —
    the shared waterfall's flat form, what title_metadata.title_key holds."""
    return f"{alias}.media || ':' || {alias}.match_source || ':' || {alias}.match_id"


def _first(field: str, key: str, *, present: str) -> str:
    """The first service's `field` for the title, in the declared order, among
    the rows where `present` holds."""
    return (f"(SELECT t.{field} FROM {rows.TITLES} t WHERE t.title_key = {key} "
            f"AND {present} ORDER BY {_RANK} LIMIT 1)")


def _from_season_holder(field: str, key: str, season: str, *, present: str) -> str:
    """`field` from the first service, in the declared order, that holds the
    row's season — NULL for a row that names none (a whole show, a film)."""
    return (f"(SELECT t.{field} FROM {rows.TITLES} t JOIN {rows.SEASONS} s "
            "ON s.source = t.source AND s.media = t.media AND s.source_id = t.source_id "
            f"WHERE t.title_key = {key} AND s.season = {season} AND {_HOLDS_SEASON} "
            f"AND {present} ORDER BY {_RANK} LIMIT 1)")


def _text(field: str, key: str, season: str | None) -> str:
    present = _NOT_EMPTY.format(f"t.{field}")
    general = _first(field, key, present=present)
    if season is None:
        return f"COALESCE({general}, '')"
    return f"COALESCE({_from_season_holder(field, key, season, present=present)}, {general}, '')"


def tracker_columns(alias: str) -> str:
    """`label_title` and `label_network` for a tracker row aliased `alias` — what
    the row is called and who carries it, by the rules in the module docstring."""
    key, season = _key(alias), f"{alias}.season"
    return (f"{_text('title', key, season)} AS label_title, "
            f"{_text('network', key, season)} AS label_network")


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
    season_field = (lambda field, present: (
        f"(SELECT s.{field} FROM {rows.TITLES} t JOIN {rows.SEASONS} s "
        "ON s.source = t.source AND s.media = t.media AND s.source_id = t.source_id "
        f"WHERE t.title_key = {key} AND s.season = {season} AND {present} "
        f"ORDER BY {_RANK} LIMIT 1)"))
    # THE SERVICE WHOSE SEASON ROWS ARE A WHOLE LIST: the first, in the declared
    # order, whose description states how many seasons the title has and whose
    # stored seasons number exactly that (descriptions.listed_seasons says why
    # the rows alone are not a list).
    listing_service = (
        f"(SELECT t.source FROM {rows.TITLES} t WHERE t.title_key = {key} "
        "AND t.season_count IS NOT NULL AND t.season_count = ("
        f"  SELECT COUNT(*) FROM {rows.SEASONS} s WHERE s.source = t.source "
        "    AND s.media = t.media AND s.source_id = t.source_id "
        "    AND s.season > 0 AND s.episode_count > 0) "
        f"ORDER BY {_RANK} LIMIT 1)")
    whole_show_episodes = (
        f"(SELECT SUM(s.episode_count) FROM {rows.TITLES} t JOIN {rows.SEASONS} s "
        "ON s.source = t.source AND s.media = t.media AND s.source_id = t.source_id "
        f"WHERE t.title_key = {key} AND s.season > 0 AND s.episode_count > 0 "
        f"AND t.source = {listing_service})")
    return ", ".join((
        f"{_text('title', key, season)} AS label_title",
        f"{_text('network', key, season)} AS label_network",
        f"{_first('year', key, present=_NOT_EMPTY.format('t.year'))} AS label_year",
        f"{_first('runtime', key, present='t.runtime IS NOT NULL')} AS label_runtime",
        f"{_first('season_count', key, present='t.season_count IS NOT NULL')} "
        "AS label_season_count",
        f"CASE WHEN {season} IS NULL THEN NULL ELSE "
        f"{season_field('name', _NOT_EMPTY.format('s.name'))} END AS label_season_name",
        f"CASE WHEN {season} IS NULL THEN {whole_show_episodes} ELSE "
        f"{season_field('episode_count', 's.episode_count IS NOT NULL')} END "
        "AS label_episode_count",
    ))
