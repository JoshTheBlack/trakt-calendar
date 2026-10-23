"""Simkl's SearchPort implementation (app/providers/simkl/search.py).

Guards the measured, non-obvious facts the module docstring documents: GET
/search/tv answers nothing for anime, so anime is its own catalogue ("simkl-anime")
searched apart, each catalogue one page per ask; /search/anime mixes films in with
TV, so each search keeps only the medium asked for; and a search hit's `ids` block
is thin (at most simkl_id, slug, tmdb) and has to be remapped onto the app's
ID_KEYS spelling the same way every other Simkl payload is.

No network — the transport's page read and its per-title read are patched with a
canned response per path.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import SEARCH_PAGE_SIZE, Media, SearchHit, Source
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


def _transport(pages: dict, records: dict | None = None):
    """Stand-ins for the transport: `pages` answers a search page by path (a
    list, (list, more), or an exception), `records` a per-title read by path."""
    records = records or {}

    async def _page(_client, _settings, path, params, page, **_kwargs):
        answer = pages[path]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, tuple) else (answer, False)

    async def _get(_client, _settings, path, _params=None, **_kwargs):
        answer = records.get(path)
        if isinstance(answer, Exception):
            raise answer
        return answer

    page_spy, get_spy = AsyncMock(side_effect=_page), AsyncMock(side_effect=_get)
    return page_spy, get_spy, patch.multiple(transport, cached_page_get=page_spy,
                                             cached_get=get_spy)


class SearchTitlesTests(unittest.IsolatedAsyncioTestCase):
    async def _hits(self, pages, media=Media.SHOW, query="x", **kwargs):
        page_spy, _get, patched = _transport(pages)
        with patched:
            answer = await search.search_titles(SETTINGS, media, query, **kwargs)
        return answer, page_spy

    async def test_an_empty_query_makes_no_call(self):
        answer, spy = await self._hits({}, query="   ")
        self.assertEqual(answer.hits, [])
        spy.assert_not_called()

    async def test_each_catalogue_asks_its_own_endpoint_once(self):
        cases = [(Media.SHOW, search.CATALOGUE, "search/tv"),
                 (Media.MOVIE, search.CATALOGUE, "search/movie"),
                 (Media.SHOW, search.ANIME_CATALOGUE, "search/anime"),
                 (Media.MOVIE, search.ANIME_CATALOGUE, "search/anime")]
        for media, catalogue, path in cases:
            with self.subTest(media=media, catalogue=catalogue):
                _answer, spy = await self._hits({path: []}, media=media, catalogue=catalogue)
                self.assertEqual([call.args[2] for call in spy.await_args_list], [path])

    async def test_one_page_is_asked_by_number_with_the_shared_page_size(self):
        _answer, spy = await self._hits({"search/tv": []}, query="  severance  ", page=3)
        call, = spy.await_args_list
        self.assertEqual(call.args[3], {"q": "severance", "limit": str(SEARCH_PAGE_SIZE)})
        self.assertEqual(call.args[4], 3)
        self.assertIs(call.kwargs["pool"], transport.CATALOG_POOL)

    async def test_more_is_simkls_own_word(self):
        answer, _spy = await self._hits({"search/tv": ([SEVERANCE], True)})
        self.assertTrue(answer.more)

    async def test_an_unknown_catalogue_is_refused(self):
        with pytest.raises(ValueError):
            await self._hits({}, catalogue="simkl-music")

    async def test_a_hit_is_a_searchhit_with_simkl_as_the_source(self):
        answer, _spy = await self._hits({"search/tv": [SEVERANCE]}, query="severance")
        hit, = answer.hits
        self.assertIsInstance(hit, SearchHit)
        self.assertEqual(hit.source, Source.SIMKL)
        self.assertEqual(hit.source_id, "1203662")
        self.assertEqual(hit.media, Media.SHOW)
        self.assertEqual((hit.title, hit.year), ("Severance", 2022))

    async def test_the_poster_path_becomes_simkls_own_address(self):
        """Every entry measured live carried a poster path; the address is built
        by the calendar's own function, so a row and a card share one picture."""
        answer, _spy = await self._hits(
            {"search/tv": [dict(SEVERANCE, poster="12/12301659c2620e9486")]})
        self.assertEqual(answer.hits[0].poster,
                         "https://simkl.in/posters/12/12301659c2620e9486_m.jpg")

    async def test_simkl_id_is_remapped_onto_the_apps_own_id_spelling(self):
        """Simkl writes its own id as `simkl_id` in a search hit's ids block;
        `collect_ids` only knows `simkl`. Un-mapped, a Simkl-only hit would
        carry no id the tracker's identity waterfall recognises as Simkl's."""
        answer, _spy = await self._hits({"search/tv": [SEVERANCE]})
        self.assertEqual(answer.hits[0].ids, {"simkl": 1203662, "slug": "severance",
                                              "simkl_slug": "severance", "tmdb": "95396"})

    async def test_the_anime_catalogue_keeps_only_the_medium_asked_for(self):
        """GET /search/anime mixes films in with TV — measured LIVE against the
        search endpoint itself 2026-09-27: "attack on titan" answers "Shingeki no
        Kyojin: The Last Attack" (a 2024 recap film, `type: "movie"`) beside the
        TV seasons. A show search keeps the shows, a film search the films; a
        `special` stays a show (its per-title record can carry a real season
        mapping, as Attack on Titan's 2023 special's does)."""
        film = {**SEVERANCE, "title": "The Last Attack", "type": "movie",
                "ids": {"simkl_id": 2544548}}
        special = {**SEVERANCE, "title": "A Special", "type": "special",
                   "ids": {"simkl_id": 7}}
        pages = {"search/anime": [film, special, BARE_ANIME]}
        shows, _ = await self._hits(pages, catalogue=search.ANIME_CATALOGUE)
        films, _ = await self._hits(pages, media=Media.MOVIE, catalogue=search.ANIME_CATALOGUE)
        self.assertEqual([h.title for h in shows.hits],
                         ["A Special", "Shingeki no Kyojin Season 3"])
        self.assertEqual([h.title for h in films.hits], ["The Last Attack"])

    async def test_a_bare_hit_carries_no_shared_id_but_is_still_returned(self):
        """Measured live: a season-title's hit often carries only simkl_id and
        slug. It is under-described, not dropped — the port's job is to hand
        it over, not to decide whether it can be filed."""
        answer, _ = await self._hits({"search/anime": [BARE_ANIME]},
                                     catalogue=search.ANIME_CATALOGUE)
        self.assertEqual(answer.hits[0].ids, {"simkl": 694485,
                                              "slug": "shingeki-no-kyojin-season-3",
                                              "simkl_slug": "shingeki-no-kyojin-season-3"})

    async def test_network_runtime_overview_and_season_are_left_empty(self):
        """Measured live: a Simkl search hit carries none of the four — the
        merge's fill-empty-from-the-other rule is what covers this."""
        answer, _ = await self._hits({"search/tv": [SEVERANCE]})
        hit = answer.hits[0]
        self.assertEqual((hit.network, hit.runtime, hit.overview, hit.season), ("", None, "", None))

    async def test_a_failure_raises_rather_than_answering_empty(self):
        """A failure must not look like Simkl genuinely finding nothing, the
        distinction every port in this app draws between "no matches" and
        "could not be asked"."""
        with pytest.raises(transport.SimklError):
            await self._hits({"search/tv": transport.SimklError("down", 503)})


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

    async def _search(self, hits, records=None, media=Media.SHOW):
        _page, get_spy, patched = _transport({"search/anime": hits, "search/movie": hits},
                                             self.RECORDS if records is None else records)
        with patched:
            answer = await search.search_titles(
                SETTINGS, media, "beastars",
                catalogue=search.ANIME_CATALOGUE if media is Media.SHOW else search.CATALOGUE)
        return answer.hits, get_spy

    async def test_hits_sharing_one_identity_each_get_their_own_season(self):
        hits, _spy = await self._search(self.SERIES)
        self.assertEqual([(h.source_id, h.season) for h in hits],
                         [("1034467", 1), ("1231401", 2), ("2831384", 3)])

    async def test_a_hit_nothing_else_collides_with_costs_no_lookup(self):
        """The cost bound, asserted rather than described: an ordinary search
        makes no extra call at all."""
        hits, spy = await self._search([SEVERANCE, BARE_ANIME])
        self.assertEqual([h.season for h in hits], [None, None])
        spy.assert_not_called()

    async def test_only_the_colliding_hits_are_looked_up(self):
        hits, spy = await self._search([SEVERANCE, *self.SERIES])
        self.assertEqual(sorted(call.args[2] for call in spy.await_args_list),
                         ["tv/1034467", "tv/1231401", "tv/2831384"])
        self.assertIsNone(next(h.season for h in hits if h.source_id == "1203662"))

    async def test_the_lookup_is_held_for_a_day_not_the_default_ttl(self):
        """A repeated search must not pay these again: a title's season mapping
        is about as static as catalogue data gets."""
        _hits, spy = await self._search(self.SERIES)
        for call in spy.await_args_list:
            with self.subTest(path=call.args[2]):
                self.assertEqual(call.kwargs["ttl_seconds"], _naming.CACHE_TTL_SECONDS)

    async def test_a_record_that_names_no_season_leaves_its_hit_unnamed(self):
        """Falls back to what the merge already does with hits it cannot tell
        apart — keep them as separate rows — rather than guessing."""
        records = {**self.RECORDS,
                   "tv/1231401": {"ids": {"simkl": 1231401}, "mapped_tvdb_seasons": [2, 3]}}
        hits, _spy = await self._search(self.SERIES, records)
        self.assertEqual([h.season for h in hits], [1, None, 3])

    async def test_a_failed_lookup_costs_a_season_label_and_not_the_search(self):
        records = {**self.RECORDS, "tv/1231401": transport.SimklError("down", 503)}
        hits, _spy = await self._search(self.SERIES, records)
        self.assertEqual([h.source_id for h in hits], ["1034467", "1231401", "2831384"])
        self.assertEqual([h.season for h in hits], [1, None, 3])

    async def test_a_movie_query_never_asks_which_season_a_film_is(self):
        """Simkl's film catalogue does not follow the season-title convention,
        and this lookup reads /tv/{id} — a wrong question about a film id."""
        films = [{"title": "Beastars", "year": 2019, "ids": {"simkl_id": 1, "tmdb": "500"}},
                 {"title": "Beastars", "year": 2020, "ids": {"simkl_id": 2, "tmdb": "500"}}]
        hits, spy = await self._search(films, media=Media.MOVIE)
        spy.assert_not_called()
        self.assertEqual([h.season for h in hits], [None, None])


