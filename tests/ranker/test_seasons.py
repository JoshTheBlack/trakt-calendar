"""A show and its seasons, side by side on one board.

A board may hold a show whole AND any of its seasons, each its own row with its
own place (tier_items.season; the unique index over IFNULL(season, -1)). Every
operation that names a row by its key has to keep those rows apart, and one that
does not fails silently: a season restored as its whole show, a deleted tier that
brings back the wrong row, a save refused for naming a title twice. So each test
here puts a show and a season of it on the same board and checks, in the
database, that the operation touched exactly the row it named.

And the other direction: a key with no season suffix — every key stored before a
board could hold a season — still names the whole-show row.
"""
from __future__ import annotations

import asyncio
import json

from app import db
from app.ranker import core as ranker, exports as ranker_export
from tests.ranker.test_removals import BOARD, _BoardTestCase, key, show_ref

SHOW = "7"


def season_key(match_id: str, season: int) -> str:
    return f"{key(match_id)}:s{season}"


class _SeasonsTestCase(_BoardTestCase):

    def add(self, *refs: dict) -> int:
        return asyncio.run(ranker.add_titles(self.user_id, BOARD, list(refs)))

    def build_with_seasons(self):
        """Tier s: the whole show, then its season 2, then another title. Pool:
        season 1 of the same show."""
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD, name="Top"))
        self.add(show_ref(SHOW), show_ref(SHOW, season=2), show_ref("8"),
                 show_ref(SHOW, season=1))
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                            "items": [key(SHOW), season_key(SHOW, 2), key("8")]}],
            "pool": [season_key(SHOW, 1)],
        }))

    def keys_in(self, uid: str) -> list[str]:
        return [item["key"] for category in self.board()["categories"]
                if category["uid"] == uid for item in category["items"]]

    def pool_keys(self) -> list[str]:
        return [item["key"] for item in self.board()["pool"]]


class TheyAreSeparateRowsTests(_SeasonsTestCase):

    def test_a_show_and_its_seasons_are_three_rows(self):
        self.build_with_seasons()
        self.assertEqual(self.keys_in("s"), [key(SHOW), season_key(SHOW, 2), key("8")])
        self.assertEqual(self.pool_keys(), [season_key(SHOW, 1)])
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items WHERE match_id = ?",
                                    (SHOW,)), 3)

    def test_adding_one_again_adds_nothing(self):
        self.build_with_seasons()
        self.assertEqual(self.add(show_ref(SHOW), show_ref(SHOW, season=2)),
                         ranker.Arrivals(added=0, converted=0))
        self.assertEqual(self.add(show_ref("8", season=3)),
                         ranker.Arrivals(added=0, converted=1))
        self.assertEqual(self.add(show_ref("9", season=3)),
                         ranker.Arrivals(added=1, converted=0))

    def test_an_unsuffixed_key_names_the_whole_show(self):
        self.build_with_seasons()
        item = asyncio.run(ranker.fetch_item(self.user_id, BOARD, key(SHOW)))
        self.assertIsNone(item["season"])
        self.assertEqual(item["key"], key(SHOW))
        season = asyncio.run(ranker.fetch_item(self.user_id, BOARD, season_key(SHOW, 2)))
        self.assertEqual((season["season"], season["key"]), (2, season_key(SHOW, 2)))

    def test_a_season_the_board_does_not_hold_is_not_found(self):
        self.build_with_seasons()
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.fetch_item(self.user_id, BOARD, season_key(SHOW, 5)))

    def test_a_film_cannot_be_given_a_season(self):
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD))
        with self.assertRaises(ranker.ValidationError):
            self.add({"media": "movie", "match_source": "tmdb", "match_id": "550",
                      "season": 1})

    def test_the_cap_counts_a_season_as_a_new_row(self):
        self.build_with_seasons()
        with mock_cap(4):
            with self.assertRaises(ranker.ValidationError):
                self.add(show_ref("9", season=3))
            self.assertEqual(self.add(show_ref(SHOW, season=2)),
                             ranker.Arrivals(added=0, converted=0))


