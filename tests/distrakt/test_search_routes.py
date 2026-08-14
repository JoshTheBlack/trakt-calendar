"""GET /api/distrakt/search and GET /api/distrakt/seasons through the catalogue
registry (app/distrakt/routes.py's api_distrakt_search and api_distrakt_seasons).

No network: each source's own search/season module function is patched at its
own module object, so these exercise the ROUTE's gating, merge-wiring and
response shape rather than either provider's HTTP call, which has its own
coverage in tests/providers/.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.config import Settings, save_settings
from app.providers.base import SearchHit, SeasonsAnswer, Source
from app.providers.simkl import detail as simkl_detail
from app.providers.simkl import search as simkl_search
from app.providers.trakt import TraktError
from app.providers.trakt import detail as trakt_detail
from tests.support import AppTestCase, ORIGIN

TRAKT_HIT = {
    "media": "show", "ids": {"trakt": 1, "tmdb": 100, "slug": "a-show"},
    "title": "A Show", "year": 2022, "network": "HBO", "runtime": None,
    "overview": "An overview.",
}

BOTH_CONFIGURED = dict(public_base_url=ORIGIN, trakt_client_id="cid",
                       simkl_client_id="scid", simkl_client_secret="ssecret")


class SearchRouteTests(AppTestCase):
    def make_settings(self):
        return Settings(**BOTH_CONFIGURED)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("searcher", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)

    def test_a_trakt_only_instance_can_still_search(self):
        """§1's whole finding: an instance missing one catalogue must not lose
        the ability to search at all."""
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[TRAKT_HIT])):
            body = self.client.get("/api/distrakt/search?q=a").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["results"][0]["title"], "A Show")

    def test_a_simkl_only_instance_can_still_search(self):
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid",
                               simkl_client_secret="ssecret"))
        with patch.object(simkl_search, "search_titles", AsyncMock(return_value=[])):
            body = self.client.get("/api/distrakt/search?q=a").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["results"], [])

    def test_no_catalogue_configured_is_refused(self):
        save_settings(Settings(public_base_url=ORIGIN))
        body = self.client.get("/api/distrakt/search?q=a").json()
        self.assertEqual(body, {"ok": False, "error": "Not configured"})

    def test_merged_results_carry_both_sources_marks(self):
        """A title both sources found comes back once, with both source marks
        — the whole reason a merge is worth doing beyond deduplication."""
        simkl_hit = SearchHit(source=Source.SIMKL, source_id="9", media="show",
                              ids={"simkl": 9, "tmdb": 100}, title="A Show", year=2022,
                              season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[TRAKT_HIT])), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[simkl_hit])):
            body = self.client.get("/api/distrakt/search?q=a").json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(set(body["results"][0]["source_ids"]), {"trakt", "simkl"})

    def test_one_source_failing_is_named_and_does_not_fail_the_search(self):
        async def _boom(*args, **kwargs):
            raise TraktError("down")

        with patch.object(trakt_detail, "search_titles", _boom), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[])):
            body = self.client.get("/api/distrakt/search?q=a").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["results"], [])
        self.assertEqual(body["failed"], ["trakt"])


class SeasonsRouteTests(AppTestCase):
    def make_settings(self):
        return Settings(**BOTH_CONFIGURED)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("picker", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)

    def test_missing_source_is_refused(self):
        body = self.client.get("/api/distrakt/seasons?id=1").json()
        self.assertEqual(body, {"ok": False, "error": "Missing or invalid source"})

    def test_an_unregistered_source_is_refused(self):
        body = self.client.get("/api/distrakt/seasons?source=letterboxd&id=1").json()
        self.assertEqual(body, {"ok": False, "error": "Missing or invalid source"})

    def test_missing_id_is_refused(self):
        body = self.client.get("/api/distrakt/seasons?source=trakt").json()
        self.assertEqual(body, {"ok": False, "error": "Missing id"})

    def test_the_sources_own_catalogue_gate_is_asked(self):
        save_settings(Settings(public_base_url=ORIGIN))
        body = self.client.get("/api/distrakt/seasons?source=trakt&id=1").json()
        self.assertEqual(body, {"ok": False, "error": "Not configured"})

    def test_a_trakt_hit_returns_its_season_list_and_no_named_season(self):
        seasons = [{"season": 1, "episode_count": 10}]
        with patch.object(trakt_detail, "fetch_show_seasons", AsyncMock(return_value=seasons)):
            body = self.client.get(
                "/api/distrakt/seasons?source=trakt&id=1&tmdb=100").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["seasons"], seasons)
        self.assertIsNone(body["season"])
        self.assertEqual(body["ids"], {"tmdb": 100})
        self.assertIsNone(body["unkeyable"])

    def test_a_bare_simkl_hit_is_resolved_by_the_same_lookup(self):
        """The item-4 case: a search hit search left bare of a shared id comes
        back keyable once the per-title lookup has run, with no `ids` query
        params needed at all — the lookup's own answer is enough on its own."""
        answer = SeasonsAnswer(seasons=[], named_season=3,
                               ids={"tmdb": 1429, "tvdb": 99, "imdb": "tt1", "mal": 51019})
        with patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            body = self.client.get(
                "/api/distrakt/seasons?source=simkl&id=694485").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["season"], 3)
        self.assertEqual(body["ids"], {"tmdb": 1429, "tvdb": 99, "imdb": "tt1", "mal": 51019})
        self.assertIsNone(body["unkeyable"])

    def test_a_hit_still_unkeyable_after_the_lookup_says_so(self):
        answer = SeasonsAnswer(seasons=[], named_season=None, ids={})
        with patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            body = self.client.get(
                "/api/distrakt/seasons?source=simkl&id=1&title=Obscure").json()
        self.assertTrue(body["ok"])
        self.assertIn("Obscure", body["unkeyable"])
        self.assertIsNone(body["season"])

    def test_given_ids_are_kept_when_the_lookup_adds_nothing_new(self):
        """A Trakt hit is never bare — its per-title lookup surfaces no new
        ids (SeasonsAnswer.ids is always {}) — so the ids the client already
        had must survive the union, or every Trakt hit would look unkeyable."""
        with patch.object(trakt_detail, "fetch_show_seasons", AsyncMock(return_value=[])):
            body = self.client.get(
                "/api/distrakt/seasons?source=trakt&id=1&tmdb=100").json()
        self.assertEqual(body["ids"], {"tmdb": 100})
        self.assertIsNone(body["unkeyable"])

    def test_an_ambiguous_season_falls_back_to_the_picker(self):
        answer = SeasonsAnswer(seasons=[{"season": 1, "episode_count": 12}],
                               named_season=None, ids={"tmdb": 1})
        with patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            body = self.client.get("/api/distrakt/seasons?source=simkl&id=1").json()
        self.assertIsNone(body["season"])
        self.assertEqual(body["seasons"], [{"season": 1, "episode_count": 12}])

    def test_a_genuine_failure_is_a_502_not_an_empty_answer(self):
        async def _boom(*args, **kwargs):
            raise TraktError("Could not reach Trakt", status=502)

        with patch.object(trakt_detail, "fetch_show_seasons", _boom):
            resp = self.client.get("/api/distrakt/seasons?source=trakt&id=1")
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.json()["ok"])


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
