"""Simkl's answer to the catalogue search port (app/providers/base.py's
SearchPort): free-text search over Simkl's own title catalogues.

TWO CATALOGUES, SEARCHED APART. Simkl keeps anime as a separate catalogue with
its own endpoint, and one query does not reach both: measured live 2026-08-12
and again 2026-10-23, GET /search/tv answers "frieren" and "kaiju no 8" with
nothing and GET /search/anime with 3 and 5 — and for "one piece" the TV
endpoint's 3 are the live-action series and its spin-offs while anime holds the
show. So the port offers two catalogues a box searches separately, each one
page of SEARCH_PAGE_SIZE per ask: "simkl" (/search/tv for shows, /search/movie
for films) and "simkl-anime" (/search/anime, holding both — see
_FILM_SEARCH_TYPE). It used to fan a show query out over both endpoints and walk
four pages of each behind one ask, which gave Simkl a different page size from
every other service a box asks and no way to ask for more of just one of them.

THESE ARE GETs, measured live 2026-08-12 against /search/tv, /search/anime and
/search/movie: all three answered 200 with results, both as a GET and
(harmlessly) as a POST, but Simkl's own docs describe free-text search as
`GET /search/{type}` and reserve the POSTs in this area for `/search/file`
and `/search/random`, which this app has no use for. So this rides
`cached_get` like every other catalogue read in this package: CATALOG_POOL,
the 10 GET/second pool, and URL-cacheable.

WHAT A SEARCH HIT DOES NOT CARRY, MEASURED AGAINST A LIVE RESPONSE: no
network, no runtime, no overview, and no season — a hit's `ids` block holds
at most `simkl_id`, `slug` and `tmdb`, never `tvdb`, `imdb` or `mal`. A
season-title's own season number lives on the PER-TITLE record
(`/tv/{id}`'s `season`/`mapped_tvdb_seasons`), one lookup deeper than search
goes, which is why `_hit` below leaves `SearchHit.season` at None.

AND THE SEASON IS FILLED IN AFTERWARDS, BUT ONLY WHERE IT SETTLES SOMETHING.
Simkl files each anime season as its own catalogue title carrying the parent
series' tmdb id, so one search can answer with four titles that share one
identity and nothing to tell them apart. `_name_the_seasons_that_collide`
pays the per-title lookup for exactly those and no others — see its own
docstring for why that bound is what makes the cost defensible, and why this
service owes its caller an answer no other service has to give.
"""
from __future__ import annotations

import asyncio
import logging

from ...config import Settings
from ..base import SEARCH_PAGE_SIZE, Media, SearchHit, SearchPage, Source, resolve_key
from . import _ids, _naming, transport
from . import calendar as simkl_calendar

logger = logging.getLogger(__name__)

# Which endpoint answers each catalogue for each medium — see the module
# docstring for why anime is its own catalogue.
CATALOGUE = "simkl"
ANIME_CATALOGUE = "simkl-anime"
_PATHS = {
    (CATALOGUE, Media.SHOW): "search/tv",
    (CATALOGUE, Media.MOVIE): "search/movie",
    (ANIME_CATALOGUE, Media.SHOW): "search/anime",
    (ANIME_CATALOGUE, Media.MOVIE): "search/anime",
}


# `GET /search/anime` ANSWERS WITH FILMS AS WELL AS TV, and nothing else in the
# payload says so except this field — measured live 2026-09-27 against the
# SEARCH RESULT ITSELF (not the per-title record, which spells the same fact
# `anime_type` instead — a distinct field this app first read here by mistake,
# confirmed live when a real search still misrouted the fixed title): a query
# for "attack on titan" against /search/anime returns entries with
# `type: "tv"`, `"movie"`, `"special"` and `"ova"` all in the one response —
# "Shingeki no Kyojin: The Last Attack" (2024, a recap film) answers with
# `type: "movie"` beside its parent series' `type: "tv"` seasons.
# `/search/tv`'s own entries carry no `type` field at all (every one of them
# reads None), so `.get` never fires outside the anime endpoint.
_FILM_SEARCH_TYPE = "movie"


