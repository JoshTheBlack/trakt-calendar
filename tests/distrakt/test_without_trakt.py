"""The tracker for somebody who has no Trakt token, and for a Trakt that is down.

TWO SEPARATE FACTS ARE PINNED HERE and they arrived together, from one account
that had signed in with Simkl and never linked Trakt:

  - A ROSTER ROW'S MODAL STILL OPENS. Everything it shows is either public
    catalogue data — overview, cast, the season's episode list, cached once for
    the whole instance — or this account's own watched episodes, read out of
    distrakt_show_progress. Neither half is a Trakt-authenticated read, so
    neither may be gated on whether THIS viewer linked Trakt.

  - WHEN A SERVICE CANNOT BE READ, THE PAGE SAYS WHICH ONE. A season's episode
    count is asked of whichever service the record carries an id for (see
    live.detail_source), so it can fail on its own, independently of the history
    sync. Both kinds of silence reach the same banner, because to a reader they
    are the same sentence.

No network: the transport's pooled client is replaced with a recording stub, and
the sync entry points are patched at their module objects.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app import db, distrakt as distrakt_store
from app.config import Settings, save_settings
from app.distrakt import live
from app.providers.base import PlayCounts
from app.providers.trakt import TraktError
from app.providers.trakt import transport
from tests.support import AppTestCase, ORIGIN, new_db_path


class _RecordingClient:
    """An httpx.AsyncClient stand-in that answers every catalogue path and keeps
    the headers it was handed, so a test can assert what would have gone out."""

    def __init__(self, bodies: dict):
        self._bodies = bodies
        self.sent_headers: list[dict] = []

    async def get(self, url, headers=None, timeout=None):
        self.sent_headers.append(dict(headers or {}))
        path = url.split("?", 1)[0].split("api.trakt.tv/", 1)[-1]
        return _Response(self._bodies.get(path, {}))


class _Response:
    status_code = 200
    text = ""
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


SHOW = {"title": "Silo", "year": 2026, "overview": "Down the silo.",
        "status": "returning series", "network": "Apple TV", "runtime": 50,
        "genres": ["drama"], "rating": 8.24, "certification": "tv-ma"}
PEOPLE = {"cast": [{"person": {"name": "Rebecca"}, "character": "Juliette"}]}
EPISODES = [{"number": n, "title": f"Ep {n}", "first_aired": "2026-07-15T20:00:00.000Z"}
            for n in range(1, 6)]


def _quiet_sources():
    """Every outbound call a month build can make, stubbed at its own module.

    The calendar supplies a month's premieres and the services supply the
    history. Neither is what the tests using this are about — they are about
    whether the month is built and kept at all — and the suite refuses a test
    that reaches the network. The history is stubbed at the TRACKER's own
    boundary rather than at each provider call, so it stays true whatever the
    ports go on to do.
    """
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("app.calendar.cache.read_month", return_value=([], None)))
    stack.enter_context(patch("app.distrakt.watch_history.tracker_ports",
                              AsyncMock(return_value=[])))
    return stack


class DetailsWithoutATraktTokenTests(AppTestCase):
    """GET /api/distrakt/details for an account that signed in with Simkl.

    The row it asks about carries a Trakt id — a title Simkl alone knows is
    Phase-8 work and is deliberately still a 404 here. What this covers is the
    other case, which was ALSO failing: a title Trakt knows perfectly well,
    asked for by somebody who has no Trakt token, which is not a credential
    either half of the answer needed.
    """

    KEY = "show:tmdb:1"
    WATCHED = '{"1": "2026-07-01T00:00:00Z", "2": "2026-07-02T00:00:00Z"}'

    def make_settings(self):
        # A client id and NO access token: the instance can read the catalogue,
        # and nobody's private Trakt data is reachable. That is exactly the
        # state an operator is in before anyone links Trakt.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("simkl_only", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "ids": {"trakt": 7, "tmdb": 1, "slug": "silo"}, "season": 3,
            "title": "Silo", "network": "Apple TV", "media": "show",
            "kind": distrakt_store.RecordKind.KEEPUP,
        }))
        asyncio.run(db.execute(
            "INSERT OR REPLACE INTO distrakt_show_progress "
            "(user_id, media, match_source, match_id, season, source, "
            "watched_episodes_json, trakt_id) VALUES (?,?,?,?,?,?,?,?)",
            (self.user_id, "show", "tmdb", "1", 3, "simkl", self.WATCHED, 7)))
        self.sign_in_as(self.user_id)

    def _details(self):
        self.client_double = _RecordingClient({
            "shows/7": SHOW, "shows/7/people": PEOPLE, "shows/7/seasons/3": EPISODES,
        })
        with patch.object(transport, "shared_client", return_value=self.client_double):
            return self.client.get(f"/api/distrakt/details?key={self.KEY}&season=3").json()

    def test_the_modal_opens_at_all(self):
        """It used to 400 "Not configured" for this whole configuration, which is
        what the viewer saw as "Could not load details from Trakt" on every row."""
        self.assertTrue(self._details()["ok"])

    def test_the_public_catalogue_fields_are_all_there(self):
        body = self._details()
        self.assertEqual(body["title"], "Silo")
        self.assertEqual(body["overview"], "Down the silo.")
        self.assertEqual([c["character"] for c in body["cast"]], ["Juliette"])
        self.assertEqual([e["number"] for e in body["episodes"]], [1, 2, 3, 4, 5])

    def test_this_accounts_own_watched_episodes_come_back_with_them(self):
        """From local storage, where whichever service this account DOES sync
        wrote them — the modal is the one place the public half and the private
        half are shown side by side."""
        self.assertEqual(self._details()["watched_episodes"], [1, 2])

    def test_nothing_it_asked_trakt_carried_an_authorization_header(self):
        """The proof that the answer really was tokenless rather than quietly
        borrowing the instance's credential."""
        self._details()
        self.assertTrue(self.client_double.sent_headers)
        for headers in self.client_double.sent_headers:
            self.assertNotIn("Authorization", headers)
            self.assertEqual(headers["trakt-api-key"], "cid")

    def test_a_row_no_configured_source_can_describe_says_so(self):
        """A Simkl-only row on an instance whose Simkl credentials are not filled
        in. Nobody can answer, and the refusal names no service — the operator's
        configuration is not a modal's business, and blaming Trakt for a title
        Trakt never listed would be a lie the reader cannot act on."""
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "ids": {"simkl": 99, "tmdb": 2}, "season": 1, "title": "Simkl Only",
            "media": "show", "kind": distrakt_store.RecordKind.KEEPUP,
        }))
        with patch.object(transport, "shared_client",
                          return_value=_RecordingClient({})):
            resp = self.client.get("/api/distrakt/details?key=show:tmdb:2&season=1")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "Nothing here can describe this item.")

    def test_a_row_that_is_not_on_the_roster_at_all_still_says_that(self):
        """The other refusal, kept apart from the one above: these are different
        facts and only one of them is something the reader can do anything
        about."""
        resp = self.client.get("/api/distrakt/details?key=show:tmdb:404404&season=1")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "Not on your roster")


