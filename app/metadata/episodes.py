"""An episode row's LISTINGS: each service's instants, which row a listing is
filed on, and the fold that decides it.

ONE ROW PER EPISODE (app/metadata/tables.py's EPISODES), keyed by its title and
its `unit`: 'S{season}E{number}' for a numbered episode, 'film' for a film's
release, and 'D{YYYY-MM-DD}' with whichever half of the coordinate it stated
for a listing no episode could take. WHICH LISTINGS ARE ONE EPISODE IS THE
ROW'S KEY, decided as a listing is written — the calendar's read used to decide
it on every page — so any number of services' listings of one episode, however
far apart they date it, are one row, and each viewer sees it on their own first
service's day.

A SERVICE'S LISTING IS ITS INSTANTS. Its earliest is the listing's own columns
(`{source}_air_ts` and the rest), every further one an entry of
`{source}_repeats_json` — a service listing one episode twice keeps both on the
one row, each instant with its own calendars (`calendars`, one bit per
Endpoint), the calendars a SEARCH put it on rather than the feed (`searched`),
which half of the coordinate its listing did not state (`unstated`), the label
it spelled, when it was last stored (`listed_at`, what retention reads) and, for
a service with more than one entry for the title, which entry listed it where
that is not the title's group (`entry`).

A LISTING THAT NAMES NO FULL EPISODE is folded onto a numbered one when that is
safe (`Book.refold`, the rule the read used to apply) and RE-DECIDED BOTH WAYS
whenever a write touches its title and day: a numbered listing arriving takes a
day row's listing in, and one leaving lets it go back. Its `unstated` bits are
what let it leave.
"""
from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from .. import db
from ..providers.base import epoch_moment
from . import groups, tables

# AN INSTANT'S `unstated` BITS: the halves of the episode's coordinate its
# listing left out (app/db.py's MIGRATION_49 writes the same bits).
UNSTATED_SEASON = 1
UNSTATED_NUMBER = 2

FILM = "film"


def utc_day(at: float) -> str:
    """The UTC calendar day of an instant, through the shared conversion so it
    agrees with every other reading of an air time (base.epoch_moment)."""
    return epoch_moment(at).strftime("%Y-%m-%d")


def unstated_of(season: int | None, number: int | None) -> int:
    """The `unstated` bits of a show listing stating `season` and `number`."""
    return ((UNSTATED_SEASON if season is None else 0)
            | (UNSTATED_NUMBER if number is None else 0))


def day_unit(day: str, season: int | None, number: int | None) -> str:
    """The unit of a listing no numbered episode could take: its day and what it
    stated — the number is part of it, so a show listed as episodes 1 and 2 on
    one day is two rows, as it was two cards."""
    return ("D" + day + (f"S{season}" if season is not None else "")
            + (f"E{number}" if number is not None else ""))


def numbered(season: int, number: int) -> str:
    return f"S{int(season)}E{int(number)}"


@dataclass
class Instant:
    """One moment a service lists an episode at, and what about it."""
    at: float
    date_only: int = 0
    calendars: int = 0
    searched: int = 0
    unstated: int = 0
    label: str = ""
    listed_at: int = 0
    entry: str | None = None

    @property
    def day(self) -> str:
        return utc_day(self.at)

    @property
    def fed(self) -> int:
        """The calendars a feed lists this instant on (not a search)."""
        return self.calendars & ~self.searched

    def same(self, other: "Instant") -> bool:
        """Whether two instants are one: one moment, stated the same way."""
        return float(self.at) == float(other.at) and self.unstated == other.unstated

    def absorb(self, other: "Instant") -> None:
        """`other` — the same instant — joined into this one: its calendars
        added, a search's mark standing down wherever a feed lists it, the
        earlier label kept, the newer store's clock, and the title's own entry
        if either was it."""
        fed = self.fed | other.fed
        self.calendars |= other.calendars
        self.searched = (self.searched | other.searched) & ~fed
        self.label = self.label or other.label
        self.listed_at = max(int(self.listed_at or 0), int(other.listed_at or 0))
        if self.entry is None or other.entry is None:
            self.entry = None
        else:
            self.entry = min(self.entry, other.entry, key=_id_order)


def _id_order(own: str) -> tuple:
    return (len(own), own) if own.isdigit() else (float("inf"), own)