def _hit(entry: dict, media: Media) -> SearchHit:
    """One Simkl search result as a SearchHit. `network`, `runtime` and
    `overview` are left at their SearchHit default (see module docstring for
    why) so a caller merging this against another source's answer can fill
    the gap from whichever source did say.

    `media` IS THE ENTRY'S OWN, NOT ALWAYS THE CALLER'S. A film hit answered
    by `/search/anime` (see `_FILM_SEARCH_TYPE`) is tagged Media.MOVIE
    regardless of the media a show search asked for — this is the one field on
    SearchHit built to carry exactly that (app/providers/title_search.py's merge keys
    on it, `resolve_key(hit.media, hit.ids)`, precisely so a film mixed into a
    show search resolves under the film's own identity rather than a show's).
    `type: "special"` is deliberately NOT redirected here: the per-title
    record for a special can still carry `mapped_tvdb_seasons` and a real
    episode count (Attack on Titan's own 2023 special does), which is the
    season-picker's ordinary case, not a film's — only `"movie"` is.
    """
    raw_ids = entry.get("ids") or {}
    hit_media = Media.MOVIE if entry.get("type") == _FILM_SEARCH_TYPE else media
    return SearchHit(
        source=Source.SIMKL,
        source_id=str(raw_ids.get("simkl_id") or raw_ids.get("simkl") or ""),
        media=hit_media,
        ids=_ids.normalize(raw_ids),
        title=str(entry.get("title") or ""),
        year=entry.get("year"),
        season=None,
        network="",
        runtime=None,
        overview="",
        # The entry's poster PATH made an address by the same function the
        # calendar's entries use, so a search row and a card for one title
        # point at one picture.
        poster=simkl_calendar.poster_url(entry.get("poster")) or "",
    )


async def _search_one(settings: Settings, path: str, query: str, media: Media,
                      page: int, *, cache_only: bool = False) -> tuple[list[SearchHit], bool]:
    """One page of one endpoint, and whether Simkl holds a later one (its own
    page count, transport.cached_page_get). Raises transport.SimklError on a
    failure. Asked with the viewer's own token, which Simkl's v2 search requires."""
    entries, more = await transport.cached_page_get(
        transport.catalog_client(), settings, path,
        {"q": query, "limit": str(SEARCH_PAGE_SIZE)}, page,
        pool=transport.CATALOG_POOL, raise_errors=True, cache_only=cache_only,
    )
    return [_hit(entry, media) for entry in entries], more


