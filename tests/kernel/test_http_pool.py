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
