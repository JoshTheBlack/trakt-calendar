"""Finding a title on the calendar, and finding one that is not on it yet.

TWO QUESTIONS THAT LOOK LIKE ONE, and every choice here falls out of keeping
them apart:

  "WHEN DOES THIS SHOW AIR?" is a question about a CATALOGUE. Any registered
  source answers it for any title, whether or not this instance has ever drawn
  it.

  "WHERE ON MY CALENDAR DOES THIS APPEAR?" is a question about THIS VIEWER's
  calendar, which is not the same set of airings. A title airs on a date and
  still does not appear, for reasons that are all live in this codebase: it is
  on a different endpoint, the instance-wide content floor removed it before
  storage, the viewer's own genre/country/certification/network/release filter
  removes it at read, their source selection admits nobody who listed it, the
  source's declared reach does not cover that month, or nobody has ever opened
  the month so nothing is stored.

WHY THAT MATTERS RATHER THAN BEING A TECHNICALITY: a jump is a promise that
there is something to jump to. Answering it from air dates alone lands a reader
on a day whose grid draws nothing, with nothing on the page able to explain
itself. So a STORED result is confirmed by the real read path before it is
offered -- `assemble_range`, the viewer's own prefs, the same call the calendar
page makes -- and never by this module deciding for itself what a filter would
have done.

A CATALOGUE RESULT IS A DIFFERENT PROMISE AND SAYS SO. It links to the MONTH the
title should be in rather than to a day, because visiting a month FILLS it: the
calendar learns about the title on arrival, and a card may then be there. It may
also not be -- if no source lists that title on that endpoint, the month fills
and the title still is not in it. The surface says "go to where this should be"
and never promises a card.

NOTHING HERE SPENDS A REQUEST UNLESS ASKED. The stored half is an index read and
runs as the viewer types; the catalogue half runs only when they press Enter or
the button. That split is the author's, and it is better than a heuristic about
when the local answer looks complete: nobody is surprised by a request they did
not make, and nobody waits on one to see what their own calendar already holds.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import cache as calendar_cache
from . import enrich as calendar_enrich
from . import entries
from . import filter as calendar_filter
from . import vocab
from .. import cache, db, providers
from ..endpoints import get_endpoint
from ..perftrace import span
# The catalogue fan-out and merge every searching feature shares, whose dedupe
# rules carry measured findings a second implementation would get wrong.
from ..providers import title_search
from ..providers.base import (Item, Media, Source, SourceUnavailable, epoch_moment,
                              render)

logger = logging.getLogger(__name__)

# HOW MANY MATCHING GROUPS ONE ENDPOINT MAY CONTRIBUTE. A group is a title, so
# this is a ceiling on titles rather than on airings — one title airing weekly
# for a year is one group and fifty rows.
GROUP_LIMIT = 40

# HOW MANY ROWS A VIEWER IS SHOWN. Beyond this a search is not answering the
# question they asked, and the honest response is to say the query was broad.
RESULT_LIMIT = 60

# HOW MANY CATALOGUE HITS ARE DESCRIBED. Each costs one per-title lookup, so
# this is the request budget of a search, spent only on an explicit ask.
CATALOGUE_LIMIT = 8

# How many of one show's season premieres a search will offer, newest first.
# WITHOUT THIS ONE FRANCHISE IS THE WHOLE RESULT LIST: a title running twenty
# seasons would push every other show off, and the seasons nobody searched for
# would crowd out the shows they did. Three is enough to reach a season now
# airing, the one before it, and the one being announced.
SEASONS_PER_SHOW = 3

# How long a title written by this path stays fresh. THE SAME DAY-SCALE THE FILL
# USES, because it is the same kind of row: a title's genres and certification
# change about as often as the fill assumes, and picking a different number here
# would mean two answers to "when is a stored title stale".
TITLE_STALE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Airing:
    """One stored airing that matched, as the calendar itself would draw it."""
    item: Item
    endpoint_key: str
    endpoint_label: str
    year: int
    month: int
    day: str          # YYYY-MM-DD, the viewer's own local day

    @property
    def url(self) -> str:
        """The calendar, at the month, anchored on THIS CARD.

        `#jump-target` AND NOT `#day-...`, because the card is what was asked
        for and the day is only where it happens to sit. `highlight=` names the
        card to the route, which does two things with it: it marks that card
        `id="jump-target"`, and it ships that card's whole day with the shell
        even when the day falls past the inline window. Both are needed for the
        anchor to work, because a browser only scrolls to an element that exists
        when it parses the page — and a card inside an unfetched placeholder
        does not.

        THE DAY ANCHOR WAS NOT WRONG, IT WAS TOO COARSE. It landed at the top of
        the right day, which on a day holding thirty titles is not the same as
        landing on the one somebody searched for.

        `day=` NAMES WHICH AIRING, because `highlight=` names a title and a
        weekly show is four rows in one month. Both are references the server
        resolves against the month it draws, not data it is being handed.
        """
        return (f"/calendar?year={self.year}&month={self.month}"
                f"&endpoint={quote(self.endpoint_key, safe='')}"
                f"&highlight={quote(self.item.mark_key)}&day={self.day}#jump-target")


@dataclass(frozen=True)
class Elsewhere:
    """One title a catalogue answered for that this viewer's calendar does not
    currently draw.

    IT CARRIES AN `Item` LIKE A STORED RESULT DOES, built from a per-title
    lookup, so the two render through the same card and a reader is not asked to
    learn a second shape for the same kind of thing. What differs is the promise
    the link makes, which is `url`'s whole job.
    """
    item: Item
    endpoint_key: str
    endpoint_label: str
    year: int
    month: int
    day: str = ""
    # THE RECORD AS WELL AS THE RENDERED ITEM, because they answer different
    # questions: the Item is what a template draws, and the Record is what the
    # viewer's filters read. Rendering first and filtering on the result would
    # mean teaching the filter a second shape.
    record: object = None

    source: str = ""
    source_id: str = ""
    media: str = "show"

    @property
    def url(self) -> str:
        """The jump route, which writes this one airing and then sends the
        reader to the month it lands on.

        NOT STRAIGHT TO THE MONTH, because the month may not draw this title
        however correctly it fills — see `fill_one_gap` for the two datasets that
        disagree. And NOT WRITTEN AT SEARCH TIME, because a search returns
        everything that matched and a reader asked for one of them.

        IT CARRIES A SOURCE AND AN ID AND NOTHING ELSE THAT MATTERS. The service
        is asked for the rest, so this link cannot be edited into a calendar row
        of somebody's choosing.

        THE DISTINCTION IS BETWEEN POINTING AND PROMISING. Arriving fills the
        month, which is the point: the calendar learns about the title, and a
        card appears if a source lists it there. Whether one does is not
        something this can know, so the link may land on a day with nothing on
        it — which is why the results say so in words.

        THE ANCHOR IS FREE AND THE HIGHLIGHT IS NOT. `#day-` is resolved by the
        browser: it scrolls to that day when the day is drawn and does nothing
        at all when it is not, so it costs nothing to be wrong about and saves
        a reader scrolling a month to find out. A `highlight=` would be a
        different claim — it names a specific CARD, and the identity a catalogue
        lookup builds need not be the one the calendar draws for that title,
        since which source describes it differs. Highlighting the wrong thing
        or nothing is worse than not offering to.
        """
        season = f"&season={self.item.season}" if self.item.season is not None else ""
        # `#jump-target` ON THE LINK ITSELF, not only on the redirect: the page is
        # boosted, so following this is a fetch, and a fetch's final URL drops
        # the fragment the redirect's Location carried — measured in Chromium and
        # Firefox, the card was drawn and marked and the page stayed at the top.
        # htmx scrolls to the anchor of the address it was asked to follow.
        return (f"/calendar/jump?source={quote(str(self.source), safe='')}"
                f"&id={quote(str(self.source_id), safe='')}"
                f"&media={quote(str(self.media), safe='')}{season}"
                f"&endpoint={quote(self.endpoint_key, safe='')}"
                f"&year={self.year}&month={self.month}#jump-target")


@dataclass(frozen=True)
class Results:
    """What one search found. `failed` names the services that could not be
    asked, so a partial catalogue answer can say so rather than reading as
    "nothing else exists"."""
    airings: tuple[Airing, ...] = ()
    elsewhere: tuple[Elsewhere, ...] = ()
    truncated: bool = False
    failed: frozenset = frozenset()
    searched: tuple[str, ...] = ()


def _local_day(air_ts: float, date_only: bool, tz: ZoneInfo) -> date:
    """The day an airing falls on FOR THIS VIEWER.

    A RELEASE DATE IS NOT AN INSTANT, which is why `date_only` is asked: a film
    released on the 4th is released on the 4th everywhere, and converting it
    into a timezone moves it a day for viewers west of the source. The same rule
    the read path applies when it groups a month.
    """
    # `epoch_moment` and not `datetime.fromtimestamp`: this is the path a
    # CATALOGUE answer travels, and a service will happily name a season that
    # premiered before 1970 — which fromtimestamp refuses outright on Windows.
    # See providers/base.epoch_moment.
    moment = epoch_moment(float(air_ts))
    return moment.date() if date_only else moment.astimezone(tz).date()


async def stored(query: str, *, settings, prefs, tz: ZoneInfo,
                 source_selection, marks, endpoints) -> Results:
    """Every airing of a matching title that THIS viewer's calendar would draw,
    across `endpoints`.

    CONFIRMED BY THE READ PATH, NEVER BY THIS MODULE'S OWN OPINION. The matching
    groups are run through `cache.visible_records` — the very pipeline
    `assemble_range` runs, with this viewer's own filters and source selection —
    and a row is offered only if it comes back out. Deciding here what a filter
    would have done would be a second implementation of the six reasons a title
    is absent, and it would be wrong the first time any of them changed.

    BY GROUP, NOT BY MONTH, AND THAT IS THE DIFFERENCE BETWEEN A SECOND AND TEN.
    An earlier version assembled every candidate MONTH; measured on a real
    instance, one `shows` month held 12,880 airings and a five-month search took
    eleven seconds, almost all of it filtering titles nobody had asked about.
    The groups a query matches are a handful, and `title_key` — the identity the
    read path groups by — is what lets exactly those be loaded whole.

    NO NETWORK. Nothing here fetches: a search may look at what is stored, and a
    month nobody has opened is a job for the catalogue half, which the viewer has
    to ask for.
    """
    # entries.MIN_QUERY, enforced here rather than in the route so the typing
    # path and the Enter path cannot disagree about it.
    needle = entries.fold_title(query)
    if len(needle) < entries.MIN_QUERY:
        return Results()

    found: list[Airing] = []
    with span("search.stored", query_len=len(needle),
              endpoints=len(endpoints)) as sp:
        for endpoint in endpoints:
            keys = await entries.groups_matching(endpoint.key, query, GROUP_LIMIT)
            if not keys:
                continue
            records = await entries.read_groups(endpoint.key, keys)
            groups = calendar_cache.group_records(records)
            # NARROWED PER ENDPOINT, INSIDE THE LOOP, because a search across
            # every calendar spans both media: the film endpoint answers under
            # the film filters and the film services, the show endpoints under
            # theirs. Resolving either once outside the loop would apply one
            # medium's answers to the other's results.
            specs = vocab.active_specs(prefs, endpoint.media, honour_pause=True)
            kept, _narrowed = await calendar_cache.visible_records(
                groups, endpoint,
                genres=specs["genres"], countries=specs["countries"],
                show_certifications=specs["show_certifications"],
                movie_certifications=specs["movie_certifications"],
                movie_release_countries=specs["movie_release_countries"],
                movie_release_types=specs["movie_release_types"],
                prefs=source_selection.for_media(endpoint.media), settings=settings)
            for record in kept:
                item = render(record, tz)
                # THE TITLE IS CHECKED AGAIN AFTER RESOLUTION, because a group is
                # loaded whole and the card may end up carrying the OTHER
                # source's spelling — which is the right card, and may not be
                # the string that matched.
                if needle not in entries.fold_title(item.title):
                    continue
                day = date.fromisoformat(item.air_date)
                found.append(Airing(
                    item=item, endpoint_key=endpoint.key,
                    endpoint_label=endpoint.label,
                    year=day.year, month=day.month, day=item.air_date))
        sp.set(found=len(found))

    found.sort(key=lambda a: a.item.air_ts, reverse=True)
    return Results(airings=tuple(found[:RESULT_LIMIT]),
                   truncated=len(found) > RESULT_LIMIT,
                   searched=tuple(e.key for e in endpoints))


def _endpoint_for(media: Media) -> str:
    """Which calendar a catalogue hit should be looked for on.

    THE PREMIERES CALENDARS, because that is what a title being searched for is
    most likely to be wanted on and it is the one a month-fill will actually put
    it on. A film has one calendar; a show is offered on the premieres one,
    which is where a title nobody has drawn yet turns up if it turns up at all.
    """
    return "movies" if media is Media.MOVIE else "shows/premieres"


async def catalogue(query: str, *, settings, prefs, tz: ZoneInfo,
                    known: frozenset[str]) -> Results:
    """Titles a registered catalogue knows about, described well enough to draw.

    ONLY ON AN EXPLICIT ASK. This is the half that spends requests, and it is
    reached from the search button or Enter rather than from typing.

    `known` IS THE `(mark_key, season)` PAIRS THE STORED HALF ALREADY ANSWERED
    FOR, so a season the calendar draws is not offered a second time as
    somewhere to go — the stored answer is strictly better, it names the day.
    The season is part of the key because a row here is a season premiere and
    `mark_key` is deliberately the TITLE's identity; matching on the title alone
    would hide every season of a show the calendar holds any airing of.

    AND THE VIEWER'S OWN FILTERS APPLY HERE TOO, through the same
    `filter.filter_records` the stored half reaches via `visible_records`. This
    half used to skip them, which made it the one surface in the app that
    offered somewhere it knew the reader could not get to: a title excluded by
    genre, country or certification was listed, clicked, and the month it opened
    could never draw it. Filtering makes "not on your calendar" mean "not there
    YET" rather than "not there, ever, and you have no way to tell which".

    A RECORD IS ASKED ONLY WHAT THE LOOKUP COULD ANSWER. `exempt_unenriched` is
    NOT set: unlike a calendar fill, every row here has just been described by a
    per-title lookup, so a missing genre means the service does not know one
    rather than that nothing has asked yet.

    A ROW IS A SEASON, NOT A SHOW, and that is the difference between offering
    somewhere useful and somewhere technically true. A long-running title has one
    first-air date and many premieres; keying the link on the show sent every
    search for a recent season to the month the show began, years earlier, which
    is a jump nobody wanted. Each season the source can date gets its own row and
    its own month.

    TWO LOOKUPS PER HIT AND THAT IS THE COST. A search hit carries no air date on
    either source measured (Trakt's search omits it, Simkl's has only a year), so
    a description supplies the poster, genres and certification a row is drawn
    and filtered on, and a season list supplies the premieres. The DATE COMES
    FREE WITH THE SEASON LIST — Trakt returns `first_aired` per season in the
    same response as the counts, and Simkl's grouping already holds every
    episode date — so resolving dates later, on a click, would be a third call
    to learn what the second already said.

    THE HITS ARE ASKED CONCURRENTLY, which is what keeps a second lookup from
    doubling the wait. Both transports pace themselves against their service's
    ceiling, so the gather is bounded by that rather than racing it.

    BOUNDED TWICE OVER: CATALOGUE_LIMIT hits are described at all, and
    SEASONS_PER_SHOW rows come back from any one of them, newest first. Without
    the second bound one franchise with twenty seasons would be the entire
    result list.
    """
    needle = entries.fold_title(query)
    if len(needle) < entries.MIN_QUERY:
        return Results()

    asked = providers.for_catalogue_search(settings)
    if not asked:
        return Results()

    out: list[Elsewhere] = []
    suppressed: list[Elsewhere] = []
    failed: set = set()
    with span("search.catalogue", sources=len(asked)) as sp:
        for media in (Media.SHOW, Media.MOVIE):
            merged = await title_search.search_catalogue(asked, settings, media, query)
            failed |= set(merged.failed)
            # THE SAME SPECS THE CALENDAR ITSELF READS, picked per media by the
            # one function that knows which column answers for which medium.
            # Every dimension needs that now, not only certification: the two
            # certification vocabularies were always different (TV Parental
            # Guidelines against MPA ratings), and genres and countries became
            # per-medium with the panel that asks for them.
            specs = vocab.active_specs(prefs, media, honour_pause=True)
            certifications = (specs["movie_certifications"] if media is Media.MOVIE
                              else specs["show_certifications"])
            described = await asyncio.gather(*(
                _describe(settings, hit, media, tz)
                for hit in merged.hits[:CATALOGUE_LIMIT]))
            for rows in described:
                for row in rows:
                    if (row.item.mark_key, row.item.season) in known:
                        suppressed.append(row)
                        continue
                    if not calendar_filter.filter_records(
                            [row.record], specs["genres"], specs["countries"],
                            certifications):
                        continue
                    out.append(row)
        sp.set(found=len(out))
        await _complete_known_seasons(suppressed)

    return Results(elsewhere=tuple(out[:RESULT_LIMIT]),
                   failed=frozenset(failed))


async def _complete_known_seasons(rows: list[Elsewhere]) -> int:
    """Store the airing each suppressed hit already built, where the stored season
    holds no airing from THAT hit's service. Returns how many were stored.

    THE SUPPRESSION IS A RENDERING DECISION AND THIS IS NOT, and keeping them
    apart is the point. A season the calendar already draws is rightly not offered
    as a second place to go — two jumps to one day is the thing that rule exists
    to prevent. But the rule is keyed on the title and season, not the SOURCE, so
    a Trakt hit for a season the calendar holds only Simkl's airing of was
    dropped as a duplicate, and the one write that could have taught the calendar
    Trakt's airing never happened. MEASURED: Groomed: The Truth Behind the Lie,
    searched for, answered by Trakt, suppressed, and left Simkl-only.

    COMPLETING A CARD, NOT ADDING ONE, which is why this may happen on a search
    where fill_one_gap may not: the season is already on the calendar, so the
    stored airing joins the card it matches (same title key) rather than drawing
    a new one — no decision about the calendar is being made that the calendar
    had not made already. It costs no request: each row's record was built by its
    own source's lookup to draw the row at all.
    """
    now = db.now()
    stored = 0
    for row in rows:
        drawn = await entries.sources_airing(row.endpoint_key, row.item.mark_key, row.item.season)
        if str(row.source) in drawn:
            continue
        written = await entries.store_loose_airings(
            row.endpoint_key, [row.record], now=now, stale_after=now + TITLE_STALE_SECONDS)
        if written:
            calendar_cache.forget_endpoint_spans(row.endpoint_key)
        stored += written
    if stored:
        logger.info("calendar search: completed %d stored season(s) with a source's "
                    "own airing", stored)
    return stored


@dataclass(frozen=True)
class Landing:
    """Where an airing `fill_one_gap` wrote is drawn: the card's mark key and the
    viewer's day it falls on. Falsy when nothing was written.

    THE DAY AS WELL AS THE MARK, because a month draws only its first few days
    with the page and fetches the rest as the reader scrolls — so a card on a
    later day does not exist when the browser looks for its anchor, and a jump
    naming the mark alone landed at the top of the month. `Jump.url` already
    carries both for a stored result; this is the same pair for a written one.
    """
    mark: str = ""
    day: str = ""

    def __bool__(self) -> bool:
        return bool(self.mark)


async def fill_one_gap(settings, *, source, source_id: str, media: Media,
                      season: int | None, endpoint_key: str, tz: ZoneInfo,
                      now: int, exact_season: bool = False) -> Landing:
    """Write ONE title's airing into the calendar and answer where it is drawn.

    WHAT THIS IS FOR. A service can be missing its own title: Trakt's show record
    dates Half Man's first season to 2026-04-28T20:00Z and Trakt's premieres
    CALENDAR for that week does not list it at all. So a search result could name
    a month, the month would fill correctly, and there would still be no such
    card — because the row's destination came from one dataset and the page is
    built from another. Writing the airing is what closes that, and it is the
    only way this app can show a premiere both services' calendars have missed.

    ONE TITLE, ON A CLICK, AND THAT IS THE WHOLE DIFFERENCE FROM WHERE THIS
    STARTED. It first ran over every row a search returned, which meant typing
    "traitors" and pressing Enter wrote sixteen season premieres into a calendar
    shared by everyone on the instance — sixteen decisions from one act of
    curiosity. A viewer following a result has asked for exactly one thing, and
    exactly one thing is written.

    THE SOURCE IS ASKED WHO THE TITLE IS; THE REQUEST IS NOT BELIEVED. The caller
    supplies only which service and that service's own id — enough to address a
    lookup and nothing more. Title, date, network, poster and the shared ids all
    come back from the service, so a hand-made URL cannot invent a calendar row.

    Returns the card's `Landing`, falsy when nothing could be written — which is
    not an error: the month is still worth opening.

    `exact_season` REFUSES THE FALLBACK. A search jump may land on a show's own
    first-air date when its season cannot be dated — the reader asked where the
    title is. Completing a SEASON's card may not: measured on a live copy, three
    of four asks got a service that could not date the season being completed
    (a sixth, a second, a fourth), and the fallback wrote that show's premiere
    from 2021, 2025 and 2024 into the shared premieres calendar with no season.
    With `exact_season`, a season the service cannot date writes nothing.
    """
    provider = providers.get(source)
    if provider is None or provider.detail_port is None:
        return Landing()

    when = ""
    if season is not None and media is not Media.MOVIE:
        for number, premiere in await _season_premieres(
                settings, provider, source, source_id, media):
            if number == season:
                when = premiere
                break
    if not when:
        if exact_season and season is not None and media is not Media.MOVIE:
            return Landing()
        # The title's own first-air date: what a film always uses, and what a
        # show falls back to when the season could not be dated.
        when = await _first_aired_of(settings, provider, media, source_id)
        season = None
    if not when:
        return Landing()

    records = await _records_for(provider, settings, source_id, media,
                                 [(season, when)])
    if not records:
        return Landing()
    await entries.store_loose_airings(
        endpoint_key, records, now=now, stale_after=now + TITLE_STALE_SECONDS)
    calendar_cache.forget_endpoint_spans(endpoint_key)
    item = render(records[0], tz)
    return Landing(item.mark_key, item.air_date)


# THE CALENDARS WHERE A CARD IS A SEASON'S PREMIERE OR A FILM'S RELEASE — the one
# airing a service's own record can be built for (DetailPort.records_for), so the
# only cards the other service's record can join. An All Episodes or Finales card
# is some other episode, and nothing here can build that service's record of it.
COMPLETABLE_ENDPOINTS = frozenset({"shows/new", "shows/premieres", "movies"})

# How long an answer to "does the other service list this?" stands before a panel
# open may ask again — found or not. A week, because a title the other service did
# not list yet is usually one it has not got round to, and asking on every open
# would put a lookup on every click for an answer that changes slowly.
OTHER_SERVICE_ASK_SECONDS = 7 * 24 * 60 * 60


def _asked_key(source: str, endpoint_key: str, title_key: str, season: int | None) -> str:
    return f"calendar_other_service_asked:{source}:{endpoint_key}:{title_key}:{season}"


async def other_service_to_ask(settings, *, endpoint_key: str, title_key: str,
                               season: int | None) -> str:
    """The service worth asking about a card whose calendar only ONE service
    draws, or "" when there is none.

    WHAT IT IS FOR. Most one-service cards are one service's alone — Simkl lists
    far more daily serials and foreign titles — so asking about all of them on a
    schedule would cost thousands of lookups a day to make a few cards two-sided
    (measured on the owner's instance: 17,205 of 18,342 stored titles are one
    service's). Opening a card's panel is somebody asking about THAT title, which
    is when one lookup is worth paying.

    EVERY CONDITION IS READ FROM WHAT THIS INSTANCE HOLDS, none from the request:
    a calendar whose cards can be completed; exactly one service with a stored
    airing of this title and season there; another service that publishes this
    calendar here and can be searched; and no answer from it in the last week.
    """
    if endpoint_key not in COMPLETABLE_ENDPOINTS or not title_key:
        return ""
    drawn = await entries.sources_airing(endpoint_key, title_key, season)
    if len(drawn) != 1:
        return ""
    on_calendar = {str(p.source) for p in providers.calendar_sources(settings=settings)
                   if p.capabilities.answers(endpoint_key)
                   and p.calendar_port.calendar_configured(settings)}
    for source, _port in providers.for_catalogue_search(settings):
        name = str(source)
        if name in drawn or name not in on_calendar:
            continue
        if await cache.get(_asked_key(name, endpoint_key, title_key, season),
                           OTHER_SERVICE_ASK_SECONDS) is None:
            return name
    return ""


async def complete_from_other_service(settings, *, source: str, endpoint_key: str,
                                      title_key: str, season: int | None,
                                      tz: ZoneInfo, now: int) -> bool:
    """Ask `source` whether it lists the title, by shared id, and store its own
    airing of this season's premiere (or the film's release) on `endpoint_key`
    if it does. Returns whether an airing was stored.

    THE SAME TWO STEPS AS ELSEWHERE, NOT A THIRD WAY. The ask is the search port's
    exact lookup (find_by_shared_id — an answer is taken only when that service's
    own record states the id asked about), and the write is fill_one_gap, the
    search's own gap fill: that service's own record, built by its own builder,
    stored as its calendar feed would have stored it. So the airing joins this
    card when the two services date it in the same week, and stands as that
    service's card on its own date when they do not, exactly as its calendar
    would draw it.

    THE IDS COME FROM THE STORE (entries.group_identity), never from the request:
    a write whose subject is a key the server resolves can only land on a title
    this instance already holds.

    AN ANSWER IS REMEMBERED FOR A WEEK, "not listed" included; a failure to ask
    is not, so a service that was down is asked again on the next open.
    """
    identity = await entries.group_identity(title_key)
    port = dict((str(s), p) for s, p in providers.for_catalogue_search(settings)).get(source)
    if identity is None or port is None:
        return False
    media_name, ids, title = identity
    media = Media(media_name)
    try:
        found = await port.find_by_shared_id(settings, media, dict(ids))
    except SourceUnavailable as exc:
        logger.info("calendar: %s could not be asked about %s (%s)", source, title_key, exc)
        return False
    await cache.set(_asked_key(source, endpoint_key, title_key, season), 1,
                    retain_seconds=OTHER_SERVICE_ASK_SECONDS)
    source_id = (found or {}).get(source)
    if not source_id:
        logger.info("calendar: %s does not list %s (%s)", source, title, title_key)
        return False
    try:
        stored = await fill_one_gap(
            settings, source=Source(source), source_id=str(source_id), media=media,
            season=None if media is Media.MOVIE else season,
            endpoint_key=endpoint_key, tz=tz, now=now, exact_season=True)
    except SourceUnavailable as exc:
        logger.info("calendar: %s's airing of %s not written (%s)", source, title_key, exc)
        return False
    logger.info("calendar: %s answered %s for %s (%s); %s", source, source_id, title,
                title_key, "its airing stored" if stored else "no airing it could date")
    return bool(stored)


async def _describe(settings, hit, media: Media, tz: ZoneInfo) -> list[Elsewhere]:
    """One catalogue hit as the rows a reader can act on — one per season the
    source can date, or one for a film, or none at all.

    NO DATE MEANS NO ROW, and that is the honest refusal rather than a
    conservative one: the whole offer is "go to where this should be", and a
    season nobody can date has no where.

    THE RECORDS ARE BUILT BY THE SOURCE, THROUGH ITS OWN CALENDAR BUILDER. This
    module used to assemble them by hand out of the detail projection, and every
    field that projection quietly dropped became a bug of its own — see
    `DetailPort.records_for` for the list. Asking the source removes the class.

    THE SEASON LIST IS ALLOWED TO FAIL WITHOUT LOSING THE TITLE. A source that
    describes a show but will not enumerate its seasons still knows when the show
    first aired, and one row pointing at that is better than dropping a title the
    reader asked for by name.
    """
    leader = next(iter(hit.source_ids.items()), None)
    if leader is None:
        return []
    source, source_id = leader
    provider = providers.get(source)
    if provider is None or provider.detail_port is None:
        return []

    moments = []
    if media is not Media.MOVIE:
        moments = await _season_premieres(settings, provider, source,
                                          source_id, media)
    if not moments:
        whole = await _first_aired_of(settings, provider, media, source_id)
        moments = [(None, whole)] if whole else []
    if not moments:
        return []

    records = await _records_for(provider, settings, source_id, media,
                                 moments[:SEASONS_PER_SHOW])
    endpoint_key = _endpoint_for(media)
    rows = []
    for record in records:
        day = _local_day(record.air_ts, record.date_only, tz)
        rows.append(Elsewhere(
            item=render(record, tz), endpoint_key=endpoint_key,
            endpoint_label=get_endpoint(endpoint_key).label,
            year=day.year, month=day.month, day=day.isoformat(), record=record,
            source=str(source), source_id=str(source_id), media=str(media)))
    return rows


async def _records_for(provider, settings, source_id, media: Media, moments):
    """The source's own calendar records for these air times, ENRICHED.

    TWO STEPS BECAUSE TWO SOURCES ANSWER DIFFERENTLY, and both are the fill's own
    steps rather than this module's. A source whose calendar files carry every
    field returns a finished record and the second step does nothing; Simkl's
    carry a title, an id, a date and a poster, so its record arrives
    `enriched=False` exactly as a filled one does and the ordinary enrichment
    finishes it. Doing either by hand here is what produced a second record
    builder and the drip of one-field bugs behind it.
    """
    records = await provider.detail_port.records_for(
        settings, source_id, media, moments)
    return await calendar_enrich.enrich_now(settings, records, now=db.now())


async def _first_aired_of(settings, provider, media: Media, source_id) -> str:
    """When the TITLE itself first aired, as the source states it, or "".

    THE FALLBACK FOR A SHOW WHOSE SEASONS COULD NOT BE LISTED, and the only date
    a film ever has. Read off the description because that is the one place both
    sources put it.
    """
    try:
        described = await provider.detail_port.fetch_details(
            settings, media, source_id, None)
    except SourceUnavailable:
        return ""
    if not isinstance(described, dict):
        return ""
    raw = str(described.get("first_aired") or "").strip()
    # REFUSED RATHER THAN PASSED ON. A source can state a date this app cannot
    # read, and handing it to a record builder turns one unreadable field into a
    # failed search. `_as_moment` is the same reading the season list gets.
    return raw if _as_moment(raw) is not None else ""


async def _season_premieres(settings, provider, source, source_id,
                            media: Media) -> list[tuple[int, str]]:
    """`(season, premiere)` for every season this source can date, NEWEST FIRST,
    with the premiere left EXACTLY as the source spelled it — see `_as_moment`
    for why the difference between a day and an instant must survive this.

    NEWEST FIRST BECAUSE THAT IS WHAT A SEARCH IS USUALLY ABOUT. Somebody typing
    a title they have just heard of wants the season now airing far more often
    than the one from 2013, and `SEASONS_PER_SHOW` cuts from the far end — so
    the bound removes the least likely rows rather than an arbitrary tail.

    A FAILURE HERE IS NOT A FAILURE OF THE SEARCH. The caller falls back to the
    show's own first-air date, so a source that cannot enumerate seasons costs
    the reader precision, not the result.
    """
    try:
        answer = await provider.detail_port.fetch_seasons(settings, source_id, media)
    except SourceUnavailable as exc:
        logger.debug("search: %s would not list seasons for %s: %s",
                     source, source_id, exc)
        return []
    out = []
    for entry in getattr(answer, "seasons", None) or []:
        number = entry.get("season")
        raw = entry.get("first_aired")
        dated = _as_moment(raw)
        if number is None or dated is None:
            continue
        # THE SOURCE'S OWN SPELLING TRAVELS, and the parsed instant is only used
        # to order these. A record builder reads the string: a bare day means a
        # calendar day and a timestamp means an instant, and handing it a parsed
        # datetime would erase that difference — `str(datetime)` separates the
        # date from the time with a SPACE, which every "is there a T in it"
        # check in this codebase reads as "no time was given".
        out.append((int(number), str(raw), dated[0]))
    out.sort(key=lambda row: row[2], reverse=True)
    return [(number, raw) for number, raw, _moment in out]


def _as_moment(raw) -> tuple[datetime, bool] | None:
    """A season's premiere as `(instant, date_only)`, or None when it has none.

    `date_only` IS THE DIFFERENCE BETWEEN THE TWO SOURCES AND IT DECIDES A DAY.
    Trakt dates a premiere to the moment it airs — `2026-04-28T20:00:00.000Z` —
    which is a real instant and converts into a viewer's zone like any other.
    Simkl's season list carries a bare calendar day, because its own episode
    reader deliberately drops the time (see that package: Simkl expresses a whole
    file in one fixed offset, so the day is reliable and the instant is not).

    A BARE DAY MUST NOT BE CONVERTED, which is the rule `_local_day` already
    states: read as UTC midnight and pushed into a zone behind it, the 28th
    becomes the 27th. That is not hypothetical — it is the reported bug, and it
    came from truncating Trakt's instant to a day and then converting it anyway.
    So the shape of the value decides: a time means an instant, no time means a
    day, and a day is the same day everywhere.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    date_only = len(text) <= 10 or ("T" not in text and " " not in text)
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)), date_only


