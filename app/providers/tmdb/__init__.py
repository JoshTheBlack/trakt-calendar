"""TMDB as a source: the registration, and the package's public error contract.

The client is reached through its own modules — `transport` for the pooled
client and the cached GET, `normalize` for the one reading of each payload,
`detail` for one title's answers, `search` for the catalogue search, `calendar`
for what airs in a span of days (with `discover` reading the listings it and
`premieres`, the season-premieres derivation, are built from). As in the Trakt
and Simkl packages, this file does not re-export them: a caller importing the
module it needs says which half of TMDB it depends on.

WHAT TMDB IS TO THIS APP TODAY: the first describer of every title it knows (it
is first in `Source`, see base.py for why), through its DetailPort; the
catalogue every search box asks, through its SearchPort; a calendar source for
Series, Season and Movie Premieres, through its CalendarPort; and, for a viewer
who linked their TMDB account on their account page, the watchlist the tracker
writes to (`watchlist`, over `account`'s reads of that person's own lists) and
the ratings a rankings board can be seeded from. NEVER A TRACKER SOURCE (no sync
port: TMDB keeps no watch history) and NEVER A WAY TO SIGN IN.
"""
from __future__ import annotations

from datetime import date

from ...config import Settings
from .. import register
from ..base import (EPISODE_KEEPING, EPISODE_LISTING, SEASON_KEEPING, TITLE_KEEPING,
                    Capabilities, Media, Reach, Record, SearchCatalogue, SearchPage,
                    SeasonsAnswer, Source, StoreColumns, TitleFacts, Video, status_spoken)
from . import calendar, detail, normalize, search, transport, watchlist
from .transport import TmdbError, TmdbNotFound, TmdbRateLimited

__all__ = ["TmdbError", "TmdbNotFound", "TmdbRateLimited"]


class _TmdbCalendarPort:
    """TMDB's answer to "what airs in these days" (base.py's CalendarPort), over
    calendar.py through the MODULE object, so a test double installed on
    app.providers.tmdb.calendar is what answers."""

    def calendar_configured(self, settings: Settings) -> bool:
        # The instance's read token, which every TMDB read sends and nothing
        # else does — the same answer as `catalogue_configured` below.
        return settings.tmdb_configured

    async def fetch_window(self, endpoint, settings: Settings, start: date, days: int,
                           revalidate: bool = True) -> list[Record]:
        # `revalidate` IS ACCEPTED AND FORBIDS NOTHING HERE: this source never
        # answers "unchanged". A listing page TMDB has not changed comes back
        # 304 and the stored page is served (transport.cached_get), so the
        # records are always rebuilt from a body — which is also why no sibling
        # calendar can be told "unchanged" for a span it has no rows in.
        return await calendar.fetch_window(endpoint, settings, start, days)


class _TmdbDetailPort:
    """TMDB's answer to "describe this one title" (base.py's DetailPort). A thin
    delegator over detail.py, through the MODULE object, so a test double
    installed on app.providers.tmdb.detail is what answers."""

    def catalogue_configured(self, settings: Settings) -> bool:
        # The instance's read token — every TMDB read authenticates with it and
        # nothing else, so this is also `catalogue_is_configured` below.
        return settings.tmdb_configured

    async def fetch_details(self, settings: Settings, media, source_id,
                            season: int | None, *, cache_only: bool = False,
                            fresh: bool = False) -> dict:
        return await detail.fetch_details(settings, media, source_id, season,
                                          cache_only=cache_only, fresh=fresh)

    async def fetch_videos(self, settings: Settings, media, source_id, *,
                           cache_only: bool = False) -> list[Video]:
        return await detail.fetch_videos(settings, media, source_id, cache_only=cache_only)

    async def records_for(self, settings: Settings, source_id, media: Media,
                          moments) -> list[Record]:
        return await detail.records_for(settings, source_id, media, moments)

    async def fetch_season_summary(self, settings: Settings, source_id,
                                   season: int, media: Media, *,
                                   fresh: bool = False) -> dict:
        return await detail.fetch_season_summary(settings, source_id, int(season), media,
                                                 fresh=fresh)

    async def fetch_title_facts(self, settings: Settings, source_id,
                                media: Media) -> TitleFacts:
        return await detail.fetch_title_facts(settings, source_id, media)

    async def fetch_seasons(self, settings: Settings, source_id, media: Media, *,
                            cached_only: bool = False, fresh: bool = False) -> SeasonsAnswer:
        return await detail.fetch_seasons(settings, source_id, media, cached_only=cached_only,
                                          fresh=fresh)


class _TmdbSearchPort:
    """TMDB's answer to "search this catalogue" (base.py's SearchPort), over
    search.py through the MODULE object, as the detail port is over detail.py.
    One catalogue: TMDB searches shows and films each in one place."""

    catalogues = (SearchCatalogue("tmdb", "TMDB"),)

    async def search_titles(self, settings: Settings, media: Media, query: str, *,
                            page: int = 1, catalogue: str | None = None,
                            cached_only: bool = False) -> SearchPage:
        if catalogue not in (None, "tmdb"):
            raise ValueError(f"TMDB has no catalogue {catalogue!r}.")
        return await search.search_titles(settings, media, query, page=page,
                                          cached_only=cached_only)

    async def find_by_shared_id(self, settings: Settings, media: Media, ids: dict) -> dict:
        return await search.find_by_shared_id(settings, media, ids)


