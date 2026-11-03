"""The calendar's storage: its listings, its coverage, and the reads that turn them
back into the records a card is drawn from.

WHAT A TITLE AND AN EPISODE ARE, IS NOT THE CALENDAR'S. The title store
(app/metadata/) holds one row per title, per season and per episode, every
service's facts side by side; a calendar's LISTING of an episode is that
service's instants on the episode's row (app/metadata/episodes.py), each with
the calendars it is on. A fill hands its records to the store's
`metadata.write_records`, which files each under its title's key, and their
instants are filed on their episodes' rows in the same transaction. Which
listings make one card is the row: decided as they are written, not at every
read.

NOTHING HERE IS PER-VIEWER, and that is the invariant the whole design serves.
Every source's listings are stored whole; which one a viewer sees, and whether
their filters hide it, is decided at READ against their own preferences.
`app/calendar/resolve.py` owns the read-time decision; this module owns the rows.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from datetime import date, timedelta
from typing import NamedTuple

from .. import db, endpoints, metadata, perftrace, providers
from ..metadata import episodes as episode_rows
from ..metadata import groups, identity as title_identity
from ..metadata import rows as title_rows
from ..metadata import tables, typed_search
from ..providers.base import Media, Record, Source, epoch_moment

logger = logging.getLogger(__name__)


# HOW MANY DAYS ONE COVERAGE ROW SPEAKS FOR. A row of `calendar_coverage` says which
# services were asked about one window of a calendar, beginning `span_start`, and
# which replied and when — so retention reads a window's listings by it, and the
# fill writes one per window its answer covers (app/calendar/cache.py tiles the
# windows by `window_start`).
WINDOW_DAYS = 7


def _bit(endpoint_key: str) -> int:
    """The calendar's bit in a listing's `calendars` mask (Endpoint.bit)."""
    return endpoints.ENDPOINTS[endpoint_key].bit


def _utc_date(air_ts: float) -> str:
    # Through the shared conversion so this agrees with every other reading of a
    # record's air time, including on a platform whose C library refuses dates
    # before 1970 — see providers/base.epoch_moment.
    return epoch_moment(air_ts).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# writing a fill
# ---------------------------------------------------------------------------
#
# A SEARCHED LISTING FILLS A GAP IN ITS SERVICE'S FEED, SO IT STANDS DOWN THE
# MOMENT THE FEED LISTS THAT EPISODE. A calendar search followed, and a title
# added to somebody's tracker (app/distrakt/arrival.py), write an instant no feed
# offered — marked `searched` for its calendar — and a service's per-title answer
# and its feed routinely disagree on the instant: Simkl's lookups keep a
# premiere's day and its calendar files give the time, so East of Eden's S01E01
# was a date-only searched listing at 00:00 UTC beside the feed's 07:00. Both
# stood, one service listed the episode twice, and the read drew it as two cards
# — MEASURED, every such title on the dev instance once a refill brought the
# feed's listing. So the rule is said by the EPISODE, at any instant: a fill takes
# the searched instants its records or its span supersede off the calendar
# (`store_span`), a search writes none for an episode the feed already lists
# (`store_loose_airings`), and a read drops one whose service's feed lists the
# same episode (`_records`).
#
# EVERY STATEMENT WRITES A WHOLE FILL, and what it writes is decided before the
# transaction opens. A fill's write lock is held for as long as its statements
# take, and while the event loop is busy each statement can wait a GIL switch
# interval to win the GIL back — so a store that issued a statement or eight per
# record held the lock 5.3 s under one month read, MEASURED on a copy of a real
# instance, and the next writer gave up at the busy timeout.

# A COVERAGE ROW'S `stored_at` IS WHEN ITS SERVICE LAST REPLIED FOR THE WINDOW —
# with listings, or "unchanged" — and it is what keeps that service's listings in
# the window (`sweep`), so a refill that changes nothing keeps them by this row
# alone. A FAILED ANSWER says nothing about them: it marks the service asked and
# not answered and leaves the time of its last reply where it was. Moving it would
# keep listings no reply had confirmed — a service whose token stopped working
# would hold its last listings for as long as somebody kept asking.
#
# ONE STATEMENT FOR EVERY WINDOW A STORE COVERS, each row [window start, service,
# whether it replied]: the window asked for every service asked, and every other
# window an answer is authoritative for (cache.answer_span) for the service whose
# answer it was.
_UPSERT_COVERAGE = """
INSERT INTO calendar_coverage
    (endpoint, span_start, source, asked, answered, stored_at, stale_after)
SELECT :endpoint, json_extract(value, '$[0]'), json_extract(value, '$[1]'), 1,
       json_extract(value, '$[2]'), :now, :stale_after
FROM json_each(:rows) WHERE true
ON CONFLICT(endpoint, span_start, source) DO UPDATE SET
    asked = 1, answered = excluded.answered,
    stored_at = CASE WHEN excluded.answered = 1 THEN excluded.stored_at
                     ELSE calendar_coverage.stored_at END,
    stale_after = CASE WHEN excluded.answered = 1 THEN excluded.stale_after
                       ELSE calendar_coverage.stale_after END
"""


def _listings(records: list[Record], keys: list[str], *, bit: int, now: int,
              searched: bool) -> list[episode_rows.Listing]:
    """Each record as the instant the store files on its episode's row."""
    out = []
    for record, key in zip(records, keys):
        out.append(episode_rows.Listing(
            key=key, source=str(record.source), media=str(record.media),
            season=record.season, number=record.episode_number,
            instant=episode_rows.Instant(
                at=float(record.air_ts), date_only=1 if record.date_only else 0,
                calendars=bit, searched=bit if searched else 0,
                label=str(record.episode_label or ""), listed_at=now),
            episode_title=str(record.episode_title or ""),
            own=title_rows.own_id(str(record.source), record.id, record.ids)))
    return out


