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
from . import vocab
from .. import cache, db, providers
from ..endpoints import get_endpoint
from ..perftrace import span
# The catalogue fan-out and merge every searching feature shares, whose dedupe
# rules carry measured findings a second implementation would get wrong.
from ..providers import season_choices, title_search
from ..providers.base import (Item, Media, Record, Source, SourceUnavailable,
                              epoch_moment, proxied_image, render)

logger = logging.getLogger(__name__)

# HOW MANY MATCHING GROUPS ONE ENDPOINT MAY CONTRIBUTE. A group is a title, so
# this is a ceiling on titles rather than on airings — one title airing weekly
# for a year is one group and fifty rows.
GROUP_LIMIT = 40

# HOW MANY ROWS A VIEWER IS SHOWN. Beyond this a search is not answering the
# question they asked, and the honest response is to say the query was broad.
RESULT_LIMIT = 60

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
        return landing_url(self.endpoint_key, self.item)


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
    # What the box draws under a catalogue answer — title_search.SearchOutcome
    # .controls — or None when no catalogue was asked.
    controls: dict | None = None
    # The titles the catalogues answered with, as every box lists them (Found).
    found: tuple = ()


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
            # WHERE IT AIRS IS WHERE THE CALENDAR DRAWS IT (cache.placed): the
            # day the viewer's own first service gives, and one row per card
            # however many services list the episode.
            items, _narrowed = await calendar_cache.placed(
                groups, endpoint, tz=tz,
                **_filters(prefs, endpoint.media),
                prefs=source_selection.for_media(endpoint.media), settings=settings)
            for item in items:
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


def _filters(prefs, media) -> dict:
    """This viewer's filters for `media`, as the read path takes them — every
    narrowing `vocab.active_specs` answers except the network list, which the
    read path applies to rendered items rather than to records."""
    specs = vocab.active_specs(prefs, media, honour_pause=True)
    return {name: value for name, value in specs.items() if name != "network_filter"}


def landing_url(endpoint_key: str, item: Item) -> str:
    """The calendar, at the month `item` is drawn in, anchored on THAT CARD.

    THE DAY RIDES WITH THE MARK: a card past the days the page draws inline does
    not exist when the browser looks for #jump-target, so a mark alone landed at
    the top of the month — see Airing.url for what each part is for."""
    day = date.fromisoformat(item.air_date)
    return (f"/calendar?year={day.year}&month={day.month}"
            f"&endpoint={quote(endpoint_key, safe='')}"
            f"&highlight={quote(item.mark_key, safe='')}"
            f"&day={quote(item.air_date, safe='')}#jump-target")


def _endpoint_for(media: Media) -> str:
    """Which calendar a catalogue hit should be looked for on.

    THE PREMIERES CALENDARS, because that is what a title being searched for is
    most likely to be wanted on and it is the one a month-fill will actually put
    it on. A film has one calendar; a show is offered on the premieres one,
    which is where a title nobody has drawn yet turns up if it turns up at all.
    """
    return "movies" if media is Media.MOVIE else "shows/premieres"


@dataclass(frozen=True)
class Found:
    """One title a catalogue search answered with, as every box lists it: the
    merged hit, the medium it was searched as, and the reference a row names it
    by (`ref`: the query, the medium, the search's own SearchRef fields and its
    place in that answer) — what the season picker and the jump re-read it from.
    """
    hit: object
    media: str
    ref: dict
    # The hit's poster through the image proxy — the one image host the
    # Content-Security-Policy admits — made here, where it becomes something a
    # browser draws.
    poster: str = ""


