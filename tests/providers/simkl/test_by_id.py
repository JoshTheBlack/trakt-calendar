"""Simkl's half of SearchPort.find_by_shared_id, through `/redirect`, and
DetailPort.fetch_title_facts.

`/redirect` answers with a Location naming the title — `simkl.com/{tv|anime|
movies}/{id}/{slug}` — and Simkl's own doc says not to follow it. So what is
pinned: the Location is READ and never followed, only a title of the media asked
for is accepted, and the extracted answer (including "not listed") is kept for a
day, while a failure to ask is never remembered as an answer.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.providers.base import Media, TitleFacts
from app.providers.simkl import SimklError, by_id, detail, transport
from tests.support import DatabaseTestCase

SETTINGS = Settings(simkl_client_id="scid")


class LocationTests(unittest.TestCase):
    def test_a_show_or_anime_location_names_the_id_and_the_slug(self):
        for kind in ("tv", "anime"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    by_id.ids_from_location(f"https://simkl.com/{kind}/694485/aot-s3", Media.SHOW),
                    {"simkl": 694485, "simkl_slug": "aot-s3"})

    def test_a_film_is_not_filed_on_a_show(self):
        self.assertEqual(by_id.ids_from_location("https://simkl.com/movies/5/x", Media.SHOW), {})

    def test_anything_else_is_not_listed(self):
        for location in ("", "https://simkl.com/search?q=x", "https://example.com/tv/1/x"):
            with self.subTest(location=location):
                self.assertEqual(by_id.ids_from_location(location, Media.SHOW), {})


class FindBySharedIdTests(DatabaseTestCase):
    async def test_an_answer_is_read_off_the_redirect_and_kept(self):
        target = AsyncMock(return_value="https://simkl.com/tv/39687/shingeki")
        with patch.object(transport, "redirect_target", target):
            first = await by_id.find_by_shared_id(SETTINGS, Media.SHOW, {"tmdb": 1429})
            second = await by_id.find_by_shared_id(SETTINGS, Media.SHOW, {"tmdb": 1429})
        self.assertEqual(first, {"simkl": 39687, "simkl_slug": "shingeki"})
        self.assertEqual(second, first)
        target.assert_awaited_once()
        self.assertEqual(target.await_args.args[2],
                         {"to": "simkl", "tmdb": 1429, "type": "tv"})

    async def test_not_listed_is_kept_too_and_the_next_id_is_tried(self):
        answers = {"tmdb": "", "imdb": "https://simkl.com/tv/7/found"}

        async def redirect(settings, path, params):
            return answers["tmdb" if "tmdb" in params else "imdb"]
        with patch.object(transport, "redirect_target", AsyncMock(side_effect=redirect)) as target:
            got = await by_id.find_by_shared_id(SETTINGS, Media.SHOW, {"tmdb": 1, "imdb": "tt1"})
            await by_id.find_by_shared_id(SETTINGS, Media.SHOW, {"tmdb": 1, "imdb": "tt1"})
        self.assertEqual(got["simkl"], 7)
        self.assertEqual(target.await_count, 2, "a kept answer was asked for again")

    async def test_a_failure_to_ask_raises_and_is_not_remembered(self):
        with patch.object(transport, "redirect_target",
                          AsyncMock(side_effect=SimklError("down"))):
            with self.assertRaises(SimklError):
                await by_id.find_by_shared_id(SETTINGS, Media.SHOW, {"tmdb": 2})
        with patch.object(transport, "redirect_target",
                          AsyncMock(return_value="https://simkl.com/tv/9/x")):
            self.assertEqual((await by_id.find_by_shared_id(
                SETTINGS, Media.SHOW, {"tmdb": 2}))["simkl"], 9)


class _Client:
    """One scripted answer per request, recording each — so "never followed"
    is a count of requests rather than an inference."""

    def __init__(self, answer):
        self.answer, self.requests, self.headers = answer, [], []

    async def get(self, url, headers=None, timeout=None):
        self.requests.append(url)
        self.headers.append(headers or {})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class RedirectTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        transport._close_breaker()
        self.addCleanup(transport._close_breaker)

    async def _ask(self, answer):
        client = _Client(answer)
        with patch.object(transport, "sync_client", return_value=client):
            got = await transport.redirect_target(SETTINGS, "redirect", {"tmdb": 1})
        return got, client

    async def test_the_location_is_read_and_not_followed(self):
        got, client = await self._ask(httpx.Response(
            301, headers={"location": "https://simkl.com/tv/1/x"}))
        self.assertEqual(got, "https://simkl.com/tv/1/x")
        self.assertEqual(len(client.requests), 1)
        self.assertIn("client_id=scid", client.requests[0])

    async def test_no_redirect_is_not_listed(self):
        for status in (200, 404):
            with self.subTest(status=status):
                got, _client = await self._ask(httpx.Response(status))
                self.assertEqual(got, "")

    async def test_an_error_or_no_connection_raises(self):
        for answer in (httpx.Response(500), httpx.ConnectError("down")):
            with self.subTest(answer=repr(answer)):
                with self.assertRaises(SimklError):
                    await self._ask(answer)

    async def test_the_callers_own_token_is_attached(self):
        """/redirect requires a user token under Simkl's v2 API (fact 3 of the
        cutover) — the caller's own bearer must ride every request, never the
        instance's, which is what `private=True` on api_headers means."""
        with_token = Settings(simkl_client_id="scid", simkl_access_token="viewer-tok")
        client = _Client(httpx.Response(200))
        with patch.object(transport, "sync_client", return_value=client):
            await transport.redirect_target(with_token, "redirect", {"tmdb": 1})
        self.assertEqual(client.headers[0].get("Authorization"), "Bearer viewer-tok")


class TitleFactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_season_title_is_its_own_name_and_its_series_network(self):
        naming = SimpleNamespace(title="Shingeki no Kyojin Season 3", network="",
                                 ids={}, season=3, siblings=(39687,))
        with patch("app.providers.simkl._naming.fetch", AsyncMock(return_value=naming)), \
             patch("app.providers.simkl._naming.network_of_series",
                   AsyncMock(return_value="NHK")):
            got = await detail.fetch_title_facts(SETTINGS, 694485, Media.SHOW)
        self.assertEqual(got, TitleFacts("Shingeki no Kyojin Season 3", "NHK"))