async def store_span(endpoint_key: str, span_start: date, span_end: date,
                     records: list[Record], *, sources, asked, unchanged=(),
                     spans=None, now: int, stale_after: int) -> None:
    """Store what each service answered for the window [span_start, span_end)
    of `endpoint_key` — each over the span its answer is authoritative for
    (`spans`, {service: [first, after)}; the window, for one not named).

    WHAT THE SOURCES THAT ANSWERED WITH DATA LISTED IN THEIR SPAN LEAVES THIS
    CALENDAR, AND WHAT THEY LIST NOW IS FILED, and it has to be both: a title a
    source has STOPPED listing leaves nothing for a write to find, so a
    write-only refresh would go on drawing it for ever. Only sources that
    ANSWERED are cleared -- wiping a source's listings because it was unreachable
    would turn a refused request into a month that lost half its entries.

    `unchanged` IS A THIRD ANSWER AND NOT A KIND OF SILENCE. A source whose files
    all replied 304 has told us something definite -- the listings already stored
    are still correct -- so its coverage advances exactly as an answer does, and
    its listings are LEFT ALONE rather than cleared. It speaks for the window
    asked alone: what its validator confirms is this window's last read.

    A COVERAGE ROW FOR EVERY WINDOW STORED, written here with the listings: the
    window asked for every service asked, and every other window of a service's
    span for that service — so one Simkl month file confirms its month's weeks,
    and every listing a feed files lies in a window whose row confirms it
    (`sweep`).

    One transaction, so a reader never sees the gap between the clearing and the
    filing, and so a title re-keyed by what this fill states moves with the
    listings that stated it.
    """
    first = span_start.isoformat()
    answered = [str(s) for s in sources]
    reach = {source: tuple((spans or {}).get(source) or (span_start, span_end))
             for source in answered}
    bit = _bit(endpoint_key)

    # NOTHING OUTSIDE ITS SERVICE'S SPAN IS STORED, here as well as at the fetch,
    # because a listing is FILED BY ITS OWN DATE: one landing in a window with no
    # coverage row would never be read, and the next fill of its own window would
    # take it off unseen. A source may treat the `days` bound as a floor rather
    # than a ceiling — Trakt has returned entries two months past a seven-day
    # window — and those are no answer about the later weeks. Dropping them
    # loudly is the honest failure; the window they belong to fetches them
    # properly when asked.
    def _inside(record: Record) -> bool:
        lo, hi = reach.get(str(record.source), (span_start, span_end))
        return lo.isoformat() <= _utc_date(record.air_ts) < hi.isoformat()

    inside = [r for r in records if _inside(r)]
    if len(inside) != len(records):
        logger.debug("%d %s entr(ies) fell outside the span their answer speaks for, "
                     "from the window starting %s; trimmed.",
                     len(records) - len(inside), endpoint_key, first)
    replied = sorted({*answered, *(str(s) for s in unchanged)})
    # EVERYTHING THE STATEMENTS CARRY, SPELLED BEFORE THE LOCK IS TAKEN.
    listed = metadata.listings(inside, now=now, stale_after=stale_after)
    coverage = [[first, name, 1 if name in replied else 0]
                for name in sorted({*map(str, asked), *replied})]
    for source, (lo, hi) in sorted(reach.items()):
        window = lo
        while window < hi:
            if window != span_start:
                coverage.append([window.isoformat(), source, 1])
            window += timedelta(days=WINDOW_DAYS)
    coverage = json.dumps(coverage)

    def _fill(conn, positions: list[int], keys: list[str], primaries,
              **halves) -> episode_rows.Fill:
        return episode_rows.plan_fill(
            conn, _listings([inside[at] for at in positions], [keys[at] for at in positions],
                            bit=bit, now=now, searched=False),
            bit=bit, primaries=primaries, span=(span_start, span_end), spans=reach,
            answered=answered, replied=replied, **halves)

    # WHICH TITLE EACH LISTING IS AND WHICH ROW IT IS FILED ON, DECIDED BEFORE THE
    # LOCK IS TAKEN: the title store's reads and the deciding they feed
    # (metadata.plan_records, episodes.plan_fill), made again inside only if
    # another writer committed in between (db.transaction's `prepare`) — so the
    # lock is held for the writes alone.
    #
    # EXCEPT FOR THE FEW TITLES A MERGE RESHAPES. A batch that re-keys a title or
    # moves an entry merges rows as it is written (app/metadata/merge.py), so
    # those titles' rows are not the rows it read: their share of the span is
    # filed inside, after the merge (`planned.landed`), and every other title's
    # before, as always. MEASURED on a copy of dev, a week of All Episodes
    # re-keying five titles while a month was read on another thread: filing
    # the whole span inside held the lock 2.98 s, this holds it 1.31 s, and the
    # store before the one-row-per-episode tables held it 1.21 s.
    planning: list[float] = []

    def _plan(conn):
        started = time.perf_counter()
        planned = metadata.plan_records(conn, listed)
        prepared = planned, _fill(conn, list(range(len(inside))), planned.keys,
                                  planned.primaries, excluding=planned.touched)
        planning.append(time.perf_counter() - started)
        return prepared

    written: list[metadata.Planned] = []

    def _work(conn, prepared) -> None:
        planned, fill = prepared
        written.append(planned)
        # WHERE THE LOCK'S TIME GOES, one DEBUG line per store: a hold over a
        # second is logged by db.transaction, and that line alone cannot say
        # whether the plan was made again under it (another connection wrote in
        # between), a re-key's merge, or the writes held it.
        laps = perftrace.Laps()
        if len(planning) > 1:
            laps.phases["prepare again"] = planning[-1]
        metadata.write_records(conn, listed, planned, laps=laps)
        episode_rows.apply(conn, fill)
        laps.lap("episodes")
        if planned.touched:
            landed = planned.landed
            positions = [at for at, key in enumerate(planned.keys) if key in landed]
            primaries = title_rows.primaries(conn, listed.rows.only(positions),
                                             [planned.keys[at] for at in positions])
            reshaped = _fill(conn, positions, planned.keys, primaries, titles=landed)
            laps.lap("reshaped re-plan")
            episode_rows.apply(conn, reshaped)
            laps.lap("reshaped write")
        conn.execute(_UPSERT_COVERAGE, {"endpoint": endpoint_key, "rows": coverage,
                                        "now": now, "stale_after": stale_after})
        laps.lap("coverage")
        logger.debug("calendar store: %s from %s held the lock %.3f s for %d listing(s), "
                     "%d re-key(s): %s", endpoint_key, first, laps.total, len(inside),
                     len(planned.settles.retired), laps)

    await db.transaction(_work, prepare=_plan)
    metadata.complete_records(listed, written[-1])


async def store_loose_airings(endpoint_key: str, records, *, now: int,
                              stale_after: int) -> list[Record]:
    """Store listings that no CALENDAR FEED offered, filling a gap in one, and
    return the ones written.

    WHY A SOURCE CAN BE MISSING ITS OWN TITLE. A service answers two different
    datasets about one show, and they disagree. Trakt's show record dates Half
    Man's first season to 2026-04-28T20:00Z; Trakt's premieres CALENDAR for that
    week does not list it, and its all-episodes calendar carries episode two and
    no episode one. The title is real, the date is the service's own, and the
    calendar this app is built from simply has a hole in it.

    SO THE SEARCH REPAIRS IT. A catalogue lookup already had to describe the
    title and date its seasons to draw a result row at all; writing that as a
    listing costs nothing more and turns "go to the month and hope" into a card
    that is actually there.

    WRITTEN AS THE FEED WOULD HAVE WRITTEN IT, deliberately. Same source, same
    ids, same title store — so a listing here resolves and filters and renders
    like every other, and a later fill listing the same episode supersedes it
    rather than drawing it twice. The records come back with their ids completed
    from the title store (metadata.write_records), so a caller drawing them keys
    them as the calendar's read will.

    NO GAP, NO LISTING: an episode its service's feed already lists on this
    calendar is not written at all, at whatever instant the feed gives it — the
    feed's listing is the card, and a second listing from one service is a
    second card.

    NO COVERAGE ROW IS WRITTEN, and that is the important restraint. Coverage
    records which sources were ASKED about a window and which ANSWERED — this
    asked nobody about a window, so claiming coverage would tell the fill path a
    span had been fetched when it has not.
    """
    records = list(records or ())
    if not records:
        return []
    bit = _bit(endpoint_key)
    listed = metadata.listings(records, now=now, stale_after=stale_after)

    def _write(conn) -> list[Record]:
        # WHICH OF THEM THE FEED ALREADY LISTS: read against where each entry is
        # filed now, before anything registers a title for a gap that is not one.
        filed = title_rows.filed_keys(conn, [(str(r.source), str(r.media), own)
                                             for r, own in zip(records, listed.rows.owns)])
        keys = [filed.get((str(r.source), str(r.media), own), "")
                for r, own in zip(records, listed.rows.owns)]
        known = title_rows.primaries(conn, listed.rows, keys)
        probe = episode_rows.plan_fill(
            conn, _listings(records, keys, bit=bit, now=now, searched=True),
            bit=bit, primaries=known)
        gaps = [at for at, (key, written) in enumerate(zip(keys, probe.written))
                if written or not key]
        if not gaps:
            return []
        narrowed = listed.only(gaps)
        planned = metadata.plan_records(conn, narrowed)
        metadata.write_records(conn, narrowed, planned)
        # A search's handful, drawn as cards straight after: completed here.
        metadata.complete_records(narrowed, planned)
        gap_records = [records[at] for at in gaps]
        fill = episode_rows.plan_fill(
            conn, _listings(gap_records, planned.keys, bit=bit, now=now, searched=True),
            bit=bit, primaries=title_rows.primaries(conn, narrowed.rows, planned.keys))
        episode_rows.apply(conn, fill)
        return [record for record, written in zip(gap_records, fill.written) if written]

    written = await db.transaction(_write)
    if written:
        logger.info("calendar search filled %d listing(s) on %s that no feed listed.",
                    len(written), endpoint_key)
    return written


