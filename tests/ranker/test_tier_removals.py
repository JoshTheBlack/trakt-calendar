"""What a rankings board remembers of the TIERS deleted from it, and putting one back.

The title history's companion (tests/ranker/test_removals.py), held to the same
standard for the same reason: what is asserted is what the DATABASE holds
afterwards — which tiers, in what order, holding which titles, under which
version — because a restore that answers 200 and puts a tier back half-filled is
exactly the failure a status check passes.

AND ONE MORE, particular to a tier: THE SENTENCE A RESTORE ANSWERS WITH MUST
ACCOUNT FOR EVERY TITLE THE TIER HELD. Some of them may legitimately stay where
they are — placed in another tier since, or taken off the board — and a restore
that said "put back" without saying so would be a partial restore that looks
whole, which is a silent loss.
"""
from __future__ import annotations

import asyncio
import json
import re
from unittest import mock

from app import db
from app.ranker import core as ranker
from tests.ranker.test_removals import BOARD, _BoardTestCase, key, show_ref


class _TierTestCase(_BoardTestCase):

    def delete(self, uid: str) -> int:
        return asyncio.run(ranker.delete_category(self.user_id, BOARD, uid))

    def restore_tier(self, uid: str) -> ranker.TierRestore:
        return asyncio.run(ranker.restore_tier_removal(self.user_id, BOARD, uid))

    def tiers(self) -> list[str]:
        """The board's tiers in the order it stores them — the tie-break a
        restore has to keep, since every tier here shares one priority."""
        return [category["uid"] for category in self.board()["categories"]]

    def category(self, uid: str) -> dict:
        return next(c for c in self.board()["categories"] if c["uid"] == uid)

    def tier_history(self) -> list[str]:
        return [entry["uid"] for entry in asyncio.run(ranker.fetch_removals(self.user_id, BOARD))
                if entry["kind"] == "tier"]

    def restore_page(self, uid: str):
        return self.client.post(f"/rankings/boards/{BOARD}/removals/tier/restore",
                                json={"tier": uid})


class DeletingWritesTheHistoryTests(_TierTestCase):

    def test_a_delete_records_the_tier_its_place_and_its_titles_in_order(self):
        self.build({"a": ["1"], "s": ["2", "3", "4"], "b": ["5"]})
        asyncio.run(ranker.update_category(self.user_id, BOARD, "s", label="Best",
                                           rank_priority=10, colour="#aa0000",
                                           is_isolated=True))
        self.assertEqual(self.delete("s"), 3)
        row, = asyncio.run(db.fetch_all("SELECT * FROM tier_category_removals"))
        self.assertEqual(row["category_uid"], "s")
        self.assertEqual(row["sort_order"], 1, "one tier sat before it")
        record = json.loads(row["category_json"])
        self.assertEqual((record["label"], record["rank_priority"], record["colour"],
                          record["is_isolated"]), ("Best", 10, "#AA0000", 1))
        self.assertIn("created_at", record)
        self.assertEqual(json.loads(row["items_json"]), [key("2"), key("3"), key("4")])
        self.assertEqual(self.pool(), ["2", "3", "4"])

    def test_the_history_is_written_in_the_deletes_own_transaction(self):
        """A failure after the history row is written and before the delete
        commits leaves NEITHER: no tier gone without its record, and no record of
        a delete that did not happen."""
        self.build({"s": ["1"]})
        with mock.patch.object(ranker, "_touch", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.delete("s")
        self.assertEqual(self.tiers(), ["s"])
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_category_removals"), 0)

    def test_a_refused_delete_writes_no_history(self):
        self.build({"s": ["1"]})
        with self.assertRaises(ranker.ValidationError):
            self.delete("ghost")
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_category_removals"), 0)

    def test_a_tier_deleted_twice_keeps_only_its_newest_deletion(self):
        self.build({"s": ["1", "2"]})
        self.delete("s")
        asyncio.run(ranker.add_categories(self.user_id, BOARD, [{"uid": "s", "label": "S2"}]))
        self.delete("s")
        row, = asyncio.run(db.fetch_all("SELECT * FROM tier_category_removals"))
        self.assertEqual(json.loads(row["category_json"])["label"], "S2")
        self.assertEqual(json.loads(row["items_json"]), [])

    def test_a_board_keeps_its_newest_tier_deletions_up_to_the_cap(self):
        uids = [f"t{n}" for n in range(ranker.TIER_REMOVAL_HISTORY_PER_BOARD + 2)]
        self.build({uid: [] for uid in uids})
        for uid in uids:
            self.delete(uid)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_category_removals"),
                         ranker.TIER_REMOVAL_HISTORY_PER_BOARD)
        self.assertEqual(
            sorted(r["category_uid"] for r in asyncio.run(db.fetch_all(
                "SELECT category_uid FROM tier_category_removals"))),
            sorted(uids[2:]))


