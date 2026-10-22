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

TRAKT IS PREFERRED WHEREVER IT HAS AN ID, INCLUDING ON A CARD ATTRIBUTED TO
SIMKL, and the asymmetry is deliberate rather than left over. The modal is about
the TITLE; the card's attribution is about whose listing put it on the page.
Trakt's per-title answer is strictly the larger one — it carries a cast, a
per-episode air date and a per-episode rating, none of which Simkl publishes
anywhere this app can reach — so preferring the card's own service would take
those away from a merged title for no gain. It DOES mean a viewer who set Simkl's
overview to win on the card can read Trakt's in the modal; that is a real
inconsistency and the smaller of the two, because the modal is one click away
from a source-flip control the card already has and the missing cast would not be
recoverable at all.
"""
from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple

# The registry through its MODULE OBJECT rather than as names bound off it, so a
# test double installed on app.providers is the one these functions call (see
# CLAUDE.md's rule about names bound at import time).
from .. import providers
from . import videos
from .base import Media, SeasonsAnswer, Source, SourceUnavailable, Video

logger = logging.getLogger(__name__)


def ports():
    """Every registered source that can describe a title, in DECLARED order.

    Declared order is the preference order — Trakt, then Simkl — for the reason
    the module docstring gives, and it comes from the registry rather than from a
    list here so a third source is a registration and not an edit.
    """
    return [(source, provider.detail_port)
            for source, provider in providers.registered().items()
            if provider.detail_port is not None]


def ids_from_query(params) -> dict[str, str]:
    """The id map a details request carried, read out of its query string.

    ONE PARAMETER PER SOURCE, NAMED FOR THAT SOURCE — `?trakt=203330&simkl=2601798`
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
    out = []
    for source, provider in providers.registered().items():
        if not str((ids or {}).get(str(source)) or "").strip():
            continue
        if url := provider.title_url(Media(media), ids or {}):
            out.append(Outbound(source, url))
    return out


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


def choose(settings, ids) -> Answerer | None:
    """Who to ask about a title carrying `ids`, and the id to ask them by.

    `ids` is {namespace: value}, straight off the card. A source is asked by ITS
    OWN name in that map — `ids["trakt"]`, `ids["simkl"]` — because a service
    cannot look a title up by an id it does not issue.

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
    known = _known_ids(ids)
    for source, value in known:
        if providers.get(source).detail_port.catalogue_configured(settings):
            return Answerer(source, value, stored_only=False)
    for source, value in known:
        return Answerer(source, value, stored_only=True)
    return None


async def for_season(settings, ids, chosen: Answerer, ask, holds):
    """(who answered, their answer) for ONE SEASON of a title: `chosen`'s answer,
    unless it holds nothing of that season and a later service the title names
    does.

    `ask(answerer)` asks one service; `holds(answer)` says whether the answer has
    anything of the season in it (episodes, a total).

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
    if holds(first) or chosen.stored_only:
        return chosen, first
    for source, value in _known_ids(ids):
        if (source == chosen.source
                or not providers.get(source).detail_port.catalogue_configured(settings)):
            continue
        other = Answerer(source, value, stored_only=False)
        try:
            answer = await ask(other)
        except SourceUnavailable:
            continue
        if holds(answer):
            return other, answer
    return chosen, first


async def describe_season(settings, chosen: Answerer, ids, media, season: int | None, *,
                          cache_only: bool = False) -> tuple[Answerer, dict | None]:
    """`describe`, with the season rule `for_season` states: (who answered, the
    answer). A panel draws the answering service's name and links, so it has to
    be told which one that was. With no season there is nothing to hold, and it
    is `describe` exactly."""
    if season is None:
        return chosen, await describe(settings, chosen, media, season, cache_only=cache_only)
    return await for_season(
        settings, ids, chosen,
        lambda answerer: describe(settings, answerer, media, season, cache_only=cache_only),
        lambda details: bool(details and details.get("episodes")))


