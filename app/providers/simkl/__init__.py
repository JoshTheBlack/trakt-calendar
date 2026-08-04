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
read as a mistake: it answers no calendar endpoint and carries no port for
private per-person reads. Both are declared below rather than implied, so
nothing has to know that Simkl is the newer source in order to avoid asking it
questions it cannot answer.
"""
from __future__ import annotations

from ...config import Settings
from .. import register
from ..base import Capabilities, Source
from .transport import SimklBlockedError, SimklError, SimklRateLimitError

__all__ = ["SimklError", "SimklRateLimitError", "SimklBlockedError"]


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
        # FALSE FOR NOW, AND THIS IS NOT A CLAIM ABOUT SIMKL. Simkl does expose a
        # person's own library and history; this package has no module that reads
        # them yet, and a source declaring private data while carrying no port is
        # lying in the one way the registry cannot catch — the tracker would find
        # a usable source and then have nothing to call. So this flips to True in
        # the same change that adds the port below.
        private_user_data=False,
    )
    sync_port = None

    def is_configured(self, settings: Settings) -> bool:
        return settings.simkl_configured


register(_SimklProvider())