class RestoringPutsItBackTests(_TierTestCase):

    def test_back_in_its_old_place_with_its_settings_and_its_titles_in_order(self):
        self.build({"a": ["1"], "s": ["2", "3", "4"], "b": ["5"]})
        asyncio.run(ranker.update_category(self.user_id, BOARD, "s", label="Best",
                                           rank_priority=10, colour="#aa0000",
                                           is_isolated=True))
        before = self.category("s")
        self.delete("s")
        result = self.restore_tier("s")
        self.assertEqual(self.tiers(), ["a", "s", "b"])
        self.assertEqual(self.category("s"), before)
        self.assertEqual(self.tier("s"), ["2", "3", "4"])
        self.assertEqual(self.pool(), [])
        self.assertEqual((result.returned, result.placed, result.removed, result.already),
                         (3, 0, 0, False))

    def test_its_old_place_is_clamped_to_the_tiers_there_are_now(self):
        self.build({"a": [], "b": [], "c": []})
        self.delete("c")
        self.delete("b")
        self.restore_tier("c")
        self.assertEqual(self.tiers(), ["a", "c"])

    def test_a_title_placed_in_another_tier_since_stays_there_and_is_counted(self):
        self.build({"s": ["1", "2", "3"], "a": ["4"]})
        self.delete("s")
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("2")], "a"))
        result = self.restore_tier("s")
        self.assertEqual(self.tier("s"), ["1", "3"])
        self.assertEqual(self.tier("a"), ["4", "2"])
        self.assertEqual((result.returned, result.placed, result.held), (2, 1, 3))

    def test_a_title_removed_since_stays_off_and_its_own_restore_lands_in_the_tier(self):
        """It was taken off the POOL, where the delete had sent it, so its own
        history entry said "the pool". The tier coming back is what makes that
        wrong, and its restore puts it back between the neighbours it had."""
        self.build({"s": ["1", "2", "3"]})
        self.delete("s")
        self.remove("2")
        result = self.restore_tier("s")
        self.assertEqual(self.tier("s"), ["1", "3"])
        self.assertEqual((result.returned, result.removed), (2, 1))
        entry, = [e for e in asyncio.run(ranker.fetch_removals(self.user_id, BOARD))
                  if e["kind"] == "title"]
        self.assertEqual(entry["tier_label"], "S")
        self.assertTrue(self.restore("2"))
        self.assertEqual(self.tier("s"), ["1", "2", "3"])

    def test_a_title_removed_from_another_tier_since_keeps_its_own_entry(self):
        """Placed elsewhere and THEN taken off: its entry names the tier it was
        taken from, which is the later decision about it, and stays that way."""
        self.build({"s": ["1", "2"], "a": []})
        self.delete("s")
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("2")], "a"))
        self.remove("2")
        self.restore_tier("s")
        self.assertEqual(self.value(
            "SELECT category_uid FROM tier_item_removals WHERE match_id = '2'"), "a")

    def test_a_tier_already_back_takes_only_the_pooled_titles_after_what_it_holds(self):
        """"Apply template" re-creating tier-s after tier-s was deleted is the way
        this happens. The tier is treated as already back: its settings stay as
        they are now, and the titles still in the pool go in after its own."""
        self.build({"tier-s": ["1", "2", "3"]})
        self.delete("tier-s")
        asyncio.run(ranker.add_categories(self.user_id, BOARD, list(ranker.TIER_TEMPLATE)))
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("2")], "tier-a"))
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref("9")]))
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("9")], "tier-s"))
        label = self.category("tier-s")["label"]
        result = self.restore_tier("tier-s")
        self.assertEqual(self.tier("tier-s"), ["9", "1", "3"])
        self.assertEqual(self.category("tier-s")["label"], label)
        self.assertEqual([c for c in self.tiers() if c == "tier-s"], ["tier-s"])
        self.assertEqual((result.already, result.returned, result.placed), (True, 2, 1))
        self.assertEqual(self.tier_history(), [])

    def test_a_tier_already_back_with_nothing_to_return_bumps_nothing(self):
        self.build({"s": ["1"]})
        self.delete("s")
        asyncio.run(ranker.add_categories(self.user_id, BOARD, [{"uid": "s", "label": "S"}]))
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("1")], "s"))
        version = self.version()
        result = self.restore_tier("s")
        self.assertEqual((result.already, result.returned), (True, 1))
        self.assertEqual(self.version(), version)
        self.assertEqual(self.tier_history(), [])

    def test_a_full_board_refuses_and_keeps_the_tier_restorable(self):
        self.build({"s": ["1"], "a": []})
        self.delete("s")
        with mock.patch.object(ranker, "MAX_CATEGORIES_PER_BOARD", 1):
            with self.assertRaises(ranker.ValidationError):
                self.restore_tier("s")
        self.assertEqual(self.tier_history(), ["s"])
        self.assertEqual(self.tiers(), ["a"])
        self.assertEqual(self.pool(), ["1"])
        self.restore_tier("s")
        self.assertEqual(self.tier("s"), ["1"])

    def test_a_restore_clears_its_own_history_row(self):
        self.build({"s": ["1"]})
        self.delete("s")
        self.restore_tier("s")
        self.assertEqual(self.tier_history(), [])
        with self.assertRaises(ranker.ValidationError):
            self.restore_tier("s")

    def test_a_restore_bumps_the_version_so_a_save_built_before_it_is_refused(self):
        self.build({"s": ["1", "2"]})
        self.delete("s")
        before = self.version()
        self.restore_tier("s")
        self.assertEqual(self.version(), before + 1)
        layout = {"categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                                  "items": [key("2"), key("1")]}], "pool": []}
        with self.assertRaises(ranker.VersionConflict):
            asyncio.run(ranker.save_layout(self.user_id, BOARD, {**layout, "version": before}))
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {**layout, "version": before + 1}))
        self.assertEqual(self.tier("s"), ["2", "1"])


