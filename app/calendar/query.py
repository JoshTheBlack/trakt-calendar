"""THE calendar read: which cards one viewer sees, on which day, judged by
which filters — and, for the cards a page draws, what each one says.

EVERY READER ASKS IT: the month and a late day, the search's stored half and
its season picker, the share page and its preview card, the tracker's calendar
import, a mark's redraw. Each used to assemble the same answer from the same
rows through a chain of helpers — the rows turned into a Record per listing,
grouped, every card judged on facts worked out in Python, the survivors
resolved — and the month's cost was those Records: MEASURED on a copy of dev,
October All Episodes read 23,785 of them to draw 1,410 cards.

FOUR STEPS, EACH WHERE IT IS CHEAPEST AND SAID ONCE:
  1. THE ROWS (`_keys`): the episode rows a reader names — a span's, or a
     title's — with each service's listings and the ids it files them under,
     and none of the words. A `Listing` is what a card is made of.
  2. THE CARDS (`cards_of`), in Python over those rows: which listings of one
     episode row are one card, as the store's own rule says, and for each
     viewer where it is placed (`View`) — the day their first service gives,
     its episode, its identity.
  3. THE JUDGING, one SQL statement for the whole read (`judge`, its statement
     `_judge_sql`): what every
     card's filters read, worked out per TITLE AND THE SERVICES LISTING IT —
     a month's cards share a few thousand such answers — and every filter, the
     operator's floor and then the viewer's own, through one predicate builder
     (`_keeps`).
  4. THE DRAWN CARDS (`present`): their winning service per field, one SQL
     statement (`_winners_sql`), and their words read for those rows alone;
     resolve.py composes what the card shows from them.

WHAT DEPENDS ON WHO IS LOOKING IS DECIDED HERE AND NEVER STORED: which services'
listings they read (their source choice), whose value wins (their order), the
day an instant is (their zone), their filters. The rows are every viewer's.
"""
from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from .. import db, endpoints, perftrace, providers, read_pool
from ..metadata import episodes as episode_rows
from ..metadata import groups
from ..metadata import identity as title_identity
from ..metadata import rows as title_rows
from ..metadata import tables
from ..providers.base import (MATCH_SOURCES, Item, Media, Record, Source, epoch_moment,
                              local_moment, render, resolve_key)
from . import filter as calendar_filter
from . import resolve as calendar_resolve
from . import vocab

_SOURCE_RANK = {str(source): rank for rank, source in enumerate(Source)}


def _bit_of(source: str) -> int:
    """A service's bit in a read's service masks: its place in the store's
    declared order."""
    return 1 << groups.sources().index(source)


def _mask(names: Iterable[str]) -> int:
    out = 0
    for name in names:
        out |= _bit_of(str(name))
    return out


# ---------------------------------------------------------------------------
# 1. the rows
# ---------------------------------------------------------------------------

def _keys(conn: db.Connection, where: str, params, extra: Sequence[str] = ()) -> list[dict]:
    """Episode rows `where` names (a condition on the row, aliased `e`): which
    row each is and every service's listings of it — what a card is made of,
    and nothing it says — in one aggregated row (db.aggregated). `extra`: more
    columns of the row, by name (the episode words a marked calendar reads)."""
    names = [*episode_rows.listing_names(), *extra]
    return [dict(zip(names, values)) for values in db.aggregated(
        conn, [f"e.{name}" for name in names], _keys_from(where), params)]


def _keys_from(where: str) -> str:
    """`_keys`' FROM clause."""
    return f"FROM {tables.EPISODES} e WHERE {where}"


def _identities(conn: db.Connection, keys: Iterable[str]) -> dict[str, dict]:
    """{title key: its identity} for each title `keys` names — the id and ids each
    service files it under and every id the store knows of it — read ONCE PER
    TITLE rather than joined to every one of its episode rows (a title row holds
    every service's words, and a month's episode rows are ten times its titles),
    in one aggregated row (db.aggregated)."""
    names = ["title_key", "known_ids"]
    columns = ["t.title_key", "t.ids_json"]
    for source in groups.sources():
        names += [f"t_{source}_id", f"t_{source}_ids_json"]
        columns += [groups.column(tables.TITLES, source, "id", "t"),
                    groups.column(tables.TITLES, source, "ids_json", "t")]
    found = db.aggregated(conn, columns, f"FROM {tables.TITLES} t "
                          "WHERE t.title_key IN (SELECT value FROM json_each(:keys))",
                          {"keys": json.dumps(sorted(keys))})
    return {row["title_key"]: row for row in (dict(zip(names, values)) for values in found)}


class Listing:
    """One service's listing of one episode at one instant — what a card is
    made of, as cheap as it can be: a month reads tens of thousands. Named like
    a Record's fields, so the card rules below (`cards_of`, resolve's airing and
    coordinate) read it as they read a Record."""
    __slots__ = ("source", "air_ts", "date_only", "id", "ids", "known", "label", "season",
                 "episode_number", "title_key", "unit", "media", "instant",
                 "release_types_by_country", "mark")

    def __init__(self, source: str, air_ts: float, date_only: bool, listing_id: str,
                 ids: dict, known: dict, label: str | None, season: int | None,
                 number: int | None, title_key: str, unit: str, media: str,
                 instant: episode_rows.Instant):
        self.source, self.air_ts, self.date_only = source, air_ts, date_only
        self.id, self.ids, self.known = listing_id, ids, known
        self.label, self.season, self.episode_number = label, season, number
        self.title_key, self.unit, self.media, self.instant = title_key, unit, media, instant
        # A Trakt film's release schedule, from its own releases read
        # (calendar_enrich.overlay_releases) — None where nothing was read.
        self.release_types_by_country: dict | None = None
        # Which finale the episode is (base.EPISODE_MARKS' key), on a calendar
        # whose cards say so (`Endpoint.marked`) — "" everywhere else.
        self.mark = ""


class _Reads(NamedTuple):
    """How one service's listings are read on one calendar."""
    bit: int               # the calendar bit its listings carry there
    marker: str | None     # whose word about the episode marks it, if anyone's
    kept: bool             # a listing stays only where `marker` marks the episode


def _calendar_reads(endpoint_key: str) -> dict[str, _Reads]:
    """{service: how its listings are read on this calendar}. A service that
    publishes the calendar is read by its bit, and marked by its own word where
    the calendar's cards say which finale they are; a service that DRAWS it
    (base.DrawnFrom) is read by the bit of the calendar it draws from and kept
    where the service it names marks the episode. From the registry alone, so a
    read worker and the web process answer alike."""
    endpoint = endpoints.ENDPOINTS[endpoint_key]
    registered = providers.registered()
    out = {}
    for source in groups.sources():
        provider = registered.get(Source(source))
        how = provider.capabilities.drawn(endpoint_key) if provider is not None else None
        if how is not None:
            out[source] = _Reads(endpoints.ENDPOINTS[how.listings_of].bit,
                                 str(how.marked_by), True)
        else:
            own = source if (endpoint.marked
                             and groups.has(tables.EPISODES, source, "episode_type")) else None
            out[source] = _Reads(endpoint.bit, own, False)
    return out


def _mark(stored: episode_rows.Row, marker: str | None) -> str:
    """Which finale `marker` says the row's episode is, or ""."""
    if marker is None:
        return ""
    word = (stored.stored or {}).get(f"{marker}_episode_type") or ""
    return providers.registered()[Source(marker)].episode_mark(str(word)) if word else ""


class _Identity(NamedTuple):
    """How one service files a title's listings: its ids with the title's
    shared ids beneath them, every id the store knows, and the id a listing of
    its own entry carries."""
    ids: dict
    known: dict
    listing_id: str


def _identity(row, source: str, decoded: dict) -> _Identity:
    """`source`'s identity for a title (`row`, from `_identities_sql`; None for a
    title the store holds no row of), decoded once per read.

    ITS IDS ARE THE SERVICE'S OWN WITH THE TITLE'S SHARED IDS BENEATH THEM. A
    listing whose service knows a show only by its tvdb id is still the title's
    listing, and the title store's ids (identity.with_shared) are what key every
    listing of one title the same way."""
    key = (id(row), source)
    held = decoded.get(key)
    if held is None:
        known = json.loads(row["known_ids"]) if row is not None and row["known_ids"] else {}
        own_text = row[f"t_{source}_ids_json"] if row is not None else None
        own = json.loads(own_text or "{}") if own_text else {}
        held = _Identity(
            title_identity.with_shared(own, known) if known else own, known,
            # The id the service files its listings of the title under, by its
            # own rule (Provider.listing_id) — from its group's ids, or what the
            # store knows of the title where the service has no group — and the
            # group's own entry where the ids state none of the service's.
            _listing_id(source, own or known)
            or str((row[f"t_{source}_id"] if row is not None else None) or ""))
        decoded[key] = held
    return held


