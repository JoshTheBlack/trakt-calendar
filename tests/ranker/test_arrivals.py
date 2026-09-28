"""The arrival rule: what a board does with a title or season it is handed.

Every way a title reaches a board — a search, the import of what somebody
finished, a ratings seed — goes through core._plan_arrivals, so these tests pin
the rule once and then walk each arrival path through it:

  - a title or season already held adds nothing;
  - a season of a show held as its whole-show row TAKES THAT ROW OVER in place —
    the same id, tier and rank — and brings what it knows about itself;
  - the cap counts only rows an arrival creates, and an add past it is refused
    whole, saying how many would still fit.

A restore is not an arrival and is pinned here as the exception.
"""
from __future__ import annotations

import asyncio

from app import db
from app.ranker import core as ranker
from tests.ranker.test_removals import BOARD, _BoardTestCase, key, show_ref
from tests.ranker.test_seasons import mock_cap, season_key

SHOW = "7"


def rows(*refs: dict) -> list[dict]:
    return [ranker._normalize_ref(ref) for ref in refs]


class PlanTests(_BoardTestCase):
    """The rule itself, pure: a board's key -> id and a batch in, the writes out."""

    def plan(self, held: dict[str, int], *refs: dict):
        return [(step.row["season"], step.item_id)
                for step in ranker._plan_arrivals(rows(*refs), held)]

    def test_a_held_title_or_season_adds_nothing(self):
        held = {key(SHOW): 1, season_key(SHOW, 2): 2}
        self.assertEqual(self.plan(held, show_ref(SHOW), show_ref(SHOW, season=2)), [])

    def test_a_season_takes_over_its_whole_show_row(self):
        self.assertEqual(self.plan({key(SHOW): 11}, show_ref(SHOW, season=3)), [(3, 11)])

    def test_of_several_seasons_the_latest_takes_the_row_over(self):
        self.assertEqual(
            self.plan({key(SHOW): 11}, show_ref(SHOW, season=1), show_ref(SHOW, season=4),
                      show_ref(SHOW, season=2)),
            [(1, None), (4, 11), (2, None)])

    def test_a_season_the_board_already_holds_does_not_take_the_row(self):
        self.assertEqual(
            self.plan({key(SHOW): 11, season_key(SHOW, 4): 12},
                      show_ref(SHOW, season=4), show_ref(SHOW, season=2)),
            [(2, 11)])

    def test_no_season_takes_the_row_when_the_batch_names_the_whole_show(self):
        self.assertEqual(
            self.plan({key(SHOW): 11}, show_ref(SHOW), show_ref(SHOW, season=2)),
            [(2, None)])

    def test_a_repeat_in_one_batch_is_written_once(self):
        self.assertEqual(
            self.plan({}, show_ref(SHOW, season=2), show_ref(SHOW, season=2), show_ref("8"),
                      show_ref("8")),
            [(2, None), (None, None)])

    def test_a_season_with_no_whole_show_row_is_a_new_row(self):
        self.assertEqual(self.plan({key("8"): 3}, show_ref(SHOW, season=1)), [(1, None)])


class _ArrivalTestCase(_BoardTestCase):
    """Tier s: another title, then the whole show, then a third; the pool holds
    one more. So a conversion that moved the row would be visible as a new rank
    or a trip to the pool."""

    def setUp(self):
        super().setUp()
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD, name="Top"))
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [
            show_ref("8"), show_ref(SHOW, season_count=5, episode_count=38, user_rating=9),
            show_ref("9"), show_ref("10")]))
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                            "items": [key("8"), key(SHOW), key("9")]}],
            "pool": [key("10")],
        }))
        self.whole = self.row(key(SHOW))

    def row(self, board_key: str):
        item = asyncio.run(ranker.fetch_item(self.user_id, BOARD, board_key))
        placed = asyncio.run(self.fetch_placement(board_key))
        return {**item, **placed}

    async def fetch_placement(self, board_key: str) -> dict:
        season = board_key.rsplit(":s", 1)[1] if ":s" in board_key else None
        found = await db.fetch_one(
            "SELECT id, category_id, rank_in_category FROM tier_items "
            " WHERE match_id = ? AND IFNULL(season, -1) = ?",
            (board_key.split(":")[2], -1 if season is None else int(season)))
        return dict(found)

    def count(self) -> int:
        return self.value("SELECT COUNT(*) FROM tier_items")


