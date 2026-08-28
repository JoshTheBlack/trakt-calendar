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

from app import cache, db, distrakt as distrakt_store
from app.config import Settings, save_settings
from app.distrakt import live, routes as distrakt_routes
from app.providers.base import PlayCounts
from app.providers.trakt import TraktError
from app.providers.trakt import detail as trakt_detail, transport
from tests.support import AppTestCase, ORIGIN, migrated_db

# THE INSTANCE'S CATALOGUE CREDENTIALS, WHICH ARE NOT ANYBODY'S TOKEN. Whether a
# season's episode count can be looked up is a client id and nothing else (see
# Settings.trakt_catalogue_configured), and the live pass now asks the question
# before it makes the call — so "the service is down" and "the service is not set
# up" are two different fixtures rather than two readings of one.
BOTH_CATALOGUES = Settings(trakt_client_id="cid", simkl_client_id="scid")
NO_CATALOGUES = Settings()
SIMKL_CATALOGUE_ONLY = Settings(simkl_client_id="scid")


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
        migrated_db("without-trakt")
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

        # BOTH CATALOGUE CREDENTIALS PRESENT, which is what makes these tests
        # about a service that was ASKED and went quiet. Whether a source can be
        # asked at all is now read off the settings before the lookup is made
        # (live.detail_source), so a bare None here would be the other failure
        # entirely — that one is TheRowSaysWhichSilenceItIsTests below.
        with patch("app.providers.trakt.detail.fetch_season_detail", _boom), \
             patch("app.providers.simkl.detail.fetch_season_detail", _boom):
            return await live.compute_live_shows(
                self.user_id, [record], BOTH_CATALOGUES, watched_lookup={},
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
                self.user_id, [dict(self.RECORD)], BOTH_CATALOGUES, watched_lookup={},
                allow_degrade=True, sources_read=("trakt",))
        self.assertEqual(live.unreadable_detail_sources(rows), [])


