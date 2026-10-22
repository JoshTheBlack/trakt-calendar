"""app/providers/tmdb/normalize.py — TMDB's payloads in this app's vocabulary.

Driven by REAL answers, trimmed and saved under tests/fixtures/tmdb/ (no
credential in any of them): Severance (a show with a US rating and a finished
second season), Frieren (an anime from Japan), Dune: Part Two (a film rated per
release), Severance's second season, and TMDB's two genre lists.
"""
from __future__ import annotations

import json
import unittest
from datetime import date

from app.calendar import vocab
from app.providers.base import Media, Source, VideoKind
from app.providers.tmdb import normalize
from tests.support import APP_DIR

FIXTURES = APP_DIR.parent / "tests" / "fixtures" / "tmdb"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


SEVERANCE = fixture("tv_severance")
FRIEREN = fixture("tv_frieren")
DUNE = fixture("movie_dune_part_two")
SEASON_2 = fixture("tv_severance_season_2")


class EveryTmdbGenreIsADecisionTests(unittest.TestCase):
    """The genre table names every genre TMDB lists, and maps each onto this
    app's own slugs — or, for TV Movie, onto nothing, deliberately."""

    def test_every_genre_tmdb_lists_has_an_entry(self):
        listed = {g["id"] for media in ("tv", "movie")
                  for g in fixture(f"genre_{media}_list")["genres"]}
        self.assertEqual(listed - set(normalize.GENRE_SLUGS), set())

    def test_every_mapped_slug_is_in_the_apps_vocabulary(self):
        """A slug no filter offers would be a genre nobody could narrow by."""
        offered = {chip.token for field in (vocab.TV_GENRES, vocab.MOVIE_GENRES)
                   for chip in vocab.VOCABULARY[field]}
        mapped = {slug for slugs in normalize.GENRE_SLUGS.values() for slug in slugs}
        self.assertEqual(mapped - offered, set())

    def test_the_combined_genres_split(self):
        cases = [
            ([{"id": 10759, "name": "Action & Adventure"}], ["action", "adventure"]),
            ([{"id": 10765, "name": "Sci-Fi & Fantasy"}], ["science-fiction", "fantasy"]),
            ([{"id": 10768, "name": "War & Politics"}], ["war"]),
            ([{"id": 878, "name": "Science Fiction"}], ["science-fiction"]),
            ([{"id": 10762, "name": "Kids"}], ["family"]),
            ([{"id": 10767, "name": "Talk"}], ["talk-show"]),
            ([{"id": 10402, "name": "Music"}], ["musical"]),
            ([{"id": 10770, "name": "TV Movie"}], []),
            # Read by id, so a name in another language maps the same way.
            ([{"id": 80, "name": "Krimi"}], ["crime"]),
            # Two genres landing on one slug name it once.
            ([{"id": 10751, "name": "Family"}, {"id": 10762, "name": "Kids"}], ["family"]),
        ]
        for genres, expected in cases:
            with self.subTest(genres=genres):
                self.assertEqual(normalize.genre_slugs(genres), expected)

    def test_a_genre_tmdb_adds_later_is_slugged_and_said(self):
        with self.assertLogs("app.providers.tmdb.normalize", "INFO"):
            self.assertEqual(normalize.genre_slugs([{"id": 1, "name": "Very New"}]),
                             ["very-new"])

    def test_real_payloads(self):
        self.assertEqual(normalize.genre_slugs(SEVERANCE["genres"]),
                         ["drama", "mystery", "science-fiction", "fantasy"])
        self.assertEqual(normalize.genre_slugs(FRIEREN["genres"]),
                         ["animation", "action", "adventure", "drama",
                          "science-fiction", "fantasy"])