class SavingAndMovingTests(_SeasonsTestCase):

    def test_a_save_places_each_row_where_it_is_named(self):
        self.build_with_seasons()
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                            "items": [season_key(SHOW, 1), key("8")]}],
            "pool": [key(SHOW), season_key(SHOW, 2)],
        }))
        self.assertEqual(self.keys_in("s"), [season_key(SHOW, 1), key("8")])
        self.assertEqual(sorted(self.pool_keys()), sorted([key(SHOW), season_key(SHOW, 2)]))

    def test_a_save_naming_one_season_twice_is_refused(self):
        self.build_with_seasons()
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.save_layout(self.user_id, BOARD, {
                "version": self.version(),
                "categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                                "items": [season_key(SHOW, 2), key("8")]}],
                "pool": [season_key(SHOW, 2)],
            }))

    def test_a_move_takes_exactly_the_rows_named(self):
        self.build_with_seasons()
        asyncio.run(ranker.add_categories(self.user_id, BOARD, [{"uid": "a", "label": "A"}]))
        asyncio.run(ranker.move_items(self.user_id, BOARD,
                                      [season_key(SHOW, 1), season_key(SHOW, 2)], "a"))
        self.assertEqual(self.keys_in("a"), [season_key(SHOW, 1), season_key(SHOW, 2)])
        self.assertEqual(self.keys_in("s"), [key(SHOW), key("8")])

    def test_the_pool_pages_after_a_season(self):
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD))
        self.add(show_ref(SHOW), show_ref(SHOW, season=1), show_ref(SHOW, season=2))
        after, _ = asyncio.run(ranker.fetch_pool_after(
            self.user_id, BOARD, season_key(SHOW, 1), 10))
        self.assertEqual([item["key"] for item in after], [season_key(SHOW, 2)])


class RemovingAndRestoringTests(_SeasonsTestCase):

    def test_removing_a_season_leaves_its_show(self):
        self.build_with_seasons()
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 2)]))
        self.assertEqual(self.keys_in("s"), [key(SHOW), key("8")])
        self.assertEqual(self.history(), [season_key(SHOW, 2)])
        row, = asyncio.run(db.fetch_all("SELECT season FROM tier_item_removals"))
        self.assertEqual(row["season"], 2)

    def test_a_removed_season_comes_back_as_that_season_where_it_sat(self):
        self.build_with_seasons()
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 2)]))
        self.assertTrue(asyncio.run(ranker.restore_removal(
            self.user_id, BOARD, season_key(SHOW, 2))))
        self.assertEqual(self.keys_in("s"), [key(SHOW), season_key(SHOW, 2), key("8")])
        self.assertEqual(self.history(), [])

    def test_a_show_and_its_season_removed_are_two_entries(self):
        self.build_with_seasons()
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [key(SHOW)]))
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 2)]))
        self.assertEqual(self.history(), [season_key(SHOW, 2), key(SHOW)])
        # Putting the show back does not put the season back, or forget it.
        asyncio.run(ranker.restore_removal(self.user_id, BOARD, key(SHOW)))
        self.assertEqual(self.keys_in("s"), [key(SHOW), key("8")])
        self.assertEqual(self.history(), [season_key(SHOW, 2)])

    def test_a_season_already_back_is_a_no_op_that_clears_only_its_entry(self):
        self.build_with_seasons()
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [key(SHOW)]))
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 2)]))
        self.add(show_ref(SHOW, season=2))
        self.assertFalse(asyncio.run(ranker.restore_removal(
            self.user_id, BOARD, season_key(SHOW, 2))))
        self.assertEqual(self.history(), [key(SHOW)])

    def test_the_whole_show_is_not_restored_from_a_season_entry(self):
        self.build_with_seasons()
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 2)]))
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.restore_removal(self.user_id, BOARD, season_key(SHOW, 3)))


class DeletingAndRestoringATierTests(_SeasonsTestCase):

    def test_a_deleted_tier_remembers_each_row_by_its_own_key(self):
        self.build_with_seasons()
        asyncio.run(ranker.delete_category(self.user_id, BOARD, "s"))
        stored = self.value("SELECT items_json FROM tier_category_removals")
        self.assertEqual(json.loads(stored), [key(SHOW), season_key(SHOW, 2), key("8")])

    def test_putting_it_back_returns_the_show_and_its_season_in_order(self):
        self.build_with_seasons()
        asyncio.run(ranker.delete_category(self.user_id, BOARD, "s"))
        result = asyncio.run(ranker.restore_tier_removal(self.user_id, BOARD, "s"))
        self.assertEqual(result.returned, 3)
        self.assertEqual(self.keys_in("s"), [key(SHOW), season_key(SHOW, 2), key("8")])
        self.assertEqual(self.pool_keys(), [season_key(SHOW, 1)])

    def test_a_season_taken_off_after_the_delete_is_aimed_back_at_the_tier(self):
        self.build_with_seasons()
        asyncio.run(ranker.delete_category(self.user_id, BOARD, "s"))
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 2)]))
        result = asyncio.run(ranker.restore_tier_removal(self.user_id, BOARD, "s"))
        self.assertEqual((result.returned, result.removed), (2, 1))
        # The season's own entry now names the tier, and its whole show's row —
        # which never left — is untouched by that.
        asyncio.run(ranker.restore_removal(self.user_id, BOARD, season_key(SHOW, 2)))
        self.assertEqual(self.keys_in("s"), [key(SHOW), season_key(SHOW, 2), key("8")])

    def test_a_tier_deleted_before_seasons_existed_brings_back_the_whole_show(self):
        """A history row written by an older build lists unsuffixed keys, which
        name the whole-show rows — never a season of the same show."""
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD))
        self.add(show_ref(SHOW), show_ref(SHOW, season=2))
        board_id = self.value("SELECT id FROM tier_boards WHERE uid = ?", (BOARD,))
        asyncio.run(db.execute(
            "INSERT INTO tier_category_removals (board_id, category_uid, sort_order, "
            "removed_at, category_json, items_json) VALUES (?, 's', 0, ?, ?, ?)",
            (board_id, db.now(), json.dumps({"label": "S", "rank_priority": 10}),
             json.dumps([key(SHOW)]))))
        asyncio.run(ranker.restore_tier_removal(self.user_id, BOARD, "s"))
        self.assertEqual(self.keys_in("s"), [key(SHOW)])
        self.assertEqual(self.pool_keys(), [season_key(SHOW, 2)])


