"""app/providers/tmdb/transport.py — the one way this app asks TMDB anything.

What is held here is what a TMDB answer must and must not carry with it: the
read token travels in the Authorization header and nowhere else (not the URL,
the cache key, a log line or an exception); a 429 opens a short circuit and
honours Retry-After rather than storming; a stored answer is revalidated with
its ETag and a 304 serves it; a 404 is a typed "not there", distinct from "could
not ask"; every stored answer is written to be gone by TMDB's six-month
ceiling; and the instance's TMDB language is part of what was asked.
"""
from __future__ import annotations

import logging
from unittest import mock

import httpx

from app import cache, db
from app.config import Settings
from app.providers.base import SourceUnavailable
from app.providers.tmdb import transport
from tests.integrations._fake_http import RecordingClient, pooled, response
from tests.support import DatabaseTestCase

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJzZWNyZXQtYXVkaWVuY2UifQ.dG9wLXNlY3JldC1zaWduYXR1cmU"
SETTINGS = Settings(tmdb_api_key=TOKEN)


class _Fresh(DatabaseTestCase):
    """Every test starts with the 429 circuit closed: it is real seconds, and a
    test that opened it would otherwise hold the next one."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = mock.patch.object(transport, "_CIRCUIT", transport._Circuit())
        patcher.start()
        self.addCleanup(patcher.stop)


def _no_sleep():
    """The circuit waits with asyncio.sleep; these tests count the waits instead
    of spending them."""
    return mock.patch.object(transport.asyncio, "sleep", mock.AsyncMock())


class TheTokenTravelsInTheHeaderOnlyTests(_Fresh):

    async def test_it_is_a_bearer_header(self):
        client = RecordingClient(response(200, json={"id": 1}))
        with pooled(transport, "POOL", client):
            await transport.cached_get(SETTINGS, "tv/1")
        self.assertEqual(client.only.headers["Authorization"], f"Bearer {TOKEN}")

    async def test_it_is_not_in_the_url_or_the_cache_key(self):
        client = RecordingClient(response(200, json={"id": 1}))
        with pooled(transport, "POOL", client):
            await transport.cached_get(SETTINGS, "tv/1", {"append_to_response": "videos"})
        self.assertNotIn(TOKEN, client.only.url)
        keys = [row["cache_key"] for row in await db.fetch_all("SELECT cache_key FROM api_cache")]
        self.assertEqual(len(keys), 1)
        self.assertNotIn(TOKEN, keys[0])
        self.assertNotIn("api_key", keys[0])

    async def test_it_is_not_in_a_log_line_or_an_exception(self):
        for status in (401, 500):
            with self.subTest(status=status):
                client = RecordingClient(response(status, json={"status_message": "no"}))
                with pooled(transport, "POOL", client), \
                        self.assertLogs("app.providers.tmdb.transport", logging.WARNING) as logs:
                    with self.assertRaises(transport.TmdbError) as raised:
                        await transport.cached_get(SETTINGS, f"tv/{status}")
                self.assertNotIn(TOKEN, "\n".join(logs.output))
                self.assertNotIn(TOKEN, str(raised.exception))

    async def test_a_network_failure_says_nothing_of_the_token(self):
        client = RecordingClient(response(200), raises=httpx.ConnectError(f"boom {TOKEN[:4]}"))
        with pooled(transport, "POOL", client), self.assertRaises(transport.TmdbError) as raised:
            await transport.cached_get(SETTINGS, "tv/1")
        self.assertNotIn(TOKEN, str(raised.exception))

    async def test_a_stored_v3_key_is_never_sent(self):
        """TMDB also takes a short key as `?api_key=`; this app will not put a
        credential in a URL, so a stored one reads as no credential at all."""
        client = RecordingClient(response(200, json={}))
        settings = Settings(tmdb_api_key="0123456789abcdef0123456789abcdef")
        with pooled(transport, "POOL", client), self.assertRaises(transport.TmdbError):
            await transport.cached_get(settings, "tv/1")
        self.assertEqual(client.calls, [])

    async def test_an_image_download_carries_no_credential(self):
        client = RecordingClient(response(200, content=b"x"))
        with pooled(transport, "POOL", client):
            await transport.download("https://image.tmdb.org/t/p/w500/x.jpg")
        self.assertIn(client.only.headers, (None, {}))


class A429OpensTheCircuitTests(_Fresh):

    async def test_retry_after_is_honoured_and_the_answer_comes_back(self):
        client = RecordingClient([response(429), response(200, json={"id": 7})])
        client.responses[0].headers["retry-after"] = "3"
        with pooled(transport, "POOL", client), _no_sleep() as slept:
            self.assertEqual(await transport.cached_get(SETTINGS, "tv/7"), {"id": 7})
        self.assertEqual(len(client.calls), 2)
        self.assertAlmostEqual(slept.await_args.args[0], 3, delta=0.5)

    async def test_two_429s_raise_rather_than_retrying_for_ever(self):
        client = RecordingClient([response(429), response(429)])
        with pooled(transport, "POOL", client), _no_sleep(), \
                self.assertLogs("app.providers.tmdb.transport", logging.WARNING):
            with self.assertRaises(transport.TmdbRateLimited):
                await transport.cached_get(SETTINGS, "tv/8")
        self.assertEqual(len(client.calls), 2)

    async def test_a_long_retry_after_refuses_the_next_request_without_sending_it(self):
        """No storm: while the circuit is open past what a request may wait,
        nothing more leaves for TMDB."""
        first = response(429)
        first.headers["retry-after"] = "50"
        client = RecordingClient([first, response(429)])
        with pooled(transport, "POOL", client), _no_sleep(), \
                self.assertLogs("app.providers.tmdb.transport", logging.WARNING):
            with self.assertRaises(transport.TmdbRateLimited):
                await transport.cached_get(SETTINGS, "tv/9")
            sent = len(client.calls)
            with self.assertRaises(transport.TmdbRateLimited):
                await transport.cached_get(SETTINGS, "tv/10")
        self.assertEqual(sent, 1)
        self.assertEqual(len(client.calls), 1)


class ConditionalGetTests(_Fresh):

    async def test_a_304_serves_the_stored_body_and_restamps_it(self):
        client = RecordingClient([response(200, json={"name": "Severance"})])
        client.responses[0].headers["etag"] = 'W/"abc"'
        with pooled(transport, "POOL", client):
            await transport.cached_get(SETTINGS, "tv/95396")
        await db.execute("UPDATE api_cache SET cached_at = cached_at - 999999")
        client = RecordingClient(response(304, content=b""))
        with pooled(transport, "POOL", client):
            body = await transport.cached_get(SETTINGS, "tv/95396")
        self.assertEqual(body, {"name": "Severance"})
        self.assertEqual(client.only.headers["If-None-Match"], 'W/"abc"')
        stamped = await db.fetch_value("SELECT cached_at FROM api_cache")
        self.assertGreater(stamped, db.now() - 60)

    async def test_a_fresh_answer_is_served_with_no_request(self):
        client = RecordingClient(response(200, json={"id": 1}))
        with pooled(transport, "POOL", client):
            await transport.cached_get(SETTINGS, "tv/1")
            await transport.cached_get(SETTINGS, "tv/1")
        self.assertEqual(len(client.calls), 1)

    async def test_cache_only_never_calls(self):
        client = RecordingClient(response(200, json={}))
        with pooled(transport, "POOL", client):
            self.assertIsNone(await transport.cached_get(SETTINGS, "tv/1", cache_only=True))
        self.assertEqual(client.calls, [])


class A404IsNotAFailureTests(_Fresh):

    async def test_it_raises_its_own_type(self):
        client = RecordingClient(response(404, json={"status_code": 34}))
        with pooled(transport, "POOL", client), self.assertRaises(transport.TmdbNotFound):
            await transport.cached_get(SETTINGS, "tv/999999999")

    def test_the_type_is_not_the_could_not_answer_contract(self):
        """A caller catching SourceUnavailable must never swallow "TMDB has no
        such season" — that answer hands the season to the next service."""
        self.assertFalse(issubclass(transport.TmdbNotFound, SourceUnavailable))
        self.assertTrue(issubclass(transport.TmdbError, SourceUnavailable))