def _listing_id(source: str, ids) -> str:
    return providers.registered()[Source(source)].listing_id(ids)


def _typed(own: str):
    return int(own) if own.isdigit() else own


def _row_listings(row, stored: episode_rows.Row, reads: dict[str, _Reads], identity,
                  decoded: dict) -> list[Listing]:
    """Every listing a stored episode row holds on the calendar `reads`
    describes — less every searched instant whose service's feed lists the same
    episode there, and every listing of a service drawing the calendar whose
    episode its marker does not mark.

    A SEARCHED LISTING STANDS DOWN FOR ITS FEED HERE TOO, not only when a fill
    takes it off (entries.store_span): a fill reaches a span only when it is
    due, and one whose service answered "unchanged" — every frozen month — may
    not be filled with data for weeks, nor ever by a share page."""
    out = []
    for source in groups.sources():
        bit, marker, kept = reads[source]
        mark = _mark(stored, marker)
        if kept and not mark:
            continue
        held = [i for i in stored.instants[source] if i.calendars & bit]
        fed = {(i.entry, i.unstated) for i in held if i.fed & bit}
        title = None
        for instant in held:
            if instant.searched & bit and (instant.entry, instant.unstated) in fed:
                continue
            title = title or _identity(identity, source, decoded)
            season, number = stored.stated(instant)
            if instant.entry is not None:
                namespace = title_rows.ID_NAMESPACE[source]
                ids = title_identity.with_shared({namespace: _typed(instant.entry)}, title.known)
                listing_id = instant.entry
            else:
                ids, listing_id = title.ids, title.listing_id
            listing = Listing(source, float(instant.at), bool(instant.date_only),
                              str(listing_id), ids, title.known, instant.label or None,
                              season, number, stored.key, stored.unit, stored.media,
                              instant)
            listing.mark = mark
            out.append(listing)
    return out


def read_rows(conn: db.Connection, endpoint_key: str, *, first: date | None = None,
              after: date | None = None, titles: Iterable[str] | None = None) -> list[Listing]:
    """Every listing on `endpoint_key` of each episode row with one dated in
    [first, after) — or of `titles`' rows — every service's, at whatever date.

    WHOLE IS THE POINT. One episode is one row, and the row is one card whatever
    dates its services give it — so a listing outside the range is part of a
    card inside it, and decides which day the card is drawn on (`cards_of`).

    A CALENDAR ONE SERVICE DRAWS FROM ANOTHER (base.DrawnFrom — Simkl's finales,
    its All Episodes listings TMDB marks) is read here, by each service's own
    bit (`_calendar_reads`), the marked rows found in SQL and the mark decided by the
    marking service's own word (`Provider.episode_mark`)."""
    reads = _calendar_reads(endpoint_key)
    markers = sorted({r.marker for r in reads.values() if r.marker})
    extra = [f"{marker}_episode_type" for marker in markers]
    decoded: dict = {}
    if titles is not None:
        found = _keys(conn, "e.title_key IN (SELECT value FROM json_each(:titles))",
                      {"titles": json.dumps(sorted({str(k) for k in titles}))}, extra)
        parsed = [(row, episode_rows.Row.read(row)) for row in found]
    else:
        lo, hi = episode_rows.epoch(first), episode_rows.epoch(after)
        bits = {source: r.bit for source, r in reads.items()}
        typed = {source: r.marker for source, r in reads.items() if r.kept}
        found = _keys(conn, f"e.rowid IN ({episode_rows.span_rowids(bits=bits, typed=typed)})",
                      {"lo": lo, "hi": hi}, extra)
        parsed = [(row, stored) for row, stored in
                  ((row, episode_rows.Row.read(row)) for row in found)
                  if _in_span(stored, reads, lo, hi)]
    identities = _identities(conn, {stored.key for _row, stored in parsed})
    listings = [listing for _row, stored in parsed
                for listing in _row_listings(None, stored, reads, identities.get(stored.key),
                                             decoded)]
    # IN THE ORDER A FILL PRODUCES THEM: air time, then the registry's declared
    # order, the rows' own order kept beneath — a stable sort. It is the order
    # cards with the same instant are drawn in, and the order a month's is-new
    # list is kept in.
    listings.sort(key=lambda listing: (listing.air_ts, _SOURCE_RANK.get(listing.source, 99)))
    return listings


def _in_span(stored: episode_rows.Row, reads: dict[str, _Reads], lo: float,
             hi: float) -> bool:
    """Whether any service lists an instant of the row inside [lo, hi) on the
    calendar, as `reads` reads it."""
    for source, held in stored.instants.items():
        bit, marker, kept = reads[source]
        if kept and not _mark(stored, marker):
            continue
        if any(instant.calendars & bit and lo <= instant.at < hi for instant in held):
            return True
    return False


# ---------------------------------------------------------------------------
# 2. the cards
# ---------------------------------------------------------------------------

def utc_day(listing) -> date:
    """The UTC day a listing airs on — the day a window owns it by."""
    return _utc_day(float(listing.air_ts))


@functools.lru_cache(maxsize=1 << 16)
def _utc_day(air_ts: float) -> date:
    # A month's listings share their instants; bounded, as every memo is.
    return epoch_moment(air_ts).date()


def _listing_order(listing) -> tuple:
    """Date order, and a total one: a tie on the instant goes to the registry's
    declared order and then the service's own id, so the same listings make the
    same cards however a reader happened to fetch them."""
    return (float(listing.air_ts), _SOURCE_RANK.get(str(listing.source), len(_SOURCE_RANK)),
            str(listing.id))


# HOW FAR APART, ACROSS A MONTH'S EDGE, ONE SERVICE'S REPEATED LISTING AND
# ANOTHER SERVICE'S LISTING OF THE SAME EPISODE MAY BE AND STILL BE DRAWN AS ONE
# CARD. Inside one calendar month any distance joins.
JOIN_ACROSS_MONTHS_DAYS = 7


def may_join(first: date, second: date) -> bool:
    """Whether two listings of ONE episode, on these UTC days, are drawn
    together where one of their services lists the episode more than once: the
    same calendar month, or at most a week apart across a month's edge (a
    premiere dated the 31st by one service and the 2nd by another).

    WHERE NO SERVICE LISTS IT TWICE, ANY DISTANCE JOINS (`cards_of`): two
    services disagreeing about which day an episode airs on — Dimension 20's
    season 29 premiere is 8 April on Trakt and 16 September on Simkl — are one
    episode said twice, drawn once on the viewer's own service's day with the
    other day beside it."""
    return ((first.year, first.month) == (second.year, second.month)
            or abs((first - second).days) <= JOIN_ACROSS_MONTHS_DAYS)


def cards_of(listings: list) -> list[list]:
    """ONE EPISODE's listings — every service's listing of one stored episode
    row — as the cards a calendar draws. Pure, and deterministic in its input.

    ONE CARD, WHATEVER THE DATES, UNLESS ONE SERVICE LISTS THE EPISODE TWICE.
    Which listings are one episode is the row, decided as they were written. One
    service saying the episode airs twice is a repeat the calendar has always
    drawn twice, so in date order a listing joins the current card when its
    service is not in it yet and it may join every listing already there
    (`may_join`), and otherwise starts the next — folding a service into its own
    other listing would collapse it rather than reconcile two services. Then
    cards that share no service are one card, each joining the nearest earlier
    one: two services' dates for one episode, however far apart, are one
    episode.

    HANDED EVERY STORED LISTING OF THE EPISODE, NEVER A RANGE'S SHARE OF THEM.
    Each card's membership depends on the listings before it, so a listing far
    outside a reader's range can still decide where a card inside it closes. A
    month, a day, a search and a share page all read an episode whole
    (`read_rows`), which is what makes them draw the same cards on the same days
    by construction.
    """
    if len({str(listing.source) for listing in listings}) == len(listings):
        # EACH SERVICE LISTING IT ONCE — almost every row — is one card: whatever
        # units the dates make, none shares a service with another, so each joins
        # the one before it, in date order.
        return [sorted(listings, key=_listing_order)]
    day_of = {id(listing): utc_day(listing) for listing in listings}
    units: list[list] = []
    for listing in sorted(listings, key=_listing_order):
        current = units[-1] if units else None
        if (current is not None
                and all(str(held.source) != str(listing.source) for held in current)
                and all(may_join(day_of[id(held)], day_of[id(listing)]) for held in current)):
            current.append(listing)
        else:
            units.append([listing])
    cards: list[list] = []
    for unit in units:
        services = {str(listing.source) for listing in unit}
        apart = [card for card in cards if not services & {str(r.source) for r in card}]
        if apart:
            min(apart, key=lambda card: min(abs(float(a.air_ts) - float(b.air_ts))
                                            for a in card for b in unit)).extend(unit)
        else:
            cards.append(list(unit))
    return cards


