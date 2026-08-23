"""Simkl's SearchPort implementation (app/providers/simkl/search.py).

Guards the two measured, non-obvious facts the module docstring documents:
GET /search/tv answers nothing for anime, so a show query is two requests
under one port call and the split must not leak into the returned list; and a
search hit's `ids` block is thin (at most simkl_id, slug, tmdb) and has to be
remapped onto the app's ID_KEYS spelling the same way every other Simkl
payload is.

No network — the transport's two GET entry points are patched with a canned
response per path.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import Media, SearchHit, Source
from app.providers.simkl import _naming, search, transport

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


def _patched(spy):
    """BOTH transport entry points, one stub. A search assembles its pages
    through `cached_paged_get`; the season disambiguation behind it reads
    per-title records through `cached_get`. The stub keys on the path, so one
    serves either."""
    return patch.multiple(transport, cached_get=spy, cached_paged_get=spy)


class SearchTitlesTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_query_makes_no_call(self):
        spy = _cached_get({})
        with _patched(spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "   ")
        self.assertEqual(hits, [])
        spy.assert_not_called()

    async def test_a_show_query_asks_both_tv_and_anime(self):
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": []})
        with _patched(spy):
            await search.search_titles(SETTINGS, Media.SHOW, "severance")
        asked_paths = {call.args[2] for call in spy.await_args_list}
        self.assertEqual(asked_paths, {"search/tv", "search/anime"})

    async def test_a_movie_query_asks_only_the_movie_path(self):
        spy = _cached_get({"search/movie": []})
        with _patched(spy):
            await search.search_titles(SETTINGS, Media.MOVIE, "dune part two")
        asked_paths = {call.args[2] for call in spy.await_args_list}
        self.assertEqual(asked_paths, {"search/movie"})

    async def test_hits_from_both_endpoints_are_merged_into_one_list(self):
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": [BARE_ANIME]})
        with _patched(spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "x")
        self.assertEqual({h.title for h in hits}, {"Severance", "Shingeki no Kyojin Season 3"})

    async def test_a_hit_is_a_searchhit_with_simkl_as_the_source(self):
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": []})
        with _patched(spy):
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
        with _patched(spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "severance")
        self.assertEqual(hits[0].ids, {"simkl": 1203662, "slug": "severance", "simkl_slug": "severance",
                          "tmdb": "95396"})

    async def test_a_bare_hit_carries_no_shared_id_but_is_still_returned(self):
        """Measured live: a season-title's hit often carries only simkl_id and
        slug. It is under-described, not dropped — the port's job is to hand
        it over, not to decide whether it can be filed."""
        spy = _cached_get({"search/tv": [], "search/anime": [BARE_ANIME]})
        with _patched(spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "attack on titan")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].ids, {"simkl": 694485, "slug": "shingeki-no-kyojin-season-3",
                          "simkl_slug": "shingeki-no-kyojin-season-3"})

    async def test_network_runtime_overview_and_season_are_left_empty(self):
        """Measured live: a Simkl search hit carries none of the four — the
        merge's fill-empty-from-the-other rule is what covers this."""
        spy = _cached_get({"search/tv": [SEVERANCE], "search/anime": []})
        with _patched(spy):
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
        with _patched(spy):
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
        with _patched(spy):
            with pytest.raises(transport.SimklError):
                await search.search_titles(SETTINGS, Media.SHOW, "x")

    async def test_a_movie_querys_one_endpoint_failing_raises(self):
        """No sibling endpoint exists for a movie query, so its one failure
        IS the whole search failing."""
        spy = _cached_get({"search/movie": transport.SimklError("down", 503)})
        with _patched(spy):
            with pytest.raises(transport.SimklError):
                await search.search_titles(SETTINGS, Media.MOVIE, "x")

    async def test_the_query_is_stripped_and_sent_as_q(self):
        spy = _cached_get({"search/tv": [], "search/anime": []})
        with _patched(spy):
            await search.search_titles(SETTINGS, Media.SHOW, "  severance  ")
        for call in spy.await_args_list:
            # The page size rides along; `page` deliberately does not — it is the
            # transport's business and must stay out of the cache key, or every
            # page of one search would address its own entry.
            self.assertEqual(call.args[3],
                             {"q": "severance", "limit": str(search.PAGE_SIZE)})

    async def test_every_call_goes_through_the_catalog_pool(self):
        spy = _cached_get({"search/tv": [], "search/anime": []})
        with _patched(spy):
            await search.search_titles(SETTINGS, Media.SHOW, "x")
        for call in spy.await_args_list:
            self.assertIs(call.kwargs["pool"], transport.CATALOG_POOL)


