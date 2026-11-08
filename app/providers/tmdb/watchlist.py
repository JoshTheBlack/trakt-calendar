"""Writing the tracker's shows to a person's TMDB watchlist, and taking those
writes back — app/providers/base.py's WatchlistPort, for TMDB.

TMDB'S WATCHLIST IS A PLAIN LIST, like Trakt's: a show is on it or it is not, so
what the service holds is LISTED or NOT_HELD, a put adds and an undo removes, and
taking a show off takes nothing else with it (its rating and favourite are
separate records at TMDB) — so no rule here turns on what has been watched.

GIVING UP ON A SHOW REMOVES IT ONLY IF THE TRACKER PUT IT THERE. The owner's rule
for this list: a show the viewer had on their TMDB watchlist before the tracker
wrote it stays when they abandon it on the tracker; one the tracker added comes
off. Which is which is the tracker's own record (WatchlistDrop.status_before),
since the list reads the same whoever added a title.

WHAT WAS MEASURED against the live service, 2026-11-08, on the owner's account
(each write undone and the list read back empty), and what each rule rests on:
  - The write is version 3's `POST /3/account/{account}/watchlist` with
    {media_type, media_id, watchlist}, and it takes the v4 USER token as its
    bearer — no v3 session is made or kept.
  - Every write answers with a body: TMDB `status_code` 1 for an add, 12
    ("updated") for an add of a title already listed, 13 ("deleted") for a
    removal — AND 13 FOR A REMOVAL OF A TITLE THAT WAS NOT LISTED, so the body
    cannot say "already gone"; only the read before the write can. An id TMDB has
    no title for answers 404 with `success: false`, status_code 34.
  - THE ACCOUNT IN THE ADDRESS IS NOT WHAT DECIDES: a write addressed to account
    1 answered 13 like any other. The token says whose list it is. That is one
    more reason no answer is taken as done until a read of the list shows it.
  - There is no batch write: one request per show. The read is account.py's,
    every page, current the moment a write answers.

NOTHING HERE IS CACHED, either way: every request goes through
transport.send_private, with the viewer's own token.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import quote

from ...config import Settings
from ..base import (NOT_HELD, Media, WatchlistDrop, WatchlistHolding, WatchlistOutcome,
                    WatchlistResult, WatchlistTitle, WatchlistUndo)
from . import account, transport

logger = logging.getLogger(__name__)

# What a watchlist read says of a show that is on it: a plain list has one status.
LISTED = "listed"

# TMDB's own codes in a write's answer body (see the module docstring).
_ADDED = 1
_ALREADY_LISTED = 12
_DELETED = 13
_NO_SUCH_TITLE = 34


def list_url(account_id: str, account_name: str | None) -> str | None:
    """The account's TV watchlist on themoviedb.org. TMDB addresses a person's
    pages by their USERNAME — the name a linked identity keeps as its display
    name, read from TMDB's own account answer at link time — so with none held
    there is no address to give. A username changed on TMDB since the link reads
    as a dead link until the next link refreshes it; it is a link, it gates
    nothing."""
    name = (account_name or "").strip()
    if not name:
        return None
    return f"https://www.themoviedb.org/u/{quote(name, safe='')}/watchlist/tv"


def _tmdb_id(title: WatchlistTitle) -> int | None:
    """TMDB's own id for `title` — which is also its tmdb id, the one TMDB keys
    a watchlist entry by. None when the tracker holds none: no shared id names a
    TMDB title exactly enough to write, and a guess would add somebody else's."""
    value = str(title.ids.get("tmdb") or "").strip()
    return int(value) if value.isdigit() else None


class _Index:
    """One read of the TV watchlist, by TMDB id."""

    def __init__(self, items: Sequence[dict]) -> None:
        self.listed: set[int] = set()
        for item in items:
            try:
                self.listed.add(int(item.get("id")))
            except (TypeError, ValueError):
                continue

    def holding(self, title: WatchlistTitle) -> WatchlistHolding:
        """What the list holds for `title`. `watched` is always False: it says
        whether taking a title away could take viewing history with it, and
        taking a show off this list cannot."""
        tmdb = _tmdb_id(title)
        service_id = None if tmdb is None else str(tmdb)
        return WatchlistHolding(LISTED if tmdb in self.listed else NOT_HELD, False, service_id)


async def _read(settings: Settings) -> _Index:
    """Every show on the viewer's TV watchlist, every page. Raises TmdbError
    when it cannot be read whole."""
    return _Index(await account.every_page(settings, Media.SHOW, "watchlist"))


async def holdings(settings: Settings,
                   titles: Sequence[WatchlistTitle]) -> dict[str, WatchlistHolding]:
    if not titles:
        return {}
    index = await _read(settings)
    return {str(title.key): index.holding(title) for title in titles}