class Card(NamedTuple):
    """One card every viewer's read shares: one episode row's listings that are
    one card (`cards_of`), each service's at most once."""
    title_key: str
    unit: str
    media: str
    listings: tuple
    # {service: its listing in the card} — one each (`cards_of`).
    by_source: dict

    def ids(self, namespaces: Iterable[str] | None = None) -> dict:
        """The ids the card is known by — every id any of its listings names,
        the earlier listing's spelling first — which is the card's identity and
        what its buttons address. `namespaces` narrows it to those (the identity
        waterfall reads four)."""
        out: dict = {}
        for listing in self.listings:
            for namespace, value in (listing.ids or {}).items():
                if (namespaces is None or namespace in namespaces) and value not in (None, ""):
                    out.setdefault(namespace, value)
        return out


def cards(listings: Iterable[Listing]) -> list[Card]:
    """`listings` as cards, row by row."""
    by_row: dict[tuple[str, str], list[Listing]] = {}
    for listing in listings:
        by_row.setdefault((listing.title_key, listing.unit), []).append(listing)
    out = []
    for (title_key, unit), held in by_row.items():
        for card in cards_of(held):
            out.append(Card(title_key, unit, held[0].media, tuple(card),
                            {str(listing.source): listing for listing in card}))
    return out


# ---------------------------------------------------------------------------
# 3. one viewer's cards: placed and judged
# ---------------------------------------------------------------------------

class Viewer(NamedTuple):
    """Everything a read decides on one viewer's behalf. `prefs` is their
    source selection and order (None: nobody is asking — a share page reads
    under its owner's), `settings` the operator's (the floor, which services
    the instance puts on its calendar), `specs` their filters
    (`vocab.active_specs`' keywords, complete)."""
    endpoint: Any
    settings: Any
    prefs: Any
    tz: ZoneInfo
    specs: dict


class View(NamedTuple):
    """One card as ONE VIEWER's read places it, before anything is drawn: the
    services the instance admits that list it (`every`, declared order), the
    viewer's own of them in their order (`listed`), whose card it is
    (`primary`), when it airs and on which local day, its episode, and its
    identity — every number a month states, without a word of what it says."""
    card: Card
    every: tuple
    listed: tuple
    air_ts: float
    date_only: bool
    air_date: str
    season: int | None
    episode_number: int | None
    episode_label: str | None
    mark_key: str
    # Filled by the judging: whether a filter a read applies could not judge
    # this card yet, on the viewer's own services (`Exempting.lets_through`).
    enriched: bool = True
    keywords_known: bool = True
    show_type_known: bool = True
    # Filled by `ranked`, for a day over a page alone: how many of
    # `COUNTED_FACTS` the card will show — what a big day's first page ranks by
    # after who lists it (app/calendar/day_pages.py).
    complete: int = 0

    @property
    def primary(self) -> Listing:
        return self.listed[0]

    @property
    def id(self) -> str:
        return self.primary.id

    @property
    def known_ids(self) -> dict:
        """Every id the title store knows of the title — what a mark filed under
        another service's spelling is matched by (calendar_state.marked)."""
        return dict(self.primary.known or {})

    @property
    def media(self) -> str:
        return self.card.media

    @property
    def ids(self) -> dict:
        return self.card.ids() or dict(self.primary.ids or {})

    @property
    def title_key(self) -> str:
        return self.card.title_key

    @property
    def keywords(self):
        """None where nothing the viewer reads states the title's keywords —
        the question `Exempting.lets_through` asks of a drawn card."""
        return [] if self.keywords_known else None

    @property
    def show_type(self):
        return "" if self.show_type_known else None


class Placed(NamedTuple):
    """One read's cards for one viewer: the ones they see, and how many of
    theirs their own filters removed (`assemble_range`'s `filtered`)."""
    views: list[View]
    filtered_out: int


def _admitted(settings) -> frozenset[str] | None:
    return calendar_resolve.instance_sources(settings)


def _reads(prefs) -> frozenset[str] | None:
    """The services this viewer reads, None for every one — the ordinary case,
    where a card's own listings are all of them."""
    if prefs is None:
        return None
    reads = frozenset(str(source) for source in Source if prefs.admits_calendar(source))
    return None if len(reads) == len(Source) else reads


def _declared(names: Iterable[str]) -> tuple[str, ...]:
    """`names` in the registry's declared order — the order a card's services are
    laid out in before a viewer's order is applied."""
    return _declared_set(frozenset(str(name) for name in names))


@functools.cache
def _declared_set(names: frozenset[str]) -> tuple[str, ...]:
    # A subset of the registered services: a handful of answers, ever.
    return tuple(sorted(names, key=lambda n: _SOURCE_RANK.get(n, 99)))


def place(card: Card, *, instance, reads, orders: calendar_resolve.Orders,
          tz: ZoneInfo, days: dict | None = None) -> View | None:
    """`card` as one viewer places it — None when none of the services they
    read lists it (it is not theirs to draw, and is counted nowhere).

    WHICH DAY IS DECIDED BY WHICH OF A CARD'S SERVICES DESCRIBES IT — the first
    in this viewer's order (resolve.resolve_airing), date and time included —
    and the day is that instant in the viewer's zone (base.local_moment). So two
    viewers with different orders, reading the very same rows, see one card each,
    each on their own service's day; and no reader works a day out from one
    listing. THE ONE PLACEMENT, for a month, a late day, a share page, the
    tracker's import and every card the search names."""
    if len(card.listings) == 1:
        # ONE SERVICE LISTS IT — most cards of a month: its own instant and its
        # own coordinate are the card's, which is what resolve_airing and
        # resolve_coordinate answer for one listing, without asking them.
        (primary,) = card.listings
        if (instance is not None and primary.source not in instance) or (
                reads is not None and primary.source not in reads):
            return None
        every = listed = (primary,)
        air_ts, date_only, stated = primary.air_ts, primary.date_only, primary
    else:
        every = tuple(card.by_source[name] for name in _declared(card.by_source)
                      if instance is None or name in instance)
        own = every if reads is None else tuple(l for l in every if l.source in reads)
        if not own:
            return None
        listed = tuple(card.by_source[name] for name in orders(_declared(l.source for l in own)))
        primary = listed[0]
        air_ts, date_only = calendar_resolve.resolve_airing(listed)
        coordinate = calendar_resolve.resolve_coordinate(listed)
        stated = coordinate if coordinate is not None else primary
    # THE CARD'S IDENTITY, by the waterfall (base.resolve_key), which reads the
    # shared namespaces alone — so the card's whole id union is not built here.
    ids = card.ids(MATCH_SOURCES) or primary.ids or {}
    identity = resolve_key(Media(card.media), ids)
    day = days.get((air_ts, date_only)) if days is not None else None
    if day is None:
        # A month's cards share their instants (a channel's evening slots), so a
        # read works each one's local day out once.
        day = local_moment(air_ts, date_only, tz).strftime("%Y-%m-%d")
        if days is not None:
            days[(air_ts, date_only)] = day
    return View(card, every, listed, air_ts, date_only, day,
                stated.season, stated.episode_number, stated.label,
                str(identity) if identity is not None else f"{primary.source}:{primary.id}")


# ---------------------------------------------------------------------------
# 3b. the judging: one statement, one predicate builder
# ---------------------------------------------------------------------------
#
# WHAT A CARD'S FILTERS READ IS WORKED OUT PER TITLE AND THE SERVICES LISTING
# IT, not per card: every card of one title that the same services list asks the
# same questions of the same groups, and a month's eighteen thousand cards are a
# few thousand such combinations. Each is one element of a JSON payload (`_combo`)
# and the statement answers each with whether the release narrowing, the
# disguised-film rule, the operator's floor and the viewer's filters keep it.

