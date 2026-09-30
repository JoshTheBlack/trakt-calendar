"""Writing the tracker's shows to a person's Simkl `plantowatch` list, and taking
those writes back — app/providers/base.py's WatchlistPort, for Simkl.

SIMKL HAS NO WATCHLIST, IT HAS STATUSES. Every title in a library sits in
exactly one of watching, plantowatch, hold, completed or dropped, and
`POST /sync/add-to-list` MOVES a title to the status each item names. So a put
is a move from wherever the viewer had it, dropping is a move to `dropped`
(which keeps the viewer's history), and the only way to take a title out of a
library at all is `POST /sync/history/remove`, which takes its history with it.

WHAT WAS MEASURED against the live service, 2026-09-30, on two titles the owner
set aside for it, and what each rule here rests on:
  - `to` is PER ITEM. A top-level `to` is Simkl's legacy form and is refused
    with 400 `empty_field` "Missed \"to\" parameter".
  - Every write answers 201 with a body: add-to-list says `added` and
    `not_found`, each item echoing the ids exactly as they were sent; a
    history removal says `deleted` (counts) and `not_found`.
  - A title already in the status asked for comes back in `added` exactly as a
    real move does. THE BODY CANNOT SAY "ALREADY THERE" — only a read before the
    write can, which is one reason every write here reads first.
  - Anime is accepted under `shows` as well as `anime` on add-to-list, and
    Simkl's documentation says a history removal SILENTLY IGNORES a top-level
    `anime` array — so everything here is sent under `shows`.
  - A move leaves the title's `added_to_watchlist_at` alone, and a removal
    takes it out of every list.
  - A move between plantowatch, hold, dropped and watching leaves the viewer's
    episode history exactly as it was. A move to COMPLETED does not: it marks
    every unwatched episode watched, dated now (see `_put_back_as`).
  - One write of 100 titles was accepted whole, each way (see WRITE_CHUNK).

WRITES ARE NEVER CACHED and every read of the library is `private=True`, which
the transport's cached_get turns into "never read from or written to the shared
cache": the answer is one person's library. Writes go through the transport's
sender, which paces POSTs to Simkl's one per second, on the sequential pool, so
there is never more than one write in flight — Simkl holds a per-user write
lock and answers a second write inside it with 400 `RATE_LIMIT`.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import quote, urlencode

from ...config import Settings
from ..base import (MATCH_SOURCES, NOT_HELD, Media, WatchlistHolding, WatchlistOutcome,
                    WatchlistResult, WatchlistTitle, WatchlistUndo, resolve_key)
from . import _ids, transport

logger = logging.getLogger(__name__)

# The status the tracker's shows are written to: the list Simkl calls "Plan to
# Watch", and the one a media server importing a Simkl watchlist reads.
PLAN_TO_WATCH = "plantowatch"
# Where a show the viewer gave up on goes. Simkl's own given-up status, so the
# viewer's history stays with the title.
DROPPED = "dropped"
COMPLETED = "completed"
WATCHING = "watching"

# The catalogues a library read carries shows under. Films are never written.
_SHOW_KEYS = ("shows", "anime")

# The ids a write may name a title by, as Simkl's add-to-list documents them,
# and which of them it wants as numbers.
_SENDABLE = ("simkl", *MATCH_SOURCES)
_NUMERIC = frozenset({"simkl", "tmdb", "mal"})


def list_url(account_id: str) -> str:
    """The account's television Plan to Watch page on simkl.com.

    THE SHAPE IS NOT MEASURED: simkl.com answers every automated visit with a
    Cloudflare challenge, headless browsers included, and Simkl's documentation
    publishes no web address for a user's lists. The account id is the numeric
    one Simkl's /users/settings states, which is what a linked identity stores.
    """
    return f"https://simkl.com/{quote(str(account_id), safe='')}/tv/{PLAN_TO_WATCH}/"


# ---------------------------------------------------------------------------
# reading the library
# ---------------------------------------------------------------------------

class _Index:
    """One read of the library, indexed the two ways a title can be found in it:
    by Simkl's own id, and by the shared identity the tracker files titles
    under. The second can name several items — Simkl files each anime season as
    its own title, and several share one TMDB id — so it keeps every one."""

    def __init__(self, document) -> None:
        self.by_simkl: dict[str, dict] = {}
        self.by_key: dict[str, list[dict]] = {}
        for list_key in _SHOW_KEYS:
            for item in (document.get(list_key) if isinstance(document, dict) else None) or []:
                if not isinstance(item, dict):
                    continue
                ids = _ids.normalize((item.get("show") or {}).get("ids") or {})
                if ids.get("simkl") not in (None, ""):
                    self.by_simkl[str(ids["simkl"])] = item
                key = resolve_key(Media.SHOW, ids)
                if key is not None:
                    self.by_key.setdefault(str(key), []).append(item)

    def find(self, title: WatchlistTitle) -> tuple[dict | None, bool]:
        """(the library item for `title` or None, whether the match was
        AMBIGUOUS). Simkl's own id wins outright; without one, a shared id that
        names more than one library item cannot say which of them is meant."""
        simkl_id = title.ids.get("simkl")
        if simkl_id not in (None, ""):
            return self.by_simkl.get(str(simkl_id)), False
        found = self.by_key.get(str(title.key)) or []
        return (found[0], False) if len(found) == 1 else (None, len(found) > 1)


def _holding(item: dict | None, title: WatchlistTitle) -> WatchlistHolding:
    if item is None:
        simkl_id = title.ids.get("simkl")
        return WatchlistHolding(NOT_HELD, False,
                                None if simkl_id in (None, "") else str(simkl_id))
    ids = _ids.normalize((item.get("show") or {}).get("ids") or {})
    try:
        watched = int(item.get("watched_episodes_count") or 0) > 0
    except (TypeError, ValueError):
        # A count this module cannot read is not evidence of nothing watched,
        # and "nothing watched" is what licenses a removal. So it reads as
        # watched.
        watched = True
    return WatchlistHolding(
        status=str(item.get("status") or NOT_HELD),
        watched=watched or bool(item.get("last_watched_at")),
        service_id=None if ids.get("simkl") in (None, "") else str(ids["simkl"]))


async def _read(settings: Settings) -> _Index:
    """The whole library, every list at once (`/sync/all-items/` with neither a
    catalogue nor a status). Raises SimklError when it cannot be read.

    A BODY THAT IS NOT A DOCUMENT READS AS AN EMPTY LIBRARY, and that is safe
    in every direction it is used: an empty read can only ever make a title look
    NOT HELD, which makes a put send it (harmless — putting it is the point), a
    drop leave it alone, and an undo leave it alone. Nothing is ever REMOVED on
    the strength of an empty read, because a removal needs the read to show the
    title held with nothing watched.
    """
    document = await transport.cached_get(
        transport.sync_client(), settings, "sync/all-items/", {},
        pool=transport.SYNC_POOL, private=True, raise_errors=True,
        app=transport.APP_NAME_TRACKER)
    return _Index(document if isinstance(document, dict) else {})


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
    """The ids a write names `title` by. SIMKL'S OWN ID ALONE when it is known —
    it names exactly one title, where a shared id may name several — and every
    shared id the tracker holds otherwise."""
    if service_id:
        return {"simkl": int(service_id) if service_id.isdigit() else service_id}
    out = {}
    for name in _SENDABLE:
        value = title.ids.get(name)
        if value in (None, ""):
            continue
        text = str(value).strip()
        out[name] = int(text) if name in _NUMERIC and text.isdigit() else text
    return out


def _echo_key(ids) -> tuple:
    """A sent id map and Simkl's echo of it, as one comparable value. The echo
    repeats the ids exactly as sent, so this is how an answer's item is tied
    back to the title it is about."""
    if not isinstance(ids, dict):
        return ()
    return tuple(sorted((str(name), str(value)) for name, value in ids.items()))


async def _post(settings: Settings, path: str, body: dict) -> dict | None:
    """One write, and its answer's body — or None when the body is not a
    document, which is never read as success. Raises SimklError when Simkl could
    not be reached or refused the write outright (any non-2xx, including a 400
    RATE_LIMIT from the per-user write lock; that is not retried here, because a
    caller whose write failed tries again on its own schedule)."""
    url = (f"{transport.API_BASE}/{path}?"
           f"{urlencode(transport.api_params(settings, app=transport.APP_NAME_TRACKER))}")
    resp = await transport.send_or_unavailable(
        transport.sync_client(), "POST", url, pool=transport.SYNC_POOL,
        headers=transport.api_headers(settings, private=True), json=body)
    if not 200 <= resp.status_code < 300:
        logger.warning("Simkl POST /%s -> HTTP %s: %s", path, resp.status_code, resp.text[:200])
        raise transport.SimklError(f"Simkl refused POST /{path} (HTTP {resp.status_code}).",
                                   resp.status_code)
    try:
        answer = resp.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        logger.warning("Simkl POST /%s answered HTTP %s with no usable body", path,
                       resp.status_code)
        return None
    return answer


def _echoed(answer: dict | None, section: str) -> set[tuple]:
    """Every item an answer lists under `section` ("added", "not_found"), as
    echo keys. Simkl files anime under `shows` in its answers."""
    block = (answer or {}).get(section)
    if not isinstance(block, dict):
        return set()
    return {_echo_key(item.get("ids")) for item in block.get("shows") or []
            if isinstance(item, dict)}


# HOW MANY TITLES ONE WRITE CARRIES. Measured on the live service: one
# add-to-list of 100 shows answered 201 in 6.0 s with all 100 added and read
# back, and one history removal of the same 100 deleted all of them — no limit
# showed. But the transport gives a request 30 s in all, and a write that
# outgrew that would be answered to nobody, so a larger batch goes out in
# chunks of the size that was proven. The per-second pacing of those chunks
# is the transport's.
WRITE_CHUNK = 100

_REMOVE = "sync/history/remove"


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
        self.expected: dict[str, frozenset[str]] = {}
        # Whether any write has been answered yet — see `write`.
        self.posted = False

    async def write(self, settings: Settings, path: str,
                    entries: Sequence[tuple[str, dict]], shows) -> None:
        """Send `entries` — (title key, the item that names it) — in chunks of
        WRITE_CHUNK, reading each answer's body, and note what the read-back
        must then show (`shows(key)`).

        A WRITE THAT FAILS BEFORE ANY HAS BEEN ANSWERED RAISES: nothing is known
        to have happened, which is "could not ask". Once one has been answered,
        a later one failing fails only its own titles, so the answer about what
        DID happen is not lost.
        """
        for start in range(0, len(entries), WRITE_CHUNK):
            chunk = entries[start:start + WRITE_CHUNK]
            keys = [key for key, _item in chunk]
            try:
                answer = await _post(settings, path, {"shows": [item for _key, item in chunk]})
            except transport.SimklError:
                if not self.posted:
                    raise
                for key in keys:
                    self.settle(key, WatchlistOutcome.FAILED)
                continue
            self.posted = True
            # A removal's answer lists what it could not match and COUNTS what
            # it deleted, so an item it does not name as unmatched is one the
            # count covers — the read-back is what says whether it really went.
            if path == _REMOVE and answer is not None:
                answer = {"added": {"shows": [{"ids": self.sent[key]} for key in keys]},
                          "not_found": answer.get("not_found")}
            for key in self.read_answer(answer, keys):
                self.expected[key] = shows(key)

    def settle(self, key: str, outcome: WatchlistOutcome) -> None:
        held = self.before[key]
        self.results[key] = WatchlistResult(outcome, held.status, held.status, held.service_id)

    def send(self, key: str) -> dict:
        ids = _sendable_ids(self.titles[key], self.before[key].service_id)
        self.sent[key] = ids
        return ids

    def unsendable(self, key: str) -> bool:
        """A title with no Simkl id of its own whose shared id names several
        library items: no write could say which of them is meant, and a guess
        would move one of the viewer's titles that is not this one."""
        if key in self.ambiguous or not self.send(key):
            self.sent.pop(key, None)
            logger.info("Simkl watchlist: not writing %s — no id names exactly one "
                        "Simkl title", key)
            return True
        return False

    def _by_what_was_sent(self, key: str) -> WatchlistTitle:
        """The title as the read-back must look for it: by the Simkl id the write
        named where it named one, so a title first found by a shared id is
        confirmed against the one Simkl title that was actually written."""
        title = self.titles[key]
        simkl_id = self.sent.get(key, {}).get("simkl")
        return title if simkl_id is None else title._replace(ids={**title.ids, "simkl": simkl_id})

    def read_answer(self, answer: dict | None, keys) -> list[str]:
        """Settle what the answer's BODY says about `keys`, and return the ones it
        says were written, which are then confirmed by reading the list back."""
        if answer is None:
            for key in keys:
                self.settle(key, WatchlistOutcome.FAILED)
            return []
        added = _echoed(answer, "added")
        missing = _echoed(answer, "not_found")
        written = []
        for key in keys:
            echo = _echo_key(self.sent[key])
            if echo in missing:
                self.settle(key, WatchlistOutcome.NOT_FOUND)
            elif echo in added:
                written.append(key)
            else:
                self.settle(key, WatchlistOutcome.FAILED)
        return written

    async def confirm(self, settings: Settings) -> None:
        """Read the library back once and settle every title a body said was
        written: WRITTEN only where the list now shows what `write` expected.
        A read-back that cannot be made confirms nothing."""
        expected = self.expected
        if not expected:
            return
        try:
            after = await _read(settings)
        except transport.SimklError as exc:
            logger.warning("Simkl watchlist: could not read the library back (%s); "
                           "%d write(s) left unconfirmed", exc, len(expected))
            for key in expected:
                self.settle(key, WatchlistOutcome.FAILED)
            return
        for key, wanted in expected.items():
            held = self.before[key]
            now = _holding(after.find(self._by_what_was_sent(key))[0], self.titles[key])
            ok = now.status in wanted
            if not ok:
                logger.warning("Simkl watchlist: %s answered as written but the list "
                               "shows %r, not %s", key, now.status, sorted(wanted))
            self.results[key] = WatchlistResult(
                WatchlistOutcome.WRITTEN if ok else WatchlistOutcome.FAILED,
                held.status, now.status, now.service_id or held.service_id)


