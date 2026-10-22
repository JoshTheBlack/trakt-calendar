"""TMDB as a source: the registration, and the package's public error contract.

The client is reached through its own modules — `transport` for the pooled
client and the cached GET, `normalize` for the one reading of each payload,
`detail` for one title's answers, `search` for the catalogue search. As in the
Trakt and Simkl packages, this file does not re-export them: a caller importing
the module it needs says which half of TMDB it depends on.

WHAT TMDB IS TO THIS APP TODAY: the first describer of every title it knows (it
is first in `Source`, see base.py for why), through its DetailPort alone. Its
search, calendar and list ports are None until each is built; the registry asks
for a port and finds none, so nothing reaches for a half TMDB does not have yet.
"""
from __future__ import annotations

from ...config import Settings
from .. import register
from ..base import (Capabilities, Media, Record, SeasonsAnswer, Source, TitleFacts,
                    Video)
from . import detail, normalize
from .transport import TmdbError, TmdbNotFound, TmdbRateLimited

__all__ = ["TmdbError", "TmdbNotFound", "TmdbRateLimited"]


class _TmdbDetailPort:
    """TMDB's answer to "describe this one title" (base.py's DetailPort). A thin
    delegator over detail.py, through the MODULE object, so a test double
    installed on app.providers.tmdb.detail is what answers."""

    def catalogue_configured(self, settings: Settings) -> bool:
        # The instance's read token — every TMDB read authenticates with it and
        # nothing else, so this is also `catalogue_is_configured` below.
        return settings.tmdb_configured

    async def fetch_details(self, settings: Settings, media, source_id,
                            season: int | None, *, cache_only: bool = False) -> dict:
        return await detail.fetch_details(settings, media, source_id, season,
                                          cache_only=cache_only)

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
                            cached_only: bool = False) -> SeasonsAnswer:
        return await detail.fetch_seasons(settings, source_id, media, cached_only=cached_only)


class _TmdbProvider:
    """TMDB as the registry sees it."""

    source = Source.TMDB
    label = "TMDB"
    capabilities = Capabilities(
        # NO CALENDAR YET: TMDB's lists are date-only discover queries and a
        # derived premiere set, none of which this package builds yet. An empty
        # set keeps it out of every calendar the registry fills.
        endpoints=frozenset(),
        days_before=None,
        days_after=None,
        # TMDB keeps no watch history, so it can never back the tracker — the
        # flag and the missing sync port below are one fact stated twice.
        private_user_data=False,
    )
    sync_port = None
    calendar_port = None
    detail_port = _TmdbDetailPort()
    search_port = None
    watchlist_port = None

    def is_configured(self, settings: Settings) -> bool:
        # THE PRIVATE QUESTION, and nobody on this instance can have linked a
        # TMDB account yet: there is no per-person TMDB credential to hold. The
        # instance's read token is a CATALOGUE credential and answers
        # `catalogue_is_configured` below, never this.
        return False

    def catalogue_is_configured(self, settings: Settings) -> bool:
        return settings.tmdb_configured

    def title_url(self, media: Media, ids) -> str:
        return normalize.title_url(Media(media), ids)

    def episode_url(self, ids, season: int, number: int) -> str:
        return normalize.episode_url(ids, season, number)


register(_TmdbProvider())