def _compared(instants: Iterable[Instant]) -> list[tuple]:
    """A service's instants as `Row.differs` compares them: what a write spells
    of each (`listing_columns`), the moment it was stored only where a search
    put it on a calendar."""
    return sorted((float(i.at), int(i.date_only), int(i.calendars), int(i.searched),
                   int(i.unstated), str(i.label or ""), str(i.entry or ""),
                   int(i.listed_at or 0) if i.searched else -1)
                  for i in instants if i.calendars)


def instants_of(row: Mapping[str, Any], source: str) -> list[Instant]:
    """`source`'s instants on a stored episode row, earliest first — none where
    it does not list the episode."""
    if not groups.has(tables.EPISODES, source, "air_ts") or row[f"{source}_air_ts"] is None:
        return []
    entry = row[f"{source}_entry"] if groups.has(tables.EPISODES, source, "entry") else None
    out = [Instant(float(row[f"{source}_air_ts"]), int(row[f"{source}_date_only"] or 0),
                   int(row[f"{source}_calendars"] or 0), int(row[f"{source}_searched"] or 0),
                   int(row[f"{source}_unstated"] or 0), str(row[f"{source}_label"] or ""),
                   int(row[f"{source}_listed_at"] or 0), entry)]
    repeats = row[f"{source}_repeats_json"]
    # Most rows hold no repeat, and a month's read parses tens of thousands.
    for repeat in (groups.loads(repeats, []) if repeats and repeats != "[]" else ()):
        out.append(Instant(float(repeat["at"]), int(repeat.get("date_only") or 0),
                           int(repeat.get("calendars") or 0), int(repeat.get("searched") or 0),
                           int(repeat.get("unstated") or 0), str(repeat.get("label") or ""),
                           int(repeat.get("listed_at") or 0), repeat.get("entry")))
    return out


def listing_columns(source: str, instants: Iterable[Instant]) -> dict[str, Any]:
    """`source`'s listing columns for a row holding `instants`: the earliest is
    the listing, every later one a repeat in time order — spelled exactly as the
    store's conversion spelled them (app/db.py's MIGRATION_49), "unstated" in a
    repeat only where it left something out and "entry" only for a service with
    entries. Every column's empty value where it lists nothing."""
    names = [name for name in groups.LISTING if groups.has(tables.EPISODES, source, name)]
    held = sorted((i for i in instants if i.calendars), key=lambda i: (i.at, i.unstated))
    if not held:
        return {name: groups.EMPTY[name] for name in names}
    has_entry = "entry" in names
    repeats = []
    for instant in held[1:]:
        spelled = {"at": instant.at, "date_only": instant.date_only,
                   "calendars": instant.calendars, "searched": instant.searched}
        if instant.unstated:
            spelled["unstated"] = instant.unstated
        spelled.update(label=instant.label, listed_at=instant.listed_at)
        if has_entry:
            spelled["entry"] = instant.entry
        repeats.append(spelled)
    first = held[0]
    out = {"air_ts": first.at, "date_only": first.date_only, "calendars": first.calendars,
           "searched": first.searched, "unstated": first.unstated, "label": first.label,
           "listed_at": first.listed_at,
           "repeats_json": json.dumps(repeats) if repeats else "[]", "entry": first.entry}
    return {name: out[name] for name in names}


def columns() -> tuple[str, ...]:
    """Every column of an episode row, in a fixed order: which row it is, then
    each service's group."""
    return (*groups.IDENTITY[tables.EPISODES],
            *(f"{source}_{name}" for source in groups.sources()
              for name in groups.every(tables.EPISODES, source)))


