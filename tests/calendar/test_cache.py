"""Unit tests for the global calendar cache and its read path (app/calendar/cache.py,
app/calendar/filter.py).

Covers: window alignment is stable across viewers (independent of "today"); the
viewer-dependent month boundary (an item at 02:00 UTC on the 1st lands in the
previous month for a UTC-8 viewer and the current month for a UTC+2 viewer); the
stored window's shape and what a payload from an older shape does; a window fetch
sends no genres/countries and no pagination headers; the instance-wide content
floor (genres/countries/certifications) excludes a show from the cached window
itself, not just from a later read; TTL freshness; the size cap evicts
least-recently-stored first; and the GOLDEN FIXTURE proving the read-time
genre/country/certification predicate reproduces Trakt's own server-side
filtering under both spec styles.

No network — the Trakt fetch is patched.
"""
from __future__ import annotations

import json
import unittest
import zlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from app import cache, db
from app.calendar import cache as calendar_cache, filter as calendar_filter
from app.providers.trakt import TraktError
from app.providers.trakt import calendar as trakt_calendar
from app.config import Settings
from app.providers import base
from app.endpoints import ENDPOINTS, get_endpoint
from tests.support import FIXTURES, calendar_records, new_db_path, window_fetch


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

    async def get(self, url, headers=None, timeout=None):
        self.url = url
        self.sent_headers = headers or {}
        return _Resp(self.body, headers=self.headers)


class CacheTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("calcache")
        await db.migrate()
        self.settings = Settings()
        # This file is about the FILL AND READ PATH, exercised against a Trakt
        # response — it is not about Simkl, which is now a second admitted
        # source for every endpoint here (Capabilities.endpoints is no longer
        # empty; the fill asks it without checking is_configured, by design —
        # see app/providers/__init__.py's calendar_sources). Left unpatched,
        # every real fetch_window_records call in this file would reach
        # Simkl's live CDN, which the suite's network guard refuses. Simkl is
        # stubbed as UNREACHABLE rather than as an empty answer so it stays out
        # of `sources`/`answered`, matching what every assertion in this file
        # already expected before a second source existed.
        simkl_patcher = patch(
            "app.providers.simkl.calendar.fetch_window",
            AsyncMock(side_effect=base.SourceUnavailable("not exercised in this test")))
        simkl_patcher.start()
        self.addCleanup(simkl_patcher.stop)

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
# the stored shape
# ---------------------------------------------------------------------------

class StoredRecordTests(unittest.TestCase):
    RICH = {
        "first_aired": "2026-07-15T20:00:00.000Z",
        "episode": {"season": 2, "number": 5, "title": "The One", "overview": "dropped"},
        "show": {
            "title": "Rich Show", "year": 2026, "network": "HBO", "country": "us",
            "language": "en", "runtime": 50, "status": "returning series", "rating": 8.456,
            "genres": ["drama", "game-show"], "overview": "An overview.",
            "certification": "TV-14",
            "ids": {"slug": "rich-show", "trakt": 123, "tvdb": 456, "tmdb": 789,
                    "imdb": "tt42", "unused": "x"},
            "images": {"poster": ["img.tmdb.example/poster.jpg"],
                       "fanart": ["fan.jpg"], "logo": ["logo.png"]},
            "unused_field": "dropped",
        },
    }

    def record(self, entry=None, endpoint=SHOWS):
        return trakt_calendar.to_record(entry or self.RICH, endpoint)

    def test_a_record_survives_the_round_trip_through_storage_unchanged(self):
        """The strongest possible statement of the stored shape's contract: what
        comes back out of a window is the record that went in.

        No permitted gap. A field the card reads but the payload drops means the
        same show renders one way on a cache miss and another on a hit, which is
        a bug that only appears once a window has been stored — so it is caught
        here instead."""
        record = self.record()
        self.assertEqual(base.Record.from_dict(record.to_dict()), record)

    def test_a_record_carries_every_id_the_source_supplied(self):
        """The invariant behind the test above, stated directly: an id namespace
        the app declares and the source supplied must survive into the cache.
        Anything else is an id present on a miss and missing on a hit.

        There is no id whitelist any more, which is the point — the old pruner
        named the namespaces it kept, so a second service's own id would have
        been dropped on the way in and the matcher would simply never have
        matched."""
        supplied = {k for k in self.RICH["show"]["ids"] if k in base.ID_KEYS}
        self.assertEqual(set(self.record().ids), supplied)
        self.assertNotIn("unused", self.record().ids)

    def test_a_window_stored_before_a_field_existed_still_reads(self):
        """A stored record predating a newer Record field simply lacks the key.
        It must read back rather than raise, and the field must come back at its
        DEFAULT — nothing invalidates the cache when a field is added, so old
        rows keep being served until their TTL expires."""
        stored = self.record().to_dict()
        del stored["certification"]
        stored.pop("date_only", None)
        revived = base.Record.from_dict(stored)
        self.assertEqual(revived.certification, "")
        self.assertFalse(revived.date_only)
        self.assertEqual(revived.ids["tmdb"], 789)

    def test_a_record_missing_something_it_cannot_do_without_is_refused(self):
        """A row with no air time is not a record at all. Refusing it is what
        lets the reader treat the whole window as a miss rather than rendering a
        card at the epoch."""
        stored = self.record().to_dict()
        del stored["air_ts"]
        with self.assertRaises(ValueError):
            base.Record.from_dict(stored)

    def test_the_stored_form_omits_what_is_at_its_default(self):
        """Which is what makes the defaults above load-bearing rather than
        decorative: the ordinary record exercises them on every window."""
        stored = trakt_calendar.to_record(
            _entry("plain", "2026-07-06T12:00:00Z"), SHOWS).to_dict()
        self.assertNotIn("certification", stored)
        self.assertNotIn("language", stored)
        self.assertNotIn("genres", stored)
        self.assertNotIn("date_only", stored)
        self.assertIn("air_ts", stored)

    def test_a_record_keeps_certification_and_the_genre_slugs(self):
        """Both are filter inputs. The genres stay hyphenated and lowercase all
        the way into storage, because that is what the per-viewer genre spec
        matches against — a stored "Game Show" breaks every multi-word genre
        filter and leaves the single-word ones working."""
        record = self.record()
        self.assertEqual(record.certification, "TV-14")
        self.assertEqual(record.genres, ["drama", "game-show"])

    def test_an_entry_with_no_media_is_not_a_record(self):
        self.assertIsNone(
            trakt_calendar.to_record({"first_aired": "2026-01-01T00:00:00Z"}, SHOWS))


# ---------------------------------------------------------------------------
# fetch shape — no genres/countries, no pagination headers
# ---------------------------------------------------------------------------