class SearchAndImportArrivalTests(_ArrivalTestCase):
    """add_titles — what a search add and the import both call."""

    def test_a_season_converts_the_whole_show_row_in_place(self):
        before = self.version()
        arrived = asyncio.run(ranker.add_titles(
            self.user_id, BOARD, [show_ref(SHOW, "Show", season=3, episode_count=8)],
            added_from="tracker"))
        self.assertEqual(arrived, ranker.Arrivals(added=0, converted=1))
        season = self.row(season_key(SHOW, 3))
        for column in ("id", "category_id", "rank_in_category"):
            self.assertEqual(season[column], self.whole[column], column)
        self.assertEqual(self.count(), 4)
        self.assertGreater(self.version(), before)
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.fetch_item(self.user_id, BOARD, key(SHOW)))

    def test_the_season_brings_what_it_knows_and_keeps_what_it_does_not(self):
        asyncio.run(ranker.add_titles(
            self.user_id, BOARD, [show_ref(SHOW, "Show S3", season=3, episode_count=8)]))
        season = self.row(season_key(SHOW, 3))
        self.assertEqual((season["title"], season["episode_count"]), ("Show S3", 8))
        # Nothing arrived about the rating, so the row keeps its own.
        self.assertEqual(season["user_rating"], 9)
        # But the whole show's season count describes the whole show: a season
        # row has none, rather than "5S 8E" beside Season 3.
        self.assertIsNone(season["season_count"])

    def test_a_season_that_knows_no_episode_total_drops_the_shows(self):
        """A seed or a search add that could not read the season's total arrives
        with none, and the converted row shows no count rather than the whole
        show's episodes as if they were the season's."""
        asyncio.run(ranker.add_titles(
            self.user_id, BOARD, [show_ref(SHOW, "Show", season=3)]))
        season = self.row(season_key(SHOW, 3))
        self.assertEqual((season["season_count"], season["episode_count"]), (None, None))

    def test_a_held_season_adds_nothing_and_bumps_nothing(self):
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref(SHOW, season=3)]))
        before = self.version()
        again = asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref(SHOW, season=3)]))
        self.assertEqual(again, ranker.Arrivals(added=0, converted=0))
        self.assertEqual(self.version(), before)

    def test_further_seasons_arrive_beside_the_converted_one(self):
        arrived = asyncio.run(ranker.add_titles(
            self.user_id, BOARD, [show_ref(SHOW, season=1), show_ref(SHOW, season=2)]))
        self.assertEqual(arrived, ranker.Arrivals(added=1, converted=1))
        self.assertEqual(self.row(season_key(SHOW, 2))["id"], self.whole["id"])
        self.assertIsNone(self.row(season_key(SHOW, 1))["category_id"])


class CapTests(_ArrivalTestCase):

    def test_a_conversion_is_not_a_new_row(self):
        with mock_cap(4):
            arrived = asyncio.run(ranker.add_titles(self.user_id, BOARD,
                                                    [show_ref(SHOW, season=2)]))
        self.assertEqual(arrived, ranker.Arrivals(added=0, converted=1))

    def test_an_add_past_the_cap_is_refused_whole_and_says_how_many_fit(self):
        with mock_cap(6):
            with self.assertRaises(ranker.ValidationError) as caught:
                asyncio.run(ranker.add_titles(self.user_id, BOARD, [
                    show_ref(SHOW, season=2), show_ref("20"), show_ref("21"),
                    show_ref("22")]))
        self.assertIn("This would add 3, and 2 more will fit.", str(caught.exception))
        # Refused whole: not the conversion, not the two that would have fitted.
        self.assertEqual(self.count(), 4)
        self.assertEqual(self.row(key(SHOW))["id"], self.whole["id"])

    def test_a_full_board_says_none_will_fit(self):
        with mock_cap(4):
            with self.assertRaises(ranker.ValidationError) as caught:
                asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref("20")]))
        self.assertIn("and none more will fit", str(caught.exception))