class TheRowSaysWhichSilenceItIsTests(_CatalogueFailureTestCase):
    """A ROW WHOSE SOURCE CANNOT BE ASKED AT ALL, which is not the same failure
    as one that was asked and did not answer, and used to be indistinguishable.

    Blanking one service's client id emptied almost a whole roster: the lookup
    was made anyway, the unconfigured service answered nothing, and the zero it
    answered with was written into the row as though it had been measured. What
    a viewer saw was a title with no episodes and a banner naming a service that
    was not down — and, because the response cache goes on serving what it holds,
    they saw it some time AFTER the credential changed rather than at the moment
    it did.
    """

    async def _rows(self, settings, *, ids=None, asked=None):
        record = dict(self.RECORD, ids=ids or {"trakt": 7, "tmdb": 1})
        # NOTHING IS PATCHED, DELIBERATELY. If any of these rows reached a
        # provider the suite's own network guard would say so; the point is that
        # the lookup is never attempted.
        return await live.compute_live_shows(
            self.user_id, [record], settings, watched_lookup={},
            allow_degrade=True, sources_read=asked or ("trakt",))

    async def test_a_source_with_no_catalogue_credential_is_not_asked_at_all(self):
        row, = await self._rows(NO_CATALOGUES)
        self.assertTrue(row["unavailable"])

    async def test_the_stored_counts_and_dates_are_drawn_rather_than_blanked(self):
        """The record already holds a last-known copy of every live field, so
        there is a real number to show. Disappearing is never the right answer
        for a fact the row already has on hand."""
        record = dict(self.RECORD, total=8, cadence="Tue", premiere="7/1",
                      finale="9/2", started_airing=True)
        rows = await live.compute_live_shows(
            self.user_id, [record], NO_CATALOGUES, watched_lookup={},
            allow_degrade=True, sources_read=("trakt",))
        self.assertEqual(rows[0]["total"], 8)
        self.assertEqual(rows[0]["cadence"], "Tue")
        self.assertEqual(rows[0]["premiere"], "7/1")
        self.assertEqual(rows[0]["finale"], "9/2")

    async def test_it_says_the_service_is_not_configured_rather_than_unreachable(self):
        """The whole sentence is the server's — the browser only draws it — and
        it has to name the settings problem rather than offer a refresh that
        cannot fix one."""
        row, = await self._rows(NO_CATALOGUES)
        self.assertIn("Trakt", row["counts_note"])
        self.assertIn("configured", row["counts_note"])
        self.assertNotIn("could not be reached", row["counts_note"])

    async def test_the_row_names_the_service_and_the_page_stays_quiet(self):
        """The row still NAMES the service it wanted — it is the only thing that
        knows — and the reason beside that name is what keeps it out of the "could
        not be read" sentence. Saying a service was unreachable when it is simply
        absent is what sent an operator looking for an outage.

        AND THE PAGE SAYS NOTHING AT ALL about it, which is a decision rather than
        an oversight: a missing credential is a standing fact about the instance,
        not news, most viewers cannot act on it, and an instance deliberately
        running one service would otherwise carry a banner about the other for
        ever. It belongs on the rows it applies to."""
        rows = await self._rows(NO_CATALOGUES)
        self.assertEqual(rows[0]["unavailable_source"], "trakt")
        self.assertEqual(rows[0]["unavailable_reason"], live.NOT_CONFIGURED)
        self.assertIn("Trakt", rows[0]["counts_note"])
        self.assertEqual(live.unreadable_detail_sources(rows), [])
        self.assertEqual(live.unavailable_notices(rows), [])

    async def test_a_service_that_was_read_and_went_quiet_is_still_announced(self):
        """The other half of the same decision. THIS one is news — it was working
        and is not now — so it keeps its banner, and one sentence covers however
        many rows are short of it."""
        records = [dict(self.RECORD, title=f"Show {n}", ids={"trakt": n, "tmdb": n})
                   for n in range(1, 6)]

        async def _boom(*args, **kwargs):
            raise TraktError("Could not reach Trakt")

        with patch("app.providers.trakt.detail.fetch_season_detail", _boom):
            rows = await live.compute_live_shows(
                self.user_id, records, BOTH_CATALOGUES, watched_lookup={},
                allow_degrade=True, sources_read=("trakt",))
        self.assertEqual(len(rows), 5)
        notice, = live.unavailable_notices(rows)
        self.assertIn("Trakt could not be read just now", notice)

    async def test_a_row_with_a_second_id_asks_the_service_that_can_answer(self):
        """The repair the roster actually needed: a record carrying both ids has
        a second answer available, and asking only the first threw it away."""
        asked: list = []

        async def _simkl_season(settings, simkl_id, season, media="show"):
            asked.append(simkl_id)
            return {"season": season, "total": 12, "cadence": "Fri",
                    "premiere": "7/4", "finale": None,
                    "started_airing": True, "finished_airing": False}

        with patch("app.providers.simkl.detail.fetch_season_detail", _simkl_season):
            rows = await live.compute_live_shows(
                self.user_id, [dict(self.RECORD, ids={"trakt": 7, "simkl": 55, "tmdb": 1})],
                SIMKL_CATALOGUE_ONLY, watched_lookup={}, allow_degrade=True,
                sources_read=("simkl",))
        self.assertEqual(asked, [55])
        self.assertFalse(rows[0]["unavailable"])
        self.assertEqual(rows[0]["total"], 12)

    async def test_a_record_naming_nobody_at_all_says_that_instead(self):
        """No id any registered source issues: there is no credential an
        operator could add that would help, so the sentence must not send them
        to Settings."""
        row, = await self._rows(BOTH_CATALOGUES, ids={"tmdb": 1})
        self.assertTrue(row["unavailable"])
        self.assertNotIn("configured", row["counts_note"])


