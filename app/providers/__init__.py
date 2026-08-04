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
    "register", "get", "registered", "for_calendar", "for_tracker_ports",
    "tracker_sources",
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
    # WHICH ORDER THESE RUN IN DOES NOT MATTER — `registered()` sorts by the
    # `Source` declaration, which is the app's one statement of the declared
    # order, precisely so that importing a provider for some unrelated reason
    # cannot decide which source is an account's primary.
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
    mutate the registry.

    IN `Source` DECLARATION ORDER, WHICH IS THE DECLARED ORDER, and that is not
    cosmetic: the first entry a title or an account has an answer from is its
    PRIMARY source — the one number a frozen month keeps and the announcement
    post carries. Ordering by the enum rather than by insertion means it cannot
    depend on which module happened to be imported first, which is a real hazard
    here: a provider registers itself on import, and importing one for something
    unrelated (a login route reaching its transport) is enough to put it at the
    front of an insertion-ordered dict.
    """
    _load_builtins()
    return {source: _REGISTRY[source] for source in Source if source in _REGISTRY}


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
    for provider in registered().values():
        if provider.capabilities.endpoints and provider.is_configured(settings):
            return provider
    return None


def tracker_sources() -> frozenset[str]:
    """The names of every source that could back the tracker at all.

    "Could" is a property of the SOURCE, not of an account: it is the sources
    that expose one person's own viewing and carry a port to read it with. The
    access gate asks this so it can say "you need one of these linked" without
    naming a service, and so a source gaining private reads widens the gate by
    being registered rather than by a second edit somewhere in auth.
    """
    return frozenset(
        str(source) for source, provider in registered().items()
        if provider.capabilities.private_user_data and provider.sync_port is not None
    )


def for_tracker_ports(prefs, linked, settings) -> list[tuple[Source, SyncPort]]:
    """Every source the tracker should read for this account, in declared order.

    Three conditions, and each one is a different question:
      - the SOURCE can answer at all (private data, and a port to read it with);
      - the ACCOUNT wants it (`prefs.admits_tracker`, given what it has linked);
      - the CREDENTIAL is there (`is_configured` against a Settings carrying that
        account's own tokens — see app/distrakt/routes.py's _distrakt_settings).

    The order is registry order, which is Trakt first, and it is load-bearing in
    one narrow place: the FIRST entry is the account's primary source, whose
    number is the one a frozen month and the announcement post carry when a
    single number is all there is room for. Everything else about reading two
    sources treats them as equals.

    An empty list is an ordinary answer — an account that has linked nothing, or
    whose one linked service has no usable token — and every caller already
    degrades to "serve what is cached" for it.
    """
    ports: list[tuple[Source, SyncPort]] = []
    for source, provider in registered().items():
        if not provider.capabilities.private_user_data or provider.sync_port is None:
            continue
        if not prefs.admits_tracker(source, linked):
            continue
        if not provider.is_configured(settings):
            continue
        ports.append((source, provider.sync_port))
    return ports
