"""Trakt as a calendar source: the registration, and the package's public
error contract.

The rest of the client is reached through its own module — `transport` for the
pooled client and the sender, `calendar` for the month fetch and the
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
from ...endpoints import ENDPOINTS, Endpoint
from .. import register
from ..base import Capabilities, Item, Source
from . import sync
from .calendar import fetch_calendar
from .transport import TraktError, TraktRateLimitError

__all__ = ["TraktError", "TraktRateLimitError"]


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

    async def fetch_watched_progress(self, settings: Settings,
                                     since_days: int | None = None) -> list[dict]:
        return await sync.fetch_watched_progress(settings, since_days=since_days)

    def watched_progress_from(self, events: list[dict]) -> list[dict]:
        return sync.watched_progress_from(events)

    def movie_plays_from(self, events: list[dict]) -> list[dict]:
        return sync.movie_plays_from(events)


class _TraktProvider:
    """Trakt as the registry sees it: an id, a label, what it can answer, and
    the one calendar call. Everything else this package exposes is still called
    directly by the code that needs Trakt specifically (the detail modal, the
    tracker's private reads) — the Protocol stays narrow on purpose, and this
    class is not a facade over the package."""

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

    def is_configured(self, settings: Settings) -> bool:
        return settings.trakt_configured

    async def fetch_calendar(self, endpoint: Endpoint, settings: Settings,
                             year: int, month: int) -> list[Item]:
        return await fetch_calendar(endpoint, settings, year, month)


register(_TraktProvider())
