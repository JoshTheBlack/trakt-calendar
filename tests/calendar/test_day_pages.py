"""app/calendar/day_pages.py — a big day drawn in pages, and the signed token its
"show the rest" control carries.

THE PROMISES: a day up to a page is drawn whole, and past it the first page — or
as far as the card a jump is aimed at; a control's token reads back as exactly
what it named, for the account it was drawn for and nobody else, within its life,
and an edited one reads as nothing.
"""
from __future__ import annotations

import base64
import unittest
from datetime import date

from app.calendar import day_pages

REST = day_pages.Rest(7, "shows", date(2026, 10, 14), 100, "trakt", "newtoken123",
                      "pagetoken12", 1_793_700_000)


class TheFirstPageTests(unittest.TestCase):

    def test_a_day_up_to_a_page_is_drawn_whole(self):
        for count in (0, 1, day_pages.DAY_PAGE_CARDS):
            self.assertEqual(day_pages.first_page(count), count)

    def test_past_a_page_the_first_page_is_drawn(self):
        self.assertEqual(day_pages.first_page(612), day_pages.DAY_PAGE_CARDS)

    def test_the_jumps_card_is_drawn_wherever_it_falls(self):
        self.assertEqual(day_pages.first_page(612, 3), day_pages.DAY_PAGE_CARDS)
        self.assertEqual(day_pages.first_page(612, 400), 401)


class TheControlsTokenTests(unittest.TestCase):

    def test_it_reads_back_as_what_it_named(self):
        self.assertEqual(day_pages.read(day_pages.token(REST), user_id=7, now=REST.issued_at),
                         REST)

    def test_another_accounts_token_reads_as_nothing(self):
        self.assertIsNone(day_pages.read(day_pages.token(REST), user_id=8,
                                         now=REST.issued_at))

    def test_a_token_past_its_life_reads_as_nothing(self):
        self.assertIsNone(day_pages.read(
            day_pages.token(REST), user_id=7,
            now=REST.issued_at + day_pages.TOKEN_SECONDS + 1))

    def test_an_edited_token_reads_as_nothing(self):
        text = day_pages.token(REST)
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        for at in range(len(raw)):
            with self.subTest(at=at):
                edited = bytearray(raw)
                edited[at] ^= 0x01
                spelled = base64.urlsafe_b64encode(bytes(edited)).rstrip(b"=").decode()
                self.assertIsNone(day_pages.read(spelled, user_id=7, now=REST.issued_at))

    def test_a_picture_token_is_not_a_day_token(self):
        """Each kind of token is sealed with a key of its own purpose."""
        from app.metadata import pictures

        report = pictures.Report(pictures.Title("show", 1, "", None), "trakt", None,
                                 pictures.digest("https://x/p.jpg"), ("trakt",),
                                 REST.issued_at)
        self.assertIsNone(day_pages.read(pictures.token(report), user_id=7,
                                         now=REST.issued_at))
