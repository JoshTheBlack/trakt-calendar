"""Which tier a whole-page answer draws open on a board big enough to draw its
tiers closed.

Every action on the board that is not a drag answers with the whole page, and
above EAGER_ROW_LIMIT that page draws each tier as a closed header. An action on
ONE ROW — changing its season, taking it off, putting it back — must bring that
row's tier back open, or the reader is left looking at a closed header where the
row they just changed was. A move and a tier restore already drew their
destination open; they are pinned here beside the rest so the rule reads as one.
"""
from __future__ import annotations

import asyncio
import re
from unittest import mock

from app.ranker import core as ranker, item_keys, routes as ranker_routes
from tests.ranker.test_fragments import FragmentTestCase, title_ref


def open_tiers(html: str) -> list[str]:
    """The uids of the tiers a page draws open."""
    return re.findall(r'<details class="ranker-tier[^"]*"\s+data-uid="([^"]+)" open', html)


def key(n: int, season: int | None = None) -> str:
    return item_keys.board_key("show", "tmdb", str(1000 + n), season)


class OpenTierTests(FragmentTestCase):
    """A board with two tiers of two titles each and one title in the pool,
    drawn with every tier closed."""

    def setUp(self):
        super().setUp()
        asyncio.run(ranker.create_board(self.user_id, uid="b1", name="Top"))
        asyncio.run(ranker.add_titles(self.user_id, "b1",
                                      [title_ref(str(1000 + n), f"T{n}") for n in range(5)]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [
                {"uid": "tier-s", "label": "S", "rank_priority": 60, "items": [key(0), key(1)]},
                {"uid": "tier-a", "label": "A", "rank_priority": 50, "items": [key(2), key(3)]},
            ],
            "pool": [key(4)],
        }))
        closed = mock.patch.object(ranker_routes, "EAGER_ROW_LIMIT", 0)
        closed.start()
        self.addCleanup(closed.stop)

    def post(self, path: str, body: dict) -> str:
        response = self.client.post(f"/rankings/boards/b1/{path}?board=b1", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.text

    def test_the_page_itself_draws_every_tier_closed(self):
        """The premise: without an action naming a tier, none is open."""
        self.assertEqual(open_tiers(self.client.get("/rankings?board=b1").text), [])

    def test_a_season_change_keeps_the_rows_tier_open(self):
        html = self.post("items/season", {"key": key(2), "season": "3"})
        self.assertIn("This row is Season 3 now.", html)
        self.assertEqual(open_tiers(html), ["tier-a"])

    def test_a_refused_season_change_still_keeps_the_rows_tier_open(self):
        self.post("items/season", {"key": key(2), "season": "3"})
        html = self.post("items/season", {"key": key(9), "season": "1"})
        self.assertEqual(open_tiers(html), [])
        html = self.post("items/season", {"key": key(2, 3), "season": "3"})
        self.assertEqual(open_tiers(html), ["tier-a"])

    def test_a_removal_keeps_the_tier_it_came_out_of_open(self):
        html = self.post("items/remove", {"key": key(1)})
        self.assertIn("Removed from the board.", html)
        self.assertEqual(open_tiers(html), ["tier-s"])

    def test_a_removal_from_the_pool_opens_no_tier(self):
        self.assertEqual(open_tiers(self.post("items/remove", {"key": key(4)})), [])

    def test_putting_a_removed_row_back_opens_its_tier(self):
        self.post("items/remove", {"key": key(3)})
        html = self.post("removals/restore", {"key": key(3)})
        self.assertIn("Put back on the board.", html)
        self.assertEqual(open_tiers(html), ["tier-a"])

    def test_a_removed_row_whose_tier_has_gone_opens_nothing(self):
        self.post("items/remove", {"key": key(0)})
        self.post("tiers/tier-s/delete", {})
        html = self.post("items/remove", {"key": key(2)})
        self.assertEqual(open_tiers(html), ["tier-a"])
        # tier-s is gone, so the history row for key(0) names nothing to open,
        # and putting it back lands it in the pool.
        self.assertEqual(open_tiers(self.post("removals/restore", {"key": key(0)})), [])

    def test_a_move_opens_where_the_titles_went(self):
        html = self.post("items/move", {f"{ranker_routes.PICK_PREFIX}{key(4)}": "1",
                                        "tier": "tier-a"})
        self.assertEqual(open_tiers(html), ["tier-a"])

    def test_restoring_a_deleted_tier_opens_it(self):
        self.post("tiers/tier-a/delete", {})
        html = self.post("removals/tier/restore", {"tier": "tier-a"})
        self.assertEqual(open_tiers(html), ["tier-a"])

    def test_deleting_a_tier_opens_nothing(self):
        self.assertEqual(open_tiers(self.post("tiers/tier-s/delete", {})), [])
