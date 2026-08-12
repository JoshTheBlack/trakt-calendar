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
goes, which is why `_hit` below always leaves `SearchHit.season` at None.
"""
from __future__ import annotations

import asyncio
import logging

from ...config import Settings
from ..base import Media, SearchHit, Source, collect_ids
from . import transport

logger = logging.getLogger(__name__)

# Which path(s) answer a media-parameterized search. A show query asks BOTH —
# see the module docstring for why — and a movie query asks the one endpoint
# Simkl publishes for films.
_SHOW_PATHS = ("search/tv", "search/anime")
_MOVIE_PATHS = ("search/movie",)


def _ids(raw: dict) -> dict:
    """A search hit's ids block, remapped onto ID_KEYS.

    THE SAME idiom sync.py's `_entry_ids` and calendar.py's `_simkl_ids` use
    for the same Simkl quirk — `simkl_id` on some payloads, `simkl` on others
    — restated here rather than shared across the three, because each module
    owns the boundary conversion for its OWN payload shape and the three are
    read from three different endpoints that happen to share one spelling
    quirk, not one payload changing for one reason.
    """
    mapped = dict(raw)
    if mapped.get("simkl") in (None, "") and mapped.get("simkl_id") not in (None, ""):
        mapped["simkl"] = mapped["simkl_id"]
    return collect_ids(mapped)


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
        ids=_ids(raw_ids),
        title=str(entry.get("title") or ""),
        year=entry.get("year"),
        season=None,
        network="",
        runtime=None,
        overview="",
    )


async def _search_one(settings: Settings, path: str, query: str, media: Media) -> list[SearchHit]:
    """One endpoint's hits. Raises transport.SimklError on a failure — caught
    and weighed by `search_titles`, which is the one place that gets to
    decide whether a failed endpoint fails the whole search."""
    results = await transport.cached_get(
        transport.catalog_client(), settings, path, {"q": query},
        pool=transport.CATALOG_POOL, raise_errors=True,
    )
    return [_hit(entry, media) for entry in results] if isinstance(results, list) else []


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
    outcomes = await asyncio.gather(
        *(_search_one(settings, path, q, media) for path in paths),
        return_exceptions=True,
    )
    hits: list[SearchHit] = []
    failures: list[BaseException] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            failures.append(outcome)
            continue
        hits.extend(outcome)
    if failures and len(failures) == len(paths):
        # Nothing answered at all — raise rather than reading as an honest
        # empty result. The first failure is as good as any to surface: they
        # are independent endpoints failing independently, not one cause with
        # several symptoms.
        raise failures[0]
    for exc in failures:
        logger.warning("Simkl search %s(%r) failed: %s", "/".join(paths), q, exc)
    return hits
