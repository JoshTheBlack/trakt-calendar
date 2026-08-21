"""Simkl's answer to the catalogue search port (app/providers/base.py's
SearchPort): free-text search over Simkl's own title catalogue.

FANS OUT TO TWO ENDPOINTS FOR A SHOW QUERY, AND HIDES THAT FROM THE CALLER.
Measured live 2026-08-12: GET /search/tv returns zero anime results — "kaiju
no 8" and "frieren" both answer 0 hits there and 3 each on GET /search/anime
— because Simkl keeps anime as a separate catalogue with its own search
endpoint. The port's contract is one media-parameterized verb returning one
list, so the split is paid here and nowhere else: a caller asking "search
shows" should not have to know Simkl needs two requests to answer that where
Trakt needs one.

THESE ARE GETs, measured live the same day against /search/tv, /search/anime
and /search/movie: all three answered 200 with results, both as a GET and
(harmlessly) as a POST, but Simkl's own docs describe free-text search as
`GET /search/{type}` and reserve the POSTs in this area for `/search/file`
and `/search/random`, which this app has no use for. transport.py's own
`cached_get` docstring used to claim "the search endpoints are POSTs" — that
sentence was about those two, not these three, and has been corrected beside
it. So this rides `cached_get` like every other catalogue read in this
package: CATALOG_POOL, the 10 GET/second pool, and URL-cacheable.

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
from ..base import Media, SearchHit, Source, resolve_key
from . import _ids, _naming, transport

logger = logging.getLogger(__name__)

# Which path(s) answer a media-parameterized search. A show query asks BOTH —
# see the module docstring for why — and a movie query asks the one endpoint
# Simkl publishes for films.
_SHOW_PATHS = ("search/tv", "search/anime")
_MOVIE_PATHS = ("search/movie",)


def _hit(entry: dict, media: Media) -> SearchHit:
    """One Simkl search result as a SearchHit. `network`, `runtime` and
    `overview` are left at their SearchHit default (see module docstring for
    why) so a caller merging this against another source's answer can fill
    the gap from whichever source did say."""
    raw_ids = entry.get("ids") or {}
    return SearchHit(
        source=Source.SIMKL,
        source_id=str(raw_ids.get("simkl_id") or raw_ids.get("simkl") or ""),
        media=media,
        ids=_ids.normalize(raw_ids),
        title=str(entry.get("title") or ""),
        year=entry.get("year"),
        season=None,
        network="",
        runtime=None,
        overview="",
    )


# How many results one page of a search asks for. Simkl serves 10 by default and
# caps this at 50 — asking for the cap makes the common query one request
# instead of several, and `cached_paged_get` walks the rest when there are more.
PAGE_SIZE = 50


async def _search_one(settings: Settings, path: str, query: str, media: Media) -> list[SearchHit]:
    """One endpoint's hits, EVERY page of them. Raises transport.SimklError on
    a failure — caught and weighed by `search_titles`, which is the one place
    that gets to decide whether a failed endpoint fails the whole search.

    THE PAGES ARE ASSEMBLED AND CACHED AS ONE ANSWER by the transport, so
    nothing here or above knows this endpoint paginates at all. Simkl serves
    ten results by default and states the real total in
    `X-Pagination-Page-Count`; taking the first page alone silently truncated
    every query with more matches than that, and somebody searching a common
    word got whichever ten Simkl ranked first with nothing to say the rest
    existed."""
    results = await transport.cached_paged_get(
        transport.catalog_client(), settings, path,
        {"q": query, "limit": str(PAGE_SIZE)},
        pool=transport.CATALOG_POOL, raise_errors=True,
    )
    return [_hit(entry, media) for entry in results]


async def _name_the_seasons_that_collide(settings: Settings,
                                         hits: list[SearchHit]) -> list[SearchHit]:
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
        *(_naming.fetch(settings, hits[index].source_id) for index in colliding),
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


async def search_titles(settings: Settings, media: Media, query: str) -> list[SearchHit]:
    """app/providers/base.py's SearchPort. Empty query returns [] without a
    call, the same rule Trakt's search keeps.

    ONE ENDPOINT FAILING DOES NOT FAIL THE OTHER. A show query is two
    requests under one port call (see module docstring); if /search/anime is
    down and /search/tv still answers, the tv hits are a real, useful answer
    on their own, and losing them to a partner endpoint's failure would be the
    opposite of every other degrade-partially-rather-than-wholesale rule this
    app follows elsewhere.

    EVERY ENDPOINT FAILING IS DIFFERENT AND MUST RAISE. SearchPort's own
    contract (see base.py) is that a failure is not the same answer as "no
    matches" — a caller reading several sources needs to tell the two apart,
    the same distinction every other port in this app draws. Swallowing a
    total failure into [] here would make Simkl being unreachable look
    identical to Simkl genuinely finding nothing, exactly the failure this
    port exists not to produce.
    """
    q = (query or "").strip()
    if not q:
        return []
    media = Media(media)
    paths = _MOVIE_PATHS if media is Media.MOVIE else _SHOW_PATHS
    # ONE AT A TIME, NOT GATHERED, AND THAT IS SIMKL'S RULE RATHER THAN A
    # PREFERENCE. Parallel requests are allowed only against the endpoints served
    # from Cloudflare's edge — the calendar files, `/tv/{id}`, `/anime/{id}` and
    # the episode lists — and search is explicitly not among them. Measured
    # 2026-08-21: `GET /search/tv` answers `cf-cache-status: DYNAMIC`, so every
    # one of these reaches the origin. Simkl names "parallelizing uncached
    # endpoints" as a common reason a client id is suspended, without warning and
    # with no appeal, which is not a risk worth one round trip.
    #
    # THE COST IS ONE EXTRA ROUND TRIP PER SEARCH, because a show query asks two
    # endpoints (Simkl's /search/tv returns no anime) and a film query asks one.
    # A search is a deliberate act by one person, not something a page load
    # spends, so serializing it is the cheap side of this trade.
    hits: list[SearchHit] = []
    failures: list[BaseException] = []
    for path in paths:
        try:
            hits.extend(await _search_one(settings, path, q, media))
        except Exception as exc:  # noqa: BLE001 — weighed below, per endpoint
            failures.append(exc)
    if failures and len(failures) == len(paths):
        # Nothing answered at all — raise rather than reading as an honest
        # empty result. The first failure is as good as any to surface: they
        # are independent endpoints failing independently, not one cause with
        # several symptoms.
        raise failures[0]
    for exc in failures:
        logger.warning("Simkl search %s(%r) failed: %s", "/".join(paths), q, exc)
    # SHOWS ONLY. A film has no season to name, and Simkl's film catalogue does
    # not follow the season-title convention its anime catalogue does —
    # measured, 41 film hits across six queries and not one of them a season of
    # something else. So a movie query has nothing to resolve, and the lookup
    # this would make reads `/tv/{id}`, which is the wrong question to ask about
    # a film id even when it is cheap.
    if media is Media.MOVIE:
        return hits
    return await _name_the_seasons_that_collide(settings, hits)
