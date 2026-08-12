"""Where a film is released, according to Trakt.

WHY THIS EXISTS AT ALL, and it was found in a browser rather than here: Trakt's
calendar payload carries no release schedule, so a film Trakt listed reached the
release filter with nothing to be judged on — and a record that cannot answer is
kept. On a film both services listed that meant the Trakt record's silence kept
the group whatever the Simkl record's map said, so the filter could not drop any
film Trakt also listed. 19 of 29 survivors on one real August.

Trakt's per-title endpoint does carry it, in the shape the filter already reads.
These tests cover the translation, the store, the read-time overlay and the
bounded drain — no network anywhere; the transport is patched at its own module.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from datetime import date
from zoneinfo import ZoneInfo

from app import db
from app.calendar import cache as calendar_cache, enrich as calendar_enrich
from app.endpoints import get_endpoint
from app.config import Settings
from app.providers.base import Media, Record, Source
from app.providers.trakt import releases as trakt_releases
from tests.support import new_db_path

_AIR_TS = 1783425600.0

# What Trakt actually answers with, taken from a live call on 2026-08-11.
LIVE_ROWS = [
    {"country": "ie", "certification": None, "release_date": "2026-07-31",
     "release_type": "premiere", "note": "GAZE International LGBTQIA Film Festival"},
    {"country": "au", "certification": "MA 15+", "release_date": "2026-07-31",
     "release_type": "digital", "note": None},
    {"country": "us", "certification": "R", "release_date": "2026-08-01",
     "release_type": "digital", "note": None},
]


def _film(trakt_id: int, *, source=Source.TRAKT, media=Media.MOVIE) -> Record:
    return Record(source=source, media=media, id=str(trakt_id),
                  ids={"trakt": trakt_id}, detail_url="", title="A Film",
                  air_ts=_AIR_TS, date_only=True)


class TranslatingTraktsWordsIntoTheFiltersNumbersTests(unittest.TestCase):
    """Trakt spells its release types as words; the filter speaks TMDB's
    numbers, because one release vocabulary serves both services and the Filters
    panel is written in it. This is the one place the two spellings meet."""

    def test_the_live_shape_reduces_to_the_filters_shape(self):
        self.assertEqual(trakt_releases.release_types_by_country(LIVE_ROWS),
                         {"IE": [1], "AU": [4], "US": [4]})

    def test_every_documented_type_translates(self):
        rows = [{"country": "us", "release_type": word} for word in
                ("premiere", "limited", "theatrical", "digital", "physical", "tv")]
        self.assertEqual(trakt_releases.release_types_by_country(rows),
                         {"US": [1, 2, 3, 4, 5, 6]})

    def test_a_country_is_upper_cased_and_its_types_sorted_and_deduplicated(self):
        """One film's map has to be ONE value however Trakt ordered the rows, or
        the same schedule stored twice would not compare equal to itself."""
        rows = [{"country": "Us", "release_type": "digital"},
                {"country": "us", "release_type": "theatrical"},
                {"country": "US", "release_type": "digital"}]
        self.assertEqual(trakt_releases.release_types_by_country(rows), {"US": [3, 4]})

    def test_a_word_this_app_does_not_know_is_dropped_rather_than_guessed(self):
        """A wrong number would put a film in a filter it does not belong in; a
        missing one only leaves that release unfilterable, which is the safe
        direction. The country survives on its other release."""
        rows = [{"country": "us", "release_type": "hologram"},
                {"country": "us", "release_type": "theatrical"},
                {"country": "gb", "release_type": "hologram"}]
        self.assertEqual(trakt_releases.release_types_by_country(rows), {"US": [3]})

    def test_nothing_usable_is_an_empty_map_rather_than_a_raise(self):
        for rows in (None, [], [{}], ["nonsense"], [{"country": "us"}]):
            with self.subTest(rows=rows):
                self.assertEqual(trakt_releases.release_types_by_country(rows), {})


class FetchingGoesThroughTheTransportTests(unittest.IsolatedAsyncioTestCase):
    """The rule for every Trakt call: the transport, never httpx. That is where
    the outbound semaphore and the 429 retry loop live, so a caller reaching past
    it would pace nothing."""

    SETTINGS = Settings(trakt_client_id="cid")

    async def test_it_asks_the_releases_endpoint_through_cached_get(self):
        with patch("app.providers.trakt.transport.cached_get",
                   new=AsyncMock(return_value=LIVE_ROWS)) as call:
            out = await trakt_releases.fetch_releases(self.SETTINGS, 42)
        self.assertEqual(out, {"IE": [1], "AU": [4], "US": [4]})
        self.assertEqual(call.await_args.args[2], "movies/42/releases")

    async def test_it_is_cached_publicly_because_nobody_s_token_decides_it(self):
        """A release schedule does not depend on whose token asked, so it may be
        written to the shared cache — which is what makes one title several
        windows name cost one call."""
        with patch("app.providers.trakt.transport.cached_get",
                   new=AsyncMock(return_value=[])) as call:
            await trakt_releases.fetch_releases(self.SETTINGS, 42)
        self.assertNotIn("private", call.await_args.kwargs)
        self.assertGreater(call.await_args.kwargs["ttl_seconds"], 0)

    async def test_a_failure_is_none_rather_than_an_exception(self):
        from app.providers.trakt import TraktError
        with patch("app.providers.trakt.transport.cached_get",
                   new=AsyncMock(side_effect=TraktError("nope", 500))):
            self.assertIsNone(await trakt_releases.fetch_releases(self.SETTINGS, 42))

    async def test_an_answer_that_is_not_a_list_is_none(self):
        with patch("app.providers.trakt.transport.cached_get",
                   new=AsyncMock(return_value={"error": "?"})):
            self.assertIsNone(await trakt_releases.fetch_releases(self.SETTINGS, 42))

    async def test_no_id_asks_nothing(self):
        with patch("app.providers.trakt.transport.cached_get", new=AsyncMock()) as call:
            self.assertIsNone(await trakt_releases.fetch_releases(self.SETTINGS, None))
        call.assert_not_awaited()


class TheStoreAndTheOverlayTests(unittest.IsolatedAsyncioTestCase):
    SETTINGS = Settings(trakt_client_id="cid")

    async def asyncSetUp(self):
        new_db_path("trakt-releases")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_a_stored_map_reaches_the_record(self):
        await calendar_enrich._upsert_release_success(42, {"US": [3]}, db.now())
        record = _film(42)
        await calendar_enrich.overlay_releases([record])
        self.assertEqual(record.release_types_by_country, {"US": [3]})

    async def test_a_film_with_nothing_stored_is_left_unable_to_answer(self):
        """Which the filter reads as "keep", so a film waiting on its first
        lookup is never dropped by a narrowing."""
        record = _film(42)
        await calendar_enrich.overlay_releases([record])
        self.assertEqual(record.release_types_by_country, {})

    async def test_it_never_speaks_for_another_service_s_record(self):
        """A Trakt answer fills a Trakt record and nothing else. Letting one
        service's catalogue fill the other's record would make a card's
        provenance a lie — the same rule the Simkl overlay follows."""
        await calendar_enrich._upsert_release_success(42, {"US": [3]}, db.now())
        simkl = Record(source=Source.SIMKL, media=Media.MOVIE, id="42",
                       ids={"trakt": 42, "simkl": 9}, detail_url="", title="A Film",
                       air_ts=_AIR_TS)
        await calendar_enrich.overlay_releases([simkl])
        self.assertEqual(simkl.release_types_by_country, {})

    async def test_a_show_is_never_asked_about(self):
        """A release schedule is a thing only films have, and the endpoint is
        /movies/{id}/releases — asking it about a show could only ever 404."""
        show = _film(42, media=Media.SHOW)
        self.assertEqual(calendar_enrich._trakt_film_candidates([show]), {})

    async def test_an_empty_answer_is_stored_and_is_not_a_failure(self):
        """A film Trakt knows and has announced no releases for. Storing it is
        what stops it being asked about again on every tick."""
        await calendar_enrich._upsert_release_success(42, {}, db.now())
        row, = await db.fetch_all("SELECT failed_at, fail_count FROM trakt_releases")
        self.assertIsNone(row["failed_at"])
        self.assertEqual(row["fail_count"], 0)

    async def test_a_failure_leaves_an_earlier_answer_in_place(self):
        """A transient failure says "this attempt did not confirm it", never
        "forget what you knew"."""
        now = db.now()
        await calendar_enrich._upsert_release_success(42, {"US": [3]}, now)
        await calendar_enrich._upsert_release_failure(42, now)
        record = _film(42)
        await calendar_enrich.overlay_releases([record])
        self.assertEqual(record.release_types_by_country, {"US": [3]})


class TheDrainTests(unittest.IsolatedAsyncioTestCase):
    SETTINGS = Settings(trakt_client_id="cid")

    async def asyncSetUp(self):
        new_db_path("trakt-release-drain")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()

    def _owed(self, *trakt_ids, media="movie"):
        return patch.object(
            calendar_enrich, "_owed_films",
            new=AsyncMock(return_value={i: f"Film {i}" for i in trakt_ids}))

    async def test_it_reads_what_the_cache_names_and_stores_it(self):
        with self._owed(1, 2), patch.object(
                trakt_releases, "fetch_releases",
                new=AsyncMock(return_value={"US": [3]})) as fetch:
            self.assertEqual(await calendar_enrich.drain_releases(self.SETTINGS), 2)
        self.assertEqual(fetch.await_count, 2)
        rows = await db.fetch_all("SELECT trakt_id FROM trakt_releases ORDER BY trakt_id")
        self.assertEqual([r["trakt_id"] for r in rows], [1, 2])

    async def test_a_film_already_answered_is_not_asked_again(self):
        await calendar_enrich._upsert_release_success(1, {"US": [3]}, db.now())
        with self._owed(1), patch.object(
                trakt_releases, "fetch_releases", new=AsyncMock()) as fetch:
            self.assertEqual(await calendar_enrich.drain_releases(self.SETTINGS), 0)
        fetch.assert_not_awaited()

    async def test_a_film_answered_with_nothing_is_not_asked_again_either(self):
        """The whole point of storing an empty answer."""
        await calendar_enrich._upsert_release_success(1, {}, db.now())
        with self._owed(1), patch.object(
                trakt_releases, "fetch_releases", new=AsyncMock()) as fetch:
            await calendar_enrich.drain_releases(self.SETTINGS)
        fetch.assert_not_awaited()

    async def test_a_recent_failure_backs_off(self):
        now = db.now()
        await calendar_enrich._upsert_release_failure(1, now)
        with self._owed(1), patch.object(
                trakt_releases, "fetch_releases", new=AsyncMock()) as fetch:
            await calendar_enrich.drain_releases(self.SETTINGS, now=now + 60)
        fetch.assert_not_awaited()

    async def test_a_failure_whose_backoff_has_elapsed_is_asked_again(self):
        now = db.now()
        await calendar_enrich._upsert_release_failure(1, now)
        with self._owed(1), patch.object(
                trakt_releases, "fetch_releases",
                new=AsyncMock(return_value={"US": [3]})) as fetch:
            await calendar_enrich.drain_releases(self.SETTINGS, now=now + 60 * 60 * 48)
        self.assertEqual(fetch.await_count, 1)

    async def test_one_tick_is_bounded(self):
        """A backlog drains over several ticks rather than becoming one burst
        against a service that gates every call behind one semaphore."""
        many = range(1, calendar_enrich.RELEASE_DRAIN_BATCH_SIZE + 6)
        with self._owed(*many), patch.object(
                trakt_releases, "fetch_releases",
                new=AsyncMock(return_value={"US": [3]})) as fetch:
            await calendar_enrich.drain_releases(self.SETTINGS)
        self.assertEqual(fetch.await_count, calendar_enrich.RELEASE_DRAIN_BATCH_SIZE)

    async def test_one_film_failing_does_not_end_the_pass(self):
        async def _flaky(settings, trakt_id, **kwargs):
            if trakt_id == 1:
                raise RuntimeError("that one broke")
            return {"US": [3]}

        with self._owed(1, 2), patch.object(trakt_releases, "fetch_releases", _flaky):
            self.assertEqual(await calendar_enrich.drain_releases(self.SETTINGS), 1)

    async def test_an_instance_that_cannot_read_the_catalogue_asks_nothing(self):
        with self._owed(1), patch.object(
                trakt_releases, "fetch_releases", new=AsyncMock()) as fetch:
            await calendar_enrich.drain_releases(Settings())
        fetch.assert_not_awaited()


MOVIES = get_endpoint("movies")


class TheDefectClosingEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """The reported fault, over a real stored window: a release filter could not
    drop a film Trakt listed, because nothing gave the Trakt record anything to
    be judged on. Reported from a browser as a `zz` filter leaving 27 American
    films on the page.
    """

    async def asyncSetUp(self):
        new_db_path("trakt-release-end-to-end")
        await db.migrate()
        # No credentials, exactly as the other end-to-end release tests do it:
        # a stored window whose `asked` set covers everything in play is a hit,
        # and nothing here is about fetching.
        self.settings = Settings()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _stored(self, *records):
        await calendar_cache.store_window(
            MOVIES.key, date(2026, 7, 6), list(records), 600, 1000,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])

    async def _read(self, **kwargs):
        return await calendar_cache.assemble_range(
            MOVIES, self.settings, tz=ZoneInfo("UTC"),
            start_date=date(2026, 7, 7), end_date=date(2026, 7, 7),
            now=1000, **kwargs)

    async def test_a_trakt_film_is_now_judged_on_its_own_release_map(self):
        await calendar_enrich._upsert_release_success(1, {"BR": [1]}, 999)
        await calendar_enrich._upsert_release_success(2, {"US": [3]}, 999)
        await self._stored(_film(1), _film(2))
        grouped, meta = await self._read(movie_release_countries="us")
        self.assertEqual([i.ids["trakt"] for g in grouped for i in g["items"]], [2])
        self.assertEqual(meta["release_filtered"], 1)

    async def test_a_trakt_film_nothing_has_looked_up_yet_is_still_kept(self):
        """The promise that survives. A film waiting on its first lookup must not
        be deleted by a filter that has no information about it."""
        await self._stored(_film(1))
        grouped, _meta = await self._read(movie_release_countries="us")
        self.assertEqual(len([i for g in grouped for i in g["items"]]), 1)

    async def test_the_country_and_the_type_still_have_to_meet(self):
        """The rule the whole filter turns on, now reachable through a Trakt
        record too: a film that premiered in Brazil and opened in American
        cinemas is not a Brazilian theatrical release."""
        await calendar_enrich._upsert_release_success(1, {"BR": [1], "US": [3]}, 999)
        await self._stored(_film(1))
        grouped, _meta = await self._read(movie_release_countries="br",
                                          movie_release_types="3")
        self.assertEqual([i for g in grouped for i in g["items"]], [])
        grouped, _meta = await self._read(movie_release_countries="us",
                                          movie_release_types="3")
        self.assertEqual(len([i for g in grouped for i in g["items"]]), 1)


class WhatTheDrainOwesTests(unittest.IsolatedAsyncioTestCase):
    """What `_owed_films` derives from the stored calendar — the same "what does
    the cache currently name" question the Simkl side asks, on Trakt's films."""

    async def asyncSetUp(self):
        new_db_path("trakt-release-owed")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_it_names_the_trakt_films_a_stored_window_holds(self):
        show = Record(source=Source.TRAKT, media=Media.SHOW, id="9",
                      ids={"trakt": 9}, detail_url="", title="A Show",
                      air_ts=_AIR_TS, season=1, episode_number=1)
        simkl = Record(source=Source.SIMKL, media=Media.MOVIE, id="7",
                       ids={"simkl": 7}, detail_url="", title="Simkl Film",
                       air_ts=_AIR_TS, date_only=True)
        await calendar_cache.store_window(
            MOVIES.key, date(2026, 7, 6), [_film(1), simkl], 600, 1000,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])
        await calendar_cache.store_window(
            "shows/new", date(2026, 7, 6), [show], 600, 1000,
            sources=["trakt", "simkl"], asked=["trakt", "simkl"])
        owed = await calendar_enrich._owed_films()
        # Trakt's film only: a show has no release schedule to ask for, and the
        # other service's film is the other overlay's business.
        self.assertEqual(sorted(owed), [1])
