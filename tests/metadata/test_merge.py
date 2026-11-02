"""app/metadata/merge.py — two of the store's titles becoming one, and one
service's entry moving from one title to another.

A RE-KEY MERGES ROWS, each service's group decided on its own: a group only one
title has is the merged title's; where both have one the entry with the most
listings is the title's group and the other a `service_ids` row, its words
logged and not kept. NO LISTING IS LOST: two services' listings of one episode
are one row, one service's two of one episode a listing and its repeat, and a
listing naming no full episode is re-decided against the merged title's
numbered ones.

AN ENTRY MOVES WITH ITS OWN ID, driven here the way it happens — a service's
listing stating another shared id for the same own id — and the title it lands
on keeps its own group if it has one.

No network.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from app import db
from app.calendar import entries
from app.metadata import merge, tables
from app.providers.base import Media, Record, Source
from tests.support import (DatabaseTestCase, listings, put_listing, put_season, put_title,
                           season_group, title_group)

NOW = 1_800_000_000
OLD, NEW = "show:tvdb:4321", "show:tmdb:900"
SPAN = (date(2026, 1, 1), date(2027, 1, 1))


def at(day: str, hour: int = 20) -> float:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc).timestamp()


async def _ids(source: str) -> dict[str, tuple[str, int]]:
    return {row["service_id"]: (row["title_key"], row["is_primary"]) for row in await db.fetch_all(
        f"SELECT * FROM {tables.SERVICE_IDS} WHERE source = ?", (source,))}


async def _row(key: str, unit: str) -> dict | None:
    found = await db.fetch_one(f"SELECT * FROM {tables.EPISODES} WHERE title_key = ? AND unit = ?",
                               (key, unit))
    return dict(found) if found is not None else None


async def _units(key: str) -> list[str]:
    return [row["unit"] for row in await db.fetch_all(
        f"SELECT unit FROM {tables.EPISODES} WHERE title_key = ? ORDER BY unit", (key,))]


class ARekeyMergesEachServicesGroupTests(DatabaseTestCase):

    async def merge(self):
        return await db.transaction(lambda conn: merge.titles(conn, OLD, NEW, now=NOW))

    async def test_a_group_only_the_old_key_has_becomes_the_merged_titles(self):
        def _hold(conn):
            put_title(conn, "simkl", "show", "55", OLD, title="Old Words")
            put_season(conn, "simkl", OLD, 1, episode_count=8)
            put_listing(conn, "simkl", "show", "55", "shows", OLD, at("2026-10-01"),
                        season=1, number=1)
            put_title(conn, "trakt", "show", "t", NEW, ids={"trakt": 7}, title="Trakt's")

        await db.transaction(_hold)
        await self.merge()
        group = await title_group(NEW, "simkl")
        self.assertEqual((group["source_id"], group["title"]), ("55", "Old Words"))
        self.assertEqual((await title_group(NEW, "trakt"))["title"], "Trakt's")
        self.assertEqual((await season_group(NEW, 1, "simkl"))["episode_count"], 8)
        self.assertEqual([(listed["title_key"], listed["unit"]) for listed in await listings()],
                         [(NEW, "S1E1")])
        self.assertEqual(await _ids("simkl"), {"55": (NEW, 1)})
        for table in (tables.SEASONS, tables.EPISODES, tables.SERVICE_IDS):
            with self.subTest(table=table):
                self.assertEqual(await db.fetch_value(
                    f"SELECT COUNT(*) FROM {table} WHERE title_key = ?", (OLD,)), 0)

    async def _two_simkl_entries(self):
        """Simkl's entry 55 under the old key, listed twice; its entry 66 under the
        new key, listed once and describing a season of its own."""
        def _hold(conn):
            put_title(conn, "simkl", "show", "55", OLD, title="Fifty-Five")
            put_listing(conn, "simkl", "show", "55", "shows", OLD, at("2026-10-01"),
                        season=1, number=1)
            put_listing(conn, "simkl", "show", "55", "shows", OLD, at("2026-10-08"),
                        season=1, number=2)
            put_title(conn, "simkl", "show", "66", NEW, title="Sixty-Six")
            put_season(conn, "simkl", NEW, 1, episode_count=24)
            put_listing(conn, "simkl", "show", "66", "shows", NEW, at("2026-10-15"),
                        season=1, number=3)

        await db.transaction(_hold)

    async def test_where_both_have_a_group_the_most_listed_entry_is_the_titles(self):
        await self._two_simkl_entries()
        with self.assertLogs("app.metadata.merge", "WARNING") as logged:
            await self.merge()
        group = await title_group(NEW, "simkl")
        self.assertEqual((group["source_id"], group["title"]), ("55", "Fifty-Five"))
        self.assertEqual(await _ids("simkl"), {"55": (NEW, 1), "66": (NEW, 0)})
        self.assertTrue(any("Sixty-Six" in line for line in logged.output))

    async def test_the_other_entrys_listings_stay_and_say_whose_they_are(self):
        await self._two_simkl_entries()
        with self.assertLogs("app.metadata.merge", "WARNING"):
            await self.merge()
        self.assertEqual(await _units(NEW), ["S1E1", "S1E2", "S1E3"])
        self.assertIsNone((await _row(NEW, "S1E1"))["simkl_entry"])
        self.assertEqual((await _row(NEW, "S1E3"))["simkl_entry"], "66")

    async def test_the_other_entrys_season_words_are_not_the_titles(self):
        """Its season 1 is its own numbering — not the merged title's."""
        await self._two_simkl_entries()
        with self.assertLogs("app.metadata.merge", "WARNING"):
            await self.merge()
        self.assertIsNone(await season_group(NEW, 1, "simkl"))

    async def test_two_services_listings_of_one_episode_are_one_row(self):
        def _hold(conn):
            put_title(conn, "trakt", "show", "t", OLD, ids={"trakt": 7})
            put_listing(conn, "trakt", "show", "t", "shows", OLD, at("2026-10-01"),
                        season=1, number=1)
            put_title(conn, "tmdb", "show", "900", NEW)
            put_listing(conn, "tmdb", "show", "900", "shows", NEW, at("2026-10-02"),
                        season=1, number=1)

        await db.transaction(_hold)
        await self.merge()
        self.assertEqual(await _units(NEW), ["S1E1"])
        row = await _row(NEW, "S1E1")
        self.assertEqual((row["trakt_air_ts"], row["tmdb_air_ts"]),
                         (at("2026-10-01"), at("2026-10-02")))

    async def test_one_services_two_listings_of_an_episode_are_a_listing_and_a_repeat(self):
        def _hold(conn):
            put_title(conn, "simkl", "show", "55", OLD)
            put_listing(conn, "simkl", "show", "55", "shows", OLD, at("2026-10-01"),
                        season=1, number=1)
            put_title(conn, "simkl", "show", "66", NEW)
            put_listing(conn, "simkl", "show", "66", "shows", NEW, at("2026-10-09"),
                        season=1, number=1)

        await db.transaction(_hold)
        with self.assertLogs("app.metadata.merge", "WARNING"):
            await self.merge()
        row = await _row(NEW, "S1E1")
        # Equal listings and words: the lower id is the group, and its instant
        # the listing.
        self.assertEqual((row["simkl_air_ts"], row["simkl_entry"]), (at("2026-10-01"), None))
        self.assertEqual([(r["at"], r["entry"]) for r in json.loads(row["simkl_repeats_json"])],
                         [(at("2026-10-09"), "66")])

    async def test_a_listing_naming_no_full_episode_is_redecided_against_the_merged_title(self):
        """Alone under its key it was a day's row; beside the other title's
        numbered episode that day it is that episode."""
        def _hold(conn):
            put_title(conn, "simkl", "show", "55", OLD)
            put_listing(conn, "simkl", "show", "55", "shows", OLD, at("2026-10-01", 21),
                        number=1)
            put_title(conn, "trakt", "show", "t", NEW, ids={"trakt": 7})
            put_listing(conn, "trakt", "show", "t", "shows", NEW, at("2026-10-01"),
                        season=2, number=1)

        await db.transaction(_hold)
        self.assertEqual(await _units(OLD), ["D2026-10-01E1"])
        await self.merge()
        self.assertEqual(await _units(NEW), ["S2E1"])
        row = await _row(NEW, "S2E1")
        self.assertEqual((row["simkl_air_ts"], row["simkl_unstated"]), (at("2026-10-01", 21), 1))


