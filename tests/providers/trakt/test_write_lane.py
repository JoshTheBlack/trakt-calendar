"""app/providers/trakt/transport.py — a person's writes to Trakt: one in flight at
a time, a second apart, and a 429 answered inside the call's budget.

Trakt allows one authenticated POST per second PER USER, where its reads are a
quota over five minutes. No network and no real waiting: the client is a
recording double and asyncio.sleep records what it was asked to wait.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from app.providers.trakt import TraktRateLimitError, transport


def _headers(token: str | None = "tok") -> dict:
    headers = {"trakt-api-key": "cid"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class _Client:
    """Answers each request from a script; a request may be held on an event,
    so a test can see what else starts while it is in flight."""

    def __init__(self, *answers, hold: asyncio.Event | None = None):
        self.answers = list(answers)
        self.hold = hold
        self.started = []
        self.in_flight = 0
        self.most_in_flight = 0

    async def _answer(self, method, url, headers):
        self.started.append((method, url, (headers or {}).get("Authorization")))
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if self.hold is not None:
                await self.hold.wait()
            answer = self.answers.pop(0) if self.answers else 201
            if isinstance(answer, Exception):
                raise answer
            status, extra = answer if isinstance(answer, tuple) else (answer, {})
            return httpx.Response(status, headers=extra, json={})
        finally:
            self.in_flight -= 1

    async def get(self, url, headers=None, timeout=None):
        return await self._answer("GET", url, headers)

    async def post(self, url, headers=None, json=None, timeout=None):
        return await self._answer("POST", url, headers)


_real_sleep = asyncio.sleep


class _RecordingSleep:
    """Records every wait and yields to the loop once instead of waiting."""

    def __init__(self):
        self.durations = []

    async def __call__(self, seconds):
        self.durations.append(seconds)
        await _real_sleep(0)


URL = f"{transport.API_BASE}/sync/watchlist"


class LaneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        transport._lanes.clear()
    async def test_a_second_write_by_the_same_person_waits_a_second(self):
        sleeps = _RecordingSleep()
        client = _Client(201, 201)
        with patch.object(transport.asyncio, "sleep", new=sleeps):
            await transport.send(client, "POST", URL, headers=_headers(), json={})
            await transport.send(client, "POST", URL, headers=_headers(), json={})
        self.assertEqual(len(client.started), 2)
        self.assertEqual(len(sleeps.durations), 1)
        self.assertAlmostEqual(sleeps.durations[0], transport.POST_MIN_INTERVAL, places=2)

    async def test_one_write_in_flight_per_person(self):
        hold = asyncio.Event()
        client = _Client(201, 201, hold=hold)
        with patch.object(transport.asyncio, "sleep", new=_RecordingSleep()):
            first = asyncio.create_task(
                transport.send(client, "POST", URL, headers=_headers(), json={}))
            second = asyncio.create_task(
                transport.send(client, "POST", URL, headers=_headers(), json={}))
            for _ in range(5):
                await _real_sleep(0)
            self.assertEqual(len(client.started), 1, "the second write left while the first was in flight")
            hold.set()
            await asyncio.gather(first, second)
        self.assertEqual(client.most_in_flight, 1)
        self.assertEqual(len(client.started), 2)

    async def test_two_people_write_side_by_side(self):
        hold = asyncio.Event()
        client = _Client(201, 201, hold=hold)
        with patch.object(transport.asyncio, "sleep", new=_RecordingSleep()) as sleeps:
            tasks = [asyncio.create_task(
                transport.send(client, "POST", URL, headers=_headers(token), json={}))
                for token in ("one", "two")]
            for _ in range(5):
                await _real_sleep(0)
            self.assertEqual(client.most_in_flight, 2)
            hold.set()
            await asyncio.gather(*tasks)
        self.assertEqual(sleeps.durations, [])

    async def test_reads_and_bearerless_writes_are_not_laned(self):
        """A read spends a read quota, which its pacer paces; a token exchange
        carries no bearer and is nobody's write."""
        sleeps = _RecordingSleep()
        client = _Client(200, 200, 200, 200)
        with patch.object(transport.asyncio, "sleep", new=sleeps):
            await transport.send(client, "GET", URL, headers=_headers())
            await transport.send(client, "GET", URL, headers=_headers())
            await transport.send(client, "POST", URL, headers=_headers(None), json={})
            await transport.send(client, "POST", URL, headers=_headers(None), json={})
        self.assertEqual(sleeps.durations, [])
        self.assertEqual(transport._lanes, {})

    async def test_an_idle_lane_is_dropped_by_the_next_write(self):
        """A lane is keyed by a token and a token rotates, so the map would grow
        by one lane per refresh for as long as the process runs — weeks. A lane
        nobody holds and whose second has passed is dropped by the next write."""
        clock = [1000.0]
        with patch.object(transport.asyncio, "sleep", new=_RecordingSleep()), \
                patch.object(transport._time, "monotonic", lambda: clock[0]):
            await transport.send(_Client(201), "POST", URL, headers=_headers("old"), json={})
            self.assertEqual(len(transport._lanes), 1)
            clock[0] += transport.POST_MIN_INTERVAL
            await transport.send(_Client(201), "POST", URL, headers=_headers("new"), json={})
        self.assertEqual(len(transport._lanes), 1, "the rotated token's lane was kept")

    async def test_the_lane_holds_no_credential(self):
        with patch.object(transport.asyncio, "sleep", new=_RecordingSleep()):
            await transport.send(_Client(201), "POST", URL, headers=_headers("secret-token"), json={})
        self.assertTrue(transport._lanes)
        for key in transport._lanes:
            self.assertNotIn("secret-token", key)

    async def test_a_write_that_never_answered_still_spends_the_second(self):
        """The limit counts writes Trakt received, and one that timed out here may
        have arrived there."""
        sleeps = _RecordingSleep()
        client = _Client(httpx.ConnectTimeout("slow"), 201)
        with patch.object(transport.asyncio, "sleep", new=sleeps):
            with self.assertRaises(httpx.ConnectTimeout):
                await transport.send(client, "POST", URL, headers=_headers(), json={})
            await transport.send(client, "POST", URL, headers=_headers(), json={})
        self.assertEqual(len(sleeps.durations), 1)
        self.assertGreater(sleeps.durations[0], 1.0)


class RateLimitedWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_429_honours_retry_after_and_the_retry_is_still_paced(self):
        sleeps = _RecordingSleep()
        client = _Client((429, {"Retry-After": "2"}), 201)
        with patch.object(transport.asyncio, "sleep", new=sleeps):
            resp = await transport.send(client, "POST", URL, headers=_headers(), json={})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(client.started), 2)
        # The Retry-After, then the lane's own second (the recorded sleeps do not
        # move the clock, so the lane still sees its last write as just sent).
        self.assertEqual(sleeps.durations[0], 2.0)
        self.assertGreater(sleeps.durations[1], 1.0)

    async def test_a_retry_after_past_the_budget_is_not_waited_out(self):
        sleeps = _RecordingSleep()
        client = _Client((429, {"Retry-After": "600"}))
        with patch.object(transport.asyncio, "sleep", new=sleeps):
            with self.assertRaises(TraktRateLimitError):
                await transport.send(client, "POST", URL, headers=_headers(), json={})
        self.assertEqual(len(client.started), 1)
        self.assertEqual(sleeps.durations, [])

    async def test_a_429_that_does_not_clear_ends_after_three_attempts(self):
        client = _Client(429, 429, 429, 201)
        with patch.object(transport.asyncio, "sleep", new=_RecordingSleep()):
            with self.assertRaises(TraktRateLimitError):
                await transport.send(client, "POST", URL, headers=_headers(), json={})
        self.assertEqual(len(client.started), 3)

    async def test_an_account_limit_comes_back_to_the_caller(self):
        """420 is not a rate limit: retrying cannot make room on a full list, so
        it is answered, untouched, for the caller to read."""
        client = _Client((420, {"X-Account-Limit": "100"}))
        with patch.object(transport.asyncio, "sleep", new=_RecordingSleep()):
            resp = await transport.send(client, "POST", URL, headers=_headers(), json={})
        self.assertEqual(resp.status_code, transport.ACCOUNT_LIMIT_STATUS)
        self.assertEqual(len(client.started), 1)


if __name__ == "__main__":
    unittest.main()