class FetchShapeTests(CacheTestCase):
    async def test_window_fetch_sends_no_filters_and_no_pagination_headers(self):
        client = _CaptureClient([StoredRecordTests.RICH])
        # The window RICH's 2026-07-15 air date actually belongs to — a fetch now
        # trims what falls outside the window it asked for.
        with patch("app.providers.trakt.transport.shared_client", return_value=client):
            records, answered = await calendar_cache.fetch_window_records(
                SHOWS, self.settings, date(2026, 7, 13))
        self.assertNotIn("genres", client.url)
        self.assertNotIn("countries", client.url)
        self.assertNotIn("X-Pagination-Page", client.sent_headers)
        self.assertNotIn("X-Pagination-Limit", client.sent_headers)
        # And what comes back is a normalized record, not the payload.
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "Rich Show")
        self.assertEqual(answered, ["trakt"])

    async def test_the_fill_names_the_source_that_answered(self):
        """`sources` is what tells a later read that a source was asked and had
        nothing, rather than never asked at all."""
        client = _CaptureClient([])
        with patch("app.providers.trakt.transport.shared_client", return_value=client):
            _, answered = await calendar_cache.fetch_window_records(
                SHOWS, self.settings, date(2026, 7, 6))
        self.assertEqual(answered, ["trakt"])

    async def test_simkl_failing_during_a_fill_does_not_claim_it_had_nothing(self):
        """The class-wide Simkl stub raises SourceUnavailable by default — this
        makes that failure EXPLICIT rather than incidental: Trakt still answers,
        so the fill must succeed with `sources = ["trakt"]`, never
        `["trakt", "simkl"]` with an empty Simkl contribution. The two are not
        the same fact — the second would be stored and read back for a whole
        TTL as "Simkl genuinely had nothing here"."""
        client = _CaptureClient([StoredRecordTests.RICH])
        with patch("app.providers.trakt.transport.shared_client", return_value=client):
            records, answered = await calendar_cache.fetch_window_records(
                SHOWS, self.settings, date(2026, 7, 13))
        self.assertEqual(answered, ["trakt"])
        self.assertTrue(records)  # Trakt's own answer still comes through

    async def test_a_source_that_answers_no_endpoint_is_never_asked(self):
        """The fill asks capabilities, never a name. A source registered for
        something else entirely must not be called for a calendar it never
        claimed to have."""
        asked = []

        class _Silent:
            source = base.Source.SIMKL
            label = "Nobody"
            capabilities = base.Capabilities(
                endpoints=frozenset({"movies"}), days_before=None, days_after=None,
                private_user_data=False)

            class calendar_port:
                @staticmethod
                async def fetch_window(endpoint, settings, start, days):
                    asked.append(endpoint.key)
                    return []

            def is_configured(self, settings):
                return True

        client = _CaptureClient([])
        with patch("app.providers.calendar_sources", return_value=[_Silent()]):
            with patch("app.providers.trakt.transport.shared_client", return_value=client):
                _, answered = await calendar_cache.fetch_window_records(
                    SHOWS, self.settings, date(2026, 7, 6))
        self.assertEqual(asked, [])
        self.assertEqual(answered, [])

    async def test_pagination_header_on_a_calendar_response_is_logged(self):
        """Logged by the SOURCE, not by this module: the cache no longer asks
        Trakt for anything itself, so the one warning about a truncated window
        now lives with the one fetch — and a cache fill still surfaces it."""
        client = _CaptureClient([], headers={"x-pagination-page-count": "3"})
        with patch("app.providers.trakt.transport.shared_client", return_value=client):
            with self.assertLogs("app.providers.trakt.calendar", level="WARNING") as logged:
                await calendar_cache.fetch_window_records(SHOWS, self.settings, date(2026, 7, 6))
        self.assertTrue(any("pagination" in m.lower() for m in logged.output))


class SimklPublicCalendarSwitchTests(CacheTestCase):
    """simkl_public_calendar_enabled: whether an UNCONFIGURED Simkl's public
    calendar CDN is reached at all. Default settings (self.settings, plain
    Settings()) is what every other test in this file already exercises — this
    class pins the two states the switch actually changes rather than the
    fill's own shape, which the tests above already cover."""

    async def test_default_on_still_reaches_simkl(self):
        """The regression that matters most: an instance that never opens the
        Simkl tab keeps making the same request it always has."""
        simkl_mock = AsyncMock(side_effect=base.SourceUnavailable("stub"))
        client = _CaptureClient([StoredRecordTests.RICH])
        with patch("app.providers.simkl.calendar.fetch_window", simkl_mock):
            with patch("app.providers.trakt.transport.shared_client", return_value=client):
                records, answered = await calendar_cache.fetch_window_records(
                    SHOWS, self.settings, date(2026, 7, 13))
        simkl_mock.assert_awaited()
        self.assertEqual(answered, ["trakt"])
        self.assertTrue(records)  # Trakt's own answer still comes through

    async def test_switched_off_makes_no_simkl_request_and_still_renders(self):
        """Off means off: not a slower failure, no call placed at all — and the
        fill still succeeds from whatever source remains rather than erroring
        or coming back empty."""
        simkl_mock = AsyncMock(side_effect=base.SourceUnavailable("must not be called"))
        off = Settings(simkl_public_calendar_enabled=False)
        client = _CaptureClient([StoredRecordTests.RICH])
        with patch("app.providers.simkl.calendar.fetch_window", simkl_mock):
            with patch("app.providers.trakt.transport.shared_client", return_value=client):
                records, answered = await calendar_cache.fetch_window_records(
                    SHOWS, off, date(2026, 7, 13))
        simkl_mock.assert_not_awaited()
        self.assertEqual(answered, ["trakt"])
        self.assertTrue(records)

    async def test_a_configured_simkl_ignores_the_switch_in_either_position(self):
        """Once simkl_configured is true, the switch is not the lever any more
        — the account's own source preference is (see app/sources/prefs.py) —
        so a configured instance must still be asked with the switch off."""
        simkl_mock = AsyncMock(return_value=[])
        configured_off = Settings(
            simkl_client_id="id", simkl_access_token="token",
            simkl_public_calendar_enabled=False)
        client = _CaptureClient([StoredRecordTests.RICH])
        with patch("app.providers.simkl.calendar.fetch_window", simkl_mock):
            with patch("app.providers.trakt.transport.shared_client", return_value=client):
                _, answered = await calendar_cache.fetch_window_records(
                    SHOWS, configured_off, date(2026, 7, 13))
        simkl_mock.assert_awaited()
        self.assertIn("trakt", answered)
        self.assertIn("simkl", answered)


