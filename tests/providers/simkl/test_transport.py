"""Simkl's transport: the POST pacer, the 429 retry/backoff loop, and the 412
circuit breaker.

No real network and no real sleeping — a fake httpx client feeds canned
responses and asyncio.sleep is patched to record durations instead of waiting,
so the pacing and backoff logic is asserted in microseconds.

THREE PIECES OF MODULE STATE ARE DELIBERATELY GLOBAL in the transport — the
POST-pacing deadline and the breaker's deadline are one budget per client id,
not one per caller — so every test here resets them. A leaked breaker deadline
would make an unrelated test fail with a refusal it never asked for, which is
exactly the confusion the reset exists to prevent.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app import changelog
from app.providers.simkl import SimklBlockedError, SimklRateLimitError
from app.providers.simkl import transport

# Enough of a Settings to satisfy api_headers()/api_params(); no real credential
# ever reaches the wire because the client is a fake.
FAKE_SETTINGS = SimpleNamespace(
    simkl_client_id="cid", simkl_access_token="tok", cache_ttl_minutes=10,
)

URL = transport.API_BASE + "/tv/1234"


def _resp(status: int, headers: dict | None = None):
    return httpx.Response(status, headers=headers or {})


class FakeClient:
    """Serves a scripted list of responses/exceptions, one per request, and
    records every call — so "the breaker refused this without a request" is
    something a test can assert rather than infer."""

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
    return patch("app.providers.simkl.transport.asyncio.sleep", new=recorder)


def _no_catalog_pacing():
    """Suppress the catalogue GET pacer for tests that are about something else.

    The pacer sleeps between catalogue GETs, and a test asserting the 429
    backoff arithmetic would otherwise be reading two mechanisms' sleeps out of
    one list. Pacing has its own tests below; these have theirs."""
    async def _immediately():
        return None
    return patch("app.providers.simkl.transport._pace_catalog", new=_immediately)


class TransportStateTestCase(unittest.IsolatedAsyncioTestCase):
    """Leaves the transport's three deadlines as it found them."""

    def setUp(self):
        transport._close_breaker()
        transport._post_ready_at = 0.0
        transport._catalog_ready_at = 0.0
        self.addCleanup(transport._close_breaker)
        self.addCleanup(setattr, transport, "_post_ready_at", 0.0)
        self.addCleanup(setattr, transport, "_catalog_ready_at", 0.0)


class PostPacerTests(TransportStateTestCase):
    """One POST per second, which the pool's concurrency gate cannot enforce on
    its own: it bounds how many requests are in flight, not how fast they
    leave."""

    async def test_the_first_post_is_not_delayed(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(200)])
        with _patch_sleep(sleep):
            await transport.send(client, "POST", URL, pool=transport.SYNC_POOL, json={})
        self.assertEqual(sleep.durations, [])

    async def test_a_second_post_waits_out_the_interval(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(200), _resp(200)])
        with _patch_sleep(sleep):
            await transport.send(client, "POST", URL, pool=transport.SYNC_POOL, json={})
            await transport.send(client, "POST", URL, pool=transport.SYNC_POOL, json={})
        self.assertEqual(len(sleep.durations), 1)
        # The recorded sleep never actually elapses, so the wait asked for is
        # essentially the whole interval. Compared with a tolerance rather than
        # exactly: the clock this is measured against has a coarser resolution
        # than the arithmetic, and a strict bound fails on the odd tick.
        self.assertAlmostEqual(sleep.durations[0], transport.POST_MIN_INTERVAL, places=2)

    async def test_the_interval_clears_the_published_one_per_second_cap(self):
        """A flat 1.0 would be a coin flip: the cap is enforced on Simkl's clock,
        not ours."""
        self.assertGreater(transport.POST_MIN_INTERVAL, 1.0)

    async def test_a_get_is_not_paced_by_the_post_interval(self):
        """GETs are paced, but on their own far smaller interval — see
        CatalogPacerTests. What must never happen is a GET waiting out the one
        POST per second the SYNC pool is capped at."""
        sleep = RecordingSleep()
        client = FakeClient([_resp(200), _resp(200)])
        with _patch_sleep(sleep):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertNotIn(transport.POST_MIN_INTERVAL, sleep.durations)
        self.assertTrue(all(d <= transport.CATALOG_MIN_INTERVAL for d in sleep.durations))

    async def test_a_failed_post_still_counts_against_the_cap(self):
        """The cap counts requests Simkl RECEIVED, and a call that failed on our
        side may well have arrived on theirs."""
        sleep = RecordingSleep()
        client = FakeClient([httpx.ConnectError("boom"), _resp(200)])
        with _patch_sleep(sleep):
            with self.assertRaises(httpx.ConnectError):
                await transport.send(client, "POST", URL, pool=transport.SYNC_POOL, json={})
            await transport.send(client, "POST", URL, pool=transport.SYNC_POOL, json={})
        self.assertEqual(len(sleep.durations), 1)


