"""Trakt 429 handling: the retry/backoff sender, the always-raise contract that
fixes the silent-corruption bug, and the per-show + top-level degradation it feeds.

No real network and no real sleeping — a fake httpx client feeds canned responses
and asyncio.sleep is patched to record durations instead of waiting, so the
wall-clock and exponential-backoff logic is asserted in microseconds. The
loop-keyed semaphore is exercised across two fresh event loops, the exact
condition a naive module-level Semaphore would fail under the test harness.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_trakt_rate_limit -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-ratelimit-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import db, distrakt as distrakt_store, watch_history  # noqa: E402
from app.providers.trakt import transport  # noqa: E402
from app.config import Settings  # noqa: E402
from app.providers.trakt import TraktRateLimitError  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

# Minimal settings whose only job is to satisfy _headers()/_cached_get() — no real
# token is ever put on the wire because the client is a fake.
FAKE_SETTINGS = SimpleNamespace(
    trakt_access_token="tok", trakt_client_id="cid",
    pagination_limit=100, cache_ttl_minutes=10,
)


def _resp(status: int, headers: dict | None = None):
    return httpx.Response(status, headers=headers or {})


class FakeClient:
    """Serves a scripted list of responses/exceptions to _send, one per request.
    Exposes get/post (the shape _send dispatches to) recording each call."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.requests = []

    async def _next(self, method, url, timeout):
        self.requests.append(SimpleNamespace(method=method, url=url, timeout=timeout))
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url, headers=None, timeout=None):
        return await self._next("GET", url, timeout)

    async def post(self, url, headers=None, json=None, timeout=None):
        return await self._next("POST", url, timeout)


class RecordingSleep:
    """A stand-in for asyncio.sleep that records durations and never waits."""

    def __init__(self):
        self.durations = []

    async def __call__(self, seconds):
        self.durations.append(seconds)


def _patch_sleep(recorder: RecordingSleep):
    return patch("app.providers.trakt.transport.asyncio.sleep", new=recorder)


def _mock_transport_client(*scripted):
    """A REAL httpx.AsyncClient whose transport returns a scripted sequence of
    responses — so the sender is exercised through genuine httpx Response/header
    parsing and the real .get plumbing, with no socket. Each item is a status int,
    or an (status, headers) tuple to attach a Retry-After."""
    it = iter(scripted)

    def handler(request):
        item = next(it)
        status, headers = item if isinstance(item, tuple) else (item, {})
        return httpx.Response(status, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class SendRetryTests(unittest.IsolatedAsyncioTestCase):
    """The 429 retry/backoff loop in _send."""

    async def test_honors_retry_after_then_succeeds(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429, {"Retry-After": "2"}), _resp(200)])
        with _patch_sleep(sleep):
            resp = await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleep.durations, [2.0])  # the header wins over the 1s step
        self.assertEqual(len(client.requests), 2)

    async def test_exponential_backoff_when_no_retry_after(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(200)])
        with _patch_sleep(sleep):
            resp = await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleep.durations, [1.0, 2.0])  # 2**0, 2**1

    async def test_exhausted_budget_raises_rate_limit_not_none(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(429)])
        with _patch_sleep(sleep):
            with self.assertRaises(TraktRateLimitError):
                await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(sleep.durations, [1.0, 2.0])  # slept twice, then gave up
        self.assertEqual(len(client.requests), 3)  # exactly _SEND_MAX_ATTEMPTS

    async def test_retry_after_beyond_budget_raises_without_sleeping(self):
        # Trakt's docs cite a 254s Retry-After from the wild: honor it, see it blows
        # the 30s budget, and raise immediately rather than hang the whole request.
        sleep = RecordingSleep()
        client = FakeClient([_resp(429, {"Retry-After": "254"})])
        with _patch_sleep(sleep):
            with self.assertRaises(TraktRateLimitError):
                await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(sleep.durations, [])  # never slept part-way in
        self.assertEqual(len(client.requests), 1)

    async def test_garbage_retry_after_falls_back_to_exponential(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429, {"Retry-After": "not-a-number"}), _resp(200)])
        with _patch_sleep(sleep):
            resp = await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleep.durations, [1.0])

    async def test_wall_clock_elapsed_stops_even_with_small_sleeps(self):
        # The bound is ELAPSED time, not cumulative sleep: simulate the clock
        # jumping past 30s between attempts (request time, not sleep) and confirm
        # the loop stops on the elapsed check, not after three full attempts.
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(200)])
        # monotonic() calls: start, iter1 remaining, iter1 post-429 check, iter2 remaining.
        clock = iter([0.0, 0.0, 0.0, 100.0])
        with _patch_sleep(sleep), patch("app.providers.trakt.transport._time.monotonic", lambda: next(clock)):
            with self.assertRaises(TraktRateLimitError):
                await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(sleep.durations, [1.0])  # one small sleep, then elapsed cut it
        self.assertEqual(len(client.requests), 1)  # second attempt never fired

    async def test_non_429_returned_untouched(self):
        for status in (200, 401, 404):
            client = FakeClient([_resp(status)])
            resp = await transport._send(client, "GET", transport.API_BASE + "/x")
            self.assertEqual(resp.status_code, status)
            self.assertEqual(len(client.requests), 1)  # no retry on non-429

    async def test_network_error_propagates_as_httpx(self):
        client = FakeClient([httpx.ConnectError("boom")])
        with self.assertRaises(httpx.ConnectError):
            await transport._send(client, "GET", transport.API_BASE + "/x")