class Row:
    """One episode row as a writer holds it: which row it is, each service's
    instants, and every other column as it was read."""
    __slots__ = ("key", "unit", "media", "season", "number", "instants", "stored", "changed")

    def __init__(self, key: str, unit: str, media: str, season: int | None,
                 number: int | None, stored: Mapping[str, Any] | None = None):
        self.key, self.unit, self.media = str(key), str(unit), str(media)
        self.season, self.number = season, number
        self.instants: dict[str, list[Instant]] = (
            {source: instants_of(stored, source) for source in groups.sources()}
            if stored is not None else {source: [] for source in groups.sources()})
        # The row as read, for a write that puts the whole row back — kept as
        # read rather than copied: a month's read holds tens of thousands.
        self.stored = stored
        self.changed = False

    @property
    def described(self) -> bool:
        """Whether a service DESCRIBES this episode — a row that holds a
        description stays when nothing lists it. Asked only of a row a writer
        read whole."""
        if self.stored is None:
            return False
        names = self.stored.keys()
        return any(self.stored[f"{source}_fetched_at"] is not None
                   for source in groups.sources() if f"{source}_fetched_at" in names)

    @classmethod
    def read(cls, stored: Mapping[str, Any]) -> "Row":
        return cls(stored["title_key"], stored["unit"], stored["media"], stored["season"],
                   stored["number"], stored)

    @property
    def coordinated(self) -> bool:
        return self.unit.startswith("S")

    @property
    def listed(self) -> bool:
        return any(i.calendars for held in self.instants.values() for i in held)

    def differs(self) -> bool:
        """Whether writing this row would change its listings: a row not stored
        yet, or one whose instants are other than those read — compared as they
        are written, less the moment each was last stored. A feed's listing a
        refill brings back as it was is NOT rewritten to say so: its window's
        coverage row is what confirms it (app/calendar/entries.py's `sweep`). A
        SEARCHED instant's moment is compared, because its own age is its clock."""
        if self.stored is None:
            return True
        return any(_compared(instants_of(self.stored, source)) != _compared(self.instants[source])
                   for source in groups.sources())

    def stated(self, instant: Instant) -> tuple[int | None, int | None]:
        """The coordinate an instant's listing stated, as the row and its
        `unstated` bits give it back — None for a half it left out."""
        return (None if instant.unstated & UNSTATED_SEASON else self.season,
                None if instant.unstated & UNSTATED_NUMBER else self.number)

    def add(self, source: str, instant: Instant) -> None:
        """`instant` listed by `source` on this row — joined into the same
        instant where the service already lists it."""
        for held in self.instants[source]:
            if held.same(instant) and (held.entry == instant.entry):
                held.absorb(instant)
                self.changed = True
                return
        for held in self.instants[source]:
            if held.same(instant):
                held.absorb(instant)
                self.changed = True
                return
        self.instants[source].append(replace(instant))
        self.changed = True

    def take(self, source: str, instant: Instant, bits: int) -> None:
        """`bits` taken off one of `source`'s instants, the instant gone when it
        is on no calendar any more."""
        if not instant.calendars & bits:
            return
        instant.calendars &= ~bits
        instant.searched &= ~bits
        if not instant.calendars:
            self.instants[source].remove(instant)
        self.changed = True


