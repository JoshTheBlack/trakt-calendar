"""The add flow's catalogue search fragments and its season picker
(app/distrakt/routes.py's `_search_fragment` and `distrakt_seasons_fragment`).

All three answer with RENDERED MARKUP rather than JSON — see `_search_fragment`
for why — so these read what the browser is handed. A row's own REQUEST is what
the pick acts on now, so it is parsed out and compared rather than
pattern-matched: markup that looked right and did not parse would fail in the
browser and nowhere here.

No network: each source's own search/season module function is patched at its
own module object, so these exercise the ROUTE's gating, merge-wiring and
rendering rather than either provider's HTTP call, which has its own coverage in
tests/providers/.
"""
from __future__ import annotations

import asyncio
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from datetime import date

from app.config import Settings, save_settings
from app.providers import title_search as catalogue_search
from app.providers.base import (Media, SearchHit, SearchPage, SeasonsAnswer, Source,
                                proxied_image, resolve_key)
from app.providers.tmdb import search as tmdb_search
from app.providers.simkl import detail as simkl_detail
from app.providers.simkl import search as simkl_search
from app.providers.trakt import TraktError
from app.providers.trakt import detail as trakt_detail
from tests.support import AppTestCase, ORIGIN

TRAKT_HIT = {
    "media": "show", "ids": {"trakt": 1, "tmdb": 100, "slug": "a-show"},
    "title": "A Show", "year": 2022, "network": "HBO", "runtime": None,
    "overview": "An overview.", "poster": "https://media.trakt.tv/a-show.jpg",
}

TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"
TMDB_HIT = SearchHit(source=Source.TMDB, source_id="100", media=Media.SHOW,
                     ids={"tmdb": 100}, title="A Show", year=2022, season=None,
                     network="", runtime=None, overview="")

BOTH_CONFIGURED = dict(public_base_url=ORIGIN, trakt_client_id="cid",
                       simkl_client_id="scid", simkl_client_secret="ssecret")

# A show row is the <summary> its season picker unfolds under; a film row a
# <button> that is the add.
_ROW = re.compile(r'<(?:button|summary) class="search-row[^"]*"(.*?)>', re.S)
_GET = re.compile(r'hx-get="([^"]*)"')
_TITLE = re.compile(r'<span class="search-title">([^<]*)')


