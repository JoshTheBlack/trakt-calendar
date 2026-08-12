"""Trakt's SearchPort implementation (app/providers/trakt/__init__.py's
_TraktSearchPort).

Guards the one thing that makes this port different from the add-show flow's
existing `detail.search_shows`: the port delegates to `detail.search_titles`
directly and must NOT carry forward `search_shows`'s trailing filter that
drops any hit with no Trakt id. That filter is a byproduct of Trakt's own
catalogue never omitting one (measured — search_shows still keeps it, and
still would filter a bare hit out), not a rule the port's contract makes; a
hit search_shows would have dropped must still come back from the port.

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
    "overview": "An overview.",
}

# search_titles keeps any hit with SOME shared id, whether or not Trakt is
# among them — search_shows is the one that then drops it. Not measured live
# (Phase 0 found Trakt search hits always carry a Trakt id today), but the
# port's contract must not assume that stays true.
NO_TRAKT_ID = {
    "media": "show", "ids": {"tmdb": 789}, "title": "No Trakt Id", "year": 2021,
    "network": "", "runtime": None, "overview": "",
}


class TraktSearchPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_search_titles_not_search_shows(self):
        spy = AsyncMock(return_value=[WITH_TRAKT_ID])
        with patch.object(detail, "search_titles", spy):
            hits = await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "a show")
        spy.assert_awaited_once_with(SETTINGS, "show", "a show")
        self.assertEqual(len(hits), 1)

    async def test_a_hit_with_no_trakt_id_is_not_dropped(self):
        """search_shows would have filtered NO_TRAKT_ID out entirely — its
        last line is `if entry["ids"].get("trakt") is not None`. The port
        must not repeat that filter."""
        with patch.object(detail, "search_titles", AsyncMock(return_value=[NO_TRAKT_ID])):
            hits = await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "x")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source_id, "")

    async def test_a_hit_is_a_searchhit_with_trakt_as_the_source(self):
        with patch.object(detail, "search_titles", AsyncMock(return_value=[WITH_TRAKT_ID])):
            hits = await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "a show")
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

    async def test_the_whole_id_map_travels_not_just_trakt(self):
        with patch.object(detail, "search_titles", AsyncMock(return_value=[WITH_TRAKT_ID])):
            hits = await _TraktSearchPort().search_titles(SETTINGS, Media.SHOW, "a show")
        self.assertIn("tmdb", hits[0].ids)

    async def test_an_empty_result_stays_empty(self):
        with patch.object(detail, "search_titles", AsyncMock(return_value=[])):
            hits = await _TraktSearchPort().search_titles(SETTINGS, Media.MOVIE, "nothing")
        self.assertEqual(hits, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