class TheRowSaysWhereItsNumbersCameFromTests(_CatalogueFailureTestCase):
    """A single number on a row says nothing about who counted it, which is the
    question a second service raises and a third makes worse."""

    async def _row(self, watched, *, unread=(), asked=("trakt", "simkl")):
        async def _season(settings, source_id, season, *a, **k):
            return {"season": season, "total": 8, "cadence": "Tue",
                    "premiere": "7/1", "finale": None,
                    "started_airing": True, "finished_airing": False}

        with patch("app.providers.trakt.detail.fetch_season_detail", _season):
            rows = await live.compute_live_shows(
                self.user_id, [dict(self.RECORD)], BOTH_CATALOGUES,
                watched_lookup={live.live_key(self.RECORD): watched},
                allow_degrade=True, sources_read=asked, sources_unread=unread)
        return rows[0]

    async def test_it_names_every_service_that_counted_it(self):
        """Both halves of x/y and they differ: the total comes from ONE source by
        design, the watched count from every service the viewer linked."""
        row = await self._row({"trakt": 4, "simkl": 4})
        self.assertEqual(row["counts_freshness"], "current")
        self.assertEqual(row["counts_note"],
                         "Counts are up to date, read from Trakt and Simkl.")

    async def test_a_service_that_could_not_be_read_stops_it_claiming_currency(self):
        """The reported case: a bogus client id left every row wearing a green
        mark while the service behind them was failing. The counts on the row are
        real — they are what could be read without it — but "up to date" is not
        true of them."""
        row = await self._row({"simkl": 4}, unread=("trakt",))
        self.assertEqual(row["counts_freshness"], "stale")
        # AND THE SERVICE THAT WENT QUIET IS NOT LISTED AMONG THE SURVIVORS. Its
        # name is still on `total_by_source` whenever the season lookup came out
        # of the cache, which had the sentence contradict itself inside its own
        # clause: "Trakt could not be read, so these counts are Trakt and
        # Simkl's alone".
        self.assertEqual(row["counts_note"],
                         "Trakt could not be read just now, so these counts are Simkl's alone.")
        # The numbers themselves are untouched: this is about what the row SAYS.
        self.assertEqual(row["total"], 8)

    async def test_a_service_nobody_asked_for_is_not_reported_missing(self):
        """`sources_unread` is narrowed to what was asked FOR THIS ACCOUNT, so
        one viewer's outage cannot appear on another's row."""
        row = await self._row({"trakt": 4}, unread=("simkl",), asked=("trakt",))
        self.assertEqual(row["counts_freshness"], "current")

    async def test_a_number_from_a_service_nobody_asked_is_not_called_up_to_date(self):
        """THE REPORTED CASE. `watched_by_source` comes off the STORED watch state,
        so a service whose credential has since been removed goes on contributing
        a number — and the row said "up to date, read from Trakt and Simkl" over
        one number that had been read and one that had merely been kept.

        It is not the `missing` case and must not render as one: nothing failed,
        nothing is waiting, and no refresh will move that number. So the mark is
        its own state and the sentence is two clauses, one per tense."""
        row = await self._row({"trakt": 4, "simkl": 4}, asked=("simkl",))
        self.assertEqual(row["counts_freshness"], "partial")
        self.assertEqual(row["counts_note"],
                         "Counts are up to date, read from Simkl. "
                         "Trakt's number is the last one read.")
        # The number itself still shows — this is about what the row SAYS.
        self.assertIn("4", row["counts"])

    async def test_a_row_whose_every_number_is_stored_says_only_that(self):
        """No first clause when nothing was read for it: "up to date, read from"
        with an empty list would be a sentence about nobody."""
        row = await self._row({"trakt": 4}, asked=("simkl",))
        self.assertEqual(row["counts_freshness"], "partial")
        self.assertEqual(row["counts_note"], "Trakt's number is the last one read.")

    async def test_a_service_that_failed_outranks_one_nobody_asked(self):
        """Both at once: one asked-and-quiet, one never asked. The failure is the
        actionable half — something is wrong right now — so it is what the mark
        and the sentence report."""
        row = await self._row({"trakt": 4, "simkl": 4}, unread=("simkl",),
                              asked=("simkl",))
        self.assertEqual(row["counts_freshness"], "stale")
        self.assertIn("could not be read", row["counts_note"])

    def test_the_list_reads_as_a_sentence_at_any_number_of_services(self):
        """Two is what is registered today and nothing about it is a rule — a
        third source joins by registering, and "both" would start lying then."""
        self.assertEqual(live.and_list(["Trakt"]), "Trakt")
        self.assertEqual(live.and_list(["Trakt", "Simkl"]), "Trakt and Simkl")
        self.assertEqual(live.and_list(["Trakt", "Simkl", "Watchstate"]),
                         "Trakt, Simkl, and Watchstate")
        self.assertEqual(live.and_list(["Trakt", "Simkl", "Watchstate", "Movietrack"]),
                         "Trakt, Simkl, Watchstate, and Movietrack")


