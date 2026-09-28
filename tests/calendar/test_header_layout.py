"""The calendar header's first row fits a phone, asserted against the stylesheet.

THE ROW IS THE WORDMARK AND THE MONTH NAV (.hero-lead), and none of its parts can
shrink: the wordmark is `flex-shrink: 0` because it is read rather than
recognised, the month title is `white-space: nowrap`, and the arrows are
`flex-shrink: 0`. Together they are about 400px wide, so on a phone the page
scrolled sideways with the next-month arrow past the edge — measured 406px in
Chromium and 414 in Firefox at a 390px viewport, and the same at 320px. The row
wrapping is what holds it; the arrows and the wordmark may keep their sizes.

Read from the stylesheet for the reason tests/calendar/test_card_layout.py gives:
this checks the declarations the layout depends on, not the layout a browser
computes, and the browser measurement is what found it.
"""
from __future__ import annotations

import unittest

from tests.calendar.test_card_layout import declarations, value


class TheHeaderLeadFitsAPhoneTests(unittest.TestCase):
    def test_the_lead_row_wraps_rather_than_running_past_the_edge(self):
        self.assertEqual(value(declarations(".hero-lead"), "flex-wrap"), "wrap")

    def test_the_lead_row_may_be_narrower_than_its_content(self):
        """A flex item's automatic minimum is its content's minimum, and the
        header is itself a flex container — so without this the row could not
        be made narrower than the wordmark beside the whole month nav."""
        self.assertEqual(value(declarations(".hero-lead"), "min-width"), "0")


if __name__ == "__main__":
    unittest.main()