class _Batch:
    """One verb's work over a batch: what was read before, each title's answer
    as it becomes known, and what the read-back must show for each title a
    write's body said was done."""

    def __init__(self, titles: Sequence[WatchlistTitle], before: _Index) -> None:
        self.titles = {str(title.key): title for title in titles}
        self.before = {key: before.holding(title) for key, title in self.titles.items()}
        self.results: dict[str, WatchlistResult] = {}
        self.expected: dict[str, str] = {}
        # Titles an add found listed although the read before said not: the
        # viewer listed it in between, so it counts as theirs (see `write`).
        self.listed_meanwhile: set[str] = set()
        self.posted = False

    def settle(self, key: str, outcome: WatchlistOutcome) -> None:
        held = self.before[key]
        self.results[key] = WatchlistResult(outcome, held.status, held.status, held.service_id)

    def unsendable(self, key: str) -> bool:
        if _tmdb_id(self.titles[key]) is None:
            logger.info("TMDB watchlist: not writing %s — the tracker holds no TMDB id "
                        "for it", key)
            return True
        return False

    async def write(self, settings: Settings, keys: Sequence[str], *, listed: bool) -> None:
        """One write per title, each answer's BODY read, and what the read-back
        must then show noted.

        A WRITE THAT FAILS BEFORE ANY HAS BEEN ANSWERED RAISES: nothing is known
        to have happened, which is "could not ask" — a revoked link (401) above
        all. Once one has been answered, a later one failing fails only itself.
        """
        account_id = settings.tmdb_account_id.strip()
        url = f"{transport.API_BASE}/account/{quote(account_id, safe='')}/watchlist"
        for key in keys:
            body = {"media_type": "tv", "media_id": _tmdb_id(self.titles[key]),
                    "watchlist": listed}
            try:
                resp = await transport.send_private(
                    "POST", url, "account/{account}/watchlist",
                    token=settings.tmdb_user_token, body=body)
                if resp.status_code not in (200, 201, 404):
                    logger.warning("TMDB watchlist write for %s -> HTTP %s", key,
                                   resp.status_code)
                    raise account.refused(resp.status_code, "a watchlist write")
            except transport.TmdbError:
                if not self.posted:
                    raise
                self.settle(key, WatchlistOutcome.FAILED)
                continue
            self.posted = True
            self._read_answer(key, resp, listed=listed)

    def _read_answer(self, key: str, resp, *, listed: bool) -> None:
        """Settle what one answer's BODY says, or note what the read-back must
        show where it says the write was done."""
        try:
            answer = resp.json()
        except ValueError:
            answer = None
        code = answer.get("status_code") if isinstance(answer, dict) else None
        if resp.status_code == 404 and code == _NO_SUCH_TITLE:
            self.settle(key, WatchlistOutcome.NOT_FOUND)
            return
        done = (answer.get("success") is True if isinstance(answer, dict) else False) and (
            code in ((_ADDED, _ALREADY_LISTED) if listed else (_DELETED,)))
        if not done:
            logger.warning("TMDB watchlist: %s answered HTTP %s, status_code %r", key,
                           resp.status_code, code)
            self.settle(key, WatchlistOutcome.FAILED)
            return
        if listed and code == _ALREADY_LISTED:
            self.listed_meanwhile.add(key)
        self.expected[key] = LISTED if listed else NOT_HELD

    async def confirm(self, settings: Settings) -> None:
        """Read the list back once and settle every title a body said was done:
        WRITTEN only where the list now shows it. A read-back that cannot be
        made confirms nothing."""
        if not self.expected:
            return
        try:
            after = await _read(settings)
        except transport.TmdbError as exc:
            logger.warning("TMDB watchlist: could not read the list back (%s); "
                           "%d write(s) left unconfirmed", exc, len(self.expected))
            for key in self.expected:
                self.settle(key, WatchlistOutcome.FAILED)
            return
        for key, wanted in self.expected.items():
            held = self.before[key]
            now = after.holding(self.titles[key])
            outcome = WatchlistOutcome.WRITTEN if now.status == wanted else WatchlistOutcome.FAILED
            if outcome is WatchlistOutcome.FAILED:
                logger.warning("TMDB watchlist: %s answered as written but the list "
                               "shows %r, not %r", key, now.status, wanted)
            before = held.status
            if key in self.listed_meanwhile and outcome is WatchlistOutcome.WRITTEN:
                # TMDB's own answer says the show was already listed when the add
                # arrived: the viewer listed it since the read before. It is
                # theirs, so an undo or a drop must leave it — and nothing was
                # written.
                before, outcome = LISTED, WatchlistOutcome.ALREADY
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
    await batch.write(settings, sending, listed=True)
    await batch.confirm(settings)
    return batch.results


async def _remove(settings: Settings, wanted: Sequence[tuple[WatchlistTitle, bool]]
                  ) -> dict[str, WatchlistResult]:
    """Take off every title whose flag says the tracker put it there, leaving the
    rest: those still listed are KEPT, and anything not listed is ALREADY — a
    removal never needs sending for a show that is not there, and the answer
    could not say so anyway (13 either way). One read, one write per removal,
    one read."""
    if not wanted:
        return {}
    batch = _Batch([title for title, _ours in wanted], await _read(settings))
    sending = []
    for title, ours in wanted:
        key = str(title.key)
        now = batch.before[key]
        if now.status == NOT_HELD:
            batch.settle(key, WatchlistOutcome.ALREADY)
        elif not ours:
            batch.settle(key, WatchlistOutcome.KEPT)
        elif batch.unsendable(key):
            batch.settle(key, WatchlistOutcome.NOT_FOUND)
        else:
            sending.append(key)
    await batch.write(settings, sending, listed=False)
    await batch.confirm(settings)
    return batch.results


async def drop(settings: Settings, rows: Sequence[WatchlistDrop]) -> dict[str, WatchlistResult]:
    """Take a given-up show off the watchlist ONLY where the tracker's record
    says the tracker put it there (status_before NOT_HELD from a confirmed
    write); a show the viewer had listed first, or one the tracker has no
    confirmed write of, is KEPT where it is."""
    return await _remove(settings, [(row.title, row.status_before == NOT_HELD) for row in rows])


async def undo(settings: Settings, rows: Sequence[WatchlistUndo]) -> dict[str, WatchlistResult]:
    """Take back the tracker's earlier adds, each by what the list held before
    it and what it holds NOW — read here, at the moment of the undo:

      listed before, listed now              -> KEPT: the viewer's own, stays
      not listed before, listed now          -> removed
      not listed now, whatever before        -> ALREADY: nothing left to take back
    """
    return await _remove(settings, [(row.title, row.status_before == NOT_HELD) for row in rows])
