"""A title on a rankings board opens the details panel every other surface opens.

WHAT IS PARTICULAR TO THE BOARD, and so what this file holds:

  A SEASON ROW OPENS ON ITS SEASON; a whole-show row has none of its own, so its
  panel opens on the first one. Either way the season picker reaches the rest —
  as wide as the row's own count, the season opened or the describing service's
  count, whichever is largest.

  THE TITLE IS NAMED BY BOARD AND KEY, and everything else comes from the stored
  row. A request cannot point the lookup at a title the board does not hold, and
  a season tab carries those two names and nothing a caller invented.

  A TITLE NOBODY HERE CAN DESCRIBE STILL OPENS, on what the row itself knows, and
  says why there are no episodes — without a single outbound call.

No network: the Trakt transport's pooled client is a stub, and where a test is
about a call NOT being made the stub fails the test if it is reached.
"""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from app import db
from app.config import Settings
from app.metadata import refresh as metadata_refresh, rows
from app.providers.trakt import transport
from app.ranker import core as ranker
from tests.support import AppTestCase, DetailsPanel, ORIGIN

BOARD = "b1"

SHOW = {"title": "The Agency", "year": 2026, "overview": "Spies.",
        "status": "returning series", "network": "Paramount+", "runtime": 50,
        "genres": ["drama"], "rating": 8.0}
SEASONS = [{"number": n, "title": "", "overview": f"What season {n} is about.",
            "episode_count": 5, "aired_episodes": 5} for n in (1, 2, 3)]


class _Response:
    status_code = 200
    text = ""
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _CatalogueClient:
    """Answers the catalogue paths a panel reads for Trakt show 7, per season, and
    records every path it was asked — so a test can say what was looked up."""

    def __init__(self):
        self.asked: list[str] = []

    async def get(self, url, headers=None, timeout=None):
        path = url.split("?", 1)[0].split("api.trakt.tv/", 1)[-1]
        self.asked.append(path)
        if path == "shows/7":
            return _Response(SHOW)
        if path == "shows/7/seasons":
            return _Response(SEASONS)
        if path == "shows/7/videos":
            return _Response([])
        if path.startswith("shows/7/seasons/") and path.endswith("/people"):
            season = path.split("/")[3]
            return _Response({"cast": [{"person": {"name": f"S{season} Actor"},
                                        "character": "Someone"}]})
        if path.startswith("shows/7/seasons/"):
            season = int(path.rsplit("/", 1)[1])
            return _Response([{"number": n, "title": f"S{season} Ep {n}",
                               "first_aired": "2026-08-01T20:00:00.000Z"} for n in range(1, 6)])
        raise AssertionError(f"the panel asked Trakt for {path!r}, which nothing here answers")


class _Unreachable:
    """A pooled client whose every request fails the test."""

    asked: list[str] = []

    async def get(self, url, headers=None, timeout=None):
        raise AssertionError(f"an outbound call was made for {url!r}")


class _BoardPanelTestCase(AppTestCase):

    def make_settings(self):
        # The instance's Trakt catalogue credential and nothing else, which is all
        # a board title's panel uses. Simkl's is left unset, so a title only Simkl
        # knows is one this instance cannot ask about.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_public_calendar_enabled=False)

    def setUp(self):
        super().setUp()
        self.make_user("admin_user", is_admin=True)
        self.user_id = self.make_user("ranker", ranker_approved=True)
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD, name="Top"))

    def add(self, match_id: str = "1", media: str = "show", **fields) -> str:
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [{
            "media": media, "match_source": "tmdb", "match_id": match_id,
            "tmdb": int(match_id), "title": "The Agency", **fields}]))
        return f"{media}:tmdb:{match_id}"

    def panel_url(self, key: str, **extra) -> str:
        return "/rankings/fragments/details?" + urlencode({"board": BOARD, "key": key, **extra})

    def get(self, url: str, client=None):
        client = client or _CatalogueClient()
        with patch.object(transport, "shared_client", return_value=client):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.text, client


