"""The season line under a card — how many episodes, what aired last, what next.

THE COMPLAINT: only a handful of cards showed it, and every one of them was a
title linked on both Trakt and Simkl. It read as missing source data and was
not: the line was asked of Trakt alone. "Which source answers for this title" is
the same question whether the answer is a description or a season
(detail_source.choose and for_season).

AND THE LINE IS DRAWN WITH THE CARD, from what the title store holds of the
season (app/metadata/panel.py's `season_lines`; its own tests are
tests/metadata/test_season_lines.py). It used to be a request per card as the
card scrolled into view, each a service's season lookup; now a season the store
does not know draws nothing and is queued behind the page.

No network.
"""
from __future__ import annotations

import asyncio
import re
import unittest
from datetime import date
from unittest.mock import patch

from app import db
from app.metadata import descriptions
from app.providers import season as season_rules

from tests.calendar.test_routes import CalendarRouteTestCase, _entry
from tests.support import window_fetch


# A season that began in August and runs weekly into September, read on the 3rd:
# two episodes behind, one ahead, which is what makes every field distinguishable.
AIRED = {
    "season": 2, "total": 5, "cadence": "Mon",
    "premiere": "18 Aug", "finale": "15 Sep",
    "started_airing": True, "finished_airing": False,
    "air_dates": ["2026-08-18", "2026-08-25", "2026-09-01",
                  "2026-09-08", "2026-09-15"],
}


class TheDerivationIsSharedTests(unittest.TestCase):
    """`season.tile_summary` — one calculation over the shape BOTH sources
    answer, because the card's line is one line however it was answered."""

    TODAY = date(2026, 9, 3)

    def test_it_splits_the_dates_around_today(self):
        out = season_rules.tile_summary(AIRED, self.TODAY)
        self.assertEqual(out["episode_count"], 5)
        self.assertEqual(out["first_aired"], "2026-08-18")
        self.assertEqual(out["last_aired"], "2026-09-01")
        self.assertEqual(out["next_aired"], "2026-09-08")

    def test_an_episode_airing_today_has_already_aired(self):
        """The boundary, and it belongs on the past side: a card read on the day
        an episode airs should say that episode is the latest, not that it is
        still to come."""
        out = season_rules.tile_summary(
            {**AIRED, "air_dates": ["2026-09-03"]}, self.TODAY)
        self.assertEqual(out["last_aired"], "2026-09-03")
        self.assertIsNone(out["next_aired"])

    def test_a_season_entirely_ahead_has_no_last_aired(self):
        out = season_rules.tile_summary(
            {**AIRED, "air_dates": ["2026-11-01", "2026-11-08"]}, self.TODAY)
        self.assertIsNone(out["last_aired"])
        self.assertEqual(out["next_aired"], "2026-11-01")

    def test_a_finished_season_has_no_next_aired(self):
        out = season_rules.tile_summary(
            {**AIRED, "air_dates": ["2020-01-01", "2020-01-08"]}, self.TODAY)
        self.assertEqual(out["last_aired"], "2020-01-08")
        self.assertIsNone(out["next_aired"])

    def test_a_season_nobody_could_date_answers_none_and_not_empty_string(self):
        """"Nothing has aired yet" and "aired on the epoch" must never look
        alike, which is the distinction this whole module keeps drawing."""
        out = season_rules.tile_summary(season_rules.empty_season(1), self.TODAY)
        self.assertIsNone(out["first_aired"])
        self.assertIsNone(out["last_aired"])
        self.assertIsNone(out["next_aired"])
        self.assertIsNone(out["episode_count"])

    def test_an_unaired_episode_does_not_count_as_a_date(self):
        """`air_dates` carries only the episodes that HAVE one, and a None or an
        empty entry that slipped in must not sort to the front and become the
        premiere."""
        out = season_rules.tile_summary(
            {**AIRED, "air_dates": [None, "", "2026-09-01"]}, self.TODAY)
        self.assertEqual(out["first_aired"], "2026-09-01")


class TheCardDrawsItsLineFromTheStoreTests(CalendarRouteTestCase):
    """What the page draws under a card: the line the store decides, or nothing
    — and never a request of its own."""

    PAGE = "/calendar?year=2026&month=7&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("linewatcher"))
        self.drama = _entry("the-drama", "The Drama", "2026-07-15T20:00:00Z")
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch([self.drama]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.queued: list = []
        viewed = patch("app.metadata.refresh.viewed",
                       lambda settings, wanted, **kwargs: self.queued.extend(wanted))
        viewed.start()
        self.addCleanup(viewed.stop)

    def _keep_the_season(self):
        ids = self.drama["show"]["ids"]
        summary = {"season": 1, **season_rules.derive_season(
            [date.fromisoformat(day) for day in AIRED["air_dates"]], date(2026, 9, 3))}
        asyncio.run(db.transaction(lambda conn: descriptions.write_season_summary(
            conn, "trakt", "show", ids["trakt"], summary, ids=dict(ids), now=db.now())))

    def test_a_season_the_store_holds_is_drawn_with_the_card(self):
        self.client.get(self.PAGE)  # the month's first load files the title
        self._keep_the_season()
        html = self.client.get(self.PAGE).text
        self.assertIn('<div class="season-info">', html)
        self.assertIn("📋 <b>5</b> eps · S1", html)

    def test_a_season_the_store_does_not_hold_draws_nothing_and_is_queued(self):
        html = self.client.get(self.PAGE).text
        self.assertNotIn("season-info", html)
        owed = [wanted for wanted in self.queued if wanted.source is not None]
        self.assertEqual([(wanted.source, wanted.season) for wanted in owed], [("trakt", 1)])

    def test_no_card_asks_for_its_own_line(self):
        html = self.client.get(self.PAGE).text
        self.assertNotIn("season-line", html)
        self.assertNotIn("data-tile-url", html)
        self.assertEqual(self.client.get(
            "/calendar/fragments/season-line?media=show&trakt=1&season=1").status_code, 404)