class CatalogPacerTests(TransportStateTestCase):
    """Catalogue GETs leave at a bounded rate.

    THE FAILURE THIS EXISTS FOR, because the reasoning that left GETs unpaced was
    documented and still wrong: Simkl names these paths parallel-safe, which was
    read as "no rate applies", and a settled instance never disproved it because
    its drain trickles. A fresh deployment's first drain — a full batch against
    an empty enrichment table — was answered 412, an instance-wide refusal that
    took the calendar's enrichment, the detail modals and signing in with Simkl
    down together for fifteen minutes.
    """

    async def test_a_healthy_instance_is_never_paced(self):
        """The default, and the one that matters for throughput: a settled
        instance fires six hundred of these in seconds and Simkl answers every
        one. Pacing every call cost a sevenfold slowdown of the drain in exchange
        for a theory that could not be reproduced from any machine."""
        sleep = RecordingSleep()
        client = FakeClient([_resp(200) for _ in range(5)])
        with _patch_sleep(sleep):
            for _ in range(5):
                await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(sleep.durations, [])

    async def test_a_refusal_arms_the_pacing_and_it_outlives_the_block(self):
        """The part that is not a theory: a 412 did happen in production, and it
        costs fifteen minutes of no Simkl at all. Going straight back to full
        rate the instant the block lifts is how an instance earns another."""
        transport._open_breaker("/tv/1")
        self.assertGreater(transport._pace_until, transport._blocked_until)
        transport._close_breaker()  # the block lifted; pacing must NOT lift with it
        transport._pace_until = transport._time.monotonic() + 60

        sleep = RecordingSleep()
        client = FakeClient([_resp(200), _resp(200)])
        with _patch_sleep(sleep):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(len(sleep.durations), 1)
        self.assertAlmostEqual(sleep.durations[0], transport.CATALOG_MIN_INTERVAL, places=2)

    async def test_a_burst_claims_distinct_slots_rather_than_agreeing_on_one(self):
        """(Armed, as it would be after a refusal.)"""
        """The property a check-then-sleep pacer would NOT have. This pool admits
        six at once; six coroutines that each read the deadline and then slept
        would wake together and burst exactly as before. Each claims its slot
        before awaiting, so the waits are staggered — 0, then one interval, then
        two, and so on."""
        transport._pace_until = transport._time.monotonic() + 60
        sleep = RecordingSleep()
        client = FakeClient([_resp(200) for _ in range(5)])
        with _patch_sleep(sleep):
            await asyncio.gather(*(
                transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
                for _ in range(5)))
        self.assertEqual(len(sleep.durations), 4)
        # ORDER AND SPACING, NOT WALL-CLOCK VALUES. The property under test is
        # that the five callers claimed FIVE DIFFERENT slots rather than agreeing
        # on one — that is what a check-then-sleep pacer would get wrong, and it
        # is visible in the waits being strictly increasing and one interval
        # apart. Asserting each duration against an absolute deadline instead
        # measured how long the test itself took to get here: the slots are
        # claimed against a real monotonic clock, so ordinary scheduler jitter
        # moved every value by a few milliseconds and the assertion flaked at
        # 10ms precision.
        waits = sorted(sleep.durations)
        self.assertEqual(waits, sorted(set(waits)), "two callers shared a slot")
        gaps = [b - a for a, b in zip(waits, waits[1:])]
        for gap in gaps:
            self.assertAlmostEqual(gap, transport.CATALOG_MIN_INTERVAL, places=2)

    async def test_the_interval_stays_under_the_published_ceiling(self):
        """10 GET/second is what Simkl publishes; the margin is because the cap is
        enforced on their clock rather than ours."""
        self.assertGreater(transport.CATALOG_MIN_INTERVAL, 1 / 10)

    async def test_the_cdn_is_not_paced(self):
        """The calendar files are static, edge-served and carry no client id, so
        they do not spend the budget this paces — and a month's fill would crawl
        for no reason."""
        transport._pace_until = transport._time.monotonic() + 60
        sleep = RecordingSleep()
        client = FakeClient([_resp(200), _resp(200)])
        with _patch_sleep(sleep):
            await transport.send(client, "GET", URL, pool=transport.CDN_POOL)
            await transport.send(client, "GET", URL, pool=transport.CDN_POOL)
        self.assertEqual(sleep.durations, [])


