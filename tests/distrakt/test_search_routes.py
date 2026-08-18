"""The add flow's catalogue search fragments and GET /api/distrakt/seasons
through the catalogue registry (app/distrakt/routes.py's `_search_fragment` and
`api_distrakt_seasons`).

The two search routes answer with RENDERED ROWS rather than JSON — see
`_search_fragment` for why — so these read the markup the browser is handed.
The row's own data attributes are what the pick reads back, so they are asserted
as data (parsed out and compared) rather than pattern-matched: markup that
looked right and did not parse would fail in the browser and nowhere here.

No network: each source's own search/season module function is patched at its
own module object, so these exercise the ROUTE's gating, merge-wiring and
rendering rather than either provider's HTTP call, which has its own coverage in
tests/providers/.
"""
from __future__ import annotations

import json
import re
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

_ROW = re.compile(r'<div class="distrakt-search-row[^"]*"(.*?)>', re.S)
_ATTR = re.compile(r"""\s([\w-]+)=(?:'([^']*)'|"([^"]*)")""")


def rows(html: str) -> list[dict]:
    """Every result row's attributes, as the browser's `dataset` would read them.

    Parsed rather than asserted against a substring because the attributes ARE
    the contract with app/static/js/tracker/add.js: the pick reads `data-ids`
    and `data-source-ids` back off the row, so a row is only correct if those
    round-trip as the maps that went in.
    """
    out = []
    for attrs in _ROW.findall(html):
        found = {name: single or double for name, single, double in _ATTR.findall(attrs)}
        for key in ("data-ids", "data-source-ids"):
            if key in found:
                found[key] = json.loads(found[key])
        # The sources arrive as ORDERED [source, id] pairs (see the template for
        # why); read back as a dict for the tests that only care who is in it,
        # with the raw list kept for the one that cares which is first.
        found["sources"] = found.get("data-source-ids") or []
        found["data-source-ids"] = dict(found["sources"])
        out.append(found)
    return out


class SearchFragmentTests(AppTestCase):
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
            html = self.client.get("/distrakt/fragments/search?q=a").text
        self.assertEqual(rows(html)[0]["data-title"], "A Show")

    def test_a_simkl_only_instance_can_still_search(self):
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid",
                               simkl_client_secret="ssecret"))
        with patch.object(simkl_search, "search_titles", AsyncMock(return_value=[])):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        self.assertEqual(rows(html), [])
        self.assertIn("No matches.", html)

    def test_no_catalogue_configured_is_its_own_state_not_a_failed_search(self):
        """An instance with neither client id has nothing to search, which is an
        operator's problem — telling the viewer their search failed would send
        them to retype a query that was never wrong."""
        save_settings(Settings(public_base_url=ORIGIN))
        html = self.client.get("/distrakt/fragments/search?q=a").text
        self.assertIn("No catalogue is configured", html)
        self.assertNotIn("No matches.", html)

    def test_merged_results_carry_both_sources_marks(self):
        """A title both sources found comes back once, with both source marks
        — the whole reason a merge is worth doing beyond deduplication."""
        simkl_hit = SearchHit(source=Source.SIMKL, source_id="9", media="show",
                              ids={"simkl": 9, "tmdb": 100}, title="A Show", year=2022,
                              season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[TRAKT_HIT])), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[simkl_hit])):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        found = rows(html)
        self.assertEqual(len(found), 1)
        self.assertEqual(set(found[0]["data-source-ids"]), {"trakt", "simkl"})
        # Both marks, from the macro that owns the filenames.
        self.assertIn("trakt-circlemark-dark.svg", html)
        self.assertIn("simkllogo.svg", html)

    def test_the_leader_is_the_first_key_of_source_ids(self):
        """The browser asks `source_ids`'s FIRST key for the season list, so the
        registry's order has to survive both the merge and the render — an
        alphabetical sort anywhere in between would silently make Simkl answer
        for a row Trakt leads."""
        simkl_hit = SearchHit(source=Source.SIMKL, source_id="9", media="show",
                              ids={"simkl": 9, "tmdb": 100}, title="A Show", year=2022,
                              season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[TRAKT_HIT])), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[simkl_hit])):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        self.assertEqual(rows(html)[0]["sources"][0][0], "trakt")

    def test_a_single_source_instance_draws_no_marks(self):
        """With one catalogue there is nothing to disambiguate, so the row looks
        exactly as it did before this instance had a second service."""
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[TRAKT_HIT])):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        self.assertNotIn("source-logo", html)

    def test_one_source_failing_is_named_and_does_not_fail_the_search(self):
        async def _boom(*args, **kwargs):
            raise TraktError("down")

        with patch.object(trakt_detail, "search_titles", _boom), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[])):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        self.assertIn("Couldn't reach Trakt", html)
        self.assertIn("No matches.", html)

    def test_a_bare_show_hit_is_drawn_like_any_other_row(self):
        """A show hit with no shared id is UNDER-DESCRIBED, not unfileable: the
        season click it is about to get pays for the lookup that resolves it, so
        refusing it here would refuse the anime season-titles this flow exists
        to reach."""
        bare_hit = SearchHit(source=Source.SIMKL, source_id="694485", media="show",
                             ids={"simkl": 694485}, title="Attack on Titan Season 3",
                             year=2018, season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[])), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[bare_hit])):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        self.assertNotIn("aria-disabled", html)
        self.assertEqual(rows(html)[0]["data-source-ids"], {"simkl": "694485"})