class _CatalogueFailureTestCase(unittest.IsolatedAsyncioTestCase):
    """A roster whose season lookups all fail, with the history sync fine."""

    RECORD = {"media": "show", "match_source": "tmdb", "match_id": "1", "season": 1,
              "title": "Silo", "ids": {"trakt": 7, "tmdb": 1}, "watched": 2, "total": 8}

    async def asyncSetUp(self):
        new_db_path("without-trakt")
        await db.migrate()
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
            "created_at, updated_at) VALUES ('viewer', 1, 1, 1, ?, ?)", (now, now))
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()


class TheBannerNamesTheServiceTests(_CatalogueFailureTestCase):
    """The second half of the browser report: the rows degraded correctly, and
    nothing on the page said which service was missing from them."""

    async def _rows(self, *, source_id_key="trakt"):
        record = dict(self.RECORD, ids={source_id_key: 7, "tmdb": 1})

        async def _boom(*args, **kwargs):
            raise TraktError("Could not reach Trakt")

        with patch("app.providers.trakt.detail.fetch_season_detail", _boom), \
             patch("app.providers.simkl.detail.fetch_season_detail", _boom):
            return await live.compute_live_shows(
                self.user_id, [record], None, watched_lookup={},
                allow_degrade=True, sources_read=("trakt",))

    async def test_a_failed_row_names_the_service_that_could_not_answer(self):
        row, = await self._rows()
        self.assertTrue(row["unavailable"])
        self.assertEqual(row["unavailable_source"], "trakt")

    async def test_the_page_level_answer_is_that_one_service_was_quiet(self):
        rows = await self._rows()
        self.assertEqual(live.unreadable_detail_sources(rows), ["trakt"])

    async def test_it_names_whichever_service_the_record_was_asked_of(self):
        """The catalogue lookup follows the id the RECORD carries, not the
        account's linked services, so the banner has to follow it too."""
        rows = await self._rows(source_id_key="simkl")
        self.assertEqual(live.unreadable_detail_sources(rows), ["simkl"])

    async def test_a_row_that_answered_names_nobody(self):
        async def _season(settings, trakt_id, season, fresh=False, client=None):
            return {"total": 8, "cadence": "Tue", "premiere": "7/1", "finale": None,
                    "started_airing": True, "finished_airing": False}

        with patch("app.providers.trakt.detail.fetch_season_detail", _season):
            rows = await live.compute_live_shows(
                self.user_id, [dict(self.RECORD)], None, watched_lookup={},
                allow_degrade=True, sources_read=("trakt",))
        self.assertEqual(live.unreadable_detail_sources(rows), [])


