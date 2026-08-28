"""Adding one token to a filter from a card's own badge.

TWO HALVES, TESTED APART. `filter.merge_token` is the pure one — what a spec
becomes when a token is set one way or the other — and it is beside the parsers
that read it because it writes what they read. The ROUTE is the I/O edge: which
stored field a dimension writes to, that it leaves the rest of that field alone,
and that it refuses what it cannot act on.

WHY THE MERGE IS SERVER-SIDE AT ALL, asserted here rather than only argued: the
alternative is the browser reading the current spec, merging, and sending the
whole thing back — which is a race, and a second statement of the spec format in
another language.
"""
from __future__ import annotations

import asyncio
import unittest

from app import auth, db
from app.calendar.filter import merge_token, parse_network_spec, parse_spec
from tests.support import AppTestCase


class MergingOneTokenIntoASpecTests(unittest.TestCase):
    """The pure half. No database, no request."""

    def test_excluding_adds_the_leading_dash_the_parser_reads(self):
        spec = merge_token("drama", "reality", "exclude")
        self.assertEqual(parse_spec(spec), ({"drama"}, {"reality"}))

    def test_including_adds_a_bare_token(self):
        spec = merge_token("", "drama", "include")
        self.assertEqual(parse_spec(spec), ({"drama"}, set()))

    def test_an_empty_mode_removes_the_token_entirely(self):
        spec = merge_token("drama, -reality", "reality", "")
        self.assertEqual(parse_spec(spec), ({"drama"}, set()))

    def test_a_token_is_never_held_twice_or_held_both_ways(self):
        """Choosing "only this" for something already excluded means the new
        answer, not both — which is plainly what pressing it means."""
        spec = merge_token("drama, -comedy", "comedy", "include")
        includes, excludes = parse_spec(spec)
        self.assertIn("comedy", includes)
        self.assertNotIn("comedy", excludes)
        self.assertEqual(spec.count("comedy"), 1)

    def test_the_rest_of_the_spec_is_untouched_and_keeps_its_order(self):
        self.assertEqual(merge_token("a, -b, c", "d", "exclude"), "a, -b, c, -d")

    def test_a_case_variant_replaces_rather_than_duplicates(self):
        """parse_spec lowercases, so `PL` and `pl` are the same token and the
        spec must not come to hold both."""
        spec = merge_token("-PL", "pl", "include")
        self.assertEqual(parse_spec(spec), ({"pl"}, set()))

    def test_a_network_keeps_its_case_and_two_spellings_are_two_networks(self):
        """parse_network_spec refuses to fold case because a single week held both
        `TVN` (Polish) and `tvN` (Korean). Replacing one with the other here would
        filter the wrong broadcaster."""
        spec = merge_token(["tvN"], "TVN", "exclude", is_list=True)
        includes, excludes = parse_network_spec(spec)
        self.assertEqual(includes, {"tvN"})
        self.assertEqual(excludes, {"TVN"})

    def test_a_network_list_comes_back_a_list(self):
        self.assertEqual(merge_token(["HBO"], "Netflix", "exclude", is_list=True),
                         ["HBO", "-Netflix"])

    def test_an_empty_token_changes_nothing(self):
        self.assertEqual(merge_token("drama", "   ", "exclude"), "drama")


class TheBadgeRouteTests(AppTestCase):
    """The I/O edge, over HTTP."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", calendar_approved=True)
        self.sign_in_as(self.user_id)

    def press(self, **body):
        return self.client.post("/api/me/filters/badge", json=body)

    def prefs(self) -> dict:
        return asyncio.run(auth.get_user_prefs(self.user_id))

    def test_a_genre_writes_the_genres_field(self):
        resp = self.press(dimension="genre", token="reality", mode="exclude")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.prefs()["genres"], "-reality")

    def test_a_country_writes_the_countries_field(self):
        self.press(dimension="country", token="pl", mode="exclude")
        self.assertEqual(self.prefs()["countries"], "-pl")

    def test_a_network_writes_the_list_field_with_its_case_kept(self):
        self.press(dimension="network", token="tvN", mode="exclude")
        self.assertEqual(self.prefs()["network_filter"], ["-tvN"])

    def test_a_show_certification_and_a_film_one_are_different_fields(self):
        """The two vocabularies do not overlap and are stored apart, so the card's
        own media is what decides which a press writes."""
        self.press(dimension="certification", token="TV-MA", mode="exclude", media="show")
        self.press(dimension="certification", token="R", mode="exclude", media="movie")
        prefs = self.prefs()
        self.assertEqual(prefs["show_certifications"], "-TV-MA")
        self.assertEqual(prefs["movie_certifications"], "-R")

    def test_pressing_one_badge_leaves_the_rest_of_the_field_alone(self):
        """THE WHOLE REASON THIS IS NOT /api/me/prefs, which replaces a field
        outright."""
        self.press(dimension="genre", token="reality", mode="exclude")
        self.press(dimension="genre", token="music", mode="exclude")
        includes, excludes = parse_spec(self.prefs()["genres"])
        self.assertEqual(excludes, {"reality", "music"})
        self.assertEqual(includes, set())

    def test_it_leaves_the_OTHER_dimensions_alone(self):
        self.press(dimension="genre", token="reality", mode="exclude")
        self.press(dimension="country", token="pl", mode="exclude")
        prefs = self.prefs()
        self.assertEqual(prefs["genres"], "-reality")
        self.assertEqual(prefs["countries"], "-pl")

    def test_removing_a_token_takes_it_back_out(self):
        self.press(dimension="genre", token="reality", mode="exclude")
        self.press(dimension="genre", token="reality", mode="")
        self.assertEqual(self.prefs()["genres"], "")

    def test_a_dimension_the_filter_does_not_have_is_refused(self):
        """A card draws a language and a weekday; neither is something this app
        filters on, and storing one would be a preference that could never act."""
        for dimension in ("language", "day", "rating", ""):
            with self.subTest(dimension=dimension):
                resp = self.press(dimension=dimension, token="en", mode="exclude")
                self.assertEqual(resp.status_code, 400, resp.text)

    def test_a_mode_it_cannot_act_on_is_refused(self):
        resp = self.press(dimension="genre", token="drama", mode="maybe")
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_a_missing_token_is_refused(self):
        resp = self.press(dimension="genre", token="  ", mode="exclude")
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_it_answers_with_what_it_stored(self):
        """So a caller never has to read the field back to know where it landed."""
        body = self.press(dimension="genre", token="reality", mode="exclude").json()
        self.assertEqual(body["field"], "genres")
        self.assertEqual(body["value"], "-reality")

    def test_it_needs_a_session(self):
        self.client.cookies.clear()
        self.assertNotEqual(
            self.press(dimension="genre", token="drama", mode="exclude").status_code,
            200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
