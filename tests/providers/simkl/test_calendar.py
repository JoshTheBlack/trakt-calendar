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


def _anime_entry(simkl_id=7, slug="an-anime", anime_type="tv", episode=1,
                 when="2026-07-07T00:00:00+09:00", release_date=None):
    """One anime.json entry. `anime_type` is on the CALENDAR entry itself —
    measured, all 1605 entries across five months of live archives carry it —
    and `release_date` defaults to something DIFFERENT from `date` because on
    this file the two genuinely disagree for a re-listed title."""
    entry = {
        "title": "An Anime", "poster": "20/aaa111", "date": when,
        "release_date": release_date if release_date is not None else "2017-12-09",
        "ids": {"simkl_id": simkl_id, "slug": slug, "tmdb": None, "mal": "63973"},
        "url": f"https://simkl.com/anime/{simkl_id}/{slug}",
        "anime_type": anime_type,
    }
    if episode is not None:
        entry["episode"] = {"episode": episode, "url": "x"}
    return entry


class AnimeFilmRoutingTests(unittest.TestCase):
    """`is_anime_film` and `to_anime_film_record` — which calendar an anime
    entry belongs on, decided at the fill from the file's own `anime_type`.

    Simkl files an anime FILM on its anime calendar and never on its movie
    one, so without this the title is on the series calendar (where a film
    does not belong) or, once the read-time prune takes it off, on no calendar
    at all — which is the state these exist to end.
    """

    def test_the_file_says_which_entries_are_films(self):
        self.assertTrue(simkl_calendar.is_anime_film(_anime_entry(anime_type="movie")))

    def test_every_serial_anime_type_is_not_a_film(self):
        """ona (Original Net Animation — a web-released SERIES), ova, tv and
        special are all episodic formats and must stay on the show endpoints.
        Only `movie` is a film."""
        for serial in ("ona", "ova", "tv", "special"):
            with self.subTest(anime_type=serial):
                self.assertFalse(simkl_calendar.is_anime_film(_anime_entry(anime_type=serial)))

    def test_an_entry_with_no_anime_type_at_all_is_not_routed(self):
        """The fill cannot decide what the file does not say, so such an entry
        stays where it was — on the show endpoints, where the read-time prune
        in app/calendar/filter.py can still act on it once enrichment answers."""
        entry = _anime_entry()
        del entry["anime_type"]
        self.assertFalse(simkl_calendar.is_anime_film(entry))

    def test_a_film_record_is_movie_media_with_no_episode_coordinate(self):
        record = simkl_calendar.to_anime_film_record(_anime_entry(anime_type="movie", episode=2))
        self.assertEqual(record.media, Media.MOVIE)
        self.assertEqual(record.source, Source.SIMKL)
        self.assertIsNone(record.season)
        self.assertIsNone(record.episode_number)
        self.assertIsNone(record.episode_label)
        self.assertFalse(record.enriched)

    def test_a_film_is_dated_from_date_not_release_date(self):
        """THE MEASURED TRAP. On anime.json `release_date` is the title's
        ORIGINAL release and `date` is the day it is being calendared on;
        Girls und Panzer das Finale is listed on 2026-10-09 with a
        release_date of 2017-12-09. Dating it from release_date would put it
        outside the window that fetched it, `in_window` would trim it away,
        and the film would vanish again."""
        from datetime import datetime
        record = simkl_calendar.to_anime_film_record(_anime_entry(
            anime_type="movie", when="2026-10-09T00:00:00+09:00", release_date="2017-12-09"))
        landed = datetime.fromtimestamp(record.air_ts, tz=timezone.utc)
        self.assertEqual(landed.date().isoformat(), "2026-10-08")  # 00:00 JST is the 8th in UTC
        # NOT date_only: the anime file's offset is real, unlike the movie
        # file's fixed -04:00 midnight, so the instant converts correctly and
        # routing the title does not also move it.
        self.assertFalse(record.date_only)

    def test_a_film_with_no_date_is_dropped_not_raised_over(self):
        entry = _anime_entry(anime_type="movie")
        del entry["date"]
        self.assertIsNone(simkl_calendar.to_anime_film_record(entry))


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
        # Two files, not one: the anime archive is read alongside the movie one
        # because Simkl files an anime FILM there. Asserted as a set — the two
        # are fetched concurrently, so their order is not a fact to pin.
        self.assertEqual({u.rsplit("/", 1)[-1] for u in client.urls},
                         {"movie_release.json", "anime.json"})
        for url in client.urls:
            self.assertIn("/2026/7/", url)
            self.assertNotIn("?", url)  # no cache-buster, no query string at all

    async def test_shows_unions_tv_and_anime(self):
        async def fake_get(url):
            return [_tv_entry(simkl_id=1)] if "tv.json" in url else [_tv_entry(simkl_id=2, slug="b")]
        with patch("app.providers.simkl.calendar._conditional_get", side_effect=fake_get):
            records = await simkl_calendar.fetch_window(SHOWS, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual({r.id for r in records}, {"a-show", "b"})

    async def test_an_anime_film_lands_on_the_movies_endpoint(self):
        """Shiranuhi's shape: in anime.json, marked `anime_type: "movie"`, and
        absent from movie_release.json entirely — measured, no entry any
        month's anime file marks a movie appears in that month's movie file."""
        film = _anime_entry(simkl_id=3157124, slug="shiranuhi", anime_type="movie")
        film["title"] = "Shiranuhi"

        async def fake_get(url):
            if "anime.json" in url:
                return [film]
            return [_movie_entry(simkl_id=11)]

        with patch("app.providers.simkl.calendar._conditional_get", side_effect=fake_get):
            records = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
        by_title = {r.title: r for r in records}
        self.assertIn("Shiranuhi", by_title)
        self.assertEqual(by_title["Shiranuhi"].media, Media.MOVIE)
        # The ordinary movie file's own entries are untouched beside it.
        self.assertIn("A Movie", by_title)

    async def test_the_same_film_is_kept_off_every_series_endpoint(self):
        """The other half of one predicate: a film the movies fill claims must
        leave the show derivations, or one title renders on both calendars."""
        film = _anime_entry(simkl_id=3157124, slug="shiranuhi", anime_type="movie")
        film["title"] = "Shiranuhi"

        async def fake_get(url):
            if "anime.json" in url:
                return [film, _anime_entry(simkl_id=8, slug="a-series", anime_type="ona")]
            return [_tv_entry(simkl_id=1)]

        for endpoint in (SHOWS, SHOWS_NEW, PREMIERES):
            with self.subTest(endpoint=endpoint.key):
                with patch("app.providers.simkl.calendar._conditional_get", side_effect=fake_get):
                    records = await simkl_calendar.fetch_window(
                        endpoint, SETTINGS, date(2026, 7, 6), 7)
                self.assertNotIn("Shiranuhi", [r.title for r in records])

    async def test_a_serial_anime_stays_on_the_series_endpoints_and_off_the_movies_one(self):
        """ona/ova/tv/special are serial formats. None of them is routed."""
        async def fake_get(url):
            if "anime.json" in url:
                return [_anime_entry(simkl_id=100 + n, slug=f"s{n}", anime_type=serial)
                        for n, serial in enumerate(("ona", "ova", "tv", "special"))]
            return []

        with patch("app.providers.simkl.calendar._conditional_get", side_effect=fake_get):
            movies = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
            series = await simkl_calendar.fetch_window(SHOWS, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual(movies, [])
        self.assertEqual(len(series), 4)
        self.assertTrue(all(r.media == Media.SHOW for r in series))

    async def test_an_unlabelled_anime_entry_stays_on_the_series_endpoints(self):
        """THE DECLARED TRANSITIONAL STATE. An entry the file does not label
        cannot be routed at fill; it stays a series entry and does not error,
        and app/calendar/filter.py's read-time prune is what still acts on it
        if enrichment later calls it a film."""
        unlabelled = _anime_entry(simkl_id=55, slug="unlabelled")
        del unlabelled["anime_type"]

        async def fake_get(url):
            return [unlabelled] if "anime.json" in url else []

        with patch("app.providers.simkl.calendar._conditional_get", side_effect=fake_get):
            movies = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
            series = await simkl_calendar.fetch_window(SHOWS, SETTINGS, date(2026, 7, 6), 7)
        self.assertEqual(movies, [])
        self.assertEqual([r.id for r in series], ["unlabelled"])

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
        # The movies fill reads the anime archive too (an anime film lives
        # there), so that file gets a stored copy of its own — otherwise this
        # would be testing the 304 path on one file and the cold path on
        # another.
        anime_url = f"{simkl_calendar.CDN_BASE}/2026/7/anime.json"
        await cache.set(simkl_calendar._cdn_cache_key(anime_url), {
            "etag": '"cached-anime"', "last_modified": None, "data": [],
        })
        client = _Client(_Resp(status=304))
        with patch("app.providers.simkl.transport.cdn_client", return_value=client):
            records = await simkl_calendar.fetch_window(MOVIES, SETTINGS, date(2026, 7, 6), 7)
        # One request PER FILE — the 304 itself — and no second request to
        # fetch a body the 304 said had not changed.
        self.assertEqual(len(client.urls), 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].ids.get("simkl"), 42)
        # And the conditional headers carried the stored ETag/Last-Modified.
        sent = dict(zip((u.rsplit("/", 1)[-1] for u in client.urls), client.headers))
        self.assertEqual(sent["movie_release.json"].get("If-None-Match"), '"cached"')
        self.assertEqual(sent["movie_release.json"].get("If-Modified-Since"),
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