# What a card's filters read of its title, each the first service in the
# viewer's order that states it (`_winner`).
_JUDGED_SCALARS = ("network", "country", "certification", "language", "poster")

# WHAT A CARD SHOWS OF ITS TITLE, counted for how complete the card is: a fact
# counts when any of the viewer's own services that speaks for the card states
# it, which is exactly when the drawn card shows it — the card takes each field
# from the first of those services that states it, whatever their order. Asked
# only for a day over a page (`ranked`), in a statement of its own over the same
# combinations the judging reads: MEASURED on a copy of dev, worked out in the
# month's judging it cost every month 0.07-0.10 s for the few big days a shell
# draws.
COUNTED_FACTS = ("poster", "overview", "network", "runtime", "genres_json", "ratings_json",
                 "certification", "country", "language", "status")


def _speaks(source: str) -> str:
    """SQL: `source` speaks for the combo's card — it lists it (`c.listed`), or
    it describes every card it does not list and holds a group of the title
    (`c.cand`; resolve.describing_sources, Capabilities.describes_every_card).
    A group that only describes NEVER makes a card, wins its attribution, its
    airing or its episode, or counts as listing it — those are chosen among the
    listings alone; it competes for the title's facts like any listing does."""
    bit = _bit_of(source)
    return (f"((c.listed & {bit}) != 0 OR ((c.cand & {bit}) != 0 AND "
            f"{groups.present(tables.TITLES, source, 't')}))")


def _stated(source: str) -> str:
    """SQL: `source`'s group states the title's keywords and show type — only a
    description does (`rows.facts_values`): a listing nobody described says
    nothing about either, which an include filter must not read as "none"."""
    if not groups.has(tables.TITLES, source, "facts_known"):
        return "0"
    return f"({_speaks(source)} AND t.{source}_facts_known)"


def _text(source: str, name: str) -> str:
    """SQL: `source`'s title word `name` is stated — present, not empty."""
    return f"NULLIF({groups.column(tables.TITLES, source, name, 't')}, '') IS NOT NULL"


def _states(source: str, name: str) -> str:
    """SQL: `source`'s title word `name` holds something — not NULL and not the
    column's own empty value (groups.EMPTY: '', '[]', '{}'); a number is an
    answer whatever it is, zero included."""
    column = groups.column(tables.TITLES, source, name, "t")
    empty = groups.EMPTY[name]
    if empty is None:
        return f"{column} IS NOT NULL"
    return f"COALESCE({column}, {groups.literal(empty)}) NOT IN ({groups.literal(empty)}, '')"


def _completeness() -> str:
    """SQL: how many of `COUNTED_FACTS` the combo's card shows — each one any of
    the viewer's own services speaking for it states."""
    return " + ".join(
        f"(CASE WHEN {_any(lambda s, f=fact: f'({_own(s)} AND {_speaks(s)} AND {_states(s, f)})')}"
        f" THEN 1 ELSE 0 END)" for fact in COUNTED_FACTS)


def _winner(order: str, present: Callable[[str], str]) -> str:
    """SQL: the first service in the JSON array `order` for which `present`
    holds — None when none does. THE ONE RULE FOR WHOSE VALUE A CARD SHOWS: a
    preference REORDERS and never excludes, so a field only one service states
    comes from that service whoever the viewer prefers, and an empty value is no
    answer (resolve's `_present`: None, "" and [] are absences, zero is not)."""
    sources = groups.sources()
    arms = []
    for at in range(len(sources)):
        cases = " ".join(f"WHEN '{source}' THEN CASE WHEN {present(source)} THEN '{source}' END"
                         for source in sources)
        arms.append(f"CASE json_extract({order}, '$[{at}]') {cases} END")
    return f"COALESCE({', '.join(arms)})"


def _value_of(winner: str, value: Callable[[str], str], default: str = "''") -> str:
    """SQL: the winning service's `value`, `default` where none won."""
    cases = " ".join(f"WHEN '{source}' THEN {value(source)}" for source in groups.sources())
    return f"COALESCE(CASE {winner} {cases} END, {default})"


def _any(term: Callable[[str], str]) -> str:
    return "(" + " OR ".join(term(source) for source in groups.sources()) + ")"


def _per_service(name: str, wanted: Callable[[str], str], column: str) -> str:
    """Each service's `column` as `{name}_{source}` — NULL where `wanted` says
    the service says nothing to this card — for a predicate to look inside."""
    return ", ".join(
        f"CASE WHEN {wanted(source)} THEN {groups.column(tables.TITLES, source, column, 't')} "
        f"END AS {name}_{source}" for source in groups.sources())


def _release_map(source: str) -> str:
    """SQL: the release schedule a service's listing of a film carries — its
    group's, or Trakt's own releases read where one was made (`c.releases`,
    calendar_enrich.overlay_releases)."""
    column = groups.column(tables.TITLES, source, "release_types_json", "t")
    return f"COALESCE(c.releases, {column})" if source == str(Source.TRAKT) else column


def _release_ok(rules: tuple, params: dict) -> str:
    """SQL: the release narrowing keeps the combo's film — a group judged on
    the listings that can answer, and kept whole when none of them can.

    A FILM'S RELEASE SCHEDULE IS A FACT ABOUT THE TITLE, so where one listing
    holds it the other's silence is no second opinion; but a group no listing
    can judge is kept, because nothing has been published to judge it by
    (filter.py's release docstrings have the measurements). Asked of the
    LISTINGS, never of a group that only describes the card. THE TWO DIMENSIONS
    ARE JUDGED ON THE SAME BLOCK: "US, theatrical" is a US block that is
    theatrical, and an excluded country disqualifies the block, not the film."""
    (c_inc, c_exc), (t_inc, t_exc) = rules
    params.update(rc_inc=json.dumps(sorted(c_inc)), rc_exc=json.dumps(sorted(c_exc)),
                  rt_inc=json.dumps(sorted(t_inc)), rt_exc=json.dumps(sorted(t_exc)))
    knows, keeps = [], []
    for source in groups.sources():
        listed = f"(c.listed & {_bit_of(source)}) != 0"
        held = _release_map(source)
        knows.append(f"({listed} AND (SELECT COUNT(*) FROM json_each({held})) > 0)")
        keeps.append(
            f"({listed} AND EXISTS (SELECT 1 FROM json_each({held}) country, "
            f"json_each(country.value) kind WHERE "
            f"lower(country.key) NOT IN (SELECT value FROM json_each(:rc_exc)) AND "
            f"(json_array_length(:rc_inc) = 0 OR "
            f"lower(country.key) IN (SELECT value FROM json_each(:rc_inc))) AND "
            f"kind.value NOT IN (SELECT value FROM json_each(:rt_exc)) AND "
            f"(json_array_length(:rt_inc) = 0 OR "
            f"kind.value IN (SELECT value FROM json_each(:rt_inc)))))")
    return f"(NOT ({' OR '.join(knows)}) OR {' OR '.join(keeps)})"


def _hits(prefix: str, param: str) -> str:
    """SQL: some service's list (`{prefix}_{source}`, a JSON array or NULL)
    names one of `param`'s values, compared folded."""
    return _any(lambda source: f"EXISTS (SELECT 1 FROM json_each(w.{prefix}_{source}) j "
                               f"WHERE lower(j.value) IN (SELECT value FROM json_each(:{param})))")


