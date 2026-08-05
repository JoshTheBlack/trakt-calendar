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
from .. import register
from ..base import Capabilities, Source
from . import sync
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
    directly by the code that needs Trakt specifically — `calendar.fetch_window`
    from the calendar cache, `detail` from the detail modal, `sync` from the
    tracker's private reads. The Protocol stays narrow on purpose, and this class
    is not a facade over the package."""

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


register(_TraktProvider())
