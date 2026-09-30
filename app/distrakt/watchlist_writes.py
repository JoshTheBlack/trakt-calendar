"""The tracker's shows, written to the viewer's own list at each service they
switched on — and taken back off it when the viewer's own hand takes a show off
the tracker.

ONE OWNER FOR "WHAT IS OWED". Nothing here keeps a list of which routes put a
show on the tracker. Every pass compares two things this app already stores —
which shows are on the tracker now (`on_tracker`), and what `watchlist_writes`
says was done about each at each service — and writes the difference. So a new
way onto the tracker is covered the day it is written, a pass that never ran
(a restart, a failure, a switch turned on late) is made up by the next one, and
two passes that overlap cannot both decide the same show is owed: they run one
at a time per viewer, and the second reads the rows the first wrote.

ON THE TRACKER means the viewer's own list plus the premiere records of the
month under way and of any month built ahead of it — a season announced and not
yet started is one the viewer means to watch. NOT a premiere whose season has
already been settled (finished or given up on) in one of those months, because
the tracker itself treats that season as done with (lifecycle.advance's own
"decided" set); NOT a completed record or a past month, which are history; and
NOT a film, which the tracker records as something watched. A SHOW is its
(match_source, match_id): a service lists a show once, so its seasons collapse.

ONCE PER ARRIVAL. A show is written when it arrives and then left alone while it
stays — the service moving it on at a play, or a media server moving it back, is
expected and never fought. When it LEAVES, its row is forgotten, so coming back
(a new season of something finished, a take-back of an abandon) is an arrival
and is written again. Two departures are the viewer's own act and have an
answer at the service: giving up drops the show there, and the ✕ takes back
the app's own write (`expect`). Every other departure changes nothing at the
service.

THE TRACKER NEVER WAITS FOR A SERVICE. A pass runs as a task of its own after
the response has gone (`nudge`, called by the tracker router's one hook), and
nothing it does can raise into a request. A failure is logged, recorded against
the show as `failed` (retried by a later pass, after a quiet spell, so a service
that is down is not asked again on every click) and shown beside the switch.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import NamedTuple

from .. import clock, db, providers
from ..auth import viewer
from ..providers.base import (NOT_HELD, ItemKey, Media, SourceUnavailable, WatchlistOutcome,
                              WatchlistResult, WatchlistTitle, WatchlistUndo, parse_item_key)
from . import store

logger = logging.getLogger(__name__)


class WriteState(StrEnum):
    """Where one show stands at one service — the `watchlist_writes.state`
    vocabulary. A StrEnum so the member IS the stored string."""
    WRITTEN = "written"
    DROPPED = "dropped"
    UNDONE = "undone"
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    FAILED = "failed"


# THE STATES A SHOW ON THE TRACKER IS STILL OWED A WRITE IN. A show the viewer
# gave up on or took off, and has since put back, is an arrival; a failed write
# is tried again. WRITTEN, NOT_FOUND and REFUSED are settled: the first is done,
# and the other two are the service saying a retry would change nothing.
OWED_STATES = frozenset({WriteState.DROPPED, WriteState.UNDONE, WriteState.FAILED})

# The states that RECORD A DEPARTURE the viewer made. Kept when the show is off
# the tracker, because they say what the app did at the service when it left;
# every other row of a show that has left is forgotten, which is what makes its
# return an arrival.
DEPARTED_STATES = frozenset({WriteState.DROPPED, WriteState.UNDONE})


class Departure(StrEnum):
    """What the viewer did to take a season off the tracker by hand."""
    ABANDON = "abandon"
    REMOVE = "remove"


# How each verb's outcome is filed. An outcome a table does not name is FAILED:
# anything unexpected from a service is tried again rather than taken as done.
#
# A put answered ALREADY is WRITTEN: the viewer had the show on that list
# themselves, the row keeps `status_before` saying so, and an undo later finds
# nothing to change — which is right, because the viewer put it there.
_PUT_STATES = {WatchlistOutcome.WRITTEN: WriteState.WRITTEN,
               WatchlistOutcome.ALREADY: WriteState.WRITTEN,
               WatchlistOutcome.NOT_FOUND: WriteState.NOT_FOUND,
               WatchlistOutcome.REFUSED: WriteState.REFUSED}
# A drop answered ALREADY is the service holding it as given up, or not holding
# it at all — the port never ADDS a title in order to drop it. Either way the
# show has left, and DROPPED is what makes its return an arrival.
_DROP_STATES = {WatchlistOutcome.WRITTEN: WriteState.DROPPED,
                WatchlistOutcome.ALREADY: WriteState.DROPPED,
                WatchlistOutcome.NOT_FOUND: WriteState.NOT_FOUND,
                WatchlistOutcome.REFUSED: WriteState.REFUSED}
# KEPT and ALREADY are an undo's final answers, not failures: the first is the
# service holding something the viewer did themselves, the second nothing left to
# put back.
_UNDO_STATES = {WatchlistOutcome.WRITTEN: WriteState.UNDONE,
                WatchlistOutcome.ALREADY: WriteState.UNDONE,
                WatchlistOutcome.KEPT: WriteState.UNDONE,
                WatchlistOutcome.NOT_FOUND: WriteState.NOT_FOUND,
                WatchlistOutcome.REFUSED: WriteState.REFUSED}

# How long a service that failed is left alone before a pass asks it again. A
# failure is usually the service being down or rate-limiting, and every tracker
# request starts a pass, so without this a viewer clicking about would ask a
# service that is refusing once per click.
QUIET_SECONDS = 300


class WriteRow(NamedTuple):
    """One `watchlist_writes` row."""
    service_id: str | None
    status_before: str
    state: WriteState


class Trouble(NamedTuple):
    """The last failure at one service, as the switch area says it."""
    message: str
    at: float


class Pending(NamedTuple):
    """One departure waiting for its answer at the services.

    `serial` tells this request's departure from a later one for the same show,
    so a pass finishing with the first never discards the second.

    `ready` is whether the request that said it has FINISHED. Until then a pass
    neither acts on it nor forgets the show's row: a pass that started earlier
    can read the tracker before this request's removal is written, find the show
    still there, and would otherwise conclude it was not the last season."""
    departure: Departure
    key: str
    serial: int
    ready: bool = False


# IN PROCESS, AND NONE OF IT IS WHAT IS OWED. The table says that. What lives
# here is only what a restart may lose without anything going unwritten: which
# viewers have a pass running, the ✕s and abandons whose answer at the service
# has not been sent yet, and when a service last failed.
_locks: dict[int, asyncio.Lock] = {}
_running: dict[int, asyncio.Task] = {}
_again: set[int] = set()
_departures: dict[int, dict[str, Pending]] = {}
_serials = itertools.count(1)
_trouble: dict[tuple[int, str], Trouble] = {}


# which shows are on the tracker

def _key(record: Mapping) -> str:
    return str(ItemKey(str(Media.SHOW), str(record["match_source"]), str(record["match_id"])))


async def on_tracker(user_id: int) -> dict[str, WatchlistTitle]:
    """Every SHOW on the viewer's tracker, keyed by its flat item key, with every
    id any of its seasons holds — see this module's header for what counts."""
    today = clock.today()
    ahead = await store.records_from(user_id, store.month_key(today.year, today.month),
                                     store.MONTH_KINDS)
    decided = {(record["key"], int(record["season"])) for record in ahead
               if record["kind"] in store.SETTLED_KINDS}
    seasons = [*await store.user_records(user_id),
               *(record for record in ahead if record["kind"] in store.PREMIERE_KINDS
                 and (record["key"], int(record["season"])) not in decided)]
    ids: dict[str, dict] = {}
    for record in seasons:
        if record["media"] != Media.SHOW:
            continue
        held = ids.setdefault(_key(record), {})
        for name, value in (record.get("ids") or {}).items():
            held.setdefault(name, value)
    return {key: WatchlistTitle(parse_item_key(key), held) for key, held in ids.items()}


