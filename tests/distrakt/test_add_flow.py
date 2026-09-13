"""Adding by hand NAMES a search result; it does not hand a title over.

WHAT CHANGED AND WHY IT MATTERS MORE HERE THAN ALMOST ANYWHERE. The add routes
used to take `ids`, `title` and `network` straight from the page — the last
reachable way for a browser to decide what identity somebody's watch record is
filed under. §7 records what that costs when it goes wrong: `detail_source`
prefers Trakt wherever a record has a Trakt id, so a WRONG Trakt id permanently
SHADOWS a correct Simkl one and the row can never repair itself.

WHAT THE PAGE SENDS NOW is the query it searched for and the result's place in
that answer. The route re-reads the very search the box made — from the shared
response cache, never asking a service — and the very season lookup the picker
made, and takes the title, the network and every id from those. So what gets
stored is the service's own answer.

AN ANSWER THE STORE NO LONGER HOLDS IS REFUSED rather than fetched. Re-asking
would turn an add into a search that nobody's budget ever counts, which is the
rule the rankings board's own add already keeps (ranker/sources.title_from_search).

No network: the cached answers are written straight into the shared response
cache under the transports' own keys, so the routes read them the way a real
warm cache would and the guard in tests/conftest.py fails anything that reaches
past it.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app import cache, distrakt as distrakt_store
from app.distrakt import watch_history
from app.config import Settings
from app.providers.base import ItemKey, Media
from tests.support import AppTestCase, ORIGIN

# The exact URLs the two Trakt reads store their answers under. Written out
# rather than built from the transport's own helper on purpose: what is being
# pinned is that the route re-reads THE SAME REQUEST the page's search made, and
# a test that derived the key the same way the code does could not tell a
# changed key from a matching one.
SEARCH_URL = "https://api.trakt.tv/search/show?query=severance&extended=full"
MOVIE_SEARCH_URL = "https://api.trakt.tv/search/movie?query=heat&extended=full"
SEASONS_URL = "https://api.trakt.tv/shows/95396/seasons?extended=full"

SHOW_HIT = {"show": {"title": "Severance", "year": 2022, "network": "Apple TV+",
                     "ids": {"trakt": 95396, "tmdb": 95396, "slug": "severance"}}}
MOVIE_HIT = {"movie": {"title": "Heat", "year": 1995,
                       "ids": {"trakt": 6, "tmdb": 949, "slug": "heat-1995"}}}
SEASONS = [{"number": 1, "episode_count": 9, "first_aired": "2022-02-18T00:00:00.000Z"}]

# The same question asked of the other catalogue. Its search is PAGED and its key
# sorts its parameters, so the URL is a different shape rather than the same one
# with a different host — which is the reason a test cannot build either of these
# from one helper and still be checking what it means to check.
SIMKL_MOVIE_SEARCH_URL = "https://api.simkl.com/search/movie?limit=10&q=heat"
SIMKL_MOVIE_HIT = {"title": "Heat", "year": 1995,
                   "ids": {"simkl_id": 4242, "tmdb": 949, "slug": "heat-1995"}}


class AddFlowTestCase(AppTestCase):
    def make_settings(self):
        # A catalogue to search, and no calendar source: these are about the add
        # and a month with premieres to import would go and read one.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_public_calendar_enabled=False)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("adder", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "tok")
        self.sign_in_as(self.user_id)
        today = date.today()
        self.current = (today.year, today.month)
        self.past = (today.year - 1, 1)
        # Every read the recomputed month makes afterwards. The add's own answer
        # is the month drawn, and drawing it is not what these are about.
        self.quiet = patch.multiple(
            "app.distrakt.watch_history",
            tracker_ports=AsyncMock(return_value=[]),
            baseline_show=AsyncMock(return_value=None),
        )
        self.no_calendar = patch("app.calendar.cache.read_month",
                                 new=AsyncMock(return_value=([], None)))
        self.no_lookup = patch(
            "app.distrakt.live.season_detail",
            new=AsyncMock(return_value={"total": 9, "cadence": "Fri",
                                        "premiere": "2/18", "finale": "4/8",
                                        "started_airing": True,
                                        "finished_airing": True}))

    def warm(self, *pairs):
        """Answers already in the shared response cache, as a warm one holds
        them — which is what a search made moments ago leaves behind."""
        for url, value in pairs:
            asyncio.run(cache.set(url, value))

    def add(self, path, body, month=None):
        year, month_no = month or self.current
        with self.quiet, self.no_calendar, self.no_lookup:
            return self.client.post(f"{path}?year={year}&month={month_no}", json=body)

    def listed(self):
        """What the add actually wrote.

        ASSERTED AGAINST THE STORE RATHER THAN THE ANSWER, and deliberately: the
        answer is the whole month redrawn, and with every history port stubbed
        away that recompute has nothing to read and says so. What the add WROTE
        and what a month can be DRAWN from are two questions, and only the first
        is this file's.
        """
        return asyncio.run(distrakt_store.user_records(self.user_id))


class AddingAShowTests(AddFlowTestCase):
    def test_it_stores_the_services_own_title_ids_and_network(self):
        self.warm((SEARCH_URL, [SHOW_HIT]), (SEASONS_URL, SEASONS))
        resp = self.add("/api/distrakt/add", {"q": "severance", "i": 0, "season": 1})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row, = self.listed()
        self.assertEqual(row["title"], "Severance")
        self.assertEqual(row["network"], "Apple TV+")
        self.assertEqual(row["ids"]["tmdb"], 95396)

    def test_a_title_the_page_sends_is_ignored_outright(self):
        """Not merely overridden where it is wrong: never read at all. A field
        the route does not look at cannot be got past by spelling it
        differently."""
        self.warm((SEARCH_URL, [SHOW_HIT]), (SEASONS_URL, SEASONS))
        self.add("/api/distrakt/add", {
            "q": "severance", "i": 0, "season": 1,
            "title": "Something Else", "network": "Nowhere",
            "ids": {"trakt": 1, "tmdb": 1}})
        row, = self.listed()
        self.assertEqual(row["title"], "Severance")
        self.assertEqual(row["network"], "Apple TV+")
        self.assertEqual(row["match_id"], "95396")

    def test_a_result_whose_search_the_store_no_longer_holds_is_refused(self):
        """Re-asking would make an add a way to search that no budget counts, so
        the honest answer is to send them back to the box."""
        self.warm((SEASONS_URL, SEASONS))
        resp = self.add("/api/distrakt/add", {"q": "severance", "i": 0, "season": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("That search has expired", resp.text)
        self.assertEqual(self.listed(), [])

    def test_a_bare_hit_whose_season_lookup_is_gone_is_refused_as_unfileable(self):
        """Both halves of the pick are re-read, and a BARE hit needs both: a
        search result with no shared id at all is under-described rather than
        unfileable, and the per-title lookup is what resolves one. With that
        answer gone there is no id to file it under, and the refusal says so in
        the same sentence the picker uses.

        A hit that already had a shared id is a different case and adds fine —
        the lookup fills a gap rather than being the only source of an id."""
        bare = {"show": {"title": "Bare", "year": 2022,
                         "ids": {"trakt": 95396, "slug": "bare"}}}
        self.warm((SEARCH_URL, [bare]))
        resp = self.add("/api/distrakt/add", {"q": "severance", "i": 0, "season": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("shared ids the tracker files records under", resp.text)
        self.assertEqual(self.listed(), [])

    def test_a_place_past_the_end_of_the_answer_is_refused(self):
        self.warm((SEARCH_URL, [SHOW_HIT]), (SEASONS_URL, SEASONS))
        resp = self.add("/api/distrakt/add", {"q": "severance", "i": 4, "season": 1})
        self.assertIn("That search has expired", resp.text)
        self.assertEqual(self.listed(), [])

    def test_a_missing_season_is_refused_before_anything_is_read(self):
        self.warm((SEARCH_URL, [SHOW_HIT]), (SEASONS_URL, SEASONS))
        resp = self.add("/api/distrakt/add", {"q": "severance", "i": 0})
        self.assertIn("Missing or invalid season", resp.text)
        self.assertEqual(self.listed(), [])


class FillingInAPastMonthTests(AddFlowTestCase):
    def test_it_records_the_services_own_title_as_finished(self):
        self.warm((SEARCH_URL, [SHOW_HIT]), (SEASONS_URL, SEASONS))
        resp = self.add("/api/distrakt/add-completed",
                        {"q": "severance", "i": 0, "season": 1}, month=self.past)
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        key = distrakt_store.month_key(*self.past)
        doc = asyncio.run(distrakt_store.load_month(self.user_id, key))
        row, = doc["shows"]
        self.assertEqual(row["title"], "Severance")
        self.assertEqual(row["kind"], distrakt_store.RecordKind.COMPLETED)

    def test_a_result_no_longer_cached_is_refused_here_too(self):
        resp = self.add("/api/distrakt/add-completed",
                        {"q": "severance", "i": 0, "season": 1}, month=self.past)
        self.assertIn("That search has expired", resp.text)
        self.assertIsNone(asyncio.run(distrakt_store.load_month(
            self.user_id, distrakt_store.month_key(*self.past))))


class AddingAFilmTests(AddFlowTestCase):
    """A film has no season to pick, so the row IS the add — and the whole hit
    comes back out of the one cached search."""

    def _watches(self):
        return asyncio.run(watch_history.load_state(self.user_id))["movies"]

    def test_it_stores_the_services_own_title_and_year(self):
        self.warm((MOVIE_SEARCH_URL, [MOVIE_HIT]))
        resp = self.add("/api/distrakt/add-movie",
                        {"q": "heat", "i": 0, "watched_on": "2026-01-05"})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        watch, = self._watches().values()
        self.assertEqual((watch["title"], watch["year"]), ("Heat", 1995))

    def test_a_title_the_page_sends_is_ignored(self):
        self.warm((MOVIE_SEARCH_URL, [MOVIE_HIT]))
        self.add("/api/distrakt/add-movie",
                 {"q": "heat", "i": 0, "watched_on": "2026-01-05",
                  "title": "Not Heat", "year": 2099, "ids": {"tmdb": 1}})
        watch, = self._watches().values()
        self.assertEqual((watch["title"], watch["year"]), ("Heat", 1995))

    def test_a_result_no_longer_cached_is_refused(self):
        resp = self.add("/api/distrakt/add-movie",
                        {"q": "heat", "i": 0, "watched_on": "2026-01-05"})
        self.assertIn("That search has expired", resp.text)
        self.assertEqual(self._watches(), {})


class AddingWhatOnlyTheOtherServiceListsTests(AddFlowTestCase):
    """A hit no Trakt search returned still adds.

    THE WHOLE POINT OF SEARCHING TWO CATALOGUES is the titles only one of them
    carries, and the add re-reads both searches out of the store — so a source
    that cannot answer from the store takes its hits out of the merge, and the
    place the page named then points at a different row or at nothing. That is
    indistinguishable, from here, from the service being down, and the viewer is
    told their search expired seconds after making it.

    ITS SEARCH IS PAGED AND THE OTHER'S IS NOT, which is why this needs a test of
    its own rather than being the Trakt case with a different constant: the two
    cached reads are two implementations of one rule (see
    tests/providers/test_protocol_conformance.py), and only one of them was on
    the path these tests already walked.
    """

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_client_id="scid",
                        simkl_public_calendar_enabled=False)

    def test_a_film_only_the_second_catalogue_lists_is_added_from_its_own_answer(self):
        self.warm(
            # Trakt answers, and answers nothing — the ordinary shape of a title
            # one catalogue carries and the other does not.
            (MOVIE_SEARCH_URL, []),
            (SIMKL_MOVIE_SEARCH_URL, [SIMKL_MOVIE_HIT]),
        )
        resp = self.add("/api/distrakt/add-movie",
                        {"q": "heat", "i": 0, "watched_on": "2026-01-05"})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        watches = asyncio.run(watch_history.load_state(self.user_id))["movies"]
        self.assertEqual(list(watches), ["movie:tmdb:949"])
        watch, = watches.values()
        self.assertEqual((watch["title"], watch["year"]), ("Heat", 1995))
        # THE PROOF OF WHOSE ANSWER THIS IS. A row is filed under the shared id
        # both services would have named, and keeps the CALL id beside it — so
        # the second catalogue's own id being the one stored is what says the
        # row came out of its stored answer rather than out of a title the page
        # sent.
        self.assertEqual(watch["ids"], {"simkl": 4242})


class TheSeasonPickerNamesTheResultTooTests(AddFlowTestCase):
    """What the row a viewer clicks actually asks for."""

    def test_the_picker_reads_the_same_search_and_draws_the_seasons(self):
        self.warm((SEARCH_URL, [SHOW_HIT]), (SEASONS_URL, SEASONS))
        year, month = self.current
        with self.no_calendar:
            html = self.client.get(
                f"/distrakt/fragments/seasons?year={year}&month={month}"
                f"&q=severance&i=0").text
        self.assertIn("Pick a season for <span>Severance</span>", html)
        self.assertIn("S01 (9 eps)", html)

    def test_the_search_rows_own_request_names_the_query_and_the_place(self):
        self.warm((SEARCH_URL, [SHOW_HIT]))
        year, month = self.current
        with self.no_calendar:
            html = self.client.get(
                f"/distrakt/fragments/search?year={year}&month={month}&q=severance").text
        self.assertIn(f"/distrakt/fragments/seasons?year={year}&amp;month={month}"
                      "&amp;q=severance&amp;i=0", html)
        # Nothing about the title travels: not its ids, not its network.
        self.assertNotIn("95396", html.split("<span class=\"distrakt-title\">")[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