class TheMonthPayloadCarriesTheBannerTests(AppTestCase):
    """End to end, because the two silences are gathered in the route and a unit
    test of either half would not have caught the missing join."""

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "user-token")
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "ids": {"trakt": 7, "tmdb": 1, "slug": "silo"}, "season": 3,
            "title": "Silo", "network": "Apple TV", "media": "show",
            "kind": distrakt_store.RecordKind.KEEPUP,
        }))
        self.sign_in_as(self.user_id)

    def test_a_catalogue_outage_reaches_sources_unreadable(self):
        today = date.today()

        async def _boom(*args, **kwargs):
            raise TraktError("Could not reach Trakt")

        beacon = {"episodes": {"watched_at": "T1", "removed_at": None},
                  "movies": {"watched_at": "T1", "removed_at": None}}
        with patch("app.calendar.cache.read_month", new=AsyncMock(return_value=([], None))), \
             patch("app.providers.trakt.sync.fetch_last_activities",
                   new=AsyncMock(return_value=beacon)), \
             patch("app.providers.trakt.sync.fetch_history",
                   new=AsyncMock(return_value=[])), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   new=AsyncMock(return_value={})),              patch("app.providers.trakt.sync.fetch_play_counts",
                   new=AsyncMock(return_value=PlayCounts({}, False))), \
             patch("app.providers.trakt.detail.fetch_season_detail", _boom):
            body = self.client.get(
                f"/api/distrakt/month?year={today.year}&month={today.month}").json()

        self.assertTrue(body["ok"])
        # The page still renders — degrading is not failing — and it now says who
        # was quiet instead of only flagging every row unavailable.
        self.assertEqual(body["sources_unreadable"], ["Trakt"])

    def test_a_month_that_read_cleanly_says_nothing(self):
        async def _season(settings, trakt_id, season, fresh=False, client=None):
            return {"total": 8, "cadence": "Tue", "premiere": "7/1", "finale": None,
                    "started_airing": True, "finished_airing": False}

        today = date.today()
        beacon = {"episodes": {"watched_at": "T1", "removed_at": None},
                  "movies": {"watched_at": "T1", "removed_at": None}}
        with patch("app.calendar.cache.read_month", new=AsyncMock(return_value=([], None))), \
             patch("app.providers.trakt.sync.fetch_last_activities",
                   new=AsyncMock(return_value=beacon)), \
             patch("app.providers.trakt.sync.fetch_history",
                   new=AsyncMock(return_value=[])), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   new=AsyncMock(return_value={})),              patch("app.providers.trakt.sync.fetch_play_counts",
                   new=AsyncMock(return_value=PlayCounts({}, False))), \
             patch("app.providers.trakt.detail.fetch_season_detail", _season):
            body = self.client.get(
                f"/api/distrakt/month?year={today.year}&month={today.month}").json()

        self.assertEqual(body["sources_unreadable"], [])


