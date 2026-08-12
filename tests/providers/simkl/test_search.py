"""Simkl's SearchPort implementation (app/providers/simkl/search.py).

Guards the two measured, non-obvious facts the module docstring documents:
GET /search/tv answers nothing for anime, so a show query is two requests
under one port call and the split must not leak into the returned list; and a
search hit's `ids` block is thin (at most simkl_id, slug, tmdb) and has to be
remapped onto the app's ID_KEYS spelling the same way every other Simkl
payload is.

No network — transport.cached_get is patched with a canned response per path.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import Media, SearchHit, Source
from app.providers.simkl import search, transport

SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="", cache_ttl_minutes=10)

# One real hit, trimmed to the fields measured live against GET /search/tv.
SEVERANCE = {
    "title": "Severance",
    "year": 2022,
    "endpoint_type": "tv",
    "ep_count": 19,
    "status": "airing",
    "ids": {"simkl_id": 1203662, "slug": "severance", "tmdb": "95396"},
}

# A bare anime season-title: no tmdb, only simkl's own id and slug — measured
# live for Attack on Titan's later seasons.
BARE_ANIME = {
    "title": "Shingeki no Kyojin Season 3",
    "year": 2018,
    "ids": {"simkl_id": 694485, "slug": "shingeki-no-kyojin-season-3"},
}


def _cached_get(by_path: dict):
    """A transport.cached_get stand-in keyed by the path it was asked for, so
    a show query's two endpoints can be told apart."""
    async def _get(_client, _settings, path, _params=None, **_kwargs):
        answer = by_path[path]
        if isinstance(answer, Exception):
            raise answer
        return answer
    spy = AsyncMock(side_effect=_get)
    return spy


class SearchTitlesTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_query_makes_no_call(self):
        spy = _cached_get({})
        with patch.object(transport, "cached_get", spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "   ")
        self.assertEqual(hits, [])
        spy.assert_not_called()

    async def test_a_show_query_asks_both_tv_and_anime(self):
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": []})
        with patch.object(transport, "cached_get", spy):
            await search.search_titles(SETTINGS, Media.SHOW, "severance")
        asked_paths = {call.args[2] for call in spy.await_args_list}
        self.assertEqual(asked_paths, {"search/tv", "search/anime"})

    async def test_a_movie_query_asks_only_the_movie_path(self):
        spy = _cached_get({"search/movie": []})
        with patch.object(transport, "cached_get", spy):
            await search.search_titles(SETTINGS, Media.MOVIE, "dune part two")
        asked_paths = {call.args[2] for call in spy.await_args_list}
        self.assertEqual(asked_paths, {"search/movie"})

    async def test_hits_from_both_endpoints_are_merged_into_one_list(self):
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": [BARE_ANIME]})
        with patch.object(transport, "cached_get", spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "x")
        self.assertEqual({h.title for h in hits}, {"Severance", "Shingeki no Kyojin Season 3"})

    async def test_a_hit_is_a_searchhit_with_simkl_as_the_source(self):
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": []})
        with patch.object(transport, "cached_get", spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "severance")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertIsInstance(hit, SearchHit)
        self.assertEqual(hit.source, Source.SIMKL)
        self.assertEqual(hit.source_id, "1203662")
        self.assertEqual(hit.media, Media.SHOW)
        self.assertEqual(hit.title, "Severance")
        self.assertEqual(hit.year, 2022)

    async def test_simkl_id_is_remapped_onto_the_apps_own_id_spelling(self):
        """Simkl writes its own id as `simkl_id` in a search hit's ids block;
        `collect_ids` only knows `simkl`. Un-mapped, a Simkl-only hit would
        carry no id the tracker's identity waterfall recognises as Simkl's."""
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": []})
        with patch.object(transport, "cached_get", spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "severance")
        self.assertEqual(hits[0].ids, {"simkl": 1203662, "slug": "severance", "tmdb": "95396"})

    async def test_a_bare_hit_carries_no_shared_id_but_is_still_returned(self):
        """Measured live: a season-title's hit often carries only simkl_id and
        slug. It is under-described, not dropped — the port's job is to hand
        it over, not to decide whether it can be filed."""
        spy = _cached_get({"search/tv": [], "search/anime": [BARE_ANIME]})
        with patch.object(transport, "cached_get", spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "attack on titan")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].ids, {"simkl": 694485, "slug": "shingeki-no-kyojin-season-3"})

    async def test_network_runtime_overview_and_season_are_left_empty(self):
        """Measured live: a Simkl search hit carries none of the four — the
        merge's fill-empty-from-the-other rule is what covers this."""
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": []})
        with patch.object(transport, "cached_get", spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "severance")
        hit = hits[0]
        self.assertEqual(hit.network, "")
        self.assertIsNone(hit.runtime)
        self.assertEqual(hit.overview, "")
        self.assertIsNone(hit.season)

    async def test_one_endpoint_failing_does_not_lose_the_others_hits(self):
        """A show query is two requests under one port call; /search/anime
        being down must not cost the /search/tv hits that DID come back."""
        spy = _cached_get({
            "search/tv": [SEVERANCE],
            "search/anime": transport.SimklError("down", 503),
        })
        with patch.object(transport, "cached_get", spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "x")
        self.assertEqual([h.title for h in hits], ["Severance"])

    async def test_every_endpoint_failing_raises_rather_than_answering_empty(self):
        """The one that matters: a total failure must not look like Simkl
        genuinely finding nothing, the same distinction every other port in
        this app draws between "no matches" and "could not be asked"."""
        spy = _cached_get({
            "search/tv": transport.SimklError("down", 503),
            "search/anime": transport.SimklError("down", 503),
        })
        with patch.object(transport, "cached_get", spy):
            with pytest.raises(transport.SimklError):
                await search.search_titles(SETTINGS, Media.SHOW, "x")

    async def test_a_movie_querys_one_endpoint_failing_raises(self):
        """No sibling endpoint exists for a movie query, so its one failure
        IS the whole search failing."""
        spy = _cached_get({"search/movie": transport.SimklError("down", 503)})
        with patch.object(transport, "cached_get", spy):
            with pytest.raises(transport.SimklError):
                await search.search_titles(SETTINGS, Media.MOVIE, "x")

    async def test_the_query_is_stripped_and_sent_as_q(self):
        spy = _cached_get({"search/tv": [], "search/anime": []})
        with patch.object(transport, "cached_get", spy):
            await search.search_titles(SETTINGS, Media.SHOW, "  severance  ")
        for call in spy.await_args_list:
            self.assertEqual(call.args[3], {"q": "severance"})

    async def test_every_call_goes_through_the_catalog_pool(self):
        spy = _cached_get({"search/tv": [], "search/anime": []})
        with patch.object(transport, "cached_get", spy):
            await search.search_titles(SETTINGS, Media.SHOW, "x")
        for call in spy.await_args_list:
            self.assertIs(call.kwargs["pool"], transport.CATALOG_POOL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
