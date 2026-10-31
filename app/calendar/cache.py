"""Global, UTC calendar cache and the read path over it.

Calendar data is the same for everyone, so it is cached once — per (endpoint,
7-day window) — and every viewer reads from the same rows. The design, all
locked by live measurement against the real Trakt API:

  - FETCH IN 7-DAY WINDOWS aligned to a fixed epoch, NOT to "today", so two
    viewers looking at the same month hit the same cache rows. A month view is
    five or six window reads, each cached and TTL'd independently.

  - STORE EACH SOURCE'S NORMALIZED RECORD AS ROWS. app/calendar/entries.py owns
    the airings and coverage, and the title store (app/metadata/) what each
    service says about a title; this module owns the window arithmetic and the
    read path over them, and a "window" is now a DATE RANGE rather than a stored
    object.
    Normalizing on the way IN is what lets a second source fill the same tables:
    a payload stored raw would have to be interpreted at read time by something
    that knows every source's field layout, and that something is exactly what a
    third source would then have to be added to. Nothing viewer-dependent may go
    in — the four local spellings of an air time are derived at READ, from
    `air_ts`, by app/providers/base.py's `render`.

    THE GROUPING HAPPENS AT READ, NOT AT FILL, and that is a correction rather
    than a move. Whether two records are one card depends on WHAT ELSE IS STORED
    (see match_keys: one episode's every listing decides its units, and an
    uncoordinated record folds into one only when exactly one candidate exists),
    so storing the answer froze a judgement made against whatever happened to be
    fetched beside it, and a later fill supplying the missing listing could not
    revise it.

    AND IT IS NOT DONE WINDOW BY WINDOW. A window is the unit a FILL works in;
    a card is ONE EPISODE whichever services list it (`may_join`), so a read
    makes its windows current one by one and then reads its airings once, whole
    — every listing of every episode it holds, at any date — and draws each card
    on the day the viewer's own service gives it (`placed`).

    `asked` NAMES WHO WAS REACHED FOR AND `answered` NAMES WHO REPLIED — two
    columns of `calendar_coverage` — and the gap between them is the only thing
    that means "incomplete": a span stored while one source was failing must not
    be served for a whole TTL as though that source had been asked and was empty.
    One gap is not a failure: a source still WORKING ITS ANSWER OUT behind the
    request (base.SourcePending — TMDB's derived season premieres) is asked and
    not yet answered, and that source's share is filled on its own once the
    answer is ready (`fill_source`).
    A source in NEITHER is not in play for that span, which is not a failure; it
    makes the span a MISS to be refilled. Coverage is a table of its own because
    "this source returned no rows" and "this source was never asked" are the same
    absence in the airings table and completely different facts.

    THE FILL ASKS EVERY SOURCE IN PLAY AND STORES THE UNION. No viewer's source
    selection reaches it; whose service a person reads is answered per group at
    READ (app/calendar/resolve.py), over these same shared rows, for exactly the
    reason the per-viewer genre filter is: whoever happened to trigger a fill
    must not decide what everybody else is served for that week.

  - `genres`/`countries`/`show_certifications`/`movie_certifications`/
    `network_filter` ARE NOT SENT TO A SOURCE AS QUERY PARAMS. They are the
    instance-wide content floor — everything the Settings screen's content-floor
    section holds — and EVERY ONE OF THEM APPLIES TWICE, which is one more time
    than it reads:
      at FETCH, so an excluded item never enters storage at all and no
      per-account filter can bring back what was never stored;
      and at READ, before the per-viewer layer, so that CHANGING the floor
      reaches the windows ALREADY stored instead of leaving them to age out
      silently at their own TTL.
    The fill's copy is what makes the exclusion hard; the read's copy is what
    makes a change to it immediate. The network list is filtered on the record's
    own `network` at fill and on the rendered item's at read — the same field
    either side of normalization.
    Every OTHER filter dimension — a signed-in viewer's own
    genre/country/certification/network choices — is a separate, read-time layer
    applied per viewer over those same floored rows, after the floor.

  - TRIM EACH WINDOW TO ITS OWN 7 DAYS. The request is the documented shape
    (/calendars/{target}/{path}/{start_date}/{days}), but Trakt treats `days` as
    a floor, not a ceiling — measured live, a 7-day window came back carrying
    entries two months past its end — so neighbouring windows overlap heavily.
    Without the trim a month read concatenates those overlaps and renders the
    same episode two or three times (see in_window / dedupe_records).

The calendar no longer shares `api_cache` with the detail lookups: it has its own
tables and its own retention, and what is left in that table is the per-title
lookups the providers make. THE READ PATH — read_month plus the window helpers
below — is what the authenticated calendar route and the public share pages both
call: pass allow_fetch=False on a share page and it serves whatever is stored
(even stale, even empty) and never asks a source.

THIS MODULE NAMES NO SOURCE. It asks the registry which sources can fill a window
and calls them through their `calendar_port`; which service that is, and how it
spells its own payload, is that provider package's business and nothing here
depends on it.
"""
from __future__ import annotations

import asyncio
import calendar as _calendar
import functools
import logging
import time
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import anyio.to_thread

from . import enrich as calendar_enrich
from . import entries
from . import filter as calendar_filter
from . import resolve as calendar_resolve
from . import state as calendar_state
from . import vocab
from .. import db
from .. import providers
from ..media import artwork
from ..metadata import identity as title_identity
from ..perftrace import activity, detach, job, span
from ..endpoints import ENDPOINTS, Endpoint
from ..providers.base import (
    Item, Media, Provider, Record, Source, SourceNotModified, SourcePending,
    SourceUnavailable, epoch_moment, render, resolve_key)

logger = logging.getLogger(__name__)
# Same "app.perf" logger the Trakt transport's cached_get already uses for its own
# netGET/cacheHIT lines — one DEBUG channel for every outbound Trakt call,
# regardless of which module made it. Enable it with LOG_LEVEL=DEBUG.
_perf = logging.getLogger("app.perf")

WINDOW_DAYS = 7

# A fixed reference point the 7-day windows tile out from. Any fixed date works;
# a Monday is chosen so a window starts on a Monday, which reads naturally. What
# matters is only that it never depends on "today", so every viewer's month maps
# to the same window rows.
_EPOCH = date(2001, 1, 1)  # a Monday