# the two tables

async def switched_on(user_id: int) -> list[str]:
    """The services this viewer has switched the tracker's writes on for."""
    found = await db.fetch_all(
        "SELECT source FROM watchlist_targets WHERE user_id = ? ORDER BY source", (user_id,))
    return [row["source"] for row in found]


async def switch(user_id: int, source: str, on: bool) -> None:
    """Switch writes to `source` on or off for this viewer.

    OFF CHANGES NOTHING AT THE SERVICE and keeps every row: what was written
    stays written, and a later switch-on picks up only what arrived meanwhile.
    ON is only the row; the pass that follows the request writes what is owed,
    which on a first switch-on is everything already on the tracker.
    """
    if on:
        await db.execute(
            "INSERT INTO watchlist_targets (user_id, source, enabled_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, source) DO NOTHING", (user_id, source, db.now()))
    else:
        await db.execute("DELETE FROM watchlist_targets WHERE user_id = ? AND source = ?",
                         (user_id, source))
    _trouble.pop((user_id, source), None)


async def rows(user_id: int, source: str) -> dict[str, WriteRow]:
    """What the tracker has done about each show at `source`, keyed like
    `on_tracker`."""
    found = await db.fetch_all(
        "SELECT match_source, match_id, service_id, status_before, state "
        "FROM watchlist_writes WHERE user_id = ? AND source = ?", (user_id, source))
    return {_key(row): WriteRow(row["service_id"], row["status_before"], WriteState(row["state"]))
            for row in found}