class Book:
    """The episode rows a write touches, as it holds them while it decides —
    read up front, changed in memory, and written in a fixed number of
    statements (`write`)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Row] = {}
        self._by_title: dict[str, set[str]] = {}

    def hold(self, stored: Iterable[Mapping[str, Any]]) -> None:
        for row in stored:
            if (row["title_key"], row["unit"]) not in self.rows:
                self._keep(Row.read(row))

    def _keep(self, row: Row) -> Row:
        self.rows[(row.key, row.unit)] = row
        self._by_title.setdefault(row.key, set()).add(row.unit)
        return row

    def of_title(self, key: str) -> list[Row]:
        return [self.rows[(key, unit)] for unit in sorted(self._by_title.get(key, ()))]

    def row(self, key: str, unit: str, media: str, season: int | None,
            number: int | None) -> Row:
        found = self.rows.get((key, unit))
        if found is None:
            found = self._keep(Row(key, unit, media, season, number))
            found.changed = True
        return found

    def place(self, key: str, media: str, source: str, season: int | None, number: int | None,
              instant: Instant) -> Row:
        """A listing's instant filed on the row its coordinate names — a film's
        release, a numbered episode — or, for one that names no full episode,
        on its own day's row; `refold` then decides whether that one joins a
        numbered episode."""
        if media == "movie":
            row = self.row(key, FILM, media, None, None)
            instant = replace(instant, unstated=0)
        elif season is not None and number is not None:
            row = self.row(key, numbered(season, number), media, season, number)
            instant = replace(instant, unstated=0)
        else:
            row = self.row(key, day_unit(instant.day, season, number), media, season, number)
            instant = replace(instant, unstated=unstated_of(season, number))
        row.add(source, instant)
        return row

    def days(self, key: str, mask: int) -> set[tuple[int, str]]:
        """Every (calendar bit, UTC day), among the calendars in `mask`, that a
        listing of the title naming no full episode sits on — what `refold` is
        asked about after a write."""
        out = set()
        for row in self.of_title(key):
            for held in row.instants.values():
                for instant in held:
                    if instant.unstated:
                        for bit in bits(instant.calendars & mask):
                            out.add((bit, instant.day))
        return out

    def refold(self, key: str, bit: int, day: str) -> None:
        """Re-decide, on one calendar and one UTC day, which row each of the
        title's listings that names no full episode belongs on — the rule the
        calendar's read applied (and the store's conversion, app/db.py's
        `_m49_fold`) made at write:

          - it joins a numbered episode of the same title only when another
            listing of that episode is on the same calendar on the same day and
            agrees with whichever half the listing did state, EXACTLY ONE episode
            qualifies — counted before anything narrows, so a day listing season
            4 and season 5 of one show never quietly takes the wrong one — and
            its own service has no listing of that episode there yet (folding a
            service's listing into its own would collapse it rather than
            reconcile two services);
          - otherwise it is a row of its own, its day and what it stated.

        In time order, then the declared order, so a listing's turn depends only
        on what was listed before it. Every row of the title on that day must be
        held."""
        loose: list[tuple[str, Instant, int | None, int | None, str]] = []
        on_day: dict[tuple[int, int], set[str]] = {}
        listed_by: dict[tuple[int, int], set[str]] = {}
        media = ""
        for row in self.of_title(key):
            media = media or row.media
            for source, held in row.instants.items():
                for instant in list(held):
                    if not instant.calendars & bit:
                        continue
                    if instant.unstated and instant.day == day:
                        season, number = row.stated(instant)
                        loose.append((source, replace(instant, calendars=bit,
                                                      searched=instant.searched & bit),
                                      season, number, instant.entry or ""))
                        row.take(source, instant, bit)
                    elif row.coordinated and not instant.unstated:
                        listed_by.setdefault((row.season, row.number), set()).add(source)
                        if instant.day == day:
                            on_day.setdefault((row.season, row.number), set()).add(source)
                    elif row.coordinated:
                        listed_by.setdefault((row.season, row.number), set()).add(source)
        order = {source: at for at, source in enumerate(groups.sources())}
        loose.sort(key=lambda item: (item[1].at, order.get(item[0], len(order)), item[4],
                                     -1 if item[3] is None else item[3],
                                     -1 if item[2] is None else item[2]))
        for source, instant, season, number, _entry in loose:
            agreeing = {episode for episode in on_day
                        if (number is None or episode[1] == number)
                        and (season is None or episode[0] == season)}
            if len(agreeing) == 1:
                (episode,) = agreeing
                services = listed_by.setdefault(episode, set())
                if source not in services:
                    services.add(source)
                    self.row(key, numbered(*episode), media, *episode).add(source, instant)
                    continue
            self.row(key, day_unit(day, season, number), media, season, number).add(source, instant)

    def spell(self) -> tuple[str, str] | None:
        """What `write` writes — every row this book changed, as two JSON
        arrays: each one's listings (all services', as held), and every row
        nothing lists or describes any more — or None for no change. Pure, so a
        caller spells it before its transaction and the lock is held for the
        statements alone."""
        changed = [row for row in self.rows.values() if row.changed and row.differs()]
        if not changed:
            return None
        kept = [row for row in changed if row.listed or row.described]
        # A row this book made and then emptied — a listing placed on its own
        # day's row and folded onto a numbered episode — was never written.
        gone = [[row.key, row.unit] for row in changed
                if row.stored is not None and not (row.listed or row.described)]
        if not kept and not gone:
            return None
        return (json.dumps([[row.key, row.unit, row.media, row.season, row.number,
                             *(value for source in groups.sources()
                               for value in listing_columns(source, row.instants[source]).values())]
                            for row in kept]),
                json.dumps(gone))

    def write(self, conn: db.Connection, spelled: tuple[str, str] | None) -> None:
        """Every row this book changed, written inside the caller's transaction
        in two statements — both issued whenever anything changed, so the count
        does not depend on what the write happened to empty.

        `spelled` IS THIS BOOK'S `spell`, MADE BEFORE THE LOCK, AND None IS ITS
        ANSWER "NOTHING CHANGED" — never "not spelled yet". Spelling here when
        handed None spelled every row of an unchanged refill a second time under
        the lock, to find again that nothing changed: MEASURED on a copy of dev, a
        Simkl month stored back unchanged held the lock 0.21–0.34 s doing it,
        where its coverage write alone takes a few milliseconds."""
        if spelled is None:
            return
        kept, gone = spelled
        conn.execute(_upsert_listings_sql(), {"rows": kept})
        conn.execute(f"DELETE FROM {tables.EPISODES} WHERE (title_key, unit) IN "
                     "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
                     "FROM json_each(?))", (gone,))


def bits(mask: int) -> list[int]:
    """Each calendar bit set in `mask`."""
    return [1 << at for at in range(mask.bit_length()) if mask >> at & 1]


def _listing_names() -> list[str]:
    return [f"{source}_{name}" for source in groups.sources()
            for name in groups.LISTING if groups.has(tables.EPISODES, source, name)]


def _upsert_listings_sql() -> str:
    names = _listing_names()
    selected = ", ".join(f"json_extract(r.value, '$[{at}]')" for at in range(5 + len(names)))
    return (f"INSERT INTO {tables.EPISODES} (title_key, unit, media, season, number, "
            + ", ".join(names) + f")\nSELECT {selected} FROM json_each(:rows) r WHERE true "
            "ORDER BY r.key\nON CONFLICT(title_key, unit) DO UPDATE SET "
            + ", ".join(f"{name} = excluded.{name}" for name in names))


# ---------------------------------------------------------------------------
# reading the rows a write touches
# ---------------------------------------------------------------------------

def epoch(day: date) -> float:
    """A UTC day's first second, as an air time compares to it."""
    return (day - date(1970, 1, 1)).days * 86400.0