class OnePageReadTests(unittest.IsolatedAsyncioTestCase):
    """transport.cached_page_get: one page, its own cache entry, and Simkl's own
    page count kept beside it so a re-read answers "is there more?" too."""

    async def test_a_page_is_stored_with_whether_more_follows_and_re_read_without_a_call(self):
        from tests.support import migrated_db
        from app import db
        migrated_db("simkl-page")
        self.addCleanup(db.close_thread_connection)
        fetch = AsyncMock(return_value=([SEVERANCE], 3))
        with patch.object(transport, "_fetch_json", fetch):
            live = await transport.cached_page_get(
                None, SETTINGS, "search/tv", {"q": "x", "limit": "20"}, 2,
                pool=transport.CATALOG_POOL)
            again = await transport.cached_page_get(
                None, SETTINGS, "search/tv", {"q": "x", "limit": "20"}, 2,
                pool=transport.CATALOG_POOL, cache_only=True)
            other = await transport.cached_page_get(
                None, SETTINGS, "search/tv", {"q": "x", "limit": "20"}, 3,
                pool=transport.CATALOG_POOL, cache_only=True)
        self.assertEqual(live, ([SEVERANCE], True))
        self.assertEqual(again, ([SEVERANCE], True))
        self.assertEqual(other, ([], False))
        fetch.assert_awaited_once()
        self.assertTrue(fetch.await_args.kwargs["private"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
