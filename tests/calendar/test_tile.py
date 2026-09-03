"""The season line under a card — how many episodes, what aired last, what next.

THE COMPLAINT: only a handful of cards showed it, and every one of them was a
title linked on both Trakt and Simkl. It read as missing source data and was
not. `/api/tile` took a bare `id`, treated it as a TRAKT id, and refused
outright unless the instance held Trakt catalogue credentials — so a Simkl-only
card could never show the line, not because Simkl cannot answer but because
nothing asked it.

WHAT CHANGED: the route takes IDS and picks a source, exactly as `/api/details`
beside it already did, and both packages' season summaries are reachable through
one port method. "Which source answers for this title" is now the same question
whether the answer is a description or a season.

No network — every source's port is stood in for.
"""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers import season as season_rules
from app.providers.base import Media, Source, SourceUnavailable

from tests.calendar.test_routes import CalendarRouteTestCase


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


class TheRouteAsksWhicheverSourceCanAnswerTests(CalendarRouteTestCase):
    """The behaviour the complaint was about: the line does not depend on a
    title happening to carry a Trakt id."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("tilewatcher"))

    def _ask(self, query, *, answer=None, raises=None, source=Source.SIMKL):
        asked = {}

        async def _summary(settings, source_id, season, media):
            asked.update(source_id=source_id, season=season, media=media)
            if raises is not None:
                raise raises
            return dict(answer if answer is not None else AIRED)

        port = SimpleNamespace(fetch_season_summary=_summary,
                               catalogue_configured=lambda settings: True)
        with patch("app.calendar.detail_source.choose",
                   return_value=SimpleNamespace(source=source, source_id="4242")), \
             patch("app.providers.get",
                   return_value=SimpleNamespace(detail_port=port)):
            return self.client.get(f"/api/tile?{query}"), asked

    def test_a_simkl_only_card_gets_its_line(self):
        """THE REPORTED BUG. This used to be a 400 on an instance without Trakt
        catalogue credentials, and a card with no Trakt id never even asked."""
        resp, asked = self._ask("media=show&simkl=4242&season=2")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["episode_count"], 5)
        self.assertEqual(body["source"], str(Source.SIMKL))
        self.assertEqual(asked["source_id"], "4242",
                         "the chosen source was not asked with its own id")

    def test_the_season_asked_for_is_the_season_asked_about(self):
        _, asked = self._ask("media=show&simkl=4242&season=2")
        self.assertEqual(asked["season"], 2)
        self.assertEqual(asked["media"], Media.SHOW)

    def test_a_title_no_source_can_answer_for_is_a_404(self):
        """Well formed, and nobody to ask — which is not the caller's mistake,
        so it is not a 400. The card leaves its line blank, which is what it
        shows before any answer arrives anyway."""
        with patch("app.calendar.detail_source.choose", return_value=None):
            resp = self.client.get("/api/tile?media=show&trakt=1&season=1")
        self.assertEqual(resp.status_code, 404)

    def test_a_request_naming_no_season_is_a_404_rather_than_a_crash(self):
        resp, _ = self._ask("media=show&simkl=4242")
        self.assertEqual(resp.status_code, 404)

    def test_a_service_that_could_not_be_reached_is_not_an_empty_season(self):
        """A rate limit must never render as "no episodes" — the card would
        state a fact nobody reported. It is the shared degradation contract
        that is caught here, not one service's error type, because either of
        two sources can now answer this route."""
        resp, _ = self._ask("media=show&simkl=4242&season=2",
                            raises=SourceUnavailable("Rate limited", status=429))
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(resp.json()["ok"])
