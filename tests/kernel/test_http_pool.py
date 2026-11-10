"""app/http_pool.py's one shared trust store, and where it gets read.

Building an SSL context parses the whole CA bundle — about a third of a second —
so it is read once per process, and read on a worker thread at start-up rather
than on the event loop by whichever request or heartbeat job builds the first
client.
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest import mock

import httpx

from app import http_pool


class TheTrustStoreIsReadOffTheLoopTests(unittest.TestCase):
    def setUp(self):
        self._kept = http_pool._ssl_context
        http_pool._ssl_context = None

    def tearDown(self):
        http_pool._ssl_context = self._kept

    def test_loading_it_builds_the_context_on_a_worker_thread(self):
        threads = []
        real = httpx.create_ssl_context

        def _watched(*args, **kwargs):
            threads.append(threading.get_ident())
            return real(*args, **kwargs)

        with mock.patch.object(httpx, "create_ssl_context", _watched):
            asyncio.run(http_pool.load_trust_store())
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertIsNotNone(http_pool._ssl_context)

    def test_a_client_built_afterwards_reads_nothing_again(self):
        """Every Pool.client() takes its context from `_shared_ssl_context`."""
        asyncio.run(http_pool.load_trust_store())
        built = http_pool._ssl_context
        with mock.patch.object(httpx, "create_ssl_context") as rebuilt:
            self.assertIs(http_pool._shared_ssl_context(), built)
        rebuilt.assert_not_called()


class ATallyCountsTheRequestsItsWorkSentTests(unittest.IsolatedAsyncioTestCase):
    """`tally` is how a background job paces itself by what it SENT: per pool,
    every attempt, its own work's requests and nobody else's."""

    async def asyncSetUp(self):
        self.pool = http_pool.Pool("tally-test")
        self.addAsyncCleanup(self.pool.aclose)
        # Off the registry afterwards, or the next test's count by name would
        # include this pool's requests.
        self.addCleanup(http_pool._pools.remove, self.pool)
        real = httpx.AsyncClient
        answer = httpx.MockTransport(lambda request: httpx.Response(200))
        building = mock.patch.object(
            httpx, "AsyncClient", lambda *args, **kwargs: real(*args, transport=answer, **kwargs))
        building.start()
        self.addCleanup(building.stop)

    async def get(self, path="/"):
        await self.pool.client().get(f"http://127.0.0.1{path}")

    async def test_each_request_inside_is_counted_under_its_pools_name(self):
        with http_pool.tally() as sent:
            await self.get()
            await asyncio.gather(self.get("/a"), self.get("/b"))
        self.assertEqual(sent, {"tally-test": 3})

    async def test_a_request_outside_is_not(self):
        await self.get()
        with http_pool.tally() as sent:
            pass
        await self.get()
        self.assertEqual(sent, {})

    async def test_work_running_beside_it_is_not(self):
        """A page served while a job runs is the page's, not the job's."""
        started, release = asyncio.Event(), asyncio.Event()

        async def _beside():
            started.set()
            await release.wait()
            await self.get("/page")

        beside = asyncio.create_task(_beside())
        await started.wait()
        with http_pool.tally() as sent:
            await self.get("/job")
            release.set()
            await beside
        self.assertEqual(sent, {"tally-test": 1})


class WhatThePoolSentRecentlyTests(ATallyCountsTheRequestsItsWorkSentTests):
    """`sent_within` is the whole app's spend through pools of one name — every
    request, whoever sent it — over a window no longer than RECENT_SECONDS."""

    async def test_every_request_counts_tallied_or_not(self):
        await self.get()
        with http_pool.tally():
            await self.get()
        self.assertEqual(http_pool.sent_within("tally-test", 60), 2)
        self.assertEqual(http_pool.sent_within("no-such-pool", 60), 0)

    async def test_a_request_older_than_the_window_is_not_counted_and_is_let_go(self):
        clock = [1000.0]
        with mock.patch.object(http_pool.time, "monotonic", lambda: clock[0]):
            await self.get("/old")
            clock[0] += 100
            await self.get("/newer")
            self.assertEqual(http_pool.sent_within("tally-test", 60), 1)
            self.assertEqual(http_pool.sent_within("tally-test", 300), 2)
            clock[0] += http_pool.RECENT_SECONDS + 1
            await self.get("/now")
            self.assertEqual(http_pool.sent_within("tally-test", 10_000), 1)
            self.assertEqual(len(self.pool._recent), 1, "nothing past the window is held")