def rows(html: str) -> list[dict]:
    """Every result row, as what it would ASK FOR if it were pressed.

    Parsed rather than asserted against a substring because the row's request IS
    the contract now: it names the query and the result's place in the answer,
    and the server resolves the title from that. A row carrying a title, or an
    id, or nothing at all would each look fine in a substring match.
    """
    titles = [title.strip() for title in _TITLE.findall(html)]
    out = []
    for index, attrs in enumerate(_ROW.findall(html)):
        found = {"disabled": "disabled" in attrs, "names": {}}
        get = _GET.search(attrs)
        if get:
            query = parse_qs(urlsplit(unescape(get.group(1))).query)
            found["names"] = {key: value[0] for key, value in query.items()}
        found["title"] = titles[index] if index < len(titles) else ""
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
        self.link_identity(self.user_id, "trakt", 77, "trakt-token")
        self.sign_in_as(self.user_id)

    def test_a_trakt_only_instance_can_still_search(self):
        """§1's whole finding: an instance missing one catalogue must not lose
        the ability to search at all."""
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_HIT], False))):
            html = self.client.get("/distrakt/fragments/search?also=trakt&q=a").text
        self.assertEqual(rows(html)[0]["title"], "A Show")

    def test_a_service_row_draws_the_poster_its_answer_carried_through_the_proxy(self):
        """The Content-Security-Policy admits one image host, the proxy, so a
        service's own address may never reach the page as itself."""
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_HIT], False))):
            html = self.client.get("/distrakt/fragments/search?also=trakt&q=a").text
        self.assertEqual(unescape(re.search(r'<img class="search-poster" src="([^"]*)"',
                                         html).group(1)),
                      proxied_image("https://media.trakt.tv/a-show.jpg"))
        self.assertNotIn('src="https://media.trakt.tv', html)

    def test_a_simkl_only_instance_can_still_search(self):
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid",
                               simkl_client_secret="ssecret"))
        with patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([]))):
            html = self.client.get("/distrakt/fragments/search?also=simkl&q=a").text
        self.assertEqual(rows(html), [])
        self.assertIn("No matches.", html)

    def test_no_tmdb_token_is_said_and_nothing_is_searched_in_its_place(self):
        """No service stands in for another: a Trakt client id is configured
        and Trakt is linked, and a plain search still asks NOBODY — it says TMDB
        is not configured and offers Trakt as an addition. Not "no matches",
        which would send somebody to retype a query that was never wrong."""
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        spy = AsyncMock(return_value=[TRAKT_HIT])
        with patch.object(trakt_detail, "search_titles", spy):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        spy.assert_not_awaited()
        self.assertIn("TMDB\n    is not configured", html)
        self.assertNotIn("No matches.", html)
        self.assertIn("Also search Trakt", html)

    def test_tmdb_answers_by_default_and_linked_services_are_offered(self):
        save_settings(Settings(public_base_url=ORIGIN, tmdb_api_key=TMDB_TOKEN,
                               trakt_client_id="cid", simkl_client_id="scid",
                               simkl_client_secret="ssecret"))
        trakt_spy = AsyncMock(return_value=[TRAKT_HIT])
        with patch.object(tmdb_search, "search_titles",
                          AsyncMock(return_value=SearchPage([TMDB_HIT], more=True))), \
             patch.object(trakt_detail, "search_titles", trakt_spy):
            html = self.client.get("/distrakt/fragments/search?q=a").text
        trakt_spy.assert_not_awaited()
        self.assertEqual(rows(html)[0]["title"], "A Show")
        self.assertIn("tmdb-mark.svg", html)
        self.assertIn("Also search Trakt", html)
        self.assertIn("Also search Simkl", html)
        self.assertIn("More from TMDB", html)

    def test_more_asks_the_next_page_by_number_and_the_rows_name_it(self):
        save_settings(Settings(public_base_url=ORIGIN, tmdb_api_key=TMDB_TOKEN))
        second = TMDB_HIT._replace(source_id="101", ids={"tmdb": 101}, title="Another")

        async def _pages(settings, media, query, *, page=1, cached_only=False):
            return SearchPage([TMDB_HIT] if page == 1 else [second], more=page == 1)

        with patch.object(tmdb_search, "search_titles", _pages):
            html = self.client.get("/distrakt/fragments/search?q=a&pages=tmdb:2").text
        self.assertEqual([r["title"] for r in rows(html)], ["A Show", "Another"])
        self.assertEqual(rows(html)[1]["names"]["pages"], "tmdb:2")
        self.assertNotIn("More from TMDB", html)

    def test_a_service_the_viewer_may_not_add_is_refused_in_words(self):
        save_settings(Settings(public_base_url=ORIGIN, tmdb_api_key=TMDB_TOKEN))
        html = self.client.get("/distrakt/fragments/search?q=a&also=plex").text
        self.assertIn("Can&#39;t add plex to this search.", html)
        self.assertEqual(rows(html), [])

    def test_merged_results_carry_both_sources_marks(self):
        """A title both sources found comes back once, with both source marks
        — the whole reason a merge is worth doing beyond deduplication."""
        simkl_hit = SearchHit(source=Source.SIMKL, source_id="9", media="show",
                              ids={"simkl": 9, "tmdb": 100}, title="A Show", year=2022,
                              season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_HIT], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([simkl_hit]))):
            html = self.client.get("/distrakt/fragments/search?also=trakt,simkl&q=a").text
        self.assertEqual(len(rows(html)), 1)
        # Both marks, from the macro that owns the filenames.
        self.assertIn("trakt-circlemark-dark.svg", html)
        self.assertIn("simkllogo.svg", html)

    def test_a_row_names_the_query_and_its_place_and_nothing_else(self):
        """The contract that replaced three data attributes holding a copy of
        the title: the server reads the hit back out of the same search, so what
        gets stored is the service's own answer and never a field a page sent.

        WHICH SOURCE LEADS is no longer a fact about the markup at all — it is
        `next(iter(hit.source_ids))` inside title_search.pick_from_hit, and SeasonPickerFragmentTests is where it
        is pinned."""
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_HIT], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([]))):
            html = self.client.get(
                "/distrakt/fragments/search?year=2026&month=8&also=trakt,simkl&q=a").text
        self.assertEqual(rows(html)[0]["names"],
                         {"year": "2026", "month": "8", "q": "a", "i": "0",
                          "also": "simkl,trakt"})
        self.assertNotIn("data-ids", html)
        self.assertNotIn("data-source-ids", html)

    def test_a_show_rows_season_picker_unfolds_under_that_row(self):
        """The calendar's shape: the row is the <summary> of its own <details>
        and asks once for its picker into the slot inside it. A slot beside the
        list needed taking down out of band by every later search, and sat at the
        bottom of the modal, far from the row pressed."""
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_HIT], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([]))):
            html = self.client.get(
                "/distrakt/fragments/search?year=2026&month=8&also=trakt,simkl&q=a").text
        hit = re.search(r'<details class="search-hit">\s*<summary class="search-row[^"]*"(.*?)>'
                        r'.*?<div class="search-pick"></div>\s*</details>', html, re.S)
        self.assertIsNotNone(hit)
        self.assertIn('hx-target="next .search-pick"', hit.group(1))
        self.assertIn('hx-trigger="click once"', hit.group(1))
        self.assertNotIn("addSeasonPick", html)

    def test_a_single_source_instance_still_draws_a_mark(self):
        """The mark is attribution as much as disambiguation — which service's
        own answer a row is drawn from is worth seeing even with one catalogue
        linked, and it is what tells apart a service's own data quirks (a
        mis-shaped hit, a missing network) from the merge's. Always drawn now,
        not only when more than one catalogue was askable."""
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_HIT], False))):
            html = self.client.get("/distrakt/fragments/search?also=trakt&q=a").text
        self.assertIn("trakt-circlemark-dark.svg", html)

    def test_one_source_failing_is_named_and_does_not_fail_the_search(self):
        async def _boom(*args, **kwargs):
            raise TraktError("down")

        with patch.object(trakt_detail, "search_titles", _boom), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([]))):
            html = self.client.get("/distrakt/fragments/search?also=trakt,simkl&q=a").text
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
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([bare_hit]))):
            html = self.client.get("/distrakt/fragments/search?also=trakt,simkl&q=a").text
        self.assertFalse(rows(html)[0]["disabled"])
        # It has no shared id at all, and naming a result by its PLACE is what
        # lets it be picked anyway.
        self.assertEqual(rows(html)[0]["names"]["i"], "0")

    def test_a_row_says_which_season_it_is_when_its_source_named_one(self):
        """Several titles of one series come back sharing a name — three
        "Sousou no Frieren" — and a year says when a season aired rather than
        which season it is. The source names the season for exactly those
        (app/providers/simkl/search.py), and the row has to draw it or the
        rows are still indistinguishable."""
        named = [SearchHit(source=Source.SIMKL, source_id=sid, media="show",
                           ids={"simkl": sid, "tmdb": 209867}, title="Sousou no Frieren",
                           year=year, season=season, network="", runtime=None, overview="")
                 for sid, year, season in (("1990194", 2023, 1), ("2595284", 2026, 2),
                                           ("3063278", 2027, 3))]
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage(named))):
            html = self.client.get("/distrakt/fragments/search?also=trakt,simkl&q=frieren").text
        for season in (1, 2, 3):
            with self.subTest(season=season):
                self.assertIn(f"Season {season}</span>", html)
        self.assertEqual(len(rows(html)), 3)

    def test_a_row_whose_source_named_no_season_claims_none(self):
        """It is about to get a picker; claiming a season it is going to ask
        about would be answering the question in the row."""
        unnamed = SearchHit(source=Source.SIMKL, source_id="10452", media="show",
                            ids={"simkl": 10452, "tmdb": 1395}, title="Gossip Girl",
                            year=2007, season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([unnamed]))):
            html = self.client.get("/distrakt/fragments/search?also=trakt,simkl&q=gossip").text
        self.assertNotIn("distrakt-search-season", html)


