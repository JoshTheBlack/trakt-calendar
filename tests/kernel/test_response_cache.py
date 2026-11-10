"""app/cache.py — `fresh_keys`, `get`'s question for many keys in one read, and
the cache's writes taking their turn.

`fresh_keys` decides which of a few thousand cards a background pass asks about
(app/calendar/completion.py), so a key it wrongly calls fresh is a card never
asked about, and one it wrongly calls stale is a request spent twice."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from app import cache, db
from tests.support import DatabaseTestCase


class FreshKeysTests(DatabaseTestCase):
    async def test_it_names_the_keys_stored_within_the_ttl_and_no_others(self):
        with patch.object(db, "now", return_value=1000):
            await cache.set("old", 1)
        with patch.object(db, "now", return_value=5000):
            await cache.set("new", 1)
            self.assertEqual(await cache.fresh_keys(["old", "new", "never"], 100),
                             frozenset({"new"}))

    async def test_more_keys_than_one_statement_holds(self):
        keys = [f"k{n}" for n in range(1200)]
        for key in keys[::100]:
            await cache.set(key, 1)
        self.assertEqual(await cache.fresh_keys(keys, 60), frozenset(keys[::100]))

    async def test_nothing_asked_and_no_ttl_name_nothing(self):
        await cache.set("k", 1)
        self.assertEqual(await cache.fresh_keys([], 60), frozenset())
        self.assertEqual(await cache.fresh_keys(["k"], 0), frozenset())


class TheCachesWritesTakeTheirTurnTests(DatabaseTestCase):
    """A write made around the writer queue commits while a calendar store is
    deciding its rows in its turn, and the store then decides them again under
    the lock. The cache's writes wait for the turn ahead of them — its
    deciding included, which holds no lock a write outside the queue would
    have to wait for."""

    async def _beside_a_store(self, write) -> list[str]:
        order: list[str] = []

        def prepare(conn):
            order.append("store decides")
            time.sleep(0.2)
            order.append("store decided")

        ahead = asyncio.ensure_future(db.transaction(
            lambda conn, _prepared: order.append("store writes"), prepare=prepare))
        await asyncio.sleep(0.05)
        await write()
        order.append("cache written")
        await ahead
        return order

    async def test_a_cache_write_waits_for_the_turn_ahead_of_it(self):
        self.assertEqual(await self._beside_a_store(lambda: cache.set("k", 1)),
                         ["store decides", "store decided", "store writes", "cache written"])
        self.assertEqual(await cache.get("k", 60), 1)

    async def test_so_does_a_cache_delete(self):
        await cache.set("k", 1)
        self.assertEqual(await self._beside_a_store(lambda: cache.delete("k")),
                         ["store decides", "store decided", "store writes", "cache written"])
        self.assertIsNone(await cache.get("k", 60))