class TheTitleFactsTests(unittest.TestCase):

    def test_table(self):
        cases = [
            # (what, payload, media, reader, expected)
            ("US rating first", SEVERANCE, Media.SHOW, normalize.certification, "TV-MA"),
            ("anime's US rating", FRIEREN, Media.SHOW, normalize.certification, "TV-14"),
            ("a film's earliest rated US release", DUNE, Media.MOVIE,
             normalize.certification, "PG-13"),
            ("country", SEVERANCE, Media.SHOW, lambda p, m: normalize.country_code(p), "US"),
            ("anime country", FRIEREN, Media.SHOW, lambda p, m: normalize.country_code(p), "JP"),
            ("language", FRIEREN, Media.SHOW, lambda p, m: normalize.language_code(p), "JA"),
            ("show runtime is its last episode's", SEVERANCE, Media.SHOW, normalize.runtime, 80),
            ("film runtime", DUNE, Media.MOVIE, normalize.runtime, 167),
            ("show status in Trakt's words", SEVERANCE, Media.SHOW,
             lambda p, m: normalize.status_word(p["status"]), "returning series"),
            ("film status", DUNE, Media.MOVIE,
             lambda p, m: normalize.status_word(p["status"]), "released"),
            ("network", SEVERANCE, Media.SHOW, lambda p, m: normalize.network_of(p), "Apple TV"),
            ("show title", SEVERANCE, Media.SHOW, normalize.title_of, "Severance"),
            ("film title", DUNE, Media.MOVIE, normalize.title_of, "Dune: Part Two"),
            ("year", SEVERANCE, Media.SHOW, normalize.year_of, 2022),
            ("rating past the vote floor", SEVERANCE, Media.SHOW,
             lambda p, m: normalize.rating(p), 8.4),
        ]
        for what, payload, media, reader, expected in cases:
            with self.subTest(what):
                self.assertEqual(reader(payload, media), expected)

    def test_no_us_rating_falls_to_the_origin_countrys(self):
        payload = {"origin_country": ["GB"], "content_ratings": {"results": [
            {"iso_3166_1": "DE", "rating": "12"}, {"iso_3166_1": "GB", "rating": "15"}]}}
        self.assertEqual(normalize.certification(payload, Media.SHOW), "15")

    def test_no_rating_anywhere_useful_is_none(self):
        payload = {"origin_country": ["GB"], "content_ratings": {"results": [
            {"iso_3166_1": "DE", "rating": "12"}]}}
        self.assertEqual(normalize.certification(payload, Media.SHOW), "")

    def test_a_score_from_too_few_votes_is_no_score(self):
        self.assertIsNone(normalize.rating({"vote_average": 9.9, "vote_count": 3}))

    def test_ids_come_from_external_ids(self):
        self.assertEqual(normalize.ids_of(SEVERANCE),
                         {"tmdb": 95396, "imdb": "tt11280740", "tvdb": 371980})
        self.assertEqual(normalize.ids_of(DUNE), {"tmdb": 693134, "imdb": "tt15239678"})

    def test_release_types_are_tmdbs_own_numbering(self):
        types = normalize.release_types_by_country(DUNE)
        self.assertIn(3, types["US"])
        self.assertEqual(types["FR"], [1, 3, 5])

    def test_the_poster_is_an_origin_address(self):
        self.assertEqual(normalize.poster_of(SEVERANCE),
                         "https://image.tmdb.org/t/p/w500/pPHpeI2X1qEd1CS1SeyrdhZ4qnT.jpg")


class VideosTests(unittest.TestCase):

    def test_youtube_entries_become_videos_tmdb_listed(self):
        videos = normalize.videos_from(SEVERANCE)
        self.assertEqual(len(videos), 3)
        first = videos[0]
        self.assertEqual((first.site, first.video_id, first.kind, first.official),
                         ("youtube", "ufaAnhgiERE", VideoKind.FEATURETTE, True))
        self.assertEqual(first.sources, (str(Source.TMDB),))
        self.assertTrue(first.embeddable)

    def test_an_unknown_site_is_skipped(self):
        payload = {"videos": {"results": [{"site": "Dailymotion", "key": "x", "type": "Trailer"}]}}
        self.assertEqual(normalize.videos_from(payload), [])


