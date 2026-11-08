"""app/calendar/day_pages.py — a big day drawn in pages, and the signed token its
"show the rest" control carries.

THE PROMISES: a day up to a page is drawn whole; past it the first page is the
day's best ranked cards — those more of the viewer's services list first, then
the more complete — drawn in the day's own order, with the card a jump is aimed
at among them wherever it ranks; a control's token reads back as exactly what it
named, for the account it was drawn for and nobody else, within its life, and an
edited one reads as nothing.
"""
from __future__ import annotations

import base64
import unittest
from datetime import date, datetime, timezone
from typing import NamedTuple

from app import db
from app.calendar import day_pages, query
from app.providers.base import Media, Record, Source
from app.sources import prefs as source_prefs
from tests.support import CardTestCase, read_views

REST = day_pages.Rest(7, "shows", date(2026, 10, 14), (0, 3, 4, 250, 611),
                      bytes(range(day_pages.DIGEST_BYTES)), "trakt", "newtoken123",
                      "pagetoken12", 1_793_700_000)


class _View(NamedTuple):
    """What the ranking reads of a card (query.View): who lists it, and how
    much of its title it shows."""
    listed: tuple
    complete: int = 0


def _day(count: int, **ranked) -> list[_View]:
    """A day of `count` cards, each listed by one service and showing nothing,
    but for `ranked` — {place: (services, complete)}."""
    out = [_View(("trakt",))] * count
    for at, (services, complete) in ((int(k[1:]), v) for k, v in ranked.items()):
        out[at] = _View(services, complete)
    return out


class TheFirstPageTests(unittest.TestCase):

    def test_a_day_up_to_a_page_is_drawn_whole(self):
        for count in (0, 1, day_pages.DAY_PAGE_CARDS):
            self.assertEqual(day_pages.first_page(_day(count)), tuple(range(count)))

    def test_past_a_page_cards_ranked_alike_go_in_the_days_order(self):
        self.assertEqual(day_pages.first_page(_day(612)),
                         tuple(range(day_pages.DAY_PAGE_CARDS)))

    def test_a_card_more_services_list_comes_first(self):
        """Every service, then all but one, then one alone."""
        three, two = ("tmdb", "trakt", "simkl"), ("tmdb", "trakt")
        day = _day(612)
        for at in range(500, 540):
            day[at] = _View(three)
        for at in range(300, 350):
            day[at] = _View(two)
        page = day_pages.first_page(day)
        self.assertEqual(len(page), day_pages.DAY_PAGE_CARDS)
        self.assertTrue(set(range(500, 540)) <= set(page))
        self.assertTrue(set(range(300, 350)) <= set(page))
        # The ten places left go to the single-service cards, in the day's order.
        self.assertEqual([at for at in page if at < 300], list(range(10)))

    def test_among_cards_listed_alike_the_more_complete_comes_first(self):
        day = _day(612)
        day[611] = _View(("trakt",), 9)
        day[400] = _View(("trakt",), 3)
        page = day_pages.first_page(day)
        self.assertIn(611, page)
        self.assertIn(400, page)
        self.assertNotIn(99, page)
        self.assertNotIn(98, page)

    def test_who_lists_it_outranks_how_complete_it_is(self):
        day = _day(612, p0=(("trakt",), 10))
        for at in range(1, 612):
            day[at] = _View(("tmdb", "trakt"), 0)
        self.assertNotIn(0, day_pages.first_page(day))

    def test_the_page_is_in_the_days_order(self):
        page = day_pages.first_page(_day(612, p600=(("tmdb", "trakt"), 0)))
        self.assertEqual(list(page), sorted(page))

    def test_the_jumps_card_is_drawn_wherever_it_ranks(self):
        self.assertEqual(day_pages.first_page(_day(612), 3), tuple(range(100)))
        page = day_pages.first_page(_day(612), 400)
        self.assertEqual(len(page), day_pages.DAY_PAGE_CARDS + 1)
        self.assertIn(400, page)


AIRS = datetime(2026, 10, 14, 1, tzinfo=timezone.utc).timestamp()


def _trakt(**fields) -> Record:
    values = dict(source=Source.TRAKT, media=Media.SHOW, id="the-show",
                  ids={"trakt": 1, "tmdb": 55}, detail_url="https://trakt.test/the-show",
                  title="The Show", air_ts=AIRS, season=2, episode_number=5)
    values.update(fields)
    return Record(**values)


def _simkl(**fields) -> Record:
    values = dict(source=Source.SIMKL, media=Media.SHOW, id="9", ids={"simkl": 9, "tmdb": 55},
                  detail_url="https://simkl.test/tv/9", title="The Show", air_ts=AIRS,
                  season=2, episode_number=5)
    values.update(fields)
    return Record(**values)


