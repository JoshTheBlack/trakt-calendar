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

import contextvars
import logging
from collections.abc import Callable

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
