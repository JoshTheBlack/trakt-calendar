"""Writing the tracker's shows to a person's Trakt watchlist, and taking those
writes back — app/providers/base.py's WatchlistPort, for Trakt.

TRAKT'S WATCHLIST IS A PLAIN LIST, NOT A SET OF STATUSES. A show is on it or it
is not, so what a service holds is "listed" or NOT_HELD, a put adds, and both a
drop and an undo remove. Removing a show from the watchlist takes nothing else
with it — its plays, ratings and collection are separate records at Trakt — so
no rule here turns on what has been watched, where Simkl's must.

WHAT WAS MEASURED against the live service, 2026-09-30, on titles the account's
owner set aside for it, and what each rule here rests on:
  - `POST /sync/watchlist` answers 201 with COUNTS — `added` and `existing` —
    and names only what it could not match, under `not_found`, each item
    echoing the ids exactly as they were sent. A put of a title already listed
    answers `existing: 1`, `added: 0`.
  - `POST /sync/watchlist/remove` answers 200 with a `deleted` count and the
    same `not_found` echo.
  - `GET /sync/watchlist/shows` PAGES, AT 100 BY DEFAULT: a 146-item read
    returned 100, and a removal built from that read missed 46. So every read
    here follows X-Pagination-Page-Count to the end.
  - One write of 100 shows was accepted whole (see WRITE_CHUNK).
  - Trakt takes a show off the watchlist by itself at the first play after it
    was listed. So a show found unlisted where the tracker wrote it may have
    been played rather than removed by hand; either way it is not the
    tracker's to put back.

WRITES ARE NEVER CACHED, AND NEITHER IS A READ. The watchlist is one person's
list, and the response cache is URL-keyed and shared by the instance; a read
here goes straight through the transport's sender and never touches the cache
at all, which is the strongest form of `private=True`. Writes go through the
same sender, which keeps one of a person's writes in flight at a time and a
second apart (transport.POST_MIN_INTERVAL).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import urlencode

from ...config import Settings
from ..base import (NOT_HELD, Media, WatchlistHolding, WatchlistOutcome, WatchlistResult,
                    WatchlistTitle, WatchlistUndo, resolve_key)
from . import _ids, transport

logger = logging.getLogger(__name__)

# What a watchlist read says of a title that is on it. A plain list has one
# status, and this is its spelling in `watchlist_writes.status_before`.
LISTED = "listed"

# The ids a write may name a show by, as Trakt's /sync endpoints take them, and
# which of them it wants as numbers.
_SENDABLE = ("tmdb", "tvdb", "imdb")
_NUMERIC = frozenset({"trakt", "tmdb", "tvdb"})

_ADD = "sync/watchlist"
_REMOVE = "sync/watchlist/remove"

# One page of a watchlist read. Trakt's own default, and the size its paging was
# measured at.
PAGE_SIZE = 100
# How many pages one read may take. The largest watchlist an account can hold is
# 5,000 shows and films together (a VIP account's limit, measured on the owner's;
# a free account's is smaller), which is 50 pages of shows at most. A read that
# claims more is not a watchlist this module understands, and it stops rather
# than pages without end.
MAX_PAGES = 60


def list_url(account_id: str) -> str:
    """The watchlist page on Trakt's site, for whoever is signed in there.

    `me` AND NOT THE ACCOUNT'S NAME, because nothing this app stores is the
    name: a linked identity keeps the account's UUID, the only handle Trakt
    cannot hand to somebody else, and its display name is the person's `name`
    when they set one. MEASURED, 2026-09-30: the account's owner, signed in,
    opened this address and saw their own watchlist; the site sends every
    visitor who is NOT signed in to its home page, whatever name the address
    carries, so an anonymous check cannot tell one address from another.
    `account_id` is accepted for the port's shape and not needed.
    """
    return "https://app.trakt.tv/users/me/watchlist"


# ---------------------------------------------------------------------------
# reading the watchlist
# ---------------------------------------------------------------------------

class _Index:
    """One read of the watchlist's shows, indexed the two ways a title can be
    found in it: by Trakt's own id, and by the shared identity the tracker
    files titles under. The second keeps every show that shares a key, so a
    key naming more than one is recognised as ambiguous rather than guessed."""

    def __init__(self, items: Sequence) -> None:
        self.by_trakt: dict[str, dict] = {}
        self.by_key: dict[str, list[dict]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            ids = _ids.normalize((item.get("show") or {}).get("ids") or {})
            if ids.get("trakt") not in (None, ""):
                self.by_trakt[str(ids["trakt"])] = ids
            key = resolve_key(Media.SHOW, ids)
            if key is not None:
                self.by_key.setdefault(str(key), []).append(ids)

    def find(self, title: WatchlistTitle) -> tuple[dict | None, bool]:
        """(the listed show's ids, or None; whether the match was AMBIGUOUS).
        Trakt's own id wins outright."""
        trakt_id = title.ids.get("trakt")
        if trakt_id not in (None, ""):
            return self.by_trakt.get(str(trakt_id)), False
        found = self.by_key.get(str(title.key)) or []
        return (found[0], False) if len(found) == 1 else (None, len(found) > 1)


def _holding(ids: dict | None, title: WatchlistTitle) -> WatchlistHolding:
    """What the list holds for `title`. `watched` is always False: it exists to
    say whether taking a title away could take viewing history with it, and
    taking a show off this list cannot."""
    if ids is None:
        trakt_id = title.ids.get("trakt")
        return WatchlistHolding(NOT_HELD, False,
                                None if trakt_id in (None, "") else str(trakt_id))
    return WatchlistHolding(LISTED, False,
                            None if ids.get("trakt") in (None, "") else str(ids["trakt"]))


def _refusal(resp, what: str) -> transport.TraktError:
    if resp.status_code in transport.CREDENTIAL_STATUSES:
        return transport.TraktError(
            "Trakt rejected the credentials (%s). The link has to be made again."
            % resp.status_code, resp.status_code)
    return transport.TraktError(f"Trakt refused {what} (HTTP {resp.status_code}).",
                                resp.status_code)


async def _read(settings: Settings) -> _Index:
    """Every show on the watchlist, every page. Raises TraktError when any page
    cannot be read: a read missing a page would make the shows on it look
    unlisted, and a drop or an undo would then leave them where they are."""
    items: list = []
    page = 1
    while True:
        url = (f"{transport.API_BASE}/sync/watchlist/shows?"
               f"{urlencode({'page': page, 'limit': PAGE_SIZE})}")
        resp = await transport.send_or_unavailable(
            transport.shared_client(), "GET", url,
            headers=transport.api_headers(settings, paginate=False))
        if resp.status_code != 200:
            logger.warning("Trakt watchlist read, page %d -> HTTP %s: %s", page,
                           resp.status_code, resp.text[:200])
            raise _refusal(resp, "a read of the watchlist")
        try:
            batch = resp.json()
        except ValueError:
            batch = None
        if not isinstance(batch, list):
            raise transport.TraktError("Trakt answered a watchlist read with no list.")
        items.extend(batch)
        try:
            pages = int(resp.headers.get("x-pagination-page-count") or 1)
        except (TypeError, ValueError):
            pages = 1
        if pages > MAX_PAGES:
            raise transport.TraktError(
                f"Trakt says the watchlist runs to {pages} pages; {MAX_PAGES} is the most "
                "a watchlist can hold.")
        if page >= pages or not batch:
            return _Index(items)
        page += 1


async def holdings(settings: Settings,
                   titles: Sequence[WatchlistTitle]) -> dict[str, WatchlistHolding]:
    if not titles:
        return {}
    index = await _read(settings)
    return {str(title.key): _holding(index.find(title)[0], title) for title in titles}


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _sendable_ids(title: WatchlistTitle, service_id: str | None) -> dict:
    """The ids a write names `title` by: TRAKT'S OWN ID ALONE when it is known,
    since it names exactly one show, and every shared id Trakt takes
    otherwise."""
    if service_id:
        return {"trakt": int(service_id) if service_id.isdigit() else service_id}
    out = {}
    for name in _SENDABLE:
        value = title.ids.get(name)
        if value in (None, ""):
            continue
        text = str(value).strip()
        out[name] = int(text) if name in _NUMERIC and text.isdigit() else text
    return out


def _echo_key(ids) -> tuple:
    """A sent id map and Trakt's echo of it, as one comparable value."""
    if not isinstance(ids, dict):
        return ()
    return tuple(sorted((str(name), str(value)) for name, value in ids.items()))


def _count(answer: dict, section: str) -> int:
    block = answer.get(section)
    try:
        return int((block or {}).get("shows") or 0) if isinstance(block, dict) else 0
    except (TypeError, ValueError):
        return 0


# HOW MANY SHOWS ONE WRITE CARRIES. Measured on the live service: one
# POST /sync/watchlist of 100 shows answered 201 in 2.7 s with all 100 added.
# A larger batch goes out in chunks of the size that was proven, each paced by
# the transport's lane.
WRITE_CHUNK = 100


class _Batch:
    """One verb's work over a batch: what was read before, which titles were
    sent and by what ids, and each title's answer as it becomes known."""

    def __init__(self, titles: Sequence[WatchlistTitle], before: _Index) -> None:
        self.titles = {str(title.key): title for title in titles}
        self.before = {key: _holding(before.find(title)[0], title)
                       for key, title in self.titles.items()}
        self.ambiguous = {key for key, title in self.titles.items() if before.find(title)[1]}
        self.results: dict[str, WatchlistResult] = {}
        self.sent: dict[str, dict] = {}
        # What the read-back must show for each title a body said was written.
        self.expected: dict[str, str] = {}
        # Titles an add answered in a chunk that said some of its shows were
        # ALREADY listed — see `write`.
        self.maybe_listed: set[str] = set()
        self.nothing_added: set[str] = set()
        self.posted = False

    def settle(self, key: str, outcome: WatchlistOutcome) -> None:
        held = self.before[key]
        self.results[key] = WatchlistResult(outcome, held.status, held.status, held.service_id)

    def unsendable(self, key: str) -> bool:
        """A title no id names exactly one show by: none Trakt takes, or a shared
        id that names several listed shows. A guess would add or remove a show
        that is not this one."""
        ids = _sendable_ids(self.titles[key], self.before[key].service_id)
        if key in self.ambiguous or not ids:
            logger.info("Trakt watchlist: not writing %s — no id names exactly one "
                        "Trakt show", key)
            return True
        self.sent[key] = ids
        return False

    async def write(self, settings: Settings, path: str, keys: Sequence[str],
                    shows: str) -> None:
        """Send `keys` in chunks of WRITE_CHUNK, reading each answer's body, and
        note what the read-back must then show (`shows`).

        A WRITE THAT FAILS BEFORE ANY HAS BEEN ANSWERED RAISES: nothing is known
        to have happened, which is "could not ask". Once one has been answered,
        a later one failing fails only its own titles.

        A LIST AT ITS LIMIT IS AN ANSWER, NOT A FAILURE: Trakt's 420 says the
        account cannot hold more, every title in that write is REFUSED, and
        nothing retries it — waiting makes no room.
        """
        for start in range(0, len(keys), WRITE_CHUNK):
            chunk = list(keys[start:start + WRITE_CHUNK])
            try:
                resp = await transport.send_or_unavailable(
                    transport.shared_client(), "POST", f"{transport.API_BASE}/{path}",
                    headers=transport.api_headers(settings, paginate=False),
                    json={"shows": [{"ids": self.sent[key]} for key in chunk]})
                if resp.status_code == transport.ACCOUNT_LIMIT_STATUS:
                    logger.warning(
                        "Trakt watchlist: the account's list is full (limit %s, VIP %s); "
                        "%d show(s) refused", resp.headers.get("x-account-limit") or "?",
                        resp.headers.get("x-vip-user") or "?", len(chunk))
                    self.posted = True
                    for key in chunk:
                        self.settle(key, WatchlistOutcome.REFUSED)
                    continue
                if not 200 <= resp.status_code < 300:
                    logger.warning("Trakt POST /%s -> HTTP %s: %s", path, resp.status_code,
                                   resp.text[:200])
                    raise _refusal(resp, f"POST /{path}")
            except transport.TraktError:
                if not self.posted:
                    raise
                for key in chunk:
                    self.settle(key, WatchlistOutcome.FAILED)
                continue
            self.posted = True
            try:
                answer = resp.json()
            except ValueError:
                answer = None
            for key in self.read_answer(answer if isinstance(answer, dict) else None, chunk):
                self.expected[key] = shows

    def read_answer(self, answer: dict | None, keys: Sequence[str]) -> list[str]:
        """Settle what the answer's BODY says about `keys`, and return the ones it
        says were written, which are then confirmed by reading the list back.

        THE BODY NAMES ONLY WHAT IT COULD NOT MATCH; what it did, it counts. So
        a title not named as unmatched is taken as written until the read-back
        says otherwise. An add whose answer counts some of its shows as ALREADY
        LISTED cannot say which — the read before the write found none of them
        listed, so the viewer listed one in between — and every title of that
        write is then recorded as listed before, because an undo must never
        remove a show the viewer listed themselves.
        """
        if answer is None:
            logger.warning("Trakt watchlist: a write answered with no usable body")
            for key in keys:
                self.settle(key, WatchlistOutcome.FAILED)
            return []
        block = answer.get("not_found")
        missing = {_echo_key(item.get("ids")) for item in
                   ((block.get("shows") if isinstance(block, dict) else None) or [])
                   if isinstance(item, dict)}
        written = []
        for key in keys:
            if _echo_key(self.sent[key]) in missing:
                self.settle(key, WatchlistOutcome.NOT_FOUND)
            else:
                written.append(key)
        if _count(answer, "existing"):
            self.maybe_listed.update(written)
            if not _count(answer, "added"):
                self.nothing_added.update(written)
        return written

    async def confirm(self, settings: Settings) -> None:
        """Read the list back once and settle every title a body said was
        written: WRITTEN only where the list now shows what `write` expected. A
        read-back that cannot be made confirms nothing."""
        if not self.expected:
            return
        try:
            after = await _read(settings)
        except transport.TraktError as exc:
            logger.warning("Trakt watchlist: could not read the list back (%s); "
                           "%d write(s) left unconfirmed", exc, len(self.expected))
            for key in self.expected:
                self.settle(key, WatchlistOutcome.FAILED)
            return
        for key, wanted in self.expected.items():
            held = self.before[key]
            title = self.titles[key]
            trakt_id = self.sent[key].get("trakt")
            if trakt_id is not None:
                title = title._replace(ids={**title.ids, "trakt": trakt_id})
            now = _holding(after.find(title)[0], title)
            outcome = WatchlistOutcome.WRITTEN if now.status == wanted else WatchlistOutcome.FAILED
            if outcome is WatchlistOutcome.FAILED:
                logger.warning("Trakt watchlist: %s answered as written but the list "
                               "shows %r, not %r", key, now.status, wanted)
            before = held.status
            if key in self.maybe_listed and outcome is WatchlistOutcome.WRITTEN:
                before = LISTED
                if key in self.nothing_added:
                    outcome = WatchlistOutcome.ALREADY
            self.results[key] = WatchlistResult(outcome, before, now.status,
                                                now.service_id or held.service_id)


async def put(settings: Settings,
              titles: Sequence[WatchlistTitle]) -> dict[str, WatchlistResult]:
    """Every title onto the watchlist. A title already listed is not sent."""
    if not titles:
        return {}
    batch = _Batch(titles, await _read(settings))
    sending = []
    for key, held in batch.before.items():
        if held.status == LISTED:
            batch.settle(key, WatchlistOutcome.ALREADY)
        elif batch.unsendable(key):
            batch.settle(key, WatchlistOutcome.NOT_FOUND)
        else:
            sending.append(key)
    await batch.write(settings, _ADD, sending, LISTED)
    await batch.confirm(settings)
    return batch.results


async def drop(settings: Settings,
               titles: Sequence[WatchlistTitle]) -> dict[str, WatchlistResult]:
    """Every title off the watchlist, because the viewer gave up on it. A title
    not listed is left alone: there is nothing to take off, and nothing here
    ever adds a show in order to drop it."""
    if not titles:
        return {}
    batch = _Batch(titles, await _read(settings))
    sending = []
    for key, held in batch.before.items():
        if held.status == NOT_HELD:
            batch.settle(key, WatchlistOutcome.ALREADY)
        elif batch.unsendable(key):
            batch.settle(key, WatchlistOutcome.NOT_FOUND)
        else:
            sending.append(key)
    await batch.write(settings, _REMOVE, sending, NOT_HELD)
    await batch.confirm(settings)
    return batch.results


async def undo(settings: Settings, rows: Sequence[WatchlistUndo]) -> dict[str, WatchlistResult]:
    """Take back the tracker's earlier adds, each by what the list held before
    it and what it holds NOW — read here, at the moment of the undo.

      listed before                          -> ALREADY: the viewer's own, stays
      not listed before, listed now          -> removed
      not listed before, not listed now      -> KEPT: the viewer took it off, or
                                                Trakt did at a play; either way
                                                there is nothing of the
                                                tracker's left to take back
    """
    if not rows:
        return {}
    batch = _Batch([row.title for row in rows], await _read(settings))
    sending = []
    for row in rows:
        key = str(row.title.key)
        now = batch.before[key]
        if row.status_before != NOT_HELD:
            batch.settle(key, WatchlistOutcome.ALREADY)
        elif now.status == NOT_HELD:
            batch.settle(key, WatchlistOutcome.KEPT)
        elif batch.unsendable(key):
            batch.settle(key, WatchlistOutcome.NOT_FOUND)
        else:
            sending.append(key)
    await batch.write(settings, _REMOVE, sending, NOT_HELD)
    await batch.confirm(settings)
    return batch.results