class _TmdbWatchlistPort:
    """TMDB's answer to "write this person's list" (base.py's WatchlistPort) — a
    linked viewer's own TMDB watchlist. A thin delegator over watchlist.py,
    through the MODULE object, as the ports above are over theirs: what a write
    to TMDB means, and what was measured before any of it was written, is that
    module's to say."""

    def list_url(self, account_id: str, account_name: str | None) -> str | None:
        return watchlist.list_url(account_id, account_name)

    async def holdings(self, settings: Settings, titles):
        return await watchlist.holdings(settings, titles)

    async def put(self, settings: Settings, titles):
        return await watchlist.put(settings, titles)

    async def drop(self, settings: Settings, rows):
        return await watchlist.drop(settings, rows)

    async def undo(self, settings: Settings, rows):
        return await watchlist.undo(settings, rows)


class _TmdbProvider:
    """TMDB as the registry sees it."""

    source = Source.TMDB
    label = "TMDB"
    capabilities = Capabilities(
        # SERIES, SEASON AND MOVIE PREMIERES (calendar.py says how each is
        # answered); finales and every episode are not TMDB's to list.
        endpoints=calendar.ENDPOINTS,
        # Discover answers for any day, past or future — what TMDB holds about a
        # day is what it lists for it — so there is no window to declare.
        days_before=None,
        days_after=None,
        # TMDB keeps no watch history, so it can never back the tracker — the
        # flag and the missing sync port below are one fact stated twice.
        private_user_data=False,
        # TMDB's API terms forbid caching anything obtained from it for longer
        # than six months; the title store drops a TMDB row not re-obtained
        # inside that, held or not (app/metadata/rows.py).
        retain_seconds=transport.MAX_CACHE_SECONDS,
        # THE WINDOW ASKED: each window is its own discover query, bounded by
        # the window's own dates (calendar.py), and the premieres TMDB derives
        # are worked out a week at a time (premieres.py).
        reach=Reach.WINDOW,
        # EVERY SEARCH BOX ASKS TMDB, with the instance's read token, whoever is
        # searching: its catalogue covers both media, it pages, and a search of it
        # spends nobody's own account. Another service joins a search only when a
        # viewer who linked it asks (providers.for_search).
        searched_by_default=True,
        # A DISCOVER LISTING IS SPARSE — a name, a day, genres, a country — and
        # the title's own answer is one cached request away with the instance's
        # token, so the title store's refresh queue completes every listing by
        # describing it (app/metadata/refresh.py's `queue_listed`).
        describes_sparse_listings=True,
        # A CATALOGUE'S DESCRIPTION IS ITS POINT, and TMDB's states what the
        # calendars that list a title often do not — measured on one October's
        # All Episodes, 10,899 of 16,997 cards had no certification and 15,038
        # no language. So its row joins every card of a title it describes and
        # does not list (app/calendar/cache.py's `with_title_only`).
        describes_every_card=True,
        # WHAT TMDB STATES, read off normalize.py and detail.py: everything a
        # title's description carries, and alone among the services its
        # keywords and show type (`facts_known` says a description stated
        # them), the title's logo, a film's collection, the next scheduled
        # episode and its popularity score. A season's own name, words, poster,
        # cast and trailers. An episode's words, type, runtime and rating, never
        # a vote count (the rating already sits behind RATING_VOTE_FLOOR). NO
        # LISTING `entry`: TMDB's own id for a title is what keys it, so a
        # listing can never come from some other TMDB id for the same title.
        store_columns=StoreColumns(
            titles=TITLE_KEEPING | {
                "title", "year", "network", "country", "language", "certification",
                "status", "overview", "poster", "runtime", "genres_json", "ratings_json",
                "first_aired", "season_count", "videos_json", "release_types_json",
                "cast_json", "homepage", "keywords_json", "show_type", "facts_known",
                "logo", "collection_json", "next_episode_json", "popularity"},
            seasons=SEASON_KEEPING | {
                "episode_count", "premiere", "air_dates_json", "name", "overview",
                "poster", "cast_json", "videos_json"},
            episodes=EPISODE_LISTING | EPISODE_KEEPING | {
                "title", "overview", "first_aired", "episode_type", "runtime", "rating"},
        ),
    )
    sync_port = None
    calendar_port = _TmdbCalendarPort()
    detail_port = _TmdbDetailPort()
    search_port = _TmdbSearchPort()
    watchlist_port = _TmdbWatchlistPort()

    def is_configured(self, settings: Settings) -> bool:
        # THE PRIVATE QUESTION: does this Settings carry a VIEWER's own TMDB
        # grant — true only on the object auth.viewer.settings_for_user builds
        # for somebody who linked TMDB on their account page. The instance's read
        # token is a CATALOGUE credential and answers `catalogue_is_configured`
        # below, never this.
        #
        # WHAT A LINK CHANGES, AND WHAT IT DOES NOT, follows from who asks this.
        # The registry's watchlist question (`for_watchlist`) does, and is the
        # point: a linked viewer's tracker can write to their TMDB watchlist. The
        # tracker's sources do not move — TMDB has no sync port and keeps no watch
        # history (`private_user_data=False`), so neither the access gate nor
        # `for_tracker_ports` can admit it. The search does not move — TMDB is
        # `searched_by_default`, and `for_search` asks this question only of a
        # service that is not. The calendar never asks it.
        return settings.tmdb_account_configured

    def catalogue_is_configured(self, settings: Settings) -> bool:
        return settings.tmdb_configured

    def title_url(self, media: Media, ids) -> str:
        return normalize.title_url(Media(media), ids)

    def episode_url(self, ids, season: int, number: int) -> str:
        return normalize.episode_url(ids, season, number)

    def listing_id(self, ids) -> str:
        return normalize.listing_id(ids)

    def status_means(self, status: str) -> frozenset[str]:
        # TMDB's own words are mapped onto the vocabulary as its answers are read
        # (normalize.STATUS_WORDS), so a stored one is already one of its words.
        return status_spoken(status)


register(_TmdbProvider())