def span_rowids(*, titles: bool = False) -> str:
    """The rowids of the episode rows on which some service lists an instant
    inside [:lo, :hi) — the rows a span's reader or writer works on, and some
    more: each service's first instants through its own air-time index, and
    every row holding repeats through theirs (a few hundred), so which instant
    is inside the span, and on which calendar, is the caller's to test.
    `titles`: only the rows of the titles :titles (a JSON array) names."""
    of_titles = " AND title_key IN (SELECT value FROM json_each(:titles))" if titles else ""
    arms = []
    for source in groups.sources():
        if not groups.has(tables.EPISODES, source, "air_ts"):
            continue
        arms.append(f"SELECT rowid FROM {tables.EPISODES} WHERE {source}_calendars != 0 "
                    f"AND {source}_calendars & :bit "
                    f"AND {source}_air_ts >= :lo AND {source}_air_ts < :hi{of_titles}")
        arms.append(f"SELECT rowid FROM {tables.EPISODES} WHERE {source}_repeats_json != '[]'"
                    f"{of_titles}")
    return " UNION ".join(arms)


def span_sql(*, select: str = "e.*", titles: bool = False) -> str:
    """`span_rowids`' rows themselves."""
    return (f"SELECT {select} FROM {tables.EPISODES} e "
            f"WHERE e.rowid IN ({span_rowids(titles=titles)})")


def listing_select(alias: str = "e") -> str:
    """The columns of an episode row that say which row it is and every
    service's listing of it — what a reader of listings selects, rather than
    every description beside them."""
    return ", ".join([*(f"{alias}.{name}" for name in groups.IDENTITY[tables.EPISODES]),
                      *(f"{alias}.{source}_{name}" for source in groups.sources()
                        for name in groups.LISTING if groups.has(tables.EPISODES, source, name))])


def in_span(row: Row, bit: int, lo: float, hi: float) -> bool:
    """Whether any service lists an instant of `row` on calendar `bit` inside
    [lo, hi)."""
    return any(instant.calendars & bit and lo <= instant.at < hi
               for held in row.instants.values() for instant in held)


def _whole_rows() -> list[str]:
    """Every column of an episode row, in a fixed order."""
    return [*groups.IDENTITY[tables.EPISODES],
            *(f"{source}_{name}" for source in groups.sources()
              for name in groups.every(tables.EPISODES, source))]


def _aggregated(conn: db.Connection, source_sql: str, params) -> list[dict]:
    """The episode rows `source_sql` (a FROM … clause over `e`) finds, read in
    ONE aggregated row. These are the reads a write makes INSIDE its lock (a
    merge, the titles a merge reshaped): the driver lets go of the GIL around
    every statement step and has to win it back — up to a switch interval each
    time while another thread is busy — so a row per episode cost a switch per
    episode, and one row costs one."""
    names = _whole_rows()
    (found,) = conn.execute(
        f"SELECT json_group_array(json_array({', '.join(f'e.{n}' for n in names)})) "
        f"{source_sql}", params).fetchone()
    return [dict(zip(names, values)) for values in json.loads(found)]


def read_span(conn: db.Connection, bit: int, lo: float, hi: float, *,
              titles: Collection[str] | None = None) -> list:
    """The stored rows with an instant on calendar `bit` inside [lo, hi) — of
    the `titles` named alone, when they are, read in one aggregated row."""
    params = {"lo": lo, "hi": hi, "bit": bit}
    if titles is None:
        found = conn.execute(span_sql(), params).fetchall()
    else:
        params["titles"] = json.dumps(sorted(titles))
        found = _aggregated(conn, f"FROM {tables.EPISODES} e WHERE e.rowid IN "
                                  f"({span_rowids(titles=True)})", params)
    return [row for row in found if in_span(Row.read(row), bit, lo, hi)]


