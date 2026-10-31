"""Whose episode total a tracker row is measured against is the VIEWER's order.

What is held here is that the order changes WHO IS ASKED and nothing else: the
first catalogue the viewer names that the title has an id for answers, a season
that catalogue holds nothing of goes on to the next one in the same order, a
catalogue this instance cannot ask is skipped, and a viewer who stated nothing is
asked in the registry's declared order exactly as before.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import db, providers
from app.distrakt import live
from app.providers import detail_source
from app.sources import prefs as source_prefs
from tests.support import DatabaseTestCase

IDS = {"tmdb": "303", "trakt": "101", "simkl": "202"}
REC = {"media": "show", "season": 2, "ids": dict(IDS)}
ALL = SimpleNamespace(tmdb_configured=True, trakt_catalogue_configured=True,
                      simkl_catalogue_configured=True)
NO_TRAKT = SimpleNamespace(tmdb_configured=True, trakt_catalogue_configured=False,
                           simkl_catalogue_configured=True)


def _summary(total: int) -> dict:
    return {"season": 2, "total": total, "cadence": "Mon", "premiere": None,
            "finale": None, "started_airing": False, "finished_airing": False,
            "air_dates": []}


def _facts(totals: dict[str, int]):
    """A `season_facts` double answering each catalogue its own total, and
    recording who was asked and in what order."""
    asked: list[str] = []

    async def season_facts(settings, answerer, media, season, **_kwargs):
        asked.append(str(answerer.source))
        return _summary(totals.get(str(answerer.source), 0))

    return AsyncMock(side_effect=season_facts), asked


class TheOrderIsTheViewersTests(unittest.TestCase):

    def test_nothing_stated_is_the_declared_order(self):
        prefs = source_prefs.SourcePrefs(user_id=1)
        self.assertEqual(live.totals_order_of(prefs), ("tmdb", "trakt", "simkl"))

    def test_a_stated_order_moves_a_service_up_and_removes_none(self):
        prefs = source_prefs.SourcePrefs(user_id=1, season_totals_order=["simkl", "trakt"])
        self.assertEqual(live.totals_order_of(prefs), ("simkl", "trakt", "tmdb"))

    def test_a_name_this_version_does_not_know_falls_out(self):
        prefs = source_prefs.SourcePrefs(user_id=1, season_totals_order=["letterboxd", "trakt"])
        self.assertEqual(live.totals_order_of(prefs), ("trakt", "tmdb", "simkl"))

    def test_the_service_that_decides_the_total_is_the_first_one_named(self):
        for order, expected in ((("trakt",), "trakt"), (("simkl", "trakt"), "simkl"),
                                (None, "tmdb"), ((), "tmdb")):
            with self.subTest(order=order):
                self.assertEqual(live.detail_source(dict(REC), ALL, order), expected)

    def test_a_catalogue_this_instance_cannot_ask_is_skipped(self):
        self.assertEqual(live.detail_source(dict(REC), NO_TRAKT, ("trakt", "simkl")), "simkl")

    def test_a_title_the_first_service_has_no_id_for_goes_to_the_next(self):
        rec = dict(REC, ids={"trakt": "101", "simkl": "202"})
        self.assertEqual(live.detail_source(rec, ALL, ("tmdb", "simkl", "trakt")), "simkl")

    def test_the_panel_is_asked_in_the_declared_order_whatever_a_viewer_wants(self):
        """`choose` with no order is every details panel's question."""
        self.assertEqual(detail_source.choose(ALL, IDS).source, "tmdb")


class TheOrderChangesWhoIsAskedTests(unittest.IsolatedAsyncioTestCase):

    async def _detail(self, order, totals):
        facts, asked = _facts(totals)
        with patch("app.distrakt.live.store_panel.season_facts", facts):
            detail = await live.season_detail(ALL, dict(REC), order=order)
        return detail, asked

    async def test_the_first_service_named_answers_and_nobody_else_is_asked(self):
        detail, asked = await self._detail(("simkl", "trakt", "tmdb"),
                                           {"tmdb": 10, "trakt": 8, "simkl": 9})
        self.assertEqual((detail["total"], detail["described_by"]), (9, "simkl"))
        self.assertEqual(asked, ["simkl"])

    async def test_no_order_asks_tmdb_first_as_every_row_always_did(self):
        detail, asked = await self._detail(None, {"tmdb": 10, "trakt": 8, "simkl": 9})
        self.assertEqual((detail["total"], detail["described_by"]), (10, "tmdb"))
        self.assertEqual(asked, ["tmdb"])

    async def test_a_season_the_first_holds_nothing_of_goes_on_down_the_same_order(self):
        """Trakt first and Trakt has no episodes of the season: the viewer's
        second choice is asked next, not the declared order's."""
        detail, asked = await self._detail(("trakt", "simkl", "tmdb"),
                                           {"trakt": 0, "simkl": 9, "tmdb": 10})
        self.assertEqual((detail["total"], detail["described_by"]), (9, "simkl"))
        self.assertEqual(asked, ["trakt", "simkl"])

    async def test_a_service_that_could_not_be_reached_is_never_read_as_empty(self):
        facts = AsyncMock(side_effect=providers.base.SourceUnavailable("down"))
        with patch("app.distrakt.live.store_panel.season_facts", facts), \
                self.assertRaises(providers.base.SourceUnavailable):
            await live.season_detail(ALL, dict(REC), order=("trakt", "simkl", "tmdb"))

    async def test_a_batch_follows_the_order_it_was_given(self):
        facts, asked = _facts({"trakt": 8, "tmdb": 10})
        with patch("app.distrakt.live.store_panel.season_facts", facts):
            details = await live.fetch_season_details(
                ALL, [dict(REC), dict(REC, season=3)], fresh=False, allow_degrade=False,
                order=("trakt", "simkl", "tmdb"))
        self.assertEqual([detail["described_by"] for detail in details], ["trakt", "trakt"])
        self.assertEqual(set(asked), {"trakt"})