class RetryTests(TransportStateTestCase):
    """429: back off within a bounded budget, then raise rather than fabricate."""

    async def test_honors_retry_after_then_succeeds(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429, {"Retry-After": "2"}), _resp(200)])
        with _patch_sleep(sleep), _no_catalog_pacing():
            resp = await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleep.durations, [2.0])  # the header wins over the 1s step
        self.assertEqual(len(client.requests), 2)

    async def test_exponential_backoff_when_no_retry_after(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(200)])
        with _patch_sleep(sleep), _no_catalog_pacing():
            resp = await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleep.durations, [1.0, 2.0])  # 2**0, 2**1

    async def test_exhausted_budget_raises_rate_limit_not_none(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(429)])
        with _patch_sleep(sleep), _no_catalog_pacing():
            with self.assertRaises(SimklRateLimitError):
                await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(sleep.durations, [1.0, 2.0])  # slept twice, then gave up
        self.assertEqual(len(client.requests), 3)

    async def test_a_huge_retry_after_raises_without_sleeping_part_way_in(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(429, {"Retry-After": "254"})])
        with _patch_sleep(sleep):
            with self.assertRaises(SimklRateLimitError):
                await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(sleep.durations, [])
        self.assertEqual(len(client.requests), 1)

    async def test_a_429_does_not_open_the_breaker(self):
        """The two failures mean opposite things: 429 clears in seconds, 412 is
        the whole instance being refused. Confusing them would take Simkl away
        for a quarter of an hour every time a burst ran slightly hot."""
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(429)])
        with _patch_sleep(sleep):
            with self.assertRaises(SimklRateLimitError):
                await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(transport.blocked_seconds_remaining(), 0.0)

    async def test_a_non_429_comes_back_untouched(self):
        for status in (200, 401, 404, 500):
            with self.subTest(status=status):
                client = FakeClient([_resp(status)])
                resp = await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
                self.assertEqual(resp.status_code, status)
                self.assertEqual(len(client.requests), 1)


class BreakerTests(TransportStateTestCase):
    """412 client_id_failed is instance-wide, and retrying into it makes it
    worse. So it stops the calls locally instead."""

    async def test_a_block_does_not_follow_a_corrected_client_id(self):
        """A 412 belongs to the client id that earned it. Simkl counts its limits
        per `client_id` and answers 412 `client_id_failed` for both an invalid id
        and an active throttle block, so a DIFFERENT id is a different bucket.
        Without this an operator who fixed a mistyped id had to restart the app:
        the breaker outlived the credential that opened it."""
        blocked = f"{URL}?client_id=old-id"
        client = FakeClient([_resp(412)])
        with _patch_sleep(RecordingSleep()):
            with self.assertRaises(SimklBlockedError):
                await transport.send(client, "GET", blocked, pool=transport.CATALOG_POOL)
        self.assertGreater(transport.blocked_seconds_remaining("old-id"), 0)
        self.assertEqual(transport.blocked_seconds_remaining("new-id"), 0.0)
        # And a call made with the corrected id actually goes out.
        fresh = FakeClient([_resp(200)])
        resp = await transport.send(fresh, "GET", f"{URL}?client_id=new-id",
                                    pool=transport.CATALOG_POOL)
        self.assertEqual(resp.status_code, 200)

    async def test_a_caller_that_does_not_say_which_id_gets_the_cautious_answer(self):
        """"I did not say" must not read as "I am somebody else" — a caller with
        no id in hand (the enrichment drain asks before it starts) still sees the
        block."""
        client = FakeClient([_resp(412)])
        with _patch_sleep(RecordingSleep()):
            with self.assertRaises(SimklBlockedError):
                await transport.send(client, "GET", f"{URL}?client_id=old-id",
                                     pool=transport.CATALOG_POOL)
        self.assertGreater(transport.blocked_seconds_remaining(), 0)

    async def test_a_412_raises_blocked_and_is_never_retried(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(412), _resp(200)])
        with _patch_sleep(sleep):
            with self.assertRaises(SimklBlockedError):
                await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(sleep.durations, [])

    async def test_the_next_call_is_refused_without_a_request(self):
        client = FakeClient([_resp(412), _resp(200)])
        with self.assertRaises(SimklBlockedError):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        with self.assertRaises(SimklBlockedError):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        # The second call never reached the client at all — which is the whole
        # point, and the part a bare "it raised" assertion would not show.
        self.assertEqual(len(client.requests), 1)

    async def test_the_refusal_covers_every_pool(self):
        """A blocked client id is blocked everywhere: the catalog half and the
        sync half are one application as far as Simkl is concerned."""
        client = FakeClient([_resp(412)])
        with self.assertRaises(SimklBlockedError):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        with self.assertRaises(SimklBlockedError):
            await transport.send(client, "POST", URL, pool=transport.SYNC_POOL, json={})
        self.assertEqual(len(client.requests), 1)

    async def test_the_breaker_closes_once_the_deadline_passes(self):
        client = FakeClient([_resp(412), _resp(200)])
        # A zero cooldown puts the deadline in the past the moment it is set, so
        # the real deadline arithmetic decides this rather than a patched clock.
        with patch.object(transport, "BLOCK_COOLDOWN_SECONDS", 0.0):
            with self.assertRaises(SimklBlockedError):
                await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(transport.blocked_seconds_remaining(), 0.0)
        resp = await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(client.requests), 2)

    async def test_the_cooldown_is_a_real_wait_by_default(self):
        client = FakeClient([_resp(412)])
        with self.assertRaises(SimklBlockedError):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        remaining = transport.blocked_seconds_remaining()
        self.assertGreater(remaining, 60.0)
        self.assertLessEqual(remaining, transport.BLOCK_COOLDOWN_SECONDS)


