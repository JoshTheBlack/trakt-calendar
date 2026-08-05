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
    CalendarPort,
    Capabilities,
    Item,
    Media,
    Provider,
    Record,
    Source,
    SyncPort,
    collect_ids,
    parse_media,
    render,
)

__all__ = [
    "CalendarPort", "Capabilities", "ID_KEYS", "Item", "Media", "Provider",
    "Record", "Source", "SyncPort", "collect_ids", "parse_media", "render",
    "register", "get", "registered", "calendar_sources", "for_calendar_sources",
    "for_tracker_ports", "tracker_sources",
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


def calendar_sources(*, prefs=None, linked=None) -> list[Provider]:
    """Every source that could put something on this account's calendar, in
    declared order. THE SET THE CACHE FILL ASKS.

    Two conditions, and a third that is the account's:
      - the source publishes a calendar at all (`capabilities.endpoints`) — a
        source usable for something else and with no calendar to give would
        otherwise be asked and answer nothing, which reads as "nothing airs";
      - it carries a `calendar_port` to be asked THROUGH, so declaring endpoints
        without one is caught here rather than as an AttributeError on whichever
        source an instance happens to have;
      - the account's `calendar_source` preference admits it, given what it has
        LINKED. `prefs=None` means "no account is asking" — the pre-warm and the
        instance-wide question below — and admits every source.

    IT DELIBERATELY DOES NOT ASK `is_configured`, and the pairing with
    `for_calendar_sources` below is the whole point. Whether a calendar can be
    read is a question about the SOURCE's own public endpoints, not about whose
    token an instance holds: Trakt's calendar authenticates with the instance's
    client id, and a second source's may need no credential at all. Making the
    fill skip a source over a credential its calendar never uses would take a
    working calendar away from an instance that has one, silently. The window
    fetch itself refuses when it genuinely cannot be made, and that refusal
    degrades one source rather than a month.

    `linked` is passed in for the same reason app/sources/prefs.py takes it: who
    is linked is auth's fact. Note this is where the calendar and the tracker
    genuinely differ — the tracker can read "linked" off the per-request Settings
    because every source it asks needs that account's token, and a calendar
    source needing no token would never appear there.
    """
    out: list[Provider] = []
    for source, provider in registered().items():
        if not provider.capabilities.endpoints or provider.calendar_port is None:
            continue
        if prefs is not None and not prefs.admits_calendar(source, linked or ()):
            continue
        out.append(provider)
    return out


def for_calendar_sources(settings, *, prefs=None, linked=None) -> list[Provider]:
    """The calendar sources this instance can actually USE right now — the same
    set as `calendar_sources`, narrowed to the ones whose credentials are filled
    in.

    THE QUESTION BEHIND `Settings.calendar_source_configured`: is there anybody
    to ask. An empty list is an ordinary state — an instance whose credentials
    have not been filled in yet is one the calendar page has always had to render
    an explanation for, not an error — and it is the state the page explains
    rather than rendering an empty month.
    """
    return [p for p in calendar_sources(prefs=prefs, linked=linked)
            if p.is_configured(settings)]


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