async def describe_panel(settings, chosen: Answerer, ids, media, season: int | None, *,
                         cache_only: bool = False) -> tuple[Answerer, dict | None]:
    """`describe_season`, with `videos` replaced by EVERY service's videos
    pooled (app/providers/videos.py says why that one field is pooled when
    nothing else is). What every surface calls to draw a WHOLE panel; a season
    tab calls `describe_season`, because the videos are the title's and a tab
    does not draw them.

    `ids` IS WHAT THE CALLER MAY ASK ABOUT, and it is the same map
    `describe_season` is handed — which is what keeps the share page's promise:
    it passes the ids its link's airings carry and nothing else, so a service
    asked for its videos is a service asked about a title the link shows. Each
    service is asked by its own id in that map, never by an id another service
    stated, so every list is that service's own answer about this title.

    ASKED BESIDE THE DESCRIPTION, not after it, so the panel waits for the
    slower of the two rather than their sum. Only where the season rule hands
    the description to a later service is the first service's list asked
    afterwards — its description was already read, so that is normally cached.

    A SERVICE THAT CANNOT BE REACHED IS READ FROM THE STORE, as `choose` reads
    it; one that FAILS contributes nothing and never fails the panel — the
    videos are an addition to a description, not a reason to lose one.
    """
    others = [(source, value) for source, value in _known_ids(ids)
              if source != chosen.source]
    described, *lists = await asyncio.gather(
        describe_season(settings, chosen, ids, media, season, cache_only=cache_only),
        *(_videos(settings, source, value, media, cache_only=cache_only)
          for source, value in others))
    answerer, details = described
    if details is None:
        return answerer, None
    by_source = {source: listed for (source, _id), listed in zip(others, lists)}
    by_source[answerer.source] = list(details.get("videos") or [])
    if chosen.source not in by_source:
        by_source[chosen.source] = await _videos(
            settings, chosen.source, chosen.source_id, media, cache_only=cache_only)
    order = [source for source, _port in ports()]
    pooled = videos.pool([by_source[source] for source in order if source in by_source],
                         str(answerer.source))
    return answerer, {**details, "videos": pooled}


def _known_ids(ids) -> list[tuple[Source, str]]:
    """(source, that source's own id) for every registered detail source `ids`
    names, in declared order."""
    return [(source, value) for source, _port in ports()
            if (value := str((ids or {}).get(str(source)) or "").strip())]


async def _videos(settings, source: Source, source_id: str, media, *,
                  cache_only: bool) -> list[Video]:
    """One service's videos, [] where it could not answer. An unreachable
    service is read from the store, for the reason `choose` gives."""
    port = providers.get(source).detail_port
    try:
        return await port.fetch_videos(
            settings, Media(media), source_id,
            cache_only=cache_only or not port.catalogue_configured(settings))
    except SourceUnavailable:
        logger.warning("%s could not list videos for %s; the panel plays the others",
                       source, source_id)
        return []


async def describe(settings, chosen: Answerer, media, season: int | None, *,
                   cache_only: bool = False) -> dict | None:
    """The chosen source's answer, or None when there is genuinely nothing to
    show — the whole "who answers and did they" step, in one call.

    IT EXISTS SO THE EMPTY CASE IS DECIDED ONCE. A stored-only answerer is a
    guess that this instance has described the title before; whether it actually
    has can only be found out by reading, and what a caller does about an empty
    read — refuse, rather than open a modal with nothing in it — is a rule both
    modals have to follow identically or the same title behaves differently
    depending on which page it was clicked from.

    `cache_only` is the CALLER's own restriction (the public pages never spend
    the owner's budget) and is ORed with the answerer's, which is a fact about
    the instance. Neither overrides the other; both mean "do not make a call".
    """
    details = await fetch(settings, chosen.source, media, chosen.source_id, season,
                          cache_only=cache_only or chosen.stored_only)
    if chosen.stored_only and not _says_anything(details):
        return None
    return details


def _says_anything(details: dict) -> bool:
    """Whether a stored answer has enough in it to be worth drawing.

    A CACHE-ONLY READ THAT MISSED STILL ANSWERS THE FULL KEY SET, empty — that is
    DetailPort's own contract, so the renderer never sees a missing key — which
    means "nothing was stored" and "this title has nothing to say" arrive
    identically and the only way to tell is to look at the values. These three
    are what a modal is FOR: what it is called, what it is about, and what is in
    it. A card with none of them is a blank the reader cannot act on.
    """
    return bool(details.get("title") or details.get("overview") or details.get("episodes"))


async def seasons(settings, chosen: Answerer, media, *,
                  cached_only: bool = False) -> SeasonsAnswer:
    """The chosen source's season list for one title — DetailPort.fetch_seasons,
    asked of whoever `choose` picked, so "which service lists this title's
    seasons" is the same answer as "which service describes it".

    A stored-only answerer is asked of its store alone, for the reason `describe`
    ORs it in: it is a fact about this instance that the source cannot be
    reached, not a caller's policy. `cached_only` is the caller's policy, ORed in
    beside it: a moment that must not spend a request — a rankings row becoming
    a season reads the list its control already fetched — asks the store alone
    and takes an empty answer as "not known".
    """
    return await providers.get(chosen.source).detail_port.fetch_seasons(
        settings, chosen.source_id, Media(media),
        cached_only=chosen.stored_only or cached_only)


async def fetch(settings, source: Source, media, source_id: str,
                season: int | None, *, cache_only: bool = False) -> dict:
    """The chosen source's answer, in the one field set the modal renders.

    A separate step from `choose` so the public share route can decide who would
    answer and then ask them with `cache_only=True`, which is the only thing that
    differs between the two callers.
    """
    return await providers.get(source).detail_port.fetch_details(
        settings, media, source_id, season, cache_only=cache_only)