def _keeps(rules: calendar_filter.TitleRules, tag: str, params: dict, sparse: int) -> str:
    """SQL: a judged card survives `rules` — THE ONE PREDICATE BUILDER, asked of
    the operator's floor and then of the viewer's filters, so a viewer can only
    narrow what the floor left and nothing about the two is judged differently.
    `tag` names this rule set's parameters; `sparse` is the mask of services
    whose listings arrive undescribed (Capabilities.describes_sparse_listings).

    THREE KINDS OF RULE, AND THEY TREAT A TITLE NOBODY HAS DESCRIBED DIFFERENTLY,
    each the instance owner's decision, settled with the numbers in front of them:
      - genres, countries, certifications, networks, language and missing data
        judge facts a description supplies, so a card nothing has described is
        shown unjudged (`w.enriched`): hiding what has not been looked up reads
        as "nothing matched";
      - keywords and show type are facts only a catalogue states, judged only
        once one has (`w.kw_known`, `w.show_type` not NULL), whoever else
        described the card; "stated, none" is judged like any empty value;
      - "only one service lists it" and "only a sparse catalogue lists it and it
        names no network" are about who LISTS the card, known the moment it is
        listed, so neither exempts anything — the second judges an undescribed
        card on a fact it may not have yet, deliberately: a catalogue's
        undescribed long tail is what it is for, and a title it describes with a
        network comes back within minutes.
    Lists compare folded, except a network's name: one week held both TVN and
    tvN, a Polish broadcaster and a Korean one (filter.parse_network_spec)."""
    if not rules.narrows():
        return "1"
    terms: list[str] = []

    def values(name: str, chosen: Iterable) -> str:
        params[f"{tag}_{name}"] = json.dumps(sorted(chosen))
        return f"(SELECT value FROM json_each(:{tag}_{name}))"

    if rules.single_service:
        terms.append("(w.listed & (w.listed - 1)) != 0")
    if rules.sparse_no_network:
        terms.append(f"NOT ((w.listed & ~{sparse}) = 0 AND "
                     f"trim(w.network, ' ' || char(9, 10, 13)) = '')")
    described: list[str] = []
    for name, (inc, exc) in (("country", rules.countries),
                             ("certification", rules.certifications),
                             ("language", rules.languages)):
        if exc:
            described.append(f"lower(w.{name}) NOT IN {values(name + '_exc', exc)}")
        if inc:
            described.append(f"lower(w.{name}) IN {values(name + '_inc', inc)}")
    g_inc, g_exc = rules.genres
    if g_exc:
        values("genres_exc", g_exc)
        described.append(f"NOT {_hits('g', f'{tag}_genres_exc')}")
    if g_inc:
        values("genres_inc", g_inc)
        described.append(_hits("g", f"{tag}_genres_inc"))
    n_inc, n_exc = rules.networks
    if n_exc:
        described.append(f"w.network NOT IN {values('network_exc', n_exc)}")
    if n_inc:
        described.append(f"w.network IN {values('network_inc', n_inc)}")
    for rule in rules.missing:
        described.append("NOT (" + " AND ".join(
            f"trim(w.{fact}, ' ' || char(9, 10, 13)) = ''" for fact in rule) + ")")
    if described:
        terms.append(f"(NOT w.enriched OR ({' AND '.join(described)}))")
    k_inc, k_exc = rules.keywords
    if k_inc or k_exc:
        stated = []
        if k_exc:
            values("keywords_exc", k_exc)
            stated.append(f"NOT {_hits('k', f'{tag}_keywords_exc')}")
        if k_inc:
            values("keywords_inc", k_inc)
            stated.append(_hits("k", f"{tag}_keywords_inc"))
        terms.append(f"(NOT w.kw_known OR ({' AND '.join(stated)}))")
    s_inc, s_exc = rules.show_types
    if s_inc or s_exc:
        stated = []
        if s_exc:
            stated.append(f"lower(w.show_type) NOT IN {values('show_types_exc', s_exc)}")
        if s_inc:
            stated.append(f"lower(w.show_type) IN {values('show_types_inc', s_inc)}")
        terms.append(f"(w.show_type IS NULL OR ({' AND '.join(stated)}))")
    return "(" + " AND ".join(terms) + ")" if terms else "1"


def _title_column(name: str) -> Callable[[str], str]:
    return lambda source: groups.column(tables.TITLES, source, name, "t")


def _judged_value(field: str) -> str:
    """SQL: the judged value of one of `_JUDGED_SCALARS` — the first service in
    the viewer's order that speaks for the card and states it, '' for none."""
    return _value_of(_winner("c.ord", lambda s: f"({_speaks(s)} AND {_text(s, field)})"),
                     _title_column(field))


def _own(source: str) -> str:
    """SQL: the viewer reads `source` — one of their own services."""
    return f"(c.own & {_bit_of(source)}) != 0"


def _judge_sql(media: str, release, floor, viewer, params: dict,
               titles: str = tables.TITLES) -> str:
    """The judging statement over `:combos` — see the section's note."""
    winners = ", ".join(f"{_judged_value(field)} AS {field}" for field in _JUDGED_SCALARS)
    show_type = _value_of(
        _winner("c.ord", lambda s: f"({_stated(s)} AND {_text(s, 'show_type')})"),
        _title_column("show_type"), default=f"CASE WHEN {_any(_stated)} THEN '' END")
    enriched = _title_column("enriched")
    return f"""
WITH c AS (
  SELECT key AS k, json_extract(value, '$[0]') AS title_key,
         json_extract(value, '$[1]') AS listed, json_extract(value, '$[2]') AS cand,
         json_extract(value, '$[3]') AS ord, json_extract(value, '$[4]') AS lead,
         json_extract(value, '$[5]') AS own, json_extract(value, '$[6]') AS releases
  FROM json_each(:combos)
), w AS (
  SELECT c.k, c.listed, {winners}, {show_type} AS show_type,
         {_any(lambda s: f"({_speaks(s)} AND {enriched(s)})")} AS enriched,
         {_any(_stated)} AS kw_known,
         {_any(lambda s: f"({_own(s)} AND {_speaks(s)} AND {enriched(s)})")} AS own_enriched,
         {_any(lambda s: f"({_own(s)} AND {_stated(s)})")} AS own_stated,
         {_value_of('c.lead', _title_column('anime_type'))} AS anime_type,
         {_per_service('g', _speaks, 'genres_json')},
         {_per_service('k', _stated, 'keywords_json')},
         {release} AS release_ok
  FROM c LEFT JOIN {titles} t ON t.title_key = c.title_key
)
SELECT w.k, w.release_ok, {"w.anime_type = 'movie'" if media == str(Media.SHOW) else "0"} AS pruned,
       {floor} AS floor_ok, {viewer} AS viewer_ok,
       w.own_enriched, w.own_stated
FROM w"""


class _Verdict(NamedTuple):
    release_ok: bool
    pruned: bool
    floor_ok: bool
    viewer_ok: bool
    own_enriched: bool
    own_stated: bool


def _combo(view: View, describers: tuple[str, ...], reads, orders,
           named: dict | None = None) -> tuple:
    """What the judging asks about a card, as the key every card asking the same
    shares and the payload the statement reads: its title, the services the
    instance admits that list it (`listed`), those that may describe it without
    listing it (`cand`), all of them in the viewer's order (`ord` — a describing
    service without a group falls out of it in the statement), the first of the
    listings in that order (`lead`, whose word decides the disguised-film rule),
    the services the viewer reads among them (`own`), and a Trakt film's
    release schedule from its own releases read."""
    listed_names = tuple(l.source for l in view.every)
    of_names = named.get(listed_names) if named is not None else None
    if of_names is None:
        cand_names = tuple(name for name in describers if name not in listed_names)
        own_names = [name for name in (*listed_names, *cand_names)
                     if reads is None or name in reads]
        of_names = (_mask(listed_names), _mask(cand_names),
                    json.dumps(orders(_declared((*listed_names, *cand_names)))),
                    orders(_declared(listed_names))[0], _mask(own_names))
        if named is not None:
            named[listed_names] = of_names
    listed, cand, ordered, lead, own = of_names
    trakt = view.card.by_source.get(str(Source.TRAKT))
    releases = (json.dumps(trakt.release_types_by_country)
                if trakt is not None and trakt.release_types_by_country is not None
                and str(Source.TRAKT) in listed_names else None)
    return (view.card.title_key, listed, cand, ordered, lead, own, releases)


def _verdicts(conn: db.Connection, combos: Sequence[tuple], viewer: Viewer, *,
              titles: str = tables.TITLES, params: dict | None = None) -> dict[int, _Verdict]:
    """The judging statement's answer for each of `combos` (`_combo`), by
    position."""
    media = str(viewer.endpoint.media)
    specs = vocab.complete_specs(viewer.specs)
    params = {**(params or {}), "combos": json.dumps(list(combos))}
    sparse = _mask(calendar_resolve.sparse_sources() & set(groups.sources()))
    release = "1"
    if media == str(Media.MOVIE):
        countries = calendar_filter.parse_spec(specs["movie_release_countries"])
        types = calendar_filter.parse_release_type_spec(specs["movie_release_types"])
        if any((*countries, *types)):
            release = _release_ok((countries, types), params)
    floor = ("1" if viewer.settings is None else
             _keeps(vocab.title_rules(vocab.floor_specs(viewer.settings, media), media),
                    "floor", params, sparse))
    mine = _keeps(vocab.title_rules(specs, media), "viewer", params, sparse)
    # One aggregated row (db.aggregated): a month's combinations are thousands.
    return {int(k): _Verdict(*(bool(value) for value in verdict))
            for k, *verdict in db.aggregated(
                conn, ("k", "release_ok", "pruned", "floor_ok", "viewer_ok", "own_enriched",
                       "own_stated"),
                f"FROM ({_judge_sql(media, release, floor, mine, params, titles)})", params)}


