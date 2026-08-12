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

    async def test_the_first_catalogue_get_is_not_delayed(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(200)])
        with _patch_sleep(sleep):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(sleep.durations, [])

    async def test_a_second_catalogue_get_waits_out_the_interval(self):
        sleep = RecordingSleep()
        client = FakeClient([_resp(200), _resp(200)])
        with _patch_sleep(sleep):
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
            await transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
        self.assertEqual(len(sleep.durations), 1)
        self.assertAlmostEqual(sleep.durations[0], transport.CATALOG_MIN_INTERVAL, places=2)

    async def test_a_burst_claims_distinct_slots_rather_than_agreeing_on_one(self):
        """The property a check-then-sleep pacer would NOT have. This pool admits
        six at once; six coroutines that each read the deadline and then slept
        would wake together and burst exactly as before. Each claims its slot
        before awaiting, so the waits are staggered — 0, then one interval, then
        two, and so on."""
        sleep = RecordingSleep()
        client = FakeClient([_resp(200) for _ in range(5)])
        with _patch_sleep(sleep):
            await asyncio.gather(*(
                transport.send(client, "GET", URL, pool=transport.CATALOG_POOL)
                for _ in range(5)))
        self.assertEqual(len(sleep.durations), 4)
        for i, waited in enumerate(sorted(sleep.durations), start=1):
            self.assertAlmostEqual(waited, i * transport.CATALOG_MIN_INTERVAL, places=2)

    async def test_the_interval_stays_under_the_published_ceiling(self):
        """10 GET/second is what Simkl publishes; the margin is because the cap is
        enforced on their clock rather than ours."""
        self.assertGreater(transport.CATALOG_MIN_INTERVAL, 1 / 10)

    async def test_the_cdn_is_not_paced(self):
        """The calendar files are static, edge-served and carry no client id, so
        they do not spend the budget this paces — and a month's fill would crawl
        for no reason."""
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
    """The response cache is URL-keyed and shared by the whole instance, and
    Simkl carries the token in a header — so every user's /sync/ request has the
    same URL. `private=True` is what keeps one person's answer from being served
    to another, and nothing personal may reach cached_get without it."""

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
        # The client id is part of the URL and therefore part of the key: another
        # application's answers are not this one's to serve.
        self.assertIn("client_id=cid", next(iter(stored)))

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

    def test_a_token_is_sent_when_there_is_one(self):
        headers = transport.api_headers(FAKE_SETTINGS)
        self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_no_empty_bearer_is_sent(self):
        """The calendar and catalog halves are unauthenticated by design, and an
        empty bearer turns a public lookup into a rejected one."""
        headers = transport.api_headers(
            SimpleNamespace(simkl_client_id="cid", simkl_access_token="", cache_ttl_minutes=10))
        self.assertNotIn("Authorization", headers)

    def test_the_application_names_itself(self):
        headers = transport.api_headers(FAKE_SETTINGS)
        self.assertEqual(headers["app-name"], transport.APP_NAME)
        self.assertEqual(headers["app-version"], transport.APP_VERSION)
        self.assertIn("trakt-new-shows", headers["User-Agent"])

    def test_the_client_id_travels_as_a_query_parameter(self):
        self.assertEqual(transport.api_params(FAKE_SETTINGS, {"extended": "full"}),
                         {"extended": "full", "client_id": "cid"})

    def test_the_callers_params_are_not_mutated(self):
        params = {"extended": "full"}
        transport.api_params(FAKE_SETTINGS, params)
        self.assertEqual(params, {"extended": "full"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
