"""app/calendar/entries.py's retention sweep.

WHY THIS FILE EXISTS AT ALL: the calendar's rows left `api_cache` when they
stopped being blobs, and app/cache.py's sweep went with them. Nothing aged a
calendar row afterwards, which is a table that grows for ever rather than a
visible fault — the kind that is only ever found by looking.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from unittest.mock import AsyncMock, patch

from app import db
from types import SimpleNamespace

from app.calendar import cache as calendar_cache, entries as calendar_entries
from app.calendar import enrich as calendar_enrich
from app.providers.base import Media, Record, Source
from app.config import Settings
from app.endpoints import get_endpoint
from tests.support import migrated_db

SHOWS = "shows"
# 2026-07-06 12:00Z, the start of the aligned window these are stored under.
AIR = 1783425600.0


def _record(source_id: str, season: int, number: int, air_ts: float) -> Record:
    return Record(
        source=Source.TRAKT, media=Media.SHOW, id=source_id,
        ids={"trakt": abs(hash(source_id)) % 9999, "tmdb": abs(hash(source_id)) % 997},
        detail_url="u", title=source_id.title(), air_ts=air_ts,
        season=season, episode_number=number,
        episode_label=f"S{season:02d}E{number:02d}",
        episode_title=f"S{season:02d}E{number:02d} title")


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    """Six months from LAST STORE, and nothing still on the calendar goes."""

    async def asyncSetUp(self):
        migrated_db("calretention")
        self.old = 1_000_000
        self.new = self.old + calendar_entries.RETAIN_SECONDS + 1

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _store(self, records, *, start: date, now: int):
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(start), records, 600, now,
            sources=["trakt"], asked=["trakt"])

    async def _count(self, table: str) -> int:
        return await db.fetch_value(f"SELECT COUNT(*) FROM {table}")

    async def test_an_airing_nothing_has_restored_is_reclaimed(self):
        await self._store([_record("gone", 1, 1, AIR)], start=date(2026, 7, 6),
                          now=self.old)
        self.assertEqual(await self._count("calendar_airings"), 1)
        self.assertEqual(await calendar_entries.sweep(now=self.new), 1)
        self.assertEqual(await self._count("calendar_airings"), 0)
        self.assertEqual(await self._count("calendar_coverage"), 0)

    async def test_a_recently_stored_airing_survives(self):
        await self._store([_record("fresh", 1, 1, AIR)], start=date(2026, 7, 6),
                          now=self.new)
        self.assertEqual(await calendar_entries.sweep(now=self.new), 0)
        self.assertEqual(await self._count("calendar_airings"), 1)

    async def test_a_title_still_airing_keeps_its_row(self):
        """THE ORDERING CLAIM. A title deleted on age alone would strand the
        airings still pointing at it, and the read's LEFT JOIN would hand back a
        card with no name — worse than either keeping the row or dropping the
        airing with it."""
        await self._store([_record("still", 1, 1, AIR)], start=date(2026, 7, 6),
                          now=self.old)
        await self._store([_record("still", 5, 9, AIR + 7 * 86400)],
                          start=date(2026, 7, 13), now=self.new)
        await calendar_entries.sweep(now=self.new)
        self.assertEqual(await self._count("calendar_titles"), 1)
        records, *_ = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 13), date(2026, 7, 20))
        self.assertEqual([r.title for r in records], ["Still"])

    async def test_an_old_season_goes_while_the_show_goes_on(self):
        """The episode check is per COORDINATE. A series in its fifth season
        stops carrying rows for its first; matching on the title alone would
        have been shorter and would have grown without bound."""
        await self._store([_record("still", 1, 1, AIR)], start=date(2026, 7, 6),
                          now=self.old)
        await self._store([_record("still", 5, 9, AIR + 7 * 86400)],
                          start=date(2026, 7, 13), now=self.new)
        await calendar_entries.sweep(now=self.new)
        rows = await db.fetch_all(
            "SELECT season, number FROM calendar_episodes ORDER BY season")
        self.assertEqual([(r["season"], r["number"]) for r in rows], [(5, 9)])

    async def test_a_files_validator_does_not_outlive_its_rows(self):
        """An ETag whose entries are gone would answer "unchanged" for a month
        this app no longer holds anything about, and the 304 it earns would be a
        lie in the one direction that matters."""
        await db.execute(
            "INSERT INTO calendar_source_files (url, source, month, etag, fetched_at) "
            "VALUES ('https://data.simkl.in/calendar/2026/1/tv.json', 'simkl', "
            "'2026-01', 'W/\"abc\"', ?)", (self.old,))
        await calendar_entries.sweep(now=self.new)
        self.assertEqual(await self._count("calendar_source_files"), 0)

    async def test_the_sweep_is_a_no_op_on_an_empty_calendar(self):
        self.assertEqual(await calendar_entries.sweep(now=self.new), 0)


class ASourceReportingUnchangedTests(unittest.IsolatedAsyncioTestCase):
    """"Unchanged" is a third answer, and both wrong readings of it lose data.

    Read as ANSWERED, the fill deletes every airing the source holds — it
    returned none. Read as FAILED, the span goes partial and is refetched for
    ever. It is neither: the source has confirmed that what is stored is still
    its answer.
    """

    async def asyncSetUp(self):
        migrated_db("calunchanged")
        self.settings = Settings(simkl_public_calendar_enabled=False)

    async def asyncTearDown(self):
        db.close_thread_connection()

    RECORD = None

    def _record(self):
        return Record(source=Source.TRAKT, media=Media.SHOW, id="keepme",
                      ids={"trakt": 1, "tmdb": 2}, detail_url="u", title="Keep Me",
                      air_ts=AIR, season=1, episode_number=1)

    async def _fill(self, result, *, now, force=False):
        with patch("app.calendar.cache.fetch_window_records",
                   AsyncMock(return_value=result)):
            return await calendar_cache.load_window(
                get_endpoint("shows"), self.settings, date(2026, 7, 6),
                now=now, force=force)

    async def test_a_304_does_not_blank_the_month_it_just_confirmed(self):
        """THE WORST OUTCOME OF A SUCCESSFUL CONDITIONAL GET. The fetch brings no
        records, so the groups built from it are empty while the stored rows are
        perfectly good — serving those would empty the calendar on the very read
        that proved it was current."""
        await self._fill(([self._record()], ["trakt"], []), now=1000)
        window, _ = await self._fill(([], [], ["trakt"]), now=999_999, force=True)
        self.assertEqual([g["key"] for g in window.groups], ["show:tmdb:2|1|1"])
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM calendar_airings"), 1)

    async def test_it_does_not_read_as_partial(self):
        """A source that replied is not a source that was silent."""
        await self._fill(([self._record()], ["trakt"], []), now=1000)
        await self._fill(([], [], ["trakt"]), now=999_999, force=True)
        row = await db.fetch_one(
            "SELECT asked, answered FROM calendar_coverage WHERE source = 'trakt'")
        self.assertEqual((row["asked"], row["answered"]), (1, 1))

    async def test_the_schedule_still_advances(self):
        """Nothing changed, but the file WAS looked at — so the next scheduled
        pass must not treat the span as overdue and ask again immediately."""
        await self._fill(([self._record()], ["trakt"], []), now=1000)
        await self._fill(([], [], ["trakt"]), now=999_999, force=True)
        self.assertEqual(
            await calendar_entries.span_stored_at(
                "shows", calendar_cache.window_start(date(2026, 7, 6))),
            999_999)


class EpisodeLevelTests(unittest.IsolatedAsyncioTestCase):
    """Level 2 — the facts that genuinely vary per episode.

    THIS APP HAS NEVER HELD THEM. The modal shows a season's worth of episode
    facts today by stamping the SHOW's runtime and rating onto every one, which
    is wrong about every episode's rating and wrong about any show with a
    double-length finale. Measured across 4,330 (source, title) pairs with more
    than one airing, `episode_title` disagrees on 95.5% of them against 0.0-0.8%
    for the title-level fields — that gap is why this is its own level.
    """

    SETTINGS = SimpleNamespace(trakt_catalogue_configured=True)

    async def asyncSetUp(self):
        migrated_db("callevel2")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _store(self, *numbers, season=1, now=1000):
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("severance", season, n, AIR + n * 3600) for n in numbers],
            600, now, sources=["trakt"], asked=["trakt"])

    async def test_the_fill_leaves_a_stub_that_still_counts_as_owed(self):
        """A calendar feed names an episode and titles it, and that is all. A row
        that exists is not a row that has been looked up — reading "has a row" as
        "answered" would leave every episode with a title and no runtime for
        ever."""
        await self._store(1, 2)
        owed = await calendar_entries.owed_episodes("trakt", "trakt", 10, 3000)
        self.assertEqual(owed, [(abs(hash("severance")) % 9999, "show", 1)])

    async def test_a_lookup_fills_every_airing_of_the_season(self):
        await self._store(1, 2)
        trakt_id = abs(hash("severance")) % 9999
        written = await calendar_entries.store_episodes(
            "trakt", trakt_id, "show", 1,
            [{"number": 1, "title": "Chikhai Bardo", "runtime": 82, "rating": 9.1},
             {"number": 2, "title": "Sweet Vitriol", "runtime": 38, "rating": 8.4}],
            now=2000)
        self.assertEqual(written, 2)
        records, *_ = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual([(r.episode_title, r.runtime) for r in records],
                         [("Chikhai Bardo", 82), ("Sweet Vitriol", 38)])

    async def test_the_episode_runtime_beats_the_shows(self):
        """The point of the split: a double-length finale is not the series'
        nominal runtime, and stamping the show's figure on it is what the level-2
        row exists to stop."""
        await self._store(1)
        await db.execute("UPDATE calendar_titles SET runtime = 40, enriched = 1")
        trakt_id = abs(hash("severance")) % 9999
        await calendar_entries.store_episodes(
            "trakt", trakt_id, "show", 1, [{"number": 1, "runtime": 82}], now=2000)
        records, *_ = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual(records[0].runtime, 82)

    async def test_a_season_trakt_has_nothing_for_still_counts_as_answered(self):
        """Otherwise it is owed again on every pass for ever — the same
        starvation the enrichment drain's failure rows exist to prevent."""
        await self._store(1)
        trakt_id = abs(hash("severance")) % 9999
        await calendar_entries.store_episodes("trakt", trakt_id, "show", 1, [], now=2000)
        self.assertEqual(await calendar_entries.owed_episodes("trakt", "trakt", 10, 3000), [])

    async def test_an_empty_answer_does_not_blank_the_feeds_episode_title(self):
        """The fill already stored a title from the calendar; a season the lookup
        could not name must not erase it."""
        await self._store(1)
        trakt_id = abs(hash("severance")) % 9999
        await calendar_entries.store_episodes("trakt", trakt_id, "show", 1, [], now=2000)
        records, *_ = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual(records[0].episode_title, "S01E01 title")

    async def test_the_drain_asks_once_per_season_not_once_per_episode(self):
        """One request returns a whole season's episode list. Asking per episode
        would be one request per airing, which is the cost this level was
        designed not to pay."""
        await self._store(1, 2, 3, 4, 5)
        calls = []

        async def _fetch(settings, trakt_id, season, client=None, *,
                         only_if_cached=False):
            # Answering the free ask as well as the paid one: the drain asks
            # `only_if_cached` first, and a double that refused it would send
            # every season down the network path and count the seasons twice.
            calls.append((trakt_id, season))
            return [{"number": n, "runtime": 40 + n} for n in range(1, 6)]

        with patch("app.providers.trakt.detail.fetch_season_episodes", _fetch):
            written = await calendar_enrich.drain_episodes(self.SETTINGS, now=2000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(written, 5)

    async def test_an_unconfigured_instance_looks_nothing_up(self):
        await self._store(1)
        never = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.providers.trakt.detail.fetch_season_episodes", never):
            self.assertEqual(
                await calendar_enrich.drain_episodes(
                    SimpleNamespace(trakt_catalogue_configured=False), now=2000), 0)
        never.assert_not_awaited()


class ASiblingEndpointStillGetsItsRowsTests(unittest.IsolatedAsyncioTestCase):
    """THE BUG THIS EXISTS FOR, found in a browser and not by the suite.

    A conditional-GET validator is per FILE. The rows derived from it are per
    (endpoint, span). Three show endpoints — shows/new, shows/premieres and
    shows — all read the same two Simkl archives, so the FIRST of them to fill a
    month fetched the bodies and recorded the validators, and the other two then
    got a 304 for a span they had nothing stored for. They stored nothing, and
    kept storing nothing on every later pass, because the file went on being
    unchanged. Simkl silently vanished from two of the three TV calendars while
    still appearing on movies — which is exactly what it looked like.
    """

    async def asyncSetUp(self):
        migrated_db("calsibling")
        self.settings = Settings(trakt_client_id="")

    async def asyncTearDown(self):
        db.close_thread_connection()

    ENTRY = {"title": "A Show", "poster": "19/a", "date": "2026-07-08T20:00:00-04:00",
             "release_date": "2026-07-08",
             "ids": {"simkl_id": 1, "slug": "a-show", "tmdb": "111"},
             "url": "https://simkl.com/tv/1/a-show",
             "episode": {"season": 1, "episode": 1}}

    def _client(self):
        entry = self.ENTRY

        class _Resp:
            def __init__(self, data=None, status=200, headers=None):
                self._data, self.status_code, self.headers = data, status, headers or {}

            def json(self):
                return self._data

        class _Client:
            def __init__(self):
                self.conditional = 0

            async def get(self, url, headers=None, timeout=None):
                if (headers or {}).get("If-None-Match"):
                    self.conditional += 1
                    return _Resp(status=304)
                return _Resp([entry] if "tv.json" in url else [],
                             headers={"ETag": '"v1"'})

        return _Client()

    async def _fill(self, key, client, *, now, force=False):
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            await calendar_cache.load_window(
                get_endpoint(key), self.settings,
                calendar_cache.window_start(date(2026, 7, 8)), now=now, force=force)
        return await db.fetch_value(
            "SELECT COUNT(*) FROM calendar_airings WHERE endpoint = ? AND source = 'simkl'",
            (key,))

    async def test_every_show_endpoint_gets_its_own_rows(self):
        client = self._client()
        stored = [await self._fill(key, client, now=1000)
                  for key in ("shows/new", "shows/premieres", "shows")]
        self.assertEqual(stored, [1, 1, 1])
        # None of them may short-circuit: a span with no rows has nothing an
        # "unchanged" answer could preserve.
        self.assertEqual(client.conditional, 0)

    async def test_the_short_circuit_returns_once_the_rows_are_there(self):
        """The optimisation is not abandoned, only gated. A span that HAS rows
        revalidates and keeps them."""
        first = self._client()
        for key in ("shows/new", "shows/premieres", "shows"):
            await self._fill(key, first, now=1000)
        second = self._client()
        stored = [await self._fill(key, second, now=999_999, force=True)
                  for key in ("shows/new", "shows/premieres", "shows")]
        self.assertEqual(stored, [1, 1, 1])
        self.assertGreater(second.conditional, 0)


class ARefillDoesNotUndoTheEpisodeDrainTests(unittest.IsolatedAsyncioTestCase):
    """A FILL MAY NOT DEMOTE WHAT A LOOKUP LEARNED — the same rule as `enriched`
    one level up, and it was missing here.

    The fill writes `fetched_at = 0` on an episode row to mean "a stub, still
    owed a lookup". Assigning that on conflict pushed an ANSWERED episode back to
    owed every time its span refilled — and the current month refills daily, so
    the work never ended. Observed on a live instance: the episode drain reported
    25 seasons filled every minute indefinitely, each refresh handing back
    exactly what the last one had finished.
    """

    async def asyncSetUp(self):
        migrated_db("calrefilllevel2")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _fill(self, now):
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("severance", 3, 7, AIR)], 600, now,
            sources=["trakt"], asked=["trakt"])

    async def test_an_answered_episode_stays_answered_across_a_refill(self):
        await self._fill(1000)
        trakt_id = abs(hash("severance")) % 9999
        await calendar_entries.store_episodes(
            "trakt", trakt_id, "show", 3,
            [{"number": 7, "title": "Chikhai Bardo", "runtime": 82}], now=2000)
        self.assertEqual(await calendar_entries.owed_episodes("trakt", "trakt", 10, 3000), [])

        await self._fill(3000)          # the span refills, exactly as it does daily

        self.assertEqual(
            await calendar_entries.owed_episodes("trakt", "trakt", 10, 3000), [],
            "a refill put an answered episode back on the owed list")
        records, *_ = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual(records[0].episode_title, "Chikhai Bardo")
        self.assertEqual(records[0].runtime, 82)

    async def test_a_stub_is_still_owed_after_a_refill(self):
        """The other side: a fill that has never been answered stays owed, or the
        guard would have turned "do not demote" into "never look at all"."""
        await self._fill(1000)
        await self._fill(3000)
        self.assertEqual(len(await calendar_entries.owed_episodes("trakt", "trakt", 10, 3000)), 1)