async def catalogue(query: str, *, settings, media: tuple = (Media.SHOW, Media.MOVIE),
                    ref: title_search.SearchRef = title_search.SearchRef()) -> Results:
    """The titles the catalogues this viewer's search asks know about — THE SAME
    ROWS EVERY BOX DRAWS, the tracker's and the board's: every title each asked
    catalogue answered, SEARCH_PAGE_SIZE a page, with every service's mark, and
    "Also search" / "More from" under them (title_search.SearchOutcome.controls).

    ONLY ON AN EXPLICIT ASK. This is the half that spends requests, and it is
    reached from the search button or Enter rather than from typing.

THE MEDIUM OF THE CALENDAR BEING SEARCHED (`media`), so a box answers
    SEARCH_PAGE_SIZE titles a page as every other box does; both media only when
    the viewer asks to search every calendar, shows first, then films, and an
    added service's titles below the default's across both (`_default_first`).
    ONE SET OF CONTROLS for both, since the catalogues asked are the same for
    each: a catalogue's "more" stands while either medium has a later page.

    NOTHING IS DESCRIBED HERE. A row is a title, as on every box; opening it asks
    for its seasons (`picker`), which is where the dates, the viewer's filters and
    what the calendar already draws are read — so a search costs the services'
    search answers and nothing else, whatever they return.
    """
    needle = entries.fold_title(query)
    if len(needle) < entries.MIN_QUERY:
        return Results()
    found: list[Found] = []
    failed: set = set()
    more_from: set[str] = set()
    plan = None
    with span("search.catalogue", pages=len(ref.pages)) as sp:
        for medium in media:
            outcome = await title_search.search(settings, medium, query, ref)
            plan = outcome.plan
            failed |= set(outcome.result.failed)
            more_from |= set(outcome.result.more_from)
            for index, hit in enumerate(outcome.result.hits):
                found.append(Found(hit, str(medium), {
                    "q": query, "media": str(medium), **ref.fields(), "i": str(index)},
                    proxied_image(hit.poster) or ""))
        sp.set(found=len(found))
    combined = title_search.SearchMergeResult(hits=[], failed=frozenset(failed),
                                              more_from=frozenset(more_from))
    controls = title_search.SearchOutcome(combined, ref, plan).controls()
    return Results(found=tuple(_default_first(found, plan)), failed=frozenset(failed),
                   controls=controls)


def _default_first(found: list[Found], plan) -> list[Found]:
    """`found` with every service's rows in the order its catalogues are asked —
    the default's first, an added service's below, both media together — as
    every box draws them: what "also search" brings is appended rather than
    shuffled in above rows already being read. Stable, so each service's rows
    keep their own order (shows, then films). There is no relevance to rank
    across services by — TMDB and Simkl answer in their own order and carry no
    score."""
    order = [str(entry.source) for entry in plan.asked] if plan else []

    def rank(row: Found) -> int:
        leader = str(next(iter(row.hit.source_ids), ""))
        return order.index(leader) if leader in order else len(order)

    return sorted(found, key=rank)


@dataclass(frozen=True)
class Picker:
    """One title's choices for the calendar's season picker, and the rows they
    were drawn from — what the jump writes and lands on."""
    options: list
    rows: list[Elsewhere]


async def picker(settings, hit, media: Media, *, prefs, tz: ZoneInfo,
                 source_selection=None) -> Picker:
    """`hit`'s seasons — or a film's release — as the app's one season picker
    offers them (app/providers/season_choices.py), for the calendar's jump.

    DESCRIBED NOW, by the service that leads the hit, which is the cost the
    search no longer pays: a season list for the premieres and a description for
    the genres and certification the viewer's filters read.

    THE VIEWER'S OWN FILTERS APPLY HERE, every one the calendar applies, through
    the calendar's own (`_kept_by_filters`): a season the viewer's filters would
    hide is listed and refused, saying so — never offered as somewhere to go
    that the month it opens could not draw.

    A SEASON THE CALENDAR ALREADY DRAWS IS A LINK TO ITS DAY rather than a
    choice, and its airing from another service is completed on the way
    (`_complete_known_seasons`): the season is already on the calendar, so the
    stored airing joins the card it matches rather than drawing a new one.
    """
    rows = await _describe(settings, hit, media, tz)
    specs = vocab.active_specs(prefs, media, honour_pause=True)
    reasons: dict = {}
    links: dict = {}
    on_calendar: list[Elsewhere] = []
    for row in rows:
        season = row.item.season if media is not Media.MOVIE else None
        drawn = await _drawn(row.endpoint_key, row.item.mark_key, row.item.season, media,
                             prefs=prefs, source_selection=source_selection,
                             settings=settings, tz=tz)
        if drawn is not None:
            on_calendar.append(row)
        # THE FILTERS FIRST, a season the calendar holds included: a link to its
        # day would open a day that cannot draw it, which is what a filtered
        # season's link did — the month opened with nothing to find.
        if not (drawn if drawn is not None
                else await _kept_by_filters(row.record, media, specs, settings)):
            reasons[season] = "your filters hide it"
        elif drawn:
            # TO THE DAY THE CALENDAR DRAWS IT ON, which is this viewer's first
            # service's day for the episode — not the describing service's,
            # which may date it a day or a week differently.
            links[season] = landing_url(row.endpoint_key, drawn[0])
    await _complete_known_seasons(on_calendar)
    if media is Media.MOVIE or all(row.item.season is None for row in rows):
        # ONE CHOICE, NOT A SEASON: a film's release, or the premiere of a show
        # whose service would not list its seasons (`_describe` falls back to
        # the show's own first-air date rather than dropping the title).
        options = [season_choices.SeasonOption(
            None, premiere=date.fromisoformat(row.day),
            name="Release" if media is Media.MOVIE else "Premiere",
            held_as="on your calendar") for row in rows[:1]]
    else:
        options = season_choices.seasons_only(season_choices.season_options(
            [{"season": row.item.season, "first_aired": row.day} for row in rows],
            held=(), current=None, finished=None, now=datetime.now(timezone.utc),
            held_as="on your calendar"))
    return Picker(season_choices.blocking(options, reasons, links), rows)


