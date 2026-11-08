"""Global, UTC calendar cache and the read path over it.

Calendar data is the same for everyone, so it is cached once — per (endpoint,
7-day window) — and every viewer reads from the same rows. The design, all
locked by live measurement against the real Trakt API:

  - FETCH IN 7-DAY WINDOWS aligned to a fixed epoch, NOT to "today", so two
    viewers looking at the same month hit the same cache rows. A month view is
    five or six window reads, each cached and TTL'd independently.

  - STORE EACH SOURCE'S NORMALIZED RECORD AS ROWS. app/calendar/entries.py owns
    the listings and coverage, and the title store (app/metadata/) one row per
    title, season and episode holding what each service says; this module owns
    the window arithmetic and the read path over them, and a "window" is a DATE
    RANGE rather than a stored object.
    Normalizing on the way IN is what lets a second source fill the same tables:
    a payload stored raw would have to be interpreted at read time by something
    that knows every source's field layout, and that something is exactly what a
    third source would then have to be added to. Nothing viewer-dependent may go
    in — the four local spellings of an air time are derived at READ, from
    `air_ts`, by app/providers/base.py's `render`.

    ONE EPISODE IS ONE ROW, AND THE ROW IS THE CARD. Which listings are one
    episode is decided as they are WRITTEN (app/metadata/episodes.py's fold), and
    re-decided by every write that touches the title and day — so a later fill
    supplying a missing listing revises it, and a read only reads the row
    (`group_records`). One service listing an episode twice is the one thing a
    row draws as more than one card.

    AND IT IS NOT DONE WINDOW BY WINDOW. A window is the unit a FILL works in;
    a card is ONE EPISODE whichever services list it and however far apart they
    date it, so a read makes its windows current one by one and then reads its
    rows once, whole — every listing of every episode it holds, at any date — and
    draws each card on the day the viewer's own service gives it (`placed`).

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
import logging
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import anyio.to_thread


from . import enrich as calendar_enrich
from . import entries
from . import filter as calendar_filter
from . import query
from . import state as calendar_state
from . import vocab
from .. import after_response
from .. import db
from .. import providers
from ..media import artwork
from ..perftrace import activity, detach, job, span
from ..endpoints import ENDPOINTS, Endpoint
from ..providers.base import (
    Item, Provider, Record, Source, SourceNotModified, SourcePending,
    SourceUnavailable, epoch_moment)

logger = logging.getLogger(__name__)
# Same "app.perf" logger the Trakt transport's cached_get already uses for its own
# netGET/cacheHIT lines — one DEBUG channel for every outbound Trakt call,
# regardless of which module made it. Enable it with LOG_LEVEL=DEBUG.
_perf = logging.getLogger("app.perf")

# HOW LONG ONE WINDOW IS — the storage's grain (entries.WINDOW_DAYS: a coverage
# row says what one window holds), named here for the fill and the read, which
# tile every month by it.
WINDOW_DAYS = entries.WINDOW_DAYS

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


def answer_span(provider, start: date) -> tuple[date, date]:
    """[first, after) — the whole windows an answer from `provider` for the
    window at `start` is authoritative for: every aligned window inside the days
    it speaks for (Capabilities.authoritative), the window asked always among
    them. A fill stores that service's listings inside it and nowhere else, takes
    its listings off inside it and nowhere else, and writes a coverage row for
    each window of it (entries.store_span) — so one Simkl month file fills its
    month's weeks, and a window straddling two months is covered only by an
    answer that read both."""
    first, after = provider.capabilities.authoritative(start, WINDOW_DAYS)
    windows = [window for window in aligned_windows(first, after)
               if window >= first and window + timedelta(days=WINDOW_DAYS) <= after]
    return windows[0], windows[-1] + timedelta(days=WINDOW_DAYS)


def answer_spans(sources, start: date) -> dict[str, tuple[date, date]]:
    """{service: `answer_span`} for each of `sources` answering the window at
    `start` — asked of the registry, so no service is named here."""
    registered = providers.registered()
    return {str(source): answer_span(registered[Source(str(source))], start)
            for source in sources}


def record_utc_date(record: Record) -> date:
    """The UTC calendar day a record airs on. The windows are UTC-aligned, so
    this is the only date that decides which window owns it — a viewer's local
    day is a read-time question and is asked much later."""
    return epoch_moment(record.air_ts).date()


def in_span(record: Record, span: tuple[date, date]) -> bool:
    """Whether a record belongs to [first, after) — the span its service's
    answer is authoritative for (`answer_span`): the window asked, or for a
    calendar published a month at a time, every whole window of the months read.

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

    WHAT LIES BEYOND THE SPAN IS NO ANSWER ABOUT IT, so it is dropped rather than
    stored: those days' entries are some of them, never all, and a fill takes a
    service's listings off only where its answer speaks for every one
    (entries.store_span) — and Trakt declares exactly the window it was asked
    (Capabilities.reach).
    """
    return span[0] <= record_utc_date(record) < span[1]


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


class WindowCoverage(NamedTuple):
    """One window's coverage and nothing else: who ANSWERED and who was ASKED,
    and when it was stored — None for a window nothing has filled.

    THE TWO LISTS ARE DIFFERENT FACTS AND CONFLATING THEM IS WHAT MADE THE
    "incomplete data" banner permanent. A source asked and not answered was
    reached for and could not answer — the only thing "partial" should mean. A
    source in neither was not in play when the window was filled — it did not
    exist on the instance yet, or its reach does not cover the window — which is
    not a failure to report but a window that predates the question, and the
    read treats it as a MISS to be refilled (`ensure_window`).

    WHAT A SPAN READ ASKS OF EACH WINDOW IT COVERS, because the airings are not
    read window by window: whether two services' listings are one card depends
    on dates either side of any window's edge, so the span's rows are read once,
    whole (`_span_views`, app/calendar/query.py), and a window contributes only
    whether it is fresh, partial or missing.
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
                               covered=(), sources=None
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

    The trim is not tidiness. Each service's records are kept inside the span its
    answer is authoritative for (`answer_span`) and nowhere else: a source may
    treat the `days` bound as a floor rather than a ceiling (see in_span), and
    what lies past it is some of those days' entries, never all of them.

    A SOURCE STILL WORKING ITS ANSWER OUT (base.SourcePending) is neither: it is
    left out of the answer, as a refusal is, and remembered as PENDING
    (`_note_pending`), which the store that follows this turns into a fill of
    that source's share alone once its answer is ready (`_follow_pending`).

    `sources`, when given, names which of the sources in play are asked — the
    ones a window's coverage says are due (`ensure_window`); the rest are not
    asked at all.
    """
    in_play = _window_sources(endpoint, settings, start)
    if sources is not None:
        named = set(map(str, sources))
        in_play = [provider for provider in in_play if str(provider.source) in named]
    records, answered, unchanged, pending = await _answers(
        in_play, endpoint, settings, start, covered=covered)
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
    # WHAT IS KEPT OF THE ANSWERS IS WORKED OUT ON A WORKER THREAD (`_kept`), the
    # bridge's read included: it is a pass over every record — a Simkl month is
    # eighteen thousand — and the records are this fill's own.
    kept = await db.run(lambda conn: _kept(conn, records, endpoint, settings, answered, start))
    return kept, answered, unchanged, pending


def _kept(conn: db.Connection, records: list[Record], endpoint: Endpoint, settings,
          answered: list[str], start: date) -> list[Record]:
    """What a fill keeps of what its services answered (`_answers`): the floor
    applied, each service's records trimmed to its span, the ids the matcher
    needs bridged in, and repeats dropped."""
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

    spans = answer_spans(answered, start)
    trimmed = [r for r in kept if in_span(r, spans[str(r.source)])]
    overrun = len(kept) - len(trimmed)
    if overrun:
        logger.debug(
            "%d entr(ies) fell outside the span their service's answer for the %s "
            "window starting %s speaks for; trimmed.", overrun, endpoint.key, start,
        )
    # THE ONE THING THE MATCHER NEEDS THAT A CALENDAR FILE DOES NOT CARRY: the
    # ids a title is known by elsewhere. A source can list an airing under an id
    # space the other source does not use at all — one live title is `mal` on one
    # side and `tmdb` on the other, with nothing shared in either payload — and
    # the group key is derived here, at fill, so an id learned later can never
    # reach it. This is a batched DB read and never a network call; see
    # enrich.overlay_match_ids for why only the IDS are taken and what its
    # realistic ceiling is.
    return dedupe_records(calendar_enrich.match_ids(conn, trimmed))


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

    WHICH LISTINGS ARE ONE EPISODE IS DECIDED HERE, AND REVISED BY EVERY LATER
    WRITE. Each listing is filed on its episode's row, and one that names no full
    episode joins a numbered one only while exactly one candidate exists
    (app/metadata/episodes.py's fold) — re-decided whenever a write touches its
    title and day, so a later fill adding another service's listing revises the
    judgement instead of being frozen out by it.
    """
    # WHAT IS ALREADY KNOWN GOES IN WITH THEM. A fresh calendar payload is
    # unenriched by construction, so storing it as-is would blank every title the
    # drain has already answered for until the next drain tick noticed and put it
    # back. Applying first means a refilled month renders enriched on the very
    # read that refilled it. On a worker thread, read and applied both.
    await db.run(lambda conn: calendar_enrich.give_stored_enrichment(conn, records))
    # EACH SERVICE THAT ANSWERED WITH LISTINGS IS STORED OVER ITS WHOLE SPAN
    # (`answer_span`): one Simkl month file fills, confirms and takes listings
    # off its month's every window, not the one week that asked.
    await entries.store_span(
        endpoint_key, start, start + timedelta(days=WINDOW_DAYS), records,
        sources=sources, asked=(sources if asked is None else asked),
        unchanged=unchanged, spans=answer_spans(sources, start),
        now=now, stale_after=now + max(0, int(ttl_seconds)))


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