async def _record(user_id: int, source: str, written: Mapping[str, WriteRow]) -> None:
    """Write one batch's answers in one transaction."""
    if not written:
        return
    now = db.now()

    def _work(conn: db.Connection) -> None:
        for key, row in written.items():
            title = parse_item_key(key)
            conn.execute(
                "INSERT INTO watchlist_writes (user_id, source, match_source, match_id, "
                "service_id, status_before, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, source, match_source, match_id) DO UPDATE SET "
                "service_id = excluded.service_id, status_before = excluded.status_before, "
                "state = excluded.state, updated_at = excluded.updated_at",
                (user_id, source, title.match_source, title.match_id, row.service_id,
                 row.status_before, str(row.state), now))

    await db.transaction(_work)


async def _forget(user_id: int, source: str, keys: Sequence[str]) -> None:
    def _work(conn: db.Connection) -> None:
        for key in keys:
            title = parse_item_key(key)
            conn.execute(
                "DELETE FROM watchlist_writes WHERE user_id = ? AND source = ? "
                "AND match_source = ? AND match_id = ?",
                (user_id, source, title.match_source, title.match_id))

    await db.transaction(_work)


def _filed(result: WatchlistResult | None, states: Mapping[WatchlistOutcome, WriteState],
           before: WriteRow | None) -> WriteRow:
    """One title's answer as the row it leaves. No answer at all — the service
    could not be asked — is FAILED, keeping whatever the row knew already."""
    if result is None:
        return WriteRow(before.service_id if before else None,
                        before.status_before if before else NOT_HELD, WriteState.FAILED)
    return WriteRow(result.service_id or (before.service_id if before else None),
                    result.status_before, states.get(result.outcome, WriteState.FAILED))


def _departed_title(key: str, source: str, row: WriteRow | None) -> WatchlistTitle:
    """A show that has LEFT the tracker, as a port is to find it: by the shared
    id the tracker filed it under, and by the service's own id where a write
    recorded one. Its seasons are gone, so those are the ids there are."""
    title = parse_item_key(key)
    ids = {title.match_source: title.match_id}
    if row is not None and row.service_id:
        ids[source] = row.service_id
    return WatchlistTitle(title, ids)


# the pass