# ---------------------------------------------------------------------------
# reading a span back
# ---------------------------------------------------------------------------

# WHAT A SERVICE'S TITLE GROUP SAYS, as every read here names it — a listing's
# record (`_records`) and a title-only record (`title_only_records`), so the two
# cannot come to read one group differently (`_words`).
_TITLE_WORDS = ("title", "ids_json", "detail_url", "year", "network", "country", "language",
                "certification", "status", "overview", "poster", "runtime", "genres_json",
                "ratings_json", "anime_type", "release_types_json", "enriched", "keywords_json",
                "show_type", "facts_known")


def _title_columns(alias: str = "t") -> str:
    """Every service's title words, each as `t_{source}_{word}`."""
    return ", ".join(f"{groups.column(tables.TITLES, source, word, alias)} AS t_{source}_{word}"
                     for source in groups.sources() for word in ("id", *_TITLE_WORDS))


# WHAT A CARD READS OF AN EPISODE'S DESCRIPTION: its name, its runtime, and
# whether the service describes it at all.
_EPISODE_WORDS = ("title", "runtime", "fetched_at")


def _rows_sql(where: str) -> str:
    """Episode rows `where` names (a condition on the row, aliased `e`) — their
    listings and what a card reads of their descriptions — with their title's
    every service's words and every id the store knows of it. The title is
    joined LEFT: a listing whose title nothing describes is drawn as a thin card
    rather than not at all."""
    described = ", ".join(f"e.{source}_{name}" for source in groups.sources()
                          for name in _EPISODE_WORDS
                          if groups.has(tables.EPISODES, source, name))
    return (f"SELECT {episode_rows.listing_select('e')}, {described}, "
            f"i.ids_json AS known_ids, {_title_columns('i')} "
            f"FROM {tables.EPISODES} e LEFT JOIN {tables.TITLES} i ON i.title_key = e.title_key "
            f"WHERE {where}")


def _group_of(row, source: str) -> dict:
    """`source`'s title words on a read row, under their own names."""
    return {word: row[f"t_{source}_{word}"] for word in ("id", *_TITLE_WORDS)}


def _words(row, source: str) -> dict:
    """The Record fields one service's title group states about the TITLE —
    read once, for a listing's record (`_records`) and a title-only one
    (`title_only_records`) alike."""
    ratings = json.loads(row["ratings_json"] or "{}") if row["ratings_json"] else {}
    # None for a group no description has stated them on — not "none" (Record.keywords).
    keywords, show_type = title_rows.facts_of(row)
    return dict(
        keywords=keywords, show_type=show_type,
        detail_url=str(row["detail_url"] or ""), title=str(row["title"] or ""),
        # BACK TO A NUMBER WHERE IT IS ONE. `Record.year` is `int | str`, the
        # column is TEXT, and a caller comparing or sorting on it would see the
        # type change in storage; every source hands it over as an int.
        year=title_rows.as_year(row["year"]), network=row["network"] or "",
        country=row["country"] or "", language=row["language"] or "",
        status=row["status"] or "",
        # BACK OUT OF THE MAP UNDER THIS SOURCE'S OWN NAME. `Record.rating` is
        # one number shown under one service's mark, so a group that holds three
        # services' scores still hands this source only its own.
        rating=ratings.get(source),
        # IMDb IS IN THE SAME MAP AND IS NOT THIS SOURCE'S OWN SCORE, which is
        # exactly why the map is keyed by who said it rather than being one
        # number. It is read out by name for every source.
        imdb_rating=ratings.get("imdb"),
        genres=json.loads(row["genres_json"] or "[]") if row["genres_json"] else [],
        certification=row["certification"] or "", overview=row["overview"] or "",
        poster=row["poster"] or "",
        enriched=bool(row["enriched"]),
    )


class _Title(NamedTuple):
    """One service's title group as a read decodes it once for every listing of
    the title: its words, its own ids, every id the store knows, its release
    schedule, its runtime and anime format, and the id its listings are filed
    under."""
    words: dict
    ids: dict
    known: dict
    releases: dict
    runtime: int | None
    anime_type: str
    listing_id: str


def _listing_id(source: str, ids) -> str:
    """The id `source` files a listing of the title `ids` name under — its own
    rule, asked of the registry (Provider.listing_id)."""
    return providers.registered()[Source(source)].listing_id(ids)


def _title(row, source: str, decoded: dict) -> _Title:
    """`source`'s title group of a read row, decoded once per read and shared:
    nothing writes into a record's containers (resolution builds its own,
    cache.group_records says so), and a month's worth of copies was tens of
    thousands of objects for the garbage collector to walk again and again while
    the month was built.

    ITS IDS ARE THE SERVICE'S OWN WITH THE TITLE'S SHARED IDS BENEATH THEM. A
    listing whose service knows a show only by its tvdb id is still the title's
    listing, and the title store's ids (identity.with_shared) are what key every
    listing of one title the same way."""
    held = decoded.get((row["title_key"], source))
    if held is None:
        group = _group_of(row, source)
        known = json.loads(row["known_ids"]) if row["known_ids"] else {}
        own = json.loads(group["ids_json"] or "{}") if group["ids_json"] else {}
        held = _Title(
            _words(group, source), title_identity.with_shared(own, known) if known else own,
            known, json.loads(group["release_types_json"] or "{}")
            if group["release_types_json"] else {},
            group["runtime"], str(group["anime_type"] or ""),
            # The id the service files its listings of the title under, by its
            # own rule (Provider.listing_id) — from its group's ids, or what the
            # store knows of the title where the service has no group — and the
            # group's own entry where the ids state none of the service's.
            _listing_id(source, own or known) or str(group["id"] or ""))
        decoded[(row["title_key"], source)] = held
    return held


