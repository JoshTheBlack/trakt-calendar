"""The add modals' as-you-type half: stored titles listed as somebody types,
and a pick from that list resolved from the stored rows rather than the page
(app/distrakt/routes.py's `_stored_fragment` and `_named_pick`).

WHAT THIS FILE HOLDS, and why each matters:
  - TYPING ASKS NO SERVICE. Not a search, not a token refresh — the list is an
    index read over what calendar months already stored, so it can run on
    every keystroke without spending anybody's rate limit.
  - A TYPED ROW NAMES ITS STORED IDENTITY AND NOTHING ELSE, and the season
    picker and every add route read the title, the ids and the network back
    out of the rows the services wrote. A key nothing stores is refused, and
    a title the page sends is never read.
  - A TITLE ONLY ONE SERVICE STORED STILL ADDS for a viewer who never linked
    that service, because describing a title is a public per-title read.
  - ENTER STILL ASKS EXACTLY THE VIEWER'S SERVICES, as it always has.

No network: stored titles are written with the calendar's own writer, a season
lookup's answer is put in the shared response cache under the transport's own
key (tests/distrakt/test_add_flow.py explains why it is spelled out), and the
autouse guard in tests/conftest.py fails anything that reaches past both.
"""
from __future__ import annotations

import asyncio
import json
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from app import db
from app.calendar import entries
from app.config import Settings
from tests.support import save_settings
from app.distrakt import watch_history
from app import distrakt as distrakt_store
from app.providers.tmdb import search as tmdb_search
from app.providers.base import Media, Record, SearchPage, SeasonsAnswer, Source, proxied_image
from app.providers.simkl import detail as simkl_detail
from app.providers.simkl import search as simkl_search
from app.providers.trakt import detail as trakt_detail
from tests.distrakt.test_add_flow import SEASONS, SEASONS_URL, AddFlowTestCase
from tests.support import AppTestCase, ORIGIN

AIR = 1783540800.0  # 2026-07-08 20:00Z
SEVERANCE_KEY = "show:tmdb:95396"
HEAT_KEY = "movie:tmdb:949"

# A show row is the <summary> its season picker unfolds under; a film row a
# <button> that is the add.
_ROW = re.compile(r'<(?:button|summary) class="search-row[^"]*"(.*?)>', re.S)
_TITLE = re.compile(r'<span class="search-title">([^<]*)')


def _severance(**overrides) -> Record:
    values = dict(source=Source.TRAKT, media=Media.SHOW, id="95396",
                  ids={"trakt": 95396, "tmdb": 95396, "slug": "severance"},
                  detail_url="https://trakt.test/severance", title="Severance",
                  air_ts=AIR, year=2022, network="Apple TV+", season=1,
                  episode_number=1, enriched=True)
    values.update(overrides)
    return Record(**values)


def _heat(**overrides) -> Record:
    values = dict(source=Source.TRAKT, media=Media.MOVIE, id="6",
                  ids={"trakt": 6, "tmdb": 949, "slug": "heat-1995"},
                  detail_url="https://trakt.test/heat", title="Heat", air_ts=AIR,
                  year=1995, runtime=170, enriched=True)
    values.update(overrides)
    return Record(**values)


def store(*records, endpoint="shows/premieres"):
    now = db.now()
    asyncio.run(entries.store_loose_airings(endpoint, list(records), now=now,
                                            stale_after=now + 86400))


def rows(html: str) -> list[dict]:
    """Every result row as what it would SEND if pressed: a show row's GET
    parameters or a film row's JSON body, with its title beside it."""
    titles = [title.strip() for title in _TITLE.findall(html)]
    out = []
    for index, attrs in enumerate(_ROW.findall(html)):
        found = {"title": titles[index] if index < len(titles) else ""}
        get = re.search(r'hx-get="([^"]*)"', attrs)
        vals = re.search(r'hx-vals="([^"]*)"', attrs)
        if get:
            found["names"] = {k: v[0] for k, v in
                              parse_qs(urlsplit(unescape(get.group(1))).query).items()}
        if vals:
            found["body"] = json.loads(unescape(vals.group(1)))
        out.append(found)
    return out


