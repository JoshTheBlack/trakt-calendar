"""Where a request's time went, and whether it was even ITS time being spent.

Three instruments, all logging to the "app.perf" logger, all cheap enough to
leave switched on in production:

    span()            how long one labelled block took
    install()         how long each whole request took, with its spans attached
    watch_event_loop  whether the single event loop was blocked while it ran

THE THIRD ONE IS WHY THIS MODULE EXISTS IN ITS CURRENT SHAPE. This app is one
process with one event loop serving every request, so a handler doing synchronous
work — a zlib inflate, an SSL handshake, an SQLite read that skipped its worker
thread, a Pillow encode — stalls every OTHER request in flight for exactly as
long as it runs. From inside those requests that is indistinguishable from being
slow themselves: their own spans all report small numbers and the total is huge.
A stall line printed alongside them is what tells the two apart, and no
per-request timing can substitute for it.

LEVELS ARE CHOSEN SO THIS IS USEFUL WITHOUT LOG_LEVEL=DEBUG. At DEBUG every span
and every request prints, which is the tracing mode and is far too loud for a
running instance. Above that, only the things that were actually SLOW print, at
WARNING, against the thresholds below. An operator with the default LOG_LEVEL=INFO
therefore sees nothing until something takes too long, and then sees the request,
its breakdown, and any loop stall that overlapped it.

Thresholds are environment variables so they can be tightened on a box that is
misbehaving without a rebuild:

    SLOW_REQUEST_MS   default 1500 — a whole request worth reporting
    SLOW_SPAN_MS      default  750 — one labelled block worth reporting
    LOOP_STALL_MS     default  250 — the loop was blocked this long by something

Set any of them to 0 to report every occurrence.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from contextlib import contextmanager

import anyio.to_thread

logger = logging.getLogger("app.perf")


def _env_ms(name: str, default: float) -> float:
    """A millisecond threshold from the environment, falling back on anything
    unparseable rather than refusing to start over a typo in a diagnostic knob."""
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


SLOW_REQUEST_MS = _env_ms("SLOW_REQUEST_MS", 1500.0)
SLOW_SPAN_MS = _env_ms("SLOW_SPAN_MS", 750.0)
LOOP_STALL_MS = _env_ms("LOOP_STALL_MS", 250.0)

# How many spans one request's breakdown keeps. A pathological request — a board
# export resolving hundreds of posters — must not turn its own trace into the
# memory problem, and the first few dozen labels have always already said which
# phase ran long.
MAX_SPANS_PER_REQUEST = 48


# ---------------------------------------------------------------------------
# the request a span belongs to
# ---------------------------------------------------------------------------
# A contextvar rather than a parameter threaded through every call, because the
# span call sites are deep inside the poster chain, the HTTP transport and the
# calendar cache, and none of those has any business taking a request object just
# so a log line can name one.

class _Request:
    """One in-flight request, and the spans that have finished inside it."""

    __slots__ = ("rid", "method", "path", "spans", "t0")

    def __init__(self, rid: str, method: str, path: str):
        self.rid = rid
        self.method = method
        self.path = path
        self.spans: list[tuple[str, float]] = []
        self.t0 = time.perf_counter()

    def record(self, label: str, dt_ms: float) -> None:
        if len(self.spans) < MAX_SPANS_PER_REQUEST:
            self.spans.append((label, dt_ms))

    def breakdown(self) -> str:
        """The spans this request finished, longest first — which is the order the
        question "where did the time go" is actually asked in."""
        if not self.spans:
            return "(no spans)"
        ranked = sorted(self.spans, key=lambda pair: pair[1], reverse=True)
        return " ".join(f"{label}={dt:.0f}ms" for label, dt in ranked)


_current: contextvars.ContextVar[_Request | None] = contextvars.ContextVar(
    "perftrace_request", default=None,
)


def detach() -> None:
    """Stop attributing anything further on this task to the request that spawned
    it.

    asyncio.create_task COPIES the spawning context, so a background job started
    from a request path inherits that request's identity and would go on appending
    spans to it — after the response has been sent, and interleaved with whatever
    else is running. A fire-and-forget job calls this first and its spans then
    stand alone, which is also the honest picture: nobody is waiting on them.
    """
    _current.set(None)


# ---------------------------------------------------------------------------
# spans
# ---------------------------------------------------------------------------

class _Span:
    __slots__ = ("fields",)

    def __init__(self, fields: dict):
        self.fields = fields

    def set(self, **kw) -> None:
        self.fields.update(kw)


@contextmanager
def span(label: str, **fields):
    """Time an awaited block and attribute it to the request that ran it.

        with span("phase", n=len(records)) as sp:
            rows = await fetch()
            sp.set(rows=len(rows))

    The elapsed time covers the awaits, which means it covers any time the block
    spent SUSPENDED while the loop was busy elsewhere — a span reading 900ms is
    not proof that the work inside it took 900ms. That ambiguity is deliberate and
    is what the stall watchdog resolves; a span that tried to subtract other
    people's time would need to know about them.
    """
    sp = _Span(dict(fields))
    t0 = time.perf_counter()
    try:
        yield sp
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if (req := _current.get()) is not None:
            req.record(label, dt_ms)
        extra = " ".join(f"{k}={v}" for k, v in sp.fields.items())
        rid = f"[{req.rid}] " if req is not None else ""
        if dt_ms >= SLOW_SPAN_MS:
            logger.warning("%s⏱ SLOW %-26s %7.1fms  %s", rid, label, dt_ms, extra)
        else:
            logger.debug("%s⏱ %-26s %7.1fms  %s", rid, label, dt_ms, extra)


# ---------------------------------------------------------------------------
# per-request timing
# ---------------------------------------------------------------------------

def _short_id(counter: list[int]) -> str:
    counter[0] = (counter[0] + 1) % 100000
    return f"r{counter[0]:05d}"


class RequestTimingMiddleware:
    """Times every request end to end and gives it an id the spans inside it
    carry.

    RAW ASGI rather than BaseHTTPMiddleware: this wraps every request on the
    instance including the streamed static files, and BaseHTTPMiddleware puts a
    task and a queue around each response body to do it — measurable overhead to
    measure overhead with, and a second place for a slow response to behave
    differently. This one touches the response only to read its status.

    OUTERMOST OF THE STACK, so what it measures is what the client waited for:
    the authorization gates, the route, the template render, and gzip. A timing
    middleware installed inside those would exonerate whichever of them was
    actually slow.
    """

    def __init__(self, app):
        self.app = app
        self._counter = [0]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        req = _Request(_short_id(self._counter), scope.get("method", "?"), path)
        token = _current.set(req)
        status = 0

        async def _send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            _current.reset(token)
            dt_ms = (time.perf_counter() - req.t0) * 1000.0
            if dt_ms >= SLOW_REQUEST_MS:
                logger.warning(
                    "[%s] SLOW %s %s -> %s in %.0fms | %s",
                    req.rid, req.method, path, status or "-", dt_ms, req.breakdown(),
                )
            else:
                logger.debug(
                    "[%s] %s %s -> %s in %.0fms", req.rid, req.method, path, status or "-", dt_ms,
                )


def install(app) -> None:
    """Add the request timer as the outermost middleware.

    Starlette reverses registration order, so this must be added LAST — see the
    class docstring for why being outermost is the whole point.
    """
    app.add_middleware(RequestTimingMiddleware)


# ---------------------------------------------------------------------------
# the event-loop stall watchdog
# ---------------------------------------------------------------------------

# How often the watchdog wakes to check. Short enough to catch a stall inside a
# single slow request, long enough that the check itself is free — the whole cost
# is one timer and one subtraction per interval.
_WATCH_INTERVAL = 0.25


async def watch_event_loop(*, interval: float = _WATCH_INTERVAL) -> None:
    """Report every time the event loop was blocked longer than LOOP_STALL_MS.

    HOW IT KNOWS: a sleep that was asked for 250ms and came back after 3s did not
    oversleep — the loop could not get back to it, because something synchronous
    was running on it. The overshoot IS the block, measured from the one vantage
    point that can see it, and it is the difference between "this request is slow"
    and "this request was starved by someone else's".

    THE THREAD-POOL NUMBERS RIDE ALONG because the commonest cause here is the
    opposite mistake: work correctly pushed to a worker thread, but more of it
    than there are threads, so callers queue on the limiter instead. Borrowed at
    the ceiling next to a stall says the pool is the constraint; borrowed near
    zero next to a stall says something ran on the loop that should not have.

    Runs for the process lifetime and is cancelled with it. Nothing depends on it,
    and it must never be able to fail the app it is observing.
    """
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        lag_ms = (time.perf_counter() - t0 - interval) * 1000.0
        if lag_ms < LOOP_STALL_MS:
            continue
        try:
            limiter = anyio.to_thread.current_default_thread_limiter()
            threads = f"{limiter.borrowed_tokens}/{limiter.total_tokens}"
        except Exception:
            # The numbers are context on a line that is already worth printing;
            # failing to read them must not cost us the stall report itself.
            threads = "?"
        logger.warning(
            "⚠ event loop blocked %.0fms (worker threads busy %s) — a request was "
            "running synchronous work on the loop, or waiting for a worker",
            lag_ms, threads,
        )