class PrivateCachingTests(TransportStateTestCase):
    """The response cache is keyed by the request and shared by the whole
    instance, and Simkl carries the token in a header — so every user's /sync/
    request asks the same question. `private=True` is what keeps one person's
    answer from being served to another, and nothing personal may reach
    cached_get without it."""

    async def test_a_private_get_neither_reads_nor_writes_the_cache(self):
        client = FakeClient([httpx.Response(200, json={"ok": True})])
        with patch("app.cache.get") as get, patch("app.cache.set") as set_:
            out = await transport.cached_get(
                client, FAKE_SETTINGS, "sync/activities", {},
                pool=transport.SYNC_POOL, private=True)
        self.assertEqual(out, {"ok": True})
        get.assert_not_called()
        set_.assert_not_called()

    async def test_a_public_get_is_stored(self):
        client = FakeClient([httpx.Response(200, json={"title": "A Show"})])

        async def _miss(url, ttl):
            return None

        stored = {}

        async def _store(url, data):
            stored[url] = data

        with patch("app.cache.get", _miss), patch("app.cache.set", _store):
            out = await transport.cached_get(
                client, FAKE_SETTINGS, "tv/1234", {}, pool=transport.CATALOG_POOL)
        self.assertEqual(out, {"title": "A Show"})
        self.assertEqual(list(stored.values()), [{"title": "A Show"}])
        # Filed under the question, not under the credential that asked it — see
        # TheCredentialIsNotPartOfTheAddressTests below.
        self.assertNotIn("client_id", next(iter(stored)))

    async def test_a_rate_limited_read_raises_rather_than_reading_as_empty(self):
        """A swallowed 429 would look exactly like "Simkl has nothing here",
        which is how a temporary slow-down becomes a stored empty month."""
        sleep = RecordingSleep()
        client = FakeClient([_resp(429), _resp(429), _resp(429)])
        with _patch_sleep(sleep):
            with self.assertRaises(SimklRateLimitError):
                await transport.cached_get(
                    client, FAKE_SETTINGS, "sync/activities", {},
                    pool=transport.SYNC_POOL, private=True)

    async def test_a_404_still_reads_as_no_answer(self):
        client = FakeClient([_resp(404)])
        out = await transport.cached_get(
            client, FAKE_SETTINGS, "sync/activities", {},
            pool=transport.SYNC_POOL, private=True)
        self.assertIsNone(out)