class TheSixMonthCeilingTests(_Fresh):

    def test_a_row_is_gone_by_the_ceiling_once_the_sweep_adds_its_grace(self):
        self.assertEqual(transport.CACHE_RETAIN_SECONDS + cache.TTL_GRACE_SECONDS,
                         transport.MAX_CACHE_SECONDS)
        self.assertEqual(transport.MAX_CACHE_SECONDS, 180 * 24 * 60 * 60)

    async def test_every_answer_is_written_with_that_retain(self):
        client = RecordingClient(response(200, json={"id": 1}))
        with pooled(transport, "POOL", client):
            await transport.cached_get(SETTINGS, "tv/1")
        self.assertEqual(await db.fetch_value("SELECT ttl_seconds FROM api_cache"),
                         transport.CACHE_RETAIN_SECONDS)

    async def test_the_sweep_removes_it_at_six_months(self):
        client = RecordingClient(response(200, json={"id": 1}))
        with pooled(transport, "POOL", client):
            await transport.cached_get(SETTINGS, "tv/1")
        await cache.sweep(now=db.now() + transport.MAX_CACHE_SECONDS + 1)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM api_cache"), 0)


class TheLanguageIsPartOfTheQuestionTests(_Fresh):

    async def test_it_is_sent_and_keys_the_answer(self):
        client = RecordingClient([response(200, json={"name": "Severance"}),
                                  response(200, json={"name": "Severance (DE)"})])
        with pooled(transport, "POOL", client):
            english = await transport.cached_get(SETTINGS, "tv/1")
            german = await transport.cached_get(
                Settings(tmdb_api_key=TOKEN, tmdb_language="de-DE"), "tv/1")
        self.assertEqual((english["name"], german["name"]), ("Severance", "Severance (DE)"))
        self.assertIn("language=en-US", client.calls[0].url)
        self.assertIn("language=de-DE", client.calls[1].url)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM api_cache"), 2)