def _records(row, bit: int, decoded: dict,
             stored: episode_rows.Row | None = None) -> list[Record]:
    """Every listing a stored episode row holds on calendar `bit`, as the Record
    the read path knows — less every searched instant whose service's feed lists
    the same episode on that calendar.

    A SEARCHED LISTING STANDS DOWN FOR ITS FEED HERE TOO, not only when a fill
    takes it off (store_span): a fill reaches a span only when it is due, and one
    whose service answered "unchanged" — every frozen month — may not be filled
    with data for weeks, nor ever by a share page.

    A MISSING TITLE GROUP IS NOT AN ERROR: a listing whose title nothing
    describes (retention swept it, or a ceiling did) renders as a thin card
    rather than vanishing."""
    stored = stored if stored is not None else episode_rows.Row.read(row)
    out = []
    for source in groups.sources():
        held = [i for i in stored.instants[source] if i.calendars & bit]
        fed = {(i.entry, i.unstated) for i in held if i.fed & bit}
        title = None
        for instant in held:
            if instant.searched & bit and (instant.entry, instant.unstated) in fed:
                continue
            title = title or _title(row, source, decoded)
            season, number = stored.stated(instant)
            if instant.entry is not None:
                namespace = title_rows.ID_NAMESPACE[source]
                ids = title_identity.with_shared({namespace: _typed(instant.entry)}, title.known)
                record_id = instant.entry
            else:
                ids, record_id = title.ids, title.listing_id
            described = groups.has(tables.EPISODES, source, "fetched_at") and \
                row[f"{source}_fetched_at"] is not None
            episode_runtime = (row[f"{source}_runtime"]
                               if described and groups.has(tables.EPISODES, source, "runtime")
                               else None)
            out.append(Record(
                **title.words,
                source=Source(source), media=Media(str(row["media"])),
                id=str(record_id), ids=ids,
                # And everything the title store knows of it, kept apart from
                # what this service stated (Record.known_ids says why).
                known_ids=title.known,
                air_ts=float(instant.at), date_only=bool(instant.date_only),
                # THE EPISODE'S OWN RUNTIME WHERE ONE IS KNOWN, the title's
                # otherwise. A show's episodes really do differ -- a double-length
                # finale is not the show's nominal runtime. Simkl publishes no
                # per-episode runtime at all, so falling back is the ordinary path
                # and not a failure.
                runtime=episode_runtime if episode_runtime is not None else title.runtime,
                episode_label=instant.label or None,
                episode_title=(str(row[f"{source}_title"] or "") if described else ""),
                season=season, episode_number=number,
                anime_type=title.anime_type,
                release_types_by_country=title.releases,
                # WHICH STORED LISTING THIS IS: the episode's row, which is the
                # card (cache.group_records).
                card=f"{row['title_key']}|{row['unit']}"))
    return out


def _typed(own: str):
    return int(own) if own.isdigit() else own


def _in_fetch_order(found, bit: int) -> list[Record]:
    """Stored rows — or (row, its parsed Row) pairs — as Records, ordered as a
    fill would have produced them.

    SQL cannot order them: a row holds several services' instants. Air time
    first, then the registry's declared order between two services describing
    the same instant — a stable sort, so each row's own order survives inside
    each source."""
    decoded: dict = {}
    records = [record for held in found
               for record in (_records(held[0], bit, decoded, held[1])
                              if isinstance(held, tuple) else _records(held, bit, decoded))]
    records.sort(key=lambda r: (r.air_ts, title_rows.SOURCE_ORDER.get(str(r.source), 99)))
    return records


def _span_rows(conn: db.Connection, bit: int, lo: float, hi: float) -> list[tuple]:
    """The episode rows with an instant on calendar `bit` inside [lo, hi), with
    their titles' words — each beside its parsed listings."""
    found = conn.execute(_rows_sql(f"e.rowid IN ({episode_rows.span_rowids()})"),
                         {"lo": lo, "hi": hi, "bit": bit}).fetchall()
    parsed = ((row, episode_rows.Row.read(row)) for row in found)
    return [(row, stored) for row, stored in parsed
            if episode_rows.in_span(stored, bit, lo, hi)]


async def read_span(endpoint_key: str, span_start: date, span_end: date
                    ) -> tuple[list[Record], tuple[str, ...], tuple[str, ...], int | None]:
    """Every stored listing for `endpoint_key` dated in [span_start, span_end),
    with who was asked, who answered, and when it was stored.

    The coverage lists come from `calendar_coverage` and NEVER from which sources
    happen to have listings here: a source that legitimately lists nothing in a
    span would otherwise be indistinguishable from one that was never asked,
    which is the difference between "partial" and "nothing to report".
    """
    bit = _bit(endpoint_key)
    lo, hi = episode_rows.epoch(span_start), episode_rows.epoch(span_end)
    found = await db.run(lambda conn: _span_rows(conn, bit, lo, hi))
    records = [record for record in _in_fetch_order(found, bit) if lo <= record.air_ts < hi]
    answered, asked, stored_at = await span_coverage(endpoint_key, span_start)
    return records, answered, asked, stored_at


async def span_coverage(endpoint_key: str, span_start: date
                        ) -> tuple[tuple[str, ...], tuple[str, ...], int | None]:
    """(who answered, who was asked, when it was stored) for the span beginning
    `span_start` — `read_span`'s coverage half, with no listing read at all.

    FOR THE READ PATH, WHICH NO LONGER GROUPS A SPAN ON ITS OWN. Whether a span
    is fresh, partial or missing is a question about coverage; its listings are
    read once for the whole range a reader asked about (`read_span_whole`), so
    reading them here as well would read every row twice.

    AND FOR THE SCHEDULE (cache.refresh_months), whose clock this is: stored
    rather than remembered, so a restart does not reset it. The EARLIEST REPLY
    rather than the latest: a span is only as fresh as its stalest source, so one
    source refilled an hour ago must not make the whole span look current while
    another has not been asked in a week. A service that FAILED is not stale by
    this clock — its failure keeps the time of its last reply (`_UPSERT_COVERAGE`)
    and is asked again on a clock of its own (cache._failed_retry_due); only a
    span nobody has replied for is dated by its asking.
    """
    held = await source_coverage(endpoint_key, span_start)
    asked = tuple(source for source, row in held.items() if row.asked)
    answered = tuple(source for source, row in held.items() if row.answered)
    stored_at = min((row.stored_at for row in held.values() if row.answered),
                    default=min((row.stored_at for row in held.values()), default=None))
    return answered, asked, stored_at


class SourceCoverage(NamedTuple):
    """One service's coverage row for one window: whether it was asked, whether
    its last answer was a reply (listings or "unchanged"), and when it last
    replied — or, for one that never has, when it was first asked."""
    asked: bool
    answered: bool
    stored_at: int


async def source_coverage(endpoint_key: str, span_start: date) -> dict[str, SourceCoverage]:
    """{service: its coverage row} for the window beginning `span_start` — what
    decides, service by service, whether the window is fresh, stale, failed or
    never asked (cache.ensure_window)."""
    return {str(r["source"]): SourceCoverage(bool(r["asked"]), bool(r["answered"]),
                                             int(r["stored_at"]))
            for r in await db.fetch_all(
                "SELECT source, asked, answered, stored_at FROM calendar_coverage "
                "WHERE endpoint = ? AND span_start = ?", (endpoint_key, span_start.isoformat()))}


async def read_span_whole(endpoint_key: str, span_start: date, span_end: date
                          ) -> list[Record]:
    """Every listing on `endpoint_key` of each episode row that has one dated in
    [span_start, span_end) — every service's, at whatever date: the rows a reader
    of that range needs to say which cards it draws, and where.

    WHOLE IS THE POINT. One episode is one row, and the row is one card whatever
    dates its services give it — so a listing outside the range is part of a card
    inside it, and decides which day this viewer's card is drawn on.

    No coverage is read: whether each span inside the range was filled is the
    caller's question (`span_coverage`), asked once per span before this.
    """
    bit = _bit(endpoint_key)
    lo, hi = episode_rows.epoch(span_start), episode_rows.epoch(span_end)

    # THE RECORDS ARE BUILT ON THE WORKER THAT READ THE ROWS, not on the event
    # loop: tens of thousands of them for a busy month, and the loop serves every
    # other request while they are built.
    def _read(conn) -> list[Record]:
        return _in_fetch_order(_span_rows(conn, bit, lo, hi), bit)

    return await db.run(_read)


# ---------------------------------------------------------------------------
# a service's description of a title, for a card it does not list
# ---------------------------------------------------------------------------