class SourcesInPlayForAWindowTests(CacheTestCase):
    """`_window_sources` — the one answer to "who should have something to say
    about this window", which the fill and the completeness check both read.

    The two tests it applies used to be applied in one place and half-applied in
    another: the reader asked only whether a source publishes the endpoint, and
    not whether its calendar reaches the dates. A window outside a source's reach
    was therefore expected from it, never asked, and reported as incomplete on
    every load for ever — refilling it could not help, because the fill went on
    skipping it for the same good reason.
    """

    def in_play(self, endpoint_key, start, settings=None):
        return sorted(str(p.source) for p in calendar_cache._window_sources(
            get_endpoint(endpoint_key), settings or self.settings, start))

    def test_a_source_that_does_not_publish_the_endpoint_is_out_of_play(self):
        """Simkl has no finales file at all (its calendar publishes premieres,
        new shows, all shows and movies), so nothing about a finales window is
        Simkl's to answer."""
        near = calendar_cache.window_start(date.today())
        self.assertEqual(self.in_play("shows", near), ["simkl", "trakt"])
        self.assertEqual(self.in_play("shows/finales", near), ["trakt"])

    def test_a_source_whose_reach_misses_the_window_is_out_of_play(self):
        """The half that was missing from the reader. Simkl's calendar declares a
        rolling window (Capabilities.days_before/days_after); Trakt declares
        none, so it answers for any date."""
        long_ago = calendar_cache.window_start(date.today() - timedelta(days=2000))
        far_ahead = calendar_cache.window_start(date.today() + timedelta(days=400))
        self.assertEqual(self.in_play("shows", long_ago), ["trakt"])
        self.assertEqual(self.in_play("shows", far_ahead), ["trakt"])

    def test_no_viewer_preference_reaches_it(self):
        """It takes an endpoint, a Settings and a date, and that is the whole of
        its input: a window is stored once per (endpoint, week) for everybody, so
        there is no viewer whose selection could narrow it."""
        import inspect
        self.assertEqual(
            list(inspect.signature(calendar_cache._window_sources).parameters),
            ["endpoint", "settings", "start"])
        self.assertEqual(
            list(inspect.signature(calendar_cache.fetch_window_records).parameters),
            ["endpoint", "settings", "start"])


class AskedAndAnsweredTests(CacheTestCase):
    """What "incomplete data" is allowed to mean.

    A window records who was ASKED as well as who ANSWERED, because the two
    failures they distinguish are opposites: a source reached for and silent is
    something to warn about, while a source that was not in play when the window
    was filled is a window to refill. Storing only "who answered" left the reader
    measuring today's sources against a window filled weeks ago, which is why
    admitting a source put a permanent warning on every cached window.
    """

    WINDOW = date(2026, 7, 6)

    async def _read(self, *, now, allow_fetch=True):
        grouped, meta = await calendar_cache.assemble_range(
            SHOWS, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
            now=now, allow_fetch=allow_fetch)
        return [i.id for g in grouped for i in g["items"]], meta

    async def _store(self, sources, asked, *, now=1000, slug="stored"):
        records = calendar_records([_entry(slug, "2026-07-07T12:00:00Z")], SHOWS)
        await calendar_cache.store_window(
            SHOWS.key, self.WINDOW, records, 600, now, sources=sources, asked=asked)

    async def test_a_source_asked_and_silent_is_what_partial_means(self):
        await self._store(["trakt"], ["trakt", "simkl"])
        ids, meta = await self._read(now=1000, allow_fetch=False)
        self.assertEqual(ids, ["stored"])
        self.assertTrue(meta["partial"])

    async def test_a_window_filled_before_a_source_was_in_play_is_refilled(self):
        """Nothing went wrong when it was written, so saying the data could not
        be loaded is untrue — and stays untrue for as long as the row lives.
        Refetching is what actually answers the question, the same treatment a
        payload in an older shape already gets."""
        await self._store(["trakt"], ["trakt"])
        with patch("app.calendar.cache.fetch_window_records",
                   window_fetch([_entry("refilled", "2026-07-07T12:00:00Z")])):
            ids, meta = await self._read(now=1000)   # still well inside the TTL
        self.assertEqual(ids, ["refilled"])
        self.assertFalse(meta["partial"])
        window, _ = await calendar_cache.read_cached_window(SHOWS.key, self.WINDOW)
        self.assertEqual(sorted(window.asked), ["simkl", "trakt"])

    async def test_a_public_page_serves_that_window_instead_of_refilling_it(self):
        """allow_fetch=False is absolute: a share page spends none of the
        instance's rate limit, whatever it thinks of what it found."""
        await self._store(["trakt"], ["trakt"])
        never = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.calendar.cache.fetch_window_records", never):
            ids, _meta = await self._read(now=1000, allow_fetch=False)
        self.assertEqual(ids, ["stored"])
        never.assert_not_awaited()

    async def test_a_window_from_the_older_envelope_reads_as_asked_by_whoever_answered(self):
        """Rows written before `asked` existed record only who answered. Reading
        them as "whoever answered was asked" is what keeps them out of the
        partial state while still making them a miss once a source they never
        recorded is in play."""
        await self._store(["trakt"], None)
        stored = await db.fetch_one(
            "SELECT payload FROM api_cache WHERE cache_key = ?",
            (calendar_cache.cache_key(SHOWS.key, self.WINDOW),))
        payload = json.loads(zlib.decompress(stored["payload"]).decode())
        del payload["asked"]
        await db.execute(
            "UPDATE api_cache SET payload = ? WHERE cache_key = ?",
            (zlib.compress(json.dumps(payload).encode(), cache.COMPRESS_LEVEL),
             calendar_cache.cache_key(SHOWS.key, self.WINDOW)))
        window, _ = await calendar_cache.read_cached_window(SHOWS.key, self.WINDOW)
        self.assertEqual(window.asked, ("trakt",))
        _ids, meta = await self._read(now=1000, allow_fetch=False)
        self.assertFalse(meta["partial"])

    async def test_a_window_no_source_can_reach_is_not_partial_and_does_not_churn(self):
        """The defect that no TTL could mask: outside Simkl's declared reach it
        was expected, never asked, and the span stayed marked incomplete however
        many times it was refilled."""
        long_ago = date.today() - timedelta(days=2000)
        with patch("app.calendar.cache.fetch_window_records",
                   window_fetch([])) as _:
            _grouped, meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=long_ago, end_date=long_ago, now=1000)
        self.assertFalse(meta["partial"])
        # And the second load of the same span is a cache hit, not another fill:
        # a source that is out of play must not read as one that is missing.
        never = AsyncMock(side_effect=AssertionError("must not refill"))
        with patch("app.calendar.cache.fetch_window_records", never):
            _grouped, meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=long_ago, end_date=long_ago, now=1000)
        self.assertFalse(meta["partial"])
        never.assert_not_awaited()

    async def test_an_endpoint_a_source_does_not_publish_is_not_partial_either(self):
        """The same statement for the other of the two tests: Simkl has no
        finales file, so a finales window it never appears in is complete."""
        finales = get_endpoint("shows/finales")
        records = calendar_records([_entry("ender", "2026-07-07T12:00:00Z")], finales)
        await calendar_cache.store_window(
            finales.key, self.WINDOW, records, 600, 1000, sources=["trakt"], asked=["trakt"])
        never = AsyncMock(side_effect=AssertionError("must not refill"))
        with patch("app.calendar.cache.fetch_window_records", never):
            _grouped, meta = await calendar_cache.assemble_range(
                finales, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 7), end_date=date(2026, 7, 7), now=1000)
        self.assertFalse(meta["partial"])
        never.assert_not_awaited()


