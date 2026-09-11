"""The season line under a card — how many episodes, what aired last, what next.

THE COMPLAINT: only a handful of cards showed it, and every one of them was a
title linked on both Trakt and Simkl. It read as missing source data and was
not. The route took a bare `id`, treated it as a TRAKT id, and refused outright
unless the instance held Trakt catalogue credentials — so a Simkl-only card could
never show the line, not because Simkl cannot answer but because nothing asked it.

WHAT CHANGED: the route takes IDS and picks a source, exactly as the details
fragment beside it does, and both packages' season summaries are reachable
through one port method. "Which source answers for this title" is the same
question whether the answer is a description or a season.

AND THE LINE ARRIVES DRAWN. The card draws a slot that fetches
/calendar/fragments/season-line as it scrolls into view and is replaced by the
answer, so nothing in the browser turns a payload into markup and nothing has to
watch the cards a late day brings.

No network — every source's port is stood in for.
"""
from __future__ import annotations

import re
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app.providers import season as season_rules
from app.providers.base import Media, Source, SourceUnavailable

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


class TheLineIsDrawnForWhicheverSourceCanAnswerTests(CalendarRouteTestCase):
    """The behaviour the complaint was about — the line does not depend on a
    title happening to carry a Trakt id — and the shape it now arrives in."""

    URL = "/calendar/fragments/season-line"

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
        with patch("app.providers.detail_source.choose",
                   return_value=SimpleNamespace(source=source, source_id="4242")), \
             patch("app.providers.get",
                   return_value=SimpleNamespace(detail_port=port)), \
             patch("app.clock.today", return_value=date(2026, 9, 3)):
            return self.client.get(f"{self.URL}?{query}"), asked

    def test_a_simkl_only_card_gets_its_line(self):
        """THE REPORTED BUG. This used to be refused on an instance without
        Trakt catalogue credentials, and a card with no Trakt id never asked."""
        resp, asked = self._ask("media=show&simkl=4242&season=2")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertIn('<div class="season-info">', resp.text)
        self.assertEqual(asked["source_id"], "4242",
                         "the chosen source was not asked with its own id")

    def test_the_line_says_how_many_episodes_what_aired_last_and_what_airs_next(self):
        resp, _ = self._ask("media=show&simkl=4242&season=2")
        self.assertIn("📋 <b>5</b> eps · S2", resp.text)
        self.assertIn("🏁 Latest: <b>2026-09-01</b>", resp.text)
        self.assertIn('<span class="si next">📡 Next: <b>2026-09-08</b>', resp.text)

    def test_one_episode_is_not_counted_in_the_plural(self):
        resp, _ = self._ask("media=show&simkl=4242&season=2",
                            answer={**AIRED, "total": 1})
        self.assertIn("📋 <b>1</b> ep · S2", resp.text)

    def test_the_season_asked_for_is_the_season_asked_about(self):
        _, asked = self._ask("media=show&simkl=4242&season=2")
        self.assertEqual(asked["season"], 2)
        self.assertEqual(asked["media"], Media.SHOW)

    def test_a_title_no_source_can_answer_for_draws_nothing(self):
        """Well formed, and nobody to ask. The answer replaces the slot that
        asked, so an empty one simply removes it — at 200, because a status the
        page does not swap would leave the slot standing."""
        with patch("app.providers.detail_source.choose", return_value=None):
            resp = self.client.get(f"{self.URL}?media=show&trakt=1&season=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text.strip(), "")

    def test_a_request_naming_no_season_draws_nothing_rather_than_crashing(self):
        resp, asked = self._ask("media=show&simkl=4242")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text.strip(), "")
        self.assertEqual(asked, {}, "a season-less request still asked a service")

    def test_a_medium_this_app_does_not_have_draws_nothing(self):
        resp, _ = self._ask("media=podcast&simkl=4242&season=2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text.strip(), "")

    def test_a_service_that_could_not_be_reached_is_not_an_empty_season(self):
        """A rate limit must never render as "no episodes" — the card would
        state a fact nobody reported. It is the shared degradation contract
        that is caught here, not one service's error type, because either of
        two sources can answer this route. Nothing is drawn, which states
        nothing."""
        resp, _ = self._ask("media=show&simkl=4242&season=2",
                            raises=SourceUnavailable("Rate limited", status=429))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text.strip(), "")
        self.assertNotIn("Rate limited", resp.text)

    def test_a_season_nobody_could_date_draws_nothing(self):
        resp, _ = self._ask("media=show&simkl=4242&season=2",
                            answer=season_rules.empty_season(2))
        self.assertEqual(resp.text.strip(), "")


class TheCardAsksForItsOwnLineTests(CalendarRouteTestCase):
    """What the card draws so the line can arrive without a script."""

    PAGE = "/calendar?year=2026&month=7&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("slotwatcher"))
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([_entry("the-drama", "The Drama", "2026-07-15T20:00:00Z")]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _slot(self) -> str:
        found = re.search(r'<div class="season-line-slot"[^>]*>',
                          self.client.get(self.PAGE).text, re.S)
        self.assertIsNotNone(found, "the card drew no season-line slot")
        return found.group(0)

    def test_the_slot_fetches_the_line_as_the_card_scrolls_into_view(self):
        slot = self._slot()
        self.assertIn('hx-get="/calendar/fragments/season-line?media=show', slot)
        self.assertIn("season=1", slot)
        self.assertIn('hx-trigger="intersect once', slot)

    def test_the_answer_replaces_the_slot(self):
        """outerHTML, so an empty answer removes the slot rather than leaving an
        empty box under the card."""
        self.assertIn('hx-swap="outerHTML"', self._slot())

    def test_the_line_does_not_light_the_page_wide_progress_bar(self):
        """Every element inherits <body>'s hx-indicator. A line per card as a
        month scrolls past is not something anybody is waiting through, so the
        slot names itself as its indicator instead."""
        self.assertIn('hx-indicator="this"', self._slot())

    def test_no_script_builds_the_line(self):
        html = self.client.get(self.PAGE).text
        self.assertNotIn("season-tiles.js", html)
        self.assertNotIn("data-tile-url", html)