class BackupTests(_SeasonsTestCase):

    def test_a_season_row_round_trips(self):
        self.build_with_seasons()
        before = self.board()
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        seasons = {(item["match_id"], item["season"]) for item in doc["boards"][0]["items"]}
        self.assertEqual(seasons, {(SHOW, None), (SHOW, 1), (SHOW, 2), ("8", None)})
        asyncio.run(ranker.restore_user_data(self.user_id, json.loads(json.dumps(doc))))
        after = self.board()
        self.assertEqual(self.keys_in("s"), [key(SHOW), season_key(SHOW, 2), key("8")])
        self.assertEqual(self.pool_keys(), [season_key(SHOW, 1)])
        self.assertEqual(len(after["pool"]), len(before["pool"]))

    def test_a_document_with_no_seasons_restores_every_title_whole(self):
        """What every backup written before a board could hold a season looks
        like: no `season` on any title."""
        self.build_with_seasons()
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        items = [item for item in doc["boards"][0]["items"] if item["season"] is None]
        for item in items:
            del item["season"]
        doc["boards"][0]["items"] = items
        asyncio.run(ranker.restore_user_data(self.user_id, doc))
        self.assertEqual(self.keys_in("s"), [key(SHOW), key("8")])

    def test_one_season_twice_on_a_board_is_refused(self):
        self.build_with_seasons()
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        items = doc["boards"][0]["items"]
        items.append(dict(next(item for item in items if item["season"] == 2)))
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.restore_user_data(self.user_id, doc))


class ExportTests(_SeasonsTestCase):

    def test_a_season_is_its_own_entry_under_its_own_key(self):
        self.build_with_seasons()
        ranked = ranker_export.consolidate(self.board())
        self.assertEqual([entry.key for entry in ranked],
                         [key(SHOW), season_key(SHOW, 2), key("8")])


class RoutesTests(_SeasonsTestCase):
    """The keys arrive through the same routes a page uses."""

    def test_a_season_row_is_drawn_under_its_own_key(self):
        self.build_with_seasons()
        body = self.client.get(f"/rankings/fragments/row?board={BOARD}"
                               f"&key={season_key(SHOW, 2)}").text
        self.assertIn(f'data-key="{season_key(SHOW, 2)}"', body)
        self.assertIn(f'name="pick:{season_key(SHOW, 2)}"', body)
        self.assertIn(f'id="move-{season_key(SHOW, 2)}"', body)

    def test_removing_a_season_by_its_key_offers_it_back(self):
        self.build_with_seasons()
        body = self.client.post(f"/rankings/boards/{BOARD}/items/remove",
                                json={"key": season_key(SHOW, 2)}).text
        self.assertIn("Removed from the board.", body)
        self.assertEqual(self.keys_in("s"), [key(SHOW), key("8")])
        self.client.post(f"/rankings/boards/{BOARD}/removals/restore",
                         json={"key": season_key(SHOW, 2)})
        self.assertEqual(self.keys_in("s"), [key(SHOW), season_key(SHOW, 2), key("8")])

    def test_a_bulk_move_names_a_season_and_its_show_by_their_fields(self):
        self.build_with_seasons()
        asyncio.run(ranker.add_categories(self.user_id, BOARD, [{"uid": "a", "label": "A"}]))
        self.client.post(f"/rankings/boards/{BOARD}/items/move", json={
            f"pick:{key(SHOW)}": "1", f"pick:{season_key(SHOW, 1)}": "1", "tier": "a"})
        self.assertEqual(self.keys_in("a"), [key(SHOW), season_key(SHOW, 1)])
        self.assertEqual(self.keys_in("s"), [season_key(SHOW, 2), key("8")])


class mock_cap:
    """MAX_ITEMS_PER_BOARD lowered for one block."""

    def __init__(self, cap: int):
        self.cap = cap

    def __enter__(self):
        self.was, ranker.MAX_ITEMS_PER_BOARD = ranker.MAX_ITEMS_PER_BOARD, self.cap

    def __exit__(self, *exc):
        ranker.MAX_ITEMS_PER_BOARD = self.was