def read_units(conn: db.Connection, refs: Iterable[tuple[str, str]]) -> list:
    """The stored rows named by (title key, unit)."""
    refs = [list(ref) for ref in dict.fromkeys(refs)]
    if not refs:
        return []
    return _aggregated(
        conn, f"FROM json_each(?) j CROSS JOIN {tables.EPISODES} e "
        "ON e.title_key = json_extract(j.value, '$[0]') AND e.unit = json_extract(j.value, '$[1]')",
        (json.dumps(refs),))


def read_where(conn: db.Connection, source_sql: str, params) -> list:
    """The stored rows `source_sql` (a FROM … clause over `e`) finds, whole, in
    one aggregated row (`_aggregated` says why) — for a caller whose question
    about the rows is its own (the calendar's retention sweep)."""
    return _aggregated(conn, source_sql, params)


def read_titles(conn: db.Connection, keys: Iterable[str]) -> list:
    """Every stored episode row of the titles named."""
    keys = sorted({str(key) for key in keys})
    if not keys:
        return []
    return _aggregated(conn, f"FROM json_each(?) j CROSS JOIN {tables.EPISODES} e "
                             "ON e.title_key = j.value", (json.dumps(keys),))


# ---------------------------------------------------------------------------
# a calendar's listings, written
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    """One listing a write files: the title key it is filed under, who lists
    it, what it states, the instant — its calendar's bit on it, and that bit as
    `searched` too for a listing a search wrote — and, for a numbered episode,
    the name the feed gave it."""
    key: str
    source: str
    media: str
    season: int | None
    number: int | None
    instant: Instant
    episode_title: str = ""
    # The service's own id for the title the listing's entry is (rows.own_id).
    own: str = ""


@dataclass
class Fill:
    """What filing a calendar's listings decided (`plan_fill`), for `apply`."""
    book: Book
    # [title key, unit, media, season, number, episode title] per service, for
    # each numbered episode the title's own entry lists: the episode's name as
    # the feed spelled it, until a description names it — a JSON array per
    # service once the plan is made.
    stubs: dict[str, Any] = field(default_factory=dict)
    # Each listing, whether it was written (a search's gap that its feed
    # already lists is not).
    written: list[bool] = field(default_factory=list)
    # The book's rows as `apply` writes them (Book.spell), spelled with the plan
    # so the write lock is held for the statements alone.
    spelled: tuple[str, str] | None = None

    @property
    def empty(self) -> bool:
        """Whether `apply` would write nothing: no row's listings and no
        episode's feed name would change."""
        return self.spelled is None and not self.stubs


