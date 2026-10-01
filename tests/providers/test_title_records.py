"""DetailPort.records_for and the small per-title reads beside it, per source.

`records_for` is how a title a calendar search found — but no calendar feed
listed — becomes calendar rows: each source builds them with ITS OWN calendar
record builder from one per-title lookup (app/providers/base.py says why a
second builder was the bug). The search's own tests replace these, so the
adapters themselves were read by nothing. Here the per-title lookup is the stub
and the record builder is real, which is the seam that has broken before: a
Simkl film came back as no record at all because the adapter handed its builder
a `date` where a film is read from `release_date`.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app import providers
from app.config import Settings
from app.providers.base import Media, Source
from app.providers.simkl import titles as simkl_titles
from app.providers.trakt import detail as trakt_detail

SETTINGS = Settings()


def _port(source: Source):
    return providers.registered()[source].detail_port


class TraktRecordsTests(unittest.IsolatedAsyncioTestCase):
    SHOW = {"title": "The Show", "year": 2026, "network": "HBO", "country": "us",
            "genres": ["drama"], "ids": {"trakt": 41, "slug": "the-show", "tmdb": 900}}

    async def _records(self, payload, media, moments):
        with patch.object(trakt_detail, "fetch_title_payload",
                          AsyncMock(return_value=payload)) as lookup:
            records = await _port(Source.TRAKT).records_for(SETTINGS, 41, media, moments)
        return records, lookup

    async def test_one_lookup_builds_a_record_per_premiere_as_episode_one(self):
        """A season premiere is episode one, and saying so is what lets the feed's
        own row for that airing replace this one rather than sit beside it."""
        records, lookup = await self._records(
            self.SHOW, Media.SHOW,
            [(2, "2026-07-01T01:00:00.000Z"), (3, "2027-07-01T01:00:00.000Z")])
        lookup.assert_awaited_once()
        self.assertEqual([(r.season, r.episode_number) for r in records], [(2, 1), (3, 1)])
        self.assertEqual({r.title for r in records}, {"The Show"})
        self.assertEqual({r.source for r in records}, {Source.TRAKT})

    async def test_a_film_has_no_episode_and_lands_on_its_day(self):
        film = {"title": "A Film", "year": 2026, "ids": {"trakt": 7, "tmdb": 70}}
        records, _ = await self._records(film, Media.MOVIE, [(None, "2026-08-14")])
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].episode_number)
        self.assertTrue(records[0].date_only)

    async def test_a_title_trakt_cannot_describe_is_no_records(self):
        records, _ = await self._records(None, Media.SHOW, [(1, "2026-07-01T01:00:00.000Z")])
        self.assertEqual(records, [])


class SimklRecordsTests(unittest.IsolatedAsyncioTestCase):
    FIELDS = {"ids": {"simkl": 55, "tmdb": 9}, "title": "A Title", "poster": "ab/cd"}

    async def _records(self, fields, media, moments):
        with patch.object(simkl_titles, "fetch_title", AsyncMock(return_value=fields)):
            return await _port(Source.SIMKL).records_for(SETTINGS, 55, media, moments)

    async def test_a_film_becomes_a_record_on_its_release_day(self):
        """THE BUG THIS PINS: handed `date` alone, Simkl's film builder answered
        nothing, so a film only Simkl knew could not be jumped to from a search."""
        records = await self._records(self.FIELDS, Media.MOVIE, [(None, "2026-08-14")])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].media, Media.MOVIE)
        self.assertTrue(records[0].date_only)
        # An instant handed in for a film is still read as its day.
        again = await self._records(self.FIELDS, Media.MOVIE,
                                    [(None, "2026-08-14T04:00:00Z")])
        self.assertEqual(again[0].air_ts, records[0].air_ts)

    async def test_a_show_premiere_is_episode_one_and_arrives_unenriched(self):
        """Simkl's calendar files carry no genres or country, so a record built
        the way a filled one is comes back unenriched exactly like it — the
        search's next step is the same enrichment the fill relies on."""
        records = await self._records(self.FIELDS, Media.SHOW, [(2, "2026-07-01")])
        self.assertEqual([(r.season, r.episode_number) for r in records], [(2, 1)])
        self.assertFalse(records[0].enriched)

    async def test_a_title_with_no_ids_or_no_answer_is_no_records(self):
        for fields in (None, {"title": "No ids"}):
            with self.subTest(fields=fields):
                self.assertEqual(
                    await self._records(fields, Media.SHOW, [(1, "2026-07-01")]), [])


class TraktTitleReadsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_films_season_is_empty_without_asking(self):
        """Movies have no seasons, and answering an empty one is what lets the
        caller treat every medium alike rather than branching first."""
        with patch.object(trakt_detail, "fetch_season_detail", AsyncMock()) as asked:
            summary = await _port(Source.TRAKT).fetch_season_summary(
                SETTINGS, 7, 1, Media.MOVIE)
        asked.assert_not_awaited()
        self.assertEqual(summary["total"], 0)

    async def test_title_facts_read_the_matching_summary_for_each_medium(self):
        answers = {"shows/41": {"title": " The Show ", "network": "HBO "},
                   "movies/7": {"title": "A Film", "network": None}}

        async def cached_get(client, settings, path, params, **kw):
            return answers.get(path)

        with patch.object(trakt_detail.transport, "cached_get", cached_get), \
             patch.object(trakt_detail.transport, "shared_client", lambda: None):
            show = await trakt_detail.fetch_title_facts(SETTINGS, 41, Media.SHOW)
            film = await trakt_detail.fetch_title_facts(SETTINGS, 7, Media.MOVIE)
            nothing = await trakt_detail.fetch_title_facts(SETTINGS, 99, Media.SHOW)
        self.assertEqual((show.title, show.network), ("The Show", "HBO"))
        self.assertEqual((film.title, film.network), ("A Film", ""))
        self.assertEqual((nothing.title, nothing.network), ("", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