async def _drawn(endpoint_key: str, title_key: str, season: int | None, media: Media, *,
                 prefs, source_selection, settings, tz: ZoneInfo,
                 written=()) -> list[Item] | None:
    """The cards THIS viewer's calendar draws for one season of a title (a film:
    its release) on `endpoint_key`, each on its own day, earliest first — [] when
    the calendar holds the season and the viewer's filters or sources hide it,
    None when it holds none.

    THE STORED RECORD, NOT THE DESCRIPTION, decides a title the calendar holds:
    the calendar draws the service that listed it, and that record can answer a
    filter differently from the describing service's. MEASURED on the owner's
    calendar: Sever (2026) is stored from Simkl with no country, so their
    US-only film filter hides the card, while TMDB's description says US — the
    picker offered it, and the jump landed on a day with nothing on it. So this
    runs the calendar's own read (`cache.placed`, with the viewer's source
    choice), the same confirmation the search's stored half makes — which also
    answers WHERE, the day the viewer's first service gives the episode.

    `written` NARROWS IT TO THE CARD THOSE RECORDS JOINED, for a jump that has
    just written them: a season two services date months apart is two cards,
    and the jump lands on the one it wrote into.
    """
    if not await entries.sources_airing(endpoint_key, title_key, season):
        return None
    records = [record for record in await entries.read_groups(endpoint_key, [title_key])
               if media is Media.MOVIE or record.season == season]
    groups = calendar_cache.group_records(records)
    named = {(str(r.source), str(r.id), float(r.air_ts)) for r in written}
    holding = [group for group in groups
               if any((name, str(stored.get("id")), float(stored.get("air_ts") or 0)) in named
                      for name, stored in group["by_source"].items())]
    endpoint = get_endpoint(endpoint_key)
    items, _narrowed = await calendar_cache.placed(
        holding or groups, endpoint, tz=tz, **_filters(prefs, endpoint.media),
        prefs=source_selection.for_media(endpoint.media) if source_selection else None,
        settings=settings)
    return sorted(items, key=lambda item: (item.air_date, item.air_ts))


async def _kept_by_filters(record, media: Media, specs, settings) -> bool:
    """Whether this viewer's filters keep a record a service has just described,
    for a title the calendar does not hold yet (`_drawn` answers one it does) —
    EVERY filter the calendar applies, through the calendar's own
    (cache.narrow_for_viewer): the release narrowing, the instance's floor and
    the viewer's genres, countries and certifications. No source admission: the
    calendar would draw another service's record of the title, and which
    service lists it is not a filter."""
    group = calendar_cache.group_records([record])[0]
    kept, _release, _resolved = calendar_cache.narrow_for_viewer(
        [(group, [record])], media, genres=specs["genres"], countries=specs["countries"],
        show_certifications=specs["show_certifications"],
        movie_certifications=specs["movie_certifications"],
        movie_release_countries=specs["movie_release_countries"],
        movie_release_types=specs["movie_release_types"], settings=settings)
    return bool(kept)