# Each (endpoint, service, window) a background refill is already renewing, so
# ten viewers opening the same stale month fire one fetch rather than ten — and
# the weeks one Simkl month file renewed, which lapse together, are renewed by
# ONE refill rather than one a week each storing the whole month again. Keyed by
# service and window because that is what an answer covers: every window of its
# service's answer span (`answer_span`). COUNTED, because two refills' spans can
# overlap — a week straddling two months is read with both months' files — and
# the first to end must not unlatch a week the other is still renewing. A key
# goes when the last refill holding it ends.
_refilling: Counter[tuple[str, str, str]] = Counter()
_refill_tasks: set[asyncio.Task] = set()


def _forget_refill(task: asyncio.Task) -> None:
    _refill_tasks.discard(task)


def schedule_refill(endpoint: Endpoint, settings, start: date,
                    sources: Iterable[str] | None = None) -> bool:
    """Refill one stale span in the background — `sources` alone, the ones its
    coverage says are due, or every source in play — and return whether it
    started one.

    THE POINT OF THE WHOLE STORAGE CHANGE, AND IT IS WORTH SECONDS RATHER THAN
    MILLISECONDS. A lapsed TTL used to make the next viewer WAIT for the fetch
    that renewed it — a page load blocked on Trakt and Simkl answering, five or
    six spans deep for a month, with nothing on screen until they did. Rows can
    be served while they are being replaced, so a stale span is now handed over
    immediately and renewed behind the request — once its answer has been sent
    (`ensure_window`).

    FIRE AND FORGET, NEVER AWAITED, and coalesced by service over what its
    answer covers: a month opened by ten people at once is one refill, not ten,
    and a service already being asked about a window — by this window's refill
    or by a neighbour's whose answer spans it — is not asked again. MEASURED on a
    copy of dev: storing one Simkl month file's answer, 12,386 listings, took
    5–7 s of worker time, and the five weeks it renews lapse at the same moment,
    so a refill per week would have planned the same month five times over. The
    latch is dropped in a `finally` so a failed fetch leaves nothing latched —
    the next read tries again rather than finding the span permanently "already
    refilling" — and a service whose answer turned out to confirm only the
    window asked ("unchanged" speaks for its own validator alone) leaves its
    other weeks to be asked when they are next read.

    DEGRADES TO A NO-OP with no running loop (a script, a sync test path, or the
    app tearing down). The scheduled month refresh still runs every tick, so a
    skipped background refill costs at most the interval until that notices.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    in_play = [str(p.source) for p in _window_sources(endpoint, settings, start)]
    wanted = in_play if sources is None else [s for s in in_play if s in set(map(str, sources))]
    ask = [source for source in wanted
           if (endpoint.key, source, start.isoformat()) not in _refilling]
    if not ask:
        return False
    latched = {(endpoint.key, source, window.isoformat())
               for source, (first, after) in answer_spans(ask, start).items()
               for window in aligned_windows(first, after - timedelta(days=1))}
    # Every service in play, unnarrowed: the whole-window fill (`None`).
    whole = ask == in_play and sources is None

    async def _run() -> None:
        # Nobody waits on this: what it does is no request's time.
        detach()
        try:
            with activity("background refill"):
                asking = await _still_lapsed(endpoint, settings, start, ask)
                if asking:
                    await fill_sources(endpoint, settings, start,
                                       None if whole and asking == ask else asking)
        except SourceUnavailable as exc:
            logger.debug("background refill of the %s span starting %s failed: %s",
                         endpoint.key, start, exc)
        except Exception:  # pragma: no cover — a background task must not vanish silently
            logger.exception("background refill of the %s span starting %s raised.",
                             endpoint.key, start)
        finally:
            for key in latched:
                _refilling[key] -= 1
                if _refilling[key] <= 0:
                    del _refilling[key]

    _refilling.update(latched)
    task = asyncio.create_task(_run())
    _refill_tasks.add(task)
    task.add_done_callback(_forget_refill)
    return True


async def _still_lapsed(endpoint: Endpoint, settings, start: date,
                        sources: Iterable[str]) -> list[str]:
    """`sources` less every one whose reply for this window is fresh NOW — the
    services a refill decided on still owes, asked when it starts.

    A REFILL IS DECIDED BY A READ AND STARTED AFTER THAT READ'S ANSWER
    (`ensure_window`, after_response), and a long answer leaves time for another
    refill to renew the same services and drop its latch. MEASURED on a copy of
    dev, a restart opening October All Episodes with every week lapsed: a filter
    save read the month while the shell's refills were still storing, and its
    own refills, started after its 44-second answer, asked Trakt again for four
    weeks those refills had renewed and stored them a second time. A service
    whose last answer failed, or that was never asked, is still owed."""
    ts = db.now()
    ttl = _ttl_seconds(settings, start, ts)
    held = await entries.source_coverage(endpoint.key, start)
    fresh = {source for source, row in held.items()
             if row.answered and ts - row.stored_at <= ttl}
    return [source for source in sources if source not in fresh]


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

    DECIDED SERVICE BY SERVICE, from each one's own coverage row
    (entries.source_coverage, `_due`):
      - a window nothing has filled, or a service in play it never asked, is
        filled NOW, those services alone — those rows are not stale, they are
        INCOMPLETE, and serving them would show a month missing a whole service
        while the banner said everything was fine;
      - a service whose reply has lapsed, or whose last answer failed and is due
        another try, is asked AFTER THIS REQUEST HAS ANSWERED, alone
        (after_response.later): the window is served from what is stored at
        once — a calendar is not a bank balance, and a span that lapsed a minute
        ago is the same span — and the refill neither delays this page nor
        competes with it for the loop and the database;
      - a service that is fresh is not asked at all — one Simkl month file
        confirms its month's weeks (answer_span), and each of them then waits
        on nobody but Trakt's own clock.
    `force` asks every service in play again, now. Nothing overrides
    allow_fetch, so a share page cannot be made to fetch by anything.
    """
    ts = db.now() if now is None else now
    ttl = _ttl_seconds(settings, start, ts)
    in_play = [str(p.source) for p in _window_sources(endpoint, settings, start)]
    held = await entries.source_coverage(endpoint.key, start)
    cached = _held_coverage(held)
    if not allow_fetch:
        return cached or WindowCoverage((), (), None)
    if force or cached is None:
        return await fill_sources(endpoint, settings, start, None, now=ts)
    unasked, behind = _due(endpoint, start, in_play, held, cached, ts, max_age=ttl,
                           failure_after=_FAILED_RETRY_SECONDS)
    if behind:
        after_response.later(lambda: schedule_refill(endpoint, settings, start, behind))
    if not unasked:
        return cached
    logger.debug("the %s window starting %s was filled without %s; asking now.",
                 endpoint.key, start, ", ".join(unasked))
    return await fill_sources(endpoint, settings, start, unasked, now=ts)


