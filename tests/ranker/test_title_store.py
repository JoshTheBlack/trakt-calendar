"""A board files every title where the title store says it is.

EVERY WAY A TITLE REACHES A BOARD — a search add, an import, a ratings seed, a
removal put back, a restored backup — is filed through the store BEFORE the board
asks whether it already holds it (ranker.core's `_filed`), so a title named by a
lower id lands on the row its higher id already holds, and a backup naming a key
the store has since retired restores under the key the title answers to now. A
seed's preview decides the same keys and writes nothing.
"""
from __future__ import annotations

import asyncio

from app import db, metadata
from app.providers.base import Media
from app.ranker import core as ranker
from tests.ranker.test_boards import RankerTestCase, show_ref


def ref(match_source: str, match_id: str, **extra) -> dict:
    return {"media": "show", "match_source": match_source, "match_id": match_id,
            "title": "A Show", **extra}


class EveryArrivalIsFiledByTheStoreTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        # The store knows this show by both ids, as another service's listing taught.
        asyncio.run(metadata.ensure(Media.SHOW, {"tmdb": 900, "tvdb": 4321}))

    def keys(self) -> list[str]:
        return [f"{r['match_source']}:{r['match_id']}" for r in
                self.rows("SELECT match_source, match_id FROM tier_items ORDER BY id")]

    def test_a_title_named_by_a_lower_id_lands_on_its_higher_row(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("900")]))
        arrived = asyncio.run(ranker.add_titles(self.user_id, "b1", [
            ref("tvdb", "4321", ids={"tvdb": 4321})]))
        self.assertEqual(arrived.added, 0)
        self.assertEqual(self.keys(), ["tmdb:900"])

    def test_an_added_title_is_registered_with_the_store(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [
            ref("tmdb", "77", tmdb=77, ids={"tmdb": 77, "imdb": "tt7"})]))
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM titles WHERE title_key = 'show:tmdb:77'"), 1)

    def test_a_seeds_preview_files_the_same_way_and_writes_nothing(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("900")]))
        before = self.value("SELECT COUNT(*) FROM titles")
        summary = asyncio.run(ranker.seed_ratings(self.user_id, "b1", [
            {**ref("tvdb", "4321", ids={"tvdb": 4321}), "user_rating": 9},
            {**ref("tmdb", "55", tmdb=55, ids={"tmdb": 55}), "user_rating": 8}]))
        self.assertEqual(summary["titles_added"], 1)
        self.assertEqual(self.value("SELECT COUNT(*) FROM titles"), before)

    def test_a_backup_naming_a_retired_key_restores_under_the_current_one(self):
        asyncio.run(metadata.ensure(Media.SHOW, {"imdb": "tt5"}))
        asyncio.run(metadata.ensure(Media.SHOW, {"imdb": "tt5", "tmdb": 55},
                                    filed_under="show:imdb:tt5"))
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        doc["boards"][0]["items"] = [{
            "media": "show", "match_source": "imdb", "match_id": "tt5", "season": None,
            "season_name": None, "tmdb": None, "ids_json": '{"imdb": "tt5"}',
            "title": "Restored", "year": None, "network": "", "season_count": None,
            "episode_count": None, "runtime": None, "user_rating": None,
            "added_from": "manual", "rank_in_category": 0, "created_at": 1,
            "category_uid": None}]
        asyncio.run(ranker.restore_user_data(self.user_id, doc))
        row = self.rows("SELECT match_source, match_id, tmdb FROM tier_items")[0]
        self.assertEqual(tuple(row), ("tmdb", "55", 55))
        # The words the file drew it with are seeded into the store, under the
        # key the title answers to now.
        item, = asyncio.run(ranker.fetch_board(self.user_id, "b1"))["pool"]
        self.assertEqual(item["title"], "Restored")
