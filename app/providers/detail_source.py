"""Which service describes a title, and whether it can.

EVERY DETAILS PANEL ASKS THIS and they must agree: the signed-in calendar, the
public share page, the tracker's roster and the rankings board all open the same
panel, and "who describes this title" is one question on all four. Written out at
each, the copies would drift the first time one was touched, and the drift would
be invisible: every surface renders a panel that looks right on its own page.

IN THIS PACKAGE, BESIDE THE REGISTRY IT READS, AND NOT IN ANY FEATURE. It began
inside the calendar because the calendar asked first; by the time four features
asked, a feature importing it meant a feature depending on the calendar for a
question that is not the calendar's — and the rankings board, which is meant to
need nothing from the calendar at all, would have had to declare exactly that. Nor
does it belong in the kernel beside app/details_panel.py: that module only
renders, while this one walks the registry and asks a source over the network, and
the kernel may reach this package only from inside a function. What this answers
IS a reading of the registry — every registered detail port, in declared order,
gated on that port's own catalogue predicate — so this package is where it lives,
and every feature already depends on this package for its own reasons.

THE CARD IS IDENTIFIED BY IDS, NOT BY A SERVICE NAME. A calendar group carries
the UNION of every id its records had (app/calendar/resolve.py hoists it), so a
title both services listed arrives here with both. The caller hands over what the
card carried and this decides who to ask; the alternative — the client naming a
service — puts the choice on the one side that cannot see whether that service's
credentials are filled in.

THE DECLARED ORDER DECIDES, AND TMDB IS FIRST IN IT, INCLUDING ON A CARD
ATTRIBUTED TO ANOTHER SERVICE. The panel is about the TITLE; the card's
attribution is about whose listing put it on the page. TMDB is first in `Source`
(base.py says why): it knows almost every title the other services list, by an id
they all state, so leading with it gives every title one describer whichever
service's card it was opened from. Trakt answers a title TMDB does not know, and
Simkl one neither knows — and either answers a SEASON the service before it holds
nothing of (`for_season`), or a title the service before it could say nothing
about. It does mean a viewer who set another service's overview to win on the
card can read TMDB's in the panel; the card's own flip control is one click away
and shows every service's.

WHAT THE CHOSEN SERVICE SAYS IS NOT THIS MODULE'S. Its answer is kept in the title
store, and app/metadata/panel.py reads it from there — asking the service only
when the store has none — so this module answers WHO, and a feature asks the
store WHAT.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import NamedTuple

# The registry through its MODULE OBJECT rather than as names bound off it, so a
# test double installed on app.providers is the one these functions call (see
# CLAUDE.md's rule about names bound at import time).
from .. import providers
from .base import Media, SeasonsAnswer, ServiceLink, Source, SourceUnavailable



def ports():
    """Every registered source that can describe a title, in DECLARED order.

    Declared order is the preference order — TMDB, then Trakt, then Simkl — for
    the reason the module docstring gives, and it comes from the registry rather
    than from a list here so another source is a registration and not an edit.
    """
    return [(source, provider.detail_port)
            for source, provider in providers.registered().items()
            if provider.detail_port is not None]


def ids_from_query(params) -> dict[str, str]:
    """The id map a details request carried, read out of its query string.

    ONE PARAMETER PER SOURCE, NAMED FOR THAT SOURCE — `?tmdb=95396&trakt=154997`
    — rather than the `?id=` a single-source app could get away with. A card
    two services described has two ids and neither is "the" id; sending one plus
    a service name would put the choice on the client (see the module docstring),
    and sending one alone would throw away the fallback that makes a merged card
    open when one service's credentials are missing.

    Only names the registry knows are read, so a query string cannot ask this app
    to look a title up under a namespace no source issues.
    """
    return {str(source): value.strip()
            for source, _port in ports()
            if (value := params.get(str(source)) or "")}


def names_from_query(params) -> dict[str, str]:
    """Each service's own name for the title, as a details request carried them.

    SEPARATE FROM `ids_from_query` BECAUSE THEY ARE FOR DIFFERENT THINGS and only
    one of them may reach `choose`. An id decides WHO IS ASKED about a title; a
    name only decides what a link to that service is spelled with. Folding them
    into one map would put a caller-supplied string into the lookup decision for
    no gain — the id is what a service is asked by.
    """
    return {f"{source}_slug": value.strip()
            for source, _port in ports()
            if (value := params.get(f"{source}_slug") or "")}


class Outbound(NamedTuple):
    """One service's own page for a title, and which service it is."""
    source: Source
    url: str


