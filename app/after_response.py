"""Work a request owes once its answer has been sent.

A STALE CALENDAR WEEK IS SERVED AT ONCE AND RENEWED BEHIND IT
(app/calendar/cache.py's `ensure_window`). "Behind" used to mean a task started
the moment the read noticed the week was stale — while the very request that
noticed was still assembling its page. The refill's fetch, its parse and the
planning of its store then ran beside the page, on the one event loop and the
worker threads the page's own reads were queued on. MEASURED on a cold start with
stale weeks: the month's shell took 9.7–12.3 s, its row read 5.9–6.9 s of that,
beside the refills it had started itself; with nothing to refill, 3.7 s.

So work a request defers waits until its response has been SENT, every byte of
it, and is started then — each piece starting a task of its own and returning —
so neither this response nor the next request on the same connection waits for
it. Outside a request (the heartbeat, a script) nobody is waiting for an answer,
and the work starts at once.

ONE PLACE, for every route at once: the middleware wraps the whole app, so a
route cannot be written that answers before what it owes is started, nor one
that starts it before it has answered.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# The current request's owed work, or None when no request is being answered.
_owed: contextvars.ContextVar[list[Callable[[], object]] | None] = contextvars.ContextVar(
    "after_response", default=None)


def later(start: Callable[[], object]) -> bool:
    """Call `start` once the current request's response has been sent — or now,
    when no request is being answered. Returns whether it was held. `start` must
    return at once: it starts a task, it does not run one."""
    owed = _owed.get()
    if owed is None:
        start()
        return False
    owed.append(start)
    return True


# The writes `write` has started and not yet seen end — held, because asyncio
# keeps only a weak reference to a task and one nobody holds can be collected
# halfway. Each leaves as it ends, so this holds no more than the writes in flight.
_writes: set[asyncio.Task] = set()


def write(make: Callable[[], Awaitable[object]]) -> bool:
    """`later`, for a WRITE the answer does not depend on: `make()` started, once
    the response has been sent, as a task of its own, held until it ends, and a
    failure logged rather than lost. Returns what `later` does.

    A PAGE'S OWN BOOKKEEPING WAITS FOR ITS ANSWER. A write takes its turn in the
    writer queue behind every write ahead of it (db._writer_queue), so one made
    inside a request made the request wait out a refill's turn as well:
    MEASURED on a copy of dev during a restart's refills, a month's is-new
    commit waited 0.75–1.17 s and a session's slide up to 0.17 s, on pages that
    drew nothing from either."""
    def start() -> None:
        task = asyncio.ensure_future(make())
        _writes.add(task)
        task.add_done_callback(_ended)
    return later(start)


def _ended(task: asyncio.Task) -> None:
    _writes.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("a write owed after a response failed", exc_info=task.exception())


class AfterResponseMiddleware:
    """A pure ASGI middleware, so the response it waits for is the one the
    client receives — its body sent in full, whatever any middleware inside it
    did to it — and nothing it starts is a task of the request's."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        owed: list[Callable[[], object]] = []
        token = _owed.set(owed)
        try:
            await self.app(scope, receive, send)
        finally:
            # Reset first: what is started now starts with nobody to wait for, so
            # anything it defers in turn starts at once rather than into a list
            # nobody will read again.
            _owed.reset(token)
            for start in owed:
                try:
                    start()
                except Exception:  # one owed piece failing must not cost the others
                    logger.exception("work owed after a response could not be started")
