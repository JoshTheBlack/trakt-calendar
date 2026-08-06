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
                 "follow_redirects", "_loop", "_client", "_sem")

    def __init__(self, name: str, *, max_connections: int = 8,
                 timeout: float = 30.0, concurrency: int | None = None,
                 follow_redirects: bool = False):
        self.name = name
        self.max_connections = max_connections
        self.timeout = timeout
        self.concurrency = concurrency
        # OFF BY DEFAULT: a redirect changes which resource answered, and most
        # of this app's services (Trakt, Sonarr, Radarr, ...) never redirect a
        # legitimate call, so following one silently would be more likely to
        # mask a misconfigured URL than to help. A service whose own catalogue
        # genuinely splits across paths that 30x between each other opts in
        # explicitly at its own Pool() declaration, with the reason written
        # there.
        self.follow_redirects = follow_redirects
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
                self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits,
                                                 follow_redirects=self.follow_redirects)
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
