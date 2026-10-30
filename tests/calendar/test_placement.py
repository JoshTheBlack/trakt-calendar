"""One episode is one card, drawn on the day the viewer's own service gives it.

END TO END THROUGH THE REAL READ PATH — rows stored the way a fill stores them,
read back the way the calendar, a late day, a share page and the search read
them. The rule's pieces have their own tests (test_matching.py for which listings
are one card, test_precedence.py for which service describes it); what this file
holds is that every reader comes to THE SAME answer from the same rows, because a
reader that came to a different one would draw a card twice, or not at all, with
nothing on the page to say why.

THE FIXTURE IS YELLOWJACKETS SEASON 4 AS THE DEV INSTANCE HOLDS IT, plus a TMDB
listing of the premiere, as TMDB's Season Premieres gives it: Simkl dates S04E01
to Saturday 21 November 01:00 UTC (Friday 20:00 in New York), Trakt to Monday 23
November 02:00 UTC (Sunday 21:00), TMDB to the 22nd with no time. 23 November is
a Monday, so the two timed listings sit in different storage windows — which is
how it drew as two cards.

No network.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache
from app.calendar import resolve as calendar_resolve
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Media, Record, Source
from app.sources import prefs as source_prefs
from tests.support import migrated_db

SHOWS = get_endpoint("shows")
NEW_YORK = ZoneInfo("America/New_York")
EVERY_SOURCE = frozenset(str(source) for source in Source)


def _at(*parts) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def _listing(source: Source, season: int, episode: int, air_ts: float, *,
             date_only: bool = False) -> Record:
    name = str(source)
    # Simkl spells a tmdb id as a string and Trakt as an int; TMDB's own id IS
    # the tmdb id.
    ids = {"tmdb": "117488" if source is Source.SIMKL else 117488}
    if source is not Source.TMDB:
        ids[name] = f"{name}-yj"
    return Record(source=source, media=Media.SHOW, id=f"{name}-yj", ids=ids,
                  detail_url=f"https://{name}.test/yellowjackets", title="Yellowjackets",
                  air_ts=air_ts, date_only=date_only, season=season,
                  episode_number=episode, episode_label=f"S{season:02d}E{episode:02d}",
                  network="Showtime", enriched=True)


def yellowjackets(*, trakt_e1=_at(2026, 11, 23, 2)) -> list[Record]:
    return [
        _listing(Source.SIMKL, 4, 1, _at(2026, 11, 21, 1)),
        _listing(Source.TRAKT, 4, 1, trakt_e1),
        _listing(Source.TMDB, 4, 1, _at(2026, 11, 22), date_only=True),
        _listing(Source.SIMKL, 4, 2, _at(2026, 11, 28, 1)),
        _listing(Source.TRAKT, 4, 2, _at(2026, 11, 30, 2)),
    ]


def _order(*names) -> source_prefs.SourcePrefs:
    return source_prefs.SourcePrefs(user_id=1, metadata_order=list(names))


DEFAULT = _order()                                  # TMDB, Trakt, Simkl — declared
SIMKL_FIRST = _order("simkl", "trakt", "tmdb")
TMDB_THEN_SIMKL = _order("tmdb", "simkl", "trakt")


class PlacementTestCase(unittest.IsolatedAsyncioTestCase):
    """Rows stored window by window, as a fill stores them, and read back with no
    fetch allowed — so whatever a read draws came out of those rows.

    EVERY SOURCE IS ADMITTED AS THE INSTANCE ADMITS IT — the operator's half of
    admission (resolve.instance_sources) names every service that lists a
    calendar, TMDB among them — and nothing here stands in for it."""

    async def asyncSetUp(self):
        migrated_db(type(self).__name__.lower())
        self.settings = Settings()
        calendar_cache._forget_spans()
        self.assertEqual(calendar_resolve.instance_sources(self.settings), EVERY_SOURCE,
                         "the instance does not admit every service that lists a calendar")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def store(self, records: list[Record]) -> None:
        by_window: dict[date, list[Record]] = {}
        for record in records:
            start = calendar_cache.window_start(calendar_cache.record_utc_date(record))
            by_window.setdefault(start, []).append(record)
        for start, held in by_window.items():
            await calendar_cache.store_window(
                SHOWS.key, start, held, 10**9, 1000,
                sources=sorted({str(r.source) for r in held}),
                asked=sorted({str(r.source) for r in held}))

    async def cards(self, prefs, start: date, end: date, *, tz=NEW_YORK) -> list[tuple]:
        grouped, _meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=tz, start_date=start, end_date=end,
            prefs=prefs, allow_fetch=False, now=2000)
        return [(item.episode_label, item.air_date, item.air_time, str(item.source))
                for group in grouped for item in group["items"]]

    async def november(self, prefs) -> list[tuple]:
        return await self.cards(prefs, date(2026, 11, 1), date(2026, 11, 30))


class YellowjacketsTests(PlacementTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.store(yellowjackets())

    async def test_the_default_order_draws_each_episode_once_on_tmdbs_day_with_trakts_time(self):
        """TMDB describes it (first in the declared order) and gives the 22nd;
        Trakt's 02:00 UTC on the 23rd is the 22nd somewhere on earth, so its time
        is borrowed — 21:00 in New York. E02 has no TMDB listing: Trakt's day."""
        self.assertEqual(await self.november(DEFAULT), [
            ("S04E01", "2026-11-22", "21:00", "tmdb"),
            ("S04E02", "2026-11-29", "21:00", "trakt"),
        ])

    async def test_tmdb_describing_an_episode_it_does_not_list_moves_nothing(self):
        """TMDB lists S04E01 alone, which files its description of the show, and
        that description joins E02's card (cache.with_title_only). TMDB is first
        in this viewer's order, and E02 is still Trakt's card on Trakt's day and
        time — the description wins a field and nothing about the airing."""
        grouped, _meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=NEW_YORK, start_date=date(2026, 11, 29),
            end_date=date(2026, 11, 29), prefs=DEFAULT, allow_fetch=False, now=2000)
        item, = grouped[0]["items"]
        self.assertEqual((item.episode_label, item.air_date, item.air_time, str(item.source)),
                         ("S04E02", "2026-11-29", "21:00", "trakt"))
        self.assertEqual(item.field_sources["network"][0], "tmdb")
        self.assertEqual(sorted(item.listed_airings), ["simkl", "trakt"])

    async def test_a_simkl_first_viewer_sees_each_episode_once_on_simkls_day(self):
        self.assertEqual(await self.november(SIMKL_FIRST), [
            ("S04E01", "2026-11-20", "20:00", "simkl"),
            ("S04E02", "2026-11-27", "20:00", "simkl"),
        ])

    async def test_a_tmdb_first_viewer_gets_no_time_when_no_timed_listing_agrees(self):
        """Trakt dated to the 24th 02:00 UTC — the 23rd in New York, and not the
        22nd anywhere — and Simkl's 21st 01:00 UTC is not the 22nd anywhere
        either: TMDB's date stands, with no time. (Refilling Trakt's window
        replaces its listing there, as a real refill would.)"""
        await self.store([_listing(Source.TRAKT, 4, 1, _at(2026, 11, 24, 2))])
        cards = await self.november(TMDB_THEN_SIMKL)
        self.assertIn(("S04E01", "2026-11-22", "", "tmdb"), cards)
        self.assertEqual(sum(1 for card in cards if card[0] == "S04E01"), 1)

    async def test_two_viewers_read_one_span_and_each_sees_their_own_day(self):
        """The rows are shared and nothing per-viewer reaches them: each viewer's
        placement is computed from the same rows at read, in either order."""
        first = await self.november(SIMKL_FIRST)
        second = await self.november(DEFAULT)
        again = await self.november(SIMKL_FIRST)
        self.assertEqual(first, again)
        self.assertEqual([card[1] for card in first], ["2026-11-20", "2026-11-27"])
        self.assertEqual([card[1] for card in second], ["2026-11-22", "2026-11-29"])

    async def test_a_day_read_agrees_with_the_month(self):
        """A late day assembles its own span; it has to draw exactly the cards
        the month draws on that day — and none of a card the month draws on
        another day."""
        for prefs in (DEFAULT, SIMKL_FIRST):
            month = await self.november(prefs)
            for day in range(19, 31):
                on = date(2026, 11, day)
                with self.subTest(prefs=prefs.metadata_order, day=day):
                    self.assertEqual(await self.cards(prefs, on, on),
                                     [card for card in month if card[1] == on.isoformat()])

    async def test_a_viewer_east_of_greenwich_sees_the_borrowed_time_on_its_own_day(self):
        """The time borrowed is an instant, rendered in the viewer's zone — in
        Tokyo, Trakt's 02:00 UTC on the 23rd is 11:00 on the 23rd."""
        cards = await self.cards(DEFAULT, date(2026, 11, 1), date(2026, 11, 30),
                                 tz=ZoneInfo("Asia/Tokyo"))
        self.assertIn(("S04E01", "2026-11-23", "11:00", "tmdb"), cards)

    async def test_the_card_says_when_the_other_services_say_it_airs(self):
        grouped, _meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=NEW_YORK, start_date=date(2026, 11, 22),
            end_date=date(2026, 11, 22), prefs=DEFAULT, allow_fetch=False, now=2000)
        item, = grouped[0]["items"]
        self.assertEqual(item.air_choices, [("tmdb", "Sun 22 Nov"),
                                            ("trakt", "Sun 22 Nov 21:00"),
                                            ("simkl", "Fri 20 Nov 20:00")])