def _jump_writer(hit, endpoint_key: str):
    """Which of the services that found `hit` writes its airing onto
    `endpoint_key`: the first, in the hit's own order, whose calendar lists it
    (`_publishes`). None when none does — a catalogue that lists no calendar
    describes the title, and the jump opens the month."""
    for source, source_id in hit.source_ids.items():
        provider = providers.get(source)
        if provider is not None and provider.detail_port is not None \
                and _publishes(provider, endpoint_key) and source_id:
            return source, str(source_id)
    return None


async def jump(settings, hit, media: Media, seasons: list, *, tz: ZoneInfo,
               now: int, prefs=None, source_selection=None) -> str:
    """Write the airings the picker's ticked seasons name (a film: its release),
    and answer where to land — the first ticked one's card, on the day this
    viewer's calendar draws it. The title is the one the reference named, re-read
    from the stored search; nothing about it comes from the request.

    EACH SEASON IS WRITTEN BY A SERVICE THAT LISTS THE CALENDAR (`_jump_writer`),
    with `exact_season`: a season that service cannot date writes nothing rather
    than the show's own start (see fill_one_gap). One that no such service found
    writes nothing, and the jump still opens the month the season should be in.

    WHERE IT LANDS IS THE CALENDAR'S ANSWER (`_drawn`, through cache.placed), not
    the written airing's own date: a written airing joins any other service's
    listing of the episode, and the card is drawn on the day the viewer's first
    service gives — which can be another day, or another month, than the one
    just written. `prefs` (the viewer's filters) and `source_selection` are what
    that answer is read under.
    """
    rows = await _describe(settings, hit, media, tz)
    # The EARLIEST ticked season leads, as the picker draws them top to bottom;
    # a film, or a show with no season list, has its one row (season None).
    wanted = sorted((row for row in rows if row.item.season in seasons
                     or (media is Media.MOVIE and row is rows[0])),
                    key=lambda row: (row.item.season is not None, row.item.season or 0))
    if not wanted:
        return ""
    written: list[list] = []
    for row in wanted:
        writer = _jump_writer(hit, row.endpoint_key)
        if writer is None:
            written.append([])
            continue
        source, source_id = writer
        written.append(await fill_one_gap(
            settings, source=source, source_id=source_id, media=media,
            season=row.item.season if media is not Media.MOVIE else None,
            endpoint_key=row.endpoint_key, now=now,
            exact_season=media is not Media.MOVIE))
    first = wanted[0]
    # THE CARD IS NAMED WHETHER OR NOT ANYTHING WAS WRITTEN: the month may draw
    # the title from a service that lists it there, under the same identity this
    # description keys (the tmdb > tvdb > imdb > mal waterfall), and naming a
    # card the month does not draw marks nothing.
    drawn = await _drawn(first.endpoint_key, first.item.mark_key, first.item.season, media,
                         prefs=prefs or {}, source_selection=source_selection,
                         settings=settings, tz=tz, written=written[0])
    if drawn:
        return landing_url(first.endpoint_key, drawn[0])
    # NOTHING DRAWN — no service lists it here, or this viewer's filters hide
    # it: the month the description puts it in, as a search result's promise is
    # "go to where this should be".
    return landing_url(first.endpoint_key, first.item)


def _publishes(provider, endpoint_key: str) -> bool:
    """Whether `provider` lists the calendar `endpoint_key` names — THE TEST FOR
    WRITING ITS AIRING ONTO IT.

    A SERVICE WRITES ONLY TO A CALENDAR IT PUBLISHES. What this module stores is
    a service's own airing, as its calendar feed would have stored it, so a
    service with no feed for that calendar has nothing to write there: a
    catalogue that describes titles without listing a calendar (TMDB, today)
    would otherwise put rows onto one under its own name. Its search result still
    links to the month, which fills from the services that do list it. Asked by
    both writers here — `fill_one_gap`, which every followed result, tracker
    arrival and card completion passes through, and `_complete_known_seasons`.
    """
    return provider.calendar_port is not None and provider.capabilities.answers(endpoint_key)


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
        if not _publishes(providers.get(row.source), row.endpoint_key):
            continue
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


