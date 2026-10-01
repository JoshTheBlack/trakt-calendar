"""`fetch_watched_map` — a viewer's watched count per (show, season), read when
a season is given up on so the frozen line says how far they got.

The per-show read under it (`_progress_record`) is the stub here: what is
asserted is the fan-out and the filing — one read per show however often it is
named, every season filed under Trakt's own (id, season), a show that could not
be read contributing nothing rather than zeros, and a refused credential
reaching the caller instead of becoming "watched nothing" one show at a time.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.providers.trakt import sync as trakt_sync
from app.providers.trakt.transport import TraktError

SETTINGS = SimpleNamespace(trakt_client_id="id", trakt_access_token="tok")


class WatchedMapTests(unittest.IsolatedAsyncioTestCase):
    async def _map(self, answers, ids):
        asked = []

        async def progress(settings, trakt_id, client=None):
            asked.append(trakt_id)
            answer = answers.get(trakt_id)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(trakt_sync, "_progress_record", progress), \
             patch.object(trakt_sync.transport, "shared_client", lambda: None):
            return await trakt_sync.fetch_watched_map(SETTINGS, ids), asked

    async def test_each_show_is_read_once_and_every_season_is_filed(self):
        answers = {10: {"seasons": [{"number": 1, "completed": 8},
                                    {"number": 2, "completed": 3},
                                    {"completed": 4}]},          # no number: skipped
                   20: {"seasons": [{"number": 1}]}}             # no count: zero
        watched, asked = await self._map(answers, [10, "10", None, 20])
        self.assertEqual(sorted(asked), [10, 20])
        self.assertEqual(watched, {(10, 1): 8, (10, 2): 3, (20, 1): 0})

    async def test_a_show_that_could_not_be_read_contributes_nothing(self):
        """Not zeros: an empty progress record is a real answer ("watched none
        of it") and the caller acts on it, so a failure must not look like one."""
        watched, _ = await self._map({10: None, 20: {"seasons": [{"number": 1, "completed": 2}]}},
                                     [10, 20])
        self.assertEqual(watched, {(20, 1): 2})

    async def test_nothing_named_asks_nothing(self):
        watched, asked = await self._map({}, [None])
        self.assertEqual((watched, asked), ({}, []))

    async def test_a_refused_credential_reaches_the_caller(self):
        with self.assertRaises(TraktError):
            await self._map({10: TraktError("Trakt rejected the credentials", 401)}, [10])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
