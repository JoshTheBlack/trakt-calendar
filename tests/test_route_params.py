"""app/route_params.py — the request coercions the calendar and tracker share.

Every one of these takes whatever a query string or JSON body actually held, so
the cases worth pinning are the ones a browser really produces: a missing
parameter, a bookmark carrying a word where a number belongs, and the wrap-around
at each end of the year.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TRAKT_DATA_DIR", tempfile.mkdtemp(prefix="tns-params-"))

from app import route_params  # noqa: E402


class ValidYearTests(unittest.TestCase):
    def test_a_number_is_taken_as_given(self):
        self.assertEqual(route_params.valid_year("2031", 2026), 2031)
        self.assertEqual(route_params.valid_year(1999, 2026), 1999)

    def test_anything_unreadable_falls_back(self):
        for value in (None, "", "soon", [], {}, "20x6"):
            with self.subTest(value=value):
                self.assertEqual(route_params.valid_year(value, 2026), 2026)

    def test_the_year_is_deliberately_unbounded(self):
        """Both pages navigate freely, and a year with no airings renders as an
        empty month rather than an error — so there is no range to enforce."""
        self.assertEqual(route_params.valid_year("1900", 2026), 1900)
        self.assertEqual(route_params.valid_year("3000", 2026), 3000)


class ValidMonthTests(unittest.TestCase):
    def test_a_month_in_range_is_taken_as_given(self):
        for value in ("1", "7", "12", 7):
            with self.subTest(value=value):
                self.assertEqual(route_params.valid_month(value, 3), int(value))

    def test_out_of_range_falls_back_rather_than_reaching_monthrange(self):
        for value in ("0", "13", "-1", "99"):
            with self.subTest(value=value):
                self.assertEqual(route_params.valid_month(value, 3), 3)

    def test_anything_unreadable_falls_back(self):
        for value in (None, "", "July", []):
            with self.subTest(value=value):
                self.assertEqual(route_params.valid_month(value, 3), 3)


class MonthGivenTests(unittest.TestCase):
    """The question valid_month cannot answer: whether a month was asked for at
    all. The picker route forwards to /calendar only when one was."""

    def test_true_only_for_a_real_month(self):
        self.assertTrue(route_params.month_given("1"))
        self.assertTrue(route_params.month_given(12))

    def test_false_for_absent_and_for_out_of_range(self):
        for value in (None, "", "July", "0", "13"):
            with self.subTest(value=value):
                self.assertFalse(route_params.month_given(value))


class SeasonTests(unittest.TestCase):
    def test_a_number_comes_back(self):
        self.assertEqual(route_params.season("3"), 3)

    def test_season_zero_is_a_real_season_not_an_absent_one(self):
        """Specials are season 0, so this must not be conflated with None the way
        a truthiness check would."""
        self.assertEqual(route_params.season("0"), 0)
        self.assertIsNotNone(route_params.season("0"))

    def test_absent_stays_none_because_none_means_whichever_is_airing(self):
        for value in (None, "", "latest"):
            with self.subTest(value=value):
                self.assertIsNone(route_params.season(value))


class AdjacentMonthsTests(unittest.TestCase):
    def test_mid_year_stays_in_the_same_year(self):
        self.assertEqual(
            route_params.adjacent_months(2026, 7),
            {"prev_month": 6, "prev_year": 2026, "next_month": 8, "next_year": 2026},
        )

    def test_january_reaches_back_into_the_previous_year(self):
        nav = route_params.adjacent_months(2026, 1)
        self.assertEqual((nav["prev_month"], nav["prev_year"]), (12, 2025))
        self.assertEqual((nav["next_month"], nav["next_year"]), (2, 2026))

    def test_december_reaches_forward_into_the_next_year(self):
        nav = route_params.adjacent_months(2026, 12)
        self.assertEqual((nav["prev_month"], nav["prev_year"]), (11, 2026))
        self.assertEqual((nav["next_month"], nav["next_year"]), (1, 2027))


if __name__ == "__main__":
    unittest.main()