class EpisodesFallDueAgainTests(unittest.IsolatedAsyncioTestCase):
    """Answering a season once is not answering it for ever.

    Episode facts are CORRECTED AFTER AIR at least as often as they are
    published before it: a mystery-box show ships "Episode 7" and a placeholder
    overview, and the real title arrives once people have watched. So the clock
    is keyed on when the episode AIRED, not on when it was fetched, and the week
    behind an airing is the fast tier rather than the first thing to go cold.
    """

    async def asyncSetUp(self):
        migrated_db("calduelevel2")

    async def asyncTearDown(self):
        db.close_thread_connection()

    def test_an_upcoming_episode_is_on_the_fast_tier(self):
        now = int(AIR) - 30 * 86400
        self.assertEqual(calendar_entries.episode_stale_after(AIR, now),
                         now + 86400)

    def test_the_week_after_air_is_still_the_fast_tier(self):
        now = int(AIR) + 3 * 86400
        self.assertEqual(calendar_entries.episode_stale_after(AIR, now),
                         now + 86400)

    def test_a_settled_episode_drops_to_the_slow_tier(self):
        now = int(AIR) + 30 * 86400
        self.assertEqual(calendar_entries.episode_stale_after(AIR, now),
                         now + 30 * 86400)

    def test_an_undated_episode_is_treated_as_upcoming(self):
        """The cheap direction to be wrong in: an airing the feed could not pin
        down is far likelier to be imminent than settled, and parking it on the
        slow tier would freeze a placeholder title for a month."""
        self.assertEqual(calendar_entries.episode_stale_after(None, 5000),
                         5000 + 86400)

    async def _fill(self, now):
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("severance", 3, 7, AIR)], 600, now,
            sources=["trakt"], asked=["trakt"])

    async def test_an_answered_season_comes_back_once_its_row_falls_due(self):
        await self._fill(int(AIR))
        trakt_id = abs(hash("severance")) % 9999
        answered_at = int(AIR) + 3600
        await calendar_entries.store_episodes(
            "trakt", trakt_id, "show", 3,
            [{"number": 7, "title": "Episode 7"}], now=answered_at)

        self.assertEqual(
            await calendar_entries.owed_episodes("trakt", "trakt", 10,
                                                 answered_at + 3600),
            [], "a season answered an hour ago was asked for again")

        due = await calendar_entries.owed_episodes(
            "trakt", "trakt", 10, answered_at + 2 * 86400)
        self.assertEqual(due, [(trakt_id, "show", 3)],
                         "an aired episode never came back for its correction")

    async def test_never_answered_seasons_take_the_batch_first(self):
        """With a fixed batch size the two compete, and letting re-reads go first
        would starve the first pass on a large calendar — a season nobody has
        looked up is showing placeholder facts to somebody right now."""
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("severance", 3, 7, AIR), _record("shrinking", 2, 4, AIR)],
            600, int(AIR), sources=["trakt"], asked=["trakt"])
        settled = abs(hash("severance")) % 9999
        fresh = abs(hash("shrinking")) % 9999
        await calendar_entries.store_episodes(
            "trakt", settled, "show", 3, [{"number": 7}], now=int(AIR))

        owed = await calendar_entries.owed_episodes(
            "trakt", "trakt", 1, int(AIR) + 2 * 86400)
        self.assertEqual(owed, [(fresh, "show", 2)])


