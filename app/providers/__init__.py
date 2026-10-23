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

from typing import NamedTuple

from .base import (
    ID_KEYS,
    CalendarPort,
    Capabilities,
    Item,
    Media,
    Provider,
    Record,
    SearchCatalogue,
    SearchHit,
    SearchPage,
    SearchPort,
    SeasonsAnswer,
    Source,
    SyncPort,
    WatchlistPort,
    collect_ids,
    parse_media,
    render,
)

__all__ = [
    "CalendarPort", "Capabilities", "ID_KEYS", "Item", "Media", "Provider",
    "AskedCatalogue", "Record", "SearchCatalogue", "SearchHit", "SearchPage",
    "SearchPlan", "SearchPort", "SeasonsAnswer",
    "Source", "SyncPort", "UnknownSearchService", "WatchlistPort", "collect_ids",
    "parse_media", "render", "register", "get", "registered", "calendar_sources",
    "for_calendar_sources", "for_tracker_ports", "tracker_sources", "for_search",
    "for_shared_id_lookup", "for_watchlist",
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
    from . import tmdb  # noqa: F401  — registers itself on import
    from . import trakt  # noqa: F401  — same
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


def calendar_sources(*, prefs=None, settings=None) -> list[Provider]:
    """Every source that could put something on this account's calendar, in
    declared order. THE SET THE CACHE FILL ASKS.

    Two conditions, and a third that is the account's:
      - the source publishes a calendar at all (`capabilities.endpoints`) — a
        source usable for something else and with no calendar to give would
        otherwise be asked and answer nothing, which reads as "nothing airs";
      - it carries a `calendar_port` to be asked THROUGH, so declaring endpoints
        without one is caught here rather than as an AttributeError on whichever
        source an instance happens to have;
      - the account's `calendar_source` preference admits it. `prefs=None` means
        "no account is asking" — the pre-warm and the instance-wide question
        below — and admits every source.

    IT DELIBERATELY DOES NOT ASK `is_configured`, and the pairing with
    `for_calendar_sources` below is the whole point. Whether a calendar can be
    read is a question about the SOURCE's own public endpoints, not about whose
    token an instance holds: Trakt's calendar authenticates with the instance's
    client id, and a second source's may need no credential at all. Making the
    fill skip a source over a credential its calendar never uses would take a
    working calendar away from an instance that has one, silently. The window
    fetch itself refuses when it genuinely cannot be made, and that refusal
    degrades one source rather than a month.

    IT TAKES NO `linked`, AND THE CONTRAST WITH `for_tracker_ports` BELOW IS THE
    POINT. The tracker asks every source for one account's own data with that
    account's own token, so a service they have not linked cannot answer and is
    not asked. A calendar is fetched with the INSTANCE's credentials or with
    none at all, so nothing here is a viewer's to supply — see
    app/sources/prefs.py's `admits_calendar`, which is where the calendar's
    `auto` is defined as "every source this instance can fill from".

    `settings=None` MEANS THE SAME "DON'T ASK" AS `prefs=None` — every existing
    caller that has not been updated to pass one keeps today's behaviour exactly.
    Where it IS passed, it governs one narrow thing: whether Simkl contributes to
    this instance's calendar at all, via `settings.simkl_public_calendar_enabled`
    (default True, so an instance that never looks at the setting sees no
    change).

    IT IS NOT CONDITIONED ON WHETHER SIMKL IS CONFIGURED, and it used to be —
    the switch applied only to an instance with no Simkl credentials. That
    qualifier was very nearly always true and therefore said nothing: Simkl's
    calendar files are public and need no credential, so essentially no instance
    ever has a reason to set the instance-level access token `is_configured`
    asks for, and an operator who HAD set one was left with a switch they could
    not use to turn Simkl's calendar off. One control that means what it says
    beats two whose truth table nobody can hold.

    WHAT IT STILL MUST NOT BE is a gate on the TRACKER. It answers exactly one
    question — does Simkl contribute to the calendar — and a calendar needing no
    credential is not withheld over an unrelated tracker credential. Scoped to
    Simkl by name rather than generalized into a per-source flag because there is
    exactly one source this is true of today; a second one earns its own question
    when it exists.
    """
    out: list[Provider] = []
    for source, provider in registered().items():
        if not provider.capabilities.endpoints or provider.calendar_port is None:
            continue
        if prefs is not None and not prefs.admits_calendar(source):
            continue
        if (settings is not None and source is Source.SIMKL
                and not settings.simkl_public_calendar_enabled):
            continue
        out.append(provider)
    return out


def for_calendar_sources(settings, *, prefs=None) -> list[Provider]:
    """The calendar sources this instance can actually USE right now — the same
    set as `calendar_sources`, narrowed to the ones whose credentials are filled
    in.

    THE QUESTION BEHIND `Settings.calendar_source_configured`: is there anybody
    to ask. An empty list is an ordinary state — an instance whose credentials
    have not been filled in yet is one the calendar page has always had to render
    an explanation for, not an error — and it is the state the page explains
    rather than rendering an empty month.

    IT ASKS THE PORT, NOT THE PROVIDER, AND THAT IS THE WHOLE DIFFERENCE FROM
    WHAT IT USED TO DO. This narrowed `calendar_sources` above by
    `Provider.is_configured` — the PRIVATE question, "has this account's own
    token been filled in" — which is precisely the filter the function above
    refuses to apply and says why. Blanking a Trakt client id then emptied the
    calendar on an instance whose other source was reading months perfectly well.
    `CalendarPort.calendar_configured` is the same question asked at the grain
    that can answer it: Trakt's calendar wants the instance's client id, Simkl's
    is an unauthenticated CDN feed and wants nothing, and a provider-level
    predicate — private or catalogue — could only have given one answer for both.
    An instance holding NO credentials at all therefore still has a calendar,
    which is what a public feed means.
    """
    return [p for p in calendar_sources(prefs=prefs, settings=settings)
            if p.calendar_port.calendar_configured(settings)]


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
        account's own tokens — see app/auth/viewer.py's settings_for_user).

    THE ORDER HERE IS REGISTRY ORDER, WHICH IS NOT THE ORDER THE TRACKER USES.
    This function knows which sources are ELIGIBLE; it has no account context
    beyond `prefs`, so it cannot know which of them the viewer wants consulted
    first. `app/distrakt/watch_history.py`'s `tracker_ports` — same name, deliberately,
    because it answers the same question one level up — takes this list and
    reorders it by the account's stated tracker priority, and that result is
    threaded to every caller as an explicit order rather than re-derived. So this
    is the NO-CONTEXT FALLBACK, and a caller that ends up using its order
    directly is a caller nobody has given the account's preference to.

    Order is load-bearing in one narrow place: the FIRST entry is the primary
    source, whose number is the one a frozen month and the announcement post
    carry when a single number is all there is room for. That is exactly why the
    viewer's preference has to win over registry order — a season finished on the
    service they actually use could otherwise never be the one that counts.
    Everything else about reading two sources treats them as equals.

    An empty list is an ordinary answer — an account that has linked nothing, or
    whose one linked service has no usable token — and every caller already
    degrades to "serve what is cached" for it.
    """
    ports: list[tuple[Source, SyncPort]] = []
    for source, provider in registered().items():
        if not provider.capabilities.private_user_data or provider.sync_port is None:
            continue
        # LINKED IS THE WHOLE OF IT NOW. There used to be an account
        # preference here as well, asked alongside the links; reading somebody's
        # history needs their token, so linking a service already IS the
        # statement, and a second one could only agree with it or contradict it.
        if str(source) not in (linked or ()):
            continue
        if not provider.is_configured(settings):
            continue
        ports.append((source, provider.sync_port))
    return ports


def for_shared_id_lookup(settings) -> list[tuple[Source, SearchPort]]:
    """Every source that can be asked RIGHT NOW which of its titles a shared id
    names (SearchPort.find_by_shared_id), in declared order — what a title
    arriving on the tracker, or a calendar card one service draws, asks each
    service it holds no id for.

    NOT THE SEARCH LIST, AND THE DIFFERENCE IS THE POINT. A search box asks the
    catalogue `searched_by_default` and only the services a viewer chose to add
    (`for_search`); an exact lookup by id is how a title found anywhere learns
    every service's id for it, so it asks every service that can answer one —
    Trakt by /search/tmdb, Simkl by /redirect, TMDB by /find — whichever box
    found the title. Reusing the search list here would stop a title found on
    TMDB from ever learning its Trakt and Simkl ids, which is what lets the
    tracker read the viewer's history for it.

    TWO CONDITIONS. The source carries a `search_port` (the lookup's home), and
    its CATALOGUE credential is present, asked through `catalogue_is_configured`
    — which answers a different question per source rather than one rule:
    Trakt's and TMDB's lookups authenticate with the instance's own credential,
    while Simkl's v2 API requires a user token for /redirect, so ITS
    `catalogue_is_configured` reads true only on a per-viewer Settings
    (auth.viewer.settings_for_user) for a viewer who linked Simkl. A caller with
    no viewer hands in the instance-wide Settings, which carries no Simkl token
    (config.load_settings leaves any stored operator token out), and Simkl drops
    out for it.
    """
    return [(source, provider.search_port)
            for source, provider in registered().items()
            if provider.search_port is not None and provider.catalogue_is_configured(settings)]


class UnknownSearchService(ValueError):
    """A search named a service to add that is not one this viewer may add —
    not registered, not searchable, already asked by default, or not linked. A
    REFUSAL, never quietly read as the default search: a box that asked for one
    service and silently got another would be one service standing in for
    another."""


class AskedCatalogue(NamedTuple):
    """One catalogue a search asks: whose it is, the port to ask it through, and
    which of that port's catalogues (base.SearchCatalogue)."""
    source: Source
    port: SearchPort
    catalogue: SearchCatalogue


class SearchPlan(NamedTuple):
    """Who one search asks, and what the box can say about who it did not.

    `asked` is every catalogue to ask, in declared order (a service's own
    catalogues in the order it lists them). `offered` is each catalogue this
    viewer could ADD and has not ("also search …"); `order` every catalogue the
    box can name at all — asked or offered — in that same order, which is the
    order its controls are drawn in; `unconfigured` the services searched by
    default that this instance holds no credential for — said in the box rather
    than replaced by anybody else.
    """
    asked: list[AskedCatalogue]
    offered: tuple[SearchCatalogue, ...]
    unconfigured: tuple[str, ...]
    order: tuple[SearchCatalogue, ...] = ()


def for_search(settings, also=()) -> SearchPlan:
    """The catalogues a search box asks for this viewer, given the catalogues the
    request ADDED (`also`, their keys — references, never data).

    BY DEFAULT, every catalogue of every source that declares itself
    `searched_by_default` and whose catalogue credential this instance holds. AN
    ADDED CATALOGUE is one of any other searchable source the viewer has LINKED —
    `is_configured` on the viewer's own Settings (auth.viewer.settings_for_user),
    the private question, because what is offered is "also search the service
    you use", asked with that viewer's own token where the service asks for one.
    Nothing linked: the default alone. A service keeping two catalogues (Simkl's
    TV and anime) offers each, to be added on its own.

    NO SERVICE STANDS IN FOR ANOTHER. A default source with no credential is
    named in `unconfigured` for the box to say so, and nothing is asked in its
    place; an added key this viewer may not add raises UnknownSearchService.

    THE SAME `also` GIVES THE SAME LIST, which is what lets a result be named by
    its place in an answer (title_search.resolve_hit asks this again to re-read
    exactly the search the page drew).
    """
    named = {str(name) for name in also}
    every: list[tuple[AskedCatalogue, bool]] = []  # (catalogue, asked by default)
    unconfigured: list[str] = []
    for source, provider in registered().items():
        port = provider.search_port
        if port is None:
            continue
        if provider.capabilities.searched_by_default:
            if not provider.catalogue_is_configured(settings):
                unconfigured.append(str(source))
                continue
            every.extend((AskedCatalogue(source, port, c), True) for c in port.catalogues)
        elif provider.is_configured(settings) and provider.catalogue_is_configured(settings):
            every.extend((AskedCatalogue(source, port, c), False) for c in port.catalogues)
    addable = {entry.catalogue.key for entry, default in every if not default}
    refused = named - addable
    if refused:
        raise UnknownSearchService(f"Can't add {', '.join(sorted(refused))} to this search.")
    return SearchPlan(
        asked=[entry for entry, default in every if default or entry.catalogue.key in named],
        offered=tuple(entry.catalogue for entry, default in every
                      if not default and entry.catalogue.key not in named),
        unconfigured=tuple(unconfigured),
        order=tuple(entry.catalogue for entry, _default in every))


def for_watchlist(settings) -> list[tuple[Source, WatchlistPort]]:
    """Every source whose own list can be written for this viewer right now, in
    declared order: it carries a `watchlist_port`, and `settings` — the VIEWER's,
    from auth.viewer.settings_for_user — holds a token for it (`is_configured`,
    the private question, because a write to somebody's list is the most
    private thing a token does). Which of these a viewer has switched on is the
    tracker's to decide; this answers only which could be."""
    return [(source, provider.watchlist_port)
            for source, provider in registered().items()
            if provider.watchlist_port is not None and provider.is_configured(settings)]