def _held_coverage(held: Mapping[str, entries.SourceCoverage]) -> WindowCoverage | None:
    """What a window's coverage rows say — who replied, who was asked, and when
    it was stored: its earliest reply (entries.span_coverage says why) — or None
    where nothing has filled it. Every row, a service out of play's included:
    its rows are still the window's, and a read narrows them at read."""
    if not held:
        return None
    # In the registry's declared order, which is the order the sources are asked
    # in; a name it does not declare (none, in a running instance) after them.
    declared = {str(source): at for at, source in enumerate(Source)}
    ordered = sorted(held.items(), key=lambda item: (declared.get(item[0], len(declared)),
                                                      item[0]))
    replied = [row.stored_at for _source, row in ordered if row.answered]
    return WindowCoverage(tuple(source for source, row in ordered if row.answered),
                          tuple(source for source, row in ordered if row.asked),
                          min(replied) if replied else min(row.stored_at for _s, row in ordered))


def _due(endpoint: Endpoint, start: date, in_play: list[str],
         held: Mapping[str, entries.SourceCoverage], cached: WindowCoverage, ts: int, *,
         max_age: int, failure_after: int) -> tuple[list[str], list[str]]:
    """(the services in play this window never asked, the services it asked that
    are due again) — one whose reply is older than `max_age`, and one whose last
    answer failed once the failure clock says so (`_failed_retry_due`, which
    notes the attempt). A service still working its answer out is neither."""
    unasked = [source for source in in_play if not (source in held and held[source].asked)]
    lapsed = [source for source in in_play if source not in unasked and held[source].answered
              and ts - held[source].stored_at > max_age]
    failing = [source for source in in_play if source not in unasked
               and not held[source].answered
               and source not in pending_sources(endpoint.key, start)]
    retry = failing if failing and _failed_retry_due(endpoint, start, cached, ts,
                                                     after=failure_after) else []
    return unasked, [*lapsed, *retry]


