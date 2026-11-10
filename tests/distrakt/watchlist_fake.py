"""A stand-in for a service's list, behaving as Simkl's was measured to.

One status per title (plantowatch, watching, completed, hold, dropped, or
nothing held); a put MOVES a title whatever it held, a drop never adds one, and
an undo follows the rules app/providers/simkl/watchlist.py implements. Every
call is recorded, so a test can say what was SENT as well as what the list
holds afterwards.

It is the port, not the transport, that is faked: the write step's own job is
choosing what to ask and filing the answers, and the transport's bodies are held
by tests/providers/simkl/test_watchlist.py.
"""
from __future__ import annotations

import asyncio

from app.providers.base import (NOT_HELD, SourceUnavailable, WatchlistHolding,
                                WatchlistOutcome, WatchlistResult)

PLAN = "plantowatch"
DROPPED = "dropped"


class FakeUnavailable(SourceUnavailable):
    """The service could not be asked."""


class FakeList:
    def __init__(self, held: dict[str, str] | None = None, *, unknown=(), watched=()):
        # {flat item key: status} for what the service holds.
        self.held: dict[str, str] = dict(held or {})
        self.unknown = set(unknown)      # keys the service names as not found
        self.watched = set(watched)      # keys with something watched there
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = False                # every call raises
        self.unconfirmed = set()         # keys a write "succeeds" on but does nothing
        self.delay = 0.0                 # seconds each call takes
        self.in_flight = asyncio.Event()  # set once any call has begun

    def list_url(self, account_id: str, account_name: str | None) -> str:
        return f"https://lists.example/{account_id}/plantowatch/"

    async def _call(self, verb: str, keys: list[str]) -> None:
        self.in_flight.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise FakeUnavailable("The fake service is down.")
        self.calls.append((verb, keys))

    def sent(self, verb: str) -> list[str]:
        """Every key `verb` was called with, in order, across calls."""
        return [key for called, keys in self.calls if called == verb for key in keys]

    def _result(self, key, outcome, before) -> WatchlistResult:
        return WatchlistResult(outcome, before, self.held.get(key, NOT_HELD), f"svc-{key}")

    async def holdings(self, settings, titles):
        await self._call("holdings", [str(t.key) for t in titles])
        return {str(t.key): WatchlistHolding(self.held.get(str(t.key), NOT_HELD),
                                             str(t.key) in self.watched, None)
                for t in titles}

    def _move(self, key: str, to: str, skip) -> WatchlistResult:
        before = self.held.get(key, NOT_HELD)
        if skip(before):
            return self._result(key, WatchlistOutcome.ALREADY, before)
        if key in self.unknown:
            return self._result(key, WatchlistOutcome.NOT_FOUND, before)
        if key in self.unconfirmed:
            return self._result(key, WatchlistOutcome.FAILED, before)
        self.held[key] = to
        return self._result(key, WatchlistOutcome.WRITTEN, before)

    async def put(self, settings, titles):
        await self._call("put", [str(t.key) for t in titles])
        return {str(t.key): self._move(str(t.key), PLAN, lambda held: held == PLAN)
                for t in titles}

    async def drop(self, settings, rows):
        # Every drop is told what the tracker's record says was held before
        # (WatchlistDrop); a status service like this one moves the title to its
        # given-up status whoever listed it, so it is only recorded.
        self.drop_rows = [*getattr(self, "drop_rows", []), *rows]
        await self._call("drop", [str(row.title.key) for row in rows])
        return {str(row.title.key): self._move(str(row.title.key), DROPPED,
                                               lambda held: held in (NOT_HELD, DROPPED))
                for row in rows}

    async def undo(self, settings, rows):
        await self._call("undo", [str(row.title.key) for row in rows])
        out = {}
        for row in rows:
            key, before = str(row.title.key), row.status_before
            now = self.held.get(key, NOT_HELD)
            back = "watching" if before == "completed" else before
            if now in (back, before):
                out[key] = self._result(key, WatchlistOutcome.ALREADY, now)
            elif now == NOT_HELD or (before == NOT_HELD and key in self.watched):
                out[key] = self._result(key, WatchlistOutcome.KEPT, now)
            elif before == NOT_HELD:
                del self.held[key]
                out[key] = self._result(key, WatchlistOutcome.WRITTEN, now)
            else:
                self.held[key] = back
                out[key] = self._result(key, WatchlistOutcome.WRITTEN, now)
        return out