def window_start(day: date) -> date:
    """The start date of the fixed 7-day window containing `day`."""
    offset = (day - _EPOCH).days
    return _EPOCH + timedelta(days=(offset // WINDOW_DAYS) * WINDOW_DAYS)


def aligned_windows(range_start: date, range_end: date) -> list[date]:
    """Every aligned window start covering [range_start, range_end] inclusive."""
    start = window_start(range_start)
    out: list[date] = []
    current = start
    while current <= range_end:
        out.append(current)
        current += timedelta(days=WINDOW_DAYS)
    return out


def record_utc_date(record: Record) -> date:
    """The UTC calendar day a record airs on. The windows are UTC-aligned, so
    this is the only date that decides which window owns it — a viewer's local
    day is a read-time question and is asked much later."""
    return epoch_moment(record.air_ts).date()


def in_window(record: Record, start: date) -> bool:
    """Whether a record belongs to the 7-day window beginning `start`.

    NEEDED BECAUSE TRAKT OVERRUNS THE `days` IT IS GIVEN. The request shape is
    exactly the documented one — /calendars/{target}/{path}/{start_date}/{days} —
    and `days` is honoured as a floor but not as a ceiling. Measured live against
    /calendars/all/shows/ from 2026-07-06:

        days=1  ->   89 entries spanning 4 days
        days=3  ->  206 entries spanning 6 days
        days=7  ->  404 entries spanning 17 days, out to 2026-07-27
        days=14 ->  793 entries spanning out to 2026-09-05

    Every show endpoint does it (new, premieres, finales, shows); movies happened
    to come back clean, which is a small dataset rather than a promise. The
    `end_date` query filter does NOT constrain it — same 404 entries, same span —
    so there is no server-side way to ask for less.

    What IS reliable: the response never starts before `start_date`, and always
    covers the range asked for. So the window owning a date always returns that
    date, and trimming the rest is lossless — verified by count on a real month
    (1691 cards with 207 duplicates -> 1484, exactly the duplicates removed).

    Windows tile contiguously, so every UTC date falls in exactly one, and an
    entry Trakt handed to the wrong window is one an adjacent window also
    returns. Without this trim a month read concatenates those overlaps and
    renders the same episode two or three times.

    Trimmed on the record's own instant, which is also what the card is drawn
    from. Checked live across every endpoint: the top-level `first_aired` never
    disagrees with `episode.first_aired`.
    """
    day = record_utc_date(record)
    return start <= day < start + timedelta(days=WINDOW_DAYS)


def record_identity(record: Record) -> tuple:
    """What makes two records the same airing FROM THE SAME SOURCE.

    The immutable source id rather than the slug (a slug is user-changeable),
    plus the episode coordinates and the air time — a show legitimately appears
    many times in a month, and only the same episode at the same moment is a
    repeat. The SOURCE is part of it because two services describing one airing
    are two records that must both survive to be merged; collapsing them here
    would throw one away before anything could compare them.
    """
    ids = record.ids or {}
    return (
        str(record.source),
        ids.get("trakt") or ids.get("simkl") or ids.get("slug") or record.id,
        record.season,
        record.episode_number,
        record.air_ts,
    )


def dedupe_records(records: list[Record]) -> list[Record]:
    """First occurrence of each airing, order preserved."""
    seen: set[tuple] = set()
    out: list[Record] = []
    for record in records:
        identity = record_identity(record)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# grouping — one entry per real title, each source's record kept whole
# ---------------------------------------------------------------------------
#
# THERE IS NO PRUNING ANY MORE, and its disappearance is the point rather than a
# side effect. Pruning existed to whitelist which of a raw payload's fields were
# worth storing; a Record already IS that whitelist, per source, by construction.
# The whitelist also carried a trap a second source would have walked straight
# into: it named the id keys it kept, so any id namespace it had not been taught
# about — a second service's own id, an anime title's `mal` — would have been
# silently dropped on the way in, and the symptom would have been a matcher that
# never matched.


def group_base(record: Record) -> str:
    """The part of a group key that names the TITLE, with no airing in it.

    The cross-source waterfall (app/providers/base.py's resolve_key), never a
    service's own id — keying on one of those would make the same title arriving
    from two services two rows for ever. It stringifies whatever id it lands on,
    which is the whole of this app's defence against the type mismatch that
    matters most here: Simkl reports a tmdb id as the string "285652" and Trakt
    reports the same one as the int 285652, and a comparison that did not coerce
    would match NOTHING while looking exactly like sparse coverage from one
    service. Nothing downstream may compare a raw id value for identity.

    A TITLE THE WATERFALL CANNOT KEY GETS A PER-SOURCE BASE and so can never
    merge with anything. That is deliberate: a visible duplicate is safer than a
    wrong merge, and a title nobody can name in a shared id space is one there is
    no honest way to recognize. The title store files such a title under the same
    spelling (app/metadata/identity.py's per_source_key).

    A STORED RECORD CARRIES ITS TITLE'S SHARED IDS AS WELL AS ITS SERVICE'S, put
    there by the read (entries._record_from_row) — so this answers the title
    store's key, and two services' listings of one title share it even when one
    of them names the title only by a lower id.
    """
    identity = resolve_key(record.media, record.ids)
    return str(identity) if identity is not None else title_identity.per_source_key(
        record.source, record.id)


def episode_coords(record: Record) -> tuple[int, int] | None:
    """(season, episode) when a record states BOTH, else None.

    A THIRD OF PREMIERE ENTRIES STATE NEITHER, measured over July and August:
    1230 of them carry no season or no episode number, overwhelmingly Simkl
    anime, where a season is simply not part of how the entry is spelled — an
    absolute episode number and nothing else — plus a scattering of Trakt records
    that give a season and no number. So "the coordinates" is not a field a
    matcher may assume it has, and None here is the ordinary case rather than a
    malformed one. What such a record can still say is handled by `match_keys`
    below.
    """
    if record.season is not None and record.episode_number is not None:
        return (record.season, record.episode_number)
    return None


def episode_of(record: Record, base: str | None = None) -> tuple | None:
    """WHICH EPISODE a record lists, as something two services can agree on: the
    title's base and (season, episode) for a show stating both, the base alone
    for a film — a film is one title on the film calendar, and its release is
    the one airing it has — and None for a show record that states no full
    coordinate, which can only be joined to an episode by its day (`match_keys`).
    `base` is the record's `group_base`, for a caller that has worked it out.
    """
    base = group_base(record) if base is None else base
    if str(record.media) == "movie":
        return (base,)
    coordinate = episode_coords(record)
    return (base, coordinate) if coordinate is not None else None


# HOW FAR APART, ACROSS A MONTH'S EDGE, TWO SERVICES' DATES FOR ONE EPISODE MAY BE
# AND STILL BE ONE CARD. Inside one calendar month any distance joins.
JOIN_ACROSS_MONTHS_DAYS = 7


def may_join(first: date, second: date) -> bool:
    """Whether two listings of ONE episode, on these UTC days, may be one card:
    the same calendar month, or at most a week apart across a month's edge.

    THE RULE IS ABOUT THE CALENDAR A READER SEES, NOT ABOUT STORAGE. It used to
    be the storage's: listings were matched inside one seven-day window, and the
    only repair across a window's edge joined halves at most a day apart. So the
    same episode drew as two cards whenever two services dated it either side of
    a Monday — MEASURED: Yellowjackets S04E01, which Simkl dates 21 November
    01:00 UTC and Trakt 23 November 02:00 UTC (a Monday), drew on both days. On
    the dev instance 30 episodes on All Episodes, 3 premieres and 4 films drew
    twice that way.

    A MONTH, BECAUSE THAT IS THE PAGE. Two services disagreeing about which day
    of the month an episode airs on still mean the same episode on the same
    page, and one card drawn on the day the viewer's own service gives is the
    answer that cannot be confused with a second airing. Across the page's edge
    a week is admitted, so a premiere dated the 31st by one service and the 2nd
    by another is still one card. FURTHER APART THAN THAT STAYS TWO, and those
    are not one airing said twice: they are numbering disputes — Dimension 20's
    season 29 premiere is 8 April on Trakt and 16 September on Simkl — where two
    cards in two months is the honest drawing of two services' disagreement.
    """
    return ((first.year, first.month) == (second.year, second.month)
            or abs((first - second).days) <= JOIN_ACROSS_MONTHS_DAYS)


_SOURCE_RANK = {str(source): rank for rank, source in enumerate(Source)}


def _listing_order(record: Record) -> tuple:
    """Date order, and a total one: a tie on the instant goes to the registry's
    declared order and then the service's own id, so the same listings make the
    same units however a reader happened to fetch them."""
    return (float(record.air_ts), _SOURCE_RANK.get(str(record.source), len(_SOURCE_RANK)),
            str(record.id))


def join_listings(listings: list[Record]) -> list[list[Record]]:
    """ONE EPISODE's listings — every service's records of it — as the units a
    calendar draws, each unit one card. Pure, and deterministic in its input.

    IN DATE ORDER, A LISTING JOINS THE CURRENT UNIT when its service is not in
    it yet and it may join (`may_join`) EVERY listing already in it; otherwise
    it starts the next unit. EVERY, not any: a unit is one airing, so each of
    its listings has to be close enough to each other one, and a chain of
    neighbours a month long would be several airings glued together.

    ONE LISTING PER SERVICE PER UNIT. A service listing one episode twice is a
    repeat the calendar has always drawn twice, and folding a service into its
    own other listing would collapse it rather than reconcile two services.

    VIEWER-INDEPENDENT, WHICH IS WHY IT MAY RUN ANYWHERE. Which units an episode
    makes depends on what the services said and nothing else; which of a unit's
    listings describes it, and so which DAY it is drawn on, is the viewer's
    (app/calendar/resolve.py).

    HANDED EVERY STORED LISTING OF THE EPISODE, NEVER A RANGE'S SHARE OF THEM.
    The scan is in date order and each unit's membership depends on the one
    before it, so a listing far outside a reader's range can still decide where
    a unit inside it closes. A month, a day, a search and a share page all read
    an episode whole (entries.read_span_whole, entries.read_groups), which is
    what makes them draw the same cards on the same days by construction.
    """
    return _units(listings, {id(record): record_utc_date(record) for record in listings})


def _units(listings: list[Record], day_of: dict[int, date]) -> list[list[Record]]:
    """`join_listings` with each listing's UTC day already worked out (`day_of`,
    by the listing's id) — the matcher works out every record's once."""
    units: list[list[Record]] = []
    for record in sorted(listings, key=_listing_order):
        day = day_of[id(record)]
        current = units[-1] if units else None
        if (current is not None
                and all(str(held.source) != str(record.source) for held in current)
                and all(may_join(day_of[id(held)], day) for held in current)):
            current.append(record)
        else:
            units.append([record])
    return units


def match_keys(records: list[Record]) -> list[str]:
    """The final group key for each of `records`, in the same order — THE
    MATCHER: which records are one card.

    TWO RULES, ONE FOR EACH KIND OF RECORD.

    A RECORD THAT NAMES AN EPISODE (`episode_of`: a full coordinate, or a film)
    is grouped with that episode's other listings by `join_listings`, whatever
    their dates — the first unit of an episode is keyed `base|season|episode`
    (a film's, `base`) and each later one carries a repeat marker (`#2`), which
    is what keeps a numbering dispute months apart, or a service's own second
    listing, its own card.

    A RECORD THAT DOES NOT is folded into a unit of the SAME TITLE when exactly
    one episode can be the airing it means. TWO SOURCES CAN AGREE PERFECTLY
    ABOUT THE ID AND STILL FAIL TO MATCH, because they describe the airing at
    DIFFERENT RESOLUTIONS. Measured on real stored windows: one service lists an
    anime premiere as episode 1 with no season at all and the other lists it as
    S01E01, same title, same tmdb id, same day. The conditions:

      - same base (the same title in the same shared id space),
      - a listing of that episode on the same UTC calendar day, because a show
        airing twice in one week is two airings and nothing here may guess which
        one an uncoordinated record is,
      - and agreement on whichever half of the coordinate the record DID state.
        This is the condition that carries most of the weight: one live window
        holds eight uncoordinated records for a single title on one day, one per
        episode, against that title's eight coordinated ones — day alone would
        make all eight ambiguous and match none of them, while the stated episode
        number picks each one out exactly.

    AMBIGUITY IS COUNTED BEFORE ANYTHING ELSE NARROWS IT, AND THAT ORDER IS THE
    WHOLE SAFETY ARGUMENT. Every episode of the title listed that day is a
    candidate, whoever listed it; only if there is EXACTLY ONE is the record
    folded in, and only then is it asked whether the unit it would join already
    carries a record from this same source — in which case it is refused too,
    because folding a service's uncoordinated listing into its own coordinated
    one would be collapsing that service's listing rather than reconciling two
    of them. Counting the other way round — narrowing by source and THEN
    counting — is a real trap and not a theoretical one: on a day where one
    service lists season 4 and the other season 5 of the same show, it would
    leave exactly one survivor for an uncoordinated third record and merge it
    into the wrong season, silently.

    ZERO CANDIDATES OR SEVERAL, AND THE RECORD KEEPS A KEY OF ITS OWN — its
    title and its day — and renders as its own card. That is the direction this
    has to fail in: a visible duplicate is a cosmetic complaint, and a wrong
    merge hides one title behind another with nothing on the page to say so. Its
    DAY and not the storage week it was filed in: no live week holds two
    services' unfolded records of one title (measured on the dev instance), and
    keying on the day is what keeps this rule the same however a reader's range
    falls across the weeks.

    WHAT THIS DELIBERATELY DOES NOT DO is match on title and day. Two different
    SEASONS of one show premiering on the same day is a real thing — three live
    examples, all with both records fully coordinated — and any rule of the form
    "same title, same day, one card" destroys them. They are untouched here
    precisely because both sides name a different episode.
    """
    return _matched(records)[0]


def _matched(records: list[Record]) -> tuple[list[str], list[str]]:
    """`match_keys`, with each record's `group_base` beside its key — the title
    each card is of, which the matcher works out on the way."""
    keys: list[str] = [""] * len(records)
    # EACH RECORD'S TITLE AND DAY, WORKED OUT ONCE: both are asked of a record
    # three or four times below, and a month holds tens of thousands of them.
    bases = [group_base(record) for record in records]
    day_of = {id(record): record_utc_date(record) for record in records}
    episodes: dict[tuple, list[int]] = {}
    for index, (record, base) in enumerate(zip(records, bases)):
        episode = episode_of(record, base)
        if episode is not None:
            episodes.setdefault(episode, []).append(index)

    # Each episode's units, and for every (title, day) the units that have a
    # listing on it — the only things an uncoordinated record can be folded into.
    position = {id(record): index for index, record in enumerate(records)}
    on_day: dict[tuple[str, date], dict[str, dict]] = {}
    for episode, members in episodes.items():
        base = episode[0]
        stem = base if len(episode) == 1 else f"{base}|{episode[1][0]}|{episode[1][1]}"
        for number, listings in enumerate(_units([records[i] for i in members], day_of), 1):
            unit = {"key": stem if number == 1 else f"{stem}#{number}",
                    "coordinate": episode[1] if len(episode) > 1 else None,
                    "sources": {str(r.source) for r in listings}}
            for record in listings:
                keys[position[id(record)]] = unit["key"]
                on_day.setdefault((base, day_of[id(record)]), {})[unit["key"]] = unit

    for index, record in enumerate(records):
        if keys[index]:
            continue
        base, day = bases[index], day_of[id(record)]
        candidates = [unit for unit in on_day.get((base, day), {}).values()
                      if unit["coordinate"] is not None
                      and (record.episode_number is None
                           or unit["coordinate"][1] == record.episode_number)
                      and (record.season is None or unit["coordinate"][0] == record.season)]
        coordinates = {unit["coordinate"] for unit in candidates}
        if len(coordinates) == 1 and str(record.source) not in candidates[0]["sources"]:
            candidates[0]["sources"].add(str(record.source))
            keys[index] = candidates[0]["key"]
        else:
            keys[index] = f"{base}@{day.isoformat()}"
    return keys, bases


def group_records(records: list[Record]) -> list[dict]:
    """The read path's groups: one per card, each source's record kept whole
    under its own name.

    PROVENANCE IS RECORDED FOR EVERY FIELD, not only for the ones that disagree,
    because "both sources agreed" and "only one source had it" must be able to
    render differently and the second is not recoverable from a de-duplicated
    blob.

    `ids` IS HOISTED onto the group because it is the MATCH RESULT rather than
    any one source's value, and it is what the arr/Seerr buttons, the tracker and
    the ranker read. First writer wins per namespace, which with records visited
    in date order and then the declared order means the earlier listing's
    spelling of an id is the one kept.

    EACH SOURCE'S RECORD IS HELD AS ITSELF, not as `Record.to_dict()`: the group
    is a read's own structure and never stored, and turning every record into a
    dict here and back into a Record at resolution cost a month a second and a
    half for nothing (MEASURED, profiled). Nothing writes into a record a group
    holds.

    A KEY COLLIDING WITH ITSELF FOR ONE SOURCE is a repeated airing of a record
    that names no episode — the same title listed twice on one day — and gets a
    distinct key rather than overwriting, because the calendar has always drawn
    both and this is not the place to decide it should stop. `match_keys` never
    puts one service twice into an episode's unit, so this is the only way two
    records of one source can meet here.

    WHICH RECORDS SHARE A KEY IS `match_keys`' ANSWER, NOT THIS FUNCTION'S, and
    it is only as good as the records handed to it: an episode's units are
    decided by ALL its listings, so a caller grouping part of them may make
    different cards from a reader holding the rest. Every read path hands over
    an episode whole (entries.read_span_whole, entries.read_groups).
    """
    index: dict[str, dict] = {}
    out: list[dict] = []
    keys, titles = _matched(records)
    for record, base, title in zip(records, keys, titles):
        name = str(record.source)
        key, bump = base, 1
        while key in index and name in index[key]["by_source"]:
            bump += 1
            key = f"{base}#{bump}"
        group = index.get(key)
        if group is None:
            # `base` is the TITLE the card is of — `group_base` of its records,
            # every one the same title by the matcher's rule — kept so that what
            # reads a group by its title need not work it out again.
            group = {"key": key, "base": title, "ids": {}, "by_source": {}}
            index[key] = group
            out.append(group)
        group["by_source"][name] = record
        for namespace, value in (record.ids or {}).items():
            if value not in (None, ""):
                group["ids"].setdefault(namespace, value)
    return out


class CachedWindow(NamedTuple):
    """One stored window: its groups, who was ASKED for them, and who ANSWERED.

    THE TWO LISTS ARE DIFFERENT FACTS AND CONFLATING THEM IS WHAT MADE THE
    "incomplete data" banner permanent. A source in `asked` but not in `sources`
    was reached for and could not answer — that is the only thing "partial"
    should ever mean. A source in neither was not in play when this window was
    filled: either it did not exist on the instance yet, or its declared reach
    does not cover this window at all. That is not a failure to report, it is a
    window that predates the question — and the read path treats it as a MISS to
    be refilled, exactly as a payload from an older shape version is a miss
    rather than an error.

    Storing only "who answered" left the reader with no way to tell those two
    apart, so it had to guess by measuring TODAY'S sources against a window
    filled possibly weeks ago; every window then read as partial the moment a
    source was added, and a window outside a source's reach read as partial for
    ever, because refilling it changed nothing.
    """
    groups: list[dict]
    sources: tuple[str, ...]
    asked: tuple[str, ...] = ()


class WindowCoverage(NamedTuple):
    """One window's coverage and nothing else: who ANSWERED and who was ASKED —
    the two facts `CachedWindow` explains — and when it was stored, None for a
    window nothing has filled.

    WHAT A SPAN READ ASKS OF EACH WINDOW IT COVERS, because the airings are no
    longer read window by window: whether two services' listings are one card
    depends on dates either side of any window's edge, so the span's airings are
    read once, whole (`_assembled_span`), and a window contributes only whether
    it is fresh, partial or missing.
    """
    sources: tuple[str, ...]
    asked: tuple[str, ...]
    cached_at: int | None


def _poster_sighting(record: Record):
    """(media, tmdb, source, url) for one record, or None when it lacks either
    id — the tuple the ranker's poster registry is keyed on."""
    tmdb_id = (record.ids or {}).get("tmdb")
    if not tmdb_id or not record.poster:
        return None
    try:
        return (str(record.media), int(tmdb_id), str(record.source), record.poster)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# fetch + store + read of one window
# ---------------------------------------------------------------------------
#
# THE WINDOW IS NO LONGER A STORED OBJECT. What used to live here was a versioned
# compressed envelope -- the finished groups, who answered, who was asked -- with
# a codec either side of it and a PAYLOAD_VERSION whose job was to make every
# stored row a miss whenever the shape or the matching rule moved. All of it is
# gone with the blob: app/calendar/entries.py stores each source's records as
# rows, and a shape change there is a migration rather than a version byte.
#
# The one thing worth carrying forward is WHY that version existed, because the
# reasoning still binds the new shape: a month spans five or six windows, so a
# mixture of two matching rules would show one airing merged in the window that
# had been refilled and split in the one that had not, on the same page. Nothing
# may leave the stored calendar half-converted.


def _covers(provider, start: date) -> bool:
    """Whether a source's declared reach overlaps this window at all.

    ASKED, NEVER ASSUMED. A source whose calendar only publishes a rolling window
    cannot answer for a month three years ago, and `Capabilities.covers` is where
    that fact is declared — so no route and nothing here learns a date range
    belonging to a particular service.
    """
    capabilities = provider.capabilities
    return (capabilities.covers(start)
            or capabilities.covers(start + timedelta(days=WINDOW_DAYS - 1)))


def month_covered(provider, year: int, month: int) -> bool:
    """Whether ANY day of {year, month} falls inside `provider`'s declared
    reach — the same question `_covers` asks per WINDOW, asked here per whole
    month, for the one caller that needs a month-shaped answer: a route
    deciding whether a source picked deliberately (not merely admitted by
    'auto') can say anything at all about the month a viewer asked for. Public,
    unlike `_covers`, because that caller lives outside this module.
    """
    days = _calendar.monthrange(year, month)[1]
    capabilities = provider.capabilities
    return capabilities.covers(date(year, month, 1)) or capabilities.covers(date(year, month, days))


def _window_sources(endpoint: Endpoint, settings, start: date) -> list[Provider]:
    """Which sources are IN PLAY for this (endpoint, window) — the one answer
    both the fill and the completeness check read.

    ONE FUNCTION BECAUSE TWO NEARLY-IDENTICAL LINES DRIFTED APART AND THE
    DIFFERENCE WAS INVISIBLE. The fill skipped a source that does not publish
    this endpoint OR whose declared reach misses this window; the reader, asking
    the same question somewhere else, applied only the first of those two tests.
    A window outside a source's reach was therefore expected from it, never
    asked, never recorded, and reported as incomplete on every load for ever —
    with a refill changing nothing, because the fill went on skipping it. Any
    condition deciding "should this source have something to say here" belongs in
    this function and nowhere else.

    NO VIEWER'S PREFERENCE REACHES THIS. The window it describes is stored once
    per (endpoint, week) and served to everybody, so narrowing the fill to what
    one viewer asked for would write that viewer's exclusion into what every
    other viewer then reads as the truth for that week. Which sources a PERSON
    sees is a read-time question, answered per group in
    app/calendar/resolve.py. What `settings` still governs here is the
    operator's, not a viewer's: whether a source contributes to this instance's
    calendar at all. That one IS applied at the fill, and belongs there for the
    same reason the content floor does — it is one decision taken for every
    viewer at once, so honouring it here costs the instance nothing it will ever
    show. It is applied at READ as well (app/calendar/resolve.py), because a
    window filled before the operator changed their mind still holds the records
    they just switched off.

    A SOURCE THIS INSTANCE CANNOT READ AT ALL IS NOT IN PLAY, and leaving it in
    was what made every window on a single-source instance report itself
    incomplete. `calendar_sources` deliberately does not ask about credentials —
    its subject is which sources COULD publish this endpoint — so the fill asked
    a source with no client id, got a refusal, and stored a window that named it
    as asked-but-silent. The read path then marked every one of those windows
    partial, on every day of every month, for ever: the "some calendar data
    couldn't be loaded" banner on an instance where nothing had gone wrong and
    nothing ever would.
    `CalendarPort.calendar_configured` is the question that separates them —
    can this instance read THIS source's calendar — and asking it here is what
    keeps "partial" meaning a source that FAILED. It also keeps the recovery
    working, without a special case: filling a credential in puts that source
    back in play, `ensure_window` sees it in `in_play` and absent from the stored
    window's `asked`, and refills for exactly the reason that rule exists.
    """
    return [p for p in providers.calendar_sources(settings=settings)
            if p.capabilities.answers(endpoint.key) and _covers(p, start)
            and p.calendar_port.calendar_configured(settings)]


async def fetch_window_records(endpoint: Endpoint, settings, start: date, *,
                               covered=()
                               ) -> tuple[list[Record], list[str]]:
    """Ask every source in play for this window what airs in it, and return
    (records, the sources that ANSWERED), floor-filtered, trimmed and de-duped.

    THE FETCH BELONGS TO THE SOURCE AND THE SHAPE BELONGS HERE: this module knows
    what a cached window has to look like and nothing about how to ask for one,
    which is what lets a second source reuse this cache instead of the cache
    growing a branch per source.

    A SOURCE THAT REFUSES IS LEFT OUT OF THE ANSWER RATHER THAN FAKED AS EMPTY.
    Storing a window that claims a source contributed nothing would serve that
    lie for the whole TTL; leaving it out of `sources` says "unknown", which the
    read path can mark partial. Only when every source that was asked refused
    does this raise — there is genuinely nothing to store, and the caller then
    falls back to the stale copy it may already have.

    The trim is not tidiness. A source may treat the `days` bound as a floor
    rather than a ceiling (see in_window), so consecutive windows overlap by days
    or weeks; storing what arrived would mean caching the same airings several
    times over and handing the page duplicate cards for every one of them.

    A SOURCE STILL WORKING ITS ANSWER OUT (base.SourcePending) is neither: it is
    left out of the answer, as a refusal is, and remembered as PENDING
    (`_note_pending`), which the store that follows this turns into a fill of
    that source's share alone once its answer is ready (`_follow_pending`).
    """
    records, answered, unchanged, pending = await _answers(
        _window_sources(endpoint, settings, start), endpoint, settings, start, covered=covered)
    for source, ready in pending:
        _note_pending(endpoint, start, source, ready)
    return records, answered, unchanged


async def _answers(asked_of: list[Provider], endpoint: Endpoint, settings, start: date, *,
                   covered=()) -> tuple[list[Record], list[str], list[str], list[tuple]]:
    """What `asked_of` say airs in this window — `fetch_window_records`' work for
    any set of sources, so a fill of one source's share (`fill_source`) is the
    same fill: (records, who answered, who said unchanged, [(who is still
    working its answer out, when it will be ready)])."""
    records: list[Record] = []
    answered: list[str] = []
    unchanged: list[str] = []
    pending: list[tuple[str, Any]] = []
    refusal: SourceUnavailable | None = None
    asked = 0
    for provider in asked_of:
        asked += 1
        try:
            # ONLY A SOURCE THAT ALREADY HAS ROWS HERE MAY ANSWER "unchanged".
            # A validator is per FILE and rows are per (endpoint, span), and
            # three show endpoints read the same two Simkl archives — so the
            # first of them to fill records the validator and the other two get
            # a 304 for a span they have nothing stored for. Left unchecked that
            # is permanent: they never fetch a body, never store a row, and the
            # source silently vanishes from those calendars.
            got = await provider.calendar_port.fetch_window(
                endpoint, settings, start, WINDOW_DAYS,
                revalidate=str(provider.source) in set(covered))
        except SourceNotModified:
            # A REPLY, NOT A FAILURE. The source has confirmed that what is
            # already stored is still its answer, so this fill contributes no
            # records for it and its rows must be LEFT ALONE — counting it as
            # answered would delete every airing it holds (it returned none),
            # and counting it as refused would mark the span partial and refetch
            # it for ever.
            unchanged.append(str(provider.source))
            logger.debug("%s reports the %s window starting %s unchanged.",
                         provider.source, endpoint.key, start)
            continue
        except SourcePending as exc:
            # NOT A FAILURE: the answer is on its way. The window is stored as
            # asked of this source and not yet answered, which is the truth, and
            # it is not counted as a refusal — a window every source of which is
            # still working is a window to store and follow, not one to fail.
            pending.append((str(provider.source), exc.ready))
            logger.debug("%s is still working out the %s window starting %s.",
                         provider.source, endpoint.key, start)
            continue
        except SourceUnavailable as exc:
            refusal = refusal or exc
            logger.debug("%s could not answer the %s window starting %s: %s",
                         provider.source, endpoint.key, start, exc)
            continue
        answered.append(str(provider.source))
        records.extend(got)
    if asked and not answered and not unchanged and not pending:
        raise refusal

    # The instance-wide content floor: an operator who excludes a genre, country,
    # or certification here means it never enters the shared cache for ANY
    # viewer, not just their own. It is the ONE filter applied before storage,
    # and it is applied to every source's records alike — every other dimension
    # is a signed-in viewer's own choice and is a read-time layer over these same
    # shared rows (see filter.py).
    certifications = (
        settings.show_certifications if endpoint.media == "show" else settings.movie_certifications
    )
    kept = calendar_filter.filter_records(
        records, settings.genres, settings.countries, certifications)
    # AND THE NETWORK LIST, WHICH IS THE SAME FLOOR. It is applied here on the
    # RECORD's own network — the field Simkl's enrichment fills in and Trakt's
    # calendar carries — rather than only on the rendered item at read, so a
    # floored network never enters storage either. No `exempt_unenriched` here,
    # for the reason the three above do not set it: a Simkl record is always
    # unenriched at fill, so exempting would make this a permanent no-op for
    # every Simkl title instead of a gap that closes when enrichment lands.
    # A SHOW CALENDAR'S ALONE: a film has no network, so an include-only list
    # judged a film calendar would store no film at all (vocab.floor_specs).
    if endpoint.media == "show":
        kept = calendar_filter.filter_by_network(kept, settings.network_filter)
    # AND THE LANGUAGE FLOOR, the one new dimension a listing can be judged on by
    # itself — a scalar on the record, like its country, and judged the same
    # way. Every other dimension the floor carries (keywords, show type, who
    # lists a title, what it is missing) is about the whole card or about a
    # catalogue's description, which no single listing carries at fill, so those
    # are applied at read only (`narrow_for_viewer`).
    kept = calendar_filter.keep_languages(kept, settings.languages)

    trimmed = [r for r in kept if in_window(r, start)]
    overrun = len(kept) - len(trimmed)
    if overrun:
        logger.debug(
            "%d entr(ies) fell outside the %s window starting %s; trimmed.",
            overrun, endpoint.key, start,
        )
    # THE ONE THING THE MATCHER NEEDS THAT A CALENDAR FILE DOES NOT CARRY: the
    # ids a title is known by elsewhere. A source can list an airing under an id
    # space the other source does not use at all — one live title is `mal` on one
    # side and `tmdb` on the other, with nothing shared in either payload — and
    # the group key is derived here, at fill, so an id learned later can never
    # reach it. This is a batched DB read and never a network call; see
    # enrich.overlay_match_ids for why only the IDS are taken and what its
    # realistic ceiling is.
    bridged = await calendar_enrich.overlay_match_ids(trimmed)
    return dedupe_records(bridged), answered, unchanged, pending


async def read_cached_window(endpoint_key: str, start: date) -> tuple[CachedWindow, int] | None:
    """The stored (window, cached_at) for one window, or None when nothing has
    been stored for it.

    THE WINDOW IS NO LONGER A STORED THING — it is a date range over rows, and
    this function is the seam that keeps that true without every caller learning
    it. What used to happen here was a zlib inflate plus a json.loads of a whole
    seven-day payload, on the event loop, five or six times per month read; what
    happens now is an indexed range scan and a grouping pass.

    ABSENT MEANS NO COVERAGE ROW, NOT NO AIRINGS. A span nobody has fetched and a
    span a source truthfully reported as empty are the same query result and
    completely different facts, and answering the second as a miss would refetch
    an empty month on every read for ever.

    THE GROUPS ARE THIS WINDOW'S ROWS ALONE, for a caller that wants one window
    by itself. The calendar does not draw from them: an episode two services
    date either side of the window's edge is half here, and only a read of the
    episode whole (`_assembled_span`) makes it the one card it is.
    """
    records, answered, asked, stored_at = await entries.read_span(
        endpoint_key, start, start + timedelta(days=WINDOW_DAYS))
    if stored_at is None:
        return None
    with span("calcache.group", records=len(records)):
        groups = group_records(records)
    return CachedWindow(groups, tuple(answered), tuple(asked)), int(stored_at)


async def stored_window_signature() -> str:
    """A short value that changes whenever the stored calendar might name a title
    it did not name before. Cheap: no payload leaves the database.

    FOR CALLERS THAT DERIVE WORK FROM THE STORED CALENDAR and would otherwise
    redo it on every request. Materialising and grouping every stored airing to
    answer a question about a handful of titles — measured at 3.0 SECONDS over
    33,314 groups on the author's instance, most of it blocking the event loop —
    is what this exists to avoid, and it is
    nothing once, and is worth avoiding on a page load that will find exactly what
    the previous one found. A caller pairs this with its own notion of what it
    still owes: unchanged on both sides means the answer cannot have moved.

    COUNT AND LATEST WRITE, not a content hash. A window is replaced wholesale
    when it refills, so a refill moves `cached_at`; a new window moves both. A
    hash of every payload would be exact and would cost precisely what the caller
    is trying not to spend.
    """
    return await entries.signature()


async def store_window(endpoint_key: str, start: date, records: list[Record],
                       ttl_seconds: int, now: int, *, sources=(), asked=None,
                       unchanged=()) -> None:
    """Store one window's records as rows.

    NOTHING IS HANDED BACK, and it used to be: the groups of the records just
    stored, so that the read that filled could serve them without building them
    twice. Those are not the window. A window's rows also hold whatever a search
    or a card's panel wrote into it (`entries.store_loose_airings`), and the
    stored rows of a source that answered "unchanged" — and a month opened for
    the first time was drawn without either. Measured: a search jump to Dexter's
    1 October 2006 premiere, which only Simkl's search-written row put on
    30 September, opened a month with no such card and nothing to land on; the
    next visit, reading rows, found it. The read path reads the rows back.

    THE GROUPING IS NO LONGER WHAT IS STORED. It used to be: the window blob held
    the finished groups, so the match ran once at fill and every read served its
    answer. Rows store each source's records separately and `match_keys` runs at
    READ instead — which is not a regression but a correction. Whether two
    records are one card depends on WHAT ELSE IS STORED (see match_keys: an
    episode's every listing decides its units, and an uncoordinated record folds
    into one only when exactly one candidate exists), so a fill that stored the
    answer froze a judgement made against whatever had been fetched alongside
    it. A later fill adding another service's listing could not revise it.
    """
    # WHAT IS ALREADY KNOWN GOES IN WITH THEM. A fresh calendar payload is
    # unenriched by construction, so storing it as-is would blank every title the
    # drain has already answered for until the next drain tick noticed and put it
    # back. Applying first means a refilled month renders enriched on the very
    # read that refilled it.
    await calendar_enrich.apply_stored_enrichment(records)
    await entries.store_span(
        endpoint_key, start, start + timedelta(days=WINDOW_DAYS), records,
        sources=sources, asked=(sources if asked is None else asked),
        unchanged=unchanged,
        now=now, stale_after=now + max(0, int(ttl_seconds)))
    # THE ROWS A MEMOISED SPAN WAS ASSEMBLED FROM HAVE JUST MOVED. This is the
    # one write that can change what a span holds, so it is the one place the
    # memo has to give one up — otherwise a refilled month would go on being
    # served from before the refill for the length of its TTL. Named, because
    # this window belongs to one endpoint and the others are untouched.
    _forget_spans(endpoint_key)


# HOW STALE A SPAN MAY BE BEFORE A READ REFILLS IT, by how far its own dates are
# from today. Tiered rather than flat because the underlying data is: measured
# against data.simkl.in on 2026-08-28, the current month and the two ahead of it
# are regenerated roughly hourly, while 2026-06 and 2025-08 had both been
# untouched for 38.9 days. A single number has to be short enough for the month a
# viewer is in, which then refetches a frozen archive from 2025 on the same
# clock — one setting spending real requests on files that provably do not move.
#
# (days ahead of / behind today, seconds a span of that age may go unrefreshed)
_STALENESS_TIERS = (
    (0, 24 * 60 * 60),            # this month and the future: a day
    (31, 7 * 24 * 60 * 60),       # the month behind: a week
    (183, 30 * 24 * 60 * 60),     # older than six months: a month
    (366, 90 * 24 * 60 * 60),     # older than a year: a quarter
)


def _ttl_seconds(settings, start: date | None = None, now: int | None = None) -> int:
    """The staleness bound for one span, or the operator's flat value when the
    caller has no span in mind.

    THE SETTING STILL WINS WHERE IT IS SHORTER, which is what keeps it a setting
    rather than a decoration. An operator who asks for ten minutes gets ten
    minutes on the month in front of them; what the tiers add is that a span two
    years back is not also refetched every ten minutes to confirm a file nobody
    has regenerated since last summer.
    """
    try:
        flat = max(0, int(settings.calendar_cache_ttl_minutes)) * 60
    except (TypeError, ValueError):
        flat = 600
    if start is None:
        return flat
    today = datetime.fromtimestamp(db.now() if now is None else now,
                                   tz=timezone.utc).date()
    behind = (today - start).days
    tier = flat
    for days, seconds in _STALENESS_TIERS:
        if behind >= days:
            tier = seconds
    return max(flat, tier) if behind >= _STALENESS_TIERS[1][0] else flat


# Spans a background refill is already running for, so ten viewers opening the
# same stale month fire one fetch rather than ten. Keyed on (endpoint, span)
# because that is exactly the unit a fill covers.
_refilling: set[tuple[str, str]] = set()
_refill_tasks: set[asyncio.Task] = set()


def _forget_refill(task: asyncio.Task) -> None:
    _refill_tasks.discard(task)


def schedule_refill(endpoint: Endpoint, settings, start: date) -> bool:
    """Refill one stale span in the background. Returns whether it started one.

    THE POINT OF THE WHOLE STORAGE CHANGE, AND IT IS WORTH SECONDS RATHER THAN
    MILLISECONDS. A lapsed TTL used to make the next viewer WAIT for the fetch
    that renewed it — a page load blocked on Trakt and Simkl answering, five or
    six spans deep for a month, with nothing on screen until they did. Rows can
    be served while they are being replaced, so a stale span is now handed over
    immediately and renewed behind the request.

    FIRE AND FORGET, NEVER AWAITED, and coalesced on the span: a month opened by
    ten people at once is one refill, not ten. The latch is dropped in a `finally`
    so a failed fetch leaves nothing latched — the next read tries again rather
    than finding the span permanently "already refilling".

    DEGRADES TO A NO-OP with no running loop (a script, a sync test path, or the
    app tearing down). The scheduled month refresh still runs every tick, so a
    skipped background refill costs at most the interval until that notices.
    """
    key = (endpoint.key, start.isoformat())
    if key in _refilling:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False

    async def _run() -> None:
        try:
            with activity("background refill"):
                await ensure_window(endpoint, settings, start,
                                    allow_fetch=True, force=True)
        except SourceUnavailable as exc:
            logger.debug("background refill of the %s span starting %s failed: %s",
                         endpoint.key, start, exc)
        except Exception:  # pragma: no cover — a background task must not vanish silently
            logger.exception("background refill of the %s span starting %s raised.",
                             endpoint.key, start)
        finally:
            _refilling.discard(key)

    _refilling.add(key)
    task = asyncio.create_task(_run())
    _refill_tasks.add(task)
    task.add_done_callback(_forget_refill)
    return True


async def ensure_window(endpoint: Endpoint, settings, start: date, *,
                        allow_fetch: bool = True, force: bool = False,
                        now: int | None = None) -> WindowCoverage:
    """Make one window as current as this read may, and say what it holds.

    Fetches and caches when the window is missing, past its TTL, or was filled
    without a source that is in play for it now — and allow_fetch is set. A
    public share page passes allow_fetch=False: it serves whatever is cached —
    even stale, even nothing (coverage with no `cached_at`) — and never asks a
    source, so an unauthenticated visitor can never spend the instance's rate
    limit. `cached_at` is None only when nothing was cached and nothing was
    fetched.

    COVERAGE ONLY, NO AIRINGS. The span read that calls this reads the airings
    of every window it covers in one go (`_assembled_span`), because an episode
    two services date either side of a window's edge is one card only when both
    halves are read together; reading each window's rows here as well would read
    every row twice.

    A SOURCE THE WINDOW PREDATES MAKES IT A MISS, NOT A FAILURE. Admitting a new
    source, or moving to a month a source only reaches now, leaves stored windows
    that were filled without ever asking it. Nothing went wrong when they were
    written, so reporting them as incomplete says something untrue and says it
    until they expire; refetching them is what actually answers the question, and
    it is the same treatment a payload in an older shape already gets.
    """
    ts = db.now() if now is None else now
    ttl = _ttl_seconds(settings, start, ts)
    in_play = [str(p.source) for p in _window_sources(endpoint, settings, start)]
    answered_before, asked_before, stored_at = await entries.span_coverage(endpoint.key, start)
    cached = (None if stored_at is None
              else WindowCoverage(answered_before, asked_before, int(stored_at)))
    # Which sources this span ALREADY holds rows from — the only ones whose
    # "unchanged" is a usable answer rather than a silent gap. Asked of the
    # AIRINGS and not of the coverage table: coverage records who replied, and a
    # source that replied "unchanged" for a span it had never filled is recorded
    # as having answered while leaving nothing behind. Gating on coverage let
    # that state justify itself for ever — see entries.sources_with_rows.
    has_rows = await entries.sources_with_rows(
        endpoint.key, start, start + timedelta(days=WINDOW_DAYS))
    if cached is not None:
        # `force` IS THE SCHEDULED REFRESH'S OWN CADENCE OVERRIDING THE READ
        # PATH'S. The TTL here answers "is this fresh enough to serve", which
        # is a different question from "is this month due a look" — see
        # refresh_months. It never overrides allow_fetch, so a share page
        # cannot be made to fetch by anything.
        fresh = (ts - cached.cached_at) <= ttl and not force
        unasked = set(in_play) - set(cached.asked)
        if not allow_fetch or (fresh and not unasked
                               and not _failed_retry_due(endpoint, start, cached, ts)):
            return cached
        if not force:
            # STALE BUT SERVEABLE: hand over what is stored and renew it behind
            # the request. Waiting here is what used to cost a page load seconds,
            # and there is nothing a fresh fetch would give this viewer that the
            # stored rows do not — a calendar is not a bank balance, and a span
            # that lapsed a minute ago is the same span.
            #
            # A SOURCE NOW IN PLAY THAT WAS NEVER ASKED IS DIFFERENT, and falls
            # through to the blocking path below: those rows are not stale, they
            # are INCOMPLETE, and serving them would show a month missing a whole
            # service while the banner said everything was fine.
            if not unasked:
                schedule_refill(endpoint, settings, start)
                return cached
        if unasked:
            logger.debug(
                "the %s window starting %s was filled without %s; refilling.",
                endpoint.key, start, ", ".join(sorted(unasked)))
    elif not allow_fetch:
        return WindowCoverage((), (), None)
    try:
        records, answered, unchanged = await fetch_window_records(
            endpoint, settings, start, covered=has_rows)
    except SourceUnavailable:
        if cached is not None:  # serve the stale copy rather than nothing
            return cached
        raise
    await store_window(endpoint.key, start, records, ttl, ts,
                       sources=answered, asked=in_play, unchanged=unchanged)
    # AFTER THE STORE, never before it: the store records a pending source as
    # asked and not answered, and a follow-up that stored first would have its
    # answer marked missing again by this one.
    _follow_pending(endpoint, settings, start)
    if unchanged and not answered:
        # EVERY SOURCE SAID "UNCHANGED", so this fetch brought no records at all
        # while the stored rows are perfectly good. The store above still ran: it
        # advanced the coverage clock without touching an airing, and the window
        # is served exactly as the coverage now stands.
        after = await entries.span_coverage(endpoint.key, start)
        if after[2] is not None:
            return WindowCoverage(after[0], after[1], int(after[2]))
    await _after_fill(settings, records)
    # Who was asked and who answered, from this fill, which knows them in the
    # sources' declared order.
    # A SOURCE THAT SAID "UNCHANGED" ANSWERED, beside one that brought rows as
    # much as on its own: naming only the second made the very read this fill
    # served report the first as unreadable — the incomplete-data banner — and,
    # being partial, it was not kept for the reads after it.
    replied = {*answered, *unchanged}
    return WindowCoverage(tuple(name for name in in_play if name in replied),
                          tuple(in_play), ts)


async def _after_fill(settings, records: list[Record]) -> None:
    """What a fill that stored records does next — the same for a whole window
    and for one source's share of it (`fill_source`).

    A FILL, NOT A READ — this only runs when something was actually fetched and
    stored, never on a cache-hit return. That is what lets it ask for enrichment
    sooner than the next heartbeat tick without weakening "the read path never
    makes an outbound call": nothing here is served to a viewer yet, and the
    scheduled drain is fire-and-forget (see enrich.schedule_drain) so the
    request does not wait on it either. And the pictures the fetch brought are
    recorded here, where the lookup was "paid for", rather than as a per-view
    cost on the hot render path.
    """
    calendar_enrich.schedule_drain(settings)
    sightings = (s for s in (_poster_sighting(r) for r in records) if s is not None)
    await artwork.record_poster_urls(sightings)


# ---------------------------------------------------------------------------
# a source still working its answer out
# ---------------------------------------------------------------------------
#
# A SOURCE MAY ANSWER A WINDOW LATER (base.SourcePending): TMDB's season
# premieres are derived from every show airing that week, tens of seconds cold,
# behind the request. The fill stores what everybody else said, with that source
# asked and not yet answered — which is the truth — and once its answer is ready
# fills that source's share ALONE (`fill_source`), leaving the other services'
# rows and coverage exactly as they are. Until then the window is not "partial":
# nothing failed, so the read path does not say "couldn't be loaded", the
# failure retry does not ask again over the top of it, and the stats box counts
# the weeks still being worked out (`pending_windows`).
#
# IN MEMORY, AS THE WORK BEHIND IT IS. A restart forgets both, and the window
# then reads as a source that has not answered — the retry a minute behind a
# reader, or the schedule's, asks again and the source starts its work again.

# THE LONGEST A FOLLOW-UP WAITS for a source's answer. The work is done one span
# at a time, so a cold instance's two scheduled months queue a dozen spans of
# tens of seconds each; half an hour clears that with room. A follow-up past it
# gives up, and the window falls to the failure retry like any unanswered one.
_PENDING_WAIT_SECONDS = 30 * 60


class _Pending(NamedTuple):
    ready: Any
    since: float
    follow_up: asyncio.Task | None


# (endpoint, window start, source) -> what is awaited. An entry leaves when its
# follow-up ends, and one whose follow-up never started — a fetch no store
# followed — is dropped on the clock above, so this holds no more than the
# windows being worked out.
_pending: dict[tuple[str, str, str], _Pending] = {}


def _note_pending(endpoint: Endpoint, start: date, source: str, ready) -> None:
    """Remember that `source` will answer this window once `ready` finishes. A
    window already being followed for it keeps its follow-up."""
    moment = time.monotonic()
    for stale in [key for key, held in _pending.items()
                  if moment - held.since >= _PENDING_WAIT_SECONDS
                  and (held.follow_up is None or held.follow_up.done())]:
        del _pending[stale]
    key = (endpoint.key, start.isoformat(), str(source))
    held = _pending.get(key)
    if held is not None and held.follow_up is not None and not held.follow_up.done():
        return
    _pending[key] = _Pending(ready, moment, None)


def _follow_pending(endpoint: Endpoint, settings, start: date) -> None:
    """Start the follow-up of every source this window is waiting on that has
    none yet — called by the store, once the window's own rows are written."""
    for key, held in list(_pending.items()):
        if key[:2] != (endpoint.key, start.isoformat()) or held.follow_up is not None:
            continue
        task = asyncio.create_task(_follow_up(endpoint, settings, start, key[2], held.ready))
        _pending[key] = held._replace(follow_up=task)


async def _follow_up(endpoint: Endpoint, settings, start: date, source: str, ready) -> None:
    """Wait for `source`'s answer to this window, then fill its share. Behind
    every request — and detached from the one that started it, whose timing it
    is no part of (perftrace.detach); a failure is logged and leaves the window
    to the failure retry, never raised at anybody."""
    key = (endpoint.key, start.isoformat(), source)
    detach()
    try:
        try:
            # SHIELDED, because the answer is shared: a follow-up giving up must
            # not cancel the work another window, or the next fill, is waiting on.
            await asyncio.wait_for(asyncio.shield(ready), _PENDING_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("%s's answer for the %s window starting %s took longer than "
                           "%d minutes; left to the retry", source, endpoint.key, start,
                           _PENDING_WAIT_SECONDS // 60)
            return
        except Exception as exc:  # the source logged its own failure where it happened
            logger.info("%s could not work out the %s window starting %s (%s); left to "
                        "the retry", source, endpoint.key, start, exc)
            return
        with activity("pending answer"):
            if await fill_source(endpoint, settings, start, source):
                logger.info("%s's answer for the %s window starting %s is stored", source,
                            endpoint.key, start)
    except SourceUnavailable as exc:
        logger.info("%s's answer for the %s window starting %s could not be read (%s); "
                    "left to the retry", source, endpoint.key, start, exc)
    except db.DatabaseError as exc:
        # A database too busy to take the write (another fill's transaction) is
        # not this answer's fault, and the retry stores it as well as this would.
        logger.warning("%s's answer for the %s window starting %s could not be stored "
                       "(%s); left to the retry", source, endpoint.key, start, exc)
    except Exception:  # pragma: no cover — a background task must not vanish silently
        logger.exception("following %s's answer for the %s window starting %s failed",
                         source, endpoint.key, start)
    finally:
        _pending.pop(key, None)


async def fill_source(endpoint: Endpoint, settings, start: date, source: str, *,
                      now: int | None = None) -> bool:
    """Fill ONE source's share of a window — its airings replaced and its coverage
    advanced, every other source's left exactly as it is (entries.store_span
    deletes and records only the sources it is told answered). Returns whether
    the source answered.

    THE ORDINARY FILL, NARROWED: the same floor, trim and id overlay
    (`_answers`), the same store, the same follow-on work (`_after_fill`). What a
    source still working its answer out (base.SourcePending) says HERE is not
    followed again — that would chain one fill to the next for as long as the
    answer kept not arriving; the window stays asked and not answered, and the
    failure retry takes it from there.
    """
    ts = db.now() if now is None else now
    provider = next((p for p in _window_sources(endpoint, settings, start)
                     if str(p.source) == str(source)), None)
    if provider is None:
        return False
    has_rows = await entries.sources_with_rows(
        endpoint.key, start, start + timedelta(days=WINDOW_DAYS))
    records, answered, unchanged, pending = await _answers(
        [provider], endpoint, settings, start, covered=has_rows)
    if not answered and not unchanged:
        if pending:
            logger.warning("%s says its answer for the %s window starting %s is still being "
                           "worked out, just after saying it was ready", source,
                           endpoint.key, start)
        return False
    await store_window(endpoint.key, start, records, _ttl_seconds(settings, start, ts), ts,
                       sources=answered, asked=[str(source)], unchanged=unchanged)
    await _after_fill(settings, records)
    return True


def pending_sources(endpoint_key: str, start: date) -> set[str]:
    """The sources this window is waiting on for an answer still being worked out."""
    window = (endpoint_key, start.isoformat())
    return {key[2] for key in _pending if key[:2] == window}


def pending_windows() -> int:
    """How many calendar windows are waiting on a source's answer still being
    worked out — the stats box's "weeks being worked out", instance-wide."""
    return len({key[:2] for key in _pending})


# HOW SOON A WINDOW A SOURCE FAILED ON IS ASKED AGAIN, behind the reader, rather
# than at the window's whole staleness bound. A failure is the one thing in a
# stored window that is KNOWN to be wrong — the page says "some calendar data
# couldn't be loaded" for as long as it stands — and waiting the operator's TTL
# (ten minutes by default) to try again left that banner up through every reload
# with no way to make it go. A minute is long enough not to hammer a service that
# is struggling, and the retry runs behind the reader, one per window at a time
# (`schedule_refill`), so the read that triggers it is answered at once.
_FAILED_RETRY_SECONDS = 60

# AND HOW SOON THE SCHEDULE DOES, with nobody reading. `refresh_months` judged a
# window only by its age, so a failure in next month's weeks — refreshed weekly —
# stood for up to seven days unless somebody opened that month, and then the
# first view drew the banner while its retry ran behind it: a Trakt failure on
# 2026-10-23 in the week of 30 November was still there on the 26th. Slower than
# a reader's minute because the schedule walks every window of two months with
# nobody waiting, and during an outage a minute would ask a struggling service
# again for each of them every tick.
_FAILED_REFRESH_SECONDS = 15 * 60

# When each (endpoint, window) last had a failed source asked again, so a source
# that keeps failing outright — which stores nothing, and so leaves the window's
# own clock where it was — is retried once a minute rather than on every read.
# Shared by the reader and the schedule, so neither repeats the other's attempt.
# In memory: a restart simply makes the first read after it try again.
#
# PRUNED ON THE SLOWER OF THE TWO CLOCKS THAT READ IT. The process runs for weeks
# between restarts and every window that ever failed used to stay here for all
# of them. An attempt older than `_FAILED_REFRESH_SECONDS` delays neither clock —
# each waits from the LATER of the window's store and the attempt, and that much
# has passed since the attempt — so dropping it changes no answer.
_failed_retried_at: dict[tuple[str, str], int] = {}


def _failed_retry_due(endpoint: Endpoint, start: date, cached: WindowCoverage,
                      ts: int, *, after: int = _FAILED_RETRY_SECONDS) -> bool:
    """Whether a source this stored window asked and did not hear from is due to
    be asked again, `after` seconds from the later of the window's store and the
    last attempt — see `_FAILED_RETRY_SECONDS` and `_FAILED_REFRESH_SECONDS` —
    and, when it is, notes the attempt so the next one waits too."""
    for done in [key for key, at in _failed_retried_at.items()
                 if ts - at >= _FAILED_REFRESH_SECONDS]:
        del _failed_retried_at[done]
    # A SOURCE STILL WORKING ITS ANSWER OUT HAS NOT FAILED, and its answer is
    # already being followed: asking every source again over the top of it would
    # spend the others' requests to learn what the follow-up is about to store.
    if not (set(cached.asked) - set(cached.sources)
            - pending_sources(endpoint.key, start)):
        return False
    key = (endpoint.key, start.isoformat())
    since = max(int(cached.cached_at or 0), _failed_retried_at.get(key, 0))
    if ts - since < after:
        return False
    _failed_retried_at[key] = ts
    return True


async def load_window(endpoint: Endpoint, settings, start: date, *,
                      allow_fetch: bool = True, force: bool = False,
                      now: int | None = None,
                      ) -> tuple[CachedWindow, int | None]:
    """(window, cached_at) for ONE window by itself: `ensure_window`, then that
    window's own rows grouped (see `read_cached_window` for why those groups are
    not what the calendar draws). An empty window, None, when nothing is stored
    and nothing could be fetched.

    SERVED FROM THE ROWS, NEVER FROM WHAT WAS FETCHED — see store_window for
    what a fetch alone leaves out. The sharpest case: every source said
    "unchanged", so a fill brought no records at all while the stored rows are
    perfectly good, and serving what was fetched would blank the window on the
    very read that confirmed it was current.
    """
    coverage = await ensure_window(endpoint, settings, start, allow_fetch=allow_fetch,
                                   force=force, now=now)
    if coverage.cached_at is None:
        return CachedWindow([], coverage.sources, coverage.asked), None
    records, _answered, _asked, _stored_at = await entries.read_span(
        endpoint.key, start, start + timedelta(days=WINDOW_DAYS))
    with span("calcache.group", records=len(records)):
        groups = group_records(records)
    return CachedWindow(groups, coverage.sources, coverage.asked), coverage.cached_at


# ---------------------------------------------------------------------------
# the assembled read path
# ---------------------------------------------------------------------------

def day_label(day: date) -> str:
    """The heading a day's block carries ("Friday, 03 July").

    Shared rather than formatted at each call site: a day that fails to load is
    still announced by its date, and a placeholder or an error whose heading is
    spelled differently from the real one reads as a different day."""
    return day.strftime("%A, %d %B")


def _local_span_utc_range(tz: ZoneInfo, start_date: date, end_date: date) -> tuple[date, date]:
    """The UTC date range whose aligned windows cover the viewer-LOCAL day span
    [start_date, end_date], padded a day each side.

    A local day is viewer-dependent in UTC — an item at 02:00 UTC on the 1st is
    the previous local day for a UTC-8 viewer and this one for a UTC+2 one, and a
    single local day can even straddle two UTC windows once the offset is applied
    — so the span is padded a day each side in the viewer's tz and then expressed
    in UTC, where the windows live. The final trim back to the exact local span
    happens after normalization, never in UTC.
    """
    local_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    local_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz)
    utc_start = (local_start - timedelta(days=1)).astimezone(timezone.utc).date()
    utc_end = (local_end + timedelta(days=1)).astimezone(timezone.utc).date()
    return utc_start, utc_end


async def with_title_only(parsed, settings=None) -> list[tuple[dict, list[Record], list[Record]]]:
    """`parsed` — `[(group, every record of it the INSTANCE admits)]` — with each
    group's TITLE-ONLY records beside its listings: `[(group, listings,
    title-only)]`.

    WHAT ONE IS. A service that describes every card (Capabilities.
    describes_every_card — TMDB) and does not list this airing may still have
    said what the title is: its row in the title store, under the title's key,
    holds the certification, language, network, genres and keywords the services
    that list the card often do not state — measured on one October's All
    Episodes, 10,899 of 16,997 cards had no certification and 15,038 no
    language. That row joins the card as a record of its own, built by
    entries.title_only_records from the store and nothing else.

    WHAT IT MAY DO, each rule held where it applies and by a test of its own:
      - it may win any SCALAR field and joins the genres, keywords and show type
        (resolve.resolve_records), so what it states reaches the card and every
        filter judges it;
      - its keywords and show type are an answer only where a description stated
        them (rows.facts_of, read by entries._words) — a row nobody has
        described says nothing about either, and is not judged by them;
      - it adds a button to the service's page, drawn as a service that knows
        the title without listing this airing (detail_source.service_links).
    WHAT IT MAY NOT:
      - be the card's PRIMARY: it never wins attribution, the airing (date and
        time) or the episode coordinate, which are chosen among the listings
        alone (resolve.resolve_records) — or a viewer whose first service only
        describes the title would see the card move to a day no listing gave;
      - admit a card by itself: a group none of a viewer's services lists is not
        theirs to draw however much a describing service says, so a viewer
        narrowed to the describing service sees only what that service LISTS
        (`narrow_for_viewer`);
      - count as listing it: "only one service lists it" and "only a sparse
        catalogue lists it" count the listings (filter.Judged.listed_by), and a
        description joined to every card would make no card single-listed;
      - reach the release narrowing or the disguised-film prune, which judge the
        records that list a card.

    ONE READ FOR THE WHOLE SPAN, of the store's rows only — nothing is asked of
    any service and nothing is written, so a read for one viewer leaves every
    row exactly as the next viewer finds it. A group that already holds a
    listing from the describing service gains nothing: that listing is the same
    row (entries._READ_SQL joins it). One record per title, shared by every card
    of it in the span — nothing downstream writes to a record it is handed.
    `settings` is the operator's admission (resolve.describing_sources): a
    service this instance keeps off its calendar describes none of its cards.
    """
    describers = calendar_resolve.describing_sources(settings)
    wanted: dict[int, tuple[str, str, list[str]]] = {}
    for index, (group, listings) in enumerate(parsed):
        if not listings:
            continue
        listed = {str(r.source) for r in listings}
        missing = [name for name in describers if name not in listed]
        if missing:
            wanted[index] = (str(listings[0].media), group["base"], missing)
    found = await entries.title_only_records(
        describers, {key for _media, key, _missing in wanted.values()}) if wanted else {}
    out = []
    for index, (group, listings) in enumerate(parsed):
        media, key, missing = wanted.get(index, ("", "", ()))
        out.append((group, listings,
                    [record for name in missing
                     if (record := found.get((name, media, key))) is not None]))
    return out


async def visible_records(groups, endpoint, *, prefs=None, settings=None,
                          **specs) -> tuple[list, int]:
    """`groups` reduced to the records THIS viewer's calendar would draw, and
    how many this viewer's filters removed on the way. `specs` are the viewer's
    filters, `vocab.active_specs`' keywords.

    EXTRACTED SO THE SEARCH RUNS THE SAME PIPELINE RATHER THAN A SECOND ONE.
    A search has to answer "would this viewer's calendar actually draw this
    card", and six independent reasons a title is absent live in these lines;
    a second implementation of them would be wrong the first time any one
    changed. What a search does NOT want is a whole month — it knows which
    groups matched, so it hands over those and pays for those. Measured
    before the split: one `shows` month held 12,880 airings and a search of
    five months took eleven seconds, nearly all of it in here.

    NOTHING BELOW WAS REWRITTEN. It is the block that sat inline in
    `assemble_range`, with the values it read from that function's scope made
    into arguments. The order of the steps is the substance and each comment
    says why it is where it is.
    """
    with span("calcache.filter", entries=len(groups)) as sp:
        # RESOLUTION IN TWO HALVES WITH THE ENRICHMENT OVERLAY BETWEEN THEM, and
        # the order is the point. The overlay fills in the fields one source's
        # calendar files do not carry, and it has to act on that source's OWN
        # record — before anything picks between the sources — or a merged group
        # whose other source supplies the card would never have its enrichment
        # considered, and a genre only that source knows could not win however
        # the viewer set their preference.
        # `settings` IS THE OPERATOR'S ADMISSION AND THE ONLY ONE APPLIED HERE —
        # a source this instance has switched off its calendar is off for
        # everybody, and a window filled while it was still in play is still
        # sitting in the cache holding its records. Reading it here is what makes
        # that switch take effect immediately instead of one TTL from now.
        # THE VIEWER'S SOURCE CHOICE IS APPLIED IN `narrow_for_viewer`, NOT HERE:
        # it decides which listings a card is drawn from, and the filters judge
        # every record the instance admits — see there.
        parsed = calendar_resolve.instance_admitted(groups, settings)
        # THE SIMKL OVERLAY IS GONE FROM HERE, AND ITS ABSENCE IS THE POINT.
        # What used to happen on this line was a batched read of `simkl_titles`
        # to paint genres, network, country and certification onto this read's
        # Simkl records, because the stored window could not hold them: a fill
        # replaced the whole blob with a fresh, unenriched payload, so anything
        # written into it was erased on the next refill. Rows do not have that
        # problem — the drain writes the title store's row and a refill is
        # forbidden to demote it (app/metadata/rows.py) — so the fields are already on
        # the records read_span handed back, and `enriched` is a statement about
        # what the row CONTAINS rather than about what a reader must look up.
        flat = [r for _, rs in parsed for r in rs]
        # AND THE OTHER SERVICE'S HALF OF THE SAME QUESTION. Trakt's calendar
        # payload carries no release schedule either, so without this a film
        # Trakt listed reached the release rule below with nothing to be judged
        # on — and a record that cannot answer is kept, which meant the filter
        # could never drop a film Trakt also listed. Same promise as the overlay
        # above: it reads what is stored and fetches nothing.
        await calendar_enrich.overlay_releases(flat)
        # AND WHAT A SERVICE THAT DESCRIBES EVERY CARD SAYS ABOUT A TITLE IT DOES
        # NOT LIST HERE, read from the store once for the span and kept apart
        # from the listings (`with_title_only` holds what it may do).
        parsed = await with_title_only(parsed, settings)
        # THE RELEASE NARROWING RUNS HERE, BEFORE THE PICK, AND ON THE GROUP.
        # A films calendar that lists every release in every market needs a way
        # to say "the ones out here, in the formats I watch"; the per-country
        # release schedule only exists on the records of whichever source
        # published one, so asking after resolution would ask the winner alone
        # and asking per record would strip a service off a merged card to
        # enforce a rule about release formats. Counted rather than silently
        # applied, so a month this empties can say so — see meta below.
        # ON A WORKER THREAD, as the grouping is (`_assembled_span` says why):
        # judging every card and resolving the drawn ones is the month's
        # largest share of pure CPU, and it reads nothing but what it is handed.
        kept, release_filtered, resolved = await anyio.to_thread.run_sync(
            functools.partial(narrow_for_viewer, parsed, endpoint.media, prefs=prefs,
                              settings=settings, **specs))
        sp.set(kept=len(kept))

    # EVERY PER-VIEWER REMOVAL, IN ONE NUMBER. The release rule and the
    # genre/country/certification rule are different questions with different
    # vocabularies, but the reader's question is one: how much of this month am I
    # not being shown. Counted here rather than inferred from a total, because
    # the instance-wide floor and `prune_disguised_films` also remove records and
    # neither is this viewer's doing — attributing those to a filter somebody set
    # would send them looking for a setting that would not bring the title back.
    return kept, release_filtered + (resolved - len(kept))


async def placed(groups, endpoint, *, tz: ZoneInfo,
                 **narrowing) -> tuple[list[Item], int]:
    """Every card `groups` make for ONE viewer, each rendered on the day it is
    PLACED on, and how many their filters removed. `narrowing` is
    `visible_records`' keywords — the viewer's filters (`vocab.active_specs`),
    their source selection (`prefs`) and the operator's `settings`.

    THE ONE PLACEMENT FUNCTION, and every path that says where a card is drawn
    asks it: a month, a day that arrives late, a share page and its card, the
    tracker's calendar import (all through `assemble_range`), and the calendar
    search's "where it airs", its season picker and the card its jump lands on
    (calendar/search.py). WHICH DAY is decided by which of a card's services
    describes it — the first in this viewer's order (resolve.py), date and time
    included — and the day is that description rendered into the viewer's zone.
    So two viewers with different orders, reading the very same rows, see one
    card each, each on their own service's day; and no path works a day out from
    one listing, which would put a card where some service said rather than
    where this viewer's calendar draws it.

    The network lists are judged with every other filter, in `narrow_for_viewer`:
    the search, its season picker and the card its jump lands on read through this
    too, and while the lists were applied only to a month they offered a title the
    viewer's network filter hides, linking to a day that could not draw it.
    """
    kept, filtered_out = await visible_records(groups, endpoint, **narrowing)
    with span("calcache.normalize", entries=len(kept)) as sp:
        items = [render(record, tz) for record in kept]
        sp.set(items=len(items))
    return items, filtered_out


def narrow_for_viewer(parsed, media, *, prefs=None, settings=None,
                      **specs) -> tuple[list, int, int]:
    """Every filter a calendar applies, in the order it applies them: `(the
    records this viewer's cards are drawn from, how many groups the release
    narrowing removed, how many cards reached the rest)`. `parsed` is
    `[(group, every listing of it the INSTANCE admits, its title-only records)]`
    (`with_title_only`); `specs` are the viewer's filters, `vocab.active_specs`'
    keywords (vocab.complete_specs refuses any other).

    ONE IMPLEMENTATION OF "WOULD THIS VIEWER'S FILTERS HIDE IT", called by
    `visible_records` for the stored calendar and by the search's season picker
    (calendar/search.py's `picker`) for a title a service has just described —
    which no source admission applies to, since the calendar would draw another
    service's record of it, but every filter does: a picker that checked genre,
    country and certification alone offered films the release narrowing hides
    (measured: Sinners, under a US-releases-only filter).

    THE VIEWER'S SOURCE CHOICE DECIDES WHICH LISTINGS A CARD IS DRAWN FROM, AND
    NEVER WHAT THEIR FILTERS JUDGE. A group none of this viewer's services lists
    is not theirs to draw, and drops out uncounted. A group they do draw is
    judged as every service the instance admits describes it — resolved in this
    viewer's order, as a viewer reading every service would see it — and drawn
    from their own services' records alone. REPORTED: Sam Pang Tonight, a talk
    show by Simkl's account and a comedy by TMDB's, was hidden by a viewer's
    "no talk shows" filter until they narrowed the calendar to TMDB and Simkl's
    genre went with Simkl's listing. A title somebody hid stays hidden whichever
    services they read. So "only one service lists it" counts the services the
    INSTANCE admits, never the viewer's narrowing of them — and never a service
    that only describes the card.

    A TITLE-ONLY RECORD IS NARROWED BY THE SOURCE CHOICE AS A LISTING IS: a viewer
    who left a service off their calendar draws none of its words, on a card it
    lists or one it only describes, and is judged by them like everybody else.
    Being narrowed to that service alone draws nothing it does not list
    (resolve.resolve_records makes no card without a listing).

    EVERY CARD IS JUDGED, AND ONLY THE CARDS THAT SURVIVE ARE DRAWN. A card is
    judged on its `resolve.Facts` — the few fields a filter reads, by the rules
    that resolve them — and resolved in full, provenance, buttons, airing and
    all, only once it has survived every filter: MEASURED, one viewer's October
    All Episodes resolved 18,759 cards in full to draw 1,263.
    """
    specs = vocab.complete_specs(specs)
    orders = calendar_resolve.Orders(prefs)
    # Which services this viewer reads, None for every one — the ordinary case,
    # where a card's own listings are all of them.
    reads = (None if prefs is None
             else frozenset(str(source) for source in Source if prefs.admits_calendar(source)))
    if reads is not None and len(reads) == len(Source):
        reads = None
    # (group, every listing, its title-only records, the viewer's own of each)
    pairs: list = []
    for group, every, describing in parsed:
        own = every if reads is None else [r for r in every if str(r.source) in reads]
        if not own:
            continue
        own_describing = (describing if reads is None
                          else [r for r in describing if str(r.source) in reads])
        pairs.append((group, every, describing, own, own_describing))
    narrowed = calendar_filter.filter_release_groups(
        pairs, media, specs["movie_release_countries"], specs["movie_release_types"])
    release_filtered = len(pairs) - len(narrowed)
    pair_of: dict[int, tuple] = {}
    cards: list[calendar_filter.Judged] = []
    for pair in narrowed:
        _group, every, describing, _own, _own_describing = pair
        facts = calendar_resolve.judged_facts(every, describing, orders=orders)
        pair_of[id(facts)] = pair
        cards.append(calendar_filter.Judged(facts, frozenset(str(r.source) for r in every)))
    # A Simkl entry Simkl's OWN enrichment marks as a film (`anime_type ==
    # "movie"`) does not belong on a series endpoint — see
    # filter.prune_disguised_films for the measured rule and why this is
    # a read-time exclusion rather than something fetch_window_records can
    # do at fill. Same reasoning as the exemption below: it can only act
    # on what enrichment has already found, so it runs right beside it.
    kept_records = {id(r) for r in calendar_filter.prune_disguised_films(
        [card.record for card in cards], media)}
    cards = [card for card in cards if id(card.record) in kept_records]
    # THE INSTANCE FLOOR, AND IT RUNS BEFORE THIS VIEWER'S OWN SPECS.
    # It is applied at FILL as well (fetch_window_records), where it is a
    # HARD exclusion — a floored title never enters the shared rows, so no
    # per-account filter can bring it back, and that promise is the reason
    # the fill keeps it. But the fill is the only moment that copy acts, so
    # an operator who tightened the floor went on seeing a month that was
    # already stored until its TTL ran out. Applying it here too is what
    # makes the change reach windows already held — the shape the instance's
    # "include Simkl results" switch already has, and for the same reason:
    # it is one decision taken for every viewer, so it can narrow the shared
    # rows without deciding anything on one viewer's behalf.
    # THE OPERATOR'S SPEC IS NOT MERGED INTO THE VIEWER'S. They are two
    # answers to the same question and either may be empty; applying them in
    # turn is also what keeps the count below honest.
    # THE NETWORK LISTS JUDGE THE RESOLVED RECORD, because a network only exists
    # on what resolution chose — the fill's floor cannot judge a merged card's.
    # The operator's list with the rest of the floor, not counted as this
    # viewer's; the viewer's own with their specs, counted, so a page it emptied
    # can say so.
    # BOTH ARE ONE FUNCTION'S WORK (filter.filter_titles), handed the floor and
    # then the viewer's filters, so a viewer can only ever narrow what the
    # floor left: a rule the operator set cannot be loosened by an account, and
    # nothing about the two is judged differently. Every exemption a read gives
    # a card nothing has described is filter.keep_title's, and never the fill's.
    sparse = calendar_resolve.sparse_sources()
    if settings is not None:
        cards = calendar_filter.filter_titles(
            cards, vocab.title_rules(vocab.floor_specs(settings, media), media), sparse)
    floored = len(cards)
    kept = calendar_filter.filter_titles(cards, vocab.title_rules(specs, media), sparse)
    drawn = []
    for card in kept:
        group, _every, _describing, own, own_describing = pair_of[id(card.record)]
        drawn.append(calendar_resolve.resolve_records(group, own, prefs, own_describing,
                                                      orders=orders))
    return drawn, release_filtered, floored


# ---------------------------------------------------------------------------
# the mark-independent half of a span, and the short memo over it
# ---------------------------------------------------------------------------

# HOW OLD A SPAN MAY BE BEFORE IT IS REFRESHED BEHIND WHOEVER ASKED FOR IT, and
# how old it may be before nobody is served it at all.
#
# SERVE, THEN REBUILD. A reader is answered from what is here and the rebuild
# happens after them, so the only request that ever waits for a month is the
# first one on a cold process. Everything after it is a dictionary lookup, and
# the copy in hand keeps up because serving it is what schedules its replacement.
#
# THE SOFT AGE IS NOT THE ONLY TRIGGER — a write bumps the endpoint's generation
# and that schedules a refresh immediately, so a refill shows up in seconds
# rather than at the end of this clock. The age is the backstop for the writers
# that cannot bump anything: a background enrichment landing between two reads is
# the one that matters, because cache.py cannot be called from enrich.py without
# an import cycle.
#
# THE HARD MAX IS A SAFETY RAIL, NOT THE DESIGN. Reaching it means refreshes have
# been failing for a quarter of an hour, and at that point a slow honest answer
# beats a fast old one. It stays well under the shortest staleness tier so that
# serving from here can never outlast the point at which the ROWS were due to be
# refetched; a test holds that.
_SPAN_SOFT_AGE = 120
_SPAN_MEMO_TTL = 900
# AT MOST THIS MANY REBUILDS AT ONCE, ACROSS THE WHOLE PROCESS. A rebuild is
# seconds of pure Python, which the GIL keeps on the one core the event loop
# needs — so letting these pile up would spend on refreshing months what the
# readers they exist for are waiting to be served. One key is refreshed once at a
# time (`_span_refreshing`) and the process runs at most two.
_SPAN_REFRESH_LIMIT = 2
_span_refreshing: set = set()
# HOW MANY SPANS ARE HELD, AND A MONTH VIEW IS NOT ONE OF THEM — it is
# THIRTY-ONE. The shell asks for the whole month and then every day block asks
# for its own day, each a span of its own with its own key, and all of them worth
# holding: measured on a real All Episodes September, scrolling the month a
# second time went from 23.9 seconds to 260ms, almost all of it the day entries.
#
# At 64 that left room for two month views. A third evicted, and so did reading
# one month on two endpoints — which is the ordinary thing to do. The key still
# carries the viewer's own filters, so this stays bounded: an entry is a list of
# references to Items that already exist, and a caller asking for a hundred
# months still cannot hold them all.
_SPAN_MEMO_MAX = 192
_span_memo: dict = {}


# HOW MANY TIMES EACH ENDPOINT'S STORED ROWS HAVE MOVED. The memo key carries
# its endpoint's number, so writing a window makes every span built from the old
# rows UNREACHABLE without touching anybody else's — which is the difference
# between invalidating what changed and invalidating everything.
_endpoint_generation: dict[str, int] = {}


def _forget_spans(endpoint_key: str | None = None) -> None:
    """Give up the spans a write has just invalidated.

    PER ENDPOINT, AND THE FIRST SHAPE HERE WAS NOT. It cleared the whole memo on
    any window write, on the reasoning that a written window carries none of the
    viewer filters a key does — true, but it made the memo useless on exactly the
    instance that needs it. A live calendar WRITES while it reads: a month opened
    on stale windows fills them, and each fill threw away every span for every
    endpoint, including the one the page had just built. The reader then paid full
    price for every mark until the refresh happened to finish. It only looked
    correct in development, where nothing can reach a provider and so nothing is
    ever written.

    A WINDOW BELONGS TO ONE ENDPOINT, which is the part the old reasoning missed.
    The filters decide what a span makes OF the rows; the endpoint decides WHICH
    rows, and that is knowable at the write. So bumping one number retires every
    span built from the rows that moved and leaves the rest alone.

    `endpoint_key=None` still drops everything, for a caller that cannot say.
    """
    if endpoint_key is None:
        _span_memo.clear()
        _endpoint_generation.clear()
        return
    _endpoint_generation[endpoint_key] = _endpoint_generation.get(endpoint_key, 0) + 1


def forget_endpoint_spans(endpoint_key: str) -> None:
    """Drop every held span of `endpoint_key` outright, for a write that did not
    come through a window fill.

    NOT _forget_spans, which only moves the endpoint's number: a span that is
    behind is still SERVED while its replacement builds behind the reader, which
    is right for a refill nobody is watching and wrong for a write somebody has
    just asked for. A gap filled from a search, a premiere a tracker add wrote,
    or a card's missing service answering on its panel is a single airing whose
    reader expects to see it on the very next read — the search jumps to it and
    the panel's answer redraws the card. With only the number moved, the read
    that follows is handed the span from before the write. These are single
    clicks, so the rebuild they cost is one month's, once.
    """
    _forget_spans(endpoint_key)
    for key in [key for key in _span_memo if key[1] == endpoint_key]:
        _span_memo.pop(key, None)


def _span_memo_key(endpoint, settings, *, tz, start_date, end_date, prefs,
                   **specs) -> tuple:
    """Everything that shapes a span EXCEPT the viewer's marks. `specs` are the
    viewer's filters (`vocab.active_specs`' keywords).

    MARKS ARE THE ONE INPUT LEFT OUT, and leaving them out is the entire point:
    they are applied after this, so a mark does not change what is memoised -
    which is what lets the redraw that ANSWERS a mark hit the entry the page's
    own read just filled. Putting them in the key would make every mark a miss,
    and marking is the case this exists for.

    EVERY PER-VIEWER NARROWING IS IN HERE BY NAME. The stored windows are shared
    and viewer-independent; this is the first point at which one account's
    answer differs from another's, so a key missing a dimension would serve one
    viewer's filtered month to somebody who filtered differently. That is the
    failure tests/calendar/test_resolve.py's TwoViewersOneWindowTests exists to
    catch, and it is why `prefs` and the operator's own network list are both in
    the key rather than assumed constant.
    """
    # THE OPERATOR'S HALF IS SPELLED OUT, NOT ASSUMED CONSTANT. The instance
    # floor and the instance's source admission are applied at READ as well as at
    # fill, precisely so that tightening or loosening either reaches windows
    # already stored (see visible_records). A memo that left them out would
    # defeat that promise for as long as its TTL, and the suite says so:
    # test_cache.py's InstanceFloorAtReadTests change the floor between two reads
    # and require the second to answer differently.
    # ANYTHING ADDED TO THE READ PATH THAT `settings` DECIDES BELONGS HERE TOO.
    # The floor is carried as `vocab.floor_specs` answers it — both media's, so a
    # change to either retires every span — because that function is the only
    # way a read consults the floor (narrow_for_viewer); test_span_memo.py holds
    # every field it reads (vocab.FLOOR_FIELDS) to moving this key.
    floor = (_hashable(vocab.floor_specs(settings, Media.SHOW)),
             _hashable(vocab.floor_specs(settings, Media.MOVIE)),
             tuple(sorted(calendar_resolve.instance_sources(settings) or ())))
    return (
        # WHICH DATABASE THIS CAME FROM. A memo is per PROCESS and the rows are
        # per FILE, so a process re-pointed at another database would otherwise
        # serve the first one's calendar out of the second.
        db.generation(),
        endpoint.key, str(tz), start_date.isoformat(), end_date.isoformat(),
        # EVERY FILTER KEYWORD, from the one tuple naming them (vocab.SPEC_KEYS),
        # so a dimension added there is in the key without being added here.
        _hashable(vocab.complete_specs(specs)), floor, repr(prefs),
    )


def _hashable(specs: dict) -> tuple:
    """A spec set as a key: every keyword in vocab.SPEC_KEYS' order, lists as
    tuples."""
    return tuple((key, tuple(value) if isinstance(value, list) else value)
                 for key, value in ((key, specs[key]) for key in vocab.SPEC_KEYS))


def _refresh_span_later(key, endpoint: Endpoint, settings, **kwargs) -> None:
    """Rebuild one span behind whoever was just served the old one.

    THE ONE IN FLIGHT PER KEY IS THE WHOLE GUARD. Every read of a stale span
    would otherwise start its own rebuild of the same month — a busy page is
    thirty requests — and they would all do the identical work while the reader
    waited on a loop none of them had left alone. A key already being rebuilt is
    simply not scheduled again; the refresh in flight will publish for everyone.

    AND AT MOST TWO ACROSS THE PROCESS. A rebuild is seconds of pure Python, and
    the GIL keeps that on the one core the event loop needs, so the cap is what
    stops refreshes from becoming the thing readers wait behind. Anything turned
    away here is picked up by the next read of that span, which is a moment away.

    IT FILLS AS WELL AS ASSEMBLES (`allow_fetch=True`), and that is the point
    rather than a detail. The fill is where the real seconds were — paced calls
    to a provider — so leaving it on the reader's path would have moved the
    cheap half of the work and left the expensive half exactly where it was. The
    reader is answered from what is stored; going and getting what is NOT stored
    happens behind them.

    NOTHING IS AWAITED AND NOTHING IS RAISED AT THE CALLER. This is scheduled by
    a request that has already been answered, so a failure must not reach it —
    the stale span simply stays, and the next read schedules another attempt.
    """
    if key in _span_refreshing or len(_span_refreshing) >= _SPAN_REFRESH_LIMIT:
        return
    _span_refreshing.add(key)

    async def _run():
        try:
            await _assembled_span(endpoint, settings, allow_fetch=True,
                                  rebuild=True, **kwargs)
        except Exception:
            logger.exception("calendar span refresh failed for %s", endpoint.key)
        finally:
            _span_refreshing.discard(key)

    asyncio.create_task(_run())


def _span_groups(records: list[Record], first: date, last: date, covered: set[date],
                 utc_start: date, utc_end: date) -> list[dict]:
    """The cards a span's airings make (`group_records`) that could be drawn
    inside it. Pure, for a worker thread (`_assembled_span`).

    A WINDOW NOTHING HAS FILLED CONTRIBUTES NO AIRINGS of its own — what sits in
    one is only what a search or a tracker wrote there, and a read that may not
    fill serves it as the empty span it is — while every other stored listing of
    an episode the span holds stays, at whatever date, because it can decide
    where a card inside the span is drawn.

    ONLY A CARD THAT COULD BE DRAWN INSIDE THE SPAN IS RESOLVED. A card is drawn
    at one of its own listings' instants (its describing service's, or the time
    it borrowed), and a zone moves an instant by less than a day — so a card none
    of whose listings falls inside the span padded a day each side cannot land in
    it. The windows a span reads hold up to a week either side of it, which for
    one day was every card of the week resolved and thrown away: measured, a day
    read resolved ~2,500 groups to draw ~350.
    """
    held = [record for record in records
            if not first <= (day := record_utc_date(record)) < last
            or window_start(day) in covered]
    return [group for group in group_records(held)
            if any(utc_start <= record_utc_date(listing) <= utc_end
                   for listing in group["by_source"].values())]


async def _assembled_span(endpoint: Endpoint, settings, *, tz: ZoneInfo,
                          start_date: date, end_date: date, allow_fetch: bool, now, prefs,
                          rebuild: bool = False, **specs):
    """One span's items in air order, with nothing about the reader's marks in
    them: (items, as_of, partial, filtered_out). `specs` are the viewer's
    filters, `vocab.active_specs`' keywords.

    SPLIT OUT OF `assemble_range` SO THAT IT CAN BE MEMOISED, and the split is
    exactly where a viewer's marks first matter. Everything here - reading the
    windows, resolving each group to the record this viewer sees, applying their
    filters, rendering into their timezone - costs about two seconds on a busy
    month (measured: 2,308 items over five windows, roughly half in
    deserialising rows and half in resolving them). None of it depends on which
    titles they are not watching, so redrawing a month because ONE mark changed
    was paying for all of it a second time.

    ONLY AN `allow_fetch=False` READ IS MEMOISED, OR SERVED FROM THE MEMO. Those
    are the reads that redraw something already on screen - a mark's answer, a
    view preference, a share page - where whatever is stored is by definition
    what drew it. A read that may FILL is left alone in both directions: it is
    the one that goes and gets new rows, and its answer is the fresh one
    everybody else's memo is then cleared for.
    """
    specs = vocab.complete_specs(specs)
    key = _span_memo_key(endpoint, settings, tz=tz, start_date=start_date,
                         end_date=end_date, prefs=prefs, **specs)
    # WHATEVER IS HERE IS WHAT THE READER GETS, and the rebuild happens after
    # them. The first shape served only the cheap reads and left opening a month
    # as slow as ever; the second made a filling read wait whenever the rows had
    # moved, which on a live instance is most of the time, because a calendar
    # WRITES while it reads — a month opened on stale windows fills them as its
    # day blocks arrive. Measured on a real instance: marking 5s, switching
    # endpoints 30s, settling only once the refresh finished.
    #
    # So nobody waits for a month twice. A span that is behind, or simply old, is
    # still the answer — it is what the page in front of the reader was drawn
    # from — and asking for it is what schedules the fresh one.
    # `rebuild` IS THE REFRESHER SAYING "NOT YOU". Without it the background job
    # asks the same question its own reader just asked, is handed the very entry
    # it was scheduled to replace, and returns having done nothing — so the span
    # never moves and every read forever schedules another refresh that does not
    # happen. Found by watching the entry's generation stay at 0 while the
    # endpoint's went to 1.
    hit = None if rebuild else _span_memo.get(key)
    if hit is not None:
        age = time.monotonic() - hit[1]
        current = _endpoint_generation.get(endpoint.key, 0)
        if age < _SPAN_MEMO_TTL:
            if hit[2] != current or age >= _SPAN_SOFT_AGE:
                _refresh_span_later(key, endpoint, settings, tz=tz,
                                    start_date=start_date, end_date=end_date,
                                    now=now, prefs=prefs, **specs)
            with span("calcache.memo_hit", items=len(hit[3]),
                      behind=current - hit[2], age=int(age)):
                # THE LIST IS COPIED AND THE ITEMS ARE NOT. `grouped` is built
                # fresh by the caller and a caller may write onto ITS dicts
                # (routes._count_visible puts a per-viewer count on each day), so
                # the structure has to be per-request. An Item is a value nothing
                # in app/ ever assigns to, so sharing one is safe - and copying
                # two thousand of them would give back what this saves.
                return list(hit[3]), hit[4], hit[5], hit[6]
        _span_memo.pop(key, None)

    utc_start, utc_end = _local_span_utc_range(tz, start_date, end_date)
    windows = aligned_windows(utc_start, utc_end)

    # return_exceptions=True both lets a single window fail without aborting the
    # span and stops a still-running sibling fetch from surfacing as an
    # "exception was never retrieved" warning.
    # THE FIRST OF TWO AWAITED PHASES, which is what makes it worth its own span:
    # a read_month slow HERE was waiting on Trakt, Simkl or the database, and one
    # slow in the CPU phases below was blocking every other request while it
    # worked.
    with span("calcache.load_windows", n=len(windows), fetch=allow_fetch):
        results = await asyncio.gather(
            *(ensure_window(endpoint, settings, start, allow_fetch=allow_fetch, now=now)
              for start in windows),
            return_exceptions=True,
        )

    covered: set[date] = set()
    as_of: int | None = None
    errored = 0
    incomplete = False
    waiting = False
    first_error: SourceUnavailable | None = None
    for start, result in zip(windows, results):
        if isinstance(result, SourceUnavailable):
            # ensure_window already served a stale copy when it had one, so
            # getting here means this window had nothing cached AND its fetch
            # failed.
            errored += 1
            first_error = first_error or result
            continue
        if isinstance(result, BaseException):
            # An unexpected failure (not a source reachability problem) is a real
            # bug, not a degraded window — surface it instead of hiding it behind
            # the partial flag.
            raise result
        if result.cached_at is not None:
            covered.add(start)
            as_of = result.cached_at if as_of is None else min(as_of, result.cached_at)
        # THE WINDOW CARRIES ITS OWN VERDICT, and it is the only honest place for
        # it: a source it names as asked but not as having answered was reached
        # for at fill time and could not be read, which is the one thing this
        # flag exists to say. Measuring today's sources against it instead — the
        # shape this replaced — could not tell that apart from a window that
        # simply predates a source, and so reported a failure nobody had.
        # A window nobody filled (an uncached one on a public page) asked nothing
        # and therefore stays quiet, rather than reporting every source missing.
        # A SOURCE STILL WORKING ITS ANSWER OUT IS NOT A FAILURE either: the
        # window waits on it (`_pending`) and says nothing — its answer arrives
        # as a fill of its own, and the stats box counts the weeks it is owed.
        missing = set(result.asked) - set(result.sources)
        owed = missing & pending_sources(endpoint.key, start)
        if missing - owed:
            incomplete = True
        if owed:
            waiting = True

    if errored and errored == len(windows):
        # Every window failed and none had a cached copy: there is no degraded
        # span to render, so surface it as the caller's hard error.
        raise first_error
    partial = errored > 0 or incomplete

    # THE SPAN'S AIRINGS, READ ONCE AND WHOLE, and both halves of that are the
    # point. ONCE: windows are where a fill is decided, not where a card is —
    # matched window by window, one episode two services dated either side of a
    # Monday drew as two cards (see may_join). WHOLE: every other stored listing
    # of each episode the span holds comes with it, at whatever date, because
    # whether a listing here is one card with a listing elsewhere decides which
    # day this viewer's card is drawn on (entries.read_span_whole has the cost).
    # A WINDOW NOTHING HAS FILLED CONTRIBUTES NO AIRINGS of its own, exactly as
    # when it was read by itself: what sits in one is only what a search or a
    # tracker wrote there, and a read that may not fill serves it as the empty
    # span it is. The second awaited phase.
    first, last = windows[0], windows[-1] + timedelta(days=WINDOW_DAYS)
    with span("calcache.read", windows=len(windows)) as sp:
        records = await entries.read_span_whole(endpoint.key, first, last)
        sp.set(records=len(records))

    # THE PHASES BELOW ARE PURE CPU, and they are timed separately because they
    # scale with different things and are fixed in different places. The
    # grouping is a sort plus a walk; the filter is per-viewer work over the
    # cards' facts and the drawn cards' resolution; the render is per-entry
    # object building and timezone arithmetic. The first two RUN ON A WORKER
    # THREAD, so the event loop serves every other request while a month is
    # built (`_span_groups`, `visible_records`) — the GIL is shared, so this
    # costs the month nothing and gives the loop its turns.
    with span("calcache.group", records=len(records)) as sp:
        groups = await anyio.to_thread.run_sync(
            _span_groups, records, first, last, covered, utc_start, utc_end)
        sp.set(groups=len(groups))
    placed_items, filtered_out = await placed(
        groups, endpoint, tz=tz, prefs=prefs, settings=settings, **specs)
    # KEPT WHEN IT IS PLACED INSIDE THE SPAN, which is a question about the
    # CARD, not about any one listing: a card's other services may list it
    # outside the span, and a listing inside the span may belong to a card
    # placed outside it — which is then drawn there, once, and not here.
    items = [item for item in placed_items
             if start_date <= date.fromisoformat(item.air_date) <= end_date]

    with span("calcache.trim", items=len(items)):
        # Sorted on the LOCAL DAY first and the instant second. The two agree for
        # anything converted through one timezone, but a date-only release is
        # pinned to its own calendar date rather than to an offset from an
        # instant — and groupby below needs each day's items contiguous, or one
        # day arrives in two pieces and draws two headings for itself.
        items.sort(key=lambda i: (i.air_date, i.air_ts))

    # ONLY A READ THAT WAS ALLOWED TO FILL MAY WRITE, AND THIS ASYMMETRY IS THE
    # WHOLE CORRECTNESS OF THE MEMO.
    #
    # A `allow_fetch=False` read is not merely cheaper — it can be SHORT. A window
    # with nothing stored returns EMPTY to it rather than raising (see
    # ensure_window), so such a read can legitimately produce a month with days
    # missing from it and no `partial` flag to say so. Memoising that would let
    # the first mark, share view or view toggle on a month nobody has opened yet
    # publish an under-filled span, which the next page load — the read that
    # would have gone and FILLED it — would then be served instead of filling.
    # Caught by tests/calendar/test_routes.py's DayLayoutTests, where marking a
    # title before the page was ever read left every day block empty.
    #
    # So: a read that may fill WRITES and reads; a read that may not only READS.
    # The page load populates, everything cheap rides on it, and nothing that
    # could be short ever gets in.
    #
    # A PARTIAL READ IS NOT KEPT EITHER. `partial` means a window had nothing
    # stored AND its fetch failed. Keeping that would turn one service being
    # briefly unreachable into five minutes of a month that stays wrong however
    # often it is reloaded — and a reload is exactly how somebody expects to
    # recover from it.
    #
    # NOR ONE WAITING ON A SOURCE'S ANSWER, for the same reload: the answer lands
    # in seconds, and the read after it must draw it rather than be served this
    # one while a rebuild runs behind it.
    if partial or waiting or not allow_fetch:
        return items, as_of, partial, filtered_out
    # WHAT IS PAST THE HARD MAX GOES FIRST, ON EVERY WRITE, and none of it would go
    # otherwise: an entry is only ever dropped when somebody reads that exact key,
    # so a month nobody returns to holds its Items until something evicts it.
    # Measured on real data, one All Episodes month view is ~35MB of the 37MB five
    # endpoints cost between them — so reclaiming the dead ones is worth more than
    # any plausible raising of the cap. It used to wait until the memo was FULL,
    # which on a quiet instance that runs for weeks between restarts meant dead
    # months held for all of them; a sweep of at most `_SPAN_MEMO_MAX` entries per
    # write is nothing beside the assembly that write just paid for.
    for dead in [k for k, v in _span_memo.items()
                 if time.monotonic() - v[1] >= _SPAN_MEMO_TTL]:
        _span_memo.pop(dead, None)
    if key not in _span_memo and len(_span_memo) >= _SPAN_MEMO_MAX:
        # THE OLDEST BY WHEN IT WAS BUILT, not by when its key first appeared,
        # and the difference threw away the best entry first. The shell's
        # whole-month span is written BEFORE the thirty day spans that follow it,
        # and re-writing a key does not move it in a dict — so a month refreshed
        # seconds ago still looked like the oldest thing held, and went while the
        # days it had been read with stayed.
        _span_memo.pop(min(_span_memo, key=lambda k: _span_memo[k][1]), None)
    _span_memo[key] = (key, time.monotonic(),
                       _endpoint_generation.get(endpoint.key, 0),
                       list(items), as_of, partial, filtered_out)
    return items, as_of, partial, filtered_out


class Exempting(NamedTuple):
    """Which of a read's exemptions are in play — `exempts_undescribed`'s answer.

    `undescribed`: a filter that shows a card nothing has described unjudged is
    set (genres, countries, certifications, networks, language, missing data).
    `keywords` / `show_types`: a keyword or show-type filter is set, which shows
    unjudged a card no catalogue has stated that fact for (Record.keywords)."""
    undescribed: bool
    keywords: bool
    show_types: bool

    def __bool__(self) -> bool:
        return any(self)

    def lets_through(self, item) -> bool:
        """Whether `item` is on the page only because a filter set here could
        not judge it yet."""
        return ((self.undescribed and not item.enriched)
                or (self.keywords and item.keywords is None)
                or (self.show_types and item.show_type is None))


def exempts_undescribed(media, settings, **specs) -> Exempting:
    """Which exemptions a read under these filters gives — the viewer's
    (`specs`, `vocab.active_specs`' keywords) or the operator's floor; falsy
    when none does.

    THE ONE ANSWER TO "IS THIS CARD HERE ONLY BY EXEMPTION" (`lets_through`).
    The calendar counts such cards to say its filters have not reached them yet,
    and the tracker's import holds them back, because a row it writes does not
    come back off when the facts arrive. Asked in two places, the two answers
    drifted: the count ignored filters entirely and apologised to viewers who had
    none, while the import's own copy had to remember the operator's network list.

    The release specs never exempt anything: they rule on a source's release
    schedule, which comes with the calendar rather than from enrichment. Nor do
    the two rules about who LISTS a card (filter.keep_title says why).
    """
    sets = [vocab.title_rules(vocab.complete_specs(specs), media)]
    if settings is not None:
        sets.append(vocab.title_rules(vocab.floor_specs(settings, media), media))

    def stated(pair) -> bool:
        return bool(pair[0] or pair[1])

    return Exempting(
        undescribed=any(stated(rules.genres) or stated(rules.countries)
                        or stated(rules.certifications) or stated(rules.networks)
                        or stated(rules.languages) or bool(rules.missing)
                        for rules in sets),
        keywords=any(stated(rules.keywords) for rules in sets),
        show_types=any(stated(rules.show_types) for rules in sets))


async def assemble_range(endpoint: Endpoint, settings, *, tz: ZoneInfo,
                         start_date: date, end_date: date,
                         not_watching_ids: set[str] | None = None,
                         allow_fetch: bool = True, now: int | None = None,
                         prefs=None, **specs,
                         ) -> tuple[list[dict], dict]:
    """Assemble one viewer's calendar for the local day span [start_date, end_date].

    The single place the cache is turned into view-ready days. It reads ONLY the
    aligned windows that cover the span — not the whole month — normalizes ONLY
    those entries into the viewer's tz, trims to [start_date, end_date], groups by
    local day, and returns (grouped, meta). A whole-month read is just
    assemble_range(first_of_month, last_of_month); a single day is
    assemble_range(d, d).

    The read path in order: figure the UTC window range covering the span ±1 day
    (a viewer-local day can straddle two UTC windows, so the padding matters);
    load every covering window CONCURRENTLY; RESOLVE each group to the one record
    this viewer sees; apply the per-user genre/country/certification filter to
    those records (before rendering, on the raw genre slugs); render the
    survivors into the viewer's tz; trim to the LOCAL span; apply the network
    filter; sort by air time; group by local day.

    RESOLUTION BEFORE FILTERING, RENDERING AFTER, and the order is not
    interchangeable. A filter has to run against the values the viewer will
    actually be shown, so it comes after resolution; and it matches genres on
    their hyphenated slugs, so it comes before rendering, which is what
    title-cases them.

    `prefs` IS THIS VIEWER'S SOURCE SELECTION, AND IT IS READ ONLY HERE — never
    on the way down to the fill. It reaches `resolve`, which drops a group no
    admitted source describes and picks between the ones that do. The windows
    underneath were filled by asking everybody, so two viewers who have chosen
    differently read the very same rows and each sees their own answer out of
    them; a selection cannot narrow what anybody else is served.
    `prefs=None` means no account is asking (a public share page) and admits
    everything the window holds — everything, that is, that `settings` still
    admits: which services this INSTANCE puts on its calendar is the operator's
    answer and applies to a share page and a signed-in viewer alike.

    RESILIENT BUT LOUD on a window no source can supply. The windows load through
    a single asyncio.gather; a window that raised (nothing cached AND the fetch
    failed) is skipped so the rest of the span still renders, and meta['partial']
    is set so the caller can say the data is incomplete. A window stored while one
    source could not be read sets it too — that source's absence from the stored
    `sources` means UNKNOWN, not "had nothing". Only a span where EVERY window
    failed raises — there is genuinely nothing to show. (A public share read
    passes allow_fetch=False, where a missing window returns empty rather than
    raising, so it never trips the partial path.)

    `specs` ARE THE VIEWER'S FILTERS, `vocab.active_specs`' keywords, and any
    other keyword is refused (vocab.complete_specs). `show_certifications`/
    `movie_certifications` are two separate specs (the two vocabularies don't
    overlap); the one matching `endpoint.media` applies here.

    `movie_release_countries`/`movie_release_types` narrow a FILMS calendar to
    the markets and formats this viewer cares about, and do nothing at all on a
    show endpoint. They apply EARLIER than the other filters — over each group's
    per-source records, before resolution picks between them — because the
    release schedule belongs to whichever source published one and a group must
    not lose a service's record to a rule about something else. See
    filter.filter_release_groups.

    `meta['filtered']` counts every card this viewer's filters removed, so a
    page emptied by it can say which filter emptied it rather than reading as a
    month with nothing in it.

    `meta` carries: total, watching, not_watching (from not_watching_ids, if
    given — otherwise every item counts as watching), show_ids (the span's full,
    de-duped, air-ordered item-id list, which is what is-new diffs against and
    must never be taken from a partially-rendered DOM), as_of (the oldest
    contributing window's cached_at, or None), and partial.

    The hide/card/day-packing view preferences remain the caller's to apply:
    those are per-request view concerns, not part of the data model returned.
    """
    specs = vocab.complete_specs(specs)
    items, as_of, partial, filtered_out = await _assembled_span(
        endpoint, settings, tz=tz, start_date=start_date, end_date=end_date,
        allow_fetch=allow_fetch, now=now, prefs=prefs, **specs)

    with span("calcache.group", items=len(items)):

        grouped = [
            {"date": day,
             "label": day_label(date.fromisoformat(day)),
             "items": list(rows)}
            for day, rows in groupby(items, key=lambda i: i.air_date)
        ]

    # THE ONE PLACE A VIEWER'S MARKS ARE TRANSLATED, and it is here because it is
    # the one place that has both the marks and the items. A stored mark may name
    # a title by whatever id the card carried when it was made; `marked_keys`
    # turns that into mark keys, and everything downstream — this count, the
    # grid, the day chips, the client's own bookkeeping — then asks the plain
    # question. The expanded set travels in `meta` so the caller uses the same
    # answer rather than expanding it a second time and possibly differently.
    nw = calendar_state.marked_keys(not_watching_ids or set(), items)
    not_watching_count = sum(1 for i in items if i.mark_key in nw)
    exempting = exempts_undescribed(endpoint.media, settings, **specs)
    meta = {
        "total": len(items),
        "watching": len(items) - not_watching_count,
        "not_watching": not_watching_count,
        "not_watching_keys": nw,
        # De-duped, first-airing order: one show airing a dozen times in a month
        # is one show as far as "which of these is new since last time" goes, and
        # this list is stored per user per view.
        # BY MARK KEY, not by the winning source's id: this list is what the
        # is-new diff compares between visits, and an id that moves when a
        # viewer reorders their sources would make every card on the month
        # read as new. See Item.mark_key.
        "show_ids": list(dict.fromkeys(i.mark_key for i in items)),
        "as_of": as_of,
        "partial": partial,
        # How many of THIS read's items survived only because a filter could
        # not judge them yet — not a count of every unenriched title on the
        # instance, only the ones this viewer's span rendered and a filter would
        # have ruled on (see exempts_undescribed).
        "unenriched": sum(1 for i in items if exempting.lets_through(i)),
        # HOW MANY CARDS THIS VIEWER'S OWN FILTERS REMOVED from this read —
        # every dimension, not the release rule alone. A filter removes titles
        # rather than re-dating them, so somebody who narrows hard enough can
        # empty a month completely, and an empty month with nothing on the page
        # to explain it reads as the app being broken. The page says how many
        # and offers the way back.
        #
        # NOT the instance-wide floor and not `prune_disguised_films`: those
        # remove titles too and neither is this viewer's to undo, so counting
        # them here would point somebody at a setting that cannot help.
        "filtered": filtered_out,
    }
    return grouped, meta


async def read_month(endpoint: Endpoint, settings, *, tz: ZoneInfo, year: int, month: int,
                     prefs=None, allow_fetch: bool = True, now: int | None = None,
                     **specs) -> tuple[list[Item], int | None]:
    """One viewer's normalized, filtered, month-trimmed calendar items, as a flat
    (items, as_of) pair — the shape the calendar route, the share pages, and the
    distrakt import already unpack.

    A thin wrapper over assemble_range for the whole local month (its [1st, last]
    span): assemble_range owns the window math, the concurrent fetch, and the
    normalize/trim/group. This keeps the well-worn (items, as_of) return; a caller
    that also needs the partial-data flag or the per-span counts calls
    assemble_range directly and reads them off `meta`.

    `prefs` IS WHOSE SOURCE PREFERENCES THIS MONTH IS READ UNDER, passed straight
    through. It is the OWNER's on a share page — a public page shows one person's
    month, and which services fill it is the same editorial choice as the genres
    and countries a share page already reads from them. None is nobody asking,
    which admits every source the stored rows hold.
    """
    days = _calendar.monthrange(year, month)[1]
    grouped, meta = await assemble_range(
        endpoint, settings, tz=tz,
        start_date=date(year, month, 1), end_date=date(year, month, days),
        prefs=prefs, allow_fetch=allow_fetch, now=now, **specs,
    )
    items = [item for day in grouped for item in day["items"]]
    return items, meta["as_of"]


# ---------------------------------------------------------------------------
# heartbeat pre-warm
# ---------------------------------------------------------------------------


# In-memory only: resets on restart, which just causes one extra (harmless)
# warm right after a deploy rather than losing pre-warm state permanently.
# HOW OFTEN A MONTH IS LOOKED AT AGAIN, by how far ahead it is. The month is the
# unit because the month is what a viewer is looking at: a rolling 14- or 42-day
# window would refresh half of what is on screen and leave the rest.
#
# MEASURED AGAINST THE SOURCE, 2026-08-28: Simkl regenerates the current month's
# archive and the two ahead of it on roughly an hourly cycle (all three carried a
# Last-Modified 1.3 hours old and one shared ETag prefix), while past months are
# frozen — 2026-06 and 2025-08 were both 38.9 days old and likewise shared a
# prefix. So refreshing the current month daily is the cadence that actually
# tracks the data, and looking at anything behind it is spending requests on
# files that do not move.
REFRESH_TIERS = (
    (0, 24 * 60 * 60),          # the month a viewer is in: daily
    (1, 7 * 24 * 60 * 60),      # the month ahead: weekly
)


def _month_span(year: int, month: int) -> tuple[date, date]:
    last = _calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _months_ahead(today: date, ahead: int) -> tuple[int, int]:
    month = today.month - 1 + ahead
    return today.year + month // 12, month % 12 + 1


@job("scheduled refresh")
async def refresh_months(settings, *, now: int | None = None) -> int:
    """Re-fetch the months a viewer is most likely to be looking at, on a
    schedule, and return how many spans were refilled.

    IT REPLACES `prewarm_calendar_cache` RATHER THAN JOINING IT. That one warmed
    [today, today+60d] across every endpoint behind a `calendar_prewarm_enabled`
    flag and a 24h-TTL floor — which made it inert on any instance running the
    default ten-minute TTL, so nothing had ever warmed a window here. Leaving
    both would put two mechanisms on different schedules filling the same rows.

    DUE-NESS COMES FROM THE STORED COVERAGE, NOT FROM AN IN-MEMORY MARKER, and
    that is the substantive improvement over what it replaces. `_last_prewarm_at`
    was a module global: a restart forgot it, so a process that restarted often
    warmed constantly and one that ran for a week warmed once. A span records
    when it was last stored, so the schedule survives a restart and two workers
    cannot both decide it is their turn.

    ONE SPAN AT A TIME, SEQUENTIALLY, because this spends the instance's request
    budget with no viewer waiting on it. Its whole reason to exist is to move
    that cost off the read path, and firing a month's worth of endpoints at once
    would move it onto the rate limiter instead.
    """
    ts = db.now() if now is None else now
    today = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    refilled = 0
    for ahead, max_age in REFRESH_TIERS:
        year, month = _months_ahead(today, ahead)
        first, last = _month_span(year, month)
        for endpoint in ENDPOINTS.values():
            for start in aligned_windows(first, last):
                answered, asked, stored = await entries.span_coverage(endpoint.key, start)
                # Due by age, or because a source it asked did not answer — see
                # _FAILED_REFRESH_SECONDS for why age alone left a failure up for
                # days.
                if (stored is not None and (ts - stored) < max_age
                        and not _failed_retry_due(
                            endpoint, start, WindowCoverage(answered, asked, stored), ts,
                            after=_FAILED_REFRESH_SECONDS)):
                    continue
                try:
                    await ensure_window(endpoint, settings, start,
                                        allow_fetch=True, force=True, now=ts)
                except SourceUnavailable:
                    # A source that cannot answer is not a reason to abandon the
                    # rest of the month; the span keeps whatever it had and is
                    # due again next tick.
                    continue
                refilled += 1
    if refilled:
        # Visible at normal log level on purpose: this spends the instance's
        # request budget on a schedule with no viewer present, and an operator
        # should be able to see that it ran.
        _perf.info("calendar refresh: refilled %d span(s) across the current and "
                   "next month.", refilled)
    return refilled
