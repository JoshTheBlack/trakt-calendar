"""app/cache.py's `fresh_keys` — `get`'s question for many keys in one read.

It decides which of a few thousand cards a background pass asks about
(app/calendar/completion.py), so a key it wrongly calls fresh is a card never
asked about, and one it wrongly calls stale is a request spent twice."""
from __future__ import annotations

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
