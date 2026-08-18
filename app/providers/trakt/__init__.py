"""Trakt as a calendar source: the registration, and the package's public
error contract.

The rest of the client is reached through its own module — `transport` for the
pooled client and the sender, `calendar` for the window fetch and the
normalizer, `detail` for public per-title lookups, `sync` for the reads that
belong to whoever's token asked. This file deliberately does NOT re-export
those: a second name for `detail.fetch_details` is a second thing to keep in
step, and a caller that imports the module it actually needs says which half of
Trakt it depends on.

The two error types ARE re-exported, because they are not one module's detail —
`except TraktError` is the app-wide degradation contract, and every caller that
writes it is saying "Trakt could not answer", not "the transport layer raised".
"""
from __future__ import annotations

from ...config import Settings
from ...endpoints import ENDPOINTS
from datetime import date

from .. import register
from ..base import Capabilities, Media, Record, SearchHit, SeasonsAnswer, Source
from . import calendar, detail, sync
from .transport import TraktError, TraktRateLimitError

__all__ = ["TraktError", "TraktRateLimitError"]


class _TraktCalendarPort:
    """Trakt's answer to "what airs in these days" (app/providers/base.py's
    CalendarPort).

    Thin, and through the module object for the same reason _TraktSyncPort is:
    patching app.providers.trakt.calendar.fetch_window has to reach what this
    calls, and a name bound at class-definition time would be a second reference
    no test double can get at.
    """

    async def fetch_window(self, endpoint, settings: Settings,
                           start: date, days: int) -> list[Record]:
        return await calendar.fetch_window(endpoint, settings, start, days)


class _TraktDetailPort:
    """Trakt's answer to "describe this one title" (app/providers/base.py's
    DetailPort).

    Thin, and through the module object, for the same reason the two ports either
    side of it are: patching app.providers.trakt.detail.fetch_details has to reach
    what this calls.
    """

    def catalogue_configured(self, settings: Settings) -> bool:
        # The client id alone. Trakt's public endpoints authenticate with the
        # `trakt-api-key` header, and only the per-person reads under /sync/ take
        # a bearer — see Settings.trakt_catalogue_configured for why asking the
        # narrower question here turned every Simkl-only viewer's roster row into
        # an error.
        return settings.trakt_catalogue_configured

    async def fetch_details(self, settings: Settings, media, source_id,
                            season: int | None, *, cache_only: bool = False) -> dict:
        return await detail.fetch_details(settings, str(media), source_id, season,
                                          cache_only=cache_only)

    async def fetch_seasons(self, settings: Settings, source_id, media: Media) -> SeasonsAnswer:
        """app/providers/base.py's DetailPort.fetch_seasons, over the existing
        `fetch_show_seasons` — see that function's own comment for the rule its
        answer already upholds (filtering on `episode_count` rather than
        `aired_episodes`, so an unaired season is not hidden from the picker).

        `named_season` IS ALWAYS None: Trakt's catalogue has no concept of a
        search hit that IS a season of a larger show, only shows and their
        seasons as a picker would offer them. `ids` IS ALWAYS EMPTY for the
        same reason `SeasonsAnswer`'s own docstring gives — a Trakt search hit
        already carries every shared id `search_titles` found, so there is
        nothing this per-title call could add.
        """
        seasons = await detail.fetch_show_seasons(settings, source_id)
        return SeasonsAnswer(seasons=seasons, named_season=None, ids={})


class _TraktSearchPort:
    """Trakt's answer to "search this catalogue" (app/providers/base.py's
    SearchPort).

    DELEGATES STRAIGHT TO `detail.search_titles`, AND DOES NOT FILTER WHAT IT
    ANSWERS WITH. The add-show flow used to reach Trakt's search through a
    helper that dropped any hit carrying no Trakt id; that filter was a
    byproduct of Trakt's own catalogue never omitting one, not a rule about
    what a search result IS, and `search_titles`'s docstring says as much for
    the layer above it — it stopped flattening results to a Trakt id because a
    result flattened that way could not be stored at all. THIS PORT ANSWERS FOR
    MORE THAN TRAKT'S CALLERS NOW: its hits are merged with another service's,
    where a title known by tmdb alone is an ordinary result, so re-adding that
    filter here would revive the same assumption one layer down and quietly
    lose rows the merge exists to find.
    """

    async def search_titles(self, settings: Settings, media: Media, query: str) -> list[SearchHit]:
        raw = await detail.search_titles(settings, str(media), query)
        return [
            SearchHit(
                source=Source.TRAKT,
                # A Trakt search hit has always carried a Trakt id in
                # measurement (search_titles's own docstring), so this is
                # taken as given rather than guarded — guarding it would be
                # re-adding, in a quieter place, the exact filter this port
                # exists to drop.
                source_id=str(entry["ids"].get("trakt") or ""),
                media=media,
                ids=entry["ids"],
                title=entry["title"],
                year=entry["year"],
                # Trakt's search never names a season — see SearchHit's own
                # docstring for why the field exists anyway.
                season=None,
                network=entry["network"],
                runtime=entry["runtime"],
                overview=entry["overview"],
            )
            for entry in raw
        ]


