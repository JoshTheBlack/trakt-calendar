"""app/calendar/completion.py — the calendar's titles matched across services in the
background, each question asked of the one service that can answer it, at that
service's pace.

What can break silently here, one class each: which cards are asked about (only
TMDB's own, or a title with no TMDB id; never one asked this week, a panel's ask
included); how much a pass may spend (the Trakt requests it sends in a minute,
nearest day first, and a bounded number of cards whatever they cost);
that only the one service is asked (another's allowance is not spent
uncounted); and that a service which could not be asked is left alone, with
nothing remembered for it.

No network: both services' lookups are stand-ins, and writing Trakt's own airing
(calendar/search.py's fill_one_gap, tested where it lives) is stubbed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import cache, db, http_pool
from app.calendar import entries as calendar_entries
from app.calendar import completion
from app.calendar import search as calendar_search
from app.config import Settings
from app.metadata import identity as title_identity
from app.providers.base import Media, Record, Source, SourceUnavailable
from app.providers.trakt import transport as trakt_transport
from tests.support import DatabaseTestCase

TMDB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJtYXRjaGluZyJ9.c2lnbmF0dXJl"
TODAY = date(2026, 10, 15)
WEEK = date(2026, 10, 12)


class _Port:
    """A service's lookup by shared id: answers from a table, or cannot be asked.
    `cost` is how many requests each lookup sends, counted where a real one's
    would be — the open tally, under the service's name."""

    def __init__(self, name, answers=None, *, down=False, cost=0):
        self.name = name
        self.answers = answers or {}
        self.down = down
        self.cost = cost
        self.asked: list[dict] = []

    async def find_by_shared_id(self, settings, media, ids):
        self.asked.append(dict(ids))
        sent = http_pool._tally.get()
        if sent is not None and self.cost:
            sent[self.name] = sent.get(self.name, 0) + self.cost
        if self.down:
            raise SourceUnavailable("down")
        for name in ("tmdb", "tvdb"):
            if (name, str(ids.get(name))) in self.answers:
                return dict(self.answers[(name, str(ids.get(name)))])
        return {}