class SimklOnlyAccountReachesItsOwnTrackerTests(AppTestCase):
    """The five actions an account signed in with Simkl alone could not take.

    THE FAULT AND WHY IT LOOKED LIKE A CONFIGURATION PROBLEM.
    `_distrakt_settings` swaps every source's credential for THIS account's own,
    which is what makes the tracker read the viewer's history rather than the
    operator's. `settings.trakt_configured` therefore stops meaning "this
    instance has Trakt set up" and starts meaning "this VIEWER linked Trakt" —
    so five routes that spend no Trakt credential at all refused the whole
    action, and said "Not configured" about an instance that was configured
    fine. Reported from a real account: import from calendar and add a show both
    refused.

    EACH ONE NOW ASKS WHAT IT ACTUALLY NEEDS. Importing needs a calendar to
    import from; looking a season up needs the instance's client id, which is
    what /search and /seasons already ask for; surveying a backfill needs some
    service that can be asked for a history, which is the question the month
    list already asks. The repair is the one already made to the month list in
    this same file, applied to the routes it was not applied to.

    HOW THESE ARE WRITTEN, AND WHY THEY TOUCH NO NETWORK: each body is chosen to
    fail the check immediately AFTER the gate. Getting that second refusal is
    proof the gate let the request through, and it costs no lookup, no history
    sweep and no month build — so what is pinned here is the gate itself rather
    than the whole action behind it.
    """

    def make_settings(self):
        # An instance whose operator set BOTH services up, which is the shape
        # this whole build is for. What the viewer has linked is a separate
        # fact, and the one every test below turns on.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        trakt_access_token="operator-token",
                        simkl_client_id="scid", simkl_client_secret="ssecret",
                        simkl_access_token="operator-simkl-token")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("simkl_only_actor", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)

    def refusal(self, resp) -> str:
        return (resp.json() or {}).get("error", "")

    def test_import_from_calendar_is_not_refused_for_a_missing_trakt_token(self):
        """It reads the month's premieres out of the instance's own calendar
        cache and this account's marks. No viewer's credential is spent."""
        with _quiet_sources():
            resp = self.client.post("/api/distrakt/import", json={"year": 2020, "month": 1})
        self.assertNotEqual(self.refusal(resp), "Not configured")
        # A month with nothing cached for it imports nothing and says so
        # politely, which is the ordinary answer and not a refusal.
        self.assertEqual(resp.status_code, 200, resp.text[:200])

    def test_adding_a_show_by_hand_is_not_refused(self):
        resp = self.client.post("/api/distrakt/add", json={"ids": {}, "season": "x"})
        self.assertNotEqual(self.refusal(resp), "Not configured")

    def test_filling_in_a_past_month_by_hand_is_not_refused(self):
        resp = self.client.post("/api/distrakt/add-completed",
                                json={"year": 2020, "month": 1, "ids": {}})
        self.assertNotEqual(self.refusal(resp), "Not configured")
        self.assertIn("season", self.refusal(resp).lower())

    def test_surveying_a_backfill_is_not_refused(self):
        """This one genuinely reads a history — but from whichever service can be
        asked, and this account has one."""
        resp = self.client.post("/api/distrakt/backfill/survey",
                                json={"start": "2026-7", "end": "2026-08"})
        self.assertNotEqual(self.refusal(resp), "Not configured")
        self.assertIn("YYYY-MM", self.refusal(resp))

    def test_saying_yes_to_an_unknown_episode_is_not_refused(self):
        resp = self.client.post("/api/distrakt/unknown-add", json={})
        self.assertNotEqual(self.refusal(resp), "Not configured")

    def test_an_account_with_nothing_to_ask_is_still_refused_a_backfill(self):
        """The other half of the survey's gate: it is not that nothing is
        checked now, it is that the right thing is. An account whose only linked
        service holds no usable token has no history to sweep."""
        empty = self.make_user("no_tokens", distrakt_approved=True, calendar_approved=True)
        self.link_identity(empty, "simkl", 4343, "")
        self.sign_in_as(empty)
        resp = self.client.post("/api/distrakt/backfill/survey",
                                json={"start": "2026-07", "end": "2026-08"})
        self.assertEqual(self.refusal(resp), "Not configured")