class BacklogCountTests(unittest.IsolatedAsyncioTestCase):
    """The number a reader watches count down has to be the work being done.

    Both drains log only what they just DID, so "filled 121 episodes across 25
    seasons" reads identically whether 26 seasons remain or twenty thousand. A
    count that disagreed with the batch would tell a reader nothing except that
    one of the two was lying — which is why the two come from one query.
    """

    async def asyncSetUp(self):
        migrated_db("calbacklog")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_the_count_matches_what_the_batch_would_hand_out(self):
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record(name, 1, n, AIR + n * 3600)
             for name in ("severance", "shrinking", "silo") for n in (1, 2)],
            600, int(AIR), sources=["trakt"], asked=["trakt"])

        now = int(AIR) + 3600
        uncapped = await calendar_entries.owed_episodes("trakt", "trakt", 999, now)
        self.assertEqual(
            await calendar_entries.owed_season_count("trakt", "trakt", now),
            len(uncapped))
        self.assertEqual(len(uncapped), 3, "one row per season, not per airing")

    async def test_the_count_falls_as_the_drain_answers(self):
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("severance", 1, 1, AIR), _record("shrinking", 1, 1, AIR)],
            600, int(AIR), sources=["trakt"], asked=["trakt"])
        now = int(AIR) + 3600
        self.assertEqual(
            await calendar_entries.owed_season_count("trakt", "trakt", now), 2)

        await calendar_entries.store_episodes(
            "trakt", abs(hash("severance")) % 9999, "show", 1,
            [{"number": 1, "title": "Chikhai Bardo"}], now=now)

        self.assertEqual(
            await calendar_entries.owed_season_count("trakt", "trakt", now), 1)


