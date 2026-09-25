"""Trakt's half of SearchPort.find_by_shared_id and DetailPort.fetch_title_facts.

The lookup files a service id on a tracker row for good — nothing overwrites an
id once stored, and a wrong Trakt id shadows a right Simkl one — so what is
pinned here is that it answers ONLY an exact match on the id it was asked
about, and asks Trakt nothing when there is nothing to ask by.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.providers.base import TitleFacts
from app.providers.trakt import detail

SETTINGS = Settings(trakt_client_id="cid")


def _hit(kind="show", **ids):
    return {"type": kind, kind: {"title": "Found", "ids": {"slug": "found", **ids}}}


def _answers(by_path: dict):
    """A cached_get stand-in answering per path, recording what was asked."""
    asked = []

    async def cached_get(client, settings, path, params, **kwargs):
        asked.append((path, params, kwargs))
        return by_path.get(path, [])
    return asked, patch("app.providers.trakt.transport.cached_get", cached_get)


class FindBySharedIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_exact_match_answers_traktss_own_ids(self):
        asked, stand_in = _answers({"search/tmdb/900": [_hit(trakt=111, tmdb=900)]})
        with stand_in:
            got = await detail.find_by_shared_id(SETTINGS, "show", {"tmdb": 900})
        self.assertEqual((got["trakt"], got["trakt_slug"]), (111, "found"))
        self.assertEqual(asked[0][1], {"type": "show"})
        # The response cache's age is the re-ask clock: a day.
        self.assertEqual(asked[0][2]["ttl_seconds"], 24 * 60 * 60)

    async def test_a_hit_stating_a_different_id_is_not_taken(self):
        asked, stand_in = _answers({"search/tmdb/900": [_hit(trakt=111, tmdb=901)]})
        with stand_in:
            self.assertEqual(await detail.find_by_shared_id(SETTINGS, "show", {"tmdb": 900}), {})

    async def test_a_hit_of_the_other_media_is_not_taken(self):
        asked, stand_in = _answers({"search/tmdb/900": [_hit("movie", trakt=111, tmdb=900)]})
        with stand_in:
            self.assertEqual(await detail.find_by_shared_id(SETTINGS, "show", {"tmdb": 900}), {})

    async def test_every_shared_id_is_tried_in_waterfall_order(self):
        asked, stand_in = _answers({"search/imdb/tt9": [_hit(trakt=5, imdb="tt9")]})
        with stand_in:
            got = await detail.find_by_shared_id(
                SETTINGS, "show", {"tmdb": 900, "tvdb": 7, "imdb": "tt9", "mal": 3})
        self.assertEqual(got["trakt"], 5)
        # mal is not a lookup Trakt offers, so it is never asked by it.
        self.assertEqual([path for path, _p, _k in asked],
                         ["search/tmdb/900", "search/tvdb/7", "search/imdb/tt9"])

    async def test_nothing_to_ask_by_asks_nothing(self):
        asked, stand_in = _answers({})
        with stand_in:
            self.assertEqual(await detail.find_by_shared_id(SETTINGS, "show", {"trakt": 1}), {})
        self.assertEqual(asked, [])


class TitleFactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_show_is_named_by_its_summary(self):
        with patch.object(detail, "fetch_show_summary",
                          AsyncMock(return_value={"title": "Severance", "network": "Apple TV"})):
            got = await detail.fetch_title_facts(SETTINGS, 1, "show")
        self.assertEqual(got, TitleFacts("Severance", "Apple TV"))

    async def test_a_title_trakt_answers_nothing_for_is_empty(self):
        with patch.object(detail, "fetch_show_summary", AsyncMock(return_value=None)):
            self.assertEqual(await detail.fetch_title_facts(SETTINGS, 1, "show"),
                             TitleFacts("", ""))