def outbound_links(ids, media) -> list[Outbound]:
    """Where a title lives at every registered service that knows it, in declared
    order — the outbound buttons a card and the details panel both draw.

    EVERY SERVICE REQUIRES THIS AND TWO SAY SO IN WRITING. Simkl's API rules ask
    that wherever their data appears it link back to the page for that specific
    item rather than to a homepage, and TMDB's terms ask for a link back too; the
    tracker had nowhere at all to do it from — its rows carry a network logo and
    its panel carried no link to any service. A calendar card has had these since
    it existed; this is the same answer, asked once, for the surfaces that have
    only an id map.

    ONE ENTRY PER SERVICE THE MAP NAMES, so a title only one service knows gets
    one button rather than a dead second. The URL is each provider's own
    (`Provider.title_url`), reached through the registry: this used to hold a
    table of two packages' builders, which would have given a third service no
    button and no error.
    """
    return [Outbound(source, url) for source, url, _listed in _pages(media, ids, {})]


def service_links(media, ids, listing: Mapping[str, str]) -> list[ServiceLink]:
    """`outbound_links` for a CARD: every service in `listing` — {service: its
    record's detail_url}, the services the card is drawn from — listed, with its
    own record's page where it gave one, and then every other service `ids`
    names, as a service that knows the title without listing this airing. In
    declared order, so a service's button sits in the same place on every card.

    ONE WALK OVER THE REGISTRY FOR BOTH, so a card and a details panel cannot
    come to different answers about where a title lives at a service; the
    panel's buttons are this with nothing listed. A listed service with no page
    of its own record's and no id to build one from gets no button — a homepage
    is no destination.
    """
    return [ServiceLink(str(source), url, listed)
            for source, url, listed in _pages(media, ids, listing)]


def _pages(media, ids, listing: Mapping[str, str]):
    """(registered source, its page for the title, whether it is listed), in
    declared order — the one walk both functions above are spelled from."""
    for source, provider in providers.registered().items():
        name = str(source)
        listed = name in listing
        url = (listing.get(name) or "") if listed else ""
        if not url and str((ids or {}).get(name) or "").strip():
            url = provider.title_url(Media(media), ids or {})
        if url:
            yield source, url, listed


class Answerer(NamedTuple):
    """Who to ask about one title, and whether they may only be asked of what is
    already stored here.

    `stored_only` is not a caller's policy — the share pages have one of those
    and pass `cache_only` themselves. It is a fact about this instance: no
    registered source that knows this title can be REACHED right now, so the only
    answer available is the one already on disk.
    """
    source: Source
    source_id: str
    stored_only: bool


def choose(settings, ids, order: Sequence[str] | None = None) -> Answerer | None:
    """Who to ask about a title carrying `ids`, and the id to ask them by.

    `ids` is {namespace: value}, straight off the card. A source is asked by ITS
    OWN name in that map — `ids["trakt"]`, `ids["simkl"]` — because a service
    cannot look a title up by an id it does not issue.

    `order` IS A VIEWER'S OWN, and only a question that is theirs passes one: the
    tracker's episode totals (app/sources/prefs.py's `totals_order`). Every details
    panel passes none and is asked in the declared order, so a panel and a card
    keep agreeing about who describes a title; `order` reorders the candidates
    and removes none.

    A SOURCE THAT CANNOT BE REACHED IS STILL WORTH ASKING OF THE LOCAL STORE,
    which is the second pass below. Every field a modal draws was cached the last
    time anybody opened that title, and reading it back needs no credential and
    makes no request — so an instance that has lost a client id can still
    describe what it has already described once, exactly as a public share page
    does for a visitor who may never spend anything. Blanking the modal instead
    threw away data this instance was holding, which is the same answer 13.2's
    roster rows now refuse to give.
    THE PREFERENCE ORDER IS STILL WHOLE-PASS, not per source: a source that can
    be reached beats a stored answer from ANY source, because a live answer is
    complete and a stored one may be a fragment.

    None means nobody can answer at all: the card carries no id any registered
    source recognises. A caller that gets an Answerer with `stored_only` and then
    finds nothing behind it is the other empty case, and only it can tell —
    reading the store is the only way to find out.
    """
    known = known_ids(ids, order)
    for source, value in known:
        if providers.get(source).detail_port.catalogue_configured(settings):
            return Answerer(source, value, stored_only=False)
    for source, value in known:
        return Answerer(source, value, stored_only=True)
    return None


