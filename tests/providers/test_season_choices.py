"""app/providers/season_choices.py — what every season picker offers, greys and
suggests (the tracker's add, the board's add and row control, the calendar's
jump). The board's own row-control tests (tests/ranker/test_season_control.py)
hold the suggestion and premiere rules in depth; these hold what the other
surfaces added: their own words for "already here", a refusal with its reason,
a link to where a season is, the episode count, and the ticked-field reader.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.providers import season_choices

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
LISTED = [{"season": 1, "episode_count": 8, "first_aired": "2022-02-18"},
          {"season": 2, "episode_count": 1, "first_aired": "2025-01-17"},
          {"season": 3, "episode_count": 0, "first_aired": ""}]


class ChoicesTests(unittest.TestCase):

    def options(self, **kwargs):
        return {o.season: o for o in season_choices.seasons_only(
            season_choices.season_options(LISTED, held=kwargs.pop("held", ()), current=None,
                                          finished=None, now=NOW, **kwargs))}

    def test_a_surface_says_held_in_its_own_words(self):
        options = self.options(held={1}, held_as="on your tracker")
        self.assertIn("on your tracker", options[1].notes)
        self.assertFalse(options[1].pickable)

    def test_the_episode_count_is_said_and_one_is_singular(self):
        options = self.options()
        self.assertEqual(options[1].notes[0], "8 eps")
        self.assertEqual(options[2].notes[0], "1 ep")

    def test_an_undated_season_is_not_called_unaired(self):
        """Nobody said anything about it, so the picker says nothing either."""
        options = self.options()
        self.assertIsNone(options[3].aired)
        self.assertEqual(options[3].notes, ())

    def test_a_refusal_and_a_link_are_said_and_neither_can_be_picked(self):
        options = {o.season: o for o in season_choices.blocking(
            self.options().values(), {1: "your filters hide it"}, {2: "/calendar?x"})}
        self.assertIn("your filters hide it", options[1].notes)
        self.assertFalse(options[1].pickable)
        self.assertEqual(options[2].href, "/calendar?x")
        self.assertFalse(options[2].pickable)
        self.assertTrue(options[3].pickable)

    def test_a_result_that_is_one_season_offers_that_season_alone(self):
        self.assertEqual([o.season for o in season_choices.only(self.options().values(), 2)],
                         [2])
        self.assertEqual([o.season for o in season_choices.only([], 7)], [7])

    def test_a_named_choice_reads_as_its_name(self):
        self.assertEqual(season_choices.SeasonOption(None, name="Release").label, "Release")


class TickedTests(unittest.TestCase):

    def test_one_flat_field_per_ticked_season_in_the_order_drawn(self):
        data = {"q": "x", "season:3": "1", "season:1": "1", "season:2": "", "seasonX": "1"}
        self.assertEqual(season_choices.ticked(data), ["3", "1"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