class _TraktSyncPort:
    """Trakt's answers to the four private, per-person questions the tracker asks
    (app/providers/base.py's SyncPort).

    Thin by design: each method is one call into `sync`, THROUGH the module object
    rather than through a name imported at class-definition time, so patching
    app.providers.trakt.sync.<fn> still reaches what this calls. A name bound here
    at import would quietly become a second, unpatchable reference.
    """

    async def fetch_last_activities(self, settings: Settings) -> dict:
        return await sync.fetch_last_activities(settings)

    async def fetch_history(self, settings: Settings, start_at: str | None = None) -> list[dict]:
        return await sync.fetch_history(settings, start_at=start_at)

    async def fetch_progress_details(self, settings: Settings, show_ids) -> dict:
        return await sync.fetch_progress_details(settings, show_ids)

    async def fetch_play_counts(self, settings: Settings):
        """app/providers/base.py's PlayCountPort, which this port also satisfies.

        Trakt does NOT implement LibraryPort and should not be made to: that
        protocol carries per-episode watch data and the endpoint behind this one
        has none. They are different questions — the other source's port answers
        "here is the whole library", this answers "here is what changed" — and the
        second is the only one Trakt can answer.
        """
        return await sync.fetch_play_counts(settings)

    async def fetch_watched_progress(self, settings: Settings,
                                     since_days: int | None = None) -> list[dict]:
        return await sync.fetch_watched_progress(settings, since_days=since_days)

    def watched_progress_from(self, events: list[dict]) -> list[dict]:
        return sync.watched_progress_from(events)

    def movie_plays_from(self, events: list[dict]) -> list[dict]:
        return sync.movie_plays_from(events)


class _TraktProvider:
    """Trakt as the registry sees it: an id, a label, what it can answer, and
    whether it is configured. Everything this package actually DOES is called
    directly by the code that needs Trakt specifically — `detail` from the detail
    modal, `sync` from the tracker's private reads. The Protocol stays narrow on
    purpose, and this class is not a facade over the package.

    THE CALENDAR IS REACHED THROUGH THE PORT rather than by importing
    `calendar.fetch_window`: the cache asks the registry which sources can fill a
    window and never names Trakt, which is what lets a second source fill the
    same window rows without the cache learning anything about it."""

    source = Source.TRAKT
    label = "Trakt"
    capabilities = Capabilities(
        endpoints=frozenset(ENDPOINTS),
        # Trakt's calendar endpoints accept any start date and any day count —
        # measured live back to 2010 and forward past the announced schedule —
        # so there is no window to declare.
        days_before=None,
        days_after=None,
        # The token belongs to a person: /users/me/history, the per-show progress
        # records and /sync/ratings are all reachable, which is what lets the
        # tracker and the ranker's ratings import be backed by this source.
        private_user_data=True,
    )
    # What makes that `private_user_data=True` checkable rather than a claim: the
    # tracker asks the registry for this and never for Trakt by name.
    sync_port = _TraktSyncPort()
    # And the same for `capabilities.endpoints`: declaring five calendars while
    # carrying no port would be claiming a calendar this source cannot produce.
    calendar_port = _TraktCalendarPort()
    # And the same again for the modal: it asks the registry which source can
    # describe the title in front of it, so a card carrying a Trakt id gets
    # Trakt's richer answer without the route naming this package.
    detail_port = _TraktDetailPort()
    # And once more for the tracker's manual add flow: it asks the registry
    # which sources can be searched and never names Trakt, so a second
    # catalogue can be searched alongside it with no edit here.
    search_port = _TraktSearchPort()

    def is_configured(self, settings: Settings) -> bool:
        return settings.trakt_configured

    def catalogue_is_configured(self, settings: Settings) -> bool:
        # The client id alone, same question `_TraktDetailPort.catalogue_configured`
        # asks and for the same reason: a catalogue search authenticates with
        # `trakt-api-key` alone, never a bearer, so asking `is_configured`
        # above would gate a public read on one account's private token.
        return settings.trakt_catalogue_configured


register(_TraktProvider())
