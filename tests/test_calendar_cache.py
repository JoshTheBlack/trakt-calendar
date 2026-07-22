"""Unit tests for the global calendar cache and its read path (app/calendar_cache,
app/calendar_filter).

Covers: window alignment is stable across viewers (independent of "today"); the
viewer-dependent month boundary (an item at 02:00 UTC on the 1st lands in the
previous month for a UTC-8 viewer and the current month for a UTC+2 viewer); the
pruner keeps every field the normalizer reads; a window fetch sends no
genres/countries and no pagination headers; TTL freshness; the size cap evicts
least-recently-stored first; and the GOLDEN FIXTURE proving the read-time
genre/country predicate reproduces Trakt's own server-side filtering under both
spec styles.

No network — the Trakt fetch is patched. TRAKT_DATA_DIR points at a temp dir
(set BEFORE importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_calendar_cache -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-calcache-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cache, calendar_cache, calendar_filter, db, trakt  # noqa: E402
from app.config import Settings  # noqa: E402
from app.endpoints import ENDPOINTS, get_endpoint  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
FIXTURES = Path(__file__).resolve().parent / "fixtures"

SHOWS = get_endpoint("shows")


class _Resp:
    def __init__(self, data, status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._data


class _CaptureClient:
    """A stand-in httpx client that records the last request and replies with a
    canned body."""
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}
        self.url = None
        self.sent_headers = None

    async def get(self, url, headers=None):
        self.url = url
        self.sent_headers = headers or {}
        return _Resp(self.body, headers=self.headers)


class CacheTestCase(unittest.IsolatedAsyncioTestCase):
    _counter = 0

    async def asyncSetUp(self):
        CacheTestCase._counter += 1
        db.set_db_path(TMP / f"calcache-{CacheTestCase._counter}.db")
        await db.migrate()
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()


# ---------------------------------------------------------------------------
# window alignment
# ---------------------------------------------------------------------------

class WindowAlignmentTests(unittest.TestCase):
    def test_window_start_is_a_multiple_of_seven_days_from_the_epoch(self):
        for d in (date(2026, 7, 1), date(2026, 7, 4), date(2001, 1, 1), date(2019, 12, 31)):
            start = calendar_cache.window_start(d)
            self.assertLessEqual(start, d)
            self.assertGreater(start + timedelta(days=7), d)
            self.assertEqual((start - calendar_cache._EPOCH).days % 7, 0)

    def test_alignment_is_independent_of_the_viewer(self):
        """Two viewers asking about the same calendar day resolve to the same
        window row — the alignment is anchored to a fixed epoch, not to today."""
        a = calendar_cache.window_start(date(2026, 9, 15))
        b = calendar_cache.window_start(date(2026, 9, 15))
        self.assertEqual(a, b)
        # Every day in a window maps to that same start.
        starts = {calendar_cache.window_start(date(2026, 9, d)) for d in range(14, 21)}
        self.assertEqual(len(starts), 1)

    def test_aligned_windows_cover_the_range_inclusively(self):
        windows = calendar_cache.aligned_windows(date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual(windows[0], calendar_cache.window_start(date(2026, 7, 1)))
        self.assertLessEqual(windows[-1], date(2026, 7, 31))
        self.assertGreater(windows[-1] + timedelta(days=7), date(2026, 7, 31))
        # Contiguous, 7 days apart, no gaps.
        for earlier, later in zip(windows, windows[1:]):
            self.assertEqual((later - earlier).days, 7)


# ---------------------------------------------------------------------------
# pruning
# ---------------------------------------------------------------------------

class PruneTests(unittest.TestCase):
    RICH = {
        "first_aired": "2026-07-15T20:00:00.000Z",
        "episode": {"season": 2, "number": 5, "title": "The One", "overview": "dropped"},
        "show": {
            "title": "Rich Show", "year": 2026, "network": "HBO", "country": "us",
            "language": "en", "runtime": 50, "status": "returning series", "rating": 8.456,
            "genres": ["drama", "game-show"], "overview": "An overview.",
            "ids": {"slug": "rich-show", "trakt": 123, "tvdb": 456, "tmdb": 789,
                    "imdb": "tt42", "unused": "x"},
            "images": {"poster": ["img.tmdb.example/poster.jpg"],
                       "fanart": ["fan.jpg"], "logo": ["logo.png"]},
            "unused_field": "dropped",
        },
    }

    def test_pruner_keeps_every_field_the_normalizer_reads(self):
        """The strongest possible statement of the pruner's contract: a raw entry
        and its pruned form normalize to the byte-identical Item."""
        tz = ZoneInfo("America/New_York")
        pruned = calendar_cache.prune_entry(self.RICH, "show")
        self.assertEqual(
            trakt.normalize(self.RICH, SHOWS, tz),
            trakt.normalize(pruned, SHOWS, tz),
        )

    def test_pruner_drops_the_bulky_unused_fields(self):
        pruned = calendar_cache.prune_entry(self.RICH, "show")
        self.assertNotIn("fanart", pruned["show"]["images"])
        self.assertNotIn("logo", pruned["show"]["images"])
        self.assertNotIn("imdb", pruned["show"]["ids"])
        self.assertNotIn("unused_field", pruned["show"])

    def test_pruner_drops_an_entry_with_no_media(self):
        self.assertIsNone(calendar_cache.prune_entry({"first_aired": "2026-01-01T00:00:00Z"}, "show"))


# ---------------------------------------------------------------------------
# fetch shape — no genres/countries, no pagination headers
# ---------------------------------------------------------------------------

class FetchShapeTests(CacheTestCase):
    async def test_window_fetch_sends_no_filters_and_no_pagination_headers(self):
        client = _CaptureClient([PruneTests.RICH])
        # The window RICH's 2026-07-15 air date actually belongs to — a fetch now
        # trims what falls outside the window it asked for.
        with patch("app.trakt.shared_client", return_value=client):
            entries = await calendar_cache.fetch_window_raw(SHOWS, self.settings, date(2026, 7, 13))
        self.assertNotIn("genres", client.url)
        self.assertNotIn("countries", client.url)
        self.assertNotIn("X-Pagination-Page", client.sent_headers)
        self.assertNotIn("X-Pagination-Limit", client.sent_headers)
        # And what comes back is pruned, not raw.
        self.assertEqual(len(entries), 1)
        self.assertNotIn("unused_field", entries[0]["show"])

    async def test_pagination_header_on_a_calendar_response_is_logged(self):
        client = _CaptureClient([], headers={"x-pagination-page-count": "3"})
        with patch("app.trakt.shared_client", return_value=client):
            with self.assertLogs("app.calendar_cache", level="WARNING") as logged:
                await calendar_cache.fetch_window_raw(SHOWS, self.settings, date(2026, 7, 6))
        self.assertTrue(any("pagination" in m.lower() for m in logged.output))


class WindowOverrunTests(CacheTestCase):
    """Trakt does not honour the `days` bound it is given.

    Measured live against the real API: `/calendars/all/shows/2026-06-29/7` came
    back carrying entries through 2026-07-11, and the 2026-07-13 window carried
    entries through 2026-09-05. Consecutive windows therefore overlap, and a
    month read that concatenated them rendered 207 duplicate cards for July 2026
    — two "House of the Dragon S03E03"s on the 5th, and so on.
    """

    async def test_a_window_keeps_only_its_own_seven_days(self):
        body = [
            _entry("day-before", "2026-07-05T12:00:00Z"),    # the previous window's
            _entry("first-day", "2026-07-06T12:00:00Z"),
            _entry("last-day", "2026-07-12T23:00:00Z"),
            _entry("day-after", "2026-07-13T00:30:00Z"),     # the next window's
            _entry("months-later", "2026-09-05T12:00:00Z"),  # the real overrun
        ]
        client = _CaptureClient(body)
        with patch("app.trakt.shared_client", return_value=client):
            entries = await calendar_cache.fetch_window_raw(SHOWS, self.settings, date(2026, 7, 6))
        self.assertEqual([e["show"]["ids"]["slug"] for e in entries],
                         ["first-day", "last-day"])

    async def test_adjacent_windows_no_longer_both_claim_the_same_airing(self):
        """The boundary case the trim exists for: whichever window Trakt hands an
        airing to, exactly one window keeps it."""
        shared = _entry("house-of-the-dragon", "2026-07-06T01:00:00Z", season=3, number=3)
        client = _CaptureClient([shared])
        with patch("app.trakt.shared_client", return_value=client):
            earlier = await calendar_cache.fetch_window_raw(SHOWS, self.settings, date(2026, 6, 29))
            owning = await calendar_cache.fetch_window_raw(SHOWS, self.settings, date(2026, 7, 6))
        self.assertEqual(earlier, [])
        self.assertEqual(len(owning), 1)

    async def test_a_read_over_already_overlapping_cached_windows_still_dedupes(self):
        """Windows cached BEFORE the trim existed overlap, and would keep drawing
        doubled cards until their TTL ran out. The read path deduplicates too, so
        the fix lands without anyone having to clear the cache."""
        airing = _entry("house-of-the-dragon", "2026-07-06T01:00:00Z", season=3, number=3)
        now = 1_800_000_000
        for start in (date(2026, 6, 29), date(2026, 7, 6)):
            await calendar_cache.store_window(SHOWS.key, start, [airing], 600, now)

        items, _ = await calendar_cache.read_month(
            SHOWS, self.settings, tz=ZoneInfo("UTC"), year=2026, month=7,
            allow_fetch=False, now=now,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["episode_label"], "S03E03")

    async def test_the_request_is_the_documented_shape_for_every_endpoint(self):
        """/calendars/{target}/{path}/{start_date}/{days}. The overrun is Trakt's
        behaviour, not a malformed request, so this pins the URL we send."""
        for key, endpoint in ENDPOINTS.items():
            with self.subTest(endpoint=key):
                client = _CaptureClient([])
                with patch("app.trakt.shared_client", return_value=client):
                    await calendar_cache.fetch_window_raw(endpoint, self.settings, date(2026, 7, 6))
                path, _, query = client.url.partition("?")
                self.assertTrue(
                    path.endswith(f"/calendars/all/{endpoint.path}/2026-07-06/7"), path)
                self.assertIn("extended=full", query)

    async def test_movies_are_trimmed_on_released_not_first_aired(self):
        """A movie entry carries `released`, not `first_aired`. Movies came back
        inside their window when measured, but that is one small dataset rather
        than a promise, so the trim has to be able to read their date at all."""
        movies = get_endpoint("movies")
        body = [
            {"released": "2026-07-08", "movie": {"title": "In Range", "ids": {"slug": "in-range", "trakt": 1}}},
            {"released": "2026-07-30", "movie": {"title": "Overrun", "ids": {"slug": "overrun", "trakt": 2}}},
        ]
        client = _CaptureClient(body)
        with patch("app.trakt.shared_client", return_value=client):
            entries = await calendar_cache.fetch_window_raw(movies, self.settings, date(2026, 7, 6))
        self.assertEqual([e["movie"]["title"] for e in entries], ["In Range"])

    async def test_two_different_episodes_of_one_show_are_not_confused(self):
        """Dedup keys on the airing, not the show — a show legitimately appears
        many times in a month."""
        entries = [
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=9, number=7),
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=9, number=7),  # repeat
            _entry("rick-and-morty", "2026-07-07T01:00:00Z", season=9, number=8),
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=0, number=76),  # a special
        ]
        kept = calendar_cache.dedupe_entries(entries, "show")
        self.assertEqual([(e["episode"]["season"], e["episode"]["number"]) for e in kept],
                         [(9, 7), (9, 8), (0, 76)])


# ---------------------------------------------------------------------------
# read path — TTL, allow_fetch, month boundary
# ---------------------------------------------------------------------------

def _entry(slug, first_aired, genres=None, country="us", season=1, number=1):
    return {
        "first_aired": first_aired,
        "episode": {"season": season, "number": number, "title": f"{slug} ep"},
        "show": {
            "title": slug, "ids": {"slug": slug, "trakt": abs(hash(slug)) % 100000},
            "genres": genres or [], "country": country,
        },
    }


class ReadPathTests(CacheTestCase):
    async def test_ttl_expiry_triggers_a_refetch(self):
        self.settings.calendar_cache_ttl_minutes = 10
        fetch = AsyncMock(side_effect=[
            [_entry("first", "2026-07-06T12:00:00Z")],
            [_entry("second", "2026-07-06T12:00:00Z")],
        ])
        with patch("app.calendar_cache.fetch_window_raw", fetch):
            entries, cached_at = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), now=1000)
            self.assertEqual(entries[0]["show"]["ids"]["slug"], "first")
            # Within TTL: served from cache, no second fetch.
            entries, _ = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), now=1000 + 9 * 60)
            self.assertEqual(entries[0]["show"]["ids"]["slug"], "first")
            self.assertEqual(fetch.call_count, 1)
            # Past TTL: refetched.
            entries, _ = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), now=1000 + 11 * 60)
            self.assertEqual(entries[0]["show"]["ids"]["slug"], "second")
            self.assertEqual(fetch.call_count, 2)

    async def test_public_read_never_fetches_and_serves_what_is_cached(self):
        # Nothing cached, fetch disabled -> empty, and Trakt is never asked.
        fetch = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.calendar_cache.fetch_window_raw", fetch):
            entries, cached_at = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), allow_fetch=False)
        self.assertEqual(entries, [])
        self.assertIsNone(cached_at)
        fetch.assert_not_awaited()

    async def test_public_read_serves_stale_cache_without_refetching(self):
        self.settings.calendar_cache_ttl_minutes = 10
        first = AsyncMock(return_value=[_entry("cached", "2026-07-06T12:00:00Z")])
        with patch("app.calendar_cache.fetch_window_raw", first):
            await calendar_cache.load_window(SHOWS, self.settings, date(2026, 7, 6), now=1000)
        # Long past the TTL, but a public read must serve the stale copy, not fetch.
        never = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.calendar_cache.fetch_window_raw", never):
            entries, cached_at = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), allow_fetch=False, now=10 ** 9)
        self.assertEqual(entries[0]["show"]["ids"]["slug"], "cached")
        self.assertEqual(cached_at, 1000)

    async def _read_boundary(self, tz_name, year, month):
        """read_month for a single item airing 2026-03-01T02:00Z, in tz_name."""
        target_window = calendar_cache.window_start(date(2026, 3, 1))

        async def fake(endpoint, settings, start):
            if start == target_window:
                return [_entry("boundary", "2026-03-01T02:00:00Z")]
            return []

        with patch("app.calendar_cache.fetch_window_raw", side_effect=fake):
            items, _ = await calendar_cache.read_month(
                SHOWS, self.settings, tz=ZoneInfo(tz_name), year=year, month=month)
        return {i["id"] for i in items}

    async def test_month_boundary_is_the_viewers(self):
        # 02:00 UTC on 1 Mar is 18:00 28 Feb in Los_Angeles (UTC-8, pre-DST) ...
        self.assertIn("boundary", await self._read_boundary("America/Los_Angeles", 2026, 2))
        self.assertNotIn("boundary", await self._read_boundary("America/Los_Angeles", 2026, 3))
        # ... and 04:00 1 Mar in Athens (UTC+2).
        self.assertIn("boundary", await self._read_boundary("Europe/Athens", 2026, 3))
        self.assertNotIn("boundary", await self._read_boundary("Europe/Athens", 2026, 2))

    async def test_read_month_reports_the_oldest_window_as_of(self):
        async def fake(endpoint, settings, start):
            return []
        with patch("app.calendar_cache.fetch_window_raw", side_effect=fake):
            _, as_of = await calendar_cache.read_month(
                SHOWS, self.settings, tz=ZoneInfo("UTC"), year=2026, month=7, now=555)
        self.assertEqual(as_of, 555)


# ---------------------------------------------------------------------------
# eviction — TTL sweep and the size-cap LRU
# ---------------------------------------------------------------------------

class EvictionTests(CacheTestCase):
    async def _insert(self, key, cached_at, byte_size, ttl_seconds=None):
        await db.execute(
            "INSERT INTO api_cache (cache_key, payload, cached_at, ttl_seconds, byte_size) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, b"x", cached_at, ttl_seconds, byte_size),
        )

    async def _keys(self):
        rows = await db.fetch_all("SELECT cache_key FROM api_cache")
        return {r["cache_key"] for r in rows}

    async def test_ttl_sweep_drops_expired_and_keeps_fresh_and_unttld(self):
        now = 1_000_000
        grace = cache.TTL_GRACE_SECONDS
        await self._insert("expired", cached_at=now - 600 - grace - 1, byte_size=10, ttl_seconds=600)
        await self._insert("fresh", cached_at=now - 60, byte_size=10, ttl_seconds=600)
        await self._insert("no-ttl", cached_at=0, byte_size=10, ttl_seconds=None)
        await cache.sweep(now=now, max_bytes=None)
        self.assertEqual(await self._keys(), {"fresh", "no-ttl"})

    async def test_size_cap_evicts_least_recently_stored_first(self):
        # Three 100-byte entries, oldest to newest; cap fits two.
        await self._insert("oldest", cached_at=100, byte_size=100)
        await self._insert("middle", cached_at=200, byte_size=100)
        await self._insert("newest", cached_at=300, byte_size=100)
        await cache.sweep(now=10 ** 9, max_bytes=200)
        self.assertEqual(await self._keys(), {"middle", "newest"})

    async def test_size_cap_leaves_everything_under_budget(self):
        await self._insert("a", cached_at=100, byte_size=100)
        await self._insert("b", cached_at=200, byte_size=100)
        await cache.sweep(now=10 ** 9, max_bytes=10_000)
        self.assertEqual(await self._keys(), {"a", "b"})


# ---------------------------------------------------------------------------
# the detail-lookup cache (app/cache) round trips through api_cache
# ---------------------------------------------------------------------------

class DetailCacheTests(CacheTestCase):
    async def test_get_set_round_trip_and_ttl(self):
        # cache.get/set are async: the read, the decompress and the write all
        # happen on a db worker thread, never on the event loop.
        await cache.set("http://x/y", {"hello": ["world", 1, 2]})
        self.assertEqual(await cache.get("http://x/y", ttl_seconds=3600), {"hello": ["world", 1, 2]})
        # ttl<=0 is an explicit always-miss.
        self.assertIsNone(await cache.get("http://x/y", ttl_seconds=0))
        # A missing key is a miss, not an error.
        self.assertIsNone(await cache.get("http://nope", ttl_seconds=3600))


# ---------------------------------------------------------------------------
# GOLDEN FIXTURE — the predicate reproduces Trakt's server-side filtering
# ---------------------------------------------------------------------------

class GoldenFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((FIXTURES / "calendar_filter_golden.json").read_text(encoding="utf-8"))

    def _kept_slugs(self, spec):
        kept = calendar_filter.filter_entries(
            self.fixture["entries"], self.fixture["media_key"], spec["genres"], spec["countries"])
        return {e["show"]["ids"]["slug"] for e in kept}

    def test_reproduces_trakt_exclude_style_filtering(self):
        self.assertEqual(
            self._kept_slugs(self.fixture["exclude_spec"]),
            set(self.fixture["expected_exclude"]),
        )

    def test_reproduces_trakt_include_style_filtering(self):
        self.assertEqual(
            self._kept_slugs(self.fixture["include_spec"]),
            set(self.fixture["expected_include"]),
        )


class FilterEdgeCaseTests(unittest.TestCase):
    """The live sample barely covered empty genres / empty country, so pin them
    down explicitly against the predicate."""
    def test_empty_genres_kept_by_exclude_only_dropped_by_include(self):
        no_genres = {"genres": [], "country": "us"}
        g_inc, g_exc = calendar_filter.parse_spec("-anime,-music")
        self.assertTrue(calendar_filter.keep_media(no_genres, g_inc, g_exc, set(), set()))
        # A genre INCLUDE spec has something to be a member of; an item with no
        # genres is a member of nothing, so it drops.
        gi_inc, gi_exc = calendar_filter.parse_spec("drama,comedy")
        self.assertFalse(calendar_filter.keep_media(no_genres, gi_inc, gi_exc, set(), set()))

    def test_missing_country_kept_by_exclude_dropped_by_allowlist(self):
        no_country = {"genres": ["drama"], "country": ""}
        c_inc, c_exc = calendar_filter.parse_spec("-kr")
        self.assertTrue(calendar_filter.keep_media(no_country, set(), set(), c_inc, c_exc))
        ai_inc, ai_exc = calendar_filter.parse_spec("us,gb,jp")
        self.assertFalse(calendar_filter.keep_media(no_country, set(), set(), ai_inc, ai_exc))

    def test_no_spec_is_a_pass_through(self):
        entries = [{"show": {"genres": ["anime"], "country": "kr"}}]
        self.assertEqual(calendar_filter.filter_entries(entries, "show", "", ""), entries)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