class InstanceFloorTests(CacheTestCase):
    """The content floor (README.md's "Genres / Countries / Networks" section)
    promises a HARD, pre-cache exclusion: a show it excludes should never enter
    api_cache at all, so no per-account filter (or lack of one) can bring it
    back. Proving that means asserting on fetch_window_records' OWN return value
    — what gets stored — not on a post-hoc read_month() filter, which would pass
    even if the fill cached everything unfiltered."""

    async def _fill(self, endpoint, body, start=date(2026, 7, 6)):
        client = _CaptureClient(body)
        with patch("app.providers.trakt.transport.shared_client", return_value=client):
            records, _ = await calendar_cache.fetch_window_records(endpoint, self.settings, start)
        return {r.id for r in records}

    async def test_a_genre_excluded_by_settings_never_survives_the_fetch(self):
        self.settings.genres = "-anime"
        kept = _entry("kept-drama", "2026-07-06T12:00:00Z", genres=["drama"])
        excluded = _entry("excluded-anime", "2026-07-06T12:00:00Z", genres=["anime"])
        self.assertEqual(await self._fill(SHOWS, [kept, excluded]), {"kept-drama"})

    async def test_a_certification_excluded_by_settings_never_survives_the_fetch(self):
        self.settings.show_certifications = "-tv-ma"
        kept = _entry("kept-tv14", "2026-07-06T12:00:00Z")
        kept["show"]["certification"] = "TV-14"
        excluded = _entry("excluded-tvma", "2026-07-06T12:00:00Z")
        excluded["show"]["certification"] = "TV-MA"
        self.assertEqual(await self._fill(SHOWS, [kept, excluded]), {"kept-tv14"})

    async def test_the_movie_certification_floor_reads_the_movie_field_not_the_show_one(self):
        movies = get_endpoint("movies")
        self.settings.movie_certifications = "-r"
        self.settings.show_certifications = "-tv-ma"  # must not leak into movie filtering
        kept = {"released": "2026-07-06", "movie": {
            "title": "Kept", "certification": "PG", "ids": {"slug": "kept-pg", "trakt": 1}}}
        excluded = {"released": "2026-07-06", "movie": {
            "title": "Excluded", "certification": "R", "ids": {"slug": "excluded-r", "trakt": 2}}}
        self.assertEqual(await self._fill(movies, [kept, excluded]), {"kept-pg"})

    async def test_floor_excluded_shows_stay_excluded_once_the_window_is_stored(self):
        """The end-to-end promise: a floor-excluded show is absent from the
        CACHED window a later read_month() call sees, not merely filtered out on
        the way to a template."""
        self.settings.genres = "-anime"
        kept = _entry("kept-drama", "2026-07-06T12:00:00Z", genres=["drama"])
        excluded = _entry("excluded-anime", "2026-07-06T12:00:00Z", genres=["anime"])
        client = _CaptureClient([kept, excluded])
        with patch("app.providers.trakt.transport.shared_client", return_value=client):
            items, _ = await calendar_cache.read_month(
                SHOWS, self.settings, tz=ZoneInfo("UTC"), year=2026, month=7, now=1000)
        ids = {i.id for i in items}
        self.assertIn("kept-drama", ids)
        self.assertNotIn("excluded-anime", ids)

        window, _ = await calendar_cache.read_cached_window(SHOWS.key, date(2026, 7, 6))
        cached_slugs = {g["by_source"]["trakt"]["id"] for g in window.groups}
        self.assertEqual(cached_slugs, {"kept-drama"})


