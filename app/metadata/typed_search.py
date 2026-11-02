"""The titles this instance already stores, as a search answers them.

WHAT "STORED" MEANS. A title is here because some service's listing or lookup
reached the store — whoever's calendar month, whichever feature's add. So this
answers "does this app already know a title by that name", which is what a person
typing into an ADD box is asking, and NOT "where on my calendar is it": there are
no viewer filters here, no source selection and no calendar endpoint.
app/calendar/search.py asks the other question, through the calendar's read path,
because it promises a card.

NO NETWORK AND NO BUDGET. Two reads over one table, which is what lets it run on
every keystroke — measured on a copy of a real instance (21,247 stored titles),
2-3.5ms for a query naming a title and ~21ms for two letters that match thousands.
"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from ..providers import title_search
from ..providers.base import ItemKey, Media, SearchHit, Source, parse_item_key
from . import groups, identity, rows, tables

# THE SHORTEST QUERY WORTH ASKING ABOUT, measured in FOLDED characters. One
# character matches most of the stored titles and answers nothing anybody
# wanted; two is where a substring starts to mean something. Here rather than in
# any one search so the calendar's search and the stored-title search a tracker
# or a board asks cannot disagree about it.
MIN_QUERY = 2


def like_escaped(folded: str) -> str:
    """A folded string with LIKE's own wildcards neutralised — see `like_needle`
    for why a search box needs that. The backslash is escaped first so it cannot
    smuggle the escape character itself."""
    for char in ("\\", "%", "_"):
        folded = folded.replace(char, "\\" + char)
    return folded


def like_needle(query: str) -> str:
    """`query` folded and wrapped for a LIKE, with the wildcards it may contain
    itself neutralised.

    A SEARCH BOX IS UNTRUSTED INPUT AND `%` IS A CHARACTER IN TITLES. Left
    unescaped, a query of `%` matches the whole table and one of `100%` silently
    matches far more than it should; both are ordinary things to type rather
    than attacks — this instance holds `100% Footy` and `The 1% Club (US)`.
    """
    return f"%{like_escaped(rows.fold_title(query))}%"


def too_short(query: str) -> bool:
    """Whether a typed query is still too short to list anything for."""
    return len(rows.fold_title(query)) < MIN_QUERY


# WHICH STORED TITLES A QUERY MATCHES, whatever calendar they sit on. Grouped by
# `title_key` because a TITLE is the answer — a search for a show to add is not
# a search for its airings — and ranked so the answer reads as a person expects:
# a title that STARTS with what was typed before one that merely contains it,
# then the shorter title (the show itself before its spin-offs), then a stable
# tie-break so the same query lists the same titles in the same order.
#
# THE MATCH IS FOUND IN THE INDEX, THEN THE ROWS ARE READ, and the subquery is
# what makes the planner do it in that order. A title row is wide — every
# service's overview, every id map — so filtering it directly scans the table:
# measured on a copy of a real instance (21,247 rows) at ~72ms a keystroke
# whatever was typed. Each service's fold index holds only its folded title and
# the rowid, so scanning THEM for the substring is a few ms, and only the
# matches are then looked up — one arm per service, each in its own index.
def _titles_matching_sql(media: Media) -> str:
    arms = " UNION ALL ".join(
        f"SELECT title_key, {source}_title_fold AS title_fold FROM {tables.TITLES} "
        f"WHERE rowid IN (SELECT rowid FROM {tables.TITLES} "
        rf"WHERE {source}_title_fold LIKE :contains ESCAPE '\') "
        f"AND media = :media {_keep_film_rows(media, source)}"
        for source in groups.sources())
    return (rf"SELECT title_key, MIN(CASE WHEN title_fold LIKE :starts ESCAPE '\' THEN 0 "
            "ELSE 1 END) AS starts, MIN(length(title_fold)) AS span "
            f"FROM ({arms}) GROUP BY title_key ORDER BY starts, span, title_key LIMIT :limit")


# SIMKL FILES SOME FILMS ON ITS ANIME SERIES CALENDAR, and `anime_type` is the
# only thing that says so (see the column's own comment in app/db.py). A show
# search never offers one: the same rule app/providers/simkl/search.py applies
# to a service's answer — asked for shows, get shows — because a film found
# from a show box has no season to pick and no watched-on day to be filed with.
def _keep_film_rows(media: Media, source: str) -> str:
    if media is not Media.SHOW or not groups.has(tables.TITLES, source, "anime_type"):
        return ""
    return f"AND {source}_anime_type != 'movie'"


# EVERY SERVICE'S GROUP OF THE TITLES NAMED, not just the groups whose spelling
# matched: two services may spell one title differently, and a title is
# described by all of its services or it reads differently here from how the
# calendar draws it.
def _title_rows_sql(media: Media) -> str:
    arms = " UNION ALL ".join(
        f"SELECT '{source}' AS source, {source}_id AS source_id, title_key, "
        + groups.select(tables.TITLES, source, ("title", "ids_json", "year", "network", "runtime",
                                                "overview", "poster"))
        + f" FROM {tables.TITLES} WHERE media = :media "
        "AND title_key IN (SELECT value FROM json_each(:keys)) "
        f"AND {groups.present(tables.TITLES, source)} {_keep_film_rows(media, source)}"
        for source in groups.sources())
    return f"SELECT * FROM ({arms}) ORDER BY title_key"


def _stored_hit(title_key: str, found, media: Media) -> title_search.MergedSearchHit:
    """One stored title's rows as the hit a service search would have answered
    with — one row per service, registry order leading, folded by the service
    merge's own rules (title_search.fold_one_title) so the two halves of a
    search cannot describe one title two ways.

    THE SERVICE'S OWN ID IS WHAT A PICK CALLS BACK WITH, read from `ids_json`
    under the name the service answers to and falling back to `source_id` — a
    Simkl row written before its storage key became the numeric id is filed
    under a slug the per-title lookup is not addressed by.
    """
    first_per_source: dict[str, Any] = {}
    for row in found:
        first_per_source.setdefault(str(row["source"]), row)
    hits = []
    for name in sorted(first_per_source, key=lambda s: rows.SOURCE_ORDER.get(s, 99)):
        row = first_per_source[name]
        ids = json.loads(row["ids_json"] or "{}") if row["ids_json"] else {}
        year = rows.as_year(row["year"])
        hits.append(SearchHit(
            source=Source(name),
            source_id=str(ids.get(rows.ID_NAMESPACE.get(name, name)) or row["source_id"]),
            media=media, ids=ids, title=str(row["title"] or ""),
            year=year if isinstance(year, int) else None, season=None,
            network=str(row["network"] or ""), runtime=row["runtime"],
            overview=str(row["overview"] or ""),
            # The service's own address, as its listing stored it — so a typed row
            # shows the picture the calendar draws, for nothing.
            poster=str(row["poster"] or "")))
    return title_search.fold_one_title(hits, _item_key_of(title_key), title_key=title_key)


def _item_key_of(title_key: str) -> ItemKey | None:
    """The shared identity a stored title is filed under, or None for a title only
    one service names (identity.per_source_key)."""
    try:
        return parse_item_key(title_key)
    except ValueError:
        return None


async def _hits_for(title_keys: list[str], media: Media) -> list[title_search.MergedSearchHit]:
    """`title_keys`' stored rows as hits, in the order the keys were given."""
    if not title_keys:
        return []
    found = await db.fetch_all(_title_rows_sql(media), {"media": str(media),
                                                        "keys": json.dumps(title_keys)})
    by_key: dict[str, list] = {}
    for row in found:
        by_key.setdefault(str(row["title_key"]), []).append(row)
    return [_stored_hit(key, by_key[key], media) for key in title_keys if key in by_key]


