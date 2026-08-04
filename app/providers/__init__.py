"""The registry of calendar sources.

One place that knows which sources exist, so a route can ask for "the source
this instance is configured to read the calendar from" instead of naming one.
The registry is keyed by `Source`, which is also what an Item carries, so a
record can always be traced back to the provider that can answer follow-up
questions about it.

Providers register THEMSELVES when their module is imported, and this module
imports them lazily (see `_load_builtins`) rather than at the top. Doing it the
other way round would mean this package imports a provider, which imports
`providers.base` — a cycle that only shows up as an ImportError in whichever
module happens to be loaded first.
"""
from __future__ import annotations

from .base import (
    ID_KEYS,
    Capabilities,
    Item,
    Media,
    Provider,
    Source,
    SyncPort,
    collect_ids,
    parse_media,
)

__all__ = [
    "Capabilities", "ID_KEYS", "Item", "Media", "Provider", "Source", "SyncPort",
    "collect_ids", "parse_media",
    "register", "get", "registered", "for_calendar", "for_tracker",
]

_REGISTRY: dict[Source, Provider] = {}
_loaded = False


def register(provider: Provider) -> None:
    """Add a provider to the registry, replacing any previous holder of its id.

    Replacing rather than refusing keeps a module re-import (which the test
    suite does routinely) from raising over a provider that is already there and
    identical.
    """
    _REGISTRY[Source(provider.source)] = provider


def _load_builtins() -> None:
    """Import the provider modules so their registrations run.

    Called from every read below rather than at import time, because a provider
    module imports `providers.base` and doing this at the top of this file would
    close that loop.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    # THE ORDER IS LOAD-BEARING while for_calendar and for_tracker below are
    # first-match-wins over this dict: registration order is what decides which
    # source answers when more than one is configured. Trakt first, because it
    # is the source every existing instance already reads and a second one
    # appearing must not silently displace it.
    from . import trakt  # noqa: F401  — registers itself on import
    from . import simkl  # noqa: F401  — same


def get(source: Source | str) -> Provider:
    """The provider for `source`. Raises KeyError for one that is not
    registered, which is a programming error rather than a runtime condition —
    every id that reaches here came from the closed `Source` set."""
    _load_builtins()
    return _REGISTRY[Source(source)]


def registered() -> dict[Source, Provider]:
    """Every registered provider, as a copy so a caller iterating it cannot
    mutate the registry."""
    _load_builtins()
    return dict(_REGISTRY)


def for_calendar(settings) -> Provider | None:
    """The provider this instance reads its calendar from, or None when none of
    them has usable credentials.

    Returns None rather than raising: an instance whose credentials have not
    been filled in yet is an ordinary state the calendar page has always had to
    render an explanation for, not an error.

    BEING CONFIGURED IS NOT ENOUGH — the source also has to answer at least one
    calendar endpoint. A source can be perfectly usable for something else and
    still have no calendar to give: returning it here would report the instance
    as having a calendar source and then render an empty month, which reads as
    "nothing airs" rather than "nobody was asked". The check is
    `capabilities.endpoints` rather than a name, so no route learns which source
    is in that state.

    FIRST MATCH WINS OVER A DICT, which means registration order decides the
    answer once more than one source qualifies. That is tolerable only while the
    answer is "there is exactly one calendar source"; a per-account preference is
    what it has to become.
    """
    _load_builtins()
    for provider in _REGISTRY.values():
        if provider.capabilities.endpoints and provider.is_configured(settings):
            return provider
    return None


def for_tracker() -> SyncPort | None:
    """The port whose private, per-person reads back the tracker, or None when no
    registered source has any.

    NO `settings` ARGUMENT, unlike for_calendar. Whether a token happens to be
    filled in does not change WHICH source the tracker reads — it changes whether
    that source answers, which the port's own callers already degrade on (an
    unreadable history serves the cache). With one private-data source registered
    the answer does not depend on configuration at all; when there are two,
    choosing between them becomes a stored preference, and this is the function
    that grows the argument for it.
    """
    _load_builtins()
    for provider in _REGISTRY.values():
        if provider.capabilities.private_user_data and provider.sync_port is not None:
            return provider.sync_port
    return None