def _completeness_sql() -> str:
    """How many of `COUNTED_FACTS` each combination of `:combos` (`_combo`)
    shows — the judging statement's own reading of the combination and its
    title row, asking nothing else."""
    return f"""
WITH c AS (
  SELECT key AS k, json_extract(value, '$[0]') AS title_key,
         json_extract(value, '$[1]') AS listed, json_extract(value, '$[2]') AS cand,
         json_extract(value, '$[5]') AS own
  FROM json_each(:combos)
)
SELECT c.k, {_completeness()} AS complete
FROM c LEFT JOIN {tables.TITLES} t ON t.title_key = c.title_key"""


def completeness(conn: db.Connection, views: Sequence[View], viewer: Viewer) -> list[View]:
    """`views` with `complete` filled — how many of `COUNTED_FACTS` each card
    shows this viewer, by one statement over the combinations they share."""
    if not views:
        return []
    describers = calendar_resolve.describing_sources(viewer.settings)
    reads = _reads(viewer.prefs)
    orders = calendar_resolve.Orders(viewer.prefs)
    combos: dict[tuple, int] = {}
    named: dict = {}
    keyed = [combos.setdefault(_combo(view, describers, reads, orders, named), len(combos))
             for view in views]
    counts = {int(row["k"]): int(row["complete"] or 0) for row in conn.execute(
        _completeness_sql(), {"combos": json.dumps(list(combos))}).fetchall()}
    return [view._replace(complete=counts.get(at, 0)) for view, at in zip(views, keyed)]


async def ranked(viewer: Viewer, views: Sequence[View]) -> list[View]:
    """`completeness` on a worker thread — what a big day's first page asks
    (app/calendar/day_pages.py) before it ranks the day."""
    return await db.run(lambda conn: completeness(conn, views, viewer))


def judge(conn: db.Connection, views: list[View], viewer: Viewer) -> Placed:
    """The views one viewer's filters keep, and how many of them their own
    filters removed. In the order the rules apply, every count as the page says
    it: the release narrowing (counted), the disguised-film rule (not the
    viewer's), the operator's floor (not counted — not theirs to undo), their
    own filters (counted)."""
    describers = calendar_resolve.describing_sources(viewer.settings)
    reads = _reads(viewer.prefs)
    orders = calendar_resolve.Orders(viewer.prefs)
    combos: dict[tuple, int] = {}
    named: dict = {}
    keyed = []
    for view in views:
        combo = _combo(view, describers, reads, orders, named)
        keyed.append(combos.setdefault(combo, len(combos)))
    if not views:
        return Placed([], 0)
    verdicts = _verdicts(conn, list(combos), viewer)
    released = floored = 0
    kept = []
    for view, at in zip(views, keyed):
        verdict = verdicts[at]
        if not verdict.release_ok:
            continue
        released += 1
        if verdict.pruned or not verdict.floor_ok:
            continue
        floored += 1
        if verdict.viewer_ok:
            kept.append(view._replace(enriched=verdict.own_enriched,
                                      keywords_known=verdict.own_stated,
                                      show_type_known=verdict.own_stated))
    # EVERY PER-VIEWER REMOVAL, IN ONE NUMBER: the release rule's and the rest of
    # this viewer's — never the floor's or the disguised-film rule's, which are
    # not theirs to undo, so counting them would send a viewer looking for a
    # setting that would not bring the title back.
    return Placed(kept, (len(views) - released) + (floored - len(kept)))


def views_of(listings: list[Listing], viewer: Viewer, *,
             near: Callable[[Card], bool] | None = None,
             narrow: Callable[[list[Card]], list[Card]] | None = None) -> list[View]:
    """Every card `listings` make, as `viewer` places it — of the cards `near`
    keeps, when a reader names a span: ONLY A CARD THAT COULD BE DRAWN INSIDE IT
    IS PLACED AND JUDGED. A card is drawn at one of its own listings' instants
    (its first service's, or the time it borrowed) and a zone moves an instant by
    less than a day, so a card none of whose listings falls inside the span
    padded a day each side cannot land in it — and the windows a span reads hold
    up to a week either side of it: measured, a day read resolved ~2,500 cards to
    draw ~350."""
    instance = _admitted(viewer.settings)
    reads = _reads(viewer.prefs)
    orders = calendar_resolve.Orders(viewer.prefs)
    made = cards(listings)
    days: dict = {}
    out = []
    for card in (narrow(made) if narrow is not None else made):
        if near is not None and not near(card):
            continue
        view = place(card, instance=instance, reads=reads, orders=orders, tz=viewer.tz,
                     days=days)
        if view is not None:
            out.append(view)
    return out


# ---------------------------------------------------------------------------
# 4. the drawn cards
# ---------------------------------------------------------------------------
#
# WHAT A DRAWN CARD SAYS IS ITS SERVICES' WORDS, each field from the first of
# the viewer's own services that states it — decided by one statement
# (`_winners_sql`) over the same `_winner` rule the judging uses — and composed
# by resolve.compose from per-service Records built for these rows alone.

def _present_for(field: str) -> Callable[[str], str]:
    """SQL, per service: `field` is stated for the drawn card — a listing's own
    episode words beside its title's, a title-only record's title words alone,
    exactly as the Records the card is composed from hold them."""
    def listing(source: str) -> str:
        return f"(c.listed & {_bit_of(source)}) != 0"

    def describing(source: str) -> str:
        bit = _bit_of(source)
        return (f"((c.cand & {bit}) != 0 AND (c.listed & {bit}) = 0 AND "
                f"{groups.present(tables.TITLES, source, 't')})")

    def described(source: str) -> str:
        return (f"e.{source}_fetched_at IS NOT NULL"
                if groups.has(tables.EPISODES, source, "fetched_at") else "0")

    def present(source: str) -> str:
        if field == "rating":
            stated = (f"json_extract({groups.column(tables.TITLES, source, 'ratings_json', 't')},"
                      f" '$.\"{source}\"') IS NOT NULL")
        elif field == "imdb_rating":
            stated = (f"json_extract({groups.column(tables.TITLES, source, 'ratings_json', 't')},"
                      f" '$.imdb') IS NOT NULL")
        elif field == "runtime":
            title_runtime = f"{groups.column(tables.TITLES, source, 'runtime', 't')} IS NOT NULL"
            episode = (f"({described(source)} AND e.{source}_runtime IS NOT NULL)"
                       if groups.has(tables.EPISODES, source, "runtime") else "0")
            return (f"(({listing(source)} AND ({episode} OR {title_runtime})) OR "
                    f"({describing(source)} AND {title_runtime}))")
        elif field == "episode_title":
            if not groups.has(tables.EPISODES, source, "title"):
                return "0"
            return (f"({listing(source)} AND {described(source)} AND "
                    f"NULLIF(e.{source}_title, '') IS NOT NULL)")
        elif field == "show_type":
            return f"(({listing(source)} OR {describing(source)}) AND {_text_stated(source)})"
        else:
            stated = _text(source, field)
        return f"(({listing(source)} OR {describing(source)}) AND {stated})"
    return present


def _text_stated(source: str) -> str:
    if not groups.has(tables.TITLES, source, "facts_known"):
        return "0"
    return f"(t.{source}_facts_known AND {_text(source, 'show_type')})"


_DRAWN_FIELDS = (*calendar_resolve.SCALAR_FIELDS, "show_type")


def _winners_sql() -> str:
    """The drawn cards' winning service per field over `:cards` — [title key,
    unit, the viewer's own listing services, the services that may describe it
    without listing it, both in the viewer's order]."""
    winners = ", ".join(f"{_winner('c.ord', _present_for(field))} AS w_{field}"
                        for field in _DRAWN_FIELDS)
    return f"""
WITH c AS (
  SELECT key AS k, json_extract(value, '$[0]') AS title_key, json_extract(value, '$[1]') AS unit,
         json_extract(value, '$[2]') AS listed, json_extract(value, '$[3]') AS cand,
         json_extract(value, '$[4]') AS ord
  FROM json_each(:cards)
)
SELECT c.k, {winners}
FROM c LEFT JOIN {tables.TITLES} t ON t.title_key = c.title_key
LEFT JOIN {tables.EPISODES} e ON e.title_key = c.title_key AND e.unit = c.unit"""