async def fill_sources(endpoint: Endpoint, settings, start: date,
                       sources: Iterable[str] | None, *, now: int | None = None
                       ) -> WindowCoverage:
    """Ask `sources` — those of the services in play it names, or every one —
    what airs in this window, store their answers, and say what the window then
    holds. Every other service's rows and coverage are left exactly as they are
    (entries.store_span writes only the services it is told were asked). THE ONE
    FILL: a window's first, a service it never asked, a stale service's refill
    behind a reader, and the schedule's (`refresh_months`).

    A FILL EVERY SERVICE IT ASKED REFUSED serves what is stored rather than
    nothing, with each of them recorded as asked and not answered — so one the
    window's rows never had (a month's file can cover a week no other service
    has answered for yet) reads as the failure it is, and is asked again on the
    failure clock (`_failed_retry_due`) rather than on every read — or, with
    nothing stored, raises.
    """
    ts = db.now() if now is None else now
    ttl = _ttl_seconds(settings, start, ts)
    in_play = [str(p.source) for p in _window_sources(endpoint, settings, start)]
    named = None if sources is None else set(map(str, sources))
    ask = [source for source in in_play if named is None or source in named]
    held = await entries.source_coverage(endpoint.key, start)
    cached = _held_coverage(held)
    if named is not None and not ask:
        return cached or WindowCoverage((), (), None)
    # Which sources this span ALREADY holds rows from — the only ones whose
    # "unchanged" is a usable answer rather than a silent gap. Asked of the
    # AIRINGS and not of the coverage table: coverage records who replied, and a
    # source that replied "unchanged" for a span it had never filled is recorded
    # as having answered while leaving nothing behind. Gating on coverage let
    # that state justify itself for ever — see entries.sources_with_rows.
    has_rows = await entries.sources_with_rows(
        endpoint.key, start, start + timedelta(days=WINDOW_DAYS))
    try:
        records, answered, unchanged = await fetch_window_records(
            endpoint, settings, start, covered=has_rows,
            **({} if named is None else {"sources": ask}))
    except SourceUnavailable:
        _note_failure(endpoint, start, ask, (), ts)
        if cached is None:
            raise
        await store_window(endpoint.key, start, [], ttl, ts, sources=[], asked=ask)
        return _held_coverage(await entries.source_coverage(endpoint.key, start))
    await store_window(endpoint.key, start, records, ttl, ts,
                       sources=answered, asked=ask, unchanged=unchanged)
    _note_failure(endpoint, start, ask, [*answered, *unchanged], ts)
    # AFTER THE STORE, never before it: the store records a pending source as
    # asked and not answered, and a follow-up that stored first would have its
    # answer marked missing again by this one.
    _follow_pending(endpoint, settings, start)
    if answered:
        await _after_fill(settings, records)
    # WHAT THE WINDOW HOLDS NOW, every service in play — this fill's and the
    # others'. A SOURCE THAT SAID "UNCHANGED" ANSWERED, beside one that brought
    # rows as much as on its own: naming only the second made the very read this
    # fill served report the first as unreadable — the incomplete-data banner —
    # and, being partial, it was not kept for the reads after it.
    return _held_coverage(await entries.source_coverage(endpoint.key, start)) \
        or WindowCoverage((), tuple(ask), ts)


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
    # A pass over every record a fill stored, so on a worker thread.
    sightings = await anyio.to_thread.run_sync(
        lambda: [s for s in (_poster_sighting(r) for r in records) if s is not None])
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
    none yet — called by the store, once the window's own rows are written,
    and started once the request it stored for has answered (after_response):
    the answer it waits for fills the window behind the page, not beside it."""
    for key, held in list(_pending.items()):
        if key[:2] == (endpoint.key, start.isoformat()) and held.follow_up is None:
            after_response.later(lambda key=key: _start_follow_up(endpoint, settings, start, key))


def _start_follow_up(endpoint: Endpoint, settings, start: date,
                     key: tuple[str, str, str]) -> None:
    # Asked again as it starts: a store of the same window may have started one
    # in between, or the source been given up on.
    held = _pending.get(key)
    if held is None or held.follow_up is not None:
        return
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
# that keeps failing — which leaves the window's own clock at its last reply
# (entries' coverage write) — is retried once a minute rather than on every read.
# A fill a source failed in counts as an attempt (`_note_failure`). Shared by the
# reader and the schedule, so neither repeats the other's attempt. In memory: a
# restart simply makes the first read after it try again.
#
# PRUNED ON THE SLOWER OF THE TWO CLOCKS THAT READ IT. The process runs for weeks
# between restarts and every window that ever failed used to stay here for all
# of them. An attempt older than `_FAILED_REFRESH_SECONDS` delays neither clock —
# each waits from the LATER of the window's store and the attempt, and that much
# has passed since the attempt — so dropping it changes no answer.
_failed_retried_at: dict[tuple[str, str], int] = {}


def _note_failure(endpoint: Endpoint, start: date, asked, replied, ts: int) -> None:
    """A fill of this window that some source it asked did not answer — the
    attempt the failure clock waits from (`_failed_retry_due`). A source still
    working its answer out has not failed."""
    if set(map(str, asked)) - set(map(str, replied)) - pending_sources(endpoint.key, start):
        _failed_retried_at[(endpoint.key, start.isoformat())] = ts


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


async def _span_views(endpoint: Endpoint, settings, *, tz: ZoneInfo, start_date: date,
                      end_date: date, allow_fetch: bool, now, prefs, specs: dict):
    """One span's cards for one viewer, placed and judged, nothing drawn yet:
    (views in air order, as_of, partial, filtered_out). `specs` are the
    viewer's filters, complete (`vocab.complete_specs`).

    NOTHING IS HELD BETWEEN READS. A span used to be memoised whole, its key
    carrying every per-viewer narrowing so one reader was never handed another's
    month; the read is now cheap enough to make every time (app/calendar/
    query.py), so there is no memo and no key to keep complete.
    """
    utc_start, utc_end = _local_span_utc_range(tz, start_date, end_date)
    windows = aligned_windows(utc_start, utc_end)

    # return_exceptions=True both lets a single window fail without aborting the
    # span and stops a still-running sibling fetch from surfacing as an
    # "exception was never retrieved" warning. THE FIRST OF TWO AWAITED PHASES,
    # which is what makes it worth its own span: a read slow HERE was waiting on
    # a service or the database, one slow below was the read itself.
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
        # THE WINDOW CARRIES ITS OWN VERDICT: a source it names as asked but not
        # as having answered was reached for and could not be read, which is the
        # one thing this flag exists to say. A window nobody filled (an uncached
        # one on a public page) asked nothing and stays quiet. A SOURCE STILL
        # WORKING ITS ANSWER OUT IS NOT A FAILURE either: the window waits on it
        # (`_pending`), and its answer arrives as a fill of its own.
        missing = set(result.asked) - set(result.sources)
        if missing - pending_sources(endpoint.key, start):
            incomplete = True

    if errored and errored == len(windows):
        # Every window failed and none had a cached copy: there is no degraded
        # span to render, so surface it as the caller's hard error.
        raise first_error
    partial = errored > 0 or incomplete

    # THE SPAN'S LISTINGS, READ ONCE AND WHOLE, and both halves are the point.
    # ONCE: windows are where a fill is decided, not where a card is — read window
    # by window, one episode two services dated either side of a Monday drew as
    # two cards. WHOLE: every other stored listing of each episode the span holds
    # comes with it, at whatever date, because it is the same card and decides
    # which day this viewer's card is drawn on (query.read_rows). A WINDOW NOTHING
    # HAS FILLED CONTRIBUTES NO LISTINGS of its own: what sits in one is only what
    # a search or a tracker wrote there, and a read that may not fill serves it
    # as the empty span it is.
    # READ BY THE DAYS THE SPAN CAN DRAW, NOT BY THE WEEKS IT COVERS: a card is
    # drawn at one of its own listings' instants, at most a day from the span in
    # any zone (`_local_span_utc_range`), so the rows with a listing in those
    # days are every row that can make a card here — read whole. A day read
    # used to read its one or two whole weeks of rows to draw one day. IN A READ
    # WORKER (query.span), kept to the span's local days and in their order there.
    viewer = query.Viewer(endpoint, settings, prefs, tz, specs)
    gaps = tuple((window, window + timedelta(days=WINDOW_DAYS))
                 for window in windows if window not in covered)
    with span("calcache.read", windows=len(windows)) as sp:
        placed = await query.span(
            viewer, utc_start, utc_end + timedelta(days=1), near=(utc_start, utc_end),
            held=query.Held(gaps), days=(start_date, end_date))
        sp.set(cards=len(placed.views))
    return placed.views, as_of, partial, placed.filtered_out


async def present(endpoint: Endpoint, settings, views, *, tz: ZoneInfo, prefs=None,
                  **specs) -> list[Item]:
    """`views` — a read's cards, placed and judged — as the Items their cards
    draw: the drawn cards' words read for their own rows alone (query.present)."""
    return await query.present(
        query.Viewer(endpoint, settings, prefs, tz, vocab.complete_specs(specs)), views)