class TheHistoryAgesTests(_TierTestCase):

    def expire(self, uid: str) -> None:
        asyncio.run(db.execute(
            "UPDATE tier_category_removals SET removed_at = ? WHERE category_uid = ?",
            (db.now() - ranker.REMOVAL_HISTORY_SECONDS - 1, uid)))

    def test_an_expired_deletion_is_neither_offered_nor_restorable_before_the_sweep(self):
        self.build({"s": [], "a": []})
        self.delete("s")
        self.delete("a")
        self.expire("s")
        self.assertEqual(self.tier_history(), ["a"])
        with self.assertRaises(ranker.ValidationError):
            self.restore_tier("s")

    def test_the_sweep_deletes_what_has_expired_and_nothing_else(self):
        self.build({"s": [], "a": []})
        self.delete("s")
        self.delete("a")
        self.expire("s")
        self.assertEqual(asyncio.run(ranker.sweep_removals()), 1)
        self.assertEqual(self.tier_history(), ["a"])

    def test_deleting_the_board_takes_its_tier_history_with_it(self):
        self.build({"s": ["1"]})
        self.delete("s")
        asyncio.run(ranker.delete_board(self.user_id, BOARD))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_category_removals"), 0)

    def test_the_history_reads_through_its_board_index(self):
        """ix_tier_category_removals_board exists for "this board, newest first",
        which is the read the page makes on every answer."""
        plan = asyncio.run(db.run(lambda conn: conn.execute(
            "EXPLAIN QUERY PLAN SELECT category_uid FROM tier_category_removals "
            " WHERE board_id = ? AND removed_at >= ? ORDER BY removed_at DESC, id DESC",
            (1, 0)).fetchall()))
        self.assertIn("ix_tier_category_removals_board",
                      " ".join(str(row["detail"]) for row in plan))


