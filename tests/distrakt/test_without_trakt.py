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
from app.config import Settings
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

    def test_a_row_with_no_trakt_id_is_still_a_404(self):
        """Rendering a modal for a title only Simkl knows is a separate piece of
        work with its own data behind it; until it exists, saying "not on your
        roster" is the honest answer rather than an empty modal."""
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "ids": {"simkl": 99, "tmdb": 2}, "season": 1, "title": "Simkl Only",
            "media": "show", "kind": distrakt_store.RecordKind.KEEPUP,
        }))
        with patch.object(transport, "shared_client",
                          return_value=_RecordingClient({})):
            resp = self.client.get("/api/distrakt/details?key=show:tmdb:2&season=1")
        self.assertEqual(resp.status_code, 404)


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