# WHAT A SERVICE'S TITLE GROUP SAYS, as a drawn card's Records name it — a
# listing's and a title-only one's alike, so the two cannot read one group
# differently (`_words`).
_TITLE_WORDS = ("title", "ids_json", "detail_url", "year", "network", "country", "language",
                "certification", "status", "overview", "poster", "runtime", "genres_json",
                "ratings_json", "anime_type", "release_types_json", "enriched", "keywords_json",
                "show_type", "facts_known")
_EPISODE_WORDS = ("title", "runtime", "fetched_at")


def _words_sql() -> str:
    """Each named title's every service's words, and each named episode row's
    every service's description of it — for the drawn cards alone."""
    title = ", ".join(f"{groups.column(tables.TITLES, source, word, 't')} AS t_{source}_{word}"
                      for source in groups.sources() for word in ("id", *_TITLE_WORDS))
    episode = ", ".join(f"e.{source}_{name} AS e_{source}_{name}" for source in groups.sources()
                        for name in _EPISODE_WORDS if groups.has(tables.EPISODES, source, name))
    return f"""
WITH c AS (SELECT DISTINCT json_extract(value, '$[0]') AS title_key,
                  json_extract(value, '$[1]') AS unit FROM json_each(:rows))
SELECT c.title_key AS title_key, c.unit AS unit, t.media AS t_media, {title}
       {(', ' + episode) if episode else ''}
FROM c LEFT JOIN {tables.TITLES} t ON t.title_key = c.title_key
LEFT JOIN {tables.EPISODES} e ON e.title_key = c.title_key AND e.unit = c.unit"""


def _words(row, source: str) -> dict:
    """The Record fields one service's title group states about the TITLE."""
    def word(name):
        return row[f"t_{source}_{name}"]
    ratings = json.loads(word("ratings_json") or "{}") if word("ratings_json") else {}
    keywords, show_type = title_rows.facts_of({
        "facts_known": word("facts_known"), "keywords_json": word("keywords_json"),
        "show_type": word("show_type")})
    return dict(
        keywords=keywords, show_type=show_type,
        detail_url=str(word("detail_url") or ""), title=str(word("title") or ""),
        # BACK TO A NUMBER WHERE IT IS ONE: `Record.year` is `int | str`, the
        # column is TEXT, and every source hands it over as an int.
        year=title_rows.as_year(word("year")), network=word("network") or "",
        country=word("country") or "", language=word("language") or "",
        status=word("status") or "",
        # Each service's own score under its own name, and IMDb's by name.
        rating=ratings.get(source), imdb_rating=ratings.get("imdb"),
        genres=json.loads(word("genres_json") or "[]") if word("genres_json") else [],
        certification=word("certification") or "", overview=word("overview") or "",
        poster=word("poster") or "", enriched=bool(word("enriched")))


def _listing_record(listing: Listing, row) -> Record:
    """A drawn card's listing as the Record presentation composes it from."""
    source = listing.source
    title_releases = row[f"t_{source}_release_types_json"]
    described = (f"e_{source}_fetched_at" in row.keys()
                 and row[f"e_{source}_fetched_at"] is not None)
    episode_runtime = (row[f"e_{source}_runtime"]
                       if described and f"e_{source}_runtime" in row.keys() else None)
    return Record(
        **_words(row, source),
        source=Source(source), media=Media(listing.media), id=listing.id, ids=listing.ids,
        known_ids=listing.known, air_ts=listing.air_ts, date_only=listing.date_only,
        # THE EPISODE'S OWN RUNTIME WHERE ONE IS KNOWN, the title's otherwise: a
        # double-length finale is not the show's nominal runtime.
        runtime=(episode_runtime if episode_runtime is not None
                 else row[f"t_{source}_runtime"]),
        episode_label=listing.label, episode_mark=listing.mark,
        episode_title=(str(row[f"e_{source}_title"] or "") if described else ""),
        season=listing.season, episode_number=listing.episode_number,
        anime_type=str(row[f"t_{source}_anime_type"] or ""),
        release_types_by_country=(listing.release_types_by_country
                                  if listing.release_types_by_country is not None
                                  else json.loads(title_releases) if title_releases else {}),
        card=f"{listing.title_key}|{listing.unit}")


def _title_only_record(row, source: str, media: str) -> Record:
    """One service's group of a title as a TITLE-ONLY record: everything it says
    about the title, and nothing about an airing — no instant (it is never
    read: the card's day is a listing's), no episode, no release schedule and no
    anime format, which are asked of the listings alone. Its ids are the ones
    its own group states, and what it knows the title by."""
    ids = json.loads(row[f"t_{source}_ids_json"] or "{}") if row[f"t_{source}_ids_json"] else {}
    words = _words(row, source)
    return Record(
        **words, source=Source(source), media=Media(media),
        id=_listing_id(source, ids) or str(row[f"t_{source}_id"] or ""), ids=ids,
        known_ids=dict(ids), air_ts=0.0, date_only=True, runtime=row[f"t_{source}_runtime"])


def _drawn_payload(view: View, viewer: Viewer, orders, describers) -> tuple:
    reads = _reads(viewer.prefs)
    listed = tuple(l.source for l in view.listed)
    cand = tuple(name for name in describers
                 if name not in {l.source for l in view.every}
                 and (reads is None or name in reads))
    return (view.title_key, view.card.unit, _mask(listed), _mask(cand),
            json.dumps(orders(_declared((*listed, *cand)))))


def present_rows(conn: db.Connection, views: Sequence[View], viewer: Viewer) -> list[Record]:
    """Each of `views` composed into the Record its card shows — the drawn
    cards only, read for their own rows."""
    if not views:
        return []
    orders = calendar_resolve.Orders(viewer.prefs)
    describers = calendar_resolve.describing_sources(viewer.settings)
    payloads = [_drawn_payload(view, viewer, orders, describers) for view in views]
    winners = {int(row["k"]): {field: row[f"w_{field}"] for field in _DRAWN_FIELDS}
               for row in conn.execute(_winners_sql(), {"cards": json.dumps(payloads)}).fetchall()}
    words = {(row["title_key"], row["unit"]): row for row in conn.execute(
        _words_sql(), {"rows": json.dumps([[v.title_key, v.card.unit] for v in views])}).fetchall()}
    out = []
    for at, (view, payload) in enumerate(zip(views, payloads)):
        row = words[(view.title_key, view.card.unit)]
        own = [_listing_record(listing, row) for listing in view.listed]
        describing = [_title_only_record(row, name, view.media)
                      for name in describers
                      if payload[3] & _bit_of(name) and row[f"t_{name}_id"] is not None]
        out.append(calendar_resolve.compose(
            own, describing, winners[at], ids=view.card.ids(), orders=orders,
            airing=(view.air_ts, view.date_only),
            coordinate=(view.season, view.episode_number, view.episode_label)))
    return out


# ---------------------------------------------------------------------------
# the read, as its callers ask it
# ---------------------------------------------------------------------------
#
# IN A READ WORKER, every step but the awaiting (app/read_pool.py): a span's rows
# are read, its cards placed and judged, and the drawn ones composed and rendered
# in a process of its own — or on a worker thread where the instance runs no
# pool — so the event loop serves every other request while a month is built,
# reads asked at once run side by side, and only the drawn cards' words are ever
# read. What crosses to a worker is values (`Viewer`, `Held`, dates, `View`s)
# and the functions it runs are this module's own, taking a connection first.

class Held(NamedTuple):
    """Which of a span's listings a read keeps by where they are filed: every
    one but those dated in a window nothing has filled (`gaps`, [first, after)
    each) — what is in one is only what a search or a tracker wrote there, and a
    read that may not fill serves it as the empty span it is. A value, so it
    crosses to a read worker."""
    gaps: tuple[tuple[date, date], ...] = ()

    def __call__(self, day: date) -> bool:
        return not any(first <= day < after for first, after in self.gaps)