async def titles_matching(query: str, media: Media, limit: int) -> list[title_search.MergedSearchHit]:
    """Every stored title of `media` whose name matches `query`, ONE HIT PER
    TITLE, as the same hit type a service search answers with.

    ONE HIT PER TITLE, whoever stored it. A title Trakt and Simkl both list is
    one hit carrying both services' marks and the union of their ids — the
    service merge's own rule for two services answering one query.

    Shorter than `MIN_QUERY` answers nothing, the same rule the calendar's own
    search keeps, because it is the same rule.
    """
    folded = rows.fold_title(query)
    if len(folded) < MIN_QUERY:
        return []
    escaped = like_escaped(folded)
    found = await db.fetch_all(_titles_matching_sql(media), {
        "starts": f"{escaped}%", "contains": f"%{escaped}%", "media": str(media),
        "limit": int(limit)})
    return await _hits_for([str(row["title_key"]) for row in found], media)


async def stored_title(title_key: str, media: Media) -> title_search.MergedSearchHit | None:
    """The one stored title `title_key` names, as `titles_matching` would have
    listed it, or None when nothing of `media` is stored under it.

    THIS IS HOW A TYPED PICK IS RESOLVED, and it is the whole of what makes one
    safe to add from. The page names a title by the identity it was listed
    under and nothing else; the title, the ids and the network are read back
    out of the rows the services wrote. A key this instance does not hold
    answers None, never a title built from what the request said. A key the
    title has been re-keyed away from since the list was drawn is followed to
    the key it answers to now.
    """
    if not title_key:
        return None
    current = (await identity.canonical_keys([str(title_key)])).get(str(title_key), str(title_key))
    hits = await _hits_for([current], media)
    return hits[0] if hits else None
