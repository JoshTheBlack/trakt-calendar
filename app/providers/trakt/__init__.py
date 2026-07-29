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
from .calendar import fetch_calendar
from .transport import TraktError, TraktRateLimitError

__all__ = ["TraktError", "TraktRateLimitError"]


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

    def is_configured(self, settings: Settings) -> bool:
        return settings.trakt_configured

    async def fetch_calendar(self, endpoint: Endpoint, settings: Settings,
                             year: int, month: int) -> list[Item]:
        return await fetch_calendar(endpoint, settings, year, month)


register(_TraktProvider())