class WindowOverrunTests(CacheTestCase):
    """Trakt does not honour the `days` bound it is given.

    Measured live against the real API: `/calendars/all/shows/2026-06-29/7` came
    back carrying entries through 2026-07-11, and the 2026-07-13 window carried
    entries through 2026-09-05. Consecutive windows therefore overlap, and a
    month read that concatenated them rendered 207 duplicate cards for July 2026
    — two "House of the Dragon S03E03"s on the 5th, and so on.
    """

    async def _fill(self, endpoint, body, start):
        client = _CaptureClient(body)
        with patch("app.providers.trakt.transport.shared_client", return_value=client):
            records, _ = await calendar_cache.fetch_window_records(endpoint, self.settings, start)
        return records

    async def test_a_window_keeps_only_its_own_seven_days(self):
        body = [
            _entry("day-before", "2026-07-05T12:00:00Z"),    # the previous window's
            _entry("first-day", "2026-07-06T12:00:00Z"),
            _entry("last-day", "2026-07-12T23:00:00Z"),
            _entry("day-after", "2026-07-13T00:30:00Z"),     # the next window's
            _entry("months-later", "2026-09-05T12:00:00Z"),  # the real overrun
        ]
        records = await self._fill(SHOWS, body, date(2026, 7, 6))
        self.assertEqual([r.id for r in records], ["first-day", "last-day"])

    async def test_adjacent_windows_no_longer_both_claim_the_same_airing(self):
        """The boundary case the trim exists for: whichever window Trakt hands an
        airing to, exactly one window keeps it."""
        shared = _entry("house-of-the-dragon", "2026-07-06T01:00:00Z", season=3, number=3)
        earlier = await self._fill(SHOWS, [shared], date(2026, 6, 29))
        owning = await self._fill(SHOWS, [shared], date(2026, 7, 6))
        self.assertEqual(earlier, [])
        self.assertEqual(len(owning), 1)

    async def test_a_read_over_already_overlapping_cached_windows_still_dedupes(self):
        """Windows cached BEFORE the trim existed overlap, and would keep drawing
        doubled cards until their TTL ran out. The read path deduplicates too, so
        the fix lands without anyone having to clear the cache."""
        airing = calendar_records(
            [_entry("house-of-the-dragon", "2026-07-06T01:00:00Z", season=3, number=3)], SHOWS)
        now = 1_800_000_000
        for start in (date(2026, 6, 29), date(2026, 7, 6)):
            await calendar_cache.store_window(SHOWS.key, start, airing, 600, now, sources=["trakt"])

        items, _ = await calendar_cache.read_month(
            SHOWS, self.settings, tz=ZoneInfo("UTC"), year=2026, month=7,
            allow_fetch=False, now=now,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].episode_label, "S03E03")

    async def test_the_request_is_the_documented_shape_for_every_endpoint(self):
        """/calendars/{target}/{path}/{start_date}/{days}. The overrun is Trakt's
        behaviour, not a malformed request, so this pins the URL we send."""
        for key, endpoint in ENDPOINTS.items():
            with self.subTest(endpoint=key):
                client = _CaptureClient([])
                with patch("app.providers.trakt.transport.shared_client", return_value=client):
                    await calendar_cache.fetch_window_records(endpoint, self.settings, date(2026, 7, 6))
                path, _, query = client.url.partition("?")
                self.assertTrue(
                    path.endswith(f"/calendars/all/{trakt_calendar.calendar_path(endpoint)}/2026-07-06/7"), path)
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
        records = await self._fill(movies, body, date(2026, 7, 6))
        self.assertEqual([r.title for r in records], ["In Range"])

    async def test_two_different_episodes_of_one_show_are_not_confused(self):
        """Dedup keys on the airing, not the show — a show legitimately appears
        many times in a month."""
        records = calendar_records([
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=9, number=7),
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=9, number=7),  # repeat
            _entry("rick-and-morty", "2026-07-07T01:00:00Z", season=9, number=8),
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=0, number=76),  # a special
        ], SHOWS)
        kept = calendar_cache.dedupe_records(records)
        self.assertEqual([(r.season, r.episode_number) for r in kept],
                         [(9, 7), (9, 8), (0, 76)])

    async def test_each_of_those_episodes_gets_its_own_stored_group(self):
        """The merge unit is (title, season, episode), so one show's different
        episodes must never collapse into one another — which is the same
        statement as the test above, made about what actually gets written."""
        records = calendar_records([
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=9, number=7),
            _entry("rick-and-morty", "2026-07-07T01:00:00Z", season=9, number=8),
            _entry("rick-and-morty", "2026-07-06T01:00:00Z", season=0, number=76),
        ], SHOWS)
        groups = calendar_cache.group_records(records)
        self.assertEqual(len(groups), 3)
        self.assertEqual(len({g["key"] for g in groups}), 3)

    async def test_one_episode_listed_twice_at_different_times_stays_two_cards(self):
        """A repeated airing is not a duplicate of itself: the calendar has
        always drawn both, and a group key that overwrote would silently lose
        one."""
        records = calendar_records([
            _entry("repeat", "2026-07-06T01:00:00Z", season=1, number=1),
            _entry("repeat", "2026-07-06T09:00:00Z", season=1, number=1),
        ], SHOWS)
        groups = calendar_cache.group_records(records)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len({g["key"] for g in groups}), 2)


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
            (calendar_records([_entry("first", "2026-07-06T12:00:00Z")], SHOWS), ["trakt"]),
            (calendar_records([_entry("second", "2026-07-06T12:00:00Z")], SHOWS), ["trakt"]),
        ])
        with patch("app.calendar.cache.fetch_window_records", fetch):
            window, cached_at = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), now=1000)
            self.assertEqual(window.groups[0]["by_source"]["trakt"]["id"], "first")
            # Within TTL: served from cache, no second fetch.
            window, _ = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), now=1000 + 9 * 60)
            self.assertEqual(window.groups[0]["by_source"]["trakt"]["id"], "first")
            self.assertEqual(fetch.call_count, 1)
            # Past TTL: refetched.
            window, _ = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), now=1000 + 11 * 60)
            self.assertEqual(window.groups[0]["by_source"]["trakt"]["id"], "second")
            self.assertEqual(fetch.call_count, 2)

    async def test_public_read_never_fetches_and_serves_what_is_cached(self):
        # Nothing cached, fetch disabled -> empty, and no source is ever asked.
        fetch = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.calendar.cache.fetch_window_records", fetch):
            window, cached_at = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), allow_fetch=False)
        self.assertEqual(window.groups, [])
        self.assertIsNone(cached_at)
        fetch.assert_not_awaited()

    async def test_public_read_serves_stale_cache_without_refetching(self):
        self.settings.calendar_cache_ttl_minutes = 10
        with patch("app.calendar.cache.fetch_window_records",
                   window_fetch([_entry("cached", "2026-07-06T12:00:00Z")])):
            await calendar_cache.load_window(SHOWS, self.settings, date(2026, 7, 6), now=1000)
        # Long past the TTL, but a public read must serve the stale copy, not fetch.
        never = AsyncMock(side_effect=AssertionError("must not fetch"))
        with patch("app.calendar.cache.fetch_window_records", never):
            window, cached_at = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), allow_fetch=False, now=10 ** 9)
        self.assertEqual(window.groups[0]["by_source"]["trakt"]["id"], "cached")
        self.assertEqual(cached_at, 1000)

    async def test_a_window_stored_in_an_older_shape_reads_as_a_miss(self):
        """Every window written before the stored shape changed is a bare list.
        It must read as a MISS — refetched, not raised over and not handed to the
        read path as though it were groups. With a ten-minute TTL the whole cache
        turns over in ten minutes, so there is nothing to migrate."""
        import json
        import zlib
        from app.cache import COMPRESS_LEVEL
        legacy = zlib.compress(json.dumps(
            [{"first_aired": "2026-07-06T12:00:00Z",
              "show": {"title": "Old", "ids": {"slug": "old", "trakt": 1}}}]).encode(),
            COMPRESS_LEVEL)
        await db.execute(
            "INSERT INTO api_cache (cache_key, payload, cached_at, ttl_seconds, byte_size) "
            "VALUES (?, ?, ?, ?, ?)",
            (calendar_cache.cache_key(SHOWS.key, date(2026, 7, 6)), legacy, 1000, 600, len(legacy)),
        )
        self.assertIsNone(await calendar_cache.read_cached_window(SHOWS.key, date(2026, 7, 6)))
        with patch("app.calendar.cache.fetch_window_records",
                   window_fetch([_entry("fresh", "2026-07-06T12:00:00Z")])):
            window, _ = await calendar_cache.load_window(
                SHOWS, self.settings, date(2026, 7, 6), now=1001)
        self.assertEqual(window.groups[0]["by_source"]["trakt"]["id"], "fresh")

    async def _read_boundary(self, tz_name, year, month):
        """read_month for a single item airing 2026-03-01T02:00Z, in tz_name."""
        target_window = calendar_cache.window_start(date(2026, 3, 1))

        def fake(endpoint, start):
            if start == target_window:
                return [_entry("boundary", "2026-03-01T02:00:00Z")]
            return []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            items, _ = await calendar_cache.read_month(
                SHOWS, self.settings, tz=ZoneInfo(tz_name), year=year, month=month)
        return {i.id for i in items}

    async def test_month_boundary_is_the_viewers(self):
        # 02:00 UTC on 1 Mar is 18:00 28 Feb in Los_Angeles (UTC-8, pre-DST) ...
        self.assertIn("boundary", await self._read_boundary("America/Los_Angeles", 2026, 2))
        self.assertNotIn("boundary", await self._read_boundary("America/Los_Angeles", 2026, 3))
        # ... and 04:00 1 Mar in Athens (UTC+2).
        self.assertIn("boundary", await self._read_boundary("Europe/Athens", 2026, 3))
        self.assertNotIn("boundary", await self._read_boundary("Europe/Athens", 2026, 2))

    async def test_read_month_reports_the_oldest_window_as_of(self):
        with patch("app.calendar.cache.fetch_window_records", window_fetch([])):
            _, as_of = await calendar_cache.read_month(
                SHOWS, self.settings, tz=ZoneInfo("UTC"), year=2026, month=7, now=555)
        self.assertEqual(as_of, 555)

    async def test_a_fill_schedules_a_drain(self):
        """A window that actually fetched and stored asks for enrichment
        sooner than the next heartbeat tick — see app/calendar/enrich.py's
        schedule_drain, called from load_window right after store_window."""
        # schedule_drain is an ordinary sync function (fire-and-forget, never
        # awaited by its caller) — a MagicMock, not an AsyncMock, is what
        # pins that: an AsyncMock here would leave an unawaited coroutine
        # behind and mask a caller that mistakenly started awaiting it.
        schedule = MagicMock()
        with patch("app.calendar.cache.calendar_enrich.schedule_drain", schedule):
            with patch("app.calendar.cache.fetch_window_records",
                       window_fetch([_entry("first", "2026-07-06T12:00:00Z")])):
                await calendar_cache.load_window(SHOWS, self.settings, date(2026, 7, 6), now=1000)
        schedule.assert_called_once_with(self.settings)

    async def test_a_cache_hit_read_schedules_no_drain(self):
        """THE INVARIANT THIS MUST NOT WEAKEN: a pure read never makes an
        outbound call, and scheduling a drain is adjacent to that promise
        because the drain itself does. Hooking schedule_drain into the FILL
        branch of load_window rather than into the function as a whole is
        what keeps a cache-hit return — no fetch, nothing new stored — from
        ever reaching it."""
        schedule = MagicMock()
        with patch("app.calendar.cache.calendar_enrich.schedule_drain", schedule):
            with patch("app.calendar.cache.fetch_window_records",
                       window_fetch([_entry("first", "2026-07-06T12:00:00Z")])):
                await calendar_cache.load_window(SHOWS, self.settings, date(2026, 7, 6), now=1000)
            schedule.reset_mock()
            # Within TTL: served from cache, no fetch and therefore no schedule.
            never_fetch = AsyncMock(side_effect=AssertionError("must not fetch"))
            with patch("app.calendar.cache.fetch_window_records", never_fetch):
                await calendar_cache.load_window(SHOWS, self.settings, date(2026, 7, 6), now=1000)
        schedule.assert_not_called()