class RedirectClassificationTests(unittest.TestCase):
    """Which pool a redirect TARGET deserves, decided from the target alone.

    Pure function, so these assert the rule directly rather than through a
    request: the pools are budgets, and the whole point of the change is that
    the budget is picked after the destination is known."""

    def test_a_parallel_safe_target_lands_on_the_catalog_pool(self):
        for path in ("/anime/3157124", "/tv/38636", "/movies/1234",
                     "/tv/episodes/99", "/anime/episodes/99"):
            with self.subTest(path=path):
                self.assertIs(
                    transport.redirect_pool(transport.API_BASE + "/tv/1",
                                            transport.API_BASE + path),
                    transport.CATALOG_POOL)

    def test_a_simkl_path_outside_the_family_lands_on_the_bounded_pool(self):
        """Still fetched — the data is real — but under the 10 GET/second
        budget rather than the parallel one, which is what SYNC_POOL is."""
        for path in ("/search/id", "/users/settings", "/sync/activities",
                     "/tv/38636/episodes"):
            with self.subTest(path=path):
                self.assertIs(
                    transport.redirect_pool(transport.API_BASE + "/tv/1",
                                            transport.API_BASE + path),
                    transport.SYNC_POOL)

    def test_the_episodes_endpoint_is_not_read_as_permission_for_the_subtree(self):
        """`/tv/episodes/{id}` is parallel-safe and `/tv/{id}/anything` is not.
        A prefix match would have conflated them."""
        self.assertIs(
            transport.redirect_pool(transport.API_BASE + "/tv/1",
                                    transport.API_BASE + "/tv/episodes/5"),
            transport.CATALOG_POOL)
        self.assertIs(
            transport.redirect_pool(transport.API_BASE + "/tv/1",
                                    transport.API_BASE + "/tv/5/seasons/1"),
            transport.SYNC_POOL)

    def test_another_host_is_refused(self):
        for target in ("https://evil.example/tv/1",
                       "https://api.simkl.com.evil.example/tv/1",
                       # Even Simkl's own other host: the rule is the host we
                       # were already talking to, not a host we know the name of.
                       "https://data.simkl.in/tv/1"):
            with self.subTest(target=target):
                self.assertIsNone(
                    transport.redirect_pool(transport.API_BASE + "/tv/1", target))

    def test_the_cdn_keeps_its_own_pool(self):
        """Every file on data.simkl.in is a static, edge-cached data file, so a
        hop within it needs no sub-classification — but it must not be answered
        on the API host's pool either."""
        self.assertIs(
            transport.redirect_pool("https://data.simkl.in/calendar/2026/8/a.json",
                                    "https://data.simkl.in/calendar/2026/8/b.json"),
            transport.CDN_POOL)


class RedirectRoutingTests(TransportStateTestCase):
    """Following the hop: GET /tv/{id} 302s to GET /anime/{id} for a real
    fraction of anime ids (measured live, see titles.py's module docstring), and
    the answer has to be fetched under the budget its TARGET deserves rather
    than the one its origin was issued on."""

    def _redirect(self, location: str, status: int = 302):
        return _resp(status, {"Location": location})

    async def test_a_parallel_safe_hop_is_followed_on_the_parallel_safe_pool(self):
        seen = []

        def _record(pool):
            seen.append(pool.name)
            return client

        client = FakeClient([self._redirect("/anime/3157124?client_id=cid"),
                             httpx.Response(200, json={"title": "Shiranuhi"})])
        with patch.object(transport, "client_for", _record):
            resp = await transport.send(
                client, "GET", transport.API_BASE + "/tv/3157124?client_id=cid",
                pool=transport.CATALOG_POOL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"title": "Shiranuhi"})
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[1].url,
                         transport.API_BASE + "/anime/3157124?client_id=cid")
        # Already on the right pool, so no client swap was needed at all.
        self.assertEqual(seen, [])

    async def test_a_hop_off_the_parallel_safe_family_moves_to_the_bounded_pool(self):
        client = FakeClient([self._redirect("/users/settings"),
                             httpx.Response(200, json={"ok": True})])
        swapped = []

        def _record(pool):
            swapped.append(pool)
            return client

        with patch.object(transport, "client_for", _record):
            resp = await transport.send(client, "GET", transport.API_BASE + "/tv/1",
                                        pool=transport.CATALOG_POOL)
        self.assertEqual(resp.status_code, 200)
        # The request IS made — the data is real — but under the pool whose
        # budget matches the 10 GET/second ceiling that applies off a cached path.
        self.assertEqual(swapped, [transport.SYNC_POOL])
        self.assertEqual(len(client.requests), 2)

    async def test_a_cross_host_hop_is_refused_and_never_sends_the_headers(self):
        client = FakeClient([self._redirect("https://evil.example/tv/1"),
                             httpx.Response(200, json={"stolen": True})])
        with self.assertRaises(transport.SimklError):
            await transport.send(client, "GET", transport.API_BASE + "/tv/1",
                                 pool=transport.CATALOG_POOL,
                                 headers=transport.api_headers(FAKE_SETTINGS))
        # THE ASSERTION THAT MATTERS: no second request happened at all, so
        # neither the custom app-name/credential headers httpx does not strip
        # nor the client id in the URL ever reached the other host.
        self.assertEqual(len(client.requests), 1)

    async def test_a_chain_longer_than_the_bound_stops(self):
        client = FakeClient([self._redirect("/anime/1"),
                             self._redirect("/anime/2"),
                             httpx.Response(200, json={"title": "never reached"})])
        with self.assertRaises(transport.SimklError):
            await transport.send(client, "GET", transport.API_BASE + "/tv/1",
                                 pool=transport.CATALOG_POOL)
        self.assertEqual(len(client.requests), transport.MAX_REDIRECT_HOPS + 1)

    async def test_a_redirect_without_a_location_comes_back_as_it_is(self):
        client = FakeClient([_resp(302)])
        resp = await transport.send(client, "GET", transport.API_BASE + "/tv/1",
                                    pool=transport.CATALOG_POOL)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(client.requests), 1)

    async def test_a_conditional_304_is_not_treated_as_a_hop(self):
        """The calendar CDN answers 304 to an If-None-Match, and that is an
        answer rather than a redirect."""
        client = FakeClient([_resp(304)])
        resp = await transport.send(client, "GET",
                                    "https://data.simkl.in/calendar/2026/8/a.json",
                                    pool=transport.CDN_POOL)
        self.assertEqual(resp.status_code, 304)
        self.assertEqual(len(client.requests), 1)

    async def test_a_post_redirect_is_not_replayed(self):
        """Replaying a POST at a new URL means deciding what happens to its
        body, and nothing this app POSTs to Simkl redirects."""
        client = FakeClient([self._redirect("/sync/elsewhere")])
        resp = await transport.send(client, "POST", transport.API_BASE + "/sync/add",
                                    pool=transport.SYNC_POOL, json={})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(client.requests), 1)

    async def test_no_pool_follows_a_redirect_on_its_own(self):
        """The classification is worth nothing if the client walks the hop
        before `send` ever sees it."""
        for pool in (transport.CATALOG_POOL, transport.SYNC_POOL, transport.CDN_POOL):
            with self.subTest(pool=pool.name):
                self.assertFalse(pool.client().follow_redirects)

    async def test_the_hop_is_walked_outside_the_pool_gate(self):
        """A gate held across the hop would DEADLOCK the moment a target routes
        back to the pool the origin was issued on — which is exactly what the
        /tv/{id} to /anime/{id} case does. Pinned by shrinking the gate to a
        single slot: with the walk inside it, the second leg waits on a
        semaphore its own caller is holding, forever."""
        client = FakeClient([self._redirect("/anime/1"),
                             httpx.Response(200, json={"title": "A Show"})])
        transport.CATALOG_POOL.gate()  # build the semaphore on this loop first
        original = transport.CATALOG_POOL._sem
        transport.CATALOG_POOL._sem = asyncio.Semaphore(1)
        try:
            resp = await asyncio.wait_for(
                transport.send(client, "GET", transport.API_BASE + "/tv/1",
                               pool=transport.CATALOG_POOL), timeout=5)
        finally:
            transport.CATALOG_POOL._sem = original
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(client.requests), 2)


