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
  - `store_records` for a SERVICE'S OWN LISTINGS (a calendar fill, an airing a
    search wrote), inside the caller's transaction: each record's title is
    resolved, its row written under that title's key, and no service is asked —
    a busy fill names hundreds of titles against a 10-a-second ceiling;
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


def store_records(conn: db.Connection, records: Iterable[Record], *, now: int,
                  stale_after: int) -> list[str]:
    """Register each record's title and write the service's own rows for it,
    inside the caller's transaction. Returns each record's title key, in order.

    THE RECORDS' IDS ARE COMPLETED IN PLACE with the title's shared ids once its
    rows are written (identity.complete). A caller that draws the records it just
    stored — a search following a result to its card — then keys them as the
    calendar's read will, rather than by whichever ids this one service stated.
    The rows keep what the service SAID.

    ONE RESOLUTION PER SERVICE TITLE, however many airings of it arrive, which is
    most of what a fill is.
    """
    keys: list[str] = []
    settled: dict[tuple[str, str, str], identity.Identity | None] = {}
    for record in records:
        ref = (str(record.source), str(record.media), str(record.id))
        if ref not in settled:
            settled[ref] = identity.settle(
                conn, record.media, record.ids,
                filed_under=rows.filed_key(conn, record),
                fallback=identity.per_source_key(record.source, record.id), now=now)
        found = settled[ref]
        key = found.key if found is not None else identity.per_source_key(record.source, record.id)
        rows.write_listing(conn, record, key, now=now, stale_after=stale_after)
        if found is not None:
            record.ids = identity.with_shared(record.ids, found.ids)
        keys.append(key)
    return keys


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
              filed_under: str | None = None) -> Asked:
    """`ensure` with the services asked, saying which could not be."""
    (found,), unreachable = await _ensure(
        [(str(media), dict(ids or {}), filed_under)], settings)
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


async def _ensure(titles: list[tuple[str, dict, str | None]], settings
                  ) -> tuple[list[Identity | None], dict[str, set[str]]]:
    if not titles:
        return [], {}
    now = db.now()

    def _settle_all(conn) -> list:
        return [identity.settle(conn, media, ids, filed_under=filed, now=now)
                for media, ids, filed in titles]

    found = await db.transaction(_settle_all)
    if settings is None:
        return found, {}
    held = {item.key: (Media(item.media), item.ids) for item in found if item is not None}
    learned, unreachable = await _ask_services(settings, held)
    if not learned:
        return found, unreachable

    def _teach(conn) -> list:
        out = []
        for item in found:
            if item is None or not learned.get(item.key):
                out.append(item)
                continue
            out.append(identity.settle(conn, item.media, {**item.ids, **learned[item.key]},
                                       filed_under=item.key, now=now))
        return out

    taught = await db.transaction(_teach)
    # Keyed by what each title answered to when asked; a re-key the answers
    # caused carries the report over to the key it has now.
    moved = {before.key: after.key for before, after in zip(found, taught)
             if before is not None and after is not None}
    return taught, {moved.get(key, key): sources for key, sources in unreachable.items()}


async def _ask_services(settings, titles: Mapping[str, tuple[Media, Mapping[str, Any]]]
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
    wrong service id is worse than a missing one.
    """
    asked = [(key, source, port)
             for key, (_media, held) in titles.items()
             for source, port in providers.for_shared_id_lookup(settings)
             if held.get(str(source)) in (None, "")]
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
