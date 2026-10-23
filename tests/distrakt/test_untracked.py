"""A tracker row no tracking service lists says so (app/distrakt/routes.py's
`_untracked`, drawn by _distrakt_rows.html's `untracked_mark`).

A search finds titles in a catalogue that keeps no watch history (TMDB), and the
lookups by shared id that teach a new row its Trakt and Simkl ids can find it at
neither. The add is allowed; a row whose counts can never move must not read as
"not watched yet".
"""
from __future__ import annotations

import unittest

from app.distrakt import routes as distrakt_routes
from app.templating import templates


class UntrackedTests(unittest.TestCase):

    def test_a_row_no_tracking_service_lists_is_untracked(self):
        show = {"ids": {"tmdb": 1, "imdb": "tt1"}}
        self.assertTrue(distrakt_routes._untracked(show, ["trakt", "simkl"]))

    def test_any_one_tracking_service_listing_it_is_enough(self):
        self.assertFalse(distrakt_routes._untracked({"ids": {"tmdb": 1, "simkl": 9}},
                                                    ["trakt", "simkl"]))

    def test_only_the_services_this_viewer_tracks_with_count(self):
        """A Trakt id says nothing to a viewer whose tracker reads Simkl alone."""
        self.assertTrue(distrakt_routes._untracked({"ids": {"tmdb": 1, "trakt": 4}},
                                                   ["simkl"]))

    def test_a_frozen_month_reads_no_service_and_marks_nothing(self):
        self.assertFalse(distrakt_routes._untracked({"ids": {"tmdb": 1}}, None))

    def test_the_mark_is_drawn_with_the_reason_and_only_when_untracked(self):
        rows = templates.env.get_template("_distrakt_rows.html").module
        self.assertIn("untracked", str(rows.untracked_mark({"untracked": True})))
        self.assertIn("nothing you watch will be counted",
                      str(rows.untracked_mark({"untracked": True})))
        self.assertEqual(str(rows.untracked_mark({"untracked": False})).strip(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