async def _name_the_seasons_that_collide(settings: Settings, hits: list[SearchHit], *,
                                         cache_only: bool = False) -> list[SearchHit]:
    """`hits` with `season` filled in on any of them that this ONE answer names
    more than once for the same title.

    WHY THIS SOURCE OWES ITS CALLER A SEASON AND THE OTHER ONE DOES NOT. Simkl
    files each anime season as its own catalogue title carrying the parent
    series' tmdb id, so a single search for "beastars" answers with four titles
    that all resolve to `show:tmdb:90937`. To anything downstream those are four
    rows with one identity and nothing to tell them apart — the merge cannot
    dedupe them (it would collapse four real results into one) and cannot NOT
    dedupe them (two services naming one title must still merge). The fact that
    settles it is which season each one is, and only this service knows: it is
    one lookup deeper than search goes, on the per-title record `_naming` reads.
    Answering it here rather than downstream is the same rule the rest of this
    package follows — how Simkl spells a season is Simkl's business, and the
    caller gets `SearchHit.season` filled in, which is what that field is for.

    ONLY THE COLLIDING HITS ARE LOOKED UP, and that bound is the whole cost
    argument. A hit no other hit shares a key with is already distinguishable
    and is left alone, so an ordinary search — every film, every live-action
    show, any anime query returning one title per series — makes no extra call
    at all. Measured 2026-08-18: "beastars" costs 4, "frieren" 3, "attack on
    titan" 3, and every non-anime query 0, each cached for a day
    (`_naming.CACHE_TTL_SECONDS`) so a repeated search costs nothing. This is
    deliberately not the eager resolution of a whole result list that was
    rejected earlier: that would spend a lookup on every row to answer a
    question most rows never get asked, while this spends one only where the
    answer is already needed to tell two results apart.

    A LOOKUP THAT FAILS OR SAYS NOTHING LEAVES ITS HIT AT None. The merge's own
    rule for hits it cannot tell apart — keep them as separate rows — is the
    right fallback and needs no help from here, so a Simkl hiccup costs a row
    its season label rather than costing the search its results.
    """
    by_key: dict[str, list[int]] = {}
    for index, hit in enumerate(hits):
        key = resolve_key(hit.media, hit.ids)
        if key is not None:
            by_key.setdefault(str(key), []).append(index)
    colliding = [index for indexes in by_key.values() if len(indexes) > 1
                 for index in indexes]
    if not colliding:
        return hits
    namings = await asyncio.gather(
        *(_naming.fetch(settings, hits[index].source_id, cache_only=cache_only)
          for index in colliding),
        return_exceptions=True,
    )
    named = list(hits)
    for index, naming in zip(colliding, namings):
        if isinstance(naming, BaseException):
            logger.warning("Simkl could not say which season %r (simkl %s) is: %s",
                           hits[index].title, hits[index].source_id, naming)
            continue
        if naming.season is not None:
            named[index] = hits[index]._replace(season=naming.season)
    return named


async def search_titles(settings: Settings, media: Media, query: str, *,
                        page: int = 1, catalogue: str = CATALOGUE,
                        cached_only: bool = False) -> SearchPage:
    """app/providers/base.py's SearchPort, for one of Simkl's two catalogues:
    page `page` of SEARCH_PAGE_SIZE. Empty query answers an empty page without a
    call, the same rule every service keeps.

    `cached_only=True` makes no call at all and reads back what an identical
    search stored — see SearchPort for what names it. The season-naming pass
    below inherits it, so a cached-only read cannot reach the network by the
    back door of resolving a collision.

    A FAILURE RAISES, never reading as "no matches": SearchPort's own contract
    (see base.py) is that a caller reading several sources must tell "found
    nothing" apart from "could not be asked".
    """
    q = (query or "").strip()
    if not q:
        return SearchPage([])
    media = Media(media)
    try:
        path = _PATHS[(catalogue, media)]
    except KeyError:
        raise ValueError(f"Simkl has no catalogue {catalogue!r} for {media}.") from None
    # WHAT A HIT IS: the medium asked for, except in the anime catalogue, where
    # the entry's own `type` says — so there everything not typed a film is a show
    # (_hit reads the film type), whichever medium the search was for.
    hits, more = await _search_one(settings, path, q,
                                   Media.SHOW if catalogue == ANIME_CATALOGUE else media,
                                   page, cache_only=cached_only)
    # THE ANIME CATALOGUE ANSWERS BOTH MEDIA AT ONE ENDPOINT, and nothing else in
    # the payload says which a hit is but its `type` (see _FILM_SEARCH_TYPE) — so
    # each search keeps only the medium it was asked for. A film hit has nowhere
    # useful to go from a show search (no season to pick, no watched-on date to
    # record one with) and the reverse holds for a film search.
    hits = [hit for hit in hits if hit.media is media]
    # SHOWS ONLY, past this point. A film has no season to name, and Simkl's
    # film catalogue does not follow the season-title convention its anime
    # catalogue does — measured, 41 film hits across six queries and not one
    # of them a season of something else. So a movie query has nothing to
    # resolve, and the lookup this would make reads `/tv/{id}`, which is the
    # wrong question to ask about a film id even when it is cheap.
    if media is Media.MOVIE:
        return SearchPage(hits, more)
    return SearchPage(await _name_the_seasons_that_collide(settings, hits,
                                                           cache_only=cached_only), more)