class SeedArrivalTests(_ArrivalTestCase):
    """seed_ratings — a rated season is a season row, through the same rule."""

    def seed(self, *entries: dict, commit: bool = True) -> dict:
        return asyncio.run(ranker.seed_ratings(self.user_id, BOARD, list(entries),
                                               commit=commit))

    def test_a_rated_season_over_a_tiered_whole_show_converts_and_stays_put(self):
        summary = self.seed(show_ref(SHOW, season=2, user_rating=10))
        self.assertEqual((summary["titles_added"], summary["titles_converted"],
                          summary["already_placed"]), (0, 1, 1))
        season = self.row(season_key(SHOW, 2))
        self.assertEqual((season["id"], season["category_id"], season["rank_in_category"]),
                         (self.whole["id"], self.whole["category_id"],
                          self.whole["rank_in_category"]))
        self.assertEqual(season["user_rating"], 10)

    def test_a_rated_season_over_a_pooled_whole_show_is_placed_by_its_score(self):
        self.seed(show_ref("10", season=1, user_rating=8))
        season = self.row(season_key("10", 1))
        board = self.board()
        tier_b = next(c for c in board["categories"] if c["uid"] == "tier-b")
        self.assertEqual([i["key"] for i in tier_b["items"]], [season_key("10", 1)])
        self.assertEqual(season["user_rating"], 8)
        self.assertEqual(self.count(), 4)

    def test_a_rated_season_of_a_show_not_on_the_board_is_a_new_season_row(self):
        summary = self.seed(show_ref("30", season=4, user_rating=7))
        self.assertEqual((summary["titles_added"], summary["titles_converted"]), (1, 0))
        self.assertEqual(self.row(season_key("30", 4))["user_rating"], 7)

    def test_a_dry_run_counts_the_conversion_and_writes_nothing(self):
        before = self.version()
        summary = self.seed(show_ref(SHOW, season=2, user_rating=10), commit=False)
        self.assertEqual(summary["titles_converted"], 1)
        self.assertEqual(self.version(), before)
        self.assertEqual(self.row(key(SHOW))["id"], self.whole["id"])

    def test_the_show_and_a_season_both_rated_keep_both_rows(self):
        summary = self.seed(show_ref(SHOW, user_rating=6),
                            show_ref(SHOW, season=2, user_rating=10))
        self.assertEqual((summary["titles_added"], summary["titles_converted"]), (1, 0))
        self.assertEqual(self.row(key(SHOW))["id"], self.whole["id"])
        self.assertEqual(self.row(season_key(SHOW, 2))["user_rating"], 10)

    def test_a_seed_past_the_cap_says_how_many_fit(self):
        with mock_cap(5):
            with self.assertRaises(ranker.ValidationError) as caught:
                self.seed(show_ref("30", user_rating=7), show_ref("31", user_rating=7))
        self.assertIn("This would add 2, and 1 more will fit.", str(caught.exception))


class RestoreIsNotAnArrivalTests(_ArrivalTestCase):

    def test_a_restored_season_comes_back_beside_its_whole_show(self):
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref(SHOW),
                                                             show_ref(SHOW, season=2)]))
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 2)]))
        self.assertTrue(asyncio.run(ranker.restore_removal(self.user_id, BOARD,
                                                           season_key(SHOW, 2))))
        self.assertEqual(self.row(key(SHOW))["id"], self.whole["id"])
        self.assertNotEqual(self.row(season_key(SHOW, 2))["id"], self.whole["id"])