def expect(user_id: int, key: ItemKey | str, departure: Departure) -> Pending:
    """Say that a request is about to take a season of `key` off the tracker by
    the viewer's hand. Whether it was the show's LAST season is decided by the
    pass, from the tracker as it then stands.

    CALLED BEFORE THE REQUEST CHANGES ANYTHING, and synchronously, because a pass
    started by some other request may run between this request's write and its
    answer — and that pass, finding the show gone, would forget its row, and with
    it the only record that the app had written the show. A pending departure
    keeps the row until it has been answered, and is acted on only once its
    request has finished (`after_request`).
    """
    pending = Pending(departure, str(key), next(_serials))
    _departures.setdefault(user_id, {})[pending.key] = pending
    return pending


def withdraw(user_id: int, pending: Pending) -> None:
    """Take back an `expect` whose request turned out to change nothing — the
    season was not where the request said — so no pass answers a departure that
    never happened. Only that request's own departure: one made since stands."""
    waiting = _departures.get(user_id, {})
    if _same(waiting.get(pending.key), pending):
        del waiting[pending.key]


def _same(found: Pending | None, pending: Pending) -> bool:
    return found is not None and found.serial == pending.serial


async def after_request(user_id: int, departed: Sequence[Pending] = ()) -> None:
    """What the tracker's router runs once one of this viewer's answers has gone:
    the departures THAT request said (`departed`, what its `expect` calls
    returned) are now whole, and a pass is started if the viewer switched any
    service on.

    Only that request's own, because another request of the viewer's may still
    be between its `expect` and its removal. Nothing switched on means no pass
    will ever answer them, so they are let go here rather than kept for good."""
    waiting = _departures.get(user_id, {})
    for pending in departed:
        if _same(waiting.get(pending.key), pending):
            waiting[pending.key] = pending._replace(ready=True)
    if await switched_on(user_id):
        nudge(user_id)
    else:
        _settle_departures(user_id, {pending.key: pending for pending in departed})


def nudge(user_id: int) -> None:
    """Start a pass for this viewer as a task of its own, or, if one is running,
    have it go round once more when it finishes. Returns at once."""
    if user_id in _running:
        _again.add(user_id)
        return
    _running[user_id] = asyncio.get_running_loop().create_task(_drive(user_id))


async def _drive(user_id: int) -> None:
    try:
        while True:
            _again.discard(user_id)
            await run(user_id)
            if user_id not in _again:
                return
    finally:
        _running.pop(user_id, None)


def busy(user_id: int) -> bool:
    """Whether a pass is running for this viewer, or about to."""
    return user_id in _running


async def run(user_id: int) -> None:
    """One pass for one viewer, never two at once. Raises nothing: a pass is
    background work, and whatever goes wrong is logged and waits for the next."""
    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        try:
            await _pass(user_id)
        except Exception:
            logger.exception("Watchlist pass failed for user %s", user_id)


def trouble(user_id: int, source: str) -> Trouble | None:
    """The last failure writing to `source` for this viewer, or None. In process:
    a restart forgets the wording, and the failed rows it left say the rest."""
    return _trouble.get((user_id, source))


def _quiet(user_id: int, source: str) -> bool:
    found = _trouble.get((user_id, source))
    return found is not None and time.monotonic() - found.at < QUIET_SECONDS


def _fail(user_id: int, source: str, message: str) -> None:
    logger.warning("Watchlist writes to %s for user %s: %s", source, user_id, message)
    _trouble[(user_id, source)] = Trouble(message, time.monotonic())