class AirningsWrittenWithoutAFeedTests(unittest.IsolatedAsyncioTestCase):
    """`store_loose_airings` — a title the CALENDAR feed never mentioned.

    THE CASE IT EXISTS FOR. A service answers two datasets about one show and
    they disagree: Trakt's show record dates Half Man's first season to
    2026-04-28T20:00Z, and Trakt's premieres calendar for that week does not
    list it at all — its all-episodes calendar carries episode two and no
    episode one. The title is real and the date is the service's own; the feed
    this app is built from simply has a hole. A catalogue lookup already had to
    describe and date the title to draw a search result, so writing that as an
    airing fills the hole for nothing extra.
    """

    async def asyncSetUp(self):
        migrated_db("calloose")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _write(self, records, *, endpoint=SHOWS, now=1_000_000):
        return await calendar_entries.store_loose_airings(
            endpoint, records, now=now, stale_after=now + 86400)

    async def test_the_airing_reads_back_like_any_other(self):
        """WRITTEN AS THE FEED WOULD HAVE WRITTEN IT is the whole design: a row
        from this path must not be a special kind of row anybody downstream has
        to know about, or it would resolve, filter and render differently from
        the rows beside it."""
        written = await self._write([_record("halfman", 1, 1, AIR)])
        self.assertEqual(written, 1)
        back, _asked, _answered, _at = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual([r.title for r in back], ["Halfman"])
        self.assertEqual(back[0].air_ts, AIR)

    async def test_it_claims_no_coverage(self):
        """THE IMPORTANT RESTRAINT. Coverage records which sources were ASKED
        about a window and which ANSWERED. This asked nobody about a window, so
        writing coverage would tell the fill path a span had been fetched when
        it has not — and the month would stay permanently half empty because
        nothing would ever go and get the rest.
        """
        await self._write([_record("halfman", 1, 1, AIR)])
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM calendar_coverage"), 0)

    async def test_writing_the_same_airing_twice_is_one_row(self):
        """A viewer searching the same title twice must not double the card.
        The natural key is the feed's own, so the second write replaces."""
        await self._write([_record("halfman", 1, 1, AIR)])
        await self._write([_record("halfman", 1, 1, AIR)])
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM calendar_airings"), 1)

    async def test_a_fill_of_the_same_span_does_not_take_it_away(self):
        """THE WHOLE POINT, AND IT WAS BRIEFLY THE OPPOSITE. A fill REPLACES what
        a source holds for a span — delete, then insert what came back — because
        a title a source has stopped listing must stop being drawn. A repaired
        row is in that delete's path and not in the insert's, so it survived only
        until the window next refetched: measured at under seven days for the
        month Half Man sits in. The delete now spares rows no feed ever listed,
        which are exactly the rows it would never put back.
        """
        await self._write([_record("halfman", 1, 1, AIR)])
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("somethingelse", 1, 1, AIR)], 600, 2_000_000,
            sources=["trakt"], asked=["trakt"])
        back, _asked, _answered, _at = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual(sorted(r.title for r in back),
                         ["Halfman", "Somethingelse"])

    async def test_the_feed_takes_the_row_back_when_it_lists_the_title(self):
        """AND THE EXEMPTION ENDS THE MOMENT IT IS NOT NEEDED. A feed row for the
        SAME airing has the same natural key, so it overwrites the searched one
        and returns it to an ordinary row — after which the next fill may remove
        it like any other. Without this a title would be exempt for ever on the
        strength of one search, and a service that later dropped it could never
        take it off the calendar.
        """
        await self._write([_record("halfman", 1, 1, AIR)])
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("halfman", 1, 1, AIR)], 600, 2_000_000,
            sources=["trakt"], asked=["trakt"])
        flag = await db.fetch_value(
            "SELECT from_search FROM calendar_airings WHERE source_id = ?",
            ("halfman",))
        self.assertEqual(flag, 0, "a feed row did not reclaim the searched one")

        # ...and now an ordinary fill that no longer lists it removes it.
        await calendar_cache.store_window(
            SHOWS, calendar_cache.window_start(date(2026, 7, 6)),
            [_record("somethingelse", 1, 1, AIR)], 600, 3_000_000,
            sources=["trakt"], asked=["trakt"])
        back, _asked, _answered, _at = await calendar_entries.read_span(
            SHOWS, date(2026, 7, 6), date(2026, 7, 13))
        self.assertEqual([r.title for r in back], ["Somethingelse"])

    async def test_a_searched_row_is_still_reclaimed_by_retention(self):
        """IT IS AN EXEMPTION FROM THE FILL, NOT FROM AGEING. A title every
        service has genuinely dropped must not live on the calendar for ever on
        the strength of one search years ago."""
        await self._write([_record("halfman", 1, 1, AIR)], now=1_000_000)
        await calendar_entries.sweep(now=1_000_000 + calendar_entries.RETAIN_SECONDS + 1)
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM calendar_airings"), 0)

    async def test_nothing_to_write_touches_nothing(self):
        self.assertEqual(await self._write([]), 0)
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM calendar_airings"), 0)
