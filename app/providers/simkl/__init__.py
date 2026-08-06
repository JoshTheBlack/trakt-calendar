"""Simkl as a source: the registration, and the package's public error contract.

The client itself is reached through its own module — `transport` for the three
pooled clients, the pacer and the sender. As in the Trakt package beside it,
this file deliberately does NOT re-export those: a second name for a transport
function is a second thing to keep in step, and a caller that imports the module
it actually needs says which half of Simkl it depends on.

The three error types ARE re-exported, because they are not one module's detail.
`except SimklError` is the app-wide degradation contract for this source, and
every caller that writes it is saying "Simkl could not answer".
"""
from __future__ import annotations

from datetime import date

from ...config import Settings
from ...endpoints import Endpoint
from .. import register
from ..base import Capabilities, Record, Source
from . import calendar, sync
from .transport import SimklBlockedError, SimklError, SimklRateLimitError

__all__ = ["SimklError", "SimklRateLimitError", "SimklBlockedError"]


class _SimklSyncPort:
    """The private, per-person reads, as the registry hands them out.

    A THIN OBJECT OVER sync.py's FUNCTIONS, exactly as the Trakt package's port
    is. It exists so the registry has something to hold and the tracker has
    something to call without importing a provider module by name; it holds no
    state and makes no decisions, and every method here delegates through the
    MODULE object so a test double installed on app.providers.simkl.sync is
    actually the thing that gets called.
    """

    async def fetch_last_activities(self, settings: Settings) -> dict:
        return await sync.fetch_last_activities(settings)

    async def fetch_history(self, settings: Settings, start_at: str | None = None) -> list[dict]:
        return await sync.fetch_history(settings, start_at=start_at)

    async def fetch_progress_details(self, settings: Settings, show_ids):
        return await sync.fetch_progress_details(settings, show_ids)

    async def fetch_library(self, settings: Settings, *, start_at: str | None = None,
                            activities: dict | None = None, since: dict | None = None):
        """Simkl can hand over a whole library at once, so this port is a
        LibraryPort as well as a SyncPort — see app/providers/base.py for why
        those are two protocols. It is what lets a title be baselined from Simkl
        with no Simkl id in hand, which is the only state a roster built entirely
        from another service is ever in.
        """
        return await sync.fetch_library(settings, start_at=start_at,
                                        activities=activities, since=since)

    async def fetch_watched_progress(self, settings: Settings,
                                     since_days: int | None = None) -> list[dict]:
        return await sync.fetch_watched_progress(settings, since_days=since_days)

    def watched_progress_from(self, events: list[dict]) -> list[dict]:
        return sync.watched_progress_from(events)

    def movie_plays_from(self, events: list[dict]) -> list[dict]:
        return sync.movie_plays_from(events)


class _SimklCalendarPort:
    """A thin object over calendar.py's fetch_window, exactly as
    _SimklSyncPort is a thin object over sync.py's functions — see that
    class's docstring for why this stays a delegator rather than reimplementing
    anything: it holds no state, and every method calls through the MODULE
    object so a test double installed on app.providers.simkl.calendar is what
    actually answers.
    """

    async def fetch_window(self, endpoint: Endpoint, settings: Settings,
                           start: date, days: int) -> list[Record]:
        return await calendar.fetch_window(endpoint, settings, start, days)


class _SimklProvider:
    """Simkl as the registry sees it: an id, a label, what it can answer, and
    whether it is configured. Everything this package actually DOES is called
    directly by the code that needs Simkl specifically, exactly as Trakt's
    provider object is not a facade over its package."""

    source = Source.SIMKL
    label = "Simkl"
    capabilities = Capabilities(
        # 'shows/finales' is deliberately absent — nothing in Simkl's calendar
        # or catalog flags a season or series finale, and deriving one from
        # the per-title episode lists is future work that needs its own
        # enrichment drain. The other four are what the CDN archive can
        # answer today; see app/providers/simkl/calendar.py for the
        # derivation each one uses.
        endpoints=frozenset({"shows/new", "shows/premieres", "shows", "movies"}),
        # Measured against Simkl's calendar archive, and re-verified live
        # against the same CDN files: it reaches roughly 37-42 months back
        # (growing month by month as the archive extends) and 3-4 months
        # forward, and the numbers here are rounded inward from the more
        # conservative of the two measurements so the declared window is one
        # the source can actually serve. An honest bound beats an empty
        # month, which reads as "nothing airs then" rather than "this source
        # does not go there".
        days_before=1080,
        days_after=90,
        # TRUE, AND IT MOVED IN THE SAME CHANGE AS THE PORT BELOW. A source
        # declaring private data while carrying no port is lying in the one way
        # the registry cannot catch — the tracker would find a usable source and
        # then have nothing to call — so the flag and the port are one fact
        # stated twice and must never be changed apart. The conformance test is
        # what fails if only one of them moves.
        private_user_data=True,
    )
    sync_port = _SimklSyncPort()
    # THE CALENDAR PORT, DECLARED IN THE SAME CHANGE AS THE ENDPOINT SET ABOVE
    # — the conformance test (tests/providers/test_protocol_conformance.py)
    # fails if only one of the two moves, exactly as it does for sync_port.
    calendar_port = _SimklCalendarPort()

    def is_configured(self, settings: Settings) -> bool:
        return settings.simkl_configured


register(_SimklProvider())