def _title_only_sql(sources: list[str]) -> str:
    """Each named service's group of the named titles, an arm per service, in
    one statement however many a span names — the keys travel as a JSON array,
    so a month of two and a half thousand titles is one seek per key."""
    arms = " UNION ALL ".join(
        f"SELECT '{source}' AS source, t.media AS media, t.title_key AS title_key, "
        + groups.select(tables.TITLES, source, ("id", *_TITLE_WORDS), "t")
        + f" FROM {tables.TITLES} t WHERE t.title_key IN (SELECT value FROM json_each(:keys)) "
        f"AND {groups.present(tables.TITLES, source, 't')}"
        for source in sources)
    return f"SELECT * FROM ({arms}) ORDER BY title_key, source"


def _title_only_from_row(row) -> Record:
    """One service's group of a title as a TITLE-ONLY record: everything it says
    about the title, and nothing about an airing.

    WHAT IT LEAVES OUT IS AS DELIBERATE AS WHAT IT CARRIES, because the read
    joins it to a card this service does not list (cache.with_title_only):
      - no airing — `air_ts` 0 and date-only, an instant nobody could mistake
        for a listing's, and never read: resolution takes the card's day and
        time from the services that list it (resolve.resolve_records);
      - no episode coordinate and no episode words — it is the title's group;
      - no release schedule and no anime format, which the read asks of the
        records that list a card (filter.filter_release_groups,
        filter.prune_disguised_films) and never of a description joined to it.
    Its ids are the ones its own group states, and they are also what it knows
    the title by (`known_ids`) — what lets the card's buttons address this
    service.
    """
    source = str(row["source"])
    ids = json.loads(row["ids_json"] or "{}") if row["ids_json"] else {}
    return Record(
        **_words(row, source),
        source=Source(source), media=Media(str(row["media"])),
        id=_listing_id(source, ids) or str(row["id"] or ""), ids=ids, known_ids=dict(ids),
        air_ts=0.0, date_only=True, runtime=row["runtime"],
    )


async def title_only_records(sources: Iterable[str], title_keys: Iterable[str]
                             ) -> dict[tuple[str, str, str], Record]:
    """`{(source, media, title key): that service's title-only record}` for every
    title among `title_keys` that one of `sources` holds a group for — one read,
    and the store's groups only: nothing is asked of any service."""
    keys = sorted({str(key) for key in title_keys if key})
    names = [str(source) for source in groups.sources() if str(source) in set(map(str, sources))]
    if not keys or not names:
        return {}
    found = await db.fetch_all(_title_only_sql(names), {"keys": json.dumps(keys)})
    return {(str(row["source"]), str(row["media"]), str(row["title_key"])):
            _title_only_from_row(row) for row in found}


# ---------------------------------------------------------------------------
# the calendar's own search
# ---------------------------------------------------------------------------

def _on_calendar(source: str, alias: str) -> str:
    """SQL: `source` lists an instant of the episode row aliased `alias` on the
    calendar `:bit`."""
    return (f"({alias}.{source}_calendars & :bit OR ({alias}.{source}_repeats_json != '[]' "
            f"AND EXISTS (SELECT 1 FROM json_each({alias}.{source}_repeats_json) r "
            f"WHERE json_extract(r.value, '$.calendars') & :bit)))")


def _matching_sql(*, matches_first: bool) -> str:
    """The title keys on calendar `:bit` a folded title of the SAME service that
    lists them there matches (`:needle`), at most `:limit`.

    TWO SHAPES OF ONE QUESTION, because each is fast exactly where the other is
    slow (`groups_matching` says which it asks when).
    MATCHES FIRST: each service's folded titles are scanned in its own fold
    index, which holds nothing else, and only the titles that matched are looked
    up on the calendar through the episode rows' title index — a few ms per
    calendar, where starting from the calendar's rows walked every one of them.
    THE CALENDAR FIRST: its rows in air order through each service's air index,
    the title checked as it goes — the shape this search has always had, and the
    one that decides WHICH titles a broad query loads: the earliest-listed."""
    arms = []
    for source in groups.sources():
        if not groups.has(tables.EPISODES, source, "air_ts"):
            continue
        if matches_first:
            arms.append(
                f"SELECT t.title_key FROM {tables.TITLES} t INDEXED BY ix_titles_{source}_fold "
                rf"WHERE t.{source}_title_fold LIKE :needle ESCAPE '\' "
                f"AND EXISTS (SELECT 1 FROM {tables.EPISODES} e WHERE e.title_key = t.title_key "
                f"AND {_on_calendar(source, 'e')})")
        else:
            arms.append(
                f"SELECT e.title_key, e.{source}_air_ts AS at FROM {tables.EPISODES} e "
                f"JOIN {tables.TITLES} t ON t.title_key = e.title_key "
                f"WHERE e.{source}_calendars != 0 AND {_on_calendar(source, 'e')} "
                rf"AND t.{source}_title_fold LIKE :needle ESCAPE '\'")
    if matches_first:
        return f"SELECT DISTINCT title_key FROM ({' UNION ALL '.join(arms)}) LIMIT :limit"
    return (f"SELECT title_key FROM ({' UNION ALL '.join(arms)}) GROUP BY title_key "
            "ORDER BY MIN(at), title_key LIMIT :limit")


# HOW MANY LISTED ROWS MAKE A CALENDAR BIG ENOUGH TO SEARCH MATCHES-FIRST. Reading
# every row of the calendar costs about 2ms a thousand on the measured copy, and
# the matches-first read about 2ms for a query naming a title — so a thousand is
# where the two meet. Counting up to it is a walk of at most this many rows.
_SMALL_CALENDAR = 1000


def _calendar_size_sql() -> str:
    arms = " UNION ALL ".join(
        f"SELECT 1 FROM {tables.EPISODES} WHERE {source}_calendars & :bit"
        for source in groups.sources() if groups.has(tables.EPISODES, source, "air_ts"))
    return f"SELECT COUNT(*) FROM ({arms} LIMIT :limit)"


async def groups_matching(endpoint_key: str, query: str, limit: int) -> list[str]:
    """The `title_key`s on `endpoint_key` whose title matches `query`.

    MATCHED ON THE FOLDED TITLE, the same folding a stored title was written
    with — case-folded and accent-stripped, so "pokemon" finds "Pokemon" (the
    title store's `like_needle`). Each service's fold index is what makes this a
    lookup rather than a scan.

    WHICH READ IS ASKED IS A COST DECISION AND NEVER CHANGES THE ANSWER. A small
    calendar is read calendar-first, whatever was typed. A big one is read
    matches-first, and fewer than `limit` back is the whole answer — the same set
    the calendar-first read would have found. `limit` or more means the query
    matched more titles than a search loads, and which of them is the
    calendar-first read's to say, so it is asked again that way. The keys come
    back in no particular order: `read_groups` sorts what it loads.
    """
    params = {"bit": _bit(endpoint_key), "needle": typed_search.like_needle(query),
              "limit": limit}
    size = await db.fetch_value(_calendar_size_sql(), {"bit": params["bit"],
                                                       "limit": _SMALL_CALENDAR}, 0)
    if size >= _SMALL_CALENDAR:
        found = await db.fetch_all(_matching_sql(matches_first=True), params)
        if len(found) < limit:
            return [str(r["title_key"]) for r in found]
    found = await db.fetch_all(_matching_sql(matches_first=False), params)
    return [str(r["title_key"]) for r in found]


