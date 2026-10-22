"""TMDB's catalogue search, and its exact-match lookup by another service's id.

PUBLIC READS, cached like every other answer in this package, so a result the
add flow names by reference — the query and its place in the answer — reads back
from the cache with `cached_only=True` and never becomes a second request.

ONE PAGE PER ASK. TMDB answers 20 results a page and refuses past page 500; the
caller names the page it wants, by number, so "more" is a reference rather than
a list of results carried back from the browser.
"""
from __future__ import annotations

from ...config import Settings
from ..base import MATCH_SOURCES, Media, SearchHit, Source
from . import normalize, transport

_SEARCH_PATHS = {Media.SHOW: "search/tv", Media.MOVIE: "search/movie"}

# TMDB answers HTTP 400 for any page past this (measured: status_code 22).
MAX_PAGE = 500

# The shared ids /find accepts, as `external_source` names, in MATCH_SOURCES
# order. TMDB has no lookup by MyAnimeList id, so a title known only by that
# cannot be found here; `tmdb` itself needs no lookup.
_FIND_SOURCES = {"imdb": "imdb_id", "tvdb": "tvdb_id"}
SHARED_ID_LOOKUPS = tuple(s for s in MATCH_SOURCES if s in _FIND_SOURCES)

# How long an answer — "not listed" included — stands before TMDB is asked
# again: the same day the other services' lookups by id use.
REASK_SECONDS = 24 * 60 * 60


async def search_titles(settings: Settings, media: Media, query: str, *,
                        page: int = 1, cached_only: bool = False) -> list[SearchHit]:
    """One page of TMDB's search for `query`, as SearchHits in TMDB's order.

    `include_adult=false`, always: this app's calendars and boards are shared
    surfaces, and TMDB's adult catalogue is not something a search box here
    should offer. A search hit carries no external ids — those are on the title
    itself, read when a hit is chosen — so `ids` is the tmdb id alone.
    """
    media = Media(media)
    text = (query or "").strip()
    if not text:
        return []
    body = await transport.cached_get(
        settings, _SEARCH_PATHS[media],
        {"query": text, "include_adult": "false",
         "page": str(max(1, min(int(page), MAX_PAGE)))},
        cache_only=cached_only)
    out = []
    for entry in (body or {}).get("results") or ():
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        ids = normalize.ids_of(entry)
        out.append(SearchHit(
            source=Source.TMDB, source_id=str(entry["id"]), media=media, ids=ids,
            title=normalize.title_of(entry, media),
            year=normalize.year_of(entry, media) or None,
            season=None,
            # A search hit names no network and no runtime; the chosen title's
            # own answer does.
            network="", runtime=None,
            overview=str(entry.get("overview") or "").strip(),
            poster=normalize.poster_of(entry)))
    return out


async def find_by_shared_id(settings: Settings, media: Media, ids: dict) -> dict:
    """TMDB's ids for the title `ids` names, by the first shared id TMDB
    recognises — {} when none is listed. An EXACT match or nothing: /find answers
    by the external id itself, never by a name, and only a hit of the media asked
    for is taken."""
    media = Media(media)
    if ids.get("tmdb"):
        return {"tmdb": ids["tmdb"]}
    key = "tv_results" if media is Media.SHOW else "movie_results"
    for id_type in SHARED_ID_LOOKUPS:
        value = ids.get(id_type)
        if value in (None, "", 0):
            continue
        try:
            body = await transport.cached_get(
                settings, f"find/{value}", {"external_source": _FIND_SOURCES[id_type]},
                ttl_seconds=REASK_SECONDS)
        except transport.TmdbNotFound:
            continue
        for entry in (body or {}).get(key) or ():
            if isinstance(entry, dict) and entry.get("id"):
                return {"tmdb": entry["id"], id_type: value}
    return {}