class CrossTenantTests(_TierTestCase):
    """Asserted on the database afterwards: a refusal that restores anyway is what
    a status check misses."""

    def test_another_account_cannot_delete_restore_or_read_this_boards_tiers(self):
        self.build({"s": ["1"], "a": ["2"]})
        self.delete("s")
        stranger = self.make_user("stranger", ranker_approved=True)
        with self.assertRaises(ranker.BoardNotFound):
            asyncio.run(ranker.restore_tier_removal(stranger, BOARD, "s"))
        with self.assertRaises(ranker.BoardNotFound):
            asyncio.run(ranker.delete_category(stranger, BOARD, "a"))
        self.sign_in_as(stranger)
        page = self.restore_page("s")
        self.assertNotIn("Put back", page.text)
        self.client.post(f"/rankings/boards/{BOARD}/tiers/a/delete", json={})
        self.assertEqual(self.tiers(), ["a"])
        self.assertEqual(self.pool(), ["1"])
        self.assertEqual(self.tier_history(), ["s"])


class RestoreRouteTests(_TierTestCase):
    """The delete and the restore are both buttons posting themselves, answered
    with the page — so what is asserted is that page, the sentence on it, and the
    database under it."""

    def test_a_delete_offers_the_historys_own_restore_in_its_toast(self):
        self.build({"s": ["1", "2"]})
        page = self.client.post(f"/rankings/boards/{BOARD}/tiers/s/delete", json={})
        self.assertEqual(page.status_code, 200)
        toast = re.search(r'<div id="toastHost">(.*?)</div>\s*</div>', page.text, re.S).group(1)
        self.assertIn("Tier deleted — its 2 titles went back to the pool.", toast)
        self.assertIn("is-offer", toast)
        self.assertIn(f'hx-post="/rankings/boards/{BOARD}/removals/tier/restore"', toast)
        self.assertIn('hx-vals=\'{"tier": "s"}\'', toast)
        self.assertIn("hx-json-enc", toast)

    def test_a_restore_answers_with_the_board_redrawn_and_its_next_save_is_accepted(self):
        self.build({"s": ["1", "2", "3"]})
        self.delete("s")
        page = self.restore_page("s")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Put back S with all 3 titles.", page.text)
        self.assertEqual(self.tier("s"), ["1", "2", "3"])
        drawn = int(re.search(r'"version": (\d+)', page.text).group(1))
        self.assertEqual(drawn, self.version())
        saved = self.client.post(f"/api/rankings/boards/{BOARD}/save", json={
            "version": drawn,
            "categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                            "items": [key("3"), key("2"), key("1")]}],
            "pool": []})
        self.assertEqual(saved.status_code, 200, saved.text)

    def test_the_restored_tier_is_drawn_open_with_its_rows(self):
        """For the reason a bulk move draws its destination open: on a board big
        enough to draw tiers closed, the titles leaving the pool would otherwise
        be seen landing nowhere."""
        self.build({"s": ["1"]})
        self.delete("s")
        with mock.patch("app.ranker.routes.EAGER_ROW_LIMIT", 0):
            page = self.restore_page("s")
        tier = re.search(r'<details class="ranker-tier[^"]*"\s+data-uid="s"[^>]*>', page.text).group(0)
        self.assertIn(" open", tier)

    def test_the_notice_says_what_could_not_be_put_back(self):
        self.build({"s": [str(n) for n in range(1, 11)], "a": []})
        self.delete("s")
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("4"), key("7")], "a"))
        page = self.restore_page("s")
        self.assertIn("Put back S with 8 of its 10 titles; 2 had been placed in "
                      "other tiers since.", page.text)

    def test_the_notice_counts_titles_taken_off_the_board_too(self):
        self.build({"s": ["1", "2", "3"], "a": []})
        self.delete("s")
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("1")], "a"))
        self.remove("3")
        page = self.restore_page("s")
        self.assertIn("Put back S with 1 of its 3 titles; 1 had been placed in another "
                      "tier since; 1 had been taken off the board since.", page.text)

    def test_the_notice_says_when_the_tier_was_already_back(self):
        self.build({"tier-s": ["1", "2"]})
        self.delete("tier-s")
        self.client.post(f"/rankings/boards/{BOARD}/tiers/template", json={})
        page = self.restore_page("tier-s")
        self.assertIn("S was already on the board; 2 of its 2 titles are in it now.", page.text)
        self.assertEqual(self.tier("tier-s"), ["1", "2"])

    def test_a_tier_no_longer_in_the_history_is_said_on_the_page(self):
        self.build({"s": ["1"]})
        page = self.restore_page("s")
        self.assertEqual(page.status_code, 200)
        self.assertIn("no longer in this board&#39;s removal history", page.text)
        self.assertIn('class="toast err is-flash"', page.text)

    def test_a_delete_and_a_restore_must_arrive_as_json_like_every_write(self):
        self.build({"s": ["1"], "a": []})
        self.delete("s")
        for path, body in ((f"/rankings/boards/{BOARD}/removals/tier/restore", {"tier": "s"}),
                           (f"/rankings/boards/{BOARD}/tiers/a/delete", {})):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data=body).status_code, 415)
        self.assertEqual(self.tiers(), ["a"])
        self.assertEqual(self.tier_history(), ["s"])