def _at(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()


def _premiere(source, own_id, ids, day, *, season=2, title="A Premiere") -> Record:
    return Record(source=source, media=Media.SHOW, id=str(own_id), ids=ids,
                  detail_url=f"https://{source}.test/{own_id}", title=title,
                  air_ts=_at(day), date_only=True, season=season, episode_number=1,
                  episode_label=f"S{season:02d}E01")


class MatchingTestCase(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.settings = Settings(tmdb_api_key=TMDB_TOKEN, trakt_client_id="cid")
        self.tmdb = _Port("tmdb")
        self.trakt = _Port("trakt")
        lookup = patch("app.providers.for_shared_id_lookup",
                       side_effect=lambda settings: [(Source.TMDB, self.tmdb),
                                                     (Source.TRAKT, self.trakt)])
        lookup.start()
        self.addCleanup(lookup.stop)
        self.fill = AsyncMock(return_value=[])
        filling = patch.object(calendar_search, "fill_one_gap", self.fill)
        filling.start()
        self.addCleanup(filling.stop)
        # What the rest of the app has sent Trakt lately — nothing, unless a
        # test says otherwise.
        self.app_sent = 0
        spend = patch.object(http_pool, "sent_within", lambda name, seconds: self.app_sent)
        spend.start()
        self.addCleanup(spend.stop)

    async def store(self, records, *, endpoint="shows/premieres", week=WEEK):
        sources = sorted({str(r.source) for r in records})
        await calendar_entries.store_span(
            endpoint, week, week + timedelta(days=7), records, sources=sources,
            asked=sources, now=1000, stale_after=10**9)

    async def run_pass(self, today=TODAY):
        return await completion.run(self.settings, today=today)


class TraktIsAskedAboutWhatOnlyTmdbListsTests(MatchingTestCase):
    async def test_a_tmdb_only_premiere_is_asked_of_trakt_by_its_tmdb_id(self):
        await self.store([_premiere(Source.TMDB, 900, {"tmdb": 900}, date(2026, 10, 14))])
        self.trakt.answers = {("tmdb", "900"): {"trakt": 99, "trakt_slug": "a-premiere",
                                                "tmdb": 900}}
        self.fill.return_value = [object()]
        self.assertEqual(await self.run_pass(), 1)
        self.assertEqual(self.trakt.asked, [{"tmdb": 900}])
        self.assertEqual(self.tmdb.asked, [])
        _settings, = self.fill.await_args.args
        kwargs = self.fill.await_args.kwargs
        self.assertEqual((kwargs["source"], kwargs["source_id"], kwargs["season"],
                          kwargs["exact_season"]), (Source.TRAKT, "99", 2, True))
        known = await title_identity.known("show:tmdb:900")
        self.assertEqual(known.ids["trakt"], 99)

    async def test_an_answer_is_remembered_under_the_panels_own_key(self):
        """"Not listed" is an answer: the next pass, and a panel's open, ask
        nobody this week."""
        await self.store([_premiere(Source.TMDB, 900, {"tmdb": 900}, date(2026, 10, 14))])
        await self.run_pass()
        self.assertIsNotNone(await cache.get(
            calendar_search.asked_key("trakt", "shows/premieres", "show:tmdb:900", 2), 60))
        await self.run_pass()
        self.assertEqual(len(self.trakt.asked), 1)
        self.assertEqual(await calendar_search.other_service_to_ask(
            self.settings, endpoint_key="shows/premieres", title_key="show:tmdb:900",
            season=2), "")

    async def test_a_card_a_panel_asked_about_this_week_is_not_asked_again(self):
        await self.store([_premiere(Source.TMDB, 900, {"tmdb": 900}, date(2026, 10, 14))])
        await cache.set(calendar_search.asked_key("trakt", "shows/premieres",
                                                  "show:tmdb:900", 2), 1)
        self.assertEqual(await self.run_pass(), 0)
        self.assertEqual(self.trakt.asked, [])

    async def test_a_card_another_service_lists_too_is_not_asked(self):
        await self.store([
            _premiere(Source.TMDB, 900, {"tmdb": 900}, date(2026, 10, 14)),
            _premiere(Source.SIMKL, 55, {"simkl": 55, "tmdb": 900}, date(2026, 10, 14))])
        self.assertEqual(await self.run_pass(), 0)
        self.assertEqual(self.trakt.asked, [])

    async def test_a_listing_outside_the_span_still_counts(self):
        """Simkl dating the premiere three months on still lists it, so the card
        is not TMDB's alone."""
        await self.store([_premiere(Source.TMDB, 900, {"tmdb": 900}, date(2026, 10, 14))])
        await self.store([_premiere(Source.SIMKL, 55, {"simkl": 55, "tmdb": 900},
                                    date(2027, 1, 13))], week=date(2027, 1, 11))
        self.assertEqual(await self.run_pass(), 0)

    async def test_a_card_outside_this_month_and_the_next_waits_for_its_panel(self):
        await self.store([_premiere(Source.TMDB, 900, {"tmdb": 900}, date(2026, 12, 2))],
                         week=date(2026, 11, 30))
        self.assertEqual(await self.run_pass(), 0)


class APassIsPacedTests(MatchingTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.days = [TODAY + timedelta(days=offset) for offset in (12, -10, 1, 7, -3, 3, 5, -1)]
        # One fill per week, as the calendar stores them: a second fill of a week
        # replaces what the first wrote.
        weeks: dict[date, list[Record]] = {}
        for number, day in enumerate(self.days):
            weeks.setdefault(day - timedelta(days=day.weekday()), []).append(
                _premiere(Source.TMDB, 900 + number, {"tmdb": 900 + number}, day))
        for week, records in weeks.items():
            await self.store(records, week=week)

    # Requests each card sends: enough that one pass's reads run out partway
    # through the eight cards.
    COST = completion.TRAKT_READS_PER_MINUTE // 4 + 1

    def per_pass(self) -> int:
        """Cards a pass asks when each sends COST requests: until the minute's
        reads are spent, the last card allowed to run over."""
        return -(-completion.TRAKT_READS_PER_MINUTE // self.COST)

    async def test_a_pass_spends_trakt_reads_up_to_its_minutes_allowance_nearest_day_first(self):
        self.trakt.cost = self.COST
        asked = self.per_pass()
        self.assertLess(asked, len(self.days), "the fixture has more cards than one pass")
        self.assertEqual(await self.run_pass(), asked)
        nearest = sorted(range(len(self.days)), key=lambda n: abs((self.days[n] - TODAY).days))
        self.assertEqual([ids["tmdb"] for ids in self.trakt.asked],
                         [900 + n for n in nearest[:asked]])

    async def test_the_next_pass_asks_the_rest(self):
        self.trakt.cost = self.COST
        await self.run_pass()
        self.assertEqual(await self.run_pass(), len(self.days) - self.per_pass())
        self.assertEqual(len({ids["tmdb"] for ids in self.trakt.asked}), len(self.days))

    async def test_a_card_the_cache_answers_costs_no_reads(self):
        """Nothing sent, nothing spent: one pass asks about every card."""
        self.assertEqual(await self.run_pass(), len(self.days))

    async def test_a_pass_takes_only_what_the_rest_of_the_app_left_under_the_ceiling(self):
        self.trakt.cost = 7
        self.app_sent = completion.TRAKT_APP_CEILING - 10
        self.assertEqual(await self.run_pass(), 2, "ten reads left: two cards of seven")

    async def test_a_pass_sends_nothing_once_the_app_is_at_the_ceiling(self):
        self.app_sent = completion.TRAKT_APP_CEILING
        self.assertEqual(await self.run_pass(), 0)
        self.assertEqual(self.trakt.asked, [])

    async def test_a_pass_weighs_a_bounded_number_of_cards_whatever_they_cost(self):
        with patch.object(completion, "TRAKT_CARDS_PER_PASS", 3):
            self.assertEqual(await self.run_pass(), 3)

    async def test_the_tally_counts_trakts_requests_under_the_services_own_name(self):
        """The pass reads its spend as `sent["trakt"]`, which is only Trakt's if
        Trakt's pool is declared under that name."""
        self.assertEqual(trakt_transport.POOL.name, str(Source.TRAKT))


class AServiceThatCannotBeAskedTests(MatchingTestCase):
    async def test_trakt_down_is_remembered_as_nothing_and_left_alone(self):
        await self.store([_premiere(Source.TMDB, 900, {"tmdb": 900}, date(2026, 10, 14)),
                          _premiere(Source.TMDB, 901, {"tmdb": 901}, date(2026, 10, 15))])
        self.trakt.down = True
        self.assertEqual(await self.run_pass(), 0)
        self.assertEqual(len(self.trakt.asked), 1, "a pass stops at the first failure")
        self.assertIsNone(await cache.get(
            calendar_search.asked_key("trakt", "shows/premieres", "show:tmdb:901", 2), 60))
        await self.run_pass()
        self.assertEqual(len(self.trakt.asked), 1, "a resting service is not asked")
        self.trakt.down = False
        completion._resting_until.clear()
        self.assertEqual(await self.run_pass(), 2)


class TmdbIsAskedAboutATitleWithNoTmdbIdTests(MatchingTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.store([_premiere(Source.SIMKL, 77, {"simkl": 77, "tvdb": 4242},
                                    date(2026, 10, 14), title="Under Her Rules")])
        self.tmdb.answers = {("tvdb", "4242"): {"tmdb": 555, "tvdb": 4242}}

    async def test_an_answer_re_keys_the_title_and_its_airing(self):
        self.assertEqual(await self.run_pass(), 1)
        self.assertEqual(self.tmdb.asked[0]["tvdb"], 4242)
        self.assertEqual(self.trakt.asked, [], "Trakt is not asked on TMDB's pace")
        self.assertEqual(await db.fetch_value("SELECT title_key FROM calendar_airings"),
                         "show:tmdb:555")

    async def test_it_is_asked_once_a_week(self):
        await self.run_pass()
        self.tmdb.answers = {}
        await db.execute("DELETE FROM titles")
        await db.execute("DELETE FROM title_aliases")
        await db.execute("UPDATE calendar_airings SET title_key = 'show:tvdb:4242'")
        await self.run_pass()
        self.assertEqual(len(self.tmdb.asked), 1)

    async def test_tmdb_down_is_remembered_as_nothing(self):
        self.tmdb.down = True
        self.assertEqual(await self.run_pass(), 0)
        completion._resting_until.clear()
        self.tmdb.down = False
        self.assertEqual(await self.run_pass(), 1)


class TheSpanTests(DatabaseTestCase):
    def test_it_is_this_month_and_the_next(self):
        self.assertEqual(completion.span(date(2026, 10, 28)),
                         (date(2026, 10, 1), date(2026, 12, 1)))
        self.assertEqual(completion.span(date(2026, 11, 2)),
                         (date(2026, 11, 1), date(2027, 1, 1)))
        self.assertEqual(completion.span(date(2026, 12, 31)),
                         (date(2026, 12, 1), date(2027, 2, 1)))