class CastTests(unittest.TestCase):
    """normalize.cast_from — both of TMDB's cast shapes, as the panel's keys."""

    def test_a_shows_aggregate_cast_names_each_persons_role(self):
        cast = normalize.cast_from(fixture("cast_tv_severance"))
        self.assertEqual(cast[0]["name"], "Adam Scott")
        self.assertTrue(cast[0]["character"])
        self.assertTrue(cast[0]["headshot"].startswith("https://wsrv.nl/"))
        self.assertEqual(set(cast[0]), {"name", "character", "headshot"})

    def test_a_films_cast_names_its_character(self):
        cast = normalize.cast_from(fixture("cast_movie_dune_part_two"))
        self.assertTrue(cast[0]["name"] and cast[0]["character"])

    def test_the_cap_is_the_panels(self):
        from app.providers.base import CAST_SHOWN
        many = {"cast": [{"name": f"P{n}", "character": "x"} for n in range(50)]}
        self.assertEqual(len(normalize.cast_from(many)), CAST_SHOWN)

    def test_no_picture_is_no_headshot(self):
        self.assertIsNone(normalize.cast_from({"cast": [{"name": "A"}]})[0]["headshot"])


class SeasonsTests(unittest.TestCase):

    def test_a_season_summary(self):
        """The fixture keeps episodes 1-3 and 10 of season 2; a summary counts
        what TMDB listed and reads its days unconverted."""
        summary = normalize.season_summary(SEASON_2, 2, date(2026, 10, 22))
        self.assertEqual(summary["season"], 2)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["premiere"], "1/16")
        self.assertEqual(summary["cadence"], "Thu")
        self.assertTrue(summary["finished_airing"])

    def test_a_season_with_no_episodes_is_empty(self):
        self.assertEqual(normalize.season_summary({}, 3, date(2026, 1, 1))["total"], 0)

    def test_the_picker_skips_specials_and_empty_seasons(self):
        seasons = normalize.picker_seasons(SEVERANCE)
        self.assertEqual([s["season"] for s in seasons], [1, 2])
        self.assertEqual(seasons[1]["episode_count"], 10)
        self.assertEqual(seasons[1]["first_aired"], "2025-01-16")
        self.assertEqual(seasons[1]["name"], "")  # "Season 2" restates the number

    def test_panel_episodes(self):
        rows = normalize.panel_episodes(SEASON_2)
        self.assertEqual(rows[0]["number"], 1)
        self.assertEqual(rows[0]["title"], "Hello, Ms. Cobel")
        self.assertEqual(rows[0]["air_display"], "16 Jan 2025")


class TheRecordTests(unittest.TestCase):

    def test_a_season_premiere_record_is_a_dated_episode_one(self):
        record = normalize.to_record(SEVERANCE, Media.SHOW, 2, "2025-01-16")
        self.assertEqual((record.source, record.id, record.season, record.episode_number),
                         (Source.TMDB, "95396", 2, 1))
        self.assertEqual(record.episode_label, "S02E01")
        self.assertTrue(record.date_only)
        self.assertEqual(record.genres, ["drama", "mystery", "science-fiction", "fantasy"])
        self.assertEqual(record.certification, "TV-MA")
        self.assertEqual(record.detail_url, "https://www.themoviedb.org/tv/95396")

    def test_a_film_record(self):
        record = normalize.to_record(DUNE, Media.MOVIE, None, "2024-03-01")
        self.assertIsNone(record.season)
        self.assertEqual(record.network, "")
        self.assertIn("US", record.release_types_by_country)

    def test_an_unreadable_day_is_no_record(self):
        self.assertIsNone(normalize.to_record(SEVERANCE, Media.SHOW, 1, "soon"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