async def for_season(settings, ids, chosen: Answerer, ask, holds,
                     order: Sequence[str] | None = None):
    """(who answered, their answer) for ONE SEASON of a title: `chosen`'s answer,
    unless it holds nothing of that season and a later service the title names
    does.

    `ask(answerer)` asks one service; `holds(answer)` says whether the answer has
    anything of the season in it (episodes, a total). `order` is the order the
    later services are tried in and should be the one `chosen` was picked by: a
    viewer who asked Trakt first for their totals and whose Trakt holds nothing
    of a season goes on to their second choice, not to the declared one.

    WHY A SEASON CAN OUTRANK THE DECLARED ORDER. `choose` picks by id, and the ids
    on a title are one service's claim that two catalogues' entries are the same
    show — which is true of the show and not always of its seasons. MEASURED:
    Simkl files its anthology "Monster" (1446614) as one show with four seasons
    and states the tmdb, imdb, tvdb ids and Trakt slug of "DAHMER – Monster: The
    Jeffrey Dahmer Story", which Trakt and TMDB list as a show with ONE season;
    each later season is a show of its own there. So Trakt, asked first, answered
    Monster's season 4 with no episodes, and the row drew TBA and a panel with no
    episode list while Simkl held all eight, dated.

    DECIDED AFTER THE FIRST ANSWER, NOT BEFORE IT: the chosen service is asked
    exactly as it always was, and only an answer with nothing of the season in it
    costs a second question. A FAILURE IS NOT "NOTHING": it raises, as it did,
    because a service that could not be asked has said nothing about the season.
    A later service that fails is skipped, and with nobody holding the season the
    first answer stands. A stored-only answerer is never followed — nobody here
    can be asked, which is what stored-only means.
    """
    first = await ask(chosen)
    if holds(first):
        return chosen, first
    for other in later_for_season(settings, ids, chosen, order):
        try:
            answer = await ask(other)
        except SourceUnavailable:
            continue
        if holds(answer):
            return other, answer
    return chosen, first


def later_for_season(settings, ids, chosen: Answerer,
                     order: Sequence[str] | None = None) -> Iterator[Answerer]:
    """The services `for_season` turns to, in turn, when `chosen` holds nothing
    of the season: every later one the title names that this instance can ask —
    none after a stored-only `chosen`. ONE WALK, so a season decided from what
    the store already holds (app/metadata/panel.py's `season_lines`) goes the way
    asking would."""
    if chosen.stored_only:
        return
    for source, value in known_ids(ids, order):
        if (source == chosen.source
                or not providers.get(source).detail_port.catalogue_configured(settings)):
            continue
        yield Answerer(source, value, stored_only=False)


def known_ids(ids, order: Sequence[str] | None = None) -> list[tuple[Source, str]]:
    """(source, that source's own id) for every registered detail source `ids`
    names, in declared order — or in `order` first, where a caller states one.

    A STATED ORDER ONLY MOVES SERVICES UP: a name in it that `ids` does not carry
    is skipped, and a service it does not name keeps its declared place after the
    ones it does, so an order written before a source existed cannot hide it.
    """
    found = [(source, value) for source, _port in ports()
             if (value := str((ids or {}).get(str(source)) or "").strip())]
    if not order:
        return found
    rank = {name: place for place, name in enumerate(order)}
    return sorted(found, key=lambda pair: rank.get(str(pair[0]), len(rank)))


async def seasons(settings, chosen: Answerer, media, *,
                  cached_only: bool = False) -> SeasonsAnswer:
    """The chosen source's season list for one title — DetailPort.fetch_seasons,
    asked of whoever `choose` picked, so "which service lists this title's
    seasons" is the same answer as "which service describes it".

    A stored-only answerer is asked of its response cache alone: it is a fact
    about this instance that the source cannot be reached, not a caller's
    policy. `cached_only` is the caller's policy, ORed in
    beside it: a moment that must not spend a request — a rankings row becoming
    a season reads the list its control already fetched — asks the store alone
    and takes an empty answer as "not known".
    """
    return await providers.get(chosen.source).detail_port.fetch_seasons(
        settings, chosen.source_id, Media(media),
        cached_only=chosen.stored_only or cached_only)