TRAKT_MOVIE_HIT = {
    "media": "movie", "ids": {"trakt": 1, "tmdb": 200, "slug": "a-movie"},
    "title": "A Movie", "year": 2022, "network": "", "runtime": 118,
    "overview": "A movie overview.",
}


class MovieSearchFragmentTests(AppTestCase):
    """The film search fragment — the same merge the show search uses,
    media-parameterized to MOVIE. Mirrors SearchFragmentTests above rather than
    re-deriving its own coverage, since the two routes share the merge and
    differ only in what they hand it and what they do with a bare hit."""

    def make_settings(self):
        return Settings(**BOTH_CONFIGURED)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("film-searcher", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)

    def test_a_trakt_only_instance_can_still_search(self):
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[TRAKT_MOVIE_HIT])):
            html = self.client.get("/distrakt/fragments/search-movie?q=a").text
        self.assertEqual(rows(html)[0]["data-title"], "A Movie")
        # A film's third column is its runtime; it has no network to show.
        self.assertIn("118 min", html)

    def test_a_simkl_only_instance_can_still_search(self):
        """The gap §1 named as sharp and total: an instance with no Trakt
        client id could not search for a film at all before this route went
        through the registry."""
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid",
                               simkl_client_secret="ssecret"))
        with patch.object(simkl_search, "search_titles", AsyncMock(return_value=[])):
            html = self.client.get("/distrakt/fragments/search-movie?q=a").text
        self.assertEqual(rows(html), [])

    def test_no_catalogue_configured_is_its_own_state(self):
        save_settings(Settings(public_base_url=ORIGIN))
        html = self.client.get("/distrakt/fragments/search-movie?q=a").text
        self.assertIn("No catalogue is configured", html)

    def test_merged_results_carry_both_sources_marks(self):
        simkl_hit = SearchHit(source=Source.SIMKL, source_id="9", media="movie",
                              ids={"simkl": 9, "tmdb": 200}, title="A Movie", year=2022,
                              season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[TRAKT_MOVIE_HIT])), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[simkl_hit])):
            html = self.client.get("/distrakt/fragments/search-movie?q=a").text
        found = rows(html)
        self.assertEqual(len(found), 1)
        self.assertEqual(set(found[0]["data-source-ids"]), {"trakt", "simkl"})

    def test_one_source_failing_is_named_and_does_not_fail_the_search(self):
        async def _boom(*args, **kwargs):
            raise TraktError("down")

        with patch.object(trakt_detail, "search_titles", _boom), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[])):
            html = self.client.get("/distrakt/fragments/search-movie?q=a").text
        self.assertIn("Couldn't reach Trakt", html)

    def test_a_hit_with_no_shared_id_is_refused_on_the_row(self):
        """FILMS HAVE NO SEASON STEP, so unlike a bare show hit (resolved for
        free on the season click) a bare film hit has no click that already pays
        for a per-title lookup. It is shown, refused, and carries the SERVER's
        own sentence for why — the same one api_distrakt_add_movie's 400 gives,
        so the two cannot drift into two explanations of one rule."""
        bare_hit = SearchHit(source=Source.SIMKL, source_id="9", media="movie",
                             ids={"simkl": 9}, title="Untethered", year=2022,
                             season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=[])), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=[bare_hit])):
            html = self.client.get("/distrakt/fragments/search-movie?q=a").text
        self.assertIn("aria-disabled", html)
        self.assertIn("Untethered", html)


class SearchLabelTests(AppTestCase):
    """The add modals' field labels name what is ACTUALLY being searched, from
    the same registry call that answers the search — "Search Trakt" stopped
    being true the moment a second catalogue could answer."""

    def make_settings(self):
        return Settings(**BOTH_CONFIGURED)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("labeller", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)

    def _page(self) -> str:
        return self.client.get("/distrakt?year=2026&month=1").text

    def test_both_catalogues_are_named(self):
        self.assertEqual(self._page().count("Search Trakt and Simkl"), 2)

    def test_one_catalogue_goes_on_naming_itself(self):
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid",
                               simkl_client_secret="ssecret"))
        page = self._page()
        self.assertEqual(page.count("Search Simkl"), 2)
        self.assertNotIn("Search Trakt", page)

    def test_no_catalogue_names_none(self):
        save_settings(Settings(public_base_url=ORIGIN))
        page = self._page()
        self.assertNotIn("Search Trakt", page)
        self.assertNotIn("Search Simkl", page)


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
        """The bare-hit case: a search hit search left without a shared id comes
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