class CollidingSeasonTests(unittest.IsolatedAsyncioTestCase):
    """Filling in `SearchHit.season` for the hits of one answer that share an
    identity — the anime season-title case, measured live 2026-08-18.

    Simkl answers "beastars" with four titles that ALL resolve to
    show:tmdb:90937. Downstream they are four rows with one identity and
    nothing to tell them apart, and only this service can say which season
    each is: it lives on the per-title record, one lookup deeper than search.
    """

    # Three season-titles of one series, as the search endpoint returns them:
    # the parent's tmdb id on every one, no season anywhere.
    SERIES = [
        {"title": "Beastars", "year": 2019,
         "ids": {"simkl_id": 1034467, "slug": "beastars", "tmdb": "90937"}},
        {"title": "Beastars", "year": 2021,
         "ids": {"simkl_id": 1231401, "slug": "beastars", "tmdb": "90937"}},
        {"title": "Beastars Final Season", "year": 2026,
         "ids": {"simkl_id": 2831384, "slug": "beastars-final", "tmdb": "90937"}},
    ]
    RECORDS = {
        "tv/1034467": {"ids": {"simkl": 1034467}, "mapped_tvdb_seasons": [1]},
        "tv/1231401": {"ids": {"simkl": 1231401}, "mapped_tvdb_seasons": [2]},
        "tv/2831384": {"ids": {"simkl": 2831384}, "mapped_tvdb_seasons": [3]},
    }

    def _transport(self, tv=None, anime=None, records=None):
        by_path = {"search/tv": tv if tv is not None else [],
                   "search/anime": anime if anime is not None else []}
        records = self.RECORDS if records is None else records

        async def _get(_client, _settings, path, _params=None, **_kwargs):
            if path.startswith("tv/"):
                answer = records.get(path)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return by_path[path]

        return AsyncMock(side_effect=_get)

    async def _search(self, **kwargs):
        spy = self._transport(**kwargs)
        with _patched(spy):
            hits = await search.search_titles(SETTINGS, Media.SHOW, "beastars")
        return hits, spy

    async def test_hits_sharing_one_identity_each_get_their_own_season(self):
        hits, _spy = await self._search(anime=self.SERIES)
        self.assertEqual([(h.source_id, h.season) for h in hits],
                         [("1034467", 1), ("1231401", 2), ("2831384", 3)])

    async def test_a_hit_nothing_else_collides_with_costs_no_lookup(self):
        """The cost bound, asserted rather than described: an ordinary search —
        every film, every live-action show, any anime query answering one title
        per series — makes no extra call at all."""
        hits, spy = await self._search(tv=[SEVERANCE], anime=[BARE_ANIME])
        self.assertEqual([h.season for h in hits], [None, None])
        self.assertEqual({call.args[2] for call in spy.await_args_list},
                         {"search/tv", "search/anime"})

    async def test_only_the_colliding_hits_are_looked_up(self):
        hits, spy = await self._search(tv=[SEVERANCE], anime=self.SERIES)
        looked_up = sorted(call.args[2] for call in spy.await_args_list
                           if call.args[2].startswith("tv/"))
        self.assertEqual(looked_up, ["tv/1034467", "tv/1231401", "tv/2831384"])
        # Severance shares its key with nothing, so it keeps its unnamed season.
        self.assertIsNone(next(h.season for h in hits if h.source_id == "1203662"))

    async def test_the_lookup_is_held_for_a_day_not_the_default_ttl(self):
        """A repeated search must not pay these again: a title's season mapping
        is about as static as catalogue data gets."""
        _hits, spy = await self._search(anime=self.SERIES)
        for call in spy.await_args_list:
            if call.args[2].startswith("tv/"):
                with self.subTest(path=call.args[2]):
                    self.assertEqual(call.kwargs["ttl_seconds"], _naming.CACHE_TTL_SECONDS)

    async def test_a_record_that_names_no_season_leaves_its_hit_unnamed(self):
        """Falls back to what the merge already does with hits it cannot tell
        apart — keep them as separate rows — rather than guessing."""
        records = {**self.RECORDS,
                   "tv/1231401": {"ids": {"simkl": 1231401},
                                  "mapped_tvdb_seasons": [2, 3]}}
        hits, _spy = await self._search(anime=self.SERIES, records=records)
        self.assertEqual([h.season for h in hits], [1, None, 3])

    async def test_a_failed_lookup_costs_a_season_label_and_not_the_search(self):
        records = {**self.RECORDS, "tv/1231401": transport.SimklError("down", 503)}
        hits, _spy = await self._search(anime=self.SERIES, records=records)
        self.assertEqual([h.source_id for h in hits], ["1034467", "1231401", "2831384"])
        self.assertEqual([h.season for h in hits], [1, None, 3])

    async def test_a_movie_query_never_asks_which_season_a_film_is(self):
        """Simkl's film catalogue does not follow the season-title convention,
        and this lookup reads /tv/{id} — a wrong question about a film id."""
        films = [{"title": "Beastars", "year": 2019,
                  "ids": {"simkl_id": 1, "tmdb": "500"}},
                 {"title": "Beastars", "year": 2020,
                  "ids": {"simkl_id": 2, "tmdb": "500"}}]
        spy = AsyncMock(side_effect=lambda *a, **k: films)
        with _patched(spy):
            hits = await search.search_titles(SETTINGS, Media.MOVIE, "beastars")
        self.assertEqual({call.args[2] for call in spy.await_args_list}, {"search/movie"})
        self.assertEqual([h.season for h in hits], [None, None])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