def plan_fill(conn: db.Connection, listings: list[Listing], *, bit: int, primaries:
              Mapping[tuple[str, str], str], span: tuple[date, date] | None = None,
              answered: Iterable[str] = (), replied: Iterable[str] = (),
              titles: Collection[str] | None = None,
              excluding: Collection[str] = ()) -> Fill:
    """Decide what filing `listings` on calendar `bit` writes — reads only, for
    db.transaction's `prepare` — as the calendar's fill always wrote it:

      - a FEED's answer over `span` replaces what the services that `answered`
        listed in it before: their instants inside the span leave that
        calendar (a title a source has stopped listing stops being drawn) — and
        a service that answered "unchanged" (`replied` without `answered`)
        keeps its own;
      - A SEARCHED LISTING STANDS DOWN FOR ITS FEED: an instant a search wrote
        of an episode is taken off the calendar wherever its service's feed
        lists that episode there, at whatever instant — by the feed's answer,
        and over the span for every service that `replied`;
      - with no `span` — a search's own write — a listing whose service's feed
        already lists the episode on the calendar is not written at all;
      - every listing is filed on its episode's row, and each title and day it
        touches is re-folded (`Book.refold`).

    `primaries` names each (title, service)'s entry, so an instant of another
    entry carries its own id (`entry`) and the episode's feed name is kept only
    from the title's own entry.

    ONE FILL CAN BE DECIDED IN TWO HALVES, BY TITLE: `excluding` leaves the
    named titles' rows and listings out, and `titles` decides those titles
    alone, from the same rows of theirs — so a fill whose titles a merge
    reshapes decides the rest before the write lock and only those few inside
    it, once the merge is written (app/calendar/entries.py's `store_span`). A
    listing left out is counted as written: the other half writes it."""
    book = Book()
    lo = hi = None
    if span is not None:
        lo, hi = epoch(span[0]), epoch(span[1])
    excluding = frozenset(excluding)

    def ours(key: str) -> bool:
        return (titles is None or key in titles) and key not in excluding

    if span is not None:
        # A FEED'S FILL READS ITS SPAN — every row a service lists an instant of
        # inside it, which holds every title's listings on every day the fill
        # touches — and the rows its listings land on, wherever else those are
        # dated. A half restricted to a few titles reads their share of it.
        book.hold(row for row in read_span(conn, bit, lo, hi, titles=titles)
                  if ours(row["title_key"]))
        book.hold(read_units(conn, {(listing.key, unit_for(listing)) for listing in listings
                                    if ours(listing.key)}))
    elif titles is not None:
        book.hold(read_titles(conn, titles))
    else:
        # A SEARCH'S WRITE names a title or two, and asks whether a feed lists
        # each episode at any date: their rows whole.
        book.hold(read_titles(conn, {listing.key for listing in listings}))
    touched: set[tuple[str, int, str]] = set()

    def entry_of(listing: Listing) -> str | None:
        return None if primaries.get((listing.key, listing.source)) == listing.own else listing.own

    # What the answering services listed in the span, gone from this calendar.
    answered = set(map(str, answered))
    if span is not None and answered:
        for row in list(book.rows.values()):
            for source in answered:
                for instant in list(row.instants.get(source, ())):
                    if instant.fed & bit and lo <= instant.at < hi:
                        touched.add((row.key, bit, instant.day))
                        row.take(source, instant, bit & ~instant.searched)
    # A search's listing of an episode the feed lists, stood down.
    feeds = {(listing.key, listing.source, listing.season, listing.number, entry_of(listing))
             for listing in listings if not listing.instant.searched & bit and ours(listing.key)}

    def fed_somewhere(row: Row, source: str, instant: Instant) -> bool:
        stated = row.stated(instant)
        return any(other is not instant and other.fed & bit and other.entry == instant.entry
                   and row.stated(other) == stated
                   for other in row.instants[source])

    replied = set(map(str, replied))
    for row in list(book.rows.values()):
        for source, held in row.instants.items():
            for instant in list(held):
                if not instant.searched & bit:
                    continue
                stated = row.stated(instant)
                if ((row.key, source, *stated, instant.entry) in feeds
                        or (source in replied and fed_somewhere(row, source, instant)
                            and lo is not None and lo <= instant.at < hi)):
                    touched.add((row.key, bit, instant.day))
                    row.take(source, instant, bit)

    fill = Fill(book)
    for listing in listings:
        if not ours(listing.key):
            fill.written.append(True)
            continue
        entry = entry_of(listing)
        instant = replace(listing.instant, entry=entry)
        if instant.searched & bit and _feed_lists(book, listing, entry, bit):
            fill.written.append(False)
            continue
        fill.written.append(True)
        row = book.place(listing.key, listing.media, listing.source, listing.season,
                         listing.number, instant)
        touched.add((listing.key, bit, instant.day))
        if entry is None and row.coordinated and listing.season is not None \
                and listing.number is not None:
            fill.stubs.setdefault(listing.source, []).append(
                [row.key, row.unit, row.media, row.season, row.number,
                 str(listing.episode_title or "")])
    # RE-FOLDED ONLY WHERE EVERY ROW OF THE DAY IS HELD: a fill holds its span
    # whole, a search its titles whole. A searched listing a feed's answer stood
    # down outside the span is a numbered one, whose leaving folds nothing.
    for key, touched_bit, day in sorted(touched):
        if lo is None or lo <= epoch(date.fromisoformat(day)) < hi:
            book.refold(key, touched_bit, day)
    # ONLY WHAT WOULD CHANGE IS WRITTEN: a row whose listings come back as they
    # were (`Row.differs`), and a stub whose name is already its row's.
    fill.spelled = book.spell()
    fill.stubs = {source: json.dumps(kept)
                  for source, kept in _changing_stubs(conn, fill.stubs).items()}
    return fill


def unit_for(listing: "Listing") -> str:
    """The row a listing's coordinate names before any fold: its film, its
    numbered episode, or its own day's row."""
    if listing.media == "movie":
        return FILM
    if listing.season is not None and listing.number is not None:
        return numbered(listing.season, listing.number)
    return day_unit(listing.instant.day, listing.season, listing.number)