async def ranked(endpoint: Endpoint, settings, views, *, tz: ZoneInfo, prefs=None,
                 **specs) -> list:
    """`views` — a big day's cards — with how complete each one is
    (query.ranked), which its first page ranks by (app/calendar/day_pages.py)."""
    return await query.ranked(
        query.Viewer(endpoint, settings, prefs, tz, vocab.complete_specs(specs)), views)


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
                         prefs=None, drawn: bool = True, **specs,
                         ) -> tuple[list[dict], dict]:
    """Assemble one viewer's calendar for the local day span [start_date, end_date].

    `drawn=False` answers with each card's identity and day alone (query.View) —
    what a month's shell counts — and `present` draws the ones it shows.

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
    views, as_of, partial, filtered_out = await _span_views(
        endpoint, settings, tz=tz, start_date=start_date, end_date=end_date,
        allow_fetch=allow_fetch, now=now, prefs=prefs, specs=specs)
    # DRAWN, OR KEYS AND DAYS ONLY: a month's shell states every day's count,
    # chips and is-new marks, which a card's identity and day answer, and draws
    # a few days' cards (`present`, for those).
    items = (await query.present(query.Viewer(endpoint, settings, prefs, tz, specs), views)
             if drawn else views)
    # GROUPED AND COUNTED ON A WORKER THREAD: passes over every card of the read
    # — an unfiltered month is twenty thousand, and its marks alone took 0.26 s
    # on the event loop beside a restart's refills (MEASURED, the span below).
    return await anyio.to_thread.run_sync(lambda: _assembled(
        endpoint, settings, items, specs, not_watching_ids, as_of=as_of, partial=partial,
        filtered_out=filtered_out))


def _assembled(endpoint: Endpoint, settings, items: list, specs: dict,
               not_watching_ids: set[str] | None, *, as_of, partial: bool,
               filtered_out: int) -> tuple[list[dict], dict]:
    """`assemble_range`'s answer from a read's cards in day order: (grouped by
    local day, the read's numbers) — pure, for a worker thread."""
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
    with span("calcache.marks", items=len(items)):
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

    THE SAME RULE AS A READ'S, SERVICE BY SERVICE (`_due`), on the schedule's own
    clocks: a service whose reply is older than the tier allows, and one whose
    last answer failed once the slower failure clock says so
    (_FAILED_REFRESH_SECONDS: age alone left a failure up for days) — and those
    alone are asked, so a month Simkl's file confirmed whole asks Simkl once, not
    once a week. A window nothing has filled is filled whole; a service a filled
    window never asked is the next read's to ask, as it always was.
    """
    ts = db.now() if now is None else now
    today = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    refilled = 0
    for ahead, max_age in REFRESH_TIERS:
        year, month = _months_ahead(today, ahead)
        first, last = _month_span(year, month)
        for endpoint in ENDPOINTS.values():
            for start in aligned_windows(first, last):
                in_play = [str(p.source) for p in _window_sources(endpoint, settings, start)]
                held = await entries.source_coverage(endpoint.key, start)
                cached = _held_coverage(held)
                # A window nothing has filled is filled whole (`None`: every
                # service in play); a filled one only for the services due.
                due = None
                if cached is not None:
                    _unasked, due = _due(endpoint, start, in_play, held, cached, ts,
                                         max_age=max_age, failure_after=_FAILED_REFRESH_SECONDS)
                    if not due:
                        continue
                try:
                    await fill_sources(endpoint, settings, start, due, now=ts)
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