class ASimklOnlyRosterRowOpensItsModalTests(AppTestCase):
    """The tracker modal on a row Trakt has never heard of.

    THE ROSTER HAS ALWAYS BEEN ABLE TO HOLD ONE. It keys on the shared identity
    waterfall — tmdb, tvdb, imdb, mal — and never on a Trakt id, so a season
    baselined out of a Simkl library read is filed perfectly well with none:
    Simkl's id map carries `traktslug` but no numeric `trakt`, and `collect_ids`
    drops the slug. What could not happen was describing one. The modal asked
    Trakt, found no Trakt id, and told the viewer the row was "not on your
    roster" — about a row the page had just drawn.

    IT IS THE CALENDAR'S OWN REPAIR, one page over: ask whichever service the row
    carries an id for, through the same chooser, so the two modals cannot come to
    different answers about who can describe a title.
    """

    SIMKL_DETAIL = {
        "title": "Simkl Only", "overview": "A show only one service lists.",
        "status": "airing", "network": "SimklVision", "runtime": 24,
        "genres": ["Drama"], "certification": "TV-14", "cast": [],
        "episodes": [{"number": n, "title": f"Ep {n}", "air_display": "12 Jul 2026"}
                     for n in range(1, 5)],
    }

    def make_settings(self):
        # Both services set up by the operator. Which one answers is then decided
        # by the row, which is the whole point.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        trakt_access_token="operator-token",
                        simkl_client_id="scid", simkl_client_secret="ssecret",
                        simkl_access_token="operator-simkl-token")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("simkl_roster", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "ids": {"simkl": 2735483, "tmdb": 55}, "season": 1, "title": "Simkl Only",
            "network": "SimklVision", "media": "show",
            "kind": distrakt_store.RecordKind.KEEPUP,
        }))
        asyncio.run(db.execute(
            "INSERT OR REPLACE INTO distrakt_show_progress "
            "(user_id, media, match_source, match_id, season, source, "
            "watched_episodes_json, simkl_id) VALUES (?,?,?,?,?,?,?,?)",
            (self.user_id, "show", "tmdb", "55", 1, "simkl",
             '{"1": "2026-07-12T15:18:00Z", "2": "2026-07-12T16:07:00Z"}', 2735483)))
        self.sign_in_as(self.user_id)

    def _details(self, asked: list | None = None):
        async def _fetch(settings, media, source_id, season, *, cache_only=False):
            if asked is not None:
                asked.append((str(source_id), season))
            return dict(self.SIMKL_DETAIL)

        with patch("app.providers.simkl.detail.fetch_details", _fetch):
            return self.client.get("/api/distrakt/details?key=show:tmdb:55&season=1")

    def test_the_modal_opens(self):
        """It used to 404 "Not on your roster" about a row that plainly is."""
        resp = self._details()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertTrue(resp.json()["ok"])

    def test_simkl_is_the_one_asked_and_it_is_asked_by_its_own_id(self):
        """A service cannot look a title up by an id it does not issue, so the
        chooser hands each one its own namespace."""
        asked: list = []
        body = self._details(asked).json()
        self.assertEqual(asked, [("2735483", 1)])
        self.assertEqual(body["source"], "simkl")

    def test_it_carries_the_fields_the_panel_draws(self):
        body = self._details().json()
        self.assertEqual(body["title"], "Simkl Only")
        self.assertEqual(body["overview"], "A show only one service lists.")
        self.assertEqual([e["number"] for e in body["episodes"]], [1, 2, 3, 4])

    def test_this_accounts_own_watched_episodes_come_with_it(self):
        """The watched half was never Trakt's to answer — it is read out of this
        app's own storage, written by whichever services the account syncs."""
        body = self._details().json()
        self.assertEqual(body["watched_episodes"], [1, 2])
        self.assertEqual(body["watched_by_source"], {"simkl": [1, 2]})

    def test_a_row_both_services_know_is_still_trakt_s_to_describe(self):
        """The regression half. Declared source order decides, so nothing moves
        for the rows that already worked — and a Simkl call on one of those
        would be a second catalogue read for an answer already in hand."""
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "ids": {"trakt": 7, "simkl": 99, "tmdb": 56}, "season": 1,
            "title": "Both", "media": "show",
            "kind": distrakt_store.RecordKind.KEEPUP,
        }))
        trakt_asked: list = []

        async def _trakt(settings, media, source_id, season, *, cache_only=False):
            trakt_asked.append(str(source_id))
            return {"title": "Both", "episodes": []}

        async def _simkl(*args, **kwargs):
            raise AssertionError("Simkl was asked about a title Trakt can describe")

        with patch("app.providers.trakt.detail.fetch_details", _trakt), \
                patch("app.providers.simkl.detail.fetch_details", _simkl):
            body = self.client.get("/api/distrakt/details?key=show:tmdb:56&season=1").json()
        self.assertEqual(trakt_asked, ["7"])
        self.assertEqual(body["source"], "trakt")