class SemaphoreLoopKeyingTests(unittest.TestCase):
    """The concurrency gate must survive the fresh-loop-per-test harness."""

    def test_semaphore_recreated_per_event_loop(self):
        async def one():
            client = FakeClient([_resp(200)])
            await transport._send(client, "GET", transport.API_BASE + "/x")
            return transport._rate_limit_semaphore()

        # Two separate asyncio.run() calls => two distinct loops. A module-level
        # Semaphore bound at import would raise "bound to a different event loop"
        # on the second; a loop-keyed one is simply recreated.
        sem1 = asyncio.run(one())
        sem2 = asyncio.run(one())
        self.assertIsNot(sem1, sem2)


class CachedGetContractTests(unittest.IsolatedAsyncioTestCase):
    """_cached_get: an exhausted retry ALWAYS raises; a real 404 still returns None."""

    async def test_exhausted_retry_raises_rate_limit(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(429)])
        with _patch_sleep(sleep):
            with self.assertRaises(TraktRateLimitError):
                # private=True keeps this off the disk cache entirely (no DB needed).
                await transport._cached_get(client, FAKE_SETTINGS, "shows/9/seasons/9",
                                        {"extended": "full"}, private=True)

    async def test_plain_404_still_returns_none(self):
        client = FakeClient([_resp(404)])
        out = await transport._cached_get(client, FAKE_SETTINGS, "shows/9/seasons/9",
                                      {"extended": "full"}, private=True)
        self.assertIsNone(out)  # a genuine miss stays None — distinct from a 429


def _record(trakt_id, season, **over):
    rec = {
        "trakt_id": trakt_id, "season": season, "tmdb": 555, "slug": "s", "media": "show",
        "title": f"Show {trakt_id}", "network": "HBO", "abandoned": False, "abandoned_form": None,
        "watched": 3, "total": 12, "cadence": "Mon", "premiere": "1/5", "finale": "3/1",
        "started_airing": True, "finished_airing": False, "bucket": "keepup",
    }
    rec.update(over)
    return rec