async def read_groups(endpoint_key: str, title_keys) -> list[Record]:
    """Every stored listing of `title_keys` on one calendar, whole.

    WHOLE IS THE POINT — every source's listing of each episode, so the read path
    resolves the same card a month read would. Loading by title rather than by
    month is what makes a search cheap: one real `shows` month held 12,880
    listings, and the titles a query matches are a handful of them.
    """
    keys = sorted({str(k) for k in title_keys})
    if not keys:
        return []
    bit = _bit(endpoint_key)
    found = await db.fetch_all(_rows_sql("e.title_key IN (SELECT value FROM json_each(?))"),
                               (json.dumps(keys),))
    return _in_fetch_order(found, bit)


async def sources_airing(endpoint_key: str, title_key: str, season: int | None) -> set[str]:
    """The sources with a stored listing of `title_key` (that season, or with no
    season for a film) on `endpoint_key` — which services' calendars already
    draw it there. A keyed read on the episode rows' title index."""
    bit = _bit(endpoint_key)
    found = await db.fetch_all(
        f"SELECT * FROM {tables.EPISODES} WHERE title_key = ?"
        + ("" if season is None else " AND season = ?"),
        (str(title_key),) if season is None else (str(title_key), int(season)))
    out = set()
    for row in found:
        stored = episode_rows.Row.read(row)
        for source, held in stored.instants.items():
            if any(instant.calendars & bit and
                   (season is None or stored.stated(instant)[0] == season) for instant in held):
                out.add(source)
    return out


class ListedCard(NamedTuple):
    """One title's season (None for a film, or a listing that names none) on one
    calendar, as the stored listings hold it in a span: the services listing it
    on that calendar AT ANY DATE (a service dating it outside the span still
    lists it), its first day in the span, and every id the title store knows for
    it."""
    endpoint: str
    title_key: str
    media: str
    season: int | None
    sources: frozenset[str]
    day: date
    known_ids: dict


async def listed_cards(endpoint_keys, first: date, last: date) -> list[ListedCard]:
    """Every title and season with a stored listing on `endpoint_keys` dated in
    [first, last) — the background matcher's worklist (app/calendar/completion.py).
    One read of the span's episode rows, with the identity of each title."""
    keys = [str(k) for k in endpoint_keys]
    if not keys:
        return []
    lo, hi = episode_rows.epoch(first), episode_rows.epoch(last)
    mask = 0
    for key in keys:
        mask |= _bit(key)

    def _read(conn) -> list:
        return conn.execute(
            f"SELECT {episode_rows.listing_select('e')}, i.ids_json AS known_ids "
            f"FROM {tables.EPISODES} e LEFT JOIN {tables.TITLES} i ON i.title_key = e.title_key "
            f"WHERE e.rowid IN ({episode_rows.span_rowids()})",
            {"lo": lo, "hi": hi, "bit": mask}).fetchall()

    found = await db.run(_read)
    # Every title's season on each calendar: the services listing it there at
    # any date, its first day in the span, what the store knows of the title.
    cards: dict[tuple[str, str, int | None], dict] = {}
    rows_of: dict[str, list] = {}
    for row in found:
        rows_of.setdefault(str(row["title_key"]), []).append(row)
    for key in keys:
        bit = _bit(key)
        for title_key, held_rows in rows_of.items():
            for row in held_rows:
                stored = episode_rows.Row.read(row)
                for source, instants in stored.instants.items():
                    for instant in instants:
                        if not instant.calendars & bit:
                            continue
                        season = stored.stated(instant)[0]
                        card = cards.setdefault((key, title_key, season), {
                            "media": str(row["media"]), "sources": set(), "day": None,
                            "known": json.loads(row["known_ids"]) if row["known_ids"] else {}})
                        card["sources"].add(source)
                        if lo <= instant.at < hi:
                            day = date.fromisoformat(instant.day)
                            card["day"] = day if card["day"] is None else min(card["day"], day)
    return [ListedCard(endpoint=key, title_key=title_key, media=card["media"], season=season,
                       sources=frozenset(card["sources"]), day=card["day"],
                       known_ids=card["known"])
            for (key, title_key, season), card in cards.items() if card["day"] is not None]


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

# HOW LONG A STORED LISTING IS KEPT AT ALL, as opposed to how fresh it has to be
# to serve a request -- two clocks, deliberately different fields, because "this
# is stale, refresh it when somebody touches it" and "delete this" are different
# instructions and one number cannot mean both.
#
# SIX MONTHS, FLAT, EVERY SOURCE. TMDB's terms forbid caching anything obtained
# from them for longer than six months, so the ceiling exists whether or not this
# design wants one; applying it to everything rather than to TMDB rows alone
# costs almost nothing and removes a per-source branch nobody would remember to
# keep correct.
#
# AND THE TIERS MAKE IT A NON-EVENT: anything with traffic refreshes far inside
# six months, so the only listings this removes are ones nobody has touched in
# half a year, which refetch on the next visit.
#
# ONE HONEST REGRESSION, RECORDED RATHER THAN DISCOVERED. The blob this replaced
# was swept at app/cache.py's RETAIN_SECONDS plus TTL_GRACE_SECONDS -- nine
# months in total, deliberately, because a public share link never refetches and
# must go on rendering whatever it has. A share link to a month between six and
# nine months old renders today and will render EMPTY under this. Narrow, real,
# and a decision rather than an oversight.
RETAIN_SECONDS = 180 * 24 * 60 * 60


class _Sweep(NamedTuple):
    """What a retention sweep takes off, decided before its lock (`_plan_sweep`)."""
    # The rows holding an instant past the window, with those instants taken
    # off, as `Book.write` writes them — and how many instants that is.
    book: episode_rows.Book
    spelled: tuple[str, str] | None
    removed: int
    # Whether a coverage row, or a calendar file's validator, is past it.
    coverage: bool
    files: bool
    # The groups nothing lists any more, and the groups past a service's ceiling.
    unlisted: title_rows.Clearing
    ceilings: title_rows.Clearing

    @property
    def due(self) -> bool:
        return bool(self.spelled or self.coverage or self.files or self.unlisted
                    or self.ceilings)


# WHICH EPISODE ROWS HOLD A LISTING OF ONE SERVICE THAT RETENTION TAKES OFF, two
# ways, each through an index or the few rows that hold repeats:
#   - A FED listing — one its service's feed gave — inside a window whose coverage
#     row for the service is past the retention window (`:windows`, [bit, lo, hi]
#     each): nothing has confirmed it for six months, whatever it was stored at;
#   - A SEARCHED one (a search or a tracker add wrote it, and no feed lists it)
#     stored before the cutoff: a per-title lookup confirms itself only when it is
#     made, so its own clock is its age.
def _due_sql(source: str) -> str:
    fed_in = ("json_extract(w.value, '$[1]') <= {at} AND {at} < json_extract(w.value, '$[2]') "
              "AND ({calendars} & ~{searched}) & json_extract(w.value, '$[0]')")
    repeat = {name: f"json_extract(r.value, '$.{name}')"
              for name in ("at", "calendars", "searched", "listed_at")}
    return (
        f"FROM {tables.EPISODES} e WHERE e.rowid IN ("
        f"SELECT x.rowid FROM json_each(:windows) w CROSS JOIN {tables.EPISODES} x "
        f"WHERE x.{source}_calendars != 0 AND x.{source}_air_ts >= json_extract(w.value, '$[1]') "
        f"AND x.{source}_air_ts < json_extract(w.value, '$[2]') "
        f"AND (x.{source}_calendars & ~x.{source}_searched) & json_extract(w.value, '$[0]') "
        f"UNION SELECT rowid FROM {tables.EPISODES} WHERE {source}_searched != 0 "
        f"AND COALESCE({source}_listed_at, 0) <= :cutoff "
        f"UNION SELECT rowid FROM {tables.EPISODES} WHERE {source}_repeats_json != '[]' "
        f"AND EXISTS (SELECT 1 FROM json_each({source}_repeats_json) r WHERE "
        f"({repeat['searched']} != 0 AND COALESCE({repeat['listed_at']}, 0) <= :cutoff) "
        f"OR EXISTS (SELECT 1 FROM json_each(:windows) w WHERE "
        + fed_in.format(at=repeat["at"], calendars=repeat["calendars"],
                        searched=repeat["searched"]) + ")))")