async def fill_one_gap(settings, *, source, source_id: str, media: Media,
                      season: int | None, endpoint_key: str, now: int,
                      exact_season: bool = False) -> list[Record]:
    """Write ONE title's airing into the calendar, returning what was written.

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

    Returns the records written — empty when nothing could be, which is not an
    error: the month is still worth opening. NOT WHERE THEY ARE DRAWN: a written
    airing joins any other service's listing of the episode, and which day the
    card lands on is the viewer's (cache.placed, asked by `jump`).

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
        return []
    if not _publishes(provider, endpoint_key):
        return []

    when = ""
    if season is not None and media is not Media.MOVIE:
        for number, premiere in await _season_premieres(
                settings, provider, source, source_id, media):
            if number == season:
                when = premiere
                break
    if not when:
        if exact_season and season is not None and media is not Media.MOVIE:
            return []
        # The title's own first-air date: what a film always uses, and what a
        # show falls back to when the season could not be dated.
        when = await _first_aired_of(settings, provider, media, source_id)
        season = None
    if not when:
        return []

    records = await _records_for(provider, settings, source_id, media,
                                 [(season, when)])
    if not records:
        return []
    await entries.store_loose_airings(
        endpoint_key, records, now=now, stale_after=now + TITLE_STALE_SECONDS)
    calendar_cache.forget_endpoint_spans(endpoint_key)
    return records


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
    calendar here and can be asked by shared id (providers.for_shared_id_lookup —
    not the search boxes' list, which a viewer shapes); and no answer from it in
    the last week.
    """
    if endpoint_key not in COMPLETABLE_ENDPOINTS or not title_key:
        return ""
    drawn = await entries.sources_airing(endpoint_key, title_key, season)
    if len(drawn) != 1:
        return ""
    on_calendar = {str(p.source) for p in providers.calendar_sources(settings=settings)
                   if p.capabilities.answers(endpoint_key)
                   and p.calendar_port.calendar_configured(settings)}
    for source, _port in providers.for_shared_id_lookup(settings):
        name = str(source)
        if name in drawn or name not in on_calendar:
            continue
        if await cache.get(_asked_key(name, endpoint_key, title_key, season),
                           OTHER_SERVICE_ASK_SECONDS) is None:
            return name
    return ""


async def complete_from_other_service(settings, *, source: str, endpoint_key: str,
                                      title_key: str, season: int | None,
                                      now: int) -> bool:
    """Ask `source` whether it lists the title, by shared id, and store its own
    airing of this season's premiere (or the film's release) on `endpoint_key`
    if it does. Returns whether an airing was stored.

    THE SAME TWO STEPS AS ELSEWHERE, NOT A THIRD WAY. The ask is the search port's
    exact lookup (find_by_shared_id — an answer is taken only when that service's
    own record states the id asked about), and the write is fill_one_gap, the
    search's own gap fill: that service's own record, built by its own builder,
    stored as its calendar feed would have stored it. So the airing joins this
    card whenever the two services' dates make one episode (cache.may_join), and
    stands as that service's own card when they do not, exactly as its calendar
    would draw it.

    THE IDS COME FROM THE STORE (entries.group_identity), never from the request:
    a write whose subject is a key the server resolves can only land on a title
    this instance already holds.

    AN ANSWER IS REMEMBERED FOR A WEEK, "not listed" included; a failure to ask
    is not, so a service that was down is asked again on the next open.
    """
    identity = await entries.group_identity(title_key)
    port = dict((str(s), p) for s, p in providers.for_shared_id_lookup(settings)).get(source)
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
            endpoint_key=endpoint_key, now=now, exact_season=True)
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

    records = await _records_for(provider, settings, source_id, media, moments)
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

    NEWEST FIRST, so a film's one row and a show's latest season lead; the
    season picker draws them in season order (season_choices.season_options).

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


