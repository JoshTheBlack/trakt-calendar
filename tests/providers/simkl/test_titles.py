"""Simkl's per-title CATALOG detail (GET /tv/{id}, GET /movies/{id}) — the
enrichment drain's only source of genres, network, country, certification,
runtime, status and overview.

Guards the two measured, non-obvious facts titles.py's docstring documents:
GET /tv/{id} answers for an anime id too (so there is no third endpoint), and
Simkl's "not found" does not reliably come back as a 404 (so a caller cannot
trust the status code alone).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.providers.base import Media
from app.providers.simkl import titles, transport

SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="", cache_ttl_minutes=10)

ANIME_PAYLOAD = {
    "title": "One Piece",
    "ids": {"simkl": 38636, "slug": "one-piece", "tmdb": "37854", "imdb": "tt0388629",
            "tvdb": "81797", "mal": "21", "anidb": "69"},
    "genres": ["Action", "Adventure", "Martial Arts"],
    "country": "JP",
    "certification": "PG-13",
    "network": "Fuji TV",
    "status": "airing",
    "runtime": 24,
    "overview": "A pirate crew searches for treasure.",
    "type": "anime",
}

TV_PAYLOAD = {
    "title": "Breaking Bad",
    "ids": {"simkl": 11121, "slug": "breaking-bad", "tmdb": "1396", "imdb": "tt0903747",
            "tvdb": "81189"},
    "genres": ["Crime", "Drama"],
    "country": "US",
    "certification": "TV-MA",
    "network": "AMC",
    "status": "ended",
    "runtime": 47,
    "overview": "A teacher turns to crime.",
}


def _cached_get(*answers):
    return AsyncMock(side_effect=list(answers))


class FetchTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_show_is_read_through_the_tv_path(self):
        spy = _cached_get(TV_PAYLOAD)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            fields = await titles.fetch_title(SETTINGS, 11121, Media.SHOW)
        call = spy.await_args
        self.assertEqual(call.args[2], "tv/11121")
        self.assertIs(call.kwargs["pool"], transport.CATALOG_POOL)
        self.assertEqual(fields["genres"], ["crime", "drama"])
        self.assertEqual(fields["country"], "US")
        self.assertEqual(fields["certification"], "TV-MA")
        self.assertEqual(fields["network"], "AMC")
        self.assertEqual(fields["status"], "ended")
        self.assertEqual(fields["runtime"], 47)
        self.assertEqual(fields["ids"], {"tvdb": "81189"})

    async def test_a_movie_is_read_through_the_movies_path(self):
        payload = {**TV_PAYLOAD, "ids": {"simkl": 472214, "tmdb": "27205"}}
        spy = _cached_get(payload)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            await titles.fetch_title(SETTINGS, 472214, Media.MOVIE)
        self.assertEqual(spy.await_args.args[2], "movies/472214")

    async def test_an_anime_id_answers_through_the_tv_path_with_no_second_call(self):
        """Measured live 2026-08-06: GET /tv/{id} for an anime id returns every
        field GET /anime/{id} does. A Simkl 'show' Record never records whether
        the title is TV or anime, so this is the only way enrichment can work
        without guessing — and it must cost exactly one call, not a fallback."""
        spy = _cached_get(ANIME_PAYLOAD)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            fields = await titles.fetch_title(SETTINGS, 38636, Media.SHOW)
        self.assertEqual(spy.await_args.args[2], "tv/38636")
        spy.assert_awaited_once()
        self.assertEqual(fields["genres"], ["action", "adventure", "martial-arts"])
        self.assertEqual(fields["ids"], {"tvdb": "81797", "mal": "21", "anidb": "69"})

    async def test_genres_are_slugged_to_match_trakts_own_spelling(self):
        """Simkl spells 'Game Show'; Trakt's calendar payload already arrives as
        'game-show', which is what a viewer's filter spec is written against."""
        payload = {**TV_PAYLOAD, "genres": ["Game Show", "Reality"]}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
        self.assertEqual(fields["genres"], ["game-show", "reality"])

    async def test_an_empty_list_answer_is_treated_as_not_found(self):
        """Measured live: an id Simkl does not recognise answers 200 with `[]`,
        never a 404 — the shape /tv/episodes/{id} already uses for the same
        reason (see test_detail.py)."""
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get([])):
            self.assertIsNone(await titles.fetch_title(SETTINGS, 999999999, Media.SHOW))

    async def test_a_page_that_is_not_a_title_is_treated_as_not_found(self):
        """Measured live: id 0 and a non-numeric tv id both answer 200 with an
        unrelated 'top aired' digest rather than an error or an empty list.
        Accepting anything without `ids`+`title` would enrich a record with
        data belonging to no title at all."""
        garbage = {"top_aired_fanarts": [{"title": "All Elite Wrestling: Dynamite"}]}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(garbage)):
            self.assertIsNone(await titles.fetch_title(SETTINGS, 0, Media.SHOW))

    async def test_a_source_failure_is_not_found_rather_than_raised(self):
        """The caller (the enrichment drain) treats a real failure and an
        unrecognised id identically — both back off the same way."""
        spy = AsyncMock(side_effect=transport.SimklError("boom"))
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            self.assertIsNone(await titles.fetch_title(SETTINGS, 1, Media.SHOW))

    async def test_a_missing_id_or_unmapped_media_costs_no_request(self):
        spy = _cached_get()
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            self.assertIsNone(await titles.fetch_title(SETTINGS, None, Media.SHOW))
        spy.assert_not_awaited()

    async def test_a_title_with_no_runtime_or_genres_answers_with_every_key(self):
        payload = {"title": "X", "ids": {"simkl": 1}}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
        self.assertEqual(set(fields), {"genres", "network", "country", "certification",
                                       "runtime", "status", "overview", "ids"})
        self.assertEqual((fields["genres"], fields["runtime"], fields["ids"]), ([], None, {}))


