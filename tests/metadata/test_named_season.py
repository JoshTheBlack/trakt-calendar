"""Which season of its title a service's entry IS — app/metadata/descriptions.py's
named season, filed from the service's season list and read by a season picker
(metadata.picked).

Simkl files a later anime season as a title of its own: "Shingeki no Kyojin
Season 3" (simkl 694485) numbers its episodes from 1 and carries the series'
shared ids, so the store files it as a second Simkl entry of the series' title,
and only Simkl's own record of the entry says it is season 3.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from app import db, metadata
from app.metadata import descriptions, rows
from app.providers.base import Media
from app.providers.title_search import Pick
from tests.metadata.test_panel import _answer
from tests.support import DatabaseTestCase, put_title

KEY = "show:tmdb:1429"
SERIES, SEASON_THREE = "39687", "694485"
UTC = ZoneInfo("UTC")


def _pick(season, *, answered=True, asked=("simkl", SEASON_THREE)) -> Pick:
    return Pick(title="Shingeki no Kyojin Season 3", network="", year=2018,
                ids={"tmdb": 1429}, seasons=[], named_season=season, media=Media.SHOW,
                asked=asked, answered=answered)


class TheNamedSeasonTests(DatabaseTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()

        def _seed(conn):
            # The series' first entry is the title's Simkl group; the season-3
            # entry is a second entry of the same title.
            put_title(conn, "simkl", "show", SERIES, KEY, ids={"simkl": 39687, "tmdb": 1429})
            rows.file_ids(conn, [["simkl", "show", SEASON_THREE, SEASON_THREE, KEY]])
        await db.transaction(_seed)

    async def _named(self, own=SEASON_THREE):
        return await db.run(lambda conn: descriptions.named_season(conn, "simkl", "show", own))

    async def test_an_entry_names_one_season_and_moving_it_lets_the_other_go(self):
        await db.transaction(lambda conn: descriptions.write_named(
            conn, "simkl", [(KEY, SEASON_THREE, 3)]))
        self.assertEqual(await self._named(), 3)
        await db.transaction(lambda conn: descriptions.write_named(
            conn, "simkl", [(KEY, SEASON_THREE, 4)]))
        self.assertEqual(await self._named(), 4)
        held = await db.run(lambda conn: conn.execute(
            "SELECT season, simkl_id, simkl_named FROM seasons WHERE title_key = ? "
            "ORDER BY season", (KEY,)).fetchall())
        self.assertEqual([tuple(row) for row in held], [(3, None, 0), (4, SEASON_THREE, 1)])

    async def test_an_entry_said_to_name_none_names_none(self):
        await db.transaction(lambda conn: descriptions.write_named(
            conn, "simkl", [(KEY, SEASON_THREE, 3)]))
        await db.transaction(lambda conn: descriptions.write_named(
            conn, "simkl", [(KEY, SEASON_THREE, None)]))
        self.assertIsNone(await self._named())

    async def test_a_refresh_files_it_from_the_season_list_it_reads(self):
        """The refresh pass reads a show's season list beside its description
        (refresh._look_up), and the list's named season is filed with it."""
        answer = _answer("simkl", SEASON_THREE, None, title="Shingeki no Kyojin Season 3")
        answer["ids"] = {"simkl": int(SEASON_THREE), "tmdb": 1429}
        await descriptions.store_many([descriptions.Answer(
            "simkl", "show", SEASON_THREE, answer, None, [], 3)], tz=UTC)
        self.assertEqual(await self._named(), 3)

    async def test_a_picker_files_what_its_lookup_said_and_reads_it_back_later(self):
        self.assertEqual((await metadata.picked(_pick(3))).named_season, 3)
        self.assertEqual(await self._named(), 3)
        # An add re-reading the lookup cached-only after the response cache let
        # it go hears nothing — and the store still says which season it is.
        self.assertEqual((await metadata.picked(_pick(None, answered=False))).named_season, 3)

    async def test_a_lookup_saying_the_same_writes_nothing(self):
        await metadata.picked(_pick(3))
        writes = []
        real = db.transaction

        async def counting(fn, **kwargs):
            writes.append(fn)
            return await real(fn, **kwargs)

        db.transaction = counting
        try:
            await metadata.picked(_pick(3))
        finally:
            db.transaction = real
        self.assertEqual(writes, [])

    async def test_an_entry_the_store_has_not_met_is_left_to_its_registration(self):
        await metadata.picked(_pick(2, asked=("simkl", "999")))
        self.assertIsNone(await self._named("999"))

    async def test_a_service_that_never_names_a_season_is_not_asked(self):
        pick = _pick(None, asked=("trakt", "77"))
        self.assertIs(await metadata.picked(pick), pick)