TRAKT_MOVIE_HIT = {
    "media": "movie", "ids": {"trakt": 1, "tmdb": 200, "slug": "a-movie"},
    "title": "A Movie", "year": 2022, "network": "", "runtime": 118,
    "overview": "A movie overview.", "poster": "",
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
        self.link_identity(self.user_id, "trakt", 77, "trakt-token")
        self.sign_in_as(self.user_id)

    def test_a_trakt_only_instance_can_still_search(self):
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid"))
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_MOVIE_HIT], False))):
            html = self.client.get("/distrakt/fragments/search-movie?also=trakt&q=a").text
        self.assertEqual(rows(html)[0]["title"], "A Movie")
        # A film's third column is its runtime; it has no network to show.
        self.assertIn("118 min", html)

    def test_a_simkl_only_instance_can_still_search(self):
        """The gap §1 named as sharp and total: an instance with no Trakt
        client id could not search for a film at all before this route went
        through the registry."""
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid",
                               simkl_client_secret="ssecret"))
        with patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([]))):
            html = self.client.get("/distrakt/fragments/search-movie?also=simkl&q=a").text
        self.assertEqual(rows(html), [])

    def test_no_tmdb_token_is_said_rather_than_searched_around(self):
        save_settings(Settings(public_base_url=ORIGIN))
        html = self.client.get("/distrakt/fragments/search-movie?q=a").text
        self.assertIn("TMDB\n    is not configured", html)
        self.assertNotIn("No matches.", html)

    def test_merged_results_carry_both_sources_marks(self):
        simkl_hit = SearchHit(source=Source.SIMKL, source_id="9", media="movie",
                              ids={"simkl": 9, "tmdb": 200}, title="A Movie", year=2022,
                              season=None, network="", runtime=None, overview="")
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([TRAKT_MOVIE_HIT], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([simkl_hit]))):
            html = self.client.get("/distrakt/fragments/search-movie?also=trakt,simkl&q=a").text
        self.assertEqual(len(rows(html)), 1)

    def test_one_source_failing_is_named_and_does_not_fail_the_search(self):
        async def _boom(*args, **kwargs):
            raise TraktError("down")

        with patch.object(trakt_detail, "search_titles", _boom), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([]))):
            html = self.client.get("/distrakt/fragments/search-movie?also=trakt,simkl&q=a").text
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
        with patch.object(trakt_detail, "search_titles", AsyncMock(return_value=([], False))), \
             patch.object(simkl_search, "search_titles", AsyncMock(return_value=SearchPage([bare_hit]))):
            html = self.client.get("/distrakt/fragments/search-movie?also=trakt,simkl&q=a").text
        # `disabled` is what refuses it now: the row became the <button> it
        # always was, and the element already knows how to be unpressable.
        self.assertTrue(rows(html)[0]["disabled"])
        self.assertIn("Untethered", html)


