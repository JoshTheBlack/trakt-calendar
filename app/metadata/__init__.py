"""The title store: what every service says about a title, the title's identity,
and the one way a title gets in.

ONE STORE OF TITLE FACTS. The same title used to be described in up to five
places — the calendar's per-service rows, the tracker's rows, a board's rows, the
film watch state, the response cache — each learning ids its own way, so a board
title found through TMDB held a tmdb id and nothing else while the calendar's rows
for it knew every service's. This package owns the rows that describe titles
(rows.py), the identity that says which title a set of ids is (identity.py), and
every column elsewhere that holds a title's key (holders.py).

ONE WAY IN. Every path that meets a title registers it here:
  - `plan_records` then `write_records` for a SERVICE'S OWN LISTINGS (a calendar
    fill, an airing a search wrote) — the reads and the deciding before the
    caller's write lock, the writes inside it: each record's title is resolved,
    its service's group written under that title's key, and no service is asked
    — a busy fill names hundreds of titles against a 10-a-second ceiling. The
    listing itself is filed on its episode's row (episodes.py) in the same
    transaction;
  - `ensure` / `ensure_many` for a title met BY ITS IDS (a search pick, a tracker
    arrival, a board add, an import, a restore). Handed a viewer's `settings`,
    they also ask each service the title has no id from, ONCE, by shared id —
    the one place in the app that does — and keep what an exact match answers.
    Without `settings` nothing is asked: the bulk paths register and move on.
A title registered here is resolved by one rule (identity.settle), so the key a
calendar card draws, a tracker row is filed under and a board row is held by are
the same key — and when a title learns an id that moves its key, every one of
them moves together.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from .. import db, providers
from ..providers.base import ItemKey, Media, Record, collect_ids
from . import identity
from . import ids as id_rules
from . import rows

logger = logging.getLogger(__name__)

# The identity a caller gets back. Named here so a feature reads one name for it.
Identity = identity.Identity


class Listings(NamedTuple):
    """Services' listings as the store will write them — everything that does not
    depend on which title each turns out to be, worked out by `listings` BEFORE
    the caller's transaction, so the write lock is held for statements and not
    for the Python that spells them."""
    records: list[Record]
    # One per service title, however many of its airings arrive — which is most
    # of what a fill is: ONE RESOLUTION PER SERVICE TITLE, from its first record.
    titles: list[identity.Settling]
    # (service, media, the id the listing is filed under), and the service's own
    # id for the title in its one spelling (rows.own_id), per service title.
    refs: list[tuple[str, str, str]]
    owns: list[str]
    # Each record's position in `titles`.
    title_of: list[int]
    rows: rows.ListingRows
    now: int

    def only(self, positions: Iterable[int]) -> "Listings":
        """These listings narrowed to the records at `positions` — for a caller
        that learns inside its transaction which of them to write."""
        positions = list(positions)
        return _listed([self.records[i] for i in positions], self.rows.only(positions),
                       self.now)


def listings(records: Iterable[Record], *, now: int, stale_after: int) -> Listings:
    """`records` made ready for `plan_records` — pure, for the caller to call
    before its transaction."""
    records = list(records)
    return _listed(records, rows.listing_rows(records, now=now, stale_after=stale_after), now)


def _listed(records: list[Record], spelled: rows.ListingRows, now: int) -> Listings:
    titles: list[identity.Settling] = []
    refs: list[tuple[str, str, str]] = []
    owns: list[str] = []
    title_of: list[int] = []
    seen: dict[tuple[str, str, str], int] = {}
    for record, own in zip(records, spelled.owns):
        ref = (str(record.source), str(record.media), str(record.id))
        if ref not in seen:
            seen[ref] = len(titles)
            refs.append(ref)
            owns.append(own)
            titles.append(identity.Settling(
                str(record.media), dict(record.ids or {}),
                fallback=identity.per_source_key(record.source, record.id)))
        title_of.append(seen[ref])
    return Listings(records, titles, refs, owns, title_of, spelled, now)


class Planned(NamedTuple):
    """What `plan_records` decided about a batch of listings."""
    settles: identity.SettlePlan
    # Each record's title key once the plan is written.
    keys: list[str]
    # {(title key, service): the entry that title's group is once written} —
    # right for every title the plan's merges leave alone; a title in `touched`
    # has its own decided by the merge, and a caller reads it again after
    # `write_records` (`rows.primaries`).
    primaries: dict[tuple[str, str], str]
    # The listings as they are written: `Listings.rows`, less the shared ids a
    # listing stated about another title than its entry's (`stated`).
    rows: rows.ListingRows
    # {position in `Listings.titles`: the ids that service title's listings are
    # taken to state} for each whose ids named another title than the one its
    # entry is filed under — `identity.Planner.agreeing`'s answer.
    stated: dict[int, dict]

    @property
    def touched(self) -> frozenset[str]:
        """Every title key whose rows writing the plan merges — each re-key's two
        titles — as they are BEFORE it is written. Rare: a few titles in a fill
        of thousands, none in most."""
        return frozenset(key for pair in self.settles.retired for key in pair)

    @property
    def landed(self) -> frozenset[str]:
        """`touched` as it is AFTER the plan is written: a retired key is the
        title it merged into."""
        retired = dict(self.settles.retired)

        def followed(key: str) -> str:
            while key in retired:
                key = retired[key]
            return key

        return frozenset(followed(key) for key in self.touched)


def plan_records(conn: db.Connection, listed: Listings) -> Planned:
    """Which title each listing is — read and decided, NOT written: for a caller
    that makes it before taking the write lock (db.transaction's `prepare`) and
    writes it inside it (`write_records`).

    A LISTING NEVER MOVES ITS SERVICE'S ENTRY. A listing of an entry the store
    has filed is filed under that entry's title, whatever title its shared ids
    name, and those ids are taught to neither title (`identity.Planner.agreeing`);
    only the service's own answer about the title moves an entry
    (app/metadata/descriptions.py's `plan`, rows.move_entries). A listing still
    re-keys the title its entry is filed under when its ids name a higher one,
    and files an entry the store has not met where its ids say.

    WHY: ONE SERVICE'S LISTINGS DISAGREE WITH EACH OTHER, and its own answer
    about the title is the statement about the entry itself. MEASURED on the
    live CDN, 2026-11-02: Simkl's October calendar file names its entry 2777093
    (Cash Battle) as TMDB's 230757 and its November and December files as
    276408; entry 1221242 is 205664 in September's and October's and 121651 in
    November's — and Simkl's own lookup of each (/tv/{id}) names the newer
    title. Every week filled from one file moved the entry, its words, its
    seasons, its episodes and its listings to one title, and every week from the
    other moved them all back, inside the fill's write lock."""
    own_refs = [(source, media, own) for (source, media, _spelling), own
                in zip(listed.refs, listed.owns)]
    filed = rows.filed_keys(conn, [*listed.refs, *own_refs])
    titles = [title._replace(filed_under=filed.get(ref) or filed.get(own))
              for title, ref, own in zip(listed.titles, listed.refs, own_refs)]
    planner = identity.Planner(conn, now=listed.now)
    planner.prefetch(titles)
    stated: dict[int, dict] = {}
    for at, title in enumerate(titles):
        # Asked in turn, after every title before it is settled: an earlier
        # listing of the batch can register or re-key a title this one names.
        apart = planner.named_apart(title)
        if apart is not None:
            source, _media, _spelling = listed.refs[at]
            logger.info("title store: a %s listing of its entry %s names %s; the entry stays "
                        "filed under %s", source, listed.owns[at], apart, title.filed_under)
            stated[at] = planner.agreeing(title)
            title = title._replace(ids=stated[at])
        planner.settle(title)
    settles = planner.plan()
    keys = [identity.per_source_key(record.source, record.id)
            if (found := settles.identities[at]) is None else found.key
            for record, at in zip(listed.records, listed.title_of)]
    written = listed.rows.with_ids({position: stated[at] for position, at
                                    in enumerate(listed.title_of) if at in stated})
    return Planned(settles, keys, rows.primaries(conn, written, keys), written, stated)


def write_records(conn: db.Connection, listed: Listings, planned: Planned) -> list[str]:
    """`listed` written as `planned` (`plan_records`, made on this connection from
    what it holds now) decided, inside the caller's transaction: each listing's
    title registered and its service's group written. Returns each record's
    title key, in order.

    A FIXED NUMBER OF STATEMENTS, however many records: the identity's batch
    (identity.settle_many) and a statement per service and table written
    (rows.write_listings) — a re-key's own repointing is the rare exception
    (identity.py). A write transaction's lock is held while it runs, and a
    statement can wait a GIL switch interval while the event loop is busy — so
    a fill's lock time grew with its records, and the next writer's patience
    ran out (identity.py's reads have the measurement).

    THE RECORDS' IDS ARE COMPLETED IN PLACE with the title's shared ids once its
    rows are written (identity.complete). A caller that draws the records it just
    stored — a search following a result to its card — then keys them as the
    calendar's read will, rather than by whichever ids this one service stated.
    The groups keep what the service SAID — less, for a listing naming another
    title than its entry's, the ids that named it (`plan_records`).

    AND EVERY KEY IS THE ONE ITS TITLE ANSWERS TO WHEN THE FILL IS DONE. A later
    record of the same fill can re-key a title an earlier one registered — Simkl's
    listing names a show by its tvdb id, Trakt's names its tmdb id too — and the
    key the first resolution returned is then retired. A listing filed under it
    would have no identity to join (identity.settle_many).
    """
    if not listed.records:
        return []
    planned.settles.apply(conn)
    for record, at in zip(listed.records, listed.title_of):
        found = planned.settles.identities[at]
        if found is not None:
            record.ids = identity.with_shared(planned.stated.get(at, record.ids), found.ids)
    rows.write_listings(conn, planned.rows, planned.keys, now=listed.now)
    return planned.keys


class Asked(NamedTuple):
    """A title registered and asked about: what the store knows of it now, and
    which services could not be asked — which is not the same as "not listed",
    and a caller that remembers answers must not remember these."""
    identity: Identity | None
    unreachable: frozenset[str]


async def ensure(media: Any, ids: Mapping[str, Any] | None, *, settings=None,
                 filed_under: str | None = None) -> Identity | None:
    """The title `ids` names, registered — and, given a viewer's `settings`, with
    each service it has no id from asked once by shared id. None for ids that
    name no shared id and no key.

    `filed_under` is a key the caller already holds for the title (see
    identity.settle): a title re-keyed since it was filed is found through it.
    """
    found, = await ensure_many([(media, ids, filed_under)], settings=settings)
    return found


async def ask(media: Any, ids: Mapping[str, Any] | None, *, settings,
              filed_under: str | None = None,
              asking: Iterable[str] | None = None) -> Asked:
    """`ensure` with the services asked, saying which could not be.

    `asking` NARROWS WHO IS ASKED to the services it names — still only those
    the title holds no id from. For a caller spending one service's allowance at
    a time (app/calendar/completion.py): asking every service on one service's
    pace would spend the others' with nobody counting."""
    (found,), unreachable = await _ensure(
        [(str(media), dict(ids or {}), filed_under)], settings,
        asking=None if asking is None else frozenset(map(str, asking)))
    key = found.key if found is not None else None
    return Asked(found, frozenset(unreachable.get(key, ())))


async def ensure_many(titles: Iterable[tuple[Any, Mapping[str, Any] | None, str | None]], *,
                      settings=None) -> list[Identity | None]:
    """`ensure` for many titles: one transaction to register them all, the
    lookups made side by side, and one more transaction for what they taught.
    Answers in the order given."""
    found, _unreachable = await _ensure(
        [(str(media), dict(ids or {}), filed) for media, ids, filed in titles], settings)
    return found


async def _ensure(titles: list[tuple[str, dict, str | None]], settings, *,
                  asking: frozenset[str] | None = None
                  ) -> tuple[list[Identity | None], dict[str, set[str]]]:
    if not titles:
        return [], {}
    now = db.now()

    def _settle_all(conn) -> list:
        return identity.settle_many(
            conn, [identity.Settling(media, ids, filed) for media, ids, filed in titles],
            now=now)

    found = await db.transaction(_settle_all)
    if settings is None:
        return found, {}
    held = {item.key: (Media(item.media), item.ids) for item in found if item is not None}
    learned, unreachable = await _ask_services(settings, held, asking=asking)
    if not learned:
        return found, unreachable

    def _teach(conn) -> list:
        taught = [at for at, item in enumerate(found)
                  if item is not None and learned.get(item.key)]
        out = list(found)
        for at, item in zip(taught, identity.settle_many(conn, [
                identity.Settling(found[at].media, {**found[at].ids, **learned[found[at].key]},
                                  found[at].key) for at in taught], now=now)):
            out[at] = item
        return out

    taught = await db.transaction(_teach)
    # Keyed by what each title answered to when asked; a re-key the answers
    # caused carries the report over to the key it has now.
    moved = {before.key: after.key for before, after in zip(found, taught)
             if before is not None and after is not None}
    return taught, {moved.get(key, key): sources for key, sources in unreachable.items()}


async def _ask_services(settings, titles: Mapping[str, tuple[Media, Mapping[str, Any]]], *,
                        asking: frozenset[str] | None = None
                        ) -> tuple[dict[str, dict], dict[str, set[str]]]:
    """({title key: the ids learned}, {title key: services that could not be
    asked}) for `titles` — {key: (media, ids held)} — asking each service that
    answers an exact lookup by shared id (providers.for_shared_id_lookup — every
    one, whichever box found the title) about every title it holds no id for,
    and keeping what an exact match answers that the held ids lack.

    THE ONE PLACE THE APP ASKS BY SHARED ID. A service that could not be asked is
    logged and reported, never taken as "not listed". THE RE-ASK CLOCK IS EACH
    PORT'S OWN: an answer, "not listed" included, is kept for a day by the port,
    so asking again within the day costs no request — and a second viewer holding
    the same title costs none either, since the answer is a fact about the title.
    An answer that does not name the asked service's own id is no answer: a
    wrong service id is worse than a missing one. `asking`, when given, is which
    of those services may be asked at all (`ask` says why).
    """
    asked = [(key, source, port)
             for key, (_media, held) in titles.items()
             for source, port in providers.for_shared_id_lookup(settings)
             if held.get(str(source)) in (None, "")
             and (asking is None or str(source) in asking)]
    if not asked:
        return {}, {}
    answers = await asyncio.gather(
        *(port.find_by_shared_id(settings, titles[key][0], dict(titles[key][1]))
          for key, _source, port in asked),
        return_exceptions=True)
    learned: dict[str, dict] = {}
    unreachable: dict[str, set[str]] = {}
    for (key, source, _port), got in zip(asked, answers):
        if isinstance(got, BaseException):
            logger.info("title store: %s could not be asked about %s (%r)", source, key, got)
            unreachable.setdefault(key, set()).add(str(source))
            continue
        held = {**titles[key][1], **learned.get(key, {})}
        found = {name: value for name, value in id_rules.canonical(got or {}).items()
                 if held.get(name) in (None, "")}
        if found.get(str(source)) in (None, ""):
            continue
        logger.info("title store: %s answered %s for %s by shared id", source, found, key)
        learned.setdefault(key, {}).update(found)
    return learned, unreachable


async def group_identity(title_key: str) -> tuple[str, dict[str, Any], str] | None:
    """`(media, ids, title)` for one stored title, or None if nothing holds it.

    THE IDS ARE EVERYTHING KNOWN: the title's identity and then each service
    row's own, first writer winning, in the declared order — exactly what a card
    carries, so a caller acting on the title (adding it to a library, asking a
    service about it) works from the ids the button in front of the reader was
    drawn from rather than from ids the page handed it.

    THAT IS THE WHOLE POINT OF IT EXISTING. A write whose subject comes off the
    page is a write somebody can aim somewhere else by editing an attribute; a
    write whose subject is a KEY the server resolves can only ever land on a title
    this instance already holds. A key retired by a re-key is followed.
    """
    title_known = await identity.known(str(title_key))
    key = title_known.key if title_known is not None else str(title_key)
    found = await rows.titles_for(key)
    if not found and title_known is None:
        return None
    merged = dict(title_known.ids) if title_known is not None else {}
    title = ""
    for row in sorted(found, key=lambda r: rows.SOURCE_ORDER.get(str(r["source"]), 99)):
        for name, value in _loads(row["ids_json"]).items():
            if value not in (None, ""):
                merged.setdefault(name, value)
        title = title or str(row["title"] or "")
    media = title_known.media if title_known is not None else str(found[0]["media"])
    return media, collect_ids(merged), title


def _loads(text) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def register(conn: db.Connection, media: Any, ids: Mapping[str, Any] | None, *,
             filed_under: str) -> Identity | None:
    """A title a row holds or is about to hold, registered inside the caller's
    transaction — a board add, a restore putting rows back. No service is asked.
    If the title answers to a key other than `filed_under`, every row already
    holding `filed_under` has just been moved to it, the caller's included."""
    return identity.settle(conn, media, ids, filed_under=filed_under, now=db.now())


def resolve_triple(conn: db.Connection, media: Any, ids: Mapping[str, Any] | None
                     ) -> ItemKey | None:
    """The key `ids` would be registered under, decided and NOT written — for a
    preview that must not change anything (identity.resolve)."""
    key = identity.resolve(conn, media, ids)
    return None if key is None else identity.Identity(key, str(media), {}).triple
