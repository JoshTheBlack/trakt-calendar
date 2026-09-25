"""app/distrakt/film_snapshots.py — a frozen month's films stored before films
had keys, keyed once.

MEASURED on the owner's database: 18 of 37 stored films in the old
`{trakt_id, title, year, watched_at}` shape; 13 of them shown twice because the
backfill had added the keyed copy beside them, and none of the 18 removable,
since the remove control names a film by its key.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.distrakt import film_snapshots, store
from tests.distrakt.test_store import DistraktTestCase

SETTINGS = Settings(trakt_client_id="cid")
OLD_ORIGIN = {"trakt_id": 592953, "title": "Origin", "year": 2023,
              "watched_at": "2026-05-30T23:50:00.000Z"}
OLD_QUEENS = {"trakt_id": 1077733, "title": "Queens of the Dead", "year": 2025,
              "watched_at": "2026-05-30T23:50:00.000Z"}
KEYED_QUEENS = {"key": "movie:tmdb:1318435", "ids": {"simkl": 2518759, "tmdb": 1318435},
                "title": "Queens of the Dead", "year": 2025,
                "watched_at": "2026-05-30T23:50:00Z"}


def _summary(tmdb=None):
    """Trakt's movie summary for a film, or None where Trakt has none."""
    body = None if tmdb is None else {"ids": {"trakt": 592953, "slug": "origin-2023", "tmdb": tmdb}}
    return patch("app.providers.trakt.detail.fetch_movie_summary", AsyncMock(return_value=body))


class KeyingOldFilmsTests(DistraktTestCase):
    MONTH = "2026-05"

    async def _stored(self, films):
        await store.save_month(self.user_id, {**store.new_month_doc(self.MONTH), "closed": True})
        await store.set_month_movies(self.user_id, self.MONTH, films)

    async def _films(self):
        return (await store.load_month(self.user_id, self.MONTH))["movies"]

    async def test_an_old_entry_beside_its_keyed_copy_is_the_duplicate_and_goes(self):
        await self._stored([OLD_QUEENS, KEYED_QUEENS])
        with _summary() as lookup:
            await film_snapshots.repair(self.user_id, SETTINGS)
        self.assertEqual(await self._films(), [KEYED_QUEENS])
        lookup.assert_not_awaited()

    async def test_an_old_entry_alone_is_keyed_so_it_can_be_removed(self):
        await self._stored([OLD_ORIGIN])
        with _summary(tmdb=1216191):
            await film_snapshots.repair(self.user_id, SETTINGS)
        film, = await self._films()
        self.assertEqual(film["key"], "movie:tmdb:1216191")
        self.assertEqual((film["title"], film["year"], film["watched_at"]),
                         ("Origin", 2023, OLD_ORIGIN["watched_at"]))
        self.assertTrue(await store.remove_month_movie(self.user_id, self.MONTH, film["key"]))

    async def test_an_old_entry_whose_key_is_already_there_goes(self):
        keyed = {**KEYED_QUEENS, "key": "movie:tmdb:1216191", "title": "Origin (2023)"}
        await self._stored([OLD_ORIGIN, keyed])
        with _summary(tmdb=1216191):
            await film_snapshots.repair(self.user_id, SETTINGS)
        self.assertEqual(await self._films(), [keyed])

    async def test_a_film_trakt_cannot_answer_for_is_left_and_not_asked_again_today(self):
        await self._stored([OLD_ORIGIN])
        with _summary(tmdb=None) as lookup:
            await film_snapshots.repair(self.user_id, SETTINGS)
            await film_snapshots.repair(self.user_id, SETTINGS)
        self.assertEqual(await self._films(), [OLD_ORIGIN])
        lookup.assert_awaited_once()

    async def test_a_repaired_month_costs_nothing_to_pass_over(self):
        await self._stored([KEYED_QUEENS])
        self.assertEqual(await store.months_with_unkeyed_films(self.user_id), [])
        self.assertEqual(await film_snapshots.repair(self.user_id, SETTINGS), 0)