# ---------------------------------------------------------------------------
# assemble_range — the per-span primitive read_month is now a wrapper over
# ---------------------------------------------------------------------------

class AssembleRangeTests(CacheTestCase):
    """assemble_range reads only the windows covering a span, loads them
    concurrently, dedupes/normalizes/trims/groups, and reports a partial flag
    when a window couldn't be loaded. read_month is now a thin wrapper over it."""

    async def test_a_single_day_reads_only_the_windows_covering_it(self):
        """The whole point of the primitive: a per-day read must not fetch the
        whole month. A mid-July UTC day sits inside one window (plus, at worst,
        the ±1-day pad's neighbour), never all of July's five."""
        seen: list[date] = []

        def fake(endpoint, start):
            seen.append(start)
            return []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 15), end_date=date(2026, 7, 15))
        self.assertLessEqual(len(seen), 2)
        self.assertNotIn(calendar_cache.window_start(date(2026, 7, 1)), seen)
        self.assertNotIn(calendar_cache.window_start(date(2026, 7, 27)), seen)

    async def test_a_local_day_straddling_two_utc_windows_loads_both(self):
        """A viewer-local day can span two UTC windows once the offset is applied.
        In UTC+14, local 13 July runs from 10:00 UTC on the 12th (the 6-Jul
        window) to 09:59 UTC on the 13th (the 13-Jul window), so a one-day read
        must load BOTH windows to see both airings."""
        tz = ZoneInfo("Pacific/Kiritimati")  # UTC+14, no DST
        early_window = calendar_cache.window_start(date(2026, 7, 12))
        late_window = calendar_cache.window_start(date(2026, 7, 13))
        self.assertNotEqual(early_window, late_window)  # genuinely two windows
        before = _entry("before-boundary", "2026-07-12T20:00:00Z")   # 13 Jul 10:00 local
        after = _entry("after-boundary", "2026-07-13T05:00:00Z")     # 13 Jul 19:00 local

        def fake(endpoint, start):
            if start == early_window:
                return [before]
            if start == late_window:
                return [after]
            return []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            grouped, meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=tz,
                start_date=date(2026, 7, 13), end_date=date(2026, 7, 13))
        self.assertEqual([g["date"] for g in grouped], ["2026-07-13"])
        self.assertEqual({i.id for i in grouped[0]["items"]},
                         {"before-boundary", "after-boundary"})
        self.assertFalse(meta["partial"])

    async def test_the_network_filter_excludes_as_well_as_includes(self):
        """The read path's own wiring, not just the predicate: one viewer's
        network choice arrives here as the list the preferences hold, and a
        leading '-' has to reach the same convention the genre, country and
        certification specs use."""
        entries = []
        for slug, network in (("apple", "Apple TV"), ("flix", "Netflix"), ("hbo", "HBO")):
            entry = _entry(slug, "2026-07-15T20:00:00Z")
            entry["show"]["network"] = network
            entries.append(entry)

        async def read(networks):
            with patch("app.calendar.cache.fetch_window_records", window_fetch(entries)):
                grouped, _ = await calendar_cache.assemble_range(
                    SHOWS, self.settings, tz=ZoneInfo("UTC"),
                    start_date=date(2026, 7, 15), end_date=date(2026, 7, 15),
                    network_filter=networks)
            return [item.id for group in grouped for item in group["items"]]

        self.assertEqual(await read(["Apple TV"]), ["apple"])
        self.assertEqual(sorted(await read(["-Apple TV"])), ["flix", "hbo"])
        # Case still has to match exactly — see calendar_filter.parse_network_spec.
        self.assertEqual(await read(["apple tv"]), [])
        self.assertEqual(sorted(await read(None)), ["apple", "flix", "hbo"])

    async def test_overlapping_windows_dedupe_keeping_the_earlier_windows_copy(self):
        """gather loads the windows concurrently, but the results are stitched
        back in window order, so an airing two adjacent windows both return is
        kept from the EARLIER window — the same copy the old sequential read
        kept. Proves ordering survives the concurrent fetch."""
        w1 = calendar_cache.window_start(date(2026, 7, 6))
        w2 = w1 + timedelta(days=7)
        from_w1 = _entry("dup", "2026-07-15T20:00:00Z", season=3, number=3)
        from_w1["show"]["title"] = "from the earlier window"
        from_w2 = _entry("dup", "2026-07-15T20:00:00Z", season=3, number=3)
        from_w2["show"]["title"] = "from the later window"

        def fake(endpoint, start):
            if start == w1:
                return [from_w1]
            if start == w2:
                return [from_w2]
            return []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            grouped, meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31))
        items = [i for g in grouped for i in g["items"]]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "from the earlier window")

    async def test_a_single_failed_window_renders_the_rest_and_flags_partial(self):
        """RESILIENT BUT LOUD: one window's fetch failing (nothing cached to fall
        back on) drops that window but still renders the others, and sets
        meta['partial'] so the caller can warn."""
        good_window = calendar_cache.window_start(date(2026, 7, 8))
        boom_window = calendar_cache.window_start(date(2026, 7, 20))
        self.assertNotEqual(good_window, boom_window)

        def fake(endpoint, start):
            if start == boom_window:
                raise TraktError("Trakt unreachable", 503)
            return [_entry("good", "2026-07-08T12:00:00Z")] if start == good_window else []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            grouped, meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31))
        items = [i for g in grouped for i in g["items"]]
        self.assertEqual({i.id for i in items}, {"good"})
        self.assertTrue(meta["partial"])

    async def test_every_window_failing_raises(self):
        """A span where nothing loaded and nothing was cached has nothing to
        show, so it surfaces as a hard error rather than a silent empty month."""
        def fake(endpoint, start):
            raise TraktError("Trakt unreachable", 503)

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            with self.assertRaises(TraktError):
                await calendar_cache.assemble_range(
                    SHOWS, self.settings, tz=ZoneInfo("UTC"),
                    start_date=date(2026, 7, 1), end_date=date(2026, 7, 31))

    async def test_meta_counts_split_watching_from_not_watching(self):
        a = _entry("a", "2026-07-08T12:00:00Z")
        b = _entry("b", "2026-07-09T12:00:00Z")
        target = calendar_cache.window_start(date(2026, 7, 8))

        def fake(endpoint, start):
            return [a, b] if start == target else []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            grouped, meta = await calendar_cache.assemble_range(
                SHOWS, self.settings, tz=ZoneInfo("UTC"),
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
                not_watching_ids={"b"})
        self.assertEqual(meta["total"], 2)
        self.assertEqual(meta["watching"], 1)
        self.assertEqual(meta["not_watching"], 1)
        self.assertEqual(set(meta["show_ids"]), {"a", "b"})
        self.assertFalse(meta["partial"])  # complete month

    async def test_read_month_serves_partial_data_without_raising(self):
        """The (items, as_of) wrapper the share and distrakt paths use inherits
        the resilience: a single failed window no longer aborts the read; only a
        total failure does."""
        good_window = calendar_cache.window_start(date(2026, 7, 8))
        boom_window = calendar_cache.window_start(date(2026, 7, 20))

        def fake(endpoint, start):
            if start == boom_window:
                raise TraktError("Trakt unreachable", 503)
            return [_entry("good", "2026-07-08T12:00:00Z")] if start == good_window else []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            items, _as_of = await calendar_cache.read_month(
                SHOWS, self.settings, tz=ZoneInfo("UTC"), year=2026, month=7)
        self.assertEqual({i.id for i in items}, {"good"})