async def _move(settings: Settings, titles: Sequence[WatchlistTitle], to: str,
                skip) -> dict[str, WatchlistResult]:
    """Move every title to `to`, except those `skip(holding)` says need nothing
    (ALREADY). One read, one write per WRITE_CHUNK titles, one read."""
    if not titles:
        return {}
    batch = _Batch(titles, await _read(settings))
    entries = []
    for key, held in batch.before.items():
        if skip(held):
            batch.settle(key, WatchlistOutcome.ALREADY)
        elif batch.unsendable(key):
            batch.settle(key, WatchlistOutcome.NOT_FOUND)
        else:
            entries.append((key, {"to": to, "ids": batch.sent[key]}))
    await batch.write(settings, "sync/add-to-list", entries, lambda _key: frozenset({to}))
    await batch.confirm(settings)
    return batch.results


async def put(settings: Settings,
              titles: Sequence[WatchlistTitle]) -> dict[str, WatchlistResult]:
    """Every title to Plan to Watch, whatever the viewer had it as — watching,
    completed, on hold or dropped all move. A title already there is not sent."""
    return await _move(settings, titles, PLAN_TO_WATCH,
                       skip=lambda held: held.status == PLAN_TO_WATCH)


async def drop(settings: Settings,
               titles: Sequence[WatchlistTitle]) -> dict[str, WatchlistResult]:
    """Every title to Dropped. A title the library does not hold is left alone —
    moving it would ADD it — as is one already dropped."""
    return await _move(settings, titles, DROPPED,
                       skip=lambda held: held.status in (NOT_HELD, DROPPED))