def in_day_order(views: list[View], start_date: date, end_date: date) -> list[View]:
    """The cards of `views` placed on a local day of [start_date, end_date], in
    day order.

    KEPT WHEN IT IS PLACED INSIDE THE SPAN, which is a question about the CARD,
    not about any one listing: a card's other services may list it outside the
    span, and a listing inside the span may belong to a card placed outside it
    — which is then drawn there, once, and not here. Sorted on the LOCAL DAY
    first and the instant second. The two agree for anything converted through
    one timezone, but a date-only release is pinned to its own calendar date
    rather than to an offset from an instant — and a day's cards must be
    contiguous, or one day arrives in two pieces."""
    first, last = start_date.isoformat(), end_date.isoformat()
    kept = [view for view in views if first <= view.air_date <= last]
    kept.sort(key=lambda view: (view.air_date, view.air_ts))
    return kept


def placed_span(conn: db.Connection, viewer: Viewer, first: date, after: date, *,
                held: Held, near: tuple[date, date],
                days: tuple[date, date] | None = None) -> Placed:
    """`span`'s answer, read on `conn` — what a read worker runs."""
    listings = [listing for listing in read_rows(conn, viewer.endpoint.key, first=first,
                                                 after=after)
                if held(utc_day(listing))]
    lo, hi = near
    placed = judged(conn, viewer, listings,
                    lambda card: any(lo <= utc_day(l) <= hi for l in card.listings))
    if days is None:
        return placed
    return Placed(in_day_order(placed.views, *days), placed.filtered_out)


def ready() -> None:
    """What a read worker runs as it starts (read_pool.start): nothing, in this
    module — which is the point: unpickling it imports what a read needs."""


async def span(viewer: Viewer, first: date, after: date, *, held: Held,
               near: tuple[date, date], days: tuple[date, date] | None = None) -> Placed:
    """The cards `viewer` sees of the rows with a listing dated in [first,
    after) — those whose every listing `held` keeps — that could be drawn inside
    the UTC days `near` (`views_of`); and with `days`, only those placed on a
    local day of [first, last] of them, in day order (`in_day_order`)."""
    with perftrace.span("query.span") as sp:
        if _answered_in_place(viewer, first, after):
            placed = await db.run(lambda conn: placed_span(
                conn, viewer, first, after, held=held, near=near, days=days))
        else:
            placed = await read_pool.read(placed_span, viewer, first, after, held=held,
                                          near=near, days=days)
        sp.set(cards=len(placed.views))
    return placed


# How many days a span may cover and still go to a read worker however much it
# answers with: a day's read, padded a day each side, and its week.
_ALWAYS_IN_A_WORKER_DAYS = 9


def _answered_in_place(viewer: Viewer, first: date, after: date) -> bool:
    """Whether a span's read is made here rather than in a read worker: a span
    longer than a few days whose viewer judges nothing (no filter of theirs
    set, or theirs paused), whose answer is then the whole calendar. Its cards
    cross to this process pickled, and MEASURED on a copy of dev, October All
    Episodes' 20,277 cost 0.86 s to hand back — more than the read itself; a
    filtered month's 1,470 cost ~60 ms, a day's tens of milliseconds."""
    return ((after - first).days > _ALWAYS_IN_A_WORKER_DAYS
            and not any(value for value in viewer.specs.values()))


async def titles(viewer: Viewer, title_keys: Iterable[str], *,
                 keep: Callable[[Listing], bool] | None = None,
                 narrow: Callable[[list[Card]], list[Card]] | None = None) -> Placed:
    """The cards `viewer` sees of `title_keys`, every stored listing of them
    whole — what the search, its season picker and a mark's redraw ask about.
    `keep` drops listings before the cards are made (a season picker reads one
    season's), `narrow` drops cards before they are judged (the card a jump
    just wrote into)."""
    keys = sorted({str(k) for k in title_keys if k})
    if not keys:
        return Placed([], 0)
    endpoint = viewer.endpoint.key

    # On a worker thread rather than a read worker: `keep` and `narrow` are the
    # caller's own functions, and a title's rows are few.
    def _read(conn) -> Placed:
        listings = read_rows(conn, endpoint, titles=keys)
        if keep is not None:
            listings = [listing for listing in listings if keep(listing)]
        return judged(conn, viewer, listings, None, narrow)
    with perftrace.span("query.titles", titles=len(keys)):
        return await db.run(_read)


# What the judging reads of a title's groups — the columns a described record
# stands in for (`described_kept`).
_JUDGE_COLUMNS = ("id", "network", "country", "certification", "language", "poster",
                  "show_type", "facts_known", "enriched", "anime_type", "genres_json",
                  "keywords_json", "release_types_json")


def _standing_in(source: str) -> str:
    """SQL: a relation shaped like `titles` holding the row of `:key`, with
    `source`'s group replaced by `:words` — a description one service has just
    given, judged where its group would be."""
    columns = []
    for name in groups.sources():
        for column in _JUDGE_COLUMNS:
            if groups.has(tables.TITLES, name, column):
                columns.append(f"json_extract(:words, '$.{column}') AS {name}_{column}"
                               if name == source else f"r.{name}_{column} AS {name}_{column}")
    return (f"(SELECT :key AS title_key, {', '.join(columns)} FROM (SELECT 1) one "
            f"LEFT JOIN {tables.TITLES} r ON r.title_key = :key)")


def _as_group(record: Record) -> dict:
    """What a group would hold of the judged columns had `record`'s service
    filed it — the description's own words (rows.facts_values' spelling)."""
    stated = record.keywords is not None and record.show_type is not None
    return {"id": str(record.id or "") or "described", "network": record.network or "",
            "country": record.country or "", "certification": record.certification or "",
            "language": record.language or "", "poster": record.poster or "",
            "show_type": str(record.show_type or ""), "facts_known": 1 if stated else 0,
            "enriched": 1 if record.enriched else 0, "anime_type": record.anime_type or "",
            "genres_json": json.dumps([str(g) for g in record.genres or ()]),
            "keywords_json": json.dumps([str(k) for k in record.keywords or ()]),
            "release_types_json": json.dumps(record.release_types_by_country or {})}


async def described_kept(viewer: Viewer, record: Record) -> bool:
    """Whether every filter a calendar applies keeps a title a service has just
    described and the calendar does not hold — judged as the one service
    describing it lists it, which is what the card a jump would write is (so
    "only one service lists it" refuses it as the calendar would hide it), with
    what the store's describing services say of the title beside it, by the very
    statement every stored card is judged by. No source admission: the calendar
    would draw another service's record of the title."""
    source = str(record.source)
    identity = resolve_key(record.media, record.ids)
    key = str(identity) if identity is not None else title_identity.per_source_key(
        record.source, record.id)
    describers = calendar_resolve.describing_sources(viewer.settings)
    cand = tuple(name for name in describers if name != source)
    combo = (key, _mask([source]), _mask(cand), json.dumps(_declared((source, *cand))),
             source, _mask([source, *cand]), None)

    def _kept(conn) -> bool:
        verdict = _verdicts(conn, [combo], viewer, titles=_standing_in(source),
                            params={"key": key, "words": json.dumps(_as_group(record))})[0]
        return (verdict.release_ok and not verdict.pruned and verdict.floor_ok
                and verdict.viewer_ok)
    return await db.run(_kept)


def judged(conn: db.Connection, viewer: Viewer, listings: list[Listing], near,
           narrow=None) -> Placed:
    """`listings`' cards placed and judged for `viewer`, on `conn`."""
    if str(viewer.endpoint.media) == str(Media.MOVIE):
        # A TRAKT FILM'S RELEASE SCHEDULE is its own releases read's, kept apart
        # from the title store (calendar_enrich.overlay_releases) — what the
        # release narrowing judges it by, read, never fetched.
        from . import enrich as calendar_enrich
        calendar_enrich.give_releases(conn, listings)
    return judge(conn, views_of(listings, viewer, near=near, narrow=narrow), viewer)


def present_items(conn: db.Connection, viewer: Viewer, views: list[View]) -> list[Item]:
    """`present`'s answer, read on `conn` — what a read worker runs."""
    return [render(record, viewer.tz) for record in present_rows(conn, views, viewer)]


async def present(viewer: Viewer, views: Sequence[View]) -> list[Item]:
    """`views` as the Items their cards draw, in the same order."""
    views = list(views)
    if not views:
        return []
    with perftrace.span("query.present", cards=len(views)):
        return await read_pool.read(present_items, viewer, views)