class ASimklOnlyAccountGetsAMonthAtAllTests(AppTestCase):
    """The month document itself, for an account signed in with Simkl alone.

    THE REPORTED SYMPTOM WAS THE STRANGEST KIND: importing from the calendar said
    it had worked and imported nothing, and adding a show by hand said it had
    been added and never showed it. Both were telling the truth about what they
    did. Rollover asked `trakt_configured` before it would CREATE a month, and on
    the per-account Settings the tracker builds that reads as "did this viewer
    link Trakt" — so for this account the answer was no, no month was ever
    persisted, and every write landed in a transient document that was discarded
    on the way out. A roster row was left behind with no month to appear on,
    which is exactly what the live database showed: one season stored, zero
    months.

    A MONTH IS BUILT FROM A CALENDAR, so that is what is asked now.
    `_initialize_month` fills a new month with that month's premieres and nothing
    else — whose token is on the request decides nothing about whether those
    exist.
    """

    def make_settings(self):
        # No Trakt access token at all: nothing on this instance or this account
        # can read anybody's private Trakt data, and Simkl is what fills the
        # calendar. The month must still be built.
        return Settings(public_base_url=ORIGIN, timezone="UTC",
                        simkl_client_id="scid", simkl_client_secret="ssecret",
                        simkl_access_token="operator-simkl-token")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("simkl_month", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)
        self.today = date.today()

    def stored_months(self) -> list[str]:
        rows = asyncio.run(db.fetch_all(
            "SELECT month FROM distrakt_months WHERE user_id = ? ORDER BY month",
            (self.user_id,)))
        return [row["month"] for row in rows]

    def open_the_month(self):
        """What the page actually does. /distrakt is a shell; the month is built
        by the call the page then makes, which is where every gate below sits.

        The build reads the calendar for its premieres and nothing here is about
        what it finds there — an unpatched read would reach the network the suite
        refuses."""
        with _quiet_sources():
            return self.client.get(
                f"/api/distrakt/month?year={self.today.year}&month={self.today.month}")

    def test_opening_the_month_persists_it(self):
        """It used to hand back an unpersisted empty document every time, so
        nothing the account did to that month could survive the response."""
        self.assertEqual(self.stored_months(), [])
        self.assertEqual(self.open_the_month().status_code, 200)
        self.assertEqual(self.stored_months(),
                         [f"{self.today.year}-{self.today.month:02d}"])

    def test_importing_writes_into_a_month_that_is_still_there_afterwards(self):
        """The reported case. The import itself had nothing wrong with it — it
        merged premieres into a document nobody kept."""
        with _quiet_sources():
            resp = self.client.post("/api/distrakt/import", json={
                "year": self.today.year, "month": self.today.month})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(self.stored_months(),
                         [f"{self.today.year}-{self.today.month:02d}"])

    def test_the_month_build_reads_the_calendar_as_the_instance(self):
        """THE 401 THIS REPAIRS, and it is the half a gate-only fix leaves behind.
        Past every gate, the build still read the calendar with the per-account
        Settings — so for an account with no Trakt token it asked Trakt's
        calendar with no bearer and the whole month failed on Trakt's own 401,
        reported from a browser as "Trakt rejected the credentials" while adding
        a film. A calendar window is fetched under the INSTANCE's credentials and
        served to everybody; whose token is on the request decides nothing about
        what a month holds."""
        save_settings(Settings(
            public_base_url=ORIGIN, timezone="UTC",
            trakt_client_id="cid", trakt_access_token="instance-token",
            simkl_client_id="scid", simkl_client_secret="ssecret",
            simkl_access_token="operator-simkl-token"))
        seen: list = []

        async def _read(endpoint, settings, **kwargs):
            seen.append(settings)
            return ([], None)

        with patch("app.calendar.cache.read_month", _read),              patch("app.distrakt.watch_history.tracker_ports", AsyncMock(return_value=[])):
            resp = self.client.get(
                f"/api/distrakt/month?year={self.today.year}&month={self.today.month}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(seen, "the month was built without reading a calendar at all")
        for settings in seen:
            self.assertEqual(settings.trakt_access_token, "instance-token")

    def test_an_instance_with_no_calendar_source_still_builds_nothing(self):
        """The other half, unchanged: with nobody able to supply a calendar there
        are no premieres to build a month out of, and baking an empty one in
        would stop a proper build happening once a source is configured."""
        save_settings(Settings(public_base_url=ORIGIN, timezone="UTC"))
        self.assertEqual(self.open_the_month().status_code, 200)
        self.assertEqual(self.stored_months(), [])