async def _pass(user_id: int) -> None:
    sources = await switched_on(user_id)
    taken = {key: pending for key, pending in _departures.get(user_id, {}).items()
             if pending.ready}
    if not sources:
        _settle_departures(user_id, taken)
        return
    tracked = await on_tracker(user_id)
    # A departure whose show is still on the tracker was not its last season,
    # and there is nothing to answer at any service.
    pending = {key: waiting.departure for key, waiting in taken.items() if key not in tracked}
    unfinished: set[str] = set()
    settings = ports = None
    for source in sources:
        held = await rows(user_id, source)
        gone = [key for key, row in held.items()
                if key not in tracked and key not in _departures.get(user_id, {})
                and row.state not in DEPARTED_STATES]
        if gone:
            await _forget(user_id, source, gone)
        owed = [title for key, title in tracked.items()
                if key not in held or held[key].state in OWED_STATES]
        if not owed and not pending:
            continue
        if _quiet(user_id, source):
            unfinished.update(pending)
            continue
        if settings is None:
            settings = await viewer.settings_for_user(user_id)
            ports = dict(providers.for_watchlist(settings))
        port = ports.get(source)
        if port is None:
            # Switched on, but the viewer's link to the service is gone. Nothing
            # is written until it is back, and the table still says what is owed.
            unfinished.update(pending)
            continue
        left = await _depart(user_id, source, port, settings, pending, held)
        failed = await _arrive(user_id, source, port, settings, owed, held)
        unfinished |= left
        if not left and not failed:
            _trouble.pop((user_id, source), None)
    _settle_departures(user_id, {key: waiting for key, waiting in taken.items()
                                 if key not in unfinished})


def _settle_departures(user_id: int, answered: Mapping[str, Pending]) -> None:
    """Take the departures this pass answered off the waiting list — each only if
    it is still the one the pass read, so a ✕ that arrived during the pass
    waits for the next."""
    waiting = _departures.get(user_id, {})
    for key, pending in answered.items():
        if _same(waiting.get(key), pending):
            del waiting[key]
    if not waiting:
        _departures.pop(user_id, None)


async def _arrive(user_id: int, source: str, port, settings,
                  owed: Sequence[WatchlistTitle], before: Mapping[str, WriteRow]) -> bool:
    """Write every owed show to the list and file each answer. True when any of
    them failed."""
    if not owed:
        return False
    try:
        results = await port.put(settings, owed)
    except SourceUnavailable as exc:
        _fail(user_id, source, str(exc))
        results = {}
    filed = {str(title.key): _filed(results.get(str(title.key)), _PUT_STATES,
                                    before.get(str(title.key)))
             for title in owed}
    failed = sum(row.state is WriteState.FAILED for row in filed.values())
    if failed and results:
        _fail(user_id, source, f"{failed} show(s) could not be confirmed on the list.")
    await _record(user_id, source, filed)
    return bool(failed)


async def _depart(user_id: int, source: str, port, settings,
                  pending: Mapping[str, Departure], held: Mapping[str, WriteRow]) -> set[str]:
    """Answer the viewer's own departures at one service: drop every show given
    up on, whatever the service held, and take back the app's own write of every
    show ✕'d off — only where a row says the app wrote it. Returns the keys still
    to be answered, which a later pass tries again.

    A FAILURE CHANGES NO ROW. The row saying WRITTEN is what licenses an undo, so
    overwriting it with FAILED would leave the retry with nothing to go on.
    """
    drops = [key for key, departure in pending.items() if departure is Departure.ABANDON]
    undos = [key for key, departure in pending.items() if departure is Departure.REMOVE
             and key in held and held[key].state is WriteState.WRITTEN]
    unfinished: set[str] = set()
    for keys, states, send in (
            (drops, _DROP_STATES, lambda: port.drop(
                settings, [_departed_title(key, source, held.get(key)) for key in drops])),
            (undos, _UNDO_STATES, lambda: port.undo(
                settings, [WatchlistUndo(_departed_title(key, source, held[key]),
                                         held[key].status_before) for key in undos]))):
        if not keys:
            continue
        try:
            results = await send()
        except SourceUnavailable as exc:
            _fail(user_id, source, str(exc))
            unfinished.update(keys)
            continue
        filed = {key: _filed(results.get(key), states, held.get(key)) for key in keys}
        stuck = {key for key, row in filed.items() if row.state is WriteState.FAILED}
        if stuck:
            _fail(user_id, source, f"{len(stuck)} show(s) taken off the tracker could not "
                                   "be changed on the list.")
        unfinished |= stuck
        await _record(user_id, source, {key: row for key, row in filed.items()
                                        if key not in stuck})
    return unfinished
