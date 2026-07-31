"""The library cache's three promises: it never makes a request wait, it never
refreshes twice at once, and a service it could not read does not read as empty.

The last one is why arr.library_ids and seer.library_ids raise LibraryUnavailable
instead of returning []: the cache stores what it is told, so a caller that
cannot tell a failure from an empty library will store the failure as truth and
quietly un-mark every add button on the calendar.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import arr, integrations_routes as ir, seer


class _Settings:
    """Enough of Settings for the reads under test, which are all patched anyway."""


def _seer(fake):
    """Adapt an arr-shaped fake — (kind, settings) — to seer's (settings).

    An async def rather than a lambda returning a coroutine: patch.object gives an
    async target an AsyncMock, which AWAITS what the side effect returns only when
    the side effect is itself awaitable. A plain lambda hands back an un-awaited
    coroutine, the read silently does nothing, and the assertion fails somewhere
    unrelated.
    """
    async def _call(settings):
        return await fake("seer", settings)
    return _call


class LibraryCacheBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._cache = dict(ir.LIBRARY_CACHE)
        self._seen = {k: dict(v) for k, v in ir._seen_at.items()}
        ir.LIBRARY_CACHE.update({"sonarr": [], "radarr": [], "seer": [], "_ts": 0.0})
        for seen in ir._seen_at.values():
            seen.clear()
        ir._refresh_task = None

    def tearDown(self):
        ir.LIBRARY_CACHE.clear()
        ir.LIBRARY_CACHE.update(self._cache)
        for kind, seen in self._seen.items():
            ir._seen_at[kind].clear()
            ir._seen_at[kind].update(seen)
        ir._refresh_task = None


class SingleFlightTests(LibraryCacheBase):
    async def test_concurrent_snapshots_trigger_exactly_one_refresh(self):
        """Several polls landing in the same moment after the TTL lapses must
        share one read, not start one each — the thundering herd that showed up
        in production as two overlapping four-second refreshes."""
        calls = []

        async def _slow(kind, _settings):
            calls.append(kind)
            await asyncio.sleep(0.05)
            return [1, 2]

        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_slow), \
             patch.object(seer, "library_ids", side_effect=_seer(_slow)):
            # Ten simultaneous callers, all seeing a lapsed TTL.
            for _ in range(10):
                ir.library_snapshot()
            await asyncio.gather(*[t for t in (ir._refresh_task,) if t])

        self.assertEqual(sorted(calls), ["radarr", "seer", "sonarr"])

    async def test_a_snapshot_never_waits_for_the_refresh(self):
        """The route answers from cache and lets the read happen behind it."""
        started = asyncio.Event()

        async def _hang(_kind, _settings):
            started.set()
            await asyncio.sleep(10)
            return []

        ir.LIBRARY_CACHE["sonarr"] = [7]
        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_hang), \
             patch.object(seer, "library_ids", side_effect=_seer(_hang)):
            snap = ir.library_snapshot()
            self.assertEqual(snap["sonarr"], [7])  # served immediately, mid-read
            await started.wait()
            ir._refresh_task.cancel()


class FailureIsNotEmptyTests(LibraryCacheBase):
    async def test_an_unreadable_service_keeps_its_previous_ids(self):
        """One timeout must not blank the add buttons."""
        async def _ok(kind, _settings):
            return [11, 22]

        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_ok), \
             patch.object(seer, "library_ids", side_effect=_seer(_ok)):
            await ir._read_all(1000.0)
        self.assertEqual(ir.LIBRARY_CACHE["sonarr"], [11, 22])

        async def _down(_kind, _settings):
            raise arr.LibraryUnavailable("connection refused")

        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_down), \
             patch.object(seer, "library_ids", side_effect=_seer(_down)):
            await ir._read_all(1100.0)
        self.assertEqual(ir.LIBRARY_CACHE["sonarr"], [11, 22])

    async def test_a_genuinely_empty_library_does_clear(self):
        """The whole point of the exception is that [] still means empty."""
        async def _ok(kind, _settings):
            return [11]

        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_ok), \
             patch.object(seer, "library_ids", side_effect=_seer(_ok)):
            await ir._read_all(1000.0)
        self.assertEqual(ir.LIBRARY_CACHE["sonarr"], [11])

        async def _empty(_kind, _settings):
            return []

        # Far enough ahead that the memory of 11 has aged out; an id that is
        # genuinely gone should not be remembered forever.
        later = 1000.0 + ir.LIBRARY_MEMORY_SECONDS + 1
        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_empty), \
             patch.object(seer, "library_ids", side_effect=_seer(_empty)):
            await ir._read_all(later)
        self.assertEqual(ir.LIBRARY_CACHE["sonarr"], [])

    async def test_an_unreachable_service_empties_out_eventually(self):
        """Degrade to stale, but not to stale forever — the memory window is the
        upper bound on how long an answer nobody could re-confirm keeps counting."""
        async def _ok(kind, _settings):
            return [5]

        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_ok), \
             patch.object(seer, "library_ids", side_effect=_seer(_ok)):
            await ir._read_all(1000.0)

        async def _down(_kind, _settings):
            raise arr.LibraryUnavailable("still down")

        past_window = 1000.0 + ir.LIBRARY_MEMORY_SECONDS + 1
        with patch.object(ir, "load_settings", return_value=_Settings()), \
             patch.object(arr, "library_ids", side_effect=_down), \
             patch.object(seer, "library_ids", side_effect=_seer(_down)):
            await ir._read_all(past_window)
        self.assertEqual(ir.LIBRARY_CACHE["sonarr"], [])


class InvalidationTests(LibraryCacheBase):
    def test_it_drops_the_memory_but_leaves_what_is_served(self):
        """Credentials changed, so remembered ids describe a DIFFERENT library and
        must not be unioned into the next read. What is currently served stands
        until that read replaces it, or every add button un-marks itself in the
        meantime — and that window is longer now the route no longer waits."""
        ir._seen_at["sonarr"][1] = 1000.0
        ir.LIBRARY_CACHE["sonarr"] = [1]
        ir.LIBRARY_CACHE["_ts"] = 1e12

        ir.invalidate_library_cache()

        self.assertEqual(ir._seen_at["sonarr"], {})
        self.assertEqual(ir.LIBRARY_CACHE["sonarr"], [1])
        self.assertEqual(ir.LIBRARY_CACHE["_ts"], 0.0)


if __name__ == "__main__":
    unittest.main()