def _plan_sweep(conn: db.Connection, now: int) -> _Sweep:
    """Everything a sweep at `now` takes off, read and decided and NOT written —
    each read a query that answers in one row, and almost always with nothing."""
    cutoff = now - RETAIN_SECONDS
    (stale,), = conn.execute(
        "SELECT json_group_array(json_array(endpoint, span_start, source)) "
        "FROM calendar_coverage WHERE stored_at <= ?", (cutoff,)).fetchall()
    windows: dict[str, list[list]] = {}
    for endpoint_key, span_start, source in json.loads(stale):
        if endpoint_key in endpoints.ENDPOINTS:
            lo = episode_rows.epoch(date.fromisoformat(span_start))
            windows.setdefault(str(source), []).append(
                [_bit(endpoint_key), lo, lo + WINDOW_DAYS * 86400.0])
    book = episode_rows.Book()
    for source in groups.sources():
        if groups.has(tables.EPISODES, source, "listed_at"):
            book.hold(episode_rows.read_where(conn, _due_sql(source), {
                "cutoff": cutoff, "windows": json.dumps(windows.get(source, []))}))
    removed = 0
    emptied = []
    for row in book.rows.values():
        for source, instants in row.instants.items():
            listed = bool(instants)
            for instant in list(instants):
                bits = instant.searched if int(instant.listed_at or 0) <= cutoff else 0
                for bit, lo, hi in windows.get(source, ()):
                    if instant.fed & bit and lo <= instant.at < hi:
                        bits |= bit
                if bits:
                    row.take(source, instant, bits)
                    removed += 1
            if listed and not instants:
                emptied.append((source, row.key, row.unit))
    (files,), = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM calendar_source_files WHERE fetched_at <= ?)",
        (cutoff,)).fetchall()
    return _Sweep(book, book.spell(), removed, stale != "[]", bool(files),
                  title_rows.plan_unlisted(conn, cutoff, emptied=emptied),
                  title_rows.plan_past_ceilings(conn, now))


