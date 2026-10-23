"""Trakt's SearchPort implementation (app/providers/trakt/__init__.py's
_TraktSearchPort).

Guards the one thing that makes this port different from how the add-show flow
used to reach Trakt's search: it delegates to `detail.search_titles` directly
and drops nothing. That flow used to go through a helper that filtered out any
hit with no Trakt id — a byproduct of Trakt's own catalogue never omitting one,
not a rule the port's contract makes. A hit that filter would have dropped must
still come back from the port, and now genuinely can: these hits are merged
with another service's, where a title known by tmdb alone is ordinary.

No network — app.providers.trakt.detail.search_titles is patched directly, so
these tests exercise the SHAPE the port produces rather than the HTTP call
underneath it (transport.cached_get already has its own coverage).
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.base import Media, SearchHit, Source
from app.providers.trakt import detail
from app.providers.trakt import _TraktSearchPort

SETTINGS = SimpleNamespace(trakt_client_id="id", trakt_access_token="", cache_ttl_minutes=10)

# What detail.search_titles hands back once it has already reduced Trakt's raw
# /search/show response to this app's shape — see search_titles's own
# docstring for the fields it keeps.
WITH_TRAKT_ID = {
    "media": "show", "ids": {"trakt": 123, "slug": "a-show", "tmdb": 456},
    "title": "A Show", "year": 2022, "network": "HBO", "runtime": None,
    "overview": "An overview.", "poster": "https://media.trakt.tv/a-show.jpg",
}

# search_titles keeps any hit with SOME shared id, whether or not Trakt is
# among them; the old add-flow helper was where such a hit got dropped. Not
# measured live (Trakt search hits do carry a Trakt id today), but the port's
# contract must not assume that stays true.
NO_TRAKT_ID = {
    "media": "show", "ids": {"tmdb": 789}, "title": "No Trakt Id", "year": 2021,
    "network": "", "runtime": None, "overview": "", "poster": "",
}


class TraktSearchPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_search_titles(self):
        spy = AsyncMock(return_value=([WITH_TRAKT_ID], False))
        with patch.object(detail, "search_titles", spy):
            hits = (await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "a show")).hits
        spy.assert_awaited_once_with(SETTINGS, "show", "a show", page=1, cache_only=False)
        self.assertEqual(len(hits), 1)

    async def test_a_hit_with_no_trakt_id_is_not_dropped(self):
        """The add flow's old search helper filtered NO_TRAKT_ID out entirely,
        on a trailing `if entry["ids"].get("trakt") is not None`. The port must
        not repeat that filter."""
        with patch.object(detail, "search_titles", AsyncMock(return_value=([NO_TRAKT_ID], False))):
            hits = (await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "x")).hits
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source_id, "")

    async def test_a_hit_is_a_searchhit_with_trakt_as_the_source(self):
        with patch.object(detail, "search_titles", AsyncMock(return_value=([WITH_TRAKT_ID], False))):
            hits = (await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "a show")).hits
        hit = hits[0]
        self.assertIsInstance(hit, SearchHit)
        self.assertEqual(hit.source, Source.TRAKT)
        self.assertEqual(hit.source_id, "123")
        self.assertEqual(hit.media, Media.SHOW)
        self.assertEqual(hit.ids, {"trakt": 123, "slug": "a-show", "tmdb": 456})
        self.assertEqual(hit.title, "A Show")
        self.assertEqual(hit.year, 2022)
        self.assertEqual(hit.network, "HBO")
        self.assertIsNone(hit.season)

    async def test_the_poster_travels_as_trakts_own_address(self):
        with patch.object(detail, "search_titles", AsyncMock(return_value=([WITH_TRAKT_ID], False))):
            hits = (await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "a show")).hits
        self.assertEqual(hits[0].poster, "https://media.trakt.tv/a-show.jpg")

    async def test_the_search_asks_for_images_and_reads_the_poster_from_them(self):
        """One request buys the row its picture: measured live, a search asked
        for `full,images` carries the same images block the calendar's does."""
        raw = [{"show": {"ids": {"trakt": 1, "tmdb": 2}, "title": "A Show", "year": 2022,
                         "images": {"poster": ["media.trakt.tv/images/a.jpg.webp"]}}},
               {"show": {"ids": {"trakt": 3}, "title": "No Art", "year": 2022}}]
        spy = AsyncMock(return_value=raw)
        with patch.object(detail.transport, "cached_get", spy):
            out, more = await detail.search_titles(SETTINGS, "show", "a show")
        self.assertEqual(spy.await_args.args[3], {"query": "a show", "extended": "full,images",
                                                  "page": "1", "limit": "20"})
        self.assertFalse(more)
        self.assertEqual([e["poster"] for e in out],
                         ["https://media.trakt.tv/images/a.jpg.webp", ""])

    async def test_the_whole_id_map_travels_not_just_trakt(self):
        with patch.object(detail, "search_titles", AsyncMock(return_value=([WITH_TRAKT_ID], False))):
            hits = (await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "a show")).hits
        self.assertIn("tmdb", hits[0].ids)

    async def test_a_page_is_asked_by_number_and_a_full_page_means_more(self):
        """Measured 2026-10-23: `page` and `limit` in the query are honoured over
        the X-Pagination-* request headers — "heat" answered 100 under the headers
        alone and 20 a page with `limit=20`."""
        full = [{"movie": {"ids": {"trakt": n}, "title": f"T{n}", "year": 2000}}
                for n in range(20)]
        spy = AsyncMock(side_effect=[full, full[:3]])
        with patch.object(detail.transport, "cached_get", spy):
            first = await _TraktSearchPort().search_titles(SETTINGS, Media.MOVIE, "heat", page=2)
            last = await _TraktSearchPort().search_titles(SETTINGS, Media.MOVIE, "heat", page=3)
        self.assertEqual(spy.await_args_list[0].args[3]["page"], "2")
        self.assertEqual(spy.await_args_list[0].args[3]["limit"], "20")
        self.assertEqual((len(first.hits), first.more), (20, True))
        self.assertEqual((len(last.hits), last.more), (3, False))

    async def test_an_empty_result_stays_empty(self):
        with patch.object(detail, "search_titles", AsyncMock(return_value=([], False))):
            hits = (await _TraktSearchPort().search_titles(SETTINGS, Media.MOVIE, "nothing")).hits
        self.assertEqual(hits, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