class HeaderTests(unittest.TestCase):
    """What goes out on every request."""

    def test_a_token_is_sent_on_a_private_read(self):
        headers = transport.api_headers(FAKE_SETTINGS, private=True)
        self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_no_token_is_sent_on_a_public_one_even_when_there_is_one(self):
        """THE EDGE CACHE IS THE REASON. Measured 2026-08-21: the same
        `GET /tv/{id}` answers `cf-cache-status: BYPASS` with an Authorization
        header and `MISS` then `HIT` without it — Cloudflare will serve an entry
        somebody warmed, but will not STORE one for an authenticated request. So
        the header turned every cold catalogue lookup into an origin hit that
        left nothing behind, on exactly the endpoints Simkl allows parallel
        requests for BECAUSE they are edge-cached."""
        headers = transport.api_headers(FAKE_SETTINGS)
        self.assertNotIn("Authorization", headers)

    def test_no_empty_bearer_is_sent(self):
        """The calendar and catalog halves are unauthenticated by design, and an
        empty bearer turns a public lookup into a rejected one."""
        headers = transport.api_headers(
            SimpleNamespace(simkl_client_id="cid", simkl_access_token="", cache_ttl_minutes=10))
        self.assertNotIn("Authorization", headers)

    def test_the_application_names_itself_in_the_query_where_simkl_asks_for_it(self):
        """Simkl documents the name and version as parameters "appended to every
        request URL", not as headers — which is where this app used to put
        them."""
        params = transport.api_params(FAKE_SETTINGS, {"extended": "full"})
        self.assertEqual(params["app-name"], transport.DEFAULT_APP_NAME)
        self.assertEqual(params["app-version"], transport.app_version())
        self.assertIn(transport.APP_NAME_CALENDAR, transport.USER_AGENT)

    def test_the_two_halves_name_themselves_differently(self):
        """The tracker reads one person's history with their token; the calendar
        reads public data with the instance's. When Simkl asks which half is
        leaning on them those are different answers."""
        tracker = transport.api_params(FAKE_SETTINGS, app=transport.APP_NAME_TRACKER)
        self.assertEqual(tracker["app-name"], "distrakkt")
        self.assertEqual(transport.api_params(FAKE_SETTINGS)["app-name"], "distrakkl")

    def test_the_version_is_the_running_one_rather_than_a_second_copy(self):
        self.assertEqual(transport.app_version(),
                         changelog.current_version() or transport.APP_VERSION_FALLBACK)

    def test_the_client_id_travels_as_a_query_parameter(self):
        params = transport.api_params(FAKE_SETTINGS, {"extended": "full"})
        self.assertEqual(params["extended"], "full")
        self.assertEqual(params["client_id"], "cid")

    def test_the_callers_params_are_not_mutated(self):
        params = {"extended": "full"}
        transport.api_params(FAKE_SETTINGS, params)
        self.assertEqual(params, {"extended": "full"})