class TypingTests(AddFlowTestCase):
    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_client_id="scid", simkl_public_calendar_enabled=False)

    def typed(self, query, path="/distrakt/fragments/search-stored"):
        year, month = self.current
        return self.client.get(f"{path}?year={year}&month={month}&q={query}").text

    def test_typing_lists_stored_titles_and_asks_no_service_at_all(self):
        """No search, and not even the viewer's settings: building those can
        refresh a token, which is a request."""
        store(_severance())
        trakt = AsyncMock(return_value=[])
        simkl = AsyncMock(return_value=[])
        viewer_settings = AsyncMock(side_effect=AssertionError("built viewer settings"))
        with patch.object(trakt_detail, "search_titles", trakt), \
             patch.object(simkl_search, "search_titles", simkl), \
             patch("app.auth.viewer.settings_for_user", viewer_settings):
            html = self.typed("sever")
        self.assertEqual([row["title"] for row in rows(html)], ["Severance"])
        trakt.assert_not_awaited()
        simkl.assert_not_awaited()
        viewer_settings.assert_not_awaited()

    def test_a_typed_row_draws_the_poster_its_calendar_card_draws(self):
        """Read from the same stored row, through the one image host the
        Content-Security-Policy admits."""
        store(_severance(poster="https://media.trakt.tv/severance.jpg"))
        html = self.typed("sever")
        src = unescape(re.search(r'<img class="search-poster" src="([^"]*)"', html).group(1))
        self.assertEqual(src, proxied_image("https://media.trakt.tv/severance.jpg"))

    def test_every_row_queues_on_the_box_s_own_results(self):
        """A server test cannot run htmx, so this pins the attribute the browser
        measurement was made with — see _title_search.html."""
        store(_severance())
        store(_heat(), endpoint="movies")
        for query, path, results in (
                ("sever", "/distrakt/fragments/search-stored", "#addSearchResults"),
                ("heat", "/distrakt/fragments/search-movie-stored", "#addMovieResults")):
            with self.subTest(path=path):
                attrs = _ROW.findall(self.typed(query, path))
                self.assertEqual(len(attrs), 1)
                self.assertIn(f'hx-sync="{results}:queue last"', attrs[0])

    def test_a_typed_row_names_its_stored_identity_and_nothing_else(self):
        store(_severance())
        year, month = self.current
        row, = rows(self.typed("sever"))
        self.assertEqual(row["names"], {"year": str(year), "month": str(month),
                                        "t": SEVERANCE_KEY})

    def test_a_title_both_services_stored_is_one_row_with_both_marks(self):
        store(_severance(network=""),
              _severance(source=Source.SIMKL, id="1234",
                         ids={"simkl": 1234, "tmdb": 95396}, title="Severance (2022)"))
        html = self.typed("sever")
        self.assertEqual(len(rows(html)), 1)
        self.assertIn("trakt-circlemark-dark.svg", html)
        self.assertIn("simkllogo.svg", html)

    def test_under_two_letters_draws_nothing_yet(self):
        store(_severance())
        html = self.typed("s")
        self.assertEqual(rows(html), [])
        self.assertNotIn("No matches", html)
        self.assertNotIn("already knows", html)

    def test_nothing_stored_says_enter_asks_the_services(self):
        html = self.typed("nothing like it")
        self.assertEqual(rows(html), [])
        self.assertIn("Press Enter to search the services", html)

    def test_the_film_box_lists_stored_films_and_its_row_is_the_add(self):
        store(_heat(), endpoint="movies")
        store(_severance())
        row, = rows(self.typed("heat", "/distrakt/fragments/search-movie-stored"))
        self.assertEqual(row["title"], "Heat")
        self.assertEqual(row["body"], {"t": HEAT_KEY})
        self.assertEqual(rows(self.typed("sever", "/distrakt/fragments/search-movie-stored")), [])

    def test_the_modals_type_into_the_stored_list_and_submit_to_the_services(self):
        page = self.client.get("/distrakt?year=%d&month=%d" % self.current).text
        for box, stored_route, live_route in (
                ("addSearchInput", "/distrakt/fragments/search-stored",
                 "/distrakt/fragments/search?"),
                ("addMovieSearch", "/distrakt/fragments/search-movie-stored",
                 "/distrakt/fragments/search-movie?")):
            with self.subTest(box=box):
                tag = re.search(rf'<input[^>]*id="{box}"[^>]*>', page, re.S).group(0)
                self.assertIn(f'hx-get="{stored_route}', tag)
                # NOT DEBOUNCED and QUEUED, not replaced: both measured in the
                # browser as what keeps Enter's answer the one left on screen —
                # see _distrakt_add_show.html. A server test cannot run htmx, so
                # this pins the attributes the measurement was made with.
                self.assertIn('hx-trigger="input changed"', tag)
                self.assertIn(':queue last"', tag)
                form = re.search(rf'<form[^>]*hx-get="{re.escape(live_route)}[^>]*>',
                                 page, re.S).group(0)
                self.assertIn(':queue last"', form)
                # The shared box's clear button, right after it (see
                # _title_search.html): a reset that asks the typed list empty.
                clear = re.search(rf'id="{box}"[^>]*>\s*(<button[^>]*>)', page, re.S).group(1)
                self.assertIn('type="reset"', clear)
                self.assertIn(f'hx-get="{stored_route}', clear)
                self.assertIn(':queue last"', clear)
        # And an empty query is the resting state: nothing listed, nothing said.
        self.assertEqual(rows(self.typed("")), [])