class PerShowDegradeTests(unittest.IsolatedAsyncioTestCase):
    """compute_live_shows: one show's 429 degrades that show, not the roster."""

    async def _fake_fsd(self, settings, trakt_id, season, fresh=False, client=None):
        if int(trakt_id) == 2:
            raise TraktRateLimitError("rate limited", 429)
        return {"total": 10, "cadence": "Tue", "premiere": "2/1", "finale": "4/1",
                "started_airing": True, "finished_airing": False}

    async def test_one_show_unavailable_rest_fine(self):
        records = [_record(1, 1), _record(2, 1)]
        watched = {(1, 1): 4, (2, 1): 7}
        with patch("app.providers.trakt.detail.fetch_season_detail", self._fake_fsd):
            shows = await distrakt_store.compute_live_shows(
                0, records, FAKE_SETTINGS, watched_lookup=watched, allow_degrade=True)
        by_id = {s["trakt_id"]: s for s in shows}
        self.assertFalse(by_id[1]["unavailable"])
        self.assertEqual(by_id[1]["total"], 10)  # live value used
        self.assertTrue(by_id[2]["unavailable"])
        self.assertEqual(by_id[2]["total"], 12)  # fell back to last-known stored total

    async def test_raises_when_not_degrading(self):
        records = [_record(1, 1), _record(2, 1)]
        watched = {(1, 1): 4}
        with patch("app.providers.trakt.detail.fetch_season_detail", self._fake_fsd):
            with self.assertRaises(TraktRateLimitError):
                await distrakt_store.compute_live_shows(
                    0, records, FAKE_SETTINGS, watched_lookup=watched, allow_degrade=False)


class TopLevelDegradeTests(unittest.IsolatedAsyncioTestCase):
    """_distrakt_month_payload: a shared-prerequisite 429 degrades the whole month
    to last-known totals + a notice at HTTP 200, never a false 0/0 or a 500."""

    _counter = 0

    async def asyncSetUp(self):
        TopLevelDegradeTests._counter += 1
        db.set_db_path(TMP / f"toplevel-{TopLevelDegradeTests._counter}.db")
        await db.migrate()
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
            "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("tracker", now, now),
        )
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_shared_prereq_rate_limit_returns_stale_plus_notice(self):
        from datetime import date

        from app.main import _distrakt_month_payload

        today = date.today()
        month_key = f"{today.year:04d}-{today.month:02d}"
        doc = distrakt_store.new_month_doc(month_key)
        doc["shows"] = [_record(1, 1, total=12, watched=8)]
        doc["totals_refreshed_at"] = db.now()
        await distrakt_store.save_month(self.user_id, doc)

        settings = Settings(trakt_client_id="cid", trakt_access_token="tok")

        async def _boom(*a, **k):
            raise TraktRateLimitError("rate limited", 429)

        # The watch-history sync is the documented shared prerequisite: a 429 there
        # can't be pinned on one show, so the whole month degrades.
        with patch("app.watch_history.sync_and_baseline", _boom):
            payload, status = await _distrakt_month_payload(
                self.user_id, today.year, today.month, settings)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["rate_limited"])
        self.assertIn("rate-limiting", payload["notice"])
        self.assertEqual(len(payload["shows"]), 1)  # rendered from the stored record
        self.assertEqual(payload["shows"][0]["total"], 12)  # last-known, not a false 0


class MockTransportSendTests(unittest.IsolatedAsyncioTestCase):
    """The same 429 logic, driven through a REAL httpx.AsyncClient (MockTransport)
    so genuine Response/header parsing and the .get plumbing are in the loop — a
    faithfulness layer over the hand-rolled FakeClient, still fully offline."""

    async def test_real_client_honors_retry_after_then_succeeds(self):
        sleep = RecordingSleep()
        async with _mock_transport_client((429, {"Retry-After": "2"}), 200) as client:
            with _patch_sleep(sleep):
                resp = await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleep.durations, [2.0])  # header parsed off a real Response

    async def test_real_client_exponential_when_no_retry_after(self):
        sleep = RecordingSleep()
        async with _mock_transport_client(429, 429, 200) as client:
            with _patch_sleep(sleep):
                resp = await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleep.durations, [1.0, 2.0])

    async def test_real_client_exhausted_raises_rate_limit(self):
        sleep = RecordingSleep()
        async with _mock_transport_client(429, 429, 429) as client:
            with _patch_sleep(sleep):
                with self.assertRaises(TraktRateLimitError):
                    await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(sleep.durations, [1.0, 2.0])

    async def test_real_client_non_429_returned_untouched(self):
        async with _mock_transport_client(404) as client:
            resp = await transport._send(client, "GET", transport.API_BASE + "/x")
        self.assertEqual(resp.status_code, 404)  # no retry, returned as-is


if __name__ == "__main__":
    unittest.main()
