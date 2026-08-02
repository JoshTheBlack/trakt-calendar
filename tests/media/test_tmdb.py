"""app/media/tmdb.py — the shared TMDB client, which had no test file.

Both app/media/posters.py and app/media/logos.py fetch through this module, and
their own tests patch their own `tmdb` attribute, so nothing had ever exercised
`get_json` or `download` themselves.

THE AUTH BRANCH IS THE REASON THIS FILE EXISTS. TMDB accepts two credentials that
look nothing alike and travel by different routes: a v3 API key goes in the query
string as `api_key`, and a v4 read token goes in an `Authorization: Bearer`
header. The module picks between them by inspecting the key's SHAPE. That is
exactly the sort of logic that silently does the wrong thing while appearing to
work — a v4 token sent as `api_key` gets a 401, which this module converts to
None, which both callers degrade on. The result is missing artwork and no error
anywhere, on an instance whose credentials are perfectly valid.

The failure-to-None paths are here too, because degrading rather than raising is
the contract both callers are written against.
"""
from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

import httpx

from app.media import tmdb

from ..integrations._fake_http import RecordingClient, pooled, response

# A v3 key is short hex; a v4 read token is a long JWT beginning "eyJ".
V3_KEY = "0123456789abcdef0123456789abcdef"
V4_TOKEN = "eyJ" + "a" * 200


def settings(key: str) -> SimpleNamespace:
    # A SimpleNamespace rather than Settings: this module reads exactly one
    # attribute, and saying so is more honest than standing up the whole record.
    return SimpleNamespace(tmdb_api_key=key)


def tmdb_pool(client: RecordingClient):
    return pooled(tmdb, "POOL", client)


class KeyShapeTests(unittest.TestCase):
    """`_is_v4_token` — the whole branch turns on this one predicate."""

    def test_a_jwt_prefix_is_a_v4_token(self):
        self.assertTrue(tmdb._is_v4_token("eyJhbGciOiJIUzI1NiJ9.short"))

    def test_a_long_key_is_treated_as_v4_even_without_the_prefix(self):
        # The length test is the backstop for a token that does not start "eyJ".
        self.assertTrue(tmdb._is_v4_token("x" * 61))

    def test_a_short_hex_key_is_v3(self):
        self.assertFalse(tmdb._is_v4_token(V3_KEY))

    def test_the_boundary_is_sixty(self):
        # Recorded because it is an arbitrary constant, and a reader who finds it
        # should be able to see that it was chosen rather than drifted into: a
        # v3 key is 32 hex characters, so 60 leaves a wide margin either side.
        self.assertFalse(tmdb._is_v4_token("x" * 60))
        self.assertTrue(tmdb._is_v4_token("x" * 61))

    def test_an_empty_key_is_v3(self):
        # It has to land somewhere; v3 means an empty `api_key` param, which TMDB
        # refuses cleanly, rather than an empty Bearer header, which is stranger.
        self.assertFalse(tmdb._is_v4_token(""))


class V3AuthTests(unittest.IsolatedAsyncioTestCase):
    """A v3 key travels as a query parameter and no Authorization header."""

    async def test_the_key_goes_in_the_query_string(self):
        client = RecordingClient(response(200, json={"id": 1}))
        with tmdb_pool(client):
            await tmdb.get_json(settings(V3_KEY), "/tv/42", "tmdb.test")
        self.assertEqual(client.only.params, {"api_key": V3_KEY})

    async def test_no_authorization_header_is_sent(self):
        client = RecordingClient(response(200, json={}))
        with tmdb_pool(client):
            await tmdb.get_json(settings(V3_KEY), "/tv/42", "tmdb.test")
        self.assertEqual(client.only.headers, {})

    async def test_the_path_is_appended_to_the_api_base(self):
        client = RecordingClient(response(200, json={}))
        with tmdb_pool(client):
            await tmdb.get_json(settings(V3_KEY), "/tv/42/season/1", "tmdb.test")
        self.assertEqual(client.only.url,
                         "https://api.themoviedb.org/3/tv/42/season/1")


class V4AuthTests(unittest.IsolatedAsyncioTestCase):
    """A v4 token travels as a Bearer header and never as a query parameter."""

    async def test_the_token_goes_in_an_authorization_header(self):
        client = RecordingClient(response(200, json={}))
        with tmdb_pool(client):
            await tmdb.get_json(settings(V4_TOKEN), "/tv/42", "tmdb.test")
        self.assertEqual(client.only.headers, {"Authorization": f"Bearer {V4_TOKEN}"})

    async def test_no_api_key_parameter_is_sent(self):
        """Sending both would be the tempting "just do each" fix and is wrong:
        TMDB rejects the combination, and the token would then also be sitting in
        the query string, which is where credentials end up in access logs."""
        client = RecordingClient(response(200, json={}))
        with tmdb_pool(client):
            await tmdb.get_json(settings(V4_TOKEN), "/tv/42", "tmdb.test")
        self.assertEqual(client.only.params, {})
        self.assertNotIn(V4_TOKEN, client.only.url)

    async def test_the_two_shapes_are_mutually_exclusive(self):
        # Stated as one assertion over both branches, because the property is
        # about the pair rather than about either one.
        for key in (V3_KEY, V4_TOKEN):
            with self.subTest(key=key[:8]):
                client = RecordingClient(response(200, json={}))
                with tmdb_pool(client):
                    await tmdb.get_json(settings(key), "/tv/42", "tmdb.test")
                has_header = "Authorization" in (client.only.headers or {})
                has_param = "api_key" in (client.only.params or {})
                self.assertNotEqual(has_header, has_param,
                                    "exactly one of the two auth shapes must be used")


class KeyHandlingTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_key_with_surrounding_whitespace_is_stripped(self):
        client = RecordingClient(response(200, json={}))
        with tmdb_pool(client):
            await tmdb.get_json(settings(f"  {V3_KEY}\n"), "/tv/42", "tmdb.test")
        self.assertEqual(client.only.params, {"api_key": V3_KEY})

    async def test_a_missing_key_does_not_raise(self):
        # `(settings.tmdb_api_key or "")` — an instance with no TMDB key still
        # loads pages, it just gets no artwork.
        client = RecordingClient(response(401, json={}))
        with tmdb_pool(client):
            self.assertIsNone(await tmdb.get_json(settings(None), "/tv/42", "tmdb.test"))


class GetJsonFailureTests(unittest.IsolatedAsyncioTestCase):
    """Every failure becomes None so a caller can degrade rather than raise."""

    async def test_a_good_response_is_returned_parsed(self):
        client = RecordingClient(response(200, json={"id": 42, "name": "A Show"}))
        with tmdb_pool(client):
            out = await tmdb.get_json(settings(V3_KEY), "/tv/42", "tmdb.test")
        self.assertEqual(out, {"id": 42, "name": "A Show"})

    async def test_a_non_200_becomes_none(self):
        for status in (401, 404, 429, 500):
            with self.subTest(status=status):
                client = RecordingClient(response(status, json={}))
                with tmdb_pool(client), self.assertLogs("app.media.tmdb", logging.WARNING):
                    self.assertIsNone(
                        await tmdb.get_json(settings(V3_KEY), "/tv/42", "tmdb.test"))

    async def test_a_network_error_becomes_none(self):
        client = RecordingClient(response(200), raises=httpx.ConnectError("no route"))
        with tmdb_pool(client), self.assertLogs("app.media.tmdb", logging.WARNING):
            self.assertIsNone(await tmdb.get_json(settings(V3_KEY), "/tv/42", "tmdb.test"))

    async def test_an_unparsable_body_becomes_none(self):
        client = RecordingClient(response(200, text="<html>not json</html>"))
        with tmdb_pool(client):
            self.assertIsNone(await tmdb.get_json(settings(V3_KEY), "/tv/42", "tmdb.test"))

    async def test_the_failure_log_does_not_carry_the_key(self):
        """A warning line is the one place a credential could escape into a log
        an operator might paste somewhere. The line names the PATH and the status,
        and the path never carries the key on either branch — which is the v4
        header's second benefit and worth an assertion rather than a reasoned
        argument."""
        for key in (V3_KEY, V4_TOKEN):
            with self.subTest(key=key[:8]):
                client = RecordingClient(response(500, json={}))
                with tmdb_pool(client), \
                        self.assertLogs("app.media.tmdb", logging.WARNING) as caught:
                    await tmdb.get_json(settings(key), "/tv/42", "tmdb.test")
                self.assertNotIn(key, "\n".join(caught.output))


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    """`download` fetches raw bytes from any URL, with NO auth at all."""

    async def test_it_returns_the_body_bytes(self):
        client = RecordingClient(response(200, content=b"\x89PNG..."))
        with tmdb_pool(client):
            out = await tmdb.download("https://image.tmdb.org/t/p/w500/x.jpg")
        self.assertEqual(out, b"\x89PNG...")

    async def test_it_sends_no_credentials(self):
        # TMDB's image CDN and Trakt's poster URLs are both unauthenticated, and
        # sending a key to a URL recorded from another provider would hand that
        # provider a TMDB credential.
        client = RecordingClient(response(200, content=b"x"))
        with tmdb_pool(client):
            await tmdb.download("https://images.example.com/poster.jpg")
        self.assertIn(client.only.headers, (None, {}))
        self.assertIn(client.only.params, (None, {}))

    async def test_the_url_is_used_exactly_as_given(self):
        client = RecordingClient(response(200, content=b"x"))
        with tmdb_pool(client):
            await tmdb.download("https://walter.trakt.tv/images/shows/1/posters/x.jpg")
        self.assertEqual(client.only.url,
                         "https://walter.trakt.tv/images/shows/1/posters/x.jpg")

    async def test_a_non_200_becomes_none(self):
        client = RecordingClient(response(404, content=b""))
        with tmdb_pool(client), self.assertLogs("app.media.tmdb", logging.WARNING):
            self.assertIsNone(await tmdb.download("https://image.tmdb.org/t/p/w500/x.jpg"))

    async def test_a_network_error_becomes_none(self):
        client = RecordingClient(response(200), raises=httpx.ReadTimeout("slow"))
        with tmdb_pool(client), self.assertLogs("app.media.tmdb", logging.WARNING):
            self.assertIsNone(await tmdb.download("https://image.tmdb.org/t/p/w500/x.jpg"))

    async def test_an_empty_body_at_200_is_returned_rather_than_treated_as_failure(self):
        # b"" is falsy, so a caller doing `if not await download(...)` cannot tell
        # it from None. Recorded as what the function actually does; the caller's
        # handling of a zero-byte image is its own business.
        client = RecordingClient(response(200, content=b""))
        with tmdb_pool(client):
            self.assertEqual(await tmdb.download("https://image.tmdb.org/t/p/w500/x.jpg"), b"")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