class ADegradedRefreshKeepsTheListOnTheScreenTests(AppTestCase):
    """REPORTED FROM A BROWSER, 2026-08-19: with a bad Trakt client id, pressing
    ⟳ Refresh emptied the list and a plain reload brought it back.

    A shared prerequisite failing takes the whole month down the degraded path,
    and that path rendered the month's own records while leaving the viewer's
    list off entirely — so the seasons somebody is keeping up with, which are the
    point of the page, vanished and returned depending on which button was
    pressed. The rows have their own last-known counts either way; drawing them
    is the same answer a single degraded row has always given.
    """

    def make_settings(self):
        # The public calendar is switched off for the shared fixture's reason
        # (tests/support.py): this is about the roster, and a month rebuild would
        # otherwise go and read a real CDN.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        trakt_access_token="tok", simkl_public_calendar_enabled=False)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("refresher", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "user-token")
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "ids": {"trakt": 7, "tmdb": 1, "slug": "silo"}, "season": 3,
            "title": "Silo", "network": "Apple TV", "media": "show",
            "kind": distrakt_store.RecordKind.KEEPUP, "watched": 4, "total": 8,
        }))
        self.sign_in_as(self.user_id)

    def _degraded_month(self):
        """The month with the shared history read failing, which is what a bad
        client id does to every path that needs it."""
        async def _boom(*args, **kwargs):
            raise TraktError("Trakt rejected the credentials")

        today = date.today()
        # STUBBED AT THE TRACKER'S OWN BOUNDARY rather than at one provider call:
        # what sends a month down this path is the shared history read failing,
        # whichever of its calls was the one to raise.
        with patch("app.calendar.cache.read_month", new=AsyncMock(return_value=([], None))), \
             patch("app.distrakt.rollover.history_records", AsyncMock(return_value=[])), \
             patch("app.distrakt.watch_history.sync_and_baseline", _boom):
            return self.client.post(
                "/api/distrakt/refresh",
                json={"year": today.year, "month": today.month}).json()

    def test_the_row_is_still_there(self):
        body = self._degraded_month()
        self.assertTrue(body["ok"])
        self.assertEqual([show["title"] for show in body["shows"]], ["Silo"])

    def test_it_draws_the_counts_the_record_already_had(self):
        row, = self._degraded_month()["shows"]
        self.assertEqual(row["total"], 8)
        self.assertIn("4/8", row["counts"])

    def test_and_says_they_are_not_this_pass_s(self):
        """Showing them is only honest with the mark that says what they are."""
        row, = self._degraded_month()["shows"]
        self.assertEqual(row["counts_freshness"], "stale")
        self.assertIn("Trakt", row["counts_note"])


