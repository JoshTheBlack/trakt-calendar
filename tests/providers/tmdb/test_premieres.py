"""app/providers/tmdb/premieres.py — TMDB's Season Premieres, derived behind the
request.

THE PROMISES:
  - THE DERIVATION: shows on air this week, less those on air in each of the
    four weeks before, each candidate confirmed by its own `/tv/{id}` season
    list — specials never count, and a candidate with no season premiering in
    the week is not a premiere.
  - EACH WEEK'S AIRED SET IS KEPT AND REUSED: the week after asks TMDB only for
    its own listing.
  - IT NEVER RUNS ON A REQUEST: a span with no derivation raises SourcePending at
    once, with the work started; asking twice starts it once; spans are worked
    out one at a time.
  - STALE IS SERVED AND RENEWED BEHIND IT; a failure reaches whoever waits.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from app import cache, db
from app.providers.base import Media, SourcePending
from app.providers.tmdb import premieres, transport
from tests.providers.tmdb.test_calendar import SETTINGS, FakeTmdb
from tests.support import DatabaseTestCase

WEEK = date(2026, 10, 26)
SPAN = premieres.Span.of(WEEK, 7)


def on_air(tmdb_id: int, day: date) -> dict:
    """A discover/tv entry listed for having an episode on `day` (`air_date`)."""
    return {"id": tmdb_id, "name": f"Show {tmdb_id}", "_air_date": day.isoformat()}


def title(tmdb_id: int, seasons: list[tuple[int, str]]) -> dict:
    """A `/tv/{id}` answer as detail.title_payload asks for it."""
    return {"id": tmdb_id, "name": f"Show {tmdb_id}", "first_air_date": "2020-01-01",
            "external_ids": {"tvdb_id": 9000 + tmdb_id}, "networks": [{"name": "HBO"}],
            "status": "Returning Series", "genres": [{"id": 18, "name": "Drama"}],
            "origin_country": ["US"], "original_language": "en",
            "seasons": [{"season_number": n, "air_date": day, "episode_count": 8}
                        for n, day in seasons]}


class _Stored(FakeTmdb):
    """FakeTmdb that answers a `cache_only` read of a title only once something
    has fetched it — as the response cache does."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stored: set[str] = set()

    async def __call__(self, settings, path, params=None, **kw):
        if kw.get("cache_only") and not path.startswith("discover/") and path not in self.stored:
            return None
        answer = await super().__call__(settings, path, params, **kw)
        self.stored.add(path)
        return answer


def week_of_tv() -> _Stored:
    """Four shows on air in the week of 26 October 2026: one on air the week
    before too (not back from a break), one on air three weeks before, and two
    back — of which only one has a season premiering this week."""
    return _Stored(
        listing={"tv": [on_air(1, WEEK), on_air(2, WEEK + timedelta(days=1)),
                        on_air(3, WEEK + timedelta(days=2)), on_air(4, WEEK + timedelta(days=3)),
                        on_air(1, WEEK - timedelta(days=3)), on_air(2, WEEK - timedelta(days=20))]},
        titles={"tv/3": title(3, [(1, "2024-01-05"), (2, "2026-10-28")]),
                "tv/4": title(4, [(0, "2026-10-29"), (1, "2025-03-01")])})