class TypedPickTests(AddFlowTestCase):
    """A typed row is added from the stored rows it names — the add guarantee
    for the half of the search that no service answered just now."""

    def pick(self, title_key, month=None):
        year, month_no = month or self.current
        with self.no_calendar:
            return self.client.get(f"/distrakt/fragments/seasons?year={year}"
                                   f"&month={month_no}&t={title_key}").text

    def test_the_picker_reads_the_stored_title_and_draws_its_seasons(self):
        store(_severance())
        self.warm((SEASONS_URL, SEASONS))
        html = self.pick(SEVERANCE_KEY)
        self.assertIn("Pick seasons of Severance", html)
        self.assertIn('name="season:1"', html)
        self.assertIn("9 eps", html)
        # The season button names the same stored identity and the season.
        self.assertIn(f'name="t" value="{SEVERANCE_KEY}"', html)
        self.assertNotIn('"q"', html)

    def test_an_identity_nothing_stores_is_refused_rather_than_built(self):
        self.assertIn("That title is no longer stored", self.pick("show:tmdb:1"))

    def test_adding_files_the_stored_title_and_never_the_pages(self):
        store(_severance())
        self.warm((SEASONS_URL, SEASONS))
        resp = self.add("/api/distrakt/add", {
            "t": SEVERANCE_KEY, "season": 1,
            "title": "Something Else", "network": "Nowhere", "ids": {"tmdb": 1}})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row, = self.listed()
        self.assertEqual(row["title"], "Severance")
        self.assertEqual(row["network"], "Apple TV+")
        self.assertEqual(row["match_id"], "95396")

    def test_adding_an_identity_nothing_stores_is_refused(self):
        resp = self.add("/api/distrakt/add", {"t": "show:tmdb:1", "season": 1})
        self.assertIn("That title is no longer stored", resp.text)
        self.assertEqual(self.listed(), [])

    def test_filling_in_a_past_month_from_a_typed_row(self):
        store(_severance())
        self.warm((SEASONS_URL, SEASONS))
        resp = self.add("/api/distrakt/add-completed", {"t": SEVERANCE_KEY, "season": 1},
                        month=self.past)
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        doc = asyncio.run(distrakt_store.load_month(
            self.user_id, distrakt_store.month_key(*self.past)))
        row, = doc["shows"]
        self.assertEqual(row["title"], "Severance")

    def test_a_film_is_recorded_from_its_stored_row(self):
        store(_heat(), endpoint="movies")
        resp = self.add("/api/distrakt/add-movie", {
            "t": HEAT_KEY, "watched_on": "2026-01-05", "title": "Not Heat", "year": 2099})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        watch, = asyncio.run(watch_history.load_state(self.user_id))["movies"].values()
        self.assertEqual((watch["title"], watch["year"]), ("Heat", 1995))

    def test_a_stored_show_key_is_not_a_film(self):
        """The film add reads films: a show's identity names nothing there."""
        store(_severance())
        resp = self.add("/api/distrakt/add-movie",
                        {"t": SEVERANCE_KEY, "watched_on": "2026-01-05"})
        self.assertIn("That title is no longer stored", resp.text)


