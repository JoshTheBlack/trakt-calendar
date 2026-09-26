"""One pooled httpx client per outbound service, and the class that owns them.

WHY THIS IS A CLASS RATHER THAN A FUNCTION PER SERVICE. Constructing an
httpx.AsyncClient is not cheap and, worse, it is not ASYNC: building the client's
SSL context reads and parses the system trust store inline, on whatever thread
asked — which for a route handler is the event loop. A module that builds a fresh
client per call therefore blocks every OTHER request in flight, briefly, every
time it talks to anything. Reusing one client per service removes that cost
entirely and keeps the connections warm besides.

The Trakt transport already knew this and had its own pooled client. Everything
else in the app did not, and app/media/tmdb.py had resorted to importing Trakt's — a
provider package handing out sockets to modules that are not providers. This
module is that one good implementation, extracted so there is exactly one copy of
the awkward part (see LOOP-KEYED below) and no reason for the next service to
invent a second.

INSTANCES LIVE WITH THEIR OWNERS, NOT HERE. This module declares no pools and
knows no service names: each provider or integration declares its own at module
level, with its own limits and timeout, in the file that knows WHY those numbers
are what they are. Adding a service should touch that service's module and
nothing else — in particular it must never require editing this file, which is
kernel and must not accumulate knowledge of features.

A POOL IS NOT A RATE LIMIT, and the two are separate on purpose. `max_connections`
bounds how many sockets may be open to a service at once — a resource question.
`concurrency` bounds how many requests may be IN FLIGHT — a politeness question,
answering an API's published quota. Trakt needs both (its quota is per 5 minutes
and its opening burst is what trips it); Sonarr on your own LAN needs neither
beyond the default. Keeping them separate is also what stops a slow bulk read on
one service from consuming the budget of another, which is the whole reason these
are per-service rather than one shared pool: a poster warm saturating eight
connections must not leave a calendar refresh queued behind it.

A POOL NEVER FOLLOWS A REDIRECT, and there is no knob here to make one. Every
client is built with httpx's default of follow_redirects=False, because a pool
is chosen BEFORE a request is made and a redirect is discovered AFTER: letting
the client walk the hop would fetch the answer under a budget decided when
nobody knew where it would land, and would forward this app's custom headers to
whatever host the Location named. A service whose catalogue genuinely splits
across paths that 30x between each other classifies the target and re-issues it
on the pool that target deserves — see app/providers/simkl/transport.py's
redirect_pool for the one implementation of that shape.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

from .perftrace import span

logger = logging.getLogger(__name__)

# Every Pool ever constructed, so shutdown can close them all without main.py
# having to name each one — a list it would inevitably fall out of step with.
# Pools are module-level constants that live for the process, so holding strong
# references here costs nothing and keeps aclose_all honest.
_pools: list[Pool] = []


class Pool:
    """A lazily-built, loop-keyed httpx client for one outbound service.

    LOOP-KEYED, AND THIS IS THE PART WORTH HAVING ONE COPY OF. An
    httpx.AsyncClient and an asyncio.Semaphore both bind to the event loop that
    was running when they were created, and the test suite runs each test on a
    FRESH loop. A client built at import time — or cached from an earlier test —
    is bound to a loop that is now dead, and using it fails in a way that reads
    like anything but the real cause. So nothing is constructed until first use,
    and the running loop is checked on every access.

    The abandoned client from a previous loop is dropped rather than closed:
    closing it would mean awaiting on a loop that no longer runs. Its sockets go
    with the loop, which is exactly what a test's teardown wants.
    """

    __slots__ = ("name", "max_connections", "timeout", "concurrency",
                 "_loop", "_client", "_sem")

    def __init__(self, name: str, *, max_connections: int = 8,
                 timeout: float = 30.0, concurrency: int | None = None):
        self.name = name
        self.max_connections = max_connections
        self.timeout = timeout
        self.concurrency = concurrency
        self._loop = None
        self._client: httpx.AsyncClient | None = None
        self._sem: asyncio.Semaphore | None = None
        _pools.append(self)

    def _rebind(self) -> None:
        """Drop anything bound to a loop that is no longer the running one."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._client = None
            self._sem = None

    def client(self) -> httpx.AsyncClient:
        """This service's client, built on first use on the running loop.

        Callers must NOT close it and must NOT use it as a context manager —
        `async with pool.client()` would close the shared client on exit and
        leave every later call to this service raising. Take the client, make the
        request, leave it open.
        """
        self._rebind()
        if self._client is None or self._client.is_closed:
            # Timed because the construction cost is the reason this class
            # exists: on a healthy instance this fires once per service per
            # process and never appears again. A slow one here, or a repeated
            # one, means something is building clients on the request path.
            with span("http_pool.build", service=self.name):
                limits = httpx.Limits(max_connections=self.max_connections,
                                      max_keepalive_connections=self.max_connections)
                self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits)
        return self._client

    def gate(self):
        """`async with pool.gate():` around a request, to honour `concurrency`.

        A no-op context when no concurrency was declared, so a call site can wrap
        unconditionally and the decision stays with the pool's declaration rather
        than being restated at each use.
        """
        if self.concurrency is None:
            return contextlib.nullcontext()
        self._rebind()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        return self._sem

    async def aclose(self) -> None:
        """Close the client if one is open. Safe to call more than once."""
        client, self._client = self._client, None
        self._loop = None
        self._sem = None
        if client is not None and not client.is_closed:
            await client.aclose()