class TheReadCountsWhatACardShowsTests(CardTestCase):
    """What the first page ranks by, worked out for a big day alone
    (query.completeness, query.COUNTED_FACTS): how many of the title's facts
    the card shows — each one any of the viewer's own services that speaks for
    the card states — and who lists it (View.listed)."""

    def _view(self, *records, prefs=None):
        placed, viewer = read_views(list(records), prefs)
        (view,) = query.completeness(db.connection(), placed.views, viewer)
        return view

    def test_the_months_judging_counts_nothing(self):
        """Only a day over a page asks (`query.ranked`): the month's read never
        pays for the count."""
        placed, _viewer = read_views([_trakt(overview="Words.")])
        self.assertEqual([view.complete for view in placed.views], [0])

    def test_a_card_that_says_nothing_counts_nothing(self):
        self.assertEqual(self._view(_trakt()).complete, 0)

    def test_every_fact_a_card_shows_counts_once(self):
        view = self._view(_trakt(
            poster="https://p/x.jpg", overview="Words.", network="HBO", runtime=0,
            genres=["drama"], rating=8.1, certification="TV-MA", country="US",
            language="en", status="returning series"))
        self.assertEqual(view.complete, len(query.COUNTED_FACTS))

    def test_two_services_facts_count_once_each(self):
        view = self._view(_trakt(overview="Trakt's words."),
                          _simkl(overview="Simkl's words.", poster="https://p/s.jpg"))
        self.assertEqual(len(view.listed), 2)
        self.assertEqual(view.complete, 2)

    def test_a_service_the_viewer_does_not_read_counts_for_nothing(self):
        only_trakt = source_prefs.SourcePrefs(user_id=1, metadata_order=[],
                                              calendar_source="trakt",
                                              movie_calendar_source="trakt")
        view = self._view(_trakt(), _simkl(overview="Simkl's words."), prefs=only_trakt)
        self.assertEqual(len(view.listed), 1)
        self.assertEqual(view.complete, 0)


class TheControlsTokenTests(unittest.TestCase):

    def test_it_reads_back_as_what_it_named(self):
        self.assertEqual(day_pages.read(day_pages.token(REST), user_id=7, now=REST.issued_at),
                         REST)

    def test_a_page_of_a_big_day_stays_short(self):
        """The places go as gaps, so a hundred of them cost about a hundred
        bytes however far apart."""
        rest = REST._replace(drawn=tuple(range(0, 2000, 20)) + (2500,))
        text = day_pages.token(rest)
        self.assertEqual(day_pages.read(text, user_id=7, now=REST.issued_at), rest)
        self.assertLess(len(text), 300)

    def test_more_places_than_a_page_draws_read_as_nothing(self):
        rest = REST._replace(drawn=tuple(range(day_pages.DAY_PAGE_CARDS + 2)))
        self.assertIsNone(day_pages.read(day_pages.token(rest), user_id=7,
                                         now=REST.issued_at))

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

    def test_a_digest_that_is_not_a_days_is_never_sealed(self):
        with self.assertRaises(ValueError):
            day_pages.token(REST._replace(counted=b"short"))

    def test_a_picture_token_is_not_a_day_token(self):
        """Each kind of token is sealed with a key of its own purpose."""
        from app.metadata import pictures

        report = pictures.Report(pictures.Title("show", 1, "", None), "trakt", None,
                                 pictures.digest("https://x/p.jpg"), ("trakt",),
                                 REST.issued_at)
        self.assertIsNone(day_pages.read(pictures.token(report), user_id=7,
                                         now=REST.issued_at))


class _Card(NamedTuple):
    title_key: str
    unit: str


class _Placed(NamedTuple):
    card: _Card


def _cards(*names: str) -> list[_Placed]:
    return [_Placed(_Card(*name.split("|"))) for name in names]


class TheDaysDigestTests(unittest.TestCase):
    """What a press checks its places against (`cards_digest`): the day's
    cards by their rows in the title store, in the day's order."""

    def test_the_same_day_digests_the_same(self):
        self.assertEqual(day_pages.cards_digest(_cards("show:tmdb:1|S1E1", "show:tmdb:2|film")),
                         day_pages.cards_digest(_cards("show:tmdb:1|S1E1", "show:tmdb:2|film")))
        self.assertEqual(len(day_pages.cards_digest([])), day_pages.DIGEST_BYTES)

    def test_a_card_added_taken_off_or_moved_changes_it(self):
        day = day_pages.cards_digest(_cards("a|S1E1", "b|S1E1", "c|S1E1"))
        for changed in (("a|S1E1", "b|S1E1"), ("a|S1E1", "b|S1E1", "c|S1E1", "d|S1E1"),
                        ("b|S1E1", "a|S1E1", "c|S1E1"), ("a|S1E1", "b|S1E2", "c|S1E1")):
            with self.subTest(changed=changed):
                self.assertNotEqual(day_pages.cards_digest(_cards(*changed)), day)

    def test_a_name_is_not_read_across_its_halves(self):
        self.assertNotEqual(day_pages.cards_digest(_cards("ab|c")),
                            day_pages.cards_digest(_cards("a|bc")))


if __name__ == "__main__":
    unittest.main()