def _feed_lists(book: Book, listing: Listing, entry: str | None, bit: int) -> bool:
    """Whether the listing's service's FEED already lists its episode on the
    calendar, at any instant — the gap a search fills is not there."""
    stated = (listing.season, listing.number) if listing.media != "movie" else (None, None)
    for row in book.of_title(listing.key):
        for instant in row.instants.get(listing.source, ()):
            if instant.fed & bit and instant.entry == entry and \
                    (row.stated(instant) if listing.media != "movie" else (None, None)) == stated:
                return True
    return False


_STUB_WRITES = {
    # ONCE A LOOKUP HAS ANSWERED, IT OWNS THE NAME. Both writers reach this
    # group: a listing, carrying whatever the calendar feed spelled, and a
    # description, carrying a whole season read from the service's own episode
    # list. The second knows more, so letting the feed overwrite it on the next
    # refill would trade a real answer for a thinner one, once a day, for ever.
    # Before a lookup has answered (`fetched_at` 0) the feed's name is the only
    # one there is and is kept.
    "title": ("CASE WHEN held.fetched_at > 0 THEN held.title "
              "WHEN given.title != '' THEN given.title ELSE held.title END"),
    # NOT MOVED ONCE SET: A LISTING MAY NOT DEMOTE WHAT A LOOKUP LEARNED. The
    # stub's 0 means "still owed a lookup", so writing it over an answered group
    # would push it back to owed on every refill.
    "fetched_at": "COALESCE(held.fetched_at, 0)",
}


def _stub_columns(source: str) -> tuple[str, ...]:
    return ("title_key", "unit", "media", "season", "number", f"{source}_title",
            f"{source}_fetched_at")


def _stub_given(source: str, rows: str = ":rows") -> str:
    """The stubs a write reads from `rows` — [title key, unit, media, season,
    number, the feed's name] each — named as the table names them, `at` each
    one's position."""
    return ("SELECT r.key AS at, " + ", ".join(
        f"json_extract(r.value, '$[{i}]') AS {name}"
        for i, name in enumerate(_stub_columns(source)[:-1]))
        + f", 0 AS {source}_fetched_at FROM json_each({rows}) r")


def _stub_sql(source: str) -> str:
    columns = ", ".join(_stub_columns(source))
    return (f"INSERT INTO {tables.EPISODES} ({columns})\n"
            f"SELECT {columns} FROM ({_stub_given(source)}) WHERE true ORDER BY at\n"
            "ON CONFLICT(title_key, unit) DO UPDATE SET "
            + ", ".join(f"{source}_{name} = {groups.spelled(rule, tables.EPISODES, source)}"
                        for name, rule in _STUB_WRITES.items()))


def _changing_stubs(conn: db.Connection, stubs: Mapping[str, list[list]]
                    ) -> dict[str, list[list]]:
    """{service: the stubs whose write would change their row} (groups.changing
    over `_STUB_WRITES`): a row with none of the service's, or a feed's name a
    description has not replaced and that differs — every service's in ONE
    statement, whichever have any, so a fill's count does not depend on them."""
    names = [source for source in groups.sources()
             if groups.has(tables.EPISODES, source, "title")]
    if not any(stubs.get(source) for source in names):
        return {}
    arms = " UNION ALL ".join(
        f"SELECT '{source}' AS source, at FROM ("
        + groups.changing_sql(tables.EPISODES, ("title_key", "unit"),
                              _stub_given(source, f":rows_{source}"), _STUB_WRITES.items(), source)
        + ")" for source in names)
    (found,) = conn.execute(f"SELECT json_group_array(json_array(source, at)) FROM ({arms})",
                            {f"rows_{source}": json.dumps(stubs.get(source, []))
                             for source in names}).fetchone()
    moving = {(str(source), int(at)) for source, at in json.loads(found)}
    return {source: kept for source in names
            if (kept := [stub for at, stub in enumerate(stubs.get(source, []))
                         if (source, at) in moving])}


def apply(conn: db.Connection, fill: Fill) -> None:
    """Write what `plan_fill` decided, inside the caller's transaction: the
    rows' listings, the rows nothing holds any more, and each numbered
    episode's feed name — a statement each, and one per service with names."""
    fill.book.write(conn, fill.spelled)
    for source, stubs in fill.stubs.items():
        conn.execute(_stub_sql(source), {"rows": stubs})