class HistoryMarkupTests(_TierTestCase):

    def test_tiers_and_titles_share_one_list_newest_first(self):
        self.build({"s": ["1", "2"], "a": ["3"]})
        self.remove("3")
        asyncio.run(db.execute("UPDATE tier_item_removals SET removed_at = removed_at - 10"))
        self.delete("s")
        html = self.client.get(f"/rankings?board={BOARD}").text
        entries = re.findall(
            r'<span class="ranker-title">([^<]*)</span>\s*<span class="ranker-year">([^<]*)</span>'
            r'\s*</span>\s*<button[^>]*data-restore-(tier|key)="([^"]*)"', html)
        self.assertEqual(entries, [
            ("Tier S", "2 titles", "tier", "s"),
            ("Title 3", "from A", "key", key("3")),
        ])

    def test_a_tier_restore_button_posts_itself_as_json(self):
        self.build({"s": ["1"]})
        self.delete("s")
        html = self.client.get(f"/rankings?board={BOARD}").text
        button = re.search(r'<button[^>]*data-restore-tier="s"[^>]*>', html, re.S).group(0)
        self.assertIn(f'hx-post="/rankings/boards/{BOARD}/removals/tier/restore"', button)
        self.assertIn("hx-json-enc", button)
        self.assertIn('hx-vals=\'{"tier": "s"}\'', button)

    def test_the_page_states_both_caps(self):
        self.build({"s": []})
        self.delete("s")
        html = self.client.get(f"/rankings?board={BOARD}").text
        self.assertRegex(
            html, rf"keeps the last {ranker.REMOVAL_HISTORY_PER_BOARD} titles\s+and "
                  rf"{ranker.TIER_REMOVAL_HISTORY_PER_BOARD} tiers for "
                  rf"{ranker.REMOVAL_HISTORY_DAYS} days")

    def test_no_script_deletes_a_tier_or_holds_its_undo(self):
        """The delete is a button posting itself; there is no ranker/tiers.js and
        no page loads one."""
        from app import assets
        from tests.support import STATIC_DIR
        self.assertFalse((STATIC_DIR / "js" / "ranker" / "tiers.js").exists())
        self.assertNotIn("static/js/ranker/tiers.js", assets.PAGE_SCRIPTS["rankings"])
        self.build({"s": ["1"]})
        html = self.client.get(f"/rankings?board={BOARD}").text
        self.assertNotIn("deleteTier", html)
        self.assertIn(f'hx-post="/rankings/boards/{BOARD}/tiers/s/delete"', html)
