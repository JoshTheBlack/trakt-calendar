"""Simkl as a source: the registration, and the package's public error contract.

The client itself is reached through its own module — `transport` for the two
pooled clients, the pacer and the sender. As in the Trakt package beside it,
this file deliberately does NOT re-export those: a second name for a transport
function is a second thing to keep in step, and a caller that imports the module
it actually needs says which half of Simkl it depends on.

The three error types ARE re-exported, because they are not one module's detail.
`except SimklError` is the app-wide degradation contract for this source, and
every caller that writes it is saying "Simkl could not answer".

WHAT THIS PACKAGE CANNOT DO YET, said plainly so an empty Capabilities does not
read as a mistake: it answers no calendar endpoint. Its private per-person reads
DO exist now and are declared below.
"""
from __future__ import annotations

from ...config import Settings
from .. import register
from ..base import Capabilities, Source
from . import sync
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

    async def fetch_watched_progress(self, settings: Settings,
                                     since_days: int | None = None) -> list[dict]:
        return await sync.fetch_watched_progress(settings, since_days=since_days)

    def watched_progress_from(self, events: list[dict]) -> list[dict]:
        return sync.watched_progress_from(events)

    def movie_plays_from(self, events: list[dict]) -> list[dict]:
        return sync.movie_plays_from(events)


class _SimklProvider:
    """Simkl as the registry sees it: an id, a label, what it can answer, and
    whether it is configured. Everything this package actually DOES is called
    directly by the code that needs Simkl specifically, exactly as Trakt's
    provider object is not a facade over its package."""

    source = Source.SIMKL
    label = "Simkl"
    capabilities = Capabilities(
        # EMPTY, AND HONEST. The endpoint keys are the calendar questions this
        # app asks; Simkl answers none of them until its calendar module exists,
        # and `answers()` returning False is how the fill path skips a source
        # without any route learning which source it is skipping.
        endpoints=frozenset(),
        # Measured against Simkl's calendar archive: it reaches roughly 37 months
        # back and 3-4 months forward, and the numbers here are rounded inward
        # from that so the declared window is one the source can actually serve.
        # An honest bound beats an empty month, which reads as "nothing airs
        # then" rather than "this source does not go there".
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

    def is_configured(self, settings: Settings) -> bool:
        return settings.simkl_configured


register(_SimklProvider())