class ABoardTitleOpensThePanelTests(_BoardPanelTestCase):

    def test_it_opens_on_the_first_season_with_the_rest_one_tab_away(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        html, _ = self.get(self.panel_url(key))
        panel = DetailsPanel(html)
        self.assertEqual(panel.open_tab, 1)
        self.assertEqual(panel.season_tabs, [1, 2, 3])
        self.assertEqual(panel.episodes, [1, 2, 3, 4, 5])
        self.assertIn("Spies.", html)
        self.assertIn("What season 1 is about.", html)

    def test_a_count_another_service_states_can_widen_the_strip(self):
        """The board row draws its season count from the title store, where a
        service other than the one describing the panel may count more seasons;
        that count is the surface's floor (details_panel.panel)."""
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        asyncio.run(db.execute(f"UPDATE {rows.TITLES} SET season_count = 5 "
                               "WHERE source = 'tmdb' AND title_key = ?", (key,)))
        panel = DetailsPanel(self.get(self.panel_url(key))[0])
        self.assertEqual(panel.season_tabs, [1, 2, 3, 4, 5])

    def test_a_tab_carries_the_board_and_the_key_and_nothing_a_caller_invented(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        panel = DetailsPanel(self.get(self.panel_url(key, evil="1"))[0])
        url = urlsplit(panel.tab_urls[2])
        self.assertEqual(url.path, "/rankings/fragments/details/season")
        self.assertEqual(parse_qs(url.query), {"board": [BOARD], "key": [key], "season": ["2"]})

    def test_a_tab_answers_with_that_season_alone(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        panel = DetailsPanel(self.get(self.panel_url(key))[0])
        html, _ = self.get(panel.tab_urls[3])
        self.assertIn("S3 Ep 1", html)
        self.assertIn("What season 3 is about.", html)
        self.assertNotIn("details-hero", html)

    def test_the_ids_come_from_the_row_and_not_from_the_request(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        html, client = self.get(self.panel_url(key, trakt="999", simkl="999", media="movie"))
        self.assertIn("Spies.", html)
        self.assertTrue(client.asked)
        self.assertTrue(all(path.startswith("shows/7") for path in client.asked), client.asked)

    def test_nobodys_viewing_is_drawn_on_a_board(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        panel = DetailsPanel(self.get(self.panel_url(key))[0])
        self.assertEqual(panel.services, [])
        self.assertEqual(panel.watched, [])

    def test_it_links_out_to_the_service_the_row_knows(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        html, _ = self.get(self.panel_url(key))
        self.assertIn("details-links", html)


class ASeasonRowOpensOnItsSeasonTests(_BoardPanelTestCase):
    """A board row can BE a season, and its panel opens on that season — through
    the same one panel and renderer; a whole-show row still opens on the first."""

    def test_a_season_row_opens_on_its_own_season(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1}, season=2)
        self.assertEqual(key, "show:tmdb:1")
        html, client = self.get(self.panel_url("show:tmdb:1:s2"))
        panel = DetailsPanel(html)
        self.assertEqual(panel.open_tab, 2)
        self.assertEqual(panel.season_tabs, [1, 2, 3])
        self.assertIn("What season 2 is about.", html)
        self.assertIn("S2 Ep 1", html)
        self.assertIn("shows/7/seasons/2", client.asked)
        self.assertNotIn("shows/7/seasons/1", client.asked)

    def test_its_tabs_still_reach_the_other_seasons(self):
        self.add(ids={"trakt": 7, "tmdb": 1}, season=2)
        panel = DetailsPanel(self.get(self.panel_url("show:tmdb:1:s2"))[0])
        url = urlsplit(panel.tab_urls[3])
        self.assertEqual(parse_qs(url.query),
                         {"board": [BOARD], "key": ["show:tmdb:1:s2"], "season": ["3"]})
        html, _ = self.get(panel.tab_urls[3])
        self.assertIn("What season 3 is about.", html)

    def test_a_whole_show_row_beside_it_still_opens_on_the_first(self):
        self.add(ids={"trakt": 7, "tmdb": 1}, season=2)
        self.add(ids={"trakt": 7, "tmdb": 1})
        self.assertEqual(DetailsPanel(self.get(self.panel_url("show:tmdb:1"))[0]).open_tab, 1)


class ATitleNobodyCanDescribeTests(_BoardPanelTestCase):

    def test_it_opens_on_what_the_row_knows_and_says_why_there_are_no_episodes(self):
        key = self.add(ids={"tmdb": 1}, network="HBO", runtime=50)
        html, _ = self.get(self.panel_url(key), client=_Unreachable())
        panel = DetailsPanel(html)
        self.assertIn("details-hero", html)
        self.assertIn("📡 HBO", html)
        self.assertIn("⏱️ 50m", html)
        self.assertIn("No overview available.", html)
        self.assertIn("No seasons or episodes to show", html)
        # A board row filed under a tmdb id is a title TMDB knows, and this
        # instance holds no TMDB token: a service that COULD answer is not set
        # up, which is a different thing to say from "nobody can".
        self.assertIn("not set up here", html)
        self.assertEqual(panel.season_tabs, [])
        self.assertEqual(panel.episodes, [])

    def test_a_row_no_service_issues_an_id_for_says_nobody_can_be_asked(self):
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [{
            "media": "show", "match_source": "imdb", "match_id": "tt0000001",
            "title": "The Agency", "ids": {"imdb": "tt0000001"}}]))
        html, _ = self.get(self.panel_url("show:imdb:tt0000001"), client=_Unreachable())
        self.assertIn("without an id from any service", html)

    def test_a_film_is_not_told_it_has_no_episodes(self):
        key = self.add("5", media="movie", ids={"tmdb": 5})
        html, _ = self.get(self.panel_url(key), client=_Unreachable())
        self.assertIn("Nothing more to show", html)
        self.assertNotIn("episode", html.lower())

    def test_a_title_whose_only_service_is_not_set_up_here_says_that_instead(self):
        """A different reason a reader can act on differently: this one could be
        looked up, on an instance holding that service's credential."""
        key = self.add(ids={"simkl": 42, "tmdb": 1})
        html, _ = self.get(self.panel_url(key), client=_Unreachable())
        self.assertIn("not set up here", html)
        self.assertNotIn("without an id", html)


class RefusalTests(_BoardPanelTestCase):

    def test_a_title_the_board_does_not_hold_is_a_drawn_refusal(self):
        html, _ = self.get(self.panel_url("show:tmdb:404"), client=_Unreachable())
        self.assertIn("no longer on this board", html)

    def test_another_accounts_board_is_refused_the_same_way_and_asks_nobody(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        self.sign_in_as(self.make_user("stranger", ranker_approved=True))
        html, _ = self.get(self.panel_url(key), client=_Unreachable())
        self.assertIn("no longer on this board", html)
        self.assertNotIn("The Agency", html)


class TheBoardCarriesThePanelTests(_BoardPanelTestCase):

    def test_the_page_includes_the_shared_shell_and_each_row_asks_by_board_and_key(self):
        self.add(ids={"trakt": 7, "tmdb": 1})
        html = self.client.get(f"/rankings?board={BOARD}").text
        self.assertIn('id="detailsModal"', html)
        self.assertIn('data-details-open="/rankings/fragments/details?board=b1&amp;'
                      'key=show%3Atmdb%3A1"', html)


class ATitleOnlyTmdbKnowsTests(_BoardPanelTestCase):
    """A board row carrying a tmdb id and nothing else — an imported title, or
    one no other service lists — is described by TMDB, which is first in the
    order, with its seasons as tabs and TMDB's own page as the outbound link."""

    TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJib2FyZCJ9.c2lnbmF0dXJl"

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, tmdb_api_key=self.TOKEN,
                        simkl_public_calendar_enabled=False)

    def test_tmdb_describes_it(self):
        asked = []

        async def tmdb_get(settings, path, params=None, **kw):
            asked.append(path)
            if path == "tv/42":
                return {"id": 42, "name": "A Show Only TMDB Lists",
                        "overview": "TMDB's words.", "status": "Ended",
                        "seasons": [{"season_number": 1, "episode_count": 3,
                                     "air_date": "2024-01-01"},
                                    {"season_number": 2, "episode_count": 2,
                                     "air_date": "2025-01-01"}]}
            if path.endswith("aggregate_credits"):
                return {"cast": [{"name": "Board Lead", "roles": [{"character": "Hero"}]}]}
            if path.startswith("tv/42/season/"):
                return {"episodes": [{"episode_number": n, "name": f"Ep {n}",
                                      "air_date": "2024-01-0%d" % n} for n in (1, 2)]}
            raise AssertionError(f"TMDB asked about {path}")

        key = self.add("42", ids={"tmdb": 42})
        with patch("app.providers.tmdb.transport.cached_get", tmdb_get):
            html, _ = self.get(self.panel_url(key), client=_Unreachable())
        panel = DetailsPanel(html)
        self.assertIn("TMDB&#39;s words.", html)
        self.assertEqual(len(panel.season_tabs), 2)
        self.assertEqual(panel.episodes, [1, 2])
        self.assertIn('href="https://www.themoviedb.org/tv/42"', html)
        self.assertIn("tv/42", asked)
        self.assertIn("Board Lead", html)


class ABoardTitleViewedTests(_BoardPanelTestCase):
    """A board's panel and its rows name their titles to the title store's
    refresh queue (app/metadata/refresh.py) — behind the page. A row's own filing
    id rides with its ids, so a title filed by one service's id is asked of it."""

    def setUp(self):
        super().setUp()
        self.seen = []
        recorder = patch.object(metadata_refresh, "viewed",
                                lambda settings, wanted, **kw: self.seen.append(
                                    (list(wanted), kw)))
        recorder.start()
        self.addCleanup(recorder.stop)

    def test_a_panel_names_its_title(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1})
        self.get(self.panel_url(key))
        (wanted, kw), = self.seen
        self.assertEqual(kw["budget"], metadata_refresh.PANEL_BUDGET)
        self.assertEqual([(w.media, str(w.ids.get("tmdb"))) for w in wanted], [("show", "1")])

    def test_the_rows_a_board_draws_are_named(self):
        self.add("1", ids={"tmdb": 1})
        self.add("2", ids={"tmdb": 2})
        self.client.get(f"/rankings?board={BOARD}")
        (wanted, kw), = self.seen
        self.assertEqual(kw["budget"], metadata_refresh.ROWS_BUDGET)
        self.assertEqual(sorted(str(w.ids.get("tmdb")) for w in wanted), ["1", "2"])