def _put_back_as(status_before: str) -> str:
    """The status an undo moves a title to, given what it was before.

    NEVER `completed`. MEASURED on the live service: moving a show to completed
    MARKS EVERY UNWATCHED EPISODE WATCHED, dated the moment of the move — a
    three-episode history became thirty-six — and moving it on to watching
    takes those back off again. A show the viewer had finished can have gained
    episodes since (a new season is exactly what brings it back onto the
    tracker), so putting it back to completed would invent viewing dated today,
    which the tracker would then read as plays. It goes back to watching, which
    keeps every real play and invents none; the viewer can mark it finished
    themselves.
    """
    return WATCHING if status_before == COMPLETED else status_before


def _restored(status_before: str) -> frozenset[str]:
    """What the list may show once a title is back: the status it was put to,
    or — for a finished show — the status it had, should Simkl itself file a
    show with nothing left unwatched as completed again."""
    return frozenset({_put_back_as(status_before), status_before})


async def undo(settings: Settings, rows: Sequence[WatchlistUndo]) -> dict[str, WatchlistResult]:
    """Take back the tracker's earlier writes, each by what the library held
    before it and what it holds NOW — read here, at the moment of the undo.

      held nothing before, holds it now, nothing watched  -> removed
      held nothing before, something watched now          -> KEPT
      held a status before, holds another now             -> moved back to it
                                                             (completed: to
                                                             watching, above)
      holds nothing now, having held something before     -> KEPT: the viewer
                                                             took it out themselves
      already as it was                                   -> ALREADY

    The moves and the removals are separate writes (two endpoints), each
    chunked as `_Batch.write` describes.
    """
    if not rows:
        return {}
    batch = _Batch([row.title for row in rows], await _read(settings))
    wanted = {str(row.title.key): row.status_before for row in rows}
    moves, removals = [], []
    for key, before in wanted.items():
        now = batch.before[key]
        if now.status in _restored(before) or (before == NOT_HELD and now.status == NOT_HELD):
            batch.settle(key, WatchlistOutcome.ALREADY)
        elif now.status == NOT_HELD or (before == NOT_HELD and now.watched):
            batch.settle(key, WatchlistOutcome.KEPT)
        elif batch.unsendable(key):
            batch.settle(key, WatchlistOutcome.NOT_FOUND)
        elif before == NOT_HELD:
            removals.append((key, {"ids": batch.sent[key]}))
        else:
            moves.append((key, {"to": _put_back_as(before), "ids": batch.sent[key]}))
    await batch.write(settings, "sync/add-to-list", moves, lambda key: _restored(wanted[key]))
    await batch.write(settings, _REMOVE, removals, lambda _key: frozenset({NOT_HELD}))
    await batch.confirm(settings)
    return batch.results