class ARowNobodyAnsweredKeepsTheViewersOrderTests(DatabaseTestCase):
    """A row whose season no service answered this pass still says whose total it
    is drawing — and that is the viewer's TOTALS order, never the order its
    watched counts are picked in (the services read for the account, most trusted
    first). The two orders were one name inside the counting step, so a degraded
    row filed its total under the tracker's first service."""

    REC = dict(REC, total=10, watched=3)

    async def test_a_degraded_row_files_its_total_under_the_first_service_in_the_totals_order(self):
        """Nothing stated is the declared order, TMDB first — while the account's
        trackers are Trakt then Simkl, which must not decide it."""
        failed = AsyncMock(return_value=[providers.base.SourceUnavailable("down")])
        with patch("app.distrakt.live.fetch_season_details", failed):
            shows = await live.compute_live_shows(
                1, [dict(self.REC)], ALL, watched_lookup={}, allow_degrade=True,
                sources_read=("trakt", "simkl"))
        self.assertTrue(shows[0]["unavailable"])
        self.assertEqual(shows[0]["total_by_source"], {"tmdb": 10})

    def test_the_stored_page_files_every_total_under_the_order_it_was_handed(self):
        """The rate-limited page reads no history at all, so the watched counts'
        order is the declared one; the totals order is Simkl first."""
        shows = live.stored_shows([dict(self.REC)], ALL, order=("simkl", "trakt", "tmdb"))
        self.assertEqual(shows[0]["total_by_source"], {"simkl": 10})


class TheOrderIsStoredPerViewerTests(DatabaseTestCase):

    async def _viewer(self, name: str) -> int:
        """A viewer whose row can be written: preferences belong to an account."""
        result = await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES (?, ?, ?)",
            (name, db.now(), db.now()))
        return result.lastrowid

    async def test_it_survives_a_round_trip_and_is_read_by_the_live_pass(self):
        viewer = await self._viewer("stated")
        await source_prefs.save(source_prefs.SourcePrefs(
            user_id=viewer, season_totals_order=["trakt", "simkl", "tmdb"]))
        self.assertEqual(await live.totals_order(viewer), ("trakt", "simkl", "tmdb"))

    async def test_a_viewer_who_stated_nothing_has_the_declared_order(self):
        self.assertEqual(await live.totals_order(8), ("tmdb", "trakt", "simkl"))

    async def test_the_other_orders_are_untouched_by_it(self):
        viewer = await self._viewer("beside")
        saved = await source_prefs.save(source_prefs.SourcePrefs(
            user_id=viewer, metadata_order=["simkl"], season_totals_order=["trakt"]))
        loaded = await source_prefs.load(viewer)
        self.assertEqual(loaded.metadata_order, ["simkl"])
        self.assertEqual(loaded.season_totals_order, saved.season_totals_order)
        self.assertEqual(loaded.tracker_priority, [])

    async def test_every_service_with_a_catalogue_can_be_named(self):
        """No per-service table to forget: whatever the registry says can
        describe a title is accepted, in any order."""
        describing = [str(source) for source, provider in providers.registered().items()
                      if provider.detail_port is not None]
        saved = await source_prefs.save(source_prefs.SourcePrefs(
            user_id=await self._viewer("every"), season_totals_order=list(reversed(describing))))
        self.assertEqual(saved.season_totals_order, list(reversed(describing)))
        self.assertEqual(set(describing), {str(source) for source in providers.registered()})

    async def test_a_service_nobody_registered_is_refused(self):
        with self.assertRaises(ValueError):
            await source_prefs.save(source_prefs.SourcePrefs(
                user_id=await self._viewer("unknown"), season_totals_order=["letterboxd"]))

    async def test_one_service_twice_is_refused(self):
        with self.assertRaises(ValueError):
            await source_prefs.save(source_prefs.SourcePrefs(
                user_id=await self._viewer("twice"), season_totals_order=["trakt", "trakt"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