class TheCollapseIsNotDelayedByTheCacheTests(_CatalogueFailureTestCase):
    """WHY THIS EXISTS AT ALL, AND WHY IT MOVES A CLOCK. The roster did not empty
    when the credential was blanked — it emptied later, and nothing on screen
    connected the two events. The response cache is URL-keyed and went on serving
    what it already held while the service was unaskable, so the list looked
    perfectly healthy until those entries passed their TTL and there was nothing
    left to serve and no way to refetch. A test that blanks an id and reloads
    once therefore passes against the BROKEN code as readily as the fixed one.

    So this one seeds the real cache, ages the row past the season TTL by
    rewriting its `cached_at` (which is what `cache.get` compares against
    `db.now()`), and asks for the same row on both sides of that line.
    """

    SEASON_URL = f"{transport.API_BASE}/shows/7/seasons/1?extended=full"
    # Trakt's own episode shape, three of them: enough for the derived total to
    # be a number no fixture could produce by accident.
    EPISODES = [{"number": n, "first_aired": f"2026-07-0{n}T01:00:00.000Z"}
                for n in (1, 2, 3)]

    async def _seed_cache(self, *, age_seconds: int) -> None:
        await cache.set(self.SEASON_URL, self.EPISODES)
        await db.execute("UPDATE api_cache SET cached_at = ? WHERE cache_key = ?",
                         (db.now() - age_seconds, self.SEASON_URL))

    async def _row(self, settings):
        rows = await live.compute_live_shows(
            self.user_id, [dict(self.RECORD)], settings, watched_lookup={},
            allow_degrade=True, sources_read=("trakt",))
        return rows[0]

    async def test_a_fresh_cached_answer_is_what_a_working_instance_reads(self):
        """The control, and it is what makes the two below mean anything: with
        the credential in place the seeded row IS the answer, so this test is
        genuinely exercising the cache and not talking past it. No network is
        touched — the suite's own guard would say so if it were."""
        await self._seed_cache(age_seconds=0)
        row = await self._row(BOTH_CATALOGUES)
        self.assertFalse(row["unavailable"])
        self.assertEqual(row["total"], 3)

    async def test_a_missing_credential_is_reported_at_once_and_not_when_the_cache_runs_out(self):
        """The row must not read as healthy while a stale answer happens to
        survive. Nothing is asked, so the cached copy is never consulted, and
        the sentence names the settings problem on the very first load."""
        await self._seed_cache(age_seconds=0)
        row = await self._row(NO_CATALOGUES)
        self.assertTrue(row["unavailable"])
        self.assertIn("configured", row["counts_note"])

    async def test_and_it_reads_exactly_the_same_once_that_answer_has_expired(self):
        """THE DELAYED COLLAPSE ITSELF. Past the season TTL there is nothing
        cached to serve, which is the moment the old code fetched, got nothing
        from an unconfigured service, and wrote the nothing down as a real zero.
        The row is unchanged: still the record's own last-known total, still the
        same sentence."""
        await self._seed_cache(age_seconds=trakt_detail.SEASON_CACHE_TTL_SECONDS + 60)
        row = await self._row(NO_CATALOGUES)
        self.assertTrue(row["unavailable"])
        self.assertIn("configured", row["counts_note"])
        self.assertEqual(row["total"], self.RECORD["total"])
        self.assertNotEqual(row["total"], 0)


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
        self.assertIn("Trakt could not be read just now", body["source_notices"][0])
        # AND THE ROW CARRIES ITS OWN SENTENCE TOO, as the tooltip behind the
        # mark that says its numbers are not this load's. It used to carry a
        # bare boolean with the words in JavaScript, where there was no way to
        # say which of the three silences this was.
        row, = body["shows"]
        self.assertIn("Trakt", row["counts_note"])
        self.assertIn("could not be reached", row["counts_note"])

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

        self.assertEqual(body["source_notices"], [])


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
        would stop a proper build happening once a source is configured.

        "NOBODY TO ASK" NOW TAKES SAYING SO. One registered source's calendar is
        public files needing no credential at all, so blank credentials no longer
        describe an instance with no calendar — switching that source off is what
        does, and it is the only remaining way to reach this state."""
        save_settings(Settings(public_base_url=ORIGIN, timezone="UTC",
                               simkl_public_calendar_enabled=False))
        self.assertEqual(self.open_the_month().status_code, 200)
        self.assertEqual(self.stored_months(), [])


class AddRoutesAskWhoeverKnowsTheTitleTests(AppTestCase):
    """Adding a title BY HAND looks its season up through the provider registry,
    not through Trakt.

    WHY THIS NEEDED ITS OWN TESTS RATHER THAN THE GATE TESTS ABOVE. Those pin
    that the routes stopped refusing a Simkl-only VIEWER, and they are written
    to fail immediately after the gate so they cost no lookup — which is exactly
    why they never noticed that the lookup behind the gate still named one
    service. `api_distrakt_add` asked `trakt_detail.fetch_season_detail` with
    `ids.get("trakt")`, so a title Simkl alone knows was handed a None id: the
    record stored fine and then carried no episode total, no air dates, and no
    way to ever acquire them, because every later pass asks the same source the
    record's own ids name (live.detail_source) and finds nothing to correct.

    THE SEASON LOOKUP IS `live.season_detail` FOR BOTH ROUTES AND THE LIVE PASS,
    so "who can answer for this record" has one implementation rather than one
    per caller.

    WHAT THESE ASSERT IS WHICH SERVICE WAS ASKED, not the route's status. The
    add ends by returning the whole recomputed month, and `_quiet_sources`
    stubs every history port away so that rebuild has nothing to read — an
    artefact of the stubbing, arriving long after the lookup these are about.
    """

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_client_id="scid", simkl_client_secret="ssecret")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("hand_adder", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "simkl", 4242, "simkl-token")
        self.sign_in_as(self.user_id)

    # A title Simkl alone can answer for: no Trakt id anywhere in the map, which
    # is the ordinary shape of a Simkl search hit once its per-title lookup has
    # filled in the shared ids.
    SIMKL_ONLY_IDS = {"simkl": 694485, "tmdb": 1429}
    SEASON = {"season": 3, "total": 12, "cadence": "b", "premiere": "7/23",
              "finale": "7/23", "started_airing": True, "finished_airing": True}

    def test_add_asks_simkl_for_a_title_trakt_does_not_name(self):
        simkl_call = AsyncMock(return_value=dict(self.SEASON))
        trakt_call = AsyncMock(return_value=dict(self.SEASON))
        with _quiet_sources(), \
             patch("app.providers.simkl.detail.fetch_season_detail", simkl_call), \
             patch("app.providers.trakt.detail.fetch_season_detail", trakt_call), \
             patch("app.distrakt.watch_history.baseline_show", AsyncMock(return_value=None)):
            resp = self.client.post("/api/distrakt/add", json={
                "year": 2026, "month": 8, "ids": dict(self.SIMKL_ONLY_IDS),
                "title": "Shingeki no Kyojin Season 3", "network": "", "season": 3,
            })
        trakt_call.assert_not_awaited()
        simkl_call.assert_awaited_once()
        # THAT SERVICE'S OWN ID, never a shared one — the same rule /seasons
        # states for the identical lookup.
        self.assertEqual(simkl_call.await_args.args[1], 694485)

    def test_add_completed_asks_simkl_too_and_no_longer_demands_a_trakt_id(self):
        """It used to refuse outright unless `ids["trakt"]` was present, which
        made a Simkl-only title impossible to fill a past month in with."""
        simkl_call = AsyncMock(return_value=dict(self.SEASON))
        trakt_call = AsyncMock(return_value=dict(self.SEASON))
        with _quiet_sources(), \
             patch("app.providers.simkl.detail.fetch_season_detail", simkl_call), \
             patch("app.providers.trakt.detail.fetch_season_detail", trakt_call):
            resp = self.client.post("/api/distrakt/add-completed", json={
                "year": 2020, "month": 1, "ids": dict(self.SIMKL_ONLY_IDS),
                "title": "Shingeki no Kyojin Season 3", "season": 3,
            })
        trakt_call.assert_not_awaited()
        simkl_call.assert_awaited_once()

    def test_a_trakt_title_still_goes_to_trakt(self):
        """The other half of the same rule: nothing here prefers Simkl, it
        follows the registry's order over the ids the record actually has."""
        simkl_call = AsyncMock(return_value=dict(self.SEASON))
        trakt_call = AsyncMock(return_value=dict(self.SEASON))
        with _quiet_sources(), \
             patch("app.providers.simkl.detail.fetch_season_detail", simkl_call), \
             patch("app.providers.trakt.detail.fetch_season_detail", trakt_call), \
             patch("app.distrakt.watch_history.baseline_show", AsyncMock(return_value=None)):
            resp = self.client.post("/api/distrakt/add", json={
                "year": 2026, "month": 8, "ids": {"trakt": 1388, "tmdb": 1396},
                "title": "Breaking Bad", "network": "AMC", "season": 3,
            })
        simkl_call.assert_not_awaited()
        trakt_call.assert_awaited_once()


