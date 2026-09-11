"""A title on a rankings board opens the details panel every other surface opens.

WHAT IS PARTICULAR TO THE BOARD, and so what this file holds:

  A BOARD TITLE HAS NO SEASON, so its panel opens on the first one and the season
  picker reaches the rest — as wide as the row's own count, the season opened or
  the describing service's count, whichever is largest.

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

from app.config import Settings
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

    def test_the_rows_own_count_can_widen_the_strip(self):
        key = self.add(ids={"trakt": 7, "tmdb": 1}, season_count=5)
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
        self.assertIn("without an id from any service", html)
        self.assertEqual(panel.season_tabs, [])
        self.assertEqual(panel.episodes, [])

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
