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

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import cache as calendar_cache
from . import entries
from .. import providers
from ..endpoints import get_endpoint
from ..perftrace import span
from ..providers.base import Item, Media, SourceUnavailable, render

logger = logging.getLogger(__name__)

# THE SHORTEST QUERY WORTH ASKING ABOUT. One character matches most of the
# calendar and answers nothing anybody wanted; two is where a substring starts
# to mean something. Enforced here rather than in the route so the typing path
# and the Enter path cannot disagree about it.
MIN_QUERY = 2

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
        """The calendar, at the month, scrolled to the day, with this card
        named so the page can pick it out.

        `#day-YYYY-MM-DD` IS A REAL ANCHOR ALREADY -- both the day block and the
        day fragment emit it, and the jump-to strip links to exactly this. It
        works for a day that has not been rendered yet, because the skeleton
        placeholder carries the same id and fetches itself when reached.
        """
        return (f"/calendar?year={self.year}&month={self.month}"
                f"&endpoint={quote(self.endpoint_key, safe='')}"
                f"&highlight={quote(self.item.mark_key)}#day-{self.day}")


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

    @property
    def url(self) -> str:
        """The MONTH, with no day and no highlight — see the module docstring.
        Arriving fills the month, which is the point: the calendar learns about
        the title, and if a source lists it there a card appears."""
        return (f"/calendar?year={self.year}&month={self.month}"
                f"&endpoint={quote(self.endpoint_key, safe='')}")


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
    moment = datetime.fromtimestamp(float(air_ts), timezone.utc)
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
    needle = entries.fold_title(query)
    if len(needle) < MIN_QUERY:
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
            kept, _narrowed = await calendar_cache.visible_records(
                groups, endpoint,
                genres=prefs["genres"], countries=prefs["countries"],
                show_certifications=prefs["show_certifications"],
                movie_certifications=prefs["movie_certifications"],
                movie_release_countries=prefs["movie_release_countries"],
                movie_release_types=prefs["movie_release_types"],
                prefs=source_selection, settings=settings)
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


async def catalogue(query: str, *, settings, tz: ZoneInfo,
                    known: frozenset[str]) -> Results:
    """Titles a registered catalogue knows about, described well enough to draw.

    ONLY ON AN EXPLICIT ASK. This is the half that spends requests, and it is
    reached from the search button or Enter rather than from typing.

    `known` IS THE MARK KEYS THE STORED HALF ALREADY ANSWERED FOR, so a title
    the calendar draws is not offered a second time as somewhere to go. The
    stored answer is strictly better — it names the day.

    ONE LOOKUP PER HIT AND THAT IS THE COST. A search hit carries no air date on
    either source measured (Trakt's search omits it, Simkl's has only a year), so
    the month a title belongs in has to come from a per-title lookup — which is
    also what makes the row drawable at all, since a hit carries no poster and
    Simkl's carries no overview either. Bounded by CATALOGUE_LIMIT.
    """
    needle = entries.fold_title(query)
    if len(needle) < MIN_QUERY:
        return Results()

    from ..distrakt import search as shared_search  # deferred: see DECLARED_EDGES

    asked = providers.for_catalogue_search(settings)
    if not asked:
        return Results()

    out: list[Elsewhere] = []
    failed: set = set()
    with span("search.catalogue", sources=len(asked)) as sp:
        for media in (Media.SHOW, Media.MOVIE):
            merged = await shared_search.search_catalogue(asked, settings, media, query)
            failed |= set(merged.failed)
            for hit in merged.hits[:CATALOGUE_LIMIT]:
                described = await _describe(settings, hit, media, tz)
                if described is None:
                    continue
                if described.item.mark_key in known:
                    continue
                out.append(described)
        sp.set(found=len(out))
    return Results(elsewhere=tuple(out[:CATALOGUE_LIMIT]),
                   failed=frozenset(failed))


async def _describe(settings, hit, media: Media, tz: ZoneInfo) -> Elsewhere | None:
    """One catalogue hit as a drawable row, or None when it cannot be placed.

    NO DATE MEANS NO ROW, and that is the honest refusal rather than a
    conservative one: the whole offer is "go to where this should be", and a
    title nobody can date has no where. It is shown by the stored half if this
    instance holds it and not at all if it does not.
    """
    provider = providers.get(hit.source)
    if provider is None or provider.detail_port is None:
        return None
    try:
        described = await provider.detail_port.fetch_details(
            settings, media, hit.source_id, None)
    except SourceUnavailable as exc:
        logger.debug("search: %s could not describe %s: %s", hit.source, hit.source_id, exc)
        return None
    if not isinstance(described, dict):
        return None
    moment = _first_aired(described)
    if moment is None:
        return None
    day = _local_day(moment.timestamp(), False, tz)
    record = _as_record(hit, described, moment)
    endpoint_key = _endpoint_for(media)
    return Elsewhere(item=render(record, tz), endpoint_key=endpoint_key,
                     endpoint_label=get_endpoint(endpoint_key).label,
                     year=day.year, month=day.month)


def _first_aired(described: dict) -> datetime | None:
    """The instant a described title first airs, or None.

    TOLERANT OF THE SHAPES THE TWO PACKAGES ACTUALLY EMIT — an ISO string with
    or without a zone — and refuses anything else rather than guessing. A guessed
    date sends a reader to the wrong month, which is worse than not offering.
    """
    raw = described.get("first_aired") or described.get("air_date") or ""
    text = str(raw).strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _as_record(hit, described: dict, moment: datetime):
    """A catalogue hit plus its description, as the Record a card draws from.

    IT IS A REAL `Record` AND NOT A LOOKALIKE, so the same template renders it
    and a field added to a card cannot quietly skip these rows. `enriched` is
    True because a per-title lookup is exactly what enrichment IS — this row is
    not waiting on one.
    """
    from ..providers.base import Record

    return Record(
        source=hit.source, media=hit.media, id=str(hit.source_id),
        ids=dict(hit.ids or {}), detail_url=str(described.get("homepage") or ""),
        title=hit.title or str(described.get("title") or ""),
        air_ts=moment.timestamp(),
        year=hit.year or described.get("year") or "",
        network=str(described.get("network") or hit.network or ""),
        runtime=described.get("runtime") or hit.runtime,
        status=str(described.get("status") or ""),
        rating=described.get("rating"),
        imdb_rating=described.get("imdb_rating"),
        genres=[str(g) for g in (described.get("genres") or [])],
        certification=str(described.get("certification") or ""),
        overview=str(described.get("overview") or hit.overview or ""),
        poster=str(described.get("poster") or ""),
        enriched=True,
    )