class Pacer:
    """At most `limit` requests leave in any `period` seconds — a RATE, which a
    pool's `concurrency` is not.

    WHY A SEPARATE THING. Concurrency bounds how many requests are in flight; at
    ordinary latencies four in flight is twenty to forty a second, and a quota
    stated per five minutes is spent in under one. MEASURED on a real instance:
    one tracker load fired over a thousand Trakt reads in a burst and was refused
    with 429s part-way, while the four-in-flight gate had done exactly what it
    was built to do.

    A TOKEN BUCKET SIZED SO IT CANNOT EXCEED THE QUOTA IN ANY WINDOW: `burst`
    tokens up front, refilled at (limit - burst) / period a second, so the most
    that can leave in any `period` is burst + refill * period = limit. An
    ordinary page's few dozen reads spend the burst and leave at once; only a
    large job is slowed, and it is slowed to the rate the service publishes.

    A TICKET, CLAIMED SYNCHRONOUSLY. `claim` debits the bucket before anything
    awaits — which under asyncio cannot interleave — so callers queue in the
    order they asked rather than all reading the same balance and leaving
    together. A caller whose turn is further off than it may wait gets None and
    takes nothing, so a page is refused quickly rather than held behind a queue.

    LOOP-KEYED LIKE THE POOL, for the same reason: the suite runs every test on a
    fresh loop, and a bucket drained by one test must not slow the next.
    """

    __slots__ = ("name", "burst", "rate", "_loop", "_tokens", "_stamp")

    def __init__(self, name: str, *, limit: int, period: float, burst: int):
        if not 0 < burst < limit:
            raise ValueError("a pacer's burst must be positive and below its limit")
        self.name = name
        self.burst = float(burst)
        self.rate = (limit - burst) / float(period)
        self._loop = None
        self._tokens = self.burst
        self._stamp = 0.0

    def claim(self, max_wait: float) -> float | None:
        """Seconds this request must wait before it leaves, or None when that
        would be longer than `max_wait` — in which case nothing is taken."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._loop is not loop:
            self._loop, self._tokens, self._stamp = loop, self.burst, now
        self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now
        wait = 0.0 if self._tokens >= 1 else (1 - self._tokens) / self.rate
        if wait > max_wait:
            return None
        self._tokens -= 1
        return wait


async def aclose_all() -> None:
    """Close every pool's client. Called once, from the app's shutdown.

    Best-effort per pool: one service's client failing to close must not leave
    the rest open, and the process is ending regardless.
    """
    for pool in _pools:
        try:
            await pool.aclose()
        except Exception:
            logger.debug("Closing the %s client failed at shutdown", pool.name, exc_info=True)