class AMonthsEdgeTests(PlacementTestCase):
    """A card two services date either side of a month's edge is drawn in ONE
    month — the one its describing service puts it in — and the other month's
    read, which holds the other listing, does not draw it as well."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.store([_listing(Source.TRAKT, 4, 1, _at(2026, 10, 30, 2)),
                          _listing(Source.SIMKL, 4, 1, _at(2026, 11, 3, 1))])

    async def test_each_order_draws_it_once_in_its_own_services_month(self):
        october = (date(2026, 10, 1), date(2026, 10, 31))
        for prefs, in_october, in_november in ((_order("trakt"), 1, 0),
                                               (SIMKL_FIRST, 0, 1)):
            with self.subTest(prefs=prefs.metadata_order):
                self.assertEqual(len(await self.cards(prefs, *october)), in_october)
                self.assertEqual(len(await self.november(prefs)), in_november)


class ASearchedListingDoesNotSplitTheFeedsCardTests(PlacementTestCase):
    """THE REPORTED FAULT, AS THE DEV INSTANCE HELD IT: East of Eden S01E01 on
    1 October — TMDB's date, Trakt's and Simkl's feeds at 07:00 UTC, and a
    date-only Simkl row a tracker add wrote while Simkl's feed did not list the
    premiere yet. In date order the searched Simkl row joined TMDB's card first,
    so Simkl's own feed listing had to start a second card: two cards, one with
    every service and one with Simkl alone, under one title (hiding one hid
    both). The searched row stands down for the feed (app/calendar/entries.py),
    and the episode is one card in every order."""

    async def asyncSetUp(self):
        from app.calendar import entries as calendar_entries

        await super().asyncSetUp()
        self.searched = _listing(Source.SIMKL, 1, 1, _at(2026, 10, 1), date_only=True)
        await calendar_entries.store_loose_airings(SHOWS.key, [self.searched], now=500,
                                                   stale_after=10**9)
        await self.store([_listing(Source.TMDB, 1, 1, _at(2026, 10, 1), date_only=True),
                          _listing(Source.TRAKT, 1, 1, _at(2026, 10, 1, 7)),
                          _listing(Source.SIMKL, 1, 1, _at(2026, 10, 1, 7))])

    async def october(self, prefs):
        return await self.cards(prefs, date(2026, 10, 1), date(2026, 10, 31))

    async def test_the_episode_is_one_card_in_every_order(self):
        for prefs in (DEFAULT, SIMKL_FIRST, TMDB_THEN_SIMKL):
            with self.subTest(prefs=prefs.metadata_order):
                self.assertEqual(len(await self.october(prefs)), 1)

    async def test_the_default_order_draws_tmdbs_day_with_the_feeds_time(self):
        self.assertEqual(await self.october(DEFAULT),
                         [("S01E01", "2026-10-01", "03:00", "tmdb")])


class TheSearchAgreesWithTheCalendarTests(PlacementTestCase):
    """The calendar search's stored half says where a title airs; it is the
    calendar's own answer, one row per card, on the viewer's day."""

    async def test_where_it_airs_is_where_the_calendar_draws_it(self):
        from app.calendar import search as calendar_search

        await self.store(yellowjackets())
        for prefs, days in ((DEFAULT, ["2026-11-29", "2026-11-22"]),
                            (SIMKL_FIRST, ["2026-11-27", "2026-11-20"])):
            with self.subTest(prefs=prefs.metadata_order):
                found = await calendar_search.stored(
                    "yellowjackets", settings=self.settings, prefs={}, tz=NEW_YORK,
                    source_selection=prefs, marks=frozenset(), endpoints=[SHOWS])
                self.assertEqual([airing.day for airing in found.airings], days)


class TheNewerFiltersAgreeOnEveryReaderTests(PlacementTestCase):
    """Who lists a card, and what TMDB says of it, decide the same thing on the
    month, a late day and the search — every reader asks `cache.placed` with the
    specs `vocab.active_specs` answers, so none can apply one and not another."""

    SPECS = ({"tv_single_service": True}, {"tv_keywords": ["-anime"]},
             {"tv_sparse_no_network": True}, {"tv_show_types": "-reality"})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        lonely = _listing(Source.TMDB, 1, 1, _at(2026, 11, 10), date_only=True)
        lonely.id, lonely.ids, lonely.title, lonely.network = "9", {"tmdb": 9}, "Lonely", ""
        lonely.keywords, lonely.show_type = ["anime"], "reality"
        await self.store(yellowjackets() + [lonely])

    async def drawn(self, prefs: dict, start: date, end: date) -> list[str]:
        from app.calendar import vocab
        grouped, _meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=NEW_YORK, start_date=start, end_date=end,
            prefs=DEFAULT, allow_fetch=False, now=2000,
            **vocab.active_specs(prefs, Media.SHOW, honour_pause=True))
        return [item.title for group in grouped for item in group["items"]]

    async def test_the_month_a_day_and_the_search_hide_the_same_card(self):
        from app.calendar import search as calendar_search
        for prefs in self.SPECS:
            with self.subTest(prefs=prefs):
                month = await self.drawn(prefs, date(2026, 11, 1), date(2026, 11, 30))
                day = await self.drawn(prefs, date(2026, 11, 10), date(2026, 11, 10))
                found = await calendar_search.stored(
                    "lonely", settings=self.settings, prefs=prefs, tz=NEW_YORK,
                    source_selection=DEFAULT, marks=frozenset(), endpoints=[SHOWS])
                self.assertNotIn("Lonely", month)
                self.assertIn("Yellowjackets", month)
                self.assertEqual(day, [])
                self.assertEqual(found.airings, ())
        self.assertIn("Lonely", await self.drawn({}, date(2026, 11, 10), date(2026, 11, 10)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