class AnimeRedirectTests(unittest.IsolatedAsyncioTestCase):
    """Regression for the 62-of-260-empty-payloads defect: measured live
    2026-08-06, GET /tv/{id} 302s to GET /anime/{id} for a real fraction of
    anime ids (100% of a 94-id sample pulled from the author's live
    database). Driven through a REAL httpx.AsyncClient with a MockTransport
    — genuine redirect-following, no socket — rather than patching
    cached_get, because the bug lived in whether the client follows the
    Location header at all, one layer below cached_get's own logic."""

    async def test_a_redirected_tv_lookup_still_answers(self):
        anime_body = {
            "title": "GuAn", "ids": {"simkl": 3198578, "mal": "62789"},
            "genres": ["Action", "Fantasy"], "country": "CN",
            "certification": "PG-13", "network": "Youku", "status": "airing",
            "runtime": 30, "overview": "A crippled youth seizes divine power.",
        }

        def handler(request):
            if request.url.path == "/tv/3198578":
                return httpx.Response(
                    302, headers={"location": "/anime/3198578?client_id=cid"})
            if request.url.path == "/anime/3198578":
                return httpx.Response(200, json=anime_body)
            raise AssertionError(f"unexpected path {request.url.path}")

        client = httpx.AsyncClient(follow_redirects=True,
                                   transport=httpx.MockTransport(handler))
        with patch("app.providers.simkl.transport.catalog_client", return_value=client), \
             patch("app.cache.get", AsyncMock(return_value=None)), \
             patch("app.cache.set", AsyncMock()):
            fields = await titles.fetch_title(SETTINGS, 3198578, Media.SHOW)
        self.assertIsNotNone(fields)
        self.assertEqual(fields["genres"], ["action", "fantasy"])
        self.assertEqual(fields["country"], "CN")
        self.assertEqual(fields["network"], "Youku")

    async def test_a_client_that_does_not_follow_redirects_would_lose_it(self):
        """The other half of the same proof: without follow_redirects, the
        bare 302 reads as a non-200 and the title is treated as unanswerable
        — this is the defect as it shipped, pinned so it cannot regress."""
        def handler(request):
            return httpx.Response(302, headers={"location": "/anime/3198578?client_id=cid"})

        client = httpx.AsyncClient(follow_redirects=False,
                                   transport=httpx.MockTransport(handler))
        with patch("app.providers.simkl.transport.catalog_client", return_value=client), \
             patch("app.cache.get", AsyncMock(return_value=None)), \
             patch("app.cache.set", AsyncMock()):
            fields = await titles.fetch_title(SETTINGS, 3198578, Media.SHOW)
        self.assertIsNone(fields)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
