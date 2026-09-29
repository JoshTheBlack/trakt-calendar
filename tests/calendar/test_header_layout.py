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


class APhoneHeaderKeepsItsIconsTests(unittest.TestCase):
    """On a phone the header's buttons drop their words (`.pill-text`, hidden by
    style.css below 560px) and the month shortens to three letters, so it fits on
    the logo's line. MEASURED in Chromium and Firefox: 241px of header at 390 and
    320 before, 117 after, no sideways scroll. What a template must keep for that
    is here: both spellings of the month, and every button that loses its word
    still named by it."""

    def test_the_month_is_drawn_both_ways(self):
        from tests.support import APP_DIR
        page = (APP_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<span class="month-full">{{ month_label }}</span>'
                      '<span class="month-abbr" aria-hidden="true">{{ month_label[:3] }}</span>',
                      page)

    def test_the_calendar_picker_says_one_word_and_keeps_the_full_name(self):
        """Two rows on a phone needs the picker to say one word, and option text
        cannot follow the screen width, so it says the short name everywhere —
        with the full name as each option's title, and everywhere else a calendar
        is named."""
        from app.endpoints import ENDPOINTS
        from tests.support import APP_DIR
        page = (APP_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('title="{{ ep.label }}"', page)
        self.assertIn(">{{ ep.short or ep.label }}</option>", page)
        for endpoint in ENDPOINTS.values():
            with self.subTest(endpoint=endpoint.key):
                self.assertTrue(endpoint.short)
                self.assertNotIn(" ", endpoint.short)

    def test_a_button_that_loses_its_word_keeps_its_name(self):
        import re

        from tests.support import APP_DIR
        for name in ("index.html", "_nav.html"):
            page = (APP_DIR / "templates" / name).read_text(encoding="utf-8")
            for button in re.findall(r"<button[^>]*>[^<]*<span class=\"pill-text\">", page):
                with self.subTest(template=name, button=button[:60]):
                    self.assertIn("aria-label=", button)


if __name__ == "__main__":
    unittest.main()