class _PremieresTestCase(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tmdb = week_of_tv()
        patch = mock.patch.object(transport, "cached_get", self.tmdb)
        patch.start()
        self.addCleanup(patch.stop)


class TheDerivationTests(_PremieresTestCase):

    async def test_a_show_back_from_a_break_with_a_season_premiering_is_a_premiere(self):
        found = await premieres.derive(SETTINGS, SPAN)
        self.assertEqual(found, [(3, 2, "2026-10-28")])
        asked = {path for path, _params in self.tmdb.asked if path.startswith("tv/")}
        self.assertEqual(asked, {"tv/3", "tv/4"}, "only the shows back from a break")

    async def test_what_it_found_is_kept_for_the_fill_to_read(self):
        await premieres.derive(SETTINGS, SPAN)
        held = await cache.get_stale(premieres._key(premieres._PREMIERES, SPAN))
        self.assertEqual(held["premieres"], [[3, 2, "2026-10-28"]])

    async def test_the_aired_sets_are_kept_and_the_next_week_reuses_them(self):
        """A week's "before" is the four weeks behind it, each of which was a
        week of its own: the week after asks TMDB for its own listing alone."""
        await premieres.derive(SETTINGS, SPAN)
        self.tmdb.asked.clear()
        await premieres.derive(SETTINGS, premieres.Span.of(WEEK + timedelta(days=7), 7))
        spans = {(first, last) for first, last, _page in self.tmdb.pages_asked("tv")}
        self.assertEqual(spans, {("2026-11-02", "2026-11-08")})

    async def test_a_tmdb_that_cannot_be_asked_fails_the_derivation_rather_than_shortening_it(self):
        self.tmdb.down = True
        with self.assertRaises(transport.TmdbError):
            await premieres.derive(SETTINGS, SPAN)
        self.assertIsNone(await cache.get_stale(premieres._key(premieres._PREMIERES, SPAN)))


class NeverOnARequestTests(_PremieresTestCase):

    async def test_a_span_nothing_has_worked_out_is_pending_at_once(self):
        """The listing is held shut: if the window waited for the derivation,
        this would never return."""
        gate = asyncio.Event()
        listing = self.tmdb.__call__

        async def _held(settings, path, params=None, **kw):
            if path.startswith("discover/"):
                await gate.wait()
            return await listing(settings, path, params, **kw)

        with mock.patch.object(transport, "cached_get", _held):
            with self.assertRaises(SourcePending) as raised:
                await asyncio.wait_for(premieres.window(SETTINGS, WEEK, 7), 1)
            self.assertEqual(premieres.pending(), 1)
            gate.set()
            self.assertEqual(await raised.exception.ready, 1)
        self.assertEqual(premieres.pending(), 0)
        records = await premieres.window(SETTINGS, WEEK, 7)
        self.assertEqual([(r.id, r.season, r.episode_label) for r in records],
                         [("3", 2, "S02E01")])
        record = records[0]
        self.assertTrue(record.date_only)
        self.assertTrue(record.enriched, "built from the title's own answer, it is complete")
        self.assertEqual((record.network, record.ids["tvdb"]), ("HBO", 9003))

    async def test_asking_twice_works_it_out_once(self):
        futures = []
        for _ in range(2):
            with self.assertRaises(SourcePending) as raised:
                await premieres.window(SETTINGS, WEEK, 7)
            futures.append(raised.exception.ready)
        self.assertIs(futures[0], futures[1])
        await futures[0]
        self.assertEqual(sum(1 for path, _ in self.tmdb.asked if path == "tv/3"), 1)

    async def test_spans_are_worked_out_one_at_a_time_in_the_order_asked(self):
        later = premieres.Span.of(WEEK + timedelta(days=7), 7)
        first = premieres.work(SETTINGS, SPAN)
        second = premieres.work(SETTINGS, later)
        await asyncio.gather(first, second)
        weeks = [first_day for first_day, _last, page in self.tmdb.pages_asked("tv") if page == 1]
        self.assertLess(weeks.index("2026-10-26"), weeks.index("2026-11-02"))

    async def test_a_failure_reaches_whoever_waits(self):
        self.tmdb.down = True
        with self.assertLogs("app.providers.tmdb.premieres", "WARNING"):
            with self.assertRaises(SourcePending) as raised:
                await premieres.window(SETTINGS, WEEK, 7)
            with self.assertRaises(transport.TmdbError):
                await raised.exception.ready
        self.assertEqual(premieres.pending(), 0)


class StaleIsServedTests(_PremieresTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await premieres.derive(SETTINGS, SPAN)
        self.tmdb.asked.clear()

    async def test_a_derivation_past_its_clock_answers_and_is_renewed_behind_it(self):
        later = db.now() + premieres._PAST_TTL_SECONDS + 1
        with mock.patch.object(db, "now", return_value=later):
            records = await premieres.window(SETTINGS, WEEK, 7)
            self.assertEqual([r.id for r in records], ["3"])
            self.assertEqual(premieres.pending(), 1, "nothing was renewed behind it")
            await premieres._waiting[SPAN].done

    async def test_a_current_one_asks_nobody(self):
        records = await premieres.window(SETTINGS, WEEK, 7)
        self.assertEqual([r.id for r in records], ["3"])
        self.assertEqual(premieres.pending(), 0)
        self.assertEqual([path for path, _ in self.tmdb.asked if path.startswith("discover/")], [])

    async def test_a_premiere_whose_title_answer_is_gone_is_worked_out_again(self):
        """A language change is part of every address, so a derivation can name
        titles whose answers the cache no longer holds. The fill does not look
        them up — that would put a few hundred requests back on it — and with
        none left to build, the span is pending again."""
        self.tmdb.stored.clear()
        with self.assertRaises(SourcePending) as raised:
            await premieres.window(SETTINGS, WEEK, 7)
        await raised.exception.ready
        self.assertEqual([r.id for r in await premieres.window(SETTINGS, WEEK, 7)], ["3"])


class ItsClocksTests(DatabaseTestCase):

    NOW = int(datetime(2026, 10, 28, 12, tzinfo=timezone.utc).timestamp())

    def test_a_week_still_running_or_just_behind_renews_on_tmdbs_clock(self):
        self.assertEqual(premieres._ttl(SPAN, self.NOW), transport.DEFAULT_TTL_SECONDS)
        self.assertEqual(premieres._ttl(premieres.Span.of(WEEK - timedelta(days=7), 7), self.NOW),
                         transport.DEFAULT_TTL_SECONDS)

    def test_a_week_well_behind_renews_weekly(self):
        self.assertEqual(premieres._ttl(premieres.Span.of(WEEK - timedelta(days=21), 7), self.NOW),
                         premieres._PAST_TTL_SECONDS)

    async def test_everything_it_keeps_is_held_to_tmdbs_ceiling(self):
        tmdb = week_of_tv()
        with mock.patch.object(transport, "cached_get", tmdb):
            await premieres.derive(SETTINGS, SPAN)
        rows = await db.fetch_all("SELECT cache_key, ttl_seconds FROM api_cache "
                                  "WHERE cache_key LIKE 'tmdb:%'")
        self.assertTrue(rows)
        self.assertTrue(all(row["ttl_seconds"] == transport.CACHE_RETAIN_SECONDS for row in rows))