async def sweep(now: int | None = None) -> int:
    """Take off every listing nothing has confirmed inside the retention window,
    and then the title store's groups nothing lists any more.

    A FEED'S LISTING IS CONFIRMED BY ITS WINDOW'S COVERAGE ROW, NOT BY ITS OWN
    CLOCK. A refill that finds nothing changed writes nothing but that row, so a
    listing's `listed_at` says when it last CHANGED; the row's `stored_at` says
    when its service last replied for the window, and a listing goes with the row
    once that is past the window (`_due_sql`). Every listing a feed files lies in
    a window the same fill writes a coverage row for (`store_span`), so none is
    left with nothing to confirm it. A searched listing is confirmed by nothing
    after its own lookup, so it goes by its own age.

    ORDER IS THE WHOLE OF THE CARE HERE. Listings go first; a service's group is
    cleared only once it is old AND NOTHING THE SERVICE STILL LISTS NAMES IT
    (title_rows.plan_unlisted, told which listings this takes off). Clearing a
    title's group on age alone would strand every listing still drawn under it
    — a card with no name, which is worse than either keeping the group or
    dropping the listing with it. One transaction, so that order cannot be seen
    half-done.

    DECIDED BEFORE THE LOCK, AND NOTHING DUE TAKES NO LOCK. The heartbeat runs
    this every minute, and nearly every minute nothing is due. It used to read,
    inside its transaction, every episode row holding a repeat (308 whole rows
    on the development copy) and every holder's keys row by row, to decide
    nothing — MEASURED on a copy of dev: the lock held 0.12 s alone, up to
    0.98 s beside one month read and 2.70 s beside three, and 4.33 s on dev under
    load. The plan reads only what is due, each read answering in one row
    (`_plan_sweep`); when it finds nothing, nothing is written and no writer
    waits; when it finds something, the same plan is made again in the write's
    turn (db.transaction's `prepare`), so the lock is held for the writes.
    """
    ts = db.now() if now is None else now
    cutoff = ts - RETAIN_SECONDS
    if not (await db.run(lambda conn: _plan_sweep(conn, ts))).due:
        return 0

    def _work(conn, planned: _Sweep) -> int:
        planned.book.write(conn, planned.spelled)
        if planned.coverage:
            conn.execute("DELETE FROM calendar_coverage WHERE stored_at <= ?", (cutoff,))
        title_rows.clear(conn, planned.unlisted)
        # And whatever a service's own terms say may not be kept, held or not —
        # after the age sweep, which it overrides (TMDB's six months).
        title_rows.report_ceilings(title_rows.clear(conn, planned.ceilings))
        # The validators outlive nothing. A file whose listings are gone has
        # nothing for a 304 to confirm, so keeping its ETag would answer
        # "unchanged" for a month this app no longer holds anything about.
        if planned.files:
            conn.execute("DELETE FROM calendar_source_files WHERE fetched_at <= ?", (cutoff,))
        return planned.removed

    removed = await db.transaction(_work, prepare=lambda conn: _plan_sweep(conn, ts))
    if removed:
        logger.info("calendar retention: removed %d listing(s) not stored inside "
                    "the last %d days.", removed, RETAIN_SECONDS // 86400)
    return removed


# ---------------------------------------------------------------------------
# which episodes the calendar holds that nobody has looked up
# ---------------------------------------------------------------------------

def _owed_seasons_sql(source: str) -> str:
    """WHICH SEASONS THE EPISODE DRAIN STILL OWES WORK ON. Written once and
    shared by the drain's batch and by the count the page shows, because those
    two answering differently is the exact failure that makes a backlog readout
    untrustworthy.

    Each numbered episode the service lists in full — the title's own entry's
    listing, stating its coordinate — whose description it owes: none, a
    listing's stub, or one fallen due. Parameters: the id namespace to read out
    of the group's ids, and the clock a due date is compared against."""
    stated = (f"(e.{source}_unstated = 0 OR EXISTS (SELECT 1 FROM "
              f"json_each(e.{source}_repeats_json) r WHERE "
              "COALESCE(json_extract(r.value, '$.unstated'), 0) = 0))")
    entry = (f"AND e.{source}_entry IS NULL"
             if groups.has(tables.EPISODES, source, "entry") else "")
    return (
        f"SELECT json_extract(t.{source}_ids_json, '$.' || ?) AS service_id, "
        "       e.media, e.season, "
        # NULL and 0 both mean "never answered", and both must sort ahead of
        # every real timestamp — hence the coalesce rather than MIN alone.
        f"       MIN(COALESCE(e.{source}_fetched_at, 0)) AS answered_at, "
        f"       MAX(e.{source}_listed_at) AS newest "
        f"FROM {tables.EPISODES} e JOIN {tables.TITLES} t ON t.title_key = e.title_key "
        f"WHERE e.{source}_air_ts IS NOT NULL AND t.{source}_id IS NOT NULL "
        f"  AND e.season >= 0 AND e.number >= 0 AND {stated} {entry} "
        f"  AND (e.{source}_fetched_at IS NULL OR e.{source}_fetched_at = 0 "
        f"       OR e.{source}_stale_after <= ?) "
        "GROUP BY service_id, e.media, e.season")


async def owed_season_count(source: str, id_namespace: str, now: int) -> int:
    """How many seasons `owed_episodes` would hand out if nothing capped it.

    The same query the batch comes from, so the number a reader watches count
    down cannot disagree with the work actually being done.
    """
    return int(await db.fetch_value(
        f"SELECT COUNT(*) FROM ({_owed_seasons_sql(source)})", (id_namespace, now)) or 0)


async def owed_episodes(source: str, id_namespace: str, limit: int,
                        now: int) -> list[tuple[int, str, int]]:
    """[(service id, media, season)] for seasons on the calendar whose episodes
    nobody has looked up yet, or whose answers have fallen due again.

    A SEASON AT A TIME, NOT AN EPISODE AT A TIME, because that is the shape of
    the call that answers it — one request returns a season's whole episode list.

    A LISTING'S STUB IS NOT AN ANSWER. The feed gives an episode title and
    nothing else, so `fetched_at = 0` marks a stub: present, keyed, and still
    owed everything a lookup would add.

    A LISTING WITH NO STATED EPISODE NUMBER IS NOT OWED, and leaving it in was a
    live defect: `store_episodes` cannot answer an episode the source never
    numbered, so such a listing came back owed on EVERY pass, and the drain
    re-read those seasons once a minute for ever.

    NEVER-LOOKED-UP SEASONS GO FIRST, and only then the ones falling due. A
    season that has never been answered shows placeholder facts to somebody
    RIGHT NOW; a due one shows facts that were true when they were fetched.
    """
    found = await db.fetch_all(
        _owed_seasons_sql(source) + " ORDER BY answered_at ASC, newest DESC LIMIT ?",
        (id_namespace, now, limit))
    out: list[tuple[int, str, int]] = []
    for row in found:
        try:
            out.append((int(row["service_id"]), str(row["media"]), int(row["season"])))
        except (TypeError, ValueError):
            continue  # a title this service never named in its own id space
    return out


async def store_episodes(source: str, service_id: int, media: str, season: int,
                         episodes: list[dict], now: int) -> int:
    """Write one season's episode facts — every episode of it the calendar lists
    — addressed by the service's own id.

    A SEASON WITH NO EPISODES STILL COUNTS AS ANSWERED, which is why each listed
    episode's description is stamped even when the list is empty: without it a
    season Trakt has nothing for is owed again on every pass for ever.

    WHEN EACH EPISODE FALLS DUE AGAIN IS DECIDED PER EPISODE, from that
    episode's own latest LISTING rather than from the season's or from the
    lookup's clock — a season part-aired sits on both sides of the correction
    window at once.
    """
    by_number = {int(e["number"]): e for e in episodes if e.get("number") is not None}

    def _work(conn) -> int:
        # The title whose group is the entry, and every listed episode of this
        # season on its rows — one row back, one statement to write them all (a
        # season used to be a statement per episode, and the drain answers a
        # couple of hundred seasons a pass).
        found = conn.execute(
            f"SELECT e.* FROM {tables.SERVICE_IDS} i JOIN {tables.EPISODES} e "
            "ON e.title_key = i.title_key AND e.season = ? AND e.number IS NOT NULL "
            "WHERE i.source = ? AND i.media = ? AND i.service_id = ? AND i.is_primary = 1",
            (int(season), source, media, str(service_id))).fetchall()
        answers = []
        for row in found:
            stored = episode_rows.Row.read(row)
            listed = [instant for instant in stored.instants.get(source, ())
                      if instant.entry is None and not instant.unstated]
            if not listed or not stored.coordinated:
                continue
            # The LAST listing, not the first: a re-airing is another chance for
            # somebody to have corrected the record, so the correction window
            # opens again behind it.
            last = max(instant.at for instant in listed)
            answers.append(title_rows.EpisodeAnswer(
                str(row["title_key"]), int(row["number"]),
                by_number.get(int(row["number"])) or {},
                title_rows.episode_stale_after(last, now)))
        title_rows.write_episode_answers(conn, source, media, season, answers, now=now)
        return len(answers)

    return await db.transaction(_work)


# ---------------------------------------------------------------------------
# the questions the read path asks about storage itself
# ---------------------------------------------------------------------------

async def signature() -> str:
    """A cheap token that changes whenever the stored calendar might name a title
    it did not name before.

    THE CONTRACT IS WHAT MATTERS, not the shape: the tracker's name resolution
    caches an index against this and must rebuild when the calendar has moved.
    How many rows something lists plus the newest store is enough -- a listing
    added, replaced or swept moves one or the other.
    """
    names = [source for source in groups.sources()
             if groups.has(tables.EPISODES, source, "listed_at")]
    listed = " OR ".join(f"{source}_air_ts IS NOT NULL" for source in names) or "0"
    newest = (f"MAX({', '.join(f'COALESCE(MAX({s}_listed_at), 0)' for s in names)}, 0)"
              if len(names) > 1 else f"COALESCE(MAX({names[0]}_listed_at), 0)" if names else "0")
    row = await db.fetch_one(
        f"SELECT (SELECT COUNT(*) FROM {tables.EPISODES} WHERE {listed}) AS n, "
        f"(SELECT {newest} FROM {tables.EPISODES}) AS newest")
    return f"{int(row['n'])}:{int(row['newest'])}" if row else "0:0"


async def sources_with_rows(endpoint_key: str, span_start: date, span_end: date
                            ) -> set[str]:
    """Which sources actually HOLD listings on this calendar for this span.

    NOT THE COVERAGE TABLE, AND THE DIFFERENCE IS A BUG THAT SHIPPED. Coverage
    records who replied; this records who left something behind. They came apart
    when a source answered "unchanged" for a span it had never stored anything
    for — coverage said answered, nothing was stored, and because the gate that
    decides whether "unchanged" is acceptable read COVERAGE, the state justified
    itself for ever.

    THE FAILURE DIRECTION IS CHOSEN. A source that has genuinely nothing to say
    about a span looks the same as one that never stored anything, so it will
    re-read a body it did not need — one 200 instead of one 304, occasionally.
    The other way round loses a whole service off a calendar silently, which is
    what happened.
    """
    names = [source for source in groups.sources()
             if groups.has(tables.EPISODES, source, "air_ts")]
    if not names:
        return set()
    # One EXISTS per service, each reaching its own air-time index — the first
    # instant — or the few rows holding repeats.
    holds = ", ".join(
        f"(EXISTS (SELECT 1 FROM {tables.EPISODES} WHERE {source}_calendars != 0 "
        f"AND {source}_calendars & :bit AND {source}_air_ts >= :lo AND {source}_air_ts < :hi) "
        f"OR EXISTS (SELECT 1 FROM {tables.EPISODES} e, json_each(e.{source}_repeats_json) r "
        f"WHERE e.{source}_repeats_json != '[]' "
        "AND json_extract(r.value, '$.calendars') & :bit "
        "AND json_extract(r.value, '$.at') >= :lo AND json_extract(r.value, '$.at') < :hi)) "
        f"AS {source}" for source in names)
    row = await db.fetch_one(f"SELECT {holds}", {
        "bit": _bit(endpoint_key), "lo": episode_rows.epoch(span_start),
        "hi": episode_rows.epoch(span_end)})
    return {source for source in names if row[source]}