# ---------------------------------------------------------------------------
# heartbeat pre-warm — gated behind calendar_prewarm_enabled + the TTL floor
# ---------------------------------------------------------------------------

class PrewarmTests(CacheTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        calendar_cache._last_prewarm_at = None

    async def asyncTearDown(self):
        calendar_cache._last_prewarm_at = None
        await super().asyncTearDown()

    async def test_disabled_setting_skips_even_with_a_qualifying_ttl(self):
        self.settings.calendar_prewarm_enabled = False
        self.settings.calendar_cache_ttl_minutes = 1440
        with patch("app.calendar.cache.load_window", new_callable=AsyncMock) as mocked:
            await calendar_cache.prewarm_calendar_cache(self.settings, now=1_000_000)
        mocked.assert_not_called()

    async def test_enabled_but_ttl_below_a_day_skips(self):
        self.settings.calendar_prewarm_enabled = True
        self.settings.calendar_cache_ttl_minutes = 1439
        with patch("app.calendar.cache.load_window", new_callable=AsyncMock) as mocked:
            await calendar_cache.prewarm_calendar_cache(self.settings, now=1_000_000)
        mocked.assert_not_called()

    async def test_enabled_and_ttl_at_the_floor_warms_every_endpoint_and_window(self):
        self.settings.calendar_prewarm_enabled = True
        self.settings.calendar_cache_ttl_minutes = 1440
        now = 1_753_000_000  # an arbitrary but fixed instant
        today = datetime.fromtimestamp(now, tz=timezone.utc).date()
        with patch("app.calendar.cache.load_window", new_callable=AsyncMock) as mocked:
            mocked.return_value = ([], None)
            await calendar_cache.prewarm_calendar_cache(self.settings, now=now)
        expected_windows = calendar_cache.aligned_windows(
            today, today + timedelta(days=calendar_cache.PREWARM_DAYS))
        self.assertEqual(mocked.call_count, len(ENDPOINTS) * len(expected_windows))
        called_endpoints = {call.args[0].key for call in mocked.call_args_list}
        self.assertEqual(called_endpoints, set(ENDPOINTS))

    async def test_runs_at_most_once_per_ttl(self):
        self.settings.calendar_prewarm_enabled = True
        self.settings.calendar_cache_ttl_minutes = 1440  # ttl = 86400 seconds
        with patch("app.calendar.cache.load_window", new_callable=AsyncMock) as mocked:
            mocked.return_value = ([], None)
            await calendar_cache.prewarm_calendar_cache(self.settings, now=1_000_000)
            first_count = mocked.call_count
            await calendar_cache.prewarm_calendar_cache(self.settings, now=1_000_000 + 100)
            self.assertEqual(mocked.call_count, first_count)  # too soon, skipped
            await calendar_cache.prewarm_calendar_cache(self.settings, now=1_000_000 + 86400 + 1)
            self.assertGreater(mocked.call_count, first_count)  # ttl elapsed, runs again

    async def test_a_failed_window_does_not_raise(self):
        self.settings.calendar_prewarm_enabled = True
        self.settings.calendar_cache_ttl_minutes = 1440
        with patch("app.calendar.cache.load_window", new_callable=AsyncMock) as mocked:
            mocked.side_effect = TraktError("Trakt unreachable", 503)
            await calendar_cache.prewarm_calendar_cache(self.settings, now=1_000_000)  # must not raise


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

    @staticmethod
    def _as_records(entries, media_key):
        """The fixture's captured payloads as the records the filter now reads.

        THE FIXTURE IS UNCHANGED AND STILL THE SUBJECT — it is a real Trakt
        response trimmed to the three fields the predicate reads, and each field
        arrives on a Record spelled exactly as the normalizer spells it, so this
        is the same live-checked data reaching the same rule by the route the app
        now takes. `country` is upper-cased here for the same reason the
        normalizer upper-cases it: that is the display form, and a spec matching
        it only because it was left lower would be a test that passes for the
        wrong reason.
        """
        records = []
        for entry in entries:
            media = entry[media_key]
            records.append(SimpleNamespace(
                slug=media["ids"]["slug"],
                genres=list(media.get("genres") or []),
                country=(media.get("country") or "").upper(),
                certification=(media.get("certification") or "").upper(),
            ))
        return records

    def _kept_slugs(self, spec):
        kept = calendar_filter.filter_records(
            self._as_records(self.fixture["entries"], self.fixture["media_key"]),
            spec["genres"], spec["countries"])
        return {r.slug for r in kept}

    def _kept_cert_slugs(self, key, media_key, spec_key):
        kept = calendar_filter.filter_records(
            self._as_records(self.fixture[key], media_key), "", "", self.fixture[spec_key])
        return {r.slug for r in kept}

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

    def test_show_certification_exclude(self):
        self.assertEqual(
            self._kept_cert_slugs("cert_entries", "show", "cert_exclude_spec"),
            set(self.fixture["expected_cert_exclude"]))

    def test_show_certification_include(self):
        self.assertEqual(
            self._kept_cert_slugs("cert_entries", "show", "cert_include_spec"),
            set(self.fixture["expected_cert_include"]))

    def test_movie_certification_exclude(self):
        """Movies use the MPA vocabulary, a different set of tokens from the
        shows' TV-* one, but read from the same `certification` key."""
        self.assertEqual(
            self._kept_cert_slugs("movie_cert_entries", self.fixture["movie_media_key"],
                                  "movie_cert_exclude_spec"),
            set(self.fixture["expected_movie_cert_exclude"]))

    def test_movie_certification_include(self):
        self.assertEqual(
            self._kept_cert_slugs("movie_cert_entries", self.fixture["movie_media_key"],
                                  "movie_cert_include_spec"),
            set(self.fixture["expected_movie_cert_include"]))


class FilterEdgeCaseTests(unittest.TestCase):
    """The live sample barely covered empty genres / empty country, so pin them
    down explicitly against the predicate."""
    def test_empty_genres_kept_by_exclude_only_dropped_by_include(self):
        g_inc, g_exc = calendar_filter.parse_spec("-anime,-music")
        self.assertTrue(calendar_filter.keep_values(
            [], "us", "", g_inc, g_exc, set(), set(), set(), set()))
        # A genre INCLUDE spec has something to be a member of; an item with no
        # genres is a member of nothing, so it drops.
        gi_inc, gi_exc = calendar_filter.parse_spec("drama,comedy")
        self.assertFalse(calendar_filter.keep_values(
            [], "us", "", gi_inc, gi_exc, set(), set(), set(), set()))

    def test_missing_country_kept_by_exclude_dropped_by_allowlist(self):
        c_inc, c_exc = calendar_filter.parse_spec("-kr")
        self.assertTrue(calendar_filter.keep_values(
            ["drama"], "", "", set(), set(), c_inc, c_exc, set(), set()))
        ai_inc, ai_exc = calendar_filter.parse_spec("us,gb,jp")
        self.assertFalse(calendar_filter.keep_values(
            ["drama"], "", "", set(), set(), ai_inc, ai_exc, set(), set()))

    def test_missing_certification_kept_by_exclude_dropped_by_allowlist(self):
        """Certification follows the country precedent, not the genre one: it is
        a single scalar, so a missing value is membership in nothing."""
        cert_inc, cert_exc = calendar_filter.parse_spec("-tv-ma")
        self.assertTrue(calendar_filter.keep_values(
            ["drama"], "us", "", set(), set(), set(), set(), cert_inc, cert_exc))
        ci_inc, ci_exc = calendar_filter.parse_spec("tv-pg,tv-14")
        self.assertFalse(calendar_filter.keep_values(
            ["drama"], "us", "", set(), set(), set(), set(), ci_inc, ci_exc))

    def test_the_display_case_of_a_value_does_not_decide_the_match(self):
        """A Record carries the country and the certification in their DISPLAY
        form (upper), and a spec is written lower. The predicate lowercases both
        sides, and the whole per-viewer country filter silently stops matching if
        that ever changes."""
        _, c_exc = calendar_filter.parse_spec("-kr")
        self.assertFalse(calendar_filter.keep_values(
            ["drama"], "KR", "", set(), set(), set(), c_exc, set(), set()))
        _, cert_exc = calendar_filter.parse_spec("-tv-ma")
        self.assertFalse(calendar_filter.keep_values(
            ["drama"], "US", "TV-MA", set(), set(), set(), set(), set(), cert_exc))

    def test_no_spec_is_a_pass_through(self):
        records = [SimpleNamespace(genres=["anime"], country="KR", certification="")]
        self.assertEqual(calendar_filter.filter_records(records, "", ""), records)


class NetworkFilterTests(unittest.TestCase):
    """The fourth dimension. It takes the same leading-'-' convention as the
    other three and, unlike them, matches with the case left alone."""

    @staticmethod
    def named(*networks):
        """Stand-ins carrying only what the network filter reads."""
        return [SimpleNamespace(network=name) for name in networks]

    def kept(self, items, spec):
        return [item.network for item in calendar_filter.filter_by_network(items, spec)]

    def test_a_bare_name_keeps_only_that_network(self):
        items = self.named("Apple TV", "Netflix", "HBO")
        self.assertEqual(self.kept(items, ["Apple TV"]), ["Apple TV"])

    def test_a_leading_dash_excludes_that_network(self):
        items = self.named("Apple TV", "Netflix", "HBO")
        self.assertEqual(self.kept(items, ["-Apple TV"]), ["Netflix", "HBO"])

    def test_excludes_and_includes_compose_the_way_the_other_dimensions_do(self):
        items = self.named("Apple TV", "Netflix", "HBO")
        self.assertEqual(self.kept(items, ["Apple TV", "Netflix", "-Netflix"]), ["Apple TV"])

    def test_matching_is_case_sensitive_in_both_directions(self):
        """Trakt spells a network however the network spells itself, and one week
        of the calendar carried both 'TVN' and 'tvN' — a Polish broadcaster and a
        Korean one. Folding case would merge two different networks."""
        items = self.named("tvN", "TVN")
        self.assertEqual(self.kept(items, ["tvN"]), ["tvN"])
        self.assertEqual(self.kept(items, ["-tvN"]), ["TVN"])
        self.assertEqual(self.kept(items, ["apple tv"]), [])

    def test_an_item_with_no_network_is_kept_by_an_exclude_and_dropped_by_an_include(self):
        """Every film answers the empty string — Trakt's movie objects carry no
        network field — so this is the common case, not an edge one. It follows
        the country and certification precedent."""
        items = self.named("", "HBO")
        self.assertEqual(self.kept(items, ["-HBO"]), [""])
        self.assertEqual(self.kept(items, ["HBO"]), ["HBO"])

    def test_an_empty_spec_is_a_pass_through(self):
        items = self.named("HBO", "")
        for spec in (None, [], ["", "  "], ["-"]):
            with self.subTest(spec=spec):
                self.assertEqual(self.kept(items, spec), ["HBO", ""])


class PruneDisguisedFilmsTests(unittest.TestCase):
    """A Simkl entry whose enrichment says `anime_type: "movie"` does not
    belong on a series endpoint — see filter.prune_disguised_films. Only
    `movie` is a film; `ona`, `ova`, `tv` and `special` are all serial
    formats and total_episodes is deliberately not the signal (a measured
    one-episode ONA series and a measured one-episode film both carry it)."""

    @staticmethod
    def _record(anime_type):
        return SimpleNamespace(title=anime_type or "unenriched", anime_type=anime_type)

    def test_a_movie_is_pruned_from_a_show_endpoint(self):
        records = [self._record("movie"), self._record("tv")]
        kept = calendar_filter.prune_disguised_films(records, "show")
        self.assertEqual([r.title for r in kept], ["tv"])

    def test_every_serial_anime_type_survives(self):
        records = [self._record(t) for t in ("ona", "ova", "tv", "special")]
        kept = calendar_filter.prune_disguised_films(records, "show")
        self.assertEqual(len(kept), 4)

    def test_an_unenriched_record_is_not_pruned(self):
        """total_episodes is not the signal, and neither is "we have not
        looked yet" — a record with no anime_type at all (unenriched, or a
        non-Simkl source) must render until enrichment actually says movie."""
        record = SimpleNamespace(title="pending")  # no anime_type attribute at all
        kept = calendar_filter.prune_disguised_films([record], "show")
        self.assertEqual(kept, [record])

    def test_a_movie_endpoint_is_never_pruned(self):
        """The rule only ever applies to a SERIES endpoint — a title correctly
        listed as a film on the movies endpoint must not be touched by it."""
        records = [self._record("movie")]
        kept = calendar_filter.prune_disguised_films(records, "movie")
        self.assertEqual(kept, records)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