class OnlyTheOtherServiceStoredItTests(AddFlowTestCase):
    """A title only Simkl's calendar stored, for a viewer who linked Trakt and
    not Simkl. Their Enter never asks Simkl — its search takes the viewer's own
    token — but DESCRIBING a title is a public per-title read, so the pick still
    gets its season list from the service that stored it."""

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_client_id="scid", simkl_public_calendar_enabled=False)

    def test_its_season_picker_asks_the_service_that_stored_it(self):
        store(_severance(source=Source.SIMKL, id="2002", title="Frieren",
                         ids={"simkl": 2002, "tmdb": 209867}, network=""))
        answer = SeasonsAnswer(seasons=[{"season": 1, "episode_count": 28}],
                               named_season=None, ids={"mal": "52991"}, network="Nippon TV")
        seasons = AsyncMock(return_value=answer)
        with patch.object(simkl_detail, "fetch_seasons", seasons), self.no_calendar:
            year, month = self.current
            html = self.client.get(f"/distrakt/fragments/seasons?year={year}"
                                   f"&month={month}&t=show:tmdb:209867").text
        self.assertIn('name="season:1"', html)
        self.assertIn("28 eps", html)
        self.assertEqual(seasons.await_args.args[1], "2002")


class EnterAsksTheViewersServicesTests(AppTestCase):
    """Submitting asks TMDB, with the instance's read token, for everybody — and
    a service the viewer has linked only when the request ADDS it (`also`), with
    that viewer's own token. Nothing is searched in TMDB's place."""

    TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("enter", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 77, "tok")
        self.sign_in_as(self.user_id)

    def _asked(self, *, tmdb: bool = True, simkl_linked: bool = False, also: str = ""):
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                               simkl_client_id="scid",
                               tmdb_api_key=self.TMDB_TOKEN if tmdb else ""))
        if simkl_linked:
            self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        mocks = {"tmdb": AsyncMock(return_value=SearchPage([])),
                 "trakt": AsyncMock(return_value=[]), "simkl": AsyncMock(return_value=[])}
        with patch.object(tmdb_search, "search_titles", mocks["tmdb"]),              patch.object(trakt_detail, "search_titles", mocks["trakt"]),              patch.object(simkl_search, "search_titles", mocks["simkl"]):
            html = self.client.get(f"/distrakt/fragments/search?q=frieren&also={also}").text
        return {name for name, mock in mocks.items() if mock.await_count}, html

    def test_tmdb_alone_by_default(self):
        asked, html = self._asked(simkl_linked=True)
        self.assertEqual(asked, {"tmdb"})
        self.assertIn("Also search Trakt", html)
        self.assertIn("Also search Simkl", html)

    def test_a_linked_service_joins_when_added(self):
        asked, _ = self._asked(simkl_linked=True, also="trakt,simkl")
        self.assertEqual(asked, {"tmdb", "trakt", "simkl"})

    def test_a_service_the_viewer_never_linked_is_neither_offered_nor_asked(self):
        asked, html = self._asked(also="simkl")
        self.assertEqual(asked, set())
        self.assertNotIn("Also search Simkl", html)

    def test_no_tmdb_token_asks_nobody_and_says_so(self):
        asked, html = self._asked(tmdb=False)
        self.assertEqual(asked, set())
        self.assertIn("is not configured", html)
        self.assertIn("Also search Trakt", html)
