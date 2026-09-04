"""Global, UTC calendar cache and the read path over it.

Calendar data is the same for everyone, so it is cached once — per (endpoint,
7-day window) — and every viewer reads from the same rows. The design, all
locked by live measurement against the real Trakt API:

  - FETCH IN 7-DAY WINDOWS aligned to a fixed epoch, NOT to "today", so two
    viewers looking at the same month hit the same cache rows. A month view is
    five or six window reads, each cached and TTL'd independently.

  - STORE EACH SOURCE'S NORMALIZED RECORD AS ROWS. app/calendar/entries.py owns
    the tables; this module owns the window arithmetic and the read path over
    them, and a "window" is now a DATE RANGE rather than a stored object.
    Normalizing on the way IN is what lets a second source fill the same tables:
    a payload stored raw would have to be interpreted at read time by something
    that knows every source's field layout, and that something is exactly what a
    third source would then have to be added to. Nothing viewer-dependent may go
    in — the four local spellings of an air time are derived at READ, from
    `air_ts`, by app/providers/base.py's `render`.

    THE GROUPING HAPPENS AT READ, NOT AT FILL, and that is a correction rather
    than a move. Whether two records are one airing depends on WHAT ELSE IS IN
    THE WINDOW (see match_keys: an uncoordinated record folds into a coordinated
    one only when exactly one candidate exists), so storing the answer froze a
    judgement made against whatever happened to be fetched beside it, and a later
    fill supplying the missing airing could not revise it.

    `asked` NAMES WHO WAS REACHED FOR AND `answered` NAMES WHO REPLIED — two
    columns of `calendar_coverage` — and the gap between them is the only thing
    that means "incomplete": a span stored while one source was failing must not
    be served for a whole TTL as though that source had been asked and was empty.
    A source in NEITHER is not in play for that span, which is not a failure; it
    makes the span a MISS to be refilled. Coverage is a table of its own because
    "this source returned no rows" and "this source was never asked" are the same
    absence in the airings table and completely different facts.

    THE FILL ASKS EVERY SOURCE IN PLAY AND STORES THE UNION. No viewer's source
    selection reaches it; whose service a person reads is answered per group at
    READ (app/calendar/resolve.py), over these same shared rows, for exactly the
    reason the per-viewer genre filter is: whoever happened to trigger a fill
    must not decide what everybody else is served for that week.

  - `genres`/`countries`/`show_certifications`/`movie_certifications` ARE NOT
    SENT TO A SOURCE AS QUERY PARAMS, but they DO apply once, at fetch time, as
    the instance-wide content floor: an item any of those four Settings fields
    excludes is filtered out before the window is stored, so it never reaches
    api_cache at all (see app/calendar/filter.py). Every OTHER filter dimension —
    a signed-in viewer's own genre/country/certification/network choices — is a
    separate, read-time layer applied per viewer against that same floored cache.

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
import logging
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from typing import NamedTuple
from zoneinfo import ZoneInfo

from . import enrich as calendar_enrich
from . import entries
from . import filter as calendar_filter
from . import resolve as calendar_resolve
from . import state as calendar_state
from .. import db
from .. import providers
from ..media import artwork
from ..perftrace import activity, job, span
from ..endpoints import ENDPOINTS, Endpoint
from ..providers.base import (
    Item, Provider, Record, SourceNotModified, SourceUnavailable, epoch_moment,
    render, resolve_key)

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

# ---------------------------------------------------------------------------
# grouping â€” one entry per real title, each source's record kept whole
# ---------------------------------------------------------------------------
#
# THERE IS NO PRUNING ANY MORE, and its disappearance is the point rather than a
# side effect. Pruning existed to whitelist which of a raw payload's fields were
# worth storing; a Record already IS that whitelist, per source, by construction.
# The whitelist also carried a trap a second source would have walked straight
# into: it named the id keys it kept, so any id namespace it had not been taught
# about â€” a second service's own id, an anime title's `mal` â€” would have been
# silently dropped on the way in, and the symptom would have been a matcher that
# never matched.

def group_base(record: Record) -> str:
    """The part of a group key that names the TITLE, with no airing in it.

    The cross-source waterfall (app/providers/base.py's resolve_key), never a
    service's own id â€” keying on one of those would make the same title arriving
    from two services two rows for ever. It stringifies whatever id it lands on,
    which is the whole of this app's defence against the type mismatch that
    matters most here: Simkl reports a tmdb id as the string "285652" and Trakt
    reports the same one as the int 285652, and a comparison that did not coerce
    would match NOTHING while looking exactly like sparse coverage from one
    service. Nothing downstream may compare a raw id value for identity.

    A TITLE THE WATERFALL CANNOT KEY GETS A PER-SOURCE BASE and so can never
    merge with anything. That is deliberate: a visible duplicate is safer than a
    wrong merge, and a title nobody can name in a shared id space is one there is
    no honest way to recognize.
    """
    identity = resolve_key(record.media, record.ids)
    return str(identity) if identity is not None else f"{record.source}:{record.id}"


def episode_coords(record: Record) -> tuple[int, int] | None:
    """(season, episode) when a record states BOTH, else None.

    A THIRD OF PREMIERE ENTRIES STATE NEITHER, measured over July and August:
    1230 of them carry no season or no episode number, overwhelmingly Simkl
    anime, where a season is simply not part of how the entry is spelled â€” an
    absolute episode number and nothing else â€” plus a scattering of Trakt records
    that give a season and no number. So "the coordinates" is not a field a
    matcher may assume it has, and None here is the ordinary case rather than a
    malformed one. What such a record can still say is handled by `match_keys`
    below.
    """
    if record.season is not None and record.episode_number is not None:
        return (record.season, record.episode_number)
    return None


def group_key(record: Record) -> str:
    """The key one record would take ON ITS OWN, before anything it was fetched
    beside is considered.

    (shared identity, season, episode) for an episodic and (shared identity) for
    a film, exactly as two services listing the same S02E05 have to become one
    card while one show's different episodes must never collapse.

    THIS IS NOT THE FINAL KEY. A record stating no coordinates can still turn out
    to be the same airing as one that states them, and deciding that needs the
    other records in the window â€” see `match_keys`, which is what `group_records`
    actually keys on. This function stays because "what does this record say
    about itself" is a separate question worth asking on its own.
    """
    base = group_base(record)
    coords = episode_coords(record)
    return f"{base}|{coords[0]}|{coords[1]}" if coords else base


def match_keys(records: list[Record]) -> list[str]:
    """The final group key for each of `records`, in the same order â€” THE
    MATCHER.

    Everything above answers "what does one record call itself". This answers the
    question that actually decides whether two services produce one card or two,
    and it needs the whole window because the hard case cannot be seen from one
    record: TWO SOURCES CAN AGREE PERFECTLY ABOUT THE ID AND STILL FAIL TO MATCH,
    because they describe the airing at DIFFERENT RESOLUTIONS. Measured on real
    stored windows: one service lists an anime premiere as episode 1 with no
    season at all and the other lists it as S01E01, same title, same tmdb id,
    same day â€” and the coordinates being part of the key is what made those two
    cards instead of one.

    So a record that states no full coordinate is folded into a coordinated
    airing of the SAME TITLE when exactly one of them can be the airing it means:

      - same base (the same title in the same shared id space),
      - same UTC calendar day, because a show airing twice in one week is two
        airings and nothing here may guess which one an uncoordinated record is,
      - and agreement on whichever half of the coordinate the record DID state.
        This is the condition that carries most of the weight: one live window
        holds eight uncoordinated records for a single title on one day, one per
        episode, against that title's eight coordinated ones â€” day alone would
        make all eight ambiguous and match none of them, while the stated episode
        number picks each one out exactly.

    AMBIGUITY IS COUNTED BEFORE ANYTHING ELSE NARROWS IT, AND THAT ORDER IS THE
    WHOLE SAFETY ARGUMENT. Every coordinated airing of the title on that day is a
    candidate, whoever listed it; only if there is EXACTLY ONE is the record
    folded in, and only then is it asked whether that airing already carries a
    record from this same source â€” in which case it is refused too, because
    folding a service's uncoordinated listing into its own coordinated one would
    be collapsing that service's listing rather than reconciling two of them, and
    a service listing one airing twice is a repeat the calendar has always drawn
    twice. Counting the other way round â€” narrowing by source and THEN counting â€”
    is a real trap and not a theoretical one: on a day where one service lists
    season 4 and the other season 5 of the same show, it would leave exactly one
    survivor for an uncoordinated third record and merge it into the wrong
    season, silently.

    ZERO CANDIDATES OR SEVERAL, AND THE RECORD KEEPS ITS OWN KEY and renders as
    its own card. That is the direction this has to fail in: a visible duplicate
    is a cosmetic complaint, and a wrong merge hides one title behind another
    with nothing on the page to say so.

    WHAT THIS DELIBERATELY DOES NOT DO is match on title and day. Two different
    SEASONS of one show premiering on the same day is a real thing â€” three live
    examples, all with both records fully coordinated â€” and any rule of the form
    "same title, same day, one card" destroys them. They are untouched here
    precisely because both sides state a full coordinate and neither is a
    candidate for folding.
    """
    bases = [group_base(record) for record in records]
    coords = [episode_coords(record) for record in records]
    keys = [f"{b}|{c[0]}|{c[1]}" if c else b for b, c in zip(bases, coords)]

    # What each title's fully-coordinated airings are, and who listed them on
    # which day â€” the only thing an uncoordinated record can be folded into.
    coordinated: dict[str, dict[tuple[int, int], dict]] = {}
    for index, record in enumerate(records):
        if coords[index] is None:
            continue
        airing = coordinated.setdefault(bases[index], {}).setdefault(
            coords[index], {"sources": set(), "days": set()})
        airing["sources"].add(str(record.source))
        airing["days"].add(record_utc_date(record))

    for index, record in enumerate(records):
        if coords[index] is not None:
            continue
        day = record_utc_date(record)
        airings = coordinated.get(bases[index], {})
        candidates = [
            coordinate for coordinate, airing in airings.items()
            if day in airing["days"]
            and (record.episode_number is None or coordinate[1] == record.episode_number)
            and (record.season is None or coordinate[0] == record.season)
        ]
        if len(candidates) != 1:
            continue
        coordinate = candidates[0]
        if str(record.source) in airings[coordinate]["sources"]:
            continue
        keys[index] = f"{bases[index]}|{coordinate[0]}|{coordinate[1]}"
    return keys


def group_records(records: list[Record]) -> list[dict]:
    """The stored `entries` list: one group per airing, each source's record kept
    whole under its own name.

    PROVENANCE IS RECORDED FOR EVERY FIELD, not only for the ones that disagree,
    because "both sources agreed" and "only one source had it" must be able to
    render differently and the second is not recoverable from a de-duplicated
    blob. Storage cost is not a concern here and must not be optimized against â€”
    windows measure a few kilobytes compressed, and de-duplicating equal values
    would destroy the agreement signal to save nothing.

    `ids` IS HOISTED onto the group because it is the MATCH RESULT rather than
    any one source's value, and it is what the arr/Seerr buttons, the tracker and
    the ranker read. First writer wins per namespace, which with sources visited
    in declared order means the earlier source's spelling of an id is the one
    kept.

    A KEY COLLIDING WITH ITSELF FOR ONE SOURCE is a repeated airing â€” the same
    episode listed twice at different times â€” and gets a distinct key rather than
    overwriting, because the calendar has always drawn both and this is not the
    place to decide it should stop. Two records from DIFFERENT sources under one
    key is the ordinary case and is exactly what the group is for.

    WHICH RECORDS SHARE A KEY IS `match_keys`' ANSWER, NOT THIS FUNCTION'S. This
    one owns the shape of what gets stored; that one owns what "the same airing"
    means, and the split is worth keeping because the second is the part with a
    live-data argument behind every clause of it.
    """
    index: dict[str, dict] = {}
    out: list[dict] = []
    for record, base in zip(records, match_keys(records)):
        name = str(record.source)
        key, bump = base, 1
        while key in index and name in index[key]["by_source"]:
            bump += 1
            key = f"{base}#{bump}"
        group = index.get(key)
        if group is None:
            group = {"key": key, "ids": {}, "by_source": {}}
            index[key] = group
            out.append(group)
        group["by_source"][name] = record.to_dict()
        for namespace, value in (record.ids or {}).items():
            if value not in (None, ""):
                group["ids"].setdefault(namespace, value)
    return out


class CachedWindow(NamedTuple):
    """One stored window: its groups, who was ASKED for them, and who ANSWERED.

    THE TWO LISTS ARE DIFFERENT FACTS AND CONFLATING THEM IS WHAT MADE THE
    "incomplete data" banner permanent. A source in `asked` but not in `sources`
    was reached for and could not answer â€” that is the only thing "partial"
    should ever mean. A source in neither was not in play when this window was
    filled: either it did not exist on the instance yet, or its declared reach
    does not cover this window at all. That is not a failure to report, it is a
    window that predates the question â€” and the read path treats it as a MISS to
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


def _poster_sighting(record: Record):
    """(media, tmdb, source, url) for one record, or None when it lacks either
    id â€” the tuple the ranker's poster registry is keyed on."""
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
    back in play, `load_window` sees it in `in_play` and absent from the stored
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
    """
    records: list[Record] = []
    answered: list[str] = []
    unchanged: list[str] = []
    refusal: SourceUnavailable | None = None
    asked = 0
    for provider in _window_sources(endpoint, settings, start):
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
        except SourceUnavailable as exc:
            refusal = refusal or exc
            logger.debug("%s could not answer the %s window starting %s: %s",
                         provider.source, endpoint.key, start, exc)
            continue
        answered.append(str(provider.source))
        records.extend(got)
    if asked and not answered and not unchanged:
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
    return dedupe_records(bridged), answered, unchanged


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
                       unchanged=()) -> list[dict]:
    """Store one window's records as rows, and hand back the groups a caller
    about to serve the same window would otherwise build a second time.

    THE GROUPING IS NO LONGER WHAT IS STORED. It used to be: the window blob held
    the finished groups, so the match ran once at fill and every read served its
    answer. Rows store each source's records separately and `match_keys` runs at
    READ instead — which is not a regression but a correction. Whether two
    records are one airing depends on WHAT ELSE IS IN THE WINDOW (see match_keys:
    an uncoordinated record folds into a coordinated one only when exactly one
    candidate exists), so a fill that stored the answer froze a judgement made
    against whatever had been fetched alongside it. A later fill adding the
    missing coordinated airing could not revise it.
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
    return group_records(records)


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
                await load_window(endpoint, settings, start,
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


async def load_window(endpoint: Endpoint, settings, start: date, *,
                      allow_fetch: bool = True, force: bool = False,
                      now: int | None = None,
                      ) -> tuple[CachedWindow, int | None]:
    """Return (window, cached_at) for one window.

    Fetches and caches when the window is missing, past its TTL, or was filled
    without a source that is in play for it now — and allow_fetch is set. A
    public share page passes allow_fetch=False: it serves whatever is cached —
    even stale, even nothing (returning an empty window, None) — and never asks a
    source, so an unauthenticated visitor can never spend the instance's rate
    limit. cached_at is None only when nothing was cached and nothing was
    fetched.

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
    cached = await read_cached_window(endpoint.key, start)
    # Which sources this span ALREADY holds rows from — the only ones whose
    # "unchanged" is a usable answer rather than a silent gap. Asked of the
    # AIRINGS and not of the coverage table: coverage records who replied, and a
    # source that replied "unchanged" for a span it had never filled is recorded
    # as having answered while leaving nothing behind. Gating on coverage let
    # that state justify itself for ever — see entries.sources_with_rows.
    has_rows = await entries.sources_with_rows(
        endpoint.key, start, start + timedelta(days=WINDOW_DAYS))
    if cached is not None:
        window, cached_at = cached
        # `force` IS THE SCHEDULED REFRESH'S OWN CADENCE OVERRIDING THE READ
        # PATH'S. The TTL here answers "is this fresh enough to serve", which
        # is a different question from "is this month due a look" — see
        # refresh_months. It never overrides allow_fetch, so a share page
        # cannot be made to fetch by anything.
        fresh = (ts - cached_at) <= ttl and not force
        unasked = set(in_play) - set(window.asked)
        if not allow_fetch or (fresh and not unasked):
            return window, cached_at
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
                return window, cached_at
        if unasked:
            logger.debug(
                "the %s window starting %s was filled without %s; refilling.",
                endpoint.key, start, ", ".join(sorted(unasked)))
    elif not allow_fetch:
        return CachedWindow([], (), ()), None
    try:
        records, answered, unchanged = await fetch_window_records(
            endpoint, settings, start, covered=has_rows)
    except SourceUnavailable:
        if cached is not None:  # serve the stale copy rather than nothing
            return cached
        raise
    groups = await store_window(endpoint.key, start, records, ttl, ts,
                                sources=answered, asked=in_play, unchanged=unchanged)
    if unchanged and not answered:
        # EVERY SOURCE SAID "UNCHANGED", SO THIS FETCH BROUGHT NO RECORDS — and
        # `groups` is therefore empty while the stored rows are perfectly good.
        # Serving what was just built would blank the month on the very read that
        # confirmed it was current, which is the worst possible outcome of a
        # successful conditional GET. The store above still ran: it advanced the
        # coverage clock without touching an airing.
        refreshed = await read_cached_window(endpoint.key, start)
        if refreshed is not None:
            return refreshed
    # A FILL, NOT A READ — this line only runs when the branch above actually
    # fetched and stored, never on a cache-hit return further up. That is
    # what lets it ask for enrichment sooner than the next heartbeat tick
    # without weakening "the read path never makes an outbound call": nothing
    # here is served to a viewer yet, and the scheduled drain is fire-and-
    # forget (see enrich.schedule_drain) so this request does not wait on it
    # either. A pure cache-hit read never reaches this line at all.
    calendar_enrich.schedule_drain(settings)
    # Recorded only on a genuine fetch, not on every cache-hit read: the URL
    # arrived here already, so this is the point a lookup is "paid for" rather
    # than a per-view cost added to the hot render path.
    sightings = (s for s in (_poster_sighting(r) for r in records) if s is not None)
    await artwork.record_poster_urls(sightings)
    return CachedWindow(groups, tuple(answered), tuple(in_play)), ts


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


def dedupe_groups(groups: list[dict]) -> list[dict]:
    """First occurrence of each airing across the windows a span covers, order
    preserved.

    Keyed on the group key AND the primary source's instant, which is the same
    identity the per-source dedupe uses one layer down: two adjacent windows both
    handed the same airing are one card, while a title genuinely listed twice at
    different times stays two, exactly as it has always rendered.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for group in groups:
        order = calendar_resolve.source_order(group)
        primary = (group.get("by_source") or {}).get(order[0], {}) if order else {}
        identity = (group.get("key"), primary.get("air_ts"))
        if identity in seen:
            continue
        seen.add(identity)
        out.append(group)
    return out


async def visible_records(groups, endpoint, *, genres="", countries="",
                          show_certifications="", movie_certifications="",
                          movie_release_countries="", movie_release_types="",
                          prefs=None, settings=None) -> tuple[list, int]:
    """`groups` reduced to the records THIS viewer's calendar would draw, and
    how many films the release narrowing removed on the way.

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
        # `settings` GOES IN BESIDE `prefs` AND ANSWERS A DIFFERENT QUESTION.
        # `prefs` is this viewer's; `settings` is the operator's — a source this
        # instance has switched off its calendar is off for everybody, and a
        # window filled while it was still in play is still sitting in the cache
        # holding its records. Reading it here is what makes that switch take
        # effect immediately instead of one TTL from now.
        parsed = [(group, calendar_resolve.admitted_records(group, prefs, endpoint.key, settings))
                  for group in dedupe_groups(groups)]
        # THE SIMKL OVERLAY IS GONE FROM HERE, AND ITS ABSENCE IS THE POINT.
        # What used to happen on this line was a batched read of `simkl_titles`
        # to paint genres, network, country and certification onto this read's
        # Simkl records, because the stored window could not hold them: a fill
        # replaced the whole blob with a fresh, unenriched payload, so anything
        # written into it was erased on the next refill. Rows do not have that
        # problem — the drain writes `calendar_titles` and a refill is forbidden
        # to demote it (see entries._UPSERT_TITLE) — so the fields are already on
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
        # THE RELEASE NARROWING RUNS HERE, BEFORE THE PICK, AND ON THE GROUP.
        # A films calendar that lists every release in every market needs a way
        # to say "the ones out here, in the formats I watch"; the per-country
        # release schedule only exists on the records of whichever source
        # published one, so asking after resolution would ask the winner alone
        # and asking per record would strip a service off a merged card to
        # enforce a rule about release formats. Counted rather than silently
        # applied, so a month this empties can say so — see meta below.
        narrowed = calendar_filter.filter_release_groups(
            parsed, endpoint.media, movie_release_countries, movie_release_types)
        release_filtered = len(parsed) - len(narrowed)
        records = [r for r in (calendar_resolve.resolve_records(group, rs, prefs)
                               for group, rs in narrowed) if r is not None]
        # A Simkl entry Simkl's OWN enrichment marks as a film (`anime_type ==
        # "movie"`) does not belong on a series endpoint — see
        # filter.prune_disguised_films for the measured rule and why this is
        # a read-time exclusion rather than something fetch_window_records can
        # do at fill. Same reasoning as the exemption below: it can only act
        # on what enrichment has already found, so it runs right beside it.
        records = calendar_filter.prune_disguised_films(records, endpoint.media)
        certifications = show_certifications if endpoint.media == "show" else movie_certifications
        # exempt_unenriched=True ONLY here, never at the floor in
        # fetch_window_records — see filter.keep_record for why the two reads
        # must not share that setting.
        kept = calendar_filter.filter_records(records, genres, countries, certifications,
                                              exempt_unenriched=True)
        sp.set(kept=len(kept))

    return kept, release_filtered


async def assemble_range(endpoint: Endpoint, settings, *, tz: ZoneInfo,
                         start_date: date, end_date: date,
                         genres: str = "", countries: str = "",
                         show_certifications: str = "", movie_certifications: str = "",
                         movie_release_countries: str = "", movie_release_types: str = "",
                         network_filter=None, not_watching_ids: set[str] | None = None,
                         allow_fetch: bool = True, now: int | None = None,
                         prefs=None,
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

    `show_certifications`/`movie_certifications` are two separate specs (the two
    vocabularies don't overlap); the one matching `endpoint.media` applies here.

    `movie_release_countries`/`movie_release_types` narrow a FILMS calendar to
    the markets and formats this viewer cares about, and do nothing at all on a
    show endpoint. They apply EARLIER than the other filters — over each group's
    per-source records, before resolution picks between them — because the
    release schedule belongs to whichever source published one and a group must
    not lose a service's record to a rule about something else. See
    filter.filter_release_groups.

    `meta['release_filtered']` counts how many groups that rule removed, so a
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
    utc_start, utc_end = _local_span_utc_range(tz, start_date, end_date)
    windows = aligned_windows(utc_start, utc_end)

    # gather preserves argument order, so `results` stays in ascending window
    # order and extending `groups` in that order keeps the windows ordered —
    # which dedupe_groups below relies on to keep the SAME copy of an
    # overlapping airing the old sequential loop did (first window wins).
    # return_exceptions=True both lets a single window fail without aborting the
    # span and stops a still-running sibling fetch from surfacing as an
    # "exception was never retrieved" warning.
    # THE ONLY AWAITED PHASE IN THIS FUNCTION, which is what makes it worth its
    # own span: everything below is synchronous CPU on the event loop, so a
    # read_month that is slow HERE was waiting on Trakt or the database, and one
    # that is slow below was blocking every other request while it worked.
    with span("calcache.load_windows", n=len(windows), fetch=allow_fetch):
        results = await asyncio.gather(
            *(load_window(endpoint, settings, start, allow_fetch=allow_fetch, now=now)
              for start in windows),
            return_exceptions=True,
        )

    groups: list[dict] = []
    as_of: int | None = None
    errored = 0
    incomplete = False
    first_error: SourceUnavailable | None = None
    for result in results:
        if isinstance(result, SourceUnavailable):
            # load_window already served a stale copy when it had one, so getting
            # here means this window had nothing cached AND its fetch failed.
            errored += 1
            first_error = first_error or result
            continue
        if isinstance(result, BaseException):
            # An unexpected failure (not a source reachability problem) is a real
            # bug, not a degraded window — surface it instead of hiding it behind
            # the partial flag.
            raise result
        window, cached_at = result
        groups.extend(window.groups)
        if cached_at is not None:
            as_of = cached_at if as_of is None else min(as_of, cached_at)
        # THE WINDOW CARRIES ITS OWN VERDICT, and it is the only honest place for
        # it: a source it names as asked but not as having answered was reached
        # for at fill time and could not be read, which is the one thing this
        # flag exists to say. Measuring today's sources against it instead — the
        # shape this replaced — could not tell that apart from a window that
        # simply predates a source, and so reported a failure nobody had.
        # A window nobody filled (an uncached one on a public page) asked nothing
        # and therefore stays quiet, rather than reporting every source missing.
        if set(window.asked) - set(window.sources):
            incomplete = True

    if errored and errored == len(windows):
        # Every window failed and none had a cached copy: there is no degraded
        # span to render, so surface it as the caller's hard error.
        raise first_error
    partial = errored > 0 or incomplete

    # The dedupe is belt and braces over the trim in fetch_window_records. That
    # one keeps NEW windows disjoint; this one also covers windows cached BEFORE
    # the trim existed, which overlap and would otherwise keep rendering doubled
    # cards until their TTL expired.
    # THE THREE PHASES BELOW ARE PURE CPU ON THE EVENT LOOP, and they are timed
    # separately because they scale with different things and are fixed in
    # different places. The filter is per-viewer work over resolved records; the
    # render is per-entry object building and timezone arithmetic, and is the one
    # that grows with a busy month; the grouping is a sort plus a walk.
    with span("calcache.filter", entries=len(groups)) as sp:
        kept, release_filtered = await visible_records(
            groups, endpoint, genres=genres, countries=countries,
            show_certifications=show_certifications,
            movie_certifications=movie_certifications,
            movie_release_countries=movie_release_countries,
            movie_release_types=movie_release_types,
            prefs=prefs, settings=settings)
        sp.set(kept=len(kept))

    with span("calcache.normalize", entries=len(kept)) as sp:
        items: list[Item] = []
        for record in kept:
            item = render(record, tz)
            air_day = date.fromisoformat(item.air_date)  # already in the viewer's tz
            if start_date <= air_day <= end_date:
                items.append(item)
        sp.set(items=len(items))

    with span("calcache.group", items=len(items)):
        items = calendar_filter.filter_by_network(items, network_filter, exempt_unenriched=True)
        # Sorted on the LOCAL DAY first and the instant second. The two agree for
        # anything converted through one timezone, but a date-only release is
        # pinned to its own calendar date rather than to an offset from an
        # instant — and groupby below needs each day's items contiguous, or one
        # day arrives in two pieces and draws two headings for itself.
        items.sort(key=lambda i: (i.air_date, i.air_ts))

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
        # How many of THIS read's items survived only because of the
        # exempt_unenriched grace period above — not a count of every
        # unenriched title on the instance, only the ones this viewer's span
        # actually rendered. Nothing reads this yet; it exists so a future
        # Sources-preference screen can say "N items here are still catching
        # up" without inventing a second way to ask the question this
        # function already answers.
        "unenriched": sum(1 for i in items if not i.enriched),
        # How many titles the release filter removed from THIS read. A country
        # rule removes films rather than re-dating them — a film released only
        # in Brazil does not match a US filter at all — so a viewer who narrows
        # hard enough can empty a month completely, and an empty month with
        # nothing on the page to explain it reads as the app being broken. The
        # calendar says which filter did it and offers the way back.
        "release_filtered": release_filtered,
    }
    return grouped, meta


async def read_month(endpoint: Endpoint, settings, *, tz: ZoneInfo, year: int, month: int,
                     genres: str = "", countries: str = "",
                     show_certifications: str = "", movie_certifications: str = "",
                     movie_release_countries: str = "", movie_release_types: str = "",
                     network_filter=None, prefs=None,
                     allow_fetch: bool = True, now: int | None = None) -> tuple[list[Item], int | None]:
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
        genres=genres, countries=countries,
        show_certifications=show_certifications, movie_certifications=movie_certifications,
        movie_release_countries=movie_release_countries,
        movie_release_types=movie_release_types,
        network_filter=network_filter, prefs=prefs, allow_fetch=allow_fetch, now=now,
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
                stored = await entries.span_stored_at(endpoint.key, start)
                if stored is not None and (ts - stored) < max_age:
                    continue
                try:
                    await load_window(endpoint, settings, start,
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