class SearchLabelTests(AppTestCase):
    """The add modals' field labels name what is ACTUALLY searched on Enter,
    from the same registry call that answers the search: the default catalogue,
    and never a linked service, which is only an offer under the results."""

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

    def test_tmdb_is_named_and_a_linked_service_is_not(self):
        save_settings(Settings(**BOTH_CONFIGURED, tmdb_api_key=TMDB_TOKEN))
        page = self._page()
        self.assertEqual(page.count("Search TMDB"), 2)
        self.assertNotIn("Search Simkl", page)
        self.assertNotIn("Search Trakt", page)

    def test_no_catalogue_names_none(self):
        save_settings(Settings(public_base_url=ORIGIN))
        page = self._page()
        self.assertNotIn("Search Trakt", page)
        self.assertNotIn("Search Simkl", page)


class SeasonPickerFragmentTests(AppTestCase):
    """What a click on a search result answers with, and how the result it names
    is resolved.

    THE ADDRESS NAMES A RESULT AND CARRIES NOTHING ELSE. It used to carry the
    source, that source's id and every shared id the hit had, and the answer was
    JSON the browser turned into buttons. Now it names the query and the result's
    place in the answer, the server reads the hit back out of the same cached
    search, and the answer is the picker itself — so no id a page could edit
    reaches the add behind it.

    THE PLACE RATHER THAN THE KEY, and that is not laziness: a show hit can
    legitimately have no shared id at all, which is the ordinary shape of a Simkl
    season-title before its per-title lookup fills one in — and those are exactly
    the rows this flow exists to reach.
    """

    def make_settings(self):
        return Settings(**BOTH_CONFIGURED)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("picker", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)
        # Taken from the clock, because which add route a season button posts to
        # follows from whether the month is behind it — a written-down month
        # would rot the day the real calendar passed it.
        today = date.today()
        self.ahead = (today.year + 1, 1)
        self.behind = (today.year - 1, 1)

    def _found(self, *hits):
        """The search the picker re-reads, stood in for.

        Patched at the merge rather than seeded into the response cache: what
        these tests are about is the per-title lookup the pick then makes, and
        which answers a transport happens to have stored is a different
        question — tests/distrakt/test_add_flow.py asks that one.
        """
        return patch.object(catalogue_search, "search_catalogue", AsyncMock(
            return_value=catalogue_search.SearchMergeResult(
                hits=list(hits), failed=frozenset())))

    def _hit(self, source, source_id, ids, title="A Show", season=None, network="",
            media=Media.SHOW):
        return catalogue_search.MergedSearchHit(
            key=None, season=season, source_ids={source: source_id}, ids=dict(ids),
            title=title, year=2022, network=network, runtime=None, overview="", media=media)

    def _pick(self, index=0, query="a", month=None):
        month = month or self.ahead
        return self.client.get(
            f"/distrakt/fragments/seasons?year={month[0]}&month={month[1]}"
            f"&q={query}&i={index}").text

    def _resolved(self, index=0, query="a"):
        """The Pick the fragment was drawn from — for the facts that are about
        the RESOLUTION rather than about the markup."""
        from app.auth import viewer

        async def _run():
            settings = await viewer.settings_for_user(self.user_id)
            return await catalogue_search.resolve_pick(
                settings, Media.SHOW, query, index, cached_only=False)

        return asyncio.run(_run())

    def test_a_result_that_is_not_in_the_answer_is_refused(self):
        with self._found():
            self.assertIn("That search has expired", self._pick())

    def test_a_result_named_past_the_end_of_the_answer_is_refused(self):
        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100})):
            self.assertIn("That search has expired", self._pick(index=9))

    def test_a_nonsense_place_is_refused_rather_than_guessed_at(self):
        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100})):
            self.assertIn("That search has expired", self._pick(index="../7"))

    def test_the_sources_own_catalogue_gate_is_asked(self):
        save_settings(Settings(public_base_url=ORIGIN))
        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100})):
            self.assertIn("That search has expired", self._pick())

    def test_a_trakt_hit_draws_its_season_list(self):
        seasons = [{"season": 1, "episode_count": 10}]
        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100})), \
             patch.object(trakt_detail, "fetch_show_seasons", AsyncMock(return_value=seasons)):
            html = self._pick(month=self.ahead)
        self.assertIn('name="season:1"', html)
        self.assertIn("10 eps", html)
        # The checklist names the result and the seasons, and nothing about the
        # title: no id a browser could edit reaches the write.
        self.assertIn('name="q" value="a"', html)
        self.assertNotIn("tmdb", html)

    def test_a_hit_that_resolves_to_a_film_is_not_offered_a_season(self):
        """DEFENSE IN DEPTH: app/providers/simkl/search.py now drops a
        movie-typed anime hit from a show search outright (_FILM_SEARCH_TYPE),
        so this shape should not reach the fragment in ordinary use any more —
        but `_found` below stands in for the merge directly, bypassing that
        filter, to pin what happens if one ever does (a stale cached search
        from before the filter existed, say). It gets no season list and no
        auto-add — a film has no roster the way a show does and needs a
        watched-on date this box never collected — so the fragment says what
        it is rather than filing it wrong or silently doing nothing."""
        with self._found(self._hit(Source.SIMKL, "2544548", {"simkl": 2544548, "tmdb": 1333100},
                                   title="The Last Attack", media=Media.MOVIE)):
            html = self._pick()
        self.assertIn("is a film, not a show", html)
        self.assertIn("The Last Attack", html)
        self.assertNotIn("hx-trigger=\"load\"", html)
        self.assertNotIn("distrakt-season-list", html)

    def test_the_add_routes_refuse_a_pick_that_resolved_to_a_film(self):
        """Defense in depth beside the fragment's own refusal above: a client
        that posts directly (or one from before this guard existed) must not
        be able to write a film onto the roster as a zero-progress show."""
        movie_pick = catalogue_search.Pick(
            title="The Last Attack", network="", year=2024, ids={"tmdb": 1333100},
            seasons=[], named_season=None, media=Media.MOVIE)
        with patch.object(catalogue_search, "resolve_pick",
                          AsyncMock(return_value=movie_pick)):
            resp = self.client.post("/api/distrakt/add",
                                    json={"q": "a", "i": 0, "season": 1})
        self.assertIn("is a film, not a show", resp.text)

    def test_a_month_behind_the_clock_records_something_as_finished_instead(self):
        """Which of the two add routes a season button posts to is the server's
        decision, and it is the same test each of those routes already enforces
        for itself: a past month is filled in by hand."""
        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100})), \
             patch.object(trakt_detail, "fetch_show_seasons",
                          AsyncMock(return_value=[{"season": 1, "episode_count": 10}])):
            self.assertIn("/api/distrakt/add-completed", self._pick(month=self.behind))
            self.assertIn("/api/distrakt/add?", self._pick(month=self.ahead))

    def test_a_bare_simkl_hit_is_resolved_by_the_same_lookup(self):
        """The bare-hit case: a hit the search left without a shared id comes
        back keyable once the per-title lookup has run, with nothing carried on
        the address at all — the lookup's own answer is enough on its own."""
        answer = SeasonsAnswer(seasons=[], named_season=3, network="",
                               ids={"tmdb": 1429, "tvdb": 99, "imdb": "tt1", "mal": 51019})
        with self._found(self._hit(Source.SIMKL, "694485", {"simkl": 694485})), \
             patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            pick = self._resolved()
        self.assertEqual(pick.named_season, 3)
        self.assertEqual(pick.ids, {"simkl": 694485, "tmdb": 1429, "tvdb": 99,
                                    "imdb": "tt1", "mal": 51019})
        self.assertIsNotNone(resolve_key(Media.SHOW, pick.ids))

    def test_a_season_that_names_itself_arrives_as_that_season_ticked(self):
        """Asking which season would be asking a question the row already
        answered, so the checklist offers that season alone, ticked — the
        app's one season picker, the same on every surface."""
        answer = SeasonsAnswer(seasons=[], named_season=3, network="",
                               ids={"tmdb": 1429})
        with self._found(self._hit(Source.SIMKL, "694485", {"simkl": 694485})), \
             patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            html = self._pick()
        self.assertIn('name="season:3" value="1" checked', html)
        self.assertNotIn('name="season:1"', html)

    def test_a_hit_still_unkeyable_after_the_lookup_says_so(self):
        answer = SeasonsAnswer(seasons=[], named_season=None, ids={}, network="")
        with self._found(self._hit(Source.SIMKL, "1", {"simkl": 1}, title="Obscure")), \
             patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            html = self._pick()
        self.assertIn("Obscure", html)
        self.assertNotIn("hx-post", html)

    def test_the_hits_own_ids_are_kept_when_the_lookup_adds_nothing_new(self):
        """A Trakt hit is never bare — its per-title lookup surfaces no new ids
        (SeasonsAnswer.ids is always {}) — so the ids the hit already had must
        survive the union, or every Trakt hit would look unkeyable."""
        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100})), \
             patch.object(trakt_detail, "fetch_show_seasons", AsyncMock(return_value=[])):
            pick = self._resolved()
        self.assertEqual(pick.ids, {"tmdb": 100})
        self.assertIsNotNone(resolve_key(Media.SHOW, pick.ids))

    def test_the_lookups_own_ids_win_a_disagreement_with_the_hits(self):
        """`season` is read off the same per-title record `ids` comes from, so
        those two describe one title by construction. An id that disagrees
        describes a DIFFERENT one — and keeping it files the record with one
        title's season beside another title's id, which is a row nothing can
        count. The hit's other ids still travel."""
        answer = SeasonsAnswer(seasons=[], named_season=2, network="",
                               ids={"simkl": 2595284, "tmdb": 209867})
        with self._found(self._hit(Source.SIMKL, "2595284",
                                   {"simkl": 1990194, "tmdb": 209867, "imdb": "tt22248376"})), \
             patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            pick = self._resolved()
        self.assertEqual(pick.ids, {"simkl": 2595284, "tmdb": 209867,
                                    "imdb": "tt22248376"})

    def test_the_lookup_carries_the_network_a_simkl_search_hit_never_has(self):
        """Simkl's search hit has no network and a single-catalogue instance has
        no other source to fill the gap from, so without this a show added by
        hand reaches the roster with none — no emoji in the announcement post,
        and "" registered in the viewer's emoji map."""
        answer = SeasonsAnswer(seasons=[], named_season=2, ids={"tmdb": 209867},
                               network="Nippon TV")
        with self._found(self._hit(Source.SIMKL, "2595284", {"simkl": 2595284})), \
             patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            pick = self._resolved()
        self.assertEqual(pick.network, "Nippon TV")

    def test_the_hits_own_network_is_not_overridden(self):
        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100}, network="HBO")), \
             patch.object(trakt_detail, "fetch_show_seasons", AsyncMock(return_value=[])):
            pick = self._resolved()
        self.assertEqual(pick.network, "HBO")

    def test_an_ambiguous_season_falls_back_to_the_picker(self):
        answer = SeasonsAnswer(seasons=[{"season": 1, "episode_count": 12}],
                               named_season=None, ids={"tmdb": 1}, network="")
        with self._found(self._hit(Source.SIMKL, "1", {"simkl": 1})), \
             patch.object(simkl_detail, "fetch_seasons", AsyncMock(return_value=answer)):
            html = self._pick()
        self.assertIn('name="season:1"', html)
        self.assertIn("12 eps", html)
        self.assertNotIn('hx-trigger="load"', html)

    def test_a_genuine_failure_is_drawn_rather_than_answered_with_a_status(self):
        """A non-200 is never swapped, so a 502 here would leave the slot empty
        with nothing to explain it."""
        async def _boom(*args, **kwargs):
            raise TraktError("Could not reach Trakt", status=502)

        with self._found(self._hit(Source.TRAKT, "1", {"tmdb": 100})), \
             patch.object(trakt_detail, "fetch_show_seasons", _boom):
            resp = self.client.get(
                f"/distrakt/fragments/seasons?year={self.ahead[0]}&month=1&q=a&i=0")
        self.assertEqual(resp.status_code, 200)
        # AND IT SAYS WHICH FAILURE IT WAS. "Search for it again" would send
        # somebody to retype a query that was never the problem.
        self.assertIn("could not be looked up", resp.text)


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