class TheLastTwoTraktOnlySeasonLookupsTests(unittest.IsolatedAsyncioTestCase):
    """Two callers in app/distrakt/routes.py went on asking Trakt directly off a
    bare `ids["trakt"]` after the add routes had stopped.

    Both degraded to nothing rather than storing something wrong, which is why
    they were twice left alone — but a Simkl-only instance got no premiere
    correction from one and no live counts from the other, and "how long is this
    season" is one question that already has one implementation.
    """

    SIMKL_RECORD = {"media": "show", "ids": {"simkl": 694485, "tmdb": 1429},
                    "season": 3, "title": "Shingeki no Kyojin", "watched": 4,
                    "total": 12}
    SEASON = {"season": 3, "total": 12, "cadence": "b", "premiere": "7/23",
              "finale": "7/23", "started_airing": True, "finished_airing": True}

    async def test_a_reopened_season_is_measured_by_whoever_knows_the_title(self):
        """`_season_lookup`'s callable, which the history reconciliation uses to
        re-measure a season that has come back onto the list."""
        simkl_call = AsyncMock(return_value=dict(self.SEASON))
        with patch("app.providers.simkl.detail.fetch_season_detail", simkl_call):
            look_up = distrakt_routes._season_lookup(SIMKL_CATALOGUE_ONLY)
            answer = await look_up(dict(self.SIMKL_RECORD), 3)
        self.assertEqual(answer["total"], 12)
        self.assertEqual(simkl_call.await_args.args[1], 694485)

    async def test_a_record_nobody_can_be_asked_about_answers_nothing_at_all(self):
        """{} rather than a zeroed season, and the distinction is load-bearing:
        this answer is merged over a withdrawn verdict's own counts, so a season
        of zero episodes would erase numbers that were right."""
        look_up = distrakt_routes._season_lookup(NO_CATALOGUES)
        self.assertEqual(await look_up(dict(self.SIMKL_RECORD), 3), {})

    async def test_the_abandoned_form_is_measured_the_same_way(self):
        """`_live_form_source`, which freezes the line an abandoned row keeps.
        Its catalogue half now follows the record's ids; its watched half is this
        viewer's own data and is still Trakt's, so with no Trakt in play the
        record's own number stands rather than being reset to zero."""
        simkl_call = AsyncMock(return_value=dict(self.SEASON))
        with patch("app.providers.simkl.detail.fetch_season_detail", simkl_call):
            form = await distrakt_routes._live_form_source(
                SIMKL_CATALOGUE_ONLY, dict(self.SIMKL_RECORD), 3)
        self.assertEqual(form["total"], 12)
        self.assertEqual(form["premiere"], "7/23")
        self.assertEqual(form["watched"], 4)

    async def test_the_viewers_own_progress_is_still_read_where_it_can_be(self):
        """The half that is genuinely private, unchanged: with a Trakt token in
        play the frozen line carries the count Trakt reports for that season."""
        settings = Settings(trakt_client_id="cid", trakt_access_token="tok")
        record = {"media": "show", "ids": {"trakt": 7, "tmdb": 1}, "season": 3,
                  "title": "Silo", "watched": 4, "total": 8}
        with patch("app.providers.trakt.detail.fetch_season_detail",
                   AsyncMock(return_value=dict(self.SEASON, season=3))), \
             patch("app.providers.trakt.sync.fetch_watched_map",
                   AsyncMock(return_value={(7, 3): 9})):
            form = await distrakt_routes._live_form_source(settings, record, 3)
        self.assertEqual(form["watched"], 9)
