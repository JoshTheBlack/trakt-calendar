"""Simkl's calendar CDN fetch and normalizer.

UNLIKE THE REST OF THIS PACKAGE, this is not api.simkl.com — a separate,
unauthenticated host serving pre-baked JSON files. No network: the transport's
one GET is stubbed, and the conditional-GET tests exercise the real disk cache
(app/cache.py) through a fresh test database, because "does a 304 make a second
request" is a question about what got cached, not about a mock's call count
alone.
"""
from __future__ import annotations

import unittest
from datetime import date, timezone
from unittest.mock import AsyncMock, patch

from app import cache, db
from app.endpoints import get_endpoint
from app.providers.base import Media, Source, SourceUnavailable
from app.providers.simkl import calendar as simkl_calendar
from app.providers.simkl.transport import SimklError
from tests.support import new_db_path

SETTINGS = object()  # fetch_window's settings arg is unused — see calendar.py's docstring

SHOWS = get_endpoint("shows")
SHOWS_NEW = get_endpoint("shows/new")
PREMIERES = get_endpoint("shows/premieres")
MOVIES = get_endpoint("movies")


class _Resp:
    def __init__(self, data=None, status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._data


class _Client:
    """Records every URL it was asked for and answers them in order, or repeats
    the last answer if more requests arrive than canned responses — most tests
    here only care about the first."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    async def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        self.headers.append(headers or {})
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _tv_entry(simkl_id=1, slug="a-show", season=1, episode=1, when="2026-07-06T20:00:00-04:00",
             tmdb="822653"):
    return {
        "title": "A Show", "poster": "19/abc123", "date": when,
        "release_date": when.split("T")[0],
        "ids": {"simkl_id": simkl_id, "slug": slug, "tmdb": tmdb, "imdb": None},
        "url": f"https://simkl.com/tv/{simkl_id}/{slug}",
        "episode": {"season": season, "episode": episode, "url": "x"},
    }


def _movie_entry(simkl_id=1, slug="a-movie", release_date="2026-07-06"):
    return {
        "title": "A Movie", "poster": "19/def456", "date": f"{release_date}T00:00:00-04:00",
        "release_date": release_date,
        "ids": {"simkl_id": simkl_id, "slug": slug, "tmdb": "1741121", "imdb": None},
        "url": f"https://simkl.com/movies/{simkl_id}/{slug}",
    }


class NormalizerTests(unittest.TestCase):
    """to_show_record / to_movie_record, pure — no cache, no network."""

    def test_a_show_entry_becomes_a_record(self):
        record = simkl_calendar.to_show_record(_tv_entry())
        self.assertEqual(record.source, Source.SIMKL)
        self.assertEqual(record.media, Media.SHOW)
        self.assertEqual(record.title, "A Show")
        self.assertEqual(record.episode_label, "S01E01")
        self.assertEqual(record.season, 1)
        self.assertEqual(record.episode_number, 1)
        self.assertFalse(record.date_only)
        self.assertEqual(record.ids["tmdb"], "822653")
        self.assertEqual(record.poster, "https://simkl.in/posters/19/abc123_m.jpg")

    def test_an_entry_with_no_episode_object_does_not_crash_the_normalizer(self):
        """One real anime entry (an anime-type movie inside anime.json) carried
        no `episode` key at all. It must normalize to a record with no
        episode coordinate rather than raising."""
        entry = _tv_entry()
        del entry["episode"]
        record = simkl_calendar.to_show_record(entry)
        self.assertIsNotNone(record)
        self.assertIsNone(record.episode_label)
        self.assertIsNone(record.season)
        self.assertIsNone(record.episode_number)

    def test_an_entry_with_no_date_is_dropped_not_raised_over(self):
        entry = _tv_entry()
        del entry["date"]
        self.assertIsNone(simkl_calendar.to_show_record(entry))

    def test_anime_carries_no_season_and_gets_no_episode_label(self):
        """None of the sampled anime entries carried episode.season — the label needs
        both coordinates and must not be built from episode number alone."""
        entry = _tv_entry()
        entry["episode"] = {"episode": 1, "url": "x"}
        record = simkl_calendar.to_show_record(entry)
        self.assertIsNone(record.episode_label)
        self.assertIsNone(record.season)
        self.assertEqual(record.episode_number, 1)

    def test_a_movie_entry_is_date_only_and_reads_release_date_not_date(self):
        """Every movie `date` is 00:00:00 in the FILE's fixed -04:00
        offset — a release DATE, not a real instant. Converting `date` instead
        of reading `release_date` directly would move the release a day for a
        viewer west of that offset."""
        entry = _movie_entry(release_date="2026-07-06")
        entry["date"] = "2026-07-06T00:00:00-04:00"  # what date WOULD say if misread
        record = simkl_calendar.to_movie_record(entry)
        self.assertTrue(record.date_only)
        from datetime import datetime
        rendered = datetime.fromtimestamp(record.air_ts, tz=timezone.utc).date()
        self.assertEqual(rendered.isoformat(), "2026-07-06")

    def test_a_percent_encoded_movie_slug_is_not_re_encoded(self):
        """Movie slugs are percent-encoded in the ids block. The record's
        detail_url must be the entry's OWN `url`, used as given — rebuilding a
        URL from the slug would double-encode it."""
        entry = _movie_entry(slug="can%E2%80%99t-sleep-at-silent-night")
        entry["url"] = "https://simkl.com/movies/1/can%E2%80%99t-sleep-at-silent-night"
        record = simkl_calendar.to_movie_record(entry)
        self.assertEqual(record.detail_url, entry["url"])

    def test_movie_ids_carry_no_mal_key(self):
        record = simkl_calendar.to_movie_record(_movie_entry())
        self.assertNotIn("mal", record.ids)


class DerivationTests(unittest.TestCase):
    """The endpoint-specific filters over the raw tv/anime entries."""

    def test_shows_new_wants_season_one_episode_one_exactly(self):
        s01e01 = _tv_entry(simkl_id=1, season=1, episode=1)
        s02e01 = _tv_entry(simkl_id=2, season=2, episode=1)
        self.assertEqual(simkl_calendar._tv_new([s01e01, s02e01]), [s01e01])

    def test_premieres_wants_episode_one_of_any_season(self):
        s01e01 = _tv_entry(simkl_id=1, season=1, episode=1)
        s02e01 = _tv_entry(simkl_id=2, season=2, episode=1)
        s02e02 = _tv_entry(simkl_id=3, season=2, episode=2)
        self.assertEqual(simkl_calendar._premieres([s01e01, s02e01, s02e02]),
                         [s01e01, s02e01])

    def test_anime_new_keeps_only_the_earliest_dated_episode_one_per_title(self):
        """Anime carries no season, so 'first episode' is 'the earliest-dated
        episode 1 this simkl_id has' — a rerun listed again at episode 1 on a
        later date must not count as a second premiere."""
        first = _tv_entry(simkl_id=9, episode=1, when="2026-07-01T00:00:00+09:00")
        del first["episode"]["season"]
        rerun = _tv_entry(simkl_id=9, episode=1, when="2026-08-01T00:00:00+09:00")
        del rerun["episode"]["season"]
        self.assertEqual(simkl_calendar._anime_new([rerun, first]), [first])

    def test_anime_new_ignores_entries_past_episode_one(self):
        ep2 = _tv_entry(simkl_id=9, episode=2)
        del ep2["episode"]["season"]
        self.assertEqual(simkl_calendar._anime_new([ep2]), [])


class MonthCoveringTests(unittest.TestCase):
    def test_a_window_within_one_month_asks_for_one_month(self):
        self.assertEqual(simkl_calendar._months_covering(date(2026, 7, 10), 7), [(2026, 7)])

    def test_a_window_crossing_a_month_boundary_asks_for_both(self):
        self.assertEqual(simkl_calendar._months_covering(date(2026, 7, 30), 7),
                         [(2026, 7), (2026, 8)])

    def test_a_window_crossing_a_year_boundary_asks_for_both_years(self):
        self.assertEqual(simkl_calendar._months_covering(date(2026, 12, 30), 7),
                         [(2026, 12), (2027, 1)])


class DedupeTests(unittest.TestCase):
    def test_a_repeated_tuple_within_one_file_is_collapsed_once(self):
        entry = _tv_entry()
        self.assertEqual(len(simkl_calendar._dedupe_file_entries([entry, dict(entry)])), 1)

    def test_a_genuinely_different_airing_of_the_same_title_survives(self):
        first = _tv_entry(season=1, episode=1)
        second = _tv_entry(season=1, episode=2)
        self.assertEqual(len(simkl_calendar._dedupe_file_entries([first, second])), 2)


class FetchWindowTests(unittest.IsolatedAsyncioTestCase):
    """The port: one archive file per (year, month) needed, conditional GET,
    and the endpoint-to-file-and-derivation mapping."""

    async def asyncSetUp(self):
        new_db_path("simkl-calendar")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_movies_reads_the_movie_release_archive(self):
        client = _Client(_Resp([_movie_entry()], headers={"ETag": '"x"'}))
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            records = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].media, Media.MOVIE)
        self.assertIn("/2026/7/movie_release.json", client.urls[0])
        self.assertNotIn("?", client.urls[0])  # no cache-buster, no query string at all

    async def test_shows_unions_tv_and_anime(self):
        async def fake_get(url):
            return [_tv_entry(simkl_id=1)] if "tv.json" in url else [_tv_entry(simkl_id=2, slug="b")]
        with patch("app.providers.simkl.calendar._conditional_get", side_effect=fake_get):
            records = await simkl_calendar.fetch_window(SHOWS, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual({r.id for r in records}, {"a-show", "b"})

    async def test_shows_finales_is_not_answered(self):
        """Not in Capabilities.endpoints (no such concept exists on the
        calendar CDN), and fetch_window's own safety net agrees."""
        finales = get_endpoint("shows/finales")
        records = await simkl_calendar.fetch_window(finales, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual(records, [])

    async def test_a_304_serves_the_stored_copy_and_makes_no_second_request(self):
        url = f"{simkl_calendar.CDN_BASE}/2026/7/movie_release.json"
        await cache.set(simkl_calendar._cdn_cache_key(url), {
            "etag": '"cached"', "last_modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "data": [_movie_entry(simkl_id=42)],
        })
        client = _Client(_Resp(status=304))
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            records = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual(len(client.urls), 1)  # exactly one request — the 304 itself
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].ids.get("simkl"), 42)
        # And the conditional headers carried the stored ETag/Last-Modified.
        self.assertEqual(client.headers[0].get("If-None-Match"), '"cached"')
        self.assertEqual(client.headers[0].get("If-Modified-Since"),
                         "Mon, 01 Jan 2026 00:00:00 GMT")

    async def test_a_200_replaces_the_stored_copy(self):
        url = f"{simkl_calendar.CDN_BASE}/2026/7/movie_release.json"
        await cache.set(simkl_calendar._cdn_cache_key(url), {
            "etag": '"old"', "last_modified": None, "data": [_movie_entry(simkl_id=1)],
        })
        client = _Client(_Resp([_movie_entry(simkl_id=2)], headers={"ETag": '"new"'}))
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            records = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual([r.ids.get("simkl") for r in records], [2])
        stored = await cache.get_stale(simkl_calendar._cdn_cache_key(url))
        self.assertEqual(stored["etag"], '"new"')

    async def test_a_404_archive_month_is_an_empty_answer_not_a_refusal(self):
        """The fill only asks for months inside the declared Capabilities
        window, so a 404 reaching this far means the archive genuinely has
        nothing for it — an empty answer, not SourceUnavailable."""
        client = _Client(_Resp(status=404))
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            records = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual(records, [])

    async def test_a_500_raises_source_unavailable(self):
        client = _Client(_Resp(status=500))
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            with self.assertRaises(SourceUnavailable):
                await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)

    async def test_an_unreadable_body_raises_rather_than_reading_as_empty(self):
        class _Unreadable(_Resp):
            def json(self):
                raise ValueError("not json")
        client = _Client(_Unreadable())
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            with self.assertRaises(SimklError):
                await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