def _simkl(own: int, tmdb: int, title: str, day: str, *, number: int = 1) -> Record:
    return Record(source=Source.SIMKL, media=Media.SHOW, id=str(own),
                  ids={"simkl": own, "tmdb": tmdb}, detail_url="s", title=title,
                  air_ts=at(day), season=1, episode_number=number, episode_label=f"S01E0{number}",
                  enriched=False)


async def _fill(endpoint: str, records: list[Record]) -> None:
    await entries.store_span(endpoint, *SPAN, records, sources=["simkl"], asked=["simkl"],
                             now=NOW, stale_after=NOW + 86_400)


class AnEntryMovesWithItsOwnIdTests(DatabaseTestCase):
    """Simkl moved The Great British Bake Off between TMDB's two titles: its own
    id stayed, the tmdb id it stated changed. The entry, its words and every
    listing it made go to the title it now names."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await _fill("shows", [_simkl(9, 1, "Bake Off", "2026-10-01")])
        await _fill("shows/premieres", [_simkl(9, 1, "Bake Off", "2026-10-01")])
        await entries.store_episodes("simkl", 9, "show", 1,
                                     [{"number": 1, "title": "Cake Week"}], now=NOW)

    async def test_the_group_its_ids_and_its_listings_go_to_the_title_it_names(self):
        await _fill("shows", [_simkl(9, 2, "Bake Off", "2026-10-01")])
        self.assertIsNone(await title_group("show:tmdb:1", "simkl"))
        self.assertEqual((await title_group("show:tmdb:2", "simkl"))["source_id"], "9")
        self.assertEqual(await _ids("simkl"), {"9": ("show:tmdb:2", 1)})
        # Including the calendar this fill did not refill.
        self.assertEqual({(listed["endpoint"], listed["title_key"]) for listed in await listings()},
                         {("shows", "show:tmdb:2"), ("shows/premieres", "show:tmdb:2")})
        self.assertEqual((await _row("show:tmdb:2", "S1E1"))["simkl_title"], "Cake Week")
        self.assertIsNone(await _row("show:tmdb:1", "S1E1"))

    async def test_a_title_with_a_group_of_its_own_keeps_it(self):
        await _fill("shows/new", [_simkl(8, 2, "Bake Off: The Professionals", "2026-11-01")])
        with self.assertLogs("app.metadata.merge", "WARNING"):
            await _fill("shows", [_simkl(9, 2, "Bake Off", "2026-10-01")])
        group = await title_group("show:tmdb:2", "simkl")
        self.assertEqual((group["source_id"], group["title"]),
                         ("8", "Bake Off: The Professionals"))
        self.assertEqual(await _ids("simkl"), {"8": ("show:tmdb:2", 1), "9": ("show:tmdb:2", 0)})
        self.assertEqual((await _row("show:tmdb:2", "S1E1"))["simkl_entry"], "9")