class TheCredentialIsNotPartOfTheAddressTests(TransportStateTestCase):
    """A cached Simkl answer is filed under the QUESTION, never under the
    credential that happened to ask it.

    Measured against the live service: `GET /tv/{id}?extended=full` returns the
    same fields with no client id, an empty one, a bogus one and the real one.
    The id identifies the application for rate limiting, not the content. Keying
    on it made every stored row the property of one credential, so rotating it
    stranded thousands of descriptions of titles that never depended on it.
    """

    def test_every_parameter_api_params_adds_is_one_cache_key_leaves_off(self):
        """The two halves of one fact, pinned against each other rather than
        restated: whatever `api_params` contributes is a credential, and a
        credential is what the key drops. A parameter added to one and forgotten
        in the other is the drift this phase exists to undo."""
        added = set(transport.api_params(FAKE_SETTINGS, {"extended": "full"})) - {"extended"}
        self.assertEqual(added, set(transport.CALLER_PARAMS))
        key = transport.cache_key("tv/1234", {"extended": "full"})
        for name in transport.CALLER_PARAMS:
            self.assertNotIn(name, key)

    def test_the_two_app_names_address_one_answer_rather_than_two(self):
        """The same defect the client id caused, in a new spelling: both halves
        of this app ask for the same titles, so a key carrying the caller's name
        would file one public answer under two addresses."""
        self.assertEqual(
            transport.cache_key("tv/1234", transport.api_params(
                FAKE_SETTINGS, {"extended": "full"}, app=transport.APP_NAME_TRACKER)),
            transport.cache_key("tv/1234", transport.api_params(
                FAKE_SETTINGS, {"extended": "full"}, app=transport.APP_NAME_CALENDAR)))

    def test_it_still_addresses_the_question_being_asked(self):
        """Dropping the credential must not drop what the answer is ABOUT — two
        different lookups have to stay two entries."""
        self.assertNotEqual(transport.cache_key("tv/1234", {"extended": "full"}),
                            transport.cache_key("tv/5678", {"extended": "full"}))
        self.assertNotEqual(transport.cache_key("tv/1234", {"extended": "full"}),
                            transport.cache_key("tv/1234", {}))

    def test_the_same_question_spelled_in_a_different_order_is_one_entry(self):
        self.assertEqual(transport.cache_key("search/tv", {"q": "silo", "extended": "full"}),
                         transport.cache_key("search/tv", {"extended": "full", "q": "silo"}))

    def test_a_credential_passed_in_by_a_caller_is_dropped_too(self):
        """The filter is on the NAME, not on which function put it there, so a
        caller spelling it out itself cannot smuggle it back into the key."""
        self.assertEqual(transport.cache_key("tv/1234", {"client_id": "other"}),
                         transport.cache_key("tv/1234", {}))

    async def _fetch(self, settings, *, stored, scripted=None):
        """One catalogue read against a cache that only these tests write to."""
        client = FakeClient(scripted or [httpx.Response(200, json={"title": "A Show"})])

        async def _get(key, ttl):
            return stored.get(key)

        async def _set(key, data):
            stored[key] = data

        with patch("app.cache.get", _get), patch("app.cache.set", _set):
            out = await transport.cached_get(client, settings, "tv/1234",
                                             {"extended": "full"},
                                             pool=transport.CATALOG_POOL)
        return out, client

    async def test_a_second_client_id_reads_the_first_ones_cached_answer(self):
        stored: dict = {}
        out, client = await self._fetch(FAKE_SETTINGS, stored=stored)
        self.assertEqual(out, {"title": "A Show"})
        self.assertEqual(len(client.requests), 1)
        other = SimpleNamespace(simkl_client_id="a-different-application",
                                simkl_access_token="tok", cache_ttl_minutes=10)
        # No scripted response at all: a request here would raise IndexError, so
        # this asserts the read was served rather than repeated.
        out, client = await self._fetch(other, stored=stored, scripted=[])
        self.assertEqual(out, {"title": "A Show"})
        self.assertEqual(client.requests, [])

    async def test_the_answer_survives_the_credential_being_cleared(self):
        """THE ROTATION CASE, WHICH IS WHAT THIS IS FOR. An instance that has lost
        its client id can still read back what it already described."""
        stored: dict = {}
        await self._fetch(FAKE_SETTINGS, stored=stored)
        blank = SimpleNamespace(simkl_client_id="", simkl_access_token="",
                                cache_ttl_minutes=10)
        out, client = await self._fetch(blank, stored=stored, scripted=[])
        self.assertEqual(out, {"title": "A Show"})
        self.assertEqual(client.requests, [])

    async def test_the_outgoing_request_still_carries_the_real_client_id(self):
        """Stripped from the ADDRESS, never from the request: a cold origin GET
        without one answers 412 client_id_failed."""
        _out, client = await self._fetch(FAKE_SETTINGS, stored={})
        request, = client.requests
        self.assertIn("client_id=cid", str(request.url))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class PagedReadTests(TransportStateTestCase):
    """A paginated endpoint, assembled into one answer and cached as one.

    THE PAGINATION IS INVISIBLE TO THE CALLER AND TO THE CACHE, which is the
    whole design. Simkl serves search ten results at a time and states the real
    total in `X-Pagination-Page-Count`; storing page one alone would make a cache
    hit serve a silently truncated answer with nothing to say it was short.
    """

    def _page(self, items, page_count):
        return httpx.Response(200, json=items,
                              headers={"x-pagination-page-count": str(page_count)})

    async def test_every_page_is_fetched_and_joined(self):
        client = FakeClient([self._page([1, 2], 3), self._page([3, 4], 3),
                             self._page([5], 3)])
        got = await transport.cached_paged_get(
            client, FAKE_SETTINGS, "search/tv", {"q": "joined"},
            pool=transport.CATALOG_POOL)
        self.assertEqual(got, [1, 2, 3, 4, 5])
        self.assertEqual(len(client.requests), 3)

    async def test_one_page_costs_one_request(self):
        """The ordinary query. A header saying there is one page ends the walk,
        and so does no header at all."""
        client = FakeClient([self._page([1, 2], 1)])
        got = await transport.cached_paged_get(
            client, FAKE_SETTINGS, "search/tv", {"q": "single"},
            pool=transport.CATALOG_POOL)
        self.assertEqual(got, [1, 2])
        self.assertEqual(len(client.requests), 1)

    async def test_the_assembled_answer_is_what_gets_cached(self):
        """NOT PAGE ONE — the whole list. Storing the first page would make a
        cache hit serve a silently truncated answer, which is worse than not
        caching at all: nothing downstream could tell it was short."""
        stored = {}

        async def _get(key, ttl):
            return stored.get(key)

        async def _set(key, value):
            stored[key] = value

        client = FakeClient([self._page([1], 2), self._page([2], 2)])
        with patch("app.cache.get", new=_get), patch("app.cache.set", new=_set):
            await transport.cached_paged_get(client, FAKE_SETTINGS, "search/tv",
                                             {"q": "x"}, pool=transport.CATALOG_POOL)
            self.assertEqual(list(stored.values()), [[1, 2]])
            # And a second ask is served whole, without a request.
            again = FakeClient([])
            got = await transport.cached_paged_get(again, FAKE_SETTINGS, "search/tv",
                                                   {"q": "x"}, pool=transport.CATALOG_POOL)
        self.assertEqual(got, [1, 2])
        self.assertEqual(len(again.requests), 0)

    async def test_the_page_number_is_not_part_of_the_address(self):
        key = transport.cache_key("search/tv", {"q": "x", "limit": "50"})
        self.assertNotIn("page=", key)

    async def test_the_walk_is_bounded(self):
        """Simkl caps `page` at 20 server-side. A payload claiming more must not
        turn one search into an unbounded run of requests."""
        client = FakeClient([self._page([n], 500) for n in range(40)])
        await transport.cached_paged_get(
            client, FAKE_SETTINGS, "search/tv", {"q": "bounded"},
            pool=transport.CATALOG_POOL, max_pages=3)
        self.assertEqual(len(client.requests), 3)

    async def test_pages_go_out_one_at_a_time(self):
        """Search answers `cf-cache-status: DYNAMIC` — it is not edge-cached, and
        Simkl names parallelizing uncached endpoints as a reason a client id is
        suspended. The requests are therefore strictly ordered."""
        client = FakeClient([self._page([1], 3), self._page([2], 3), self._page([3], 3)])
        await transport.cached_paged_get(
            client, FAKE_SETTINGS, "search/tv", {"q": "ordered"},
            pool=transport.CATALOG_POOL)
        pages = [r.url.split("page=")[1].split("&")[0] for r in client.requests]
        self.assertEqual(pages, ["1", "2", "3"])
