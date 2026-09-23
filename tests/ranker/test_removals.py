"""What a rankings board remembers of the titles taken off it, and putting one back.

THE POINT OF THIS FILE is the one place in the ranker where getting it wrong
loses somebody's work. Taking a title off a board deletes its row; what makes
that recoverable is a history written in the SAME transaction, and a restore that
puts the title back where it sat. So what is asserted here is what the DATABASE
holds afterwards — which tier, in what order, under which version — and not only
what a route answered, because a restore that answers 200 and puts the title in
the wrong place is exactly the failure a status check passes.

The version assertions are the other half. A restore is a board mutation, and a
page that did not learn of the bump would have its next drag refused with the 409
that means "another tab changed this" — for a change it made itself.
"""
from __future__ import annotations

import asyncio
import json
import re
from unittest import mock

from app import authz, db
from app.main import app
from app.ranker import core as ranker
from tests.support import STATIC_DIR, TEMPLATES_DIR, AppTestCase

BOARD = "b1"


def show_ref(match_id: str, title: str | None = None, **extra) -> dict:
    return {"media": "show", "match_source": "tmdb", "match_id": match_id,
            "tmdb": int(match_id), "title": title or f"Title {match_id}", **extra}


def key(match_id: str) -> str:
    return f"show:tmdb:{match_id}"


class _BoardTestCase(AppTestCase):
    """One ranker account and a board built from tiers of tmdb ids."""

    def setUp(self):
        super().setUp()
        # Something has to exist or the first-run gate answers before any access
        # level is consulted.
        self.make_user("admin_user", is_admin=True)
        self.user_id = self.make_user("ranker", ranker_approved=True)
        self.sign_in_as(self.user_id)

    def build(self, tiers: dict[str, list[str]] | None = None, pool: tuple[str, ...] = ()):
        tiers = tiers or {}
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD, name="Top"))
        wanted = [*(i for items in tiers.values() for i in items), *pool]
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref(i) for i in wanted]))
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": uid, "label": uid.upper(), "rank_priority": 10,
                            "items": [key(i) for i in items]}
                           for uid, items in tiers.items()],
            "pool": [key(i) for i in pool],
        }))

    def board(self) -> dict:
        return asyncio.run(ranker.fetch_board(self.user_id, BOARD))

    def version(self) -> int:
        return self.board()["version"]

    def tier(self, uid: str) -> list[str]:
        return [item["match_id"] for category in self.board()["categories"]
                if category["uid"] == uid for item in category["items"]]

    def pool(self) -> list[str]:
        return [item["match_id"] for item in self.board()["pool"]]

    def remove(self, *ids: str) -> int:
        return asyncio.run(ranker.remove_items(self.user_id, BOARD, [key(i) for i in ids]))

    def restore(self, match_id: str) -> bool:
        return asyncio.run(ranker.restore_removal(self.user_id, BOARD, key(match_id)))

    def history(self) -> list[str]:
        return [entry["key"] for entry in asyncio.run(ranker.fetch_removals(self.user_id, BOARD))]

    def value(self, sql: str, params=()):
        return asyncio.run(db.fetch_value(sql, params))


class RemovingWritesTheHistoryTests(_BoardTestCase):

    def test_a_removal_is_recorded_with_where_it_sat(self):
        self.build({"s": ["1", "2", "3"]})
        self.remove("2")
        row, = asyncio.run(db.fetch_all("SELECT * FROM tier_item_removals"))
        self.assertEqual((row["media"], row["match_source"], row["match_id"]),
                         ("show", "tmdb", "2"))
        self.assertEqual(row["category_uid"], "s")
        self.assertEqual(row["rank_in_category"], 1)
        self.assertEqual(json.loads(row["item_json"])["title"], "Title 2")
        self.assertEqual(self.tier("s"), ["1", "3"])

    def test_a_restored_title_is_the_same_title_it_was(self):
        """Every field the row holds comes back, not an approximation of it —
        which is what storing the record whole is for."""
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD))
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref(
            "7", "Seven", ids={"trakt": 70, "tmdb": 7, "trakt_slug": "seven"},
            year=2026, network="HBO", season_count=3, episode_count=24, runtime=55,
            user_rating=9)], added_from="ratings"))
        before = asyncio.run(ranker.fetch_item(self.user_id, BOARD, key("7")))
        self.remove("7")
        self.restore("7")
        after = asyncio.run(ranker.fetch_item(self.user_id, BOARD, key("7")))
        self.assertEqual(after, before)

    def test_a_title_removed_twice_keeps_only_its_newest_removal(self):
        """Removed from a tier, re-added by hand into the pool, removed again: a
        restore puts it back where it sat LAST, and there is one row to restore."""
        self.build({"s": ["1", "2"]})
        self.remove("1")
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref("1")]))
        self.remove("1")
        rows = asyncio.run(db.fetch_all("SELECT category_uid FROM tier_item_removals"))
        self.assertEqual([r["category_uid"] for r in rows], [None])

    def test_a_removal_that_is_refused_writes_no_history(self):
        self.build({"s": ["1"]})
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.remove_items(self.user_id, BOARD, [key("1"), key("404")]))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_item_removals"), 0)
        self.assertEqual(self.tier("s"), ["1"])

    def test_a_board_keeps_its_newest_removals_up_to_the_cap(self):
        ids = [str(n) for n in range(1, ranker.REMOVAL_HISTORY_PER_BOARD + 3)]
        self.build(pool=tuple(ids))
        for match_id in ids:
            self.remove(match_id)
        self.assertEqual(self.history(), [key(i) for i in reversed(ids)][:ranker.REMOVAL_HISTORY_PER_BOARD])
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_item_removals"),
                         ranker.REMOVAL_HISTORY_PER_BOARD)


class RestoringPutsItBackTests(_BoardTestCase):

    def test_back_into_its_tier_between_the_neighbours_it_had(self):
        """With a save between the removal and the restore, which is what a page
        does after any drag: the save closes the gap the removal left, so the
        title that took the old rank has to move down or the two would tie."""
        self.build({"s": ["1", "2", "3"]})
        self.remove("2")
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                            "items": [key("1"), key("3")]}],
            "pool": []}))
        self.assertTrue(self.restore("2"))
        self.assertEqual(self.tier("s"), ["1", "2", "3"])

    def test_a_gap_no_save_has_closed_does_not_misplace_a_restore(self):
        """Two removals with no save between them leave the stored ranks 0 and 3.
        Putting back the title that sat at rank 2 still lands it between the
        neighbours it had."""
        self.build({"s": ["1", "2", "3", "4"]})
        self.remove("2")
        self.remove("3")
        self.assertTrue(self.restore("3"))
        self.assertEqual(self.tier("s"), ["1", "3", "4"])

    def test_a_tier_deleted_since_sends_it_to_the_pool(self):
        self.build({"s": ["1", "2"]})
        self.remove("1")
        asyncio.run(ranker.delete_category(self.user_id, BOARD, "s"))
        self.assertTrue(self.restore("1"))
        self.assertIn("1", self.pool())

    def test_a_tier_renamed_since_still_takes_it_back_and_is_named_by_its_new_label(self):
        self.build({"s": ["1", "2"]})
        self.remove("1")
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": "s", "label": "Best", "rank_priority": 10, "items": [key("2")]}],
            "pool": []}))
        entry, = asyncio.run(ranker.fetch_removals(self.user_id, BOARD))
        self.assertEqual(entry["tier_label"], "Best")
        self.restore("1")
        self.assertEqual(self.tier("s"), ["1", "2"])

    def test_a_title_re_added_by_hand_is_left_alone_and_the_answer_says_so(self):
        self.build({"s": ["1", "2"]})
        self.remove("1")
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref("1")]))
        version = self.version()
        self.assertFalse(self.restore("1"))
        self.assertEqual(self.pool(), ["1"])
        self.assertEqual(self.tier("s"), ["2"])
        self.assertEqual(self.version(), version, "a restore that wrote nothing bumped the version")
        self.assertEqual(self.history(), [], "the no-op left a restore on offer that can only be a no-op")

    def test_a_restore_clears_its_own_history_row(self):
        self.build({"s": ["1"]})
        self.remove("1")
        self.restore("1")
        self.assertEqual(self.history(), [])
        with self.assertRaises(ranker.ValidationError):
            self.restore("1")

    def test_a_restore_bumps_the_version_so_a_save_built_before_it_is_refused(self):
        self.build({"s": ["1", "2"]})
        self.remove("1")
        before = self.version()
        self.restore("1")
        self.assertEqual(self.version(), before + 1)
        layout = {"categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                                  "items": [key("2"), key("1")]}], "pool": []}
        with self.assertRaises(ranker.VersionConflict):
            asyncio.run(ranker.save_layout(self.user_id, BOARD, {**layout, "version": before}))
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {**layout, "version": before + 1}))
        self.assertEqual(self.tier("s"), ["2", "1"])

    def test_a_full_board_refuses_and_keeps_the_title_restorable(self):
        self.build(pool=("1", "2"))
        self.remove("1")
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref("3")]))
        with mock.patch.object(ranker, "MAX_ITEMS_PER_BOARD", 2):
            with self.assertRaises(ranker.ValidationError):
                self.restore("1")
        self.assertEqual(self.history(), [key("1")])
        self.restore("1")
        self.assertIn("1", self.pool())


class TheHistoryAgesTests(_BoardTestCase):

    def expire(self, match_id: str) -> None:
        asyncio.run(db.execute(
            "UPDATE tier_item_removals SET removed_at = ? WHERE match_id = ?",
            (db.now() - ranker.REMOVAL_HISTORY_SECONDS - 1, match_id)))

    def test_an_expired_removal_is_neither_offered_nor_restorable_before_the_sweep(self):
        self.build(pool=("1", "2"))
        self.remove("1", "2")
        self.expire("1")
        self.assertEqual(self.history(), [key("2")])
        with self.assertRaises(ranker.ValidationError):
            self.restore("1")

    def test_the_sweep_deletes_what_has_expired_and_nothing_else(self):
        self.build(pool=("1", "2"))
        self.remove("1", "2")
        self.expire("1")
        self.assertEqual(asyncio.run(ranker.sweep_removals()), 1)
        self.assertEqual(self.history(), [key("2")])

    def test_the_sweep_is_one_range_delete_on_each_tables_index(self):
        """ix_tier_item_removals_sweep and ix_tier_category_removals_sweep exist
        for exactly these statements, so the statements the sweep actually issues
        are asked how SQLite will run them — one per history table."""
        issued = []
        real = db.execute

        async def recording(sql, params=()):
            issued.append(sql)
            return await real(sql, params)

        with mock.patch.object(db, "execute", recording):
            asyncio.run(ranker.sweep_removals())
        plans = {
            statement: " ".join(str(row["detail"]) for row in asyncio.run(db.run(
                lambda conn, s=statement: conn.execute("EXPLAIN QUERY PLAN " + s, (0,)).fetchall())))
            for statement in issued
        }
        self.assertEqual(len(plans), 2)
        for index in ("ix_tier_item_removals_sweep", "ix_tier_category_removals_sweep"):
            with self.subTest(index=index):
                self.assertEqual(sum(index in plan for plan in plans.values()), 1)

    def test_deleting_the_board_takes_its_history_with_it(self):
        self.build(pool=("1",))
        self.remove("1")
        asyncio.run(ranker.delete_board(self.user_id, BOARD))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_item_removals"), 0)


class CrossTenantTests(_BoardTestCase):
    """Asserted on the database afterwards, as the board's own cross-tenant tests
    are: a refusal that restores anyway is what a status check misses."""

    def test_another_account_cannot_restore_or_read_this_boards_history(self):
        self.build({"s": ["1", "2"]})
        self.remove("1")
        stranger = self.make_user("stranger", ranker_approved=True)
        with self.assertRaises(ranker.BoardNotFound):
            asyncio.run(ranker.restore_removal(stranger, BOARD, key("1")))
        with self.assertRaises(ranker.BoardNotFound):
            asyncio.run(ranker.fetch_removals(stranger, BOARD))
        self.sign_in_as(stranger)
        response = self.client.post(f"/rankings/boards/{BOARD}/removals/restore",
                                    json={"key": key("1")})
        self.assertNotIn("Put back on the board", response.text)
        self.assertNotIn("Title 2", response.text)
        self.assertNotIn("Title 1", self.client.get(
            f"/rankings/fragments/removed?board={BOARD}").text)
        self.assertEqual(self.tier("s"), ["2"])
        self.assertEqual(self.history(), [key("1")])


class RestoreRouteTests(_BoardTestCase):
    """The Restore button posts itself and the answer is the page, redrawn. So
    what is asserted is that page and the database under it — and that the
    version the redrawn page carries is the one its next save needs, which is what
    a restore used to have to bump by hand in a script."""

    def restore_page(self, match_id: str):
        return self.client.post(f"/rankings/boards/{BOARD}/removals/restore",
                                json={"key": key(match_id)})

    def test_a_restore_answers_with_the_board_redrawn_and_its_next_save_is_accepted(self):
        self.build({"s": ["1", "2", "3"]})
        self.remove("2")
        page = self.restore_page("2")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Put back on the board.", page.text)
        # In the floating toast host, which is out of the page's flow — the first
        # version sat in the pool column and moved everything under it as it went.
        self.assertRegex(
            page.text,
            r'<div id="toastHost">\s*<div class="toast ok is-flash"\s+role="status">\s*'
            r'Put back on the board\.')
        self.assertEqual(self.tier("s"), ["1", "2", "3"])
        drawn = int(re.search(r'"version": (\d+)', page.text).group(1))
        self.assertEqual(drawn, self.version())
        saved = self.client.post(f"/api/rankings/boards/{BOARD}/save", json={
            "version": drawn,
            "categories": [{"uid": "s", "label": "S", "rank_priority": 10,
                            "items": [key("3"), key("2"), key("1")]}],
            "pool": []})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.tier("s"), ["3", "2", "1"])

    def test_the_redrawn_page_has_the_history_open(self):
        """The button that asked sits inside it, and a page that closed it on
        every press would make restoring several titles a chore."""
        self.build({"s": ["1", "2"]})
        self.remove("1", "2")
        page = self.restore_page("1")
        self.assertIn('<details class="ranker-removed" id="rankerRemoved" open>', page.text)

    def test_the_no_op_is_said_on_the_page(self):
        self.build({"s": ["1"]})
        self.remove("1")
        asyncio.run(ranker.add_titles(self.user_id, BOARD, [show_ref("1")]))
        page = self.restore_page("1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("already on this board", page.text)

    def test_a_title_no_longer_in_the_history_is_said_on_the_page(self):
        self.build({"s": ["1"]})
        page = self.restore_page("1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("no longer in this board&#39;s removal history", page.text)
        self.assertIn('class="toast err is-flash"', page.text)

    def test_a_restore_must_arrive_as_json_like_every_write(self):
        """The button uses hx-json-enc for exactly this: a form-encoded post, which
        is all a cross-origin page could send, is refused before it is routed."""
        self.build({"s": ["1"]})
        self.remove("1")
        response = self.client.post(f"/rankings/boards/{BOARD}/removals/restore",
                                    data={"key": key("1")})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.history(), [key("1")])


class HistoryMarkupTests(_BoardTestCase):

    def test_the_page_draws_the_history_newest_first_saying_where_each_goes_back_to(self):
        self.build({"s": ["1", "2"], "a": ["3"]}, pool=("4",))
        self.remove("4")
        self.remove("1")
        self.remove("3")
        asyncio.run(ranker.delete_category(self.user_id, BOARD, "a"))
        html = self.client.get(f"/rankings?board={BOARD}").text
        entries = re.findall(
            r'<span class="ranker-title">([^<]*)</span>\s*<span class="ranker-year">([^<]*)</span>'
            r'\s*</span>\s*<button[^>]*data-restore-key="([^"]*)"', html)
        self.assertEqual(entries, [
            ("Title 3", "its tier is gone, so back to the pool", key("3")),
            ("Title 1", "from S", key("1")),
            ("Title 4", "from the pool", key("4")),
        ])

    def test_the_history_arrives_with_the_page_a_removal_answers_with(self):
        """IT HAS NO FRAGMENT OF ITS OWN ANY MORE. It used to redraw itself on an
        event the page fired after a removal; a removal answers with the whole
        page now, so the list comes with everything else and there is nothing
        left to keep in step."""
        self.build(pool=("1",))
        page = self.client.post(f"/rankings/boards/{BOARD}/items/remove",
                                json={"key": key("1")})
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'data-restore-key="{key("1")}"', page.text)
        self.assertNotIn("rankerRemovedRefresh", page.text)
        self.assertNotIn(
            "/rankings/fragments/removed",
            {str(getattr(r, "path", "")) for r in authz.iter_routes(app)})

    def test_a_removal_offers_the_same_button_in_a_toast(self):
        """One way a title comes back however it is asked for: the toast's Undo is
        the history's own Restore, posting to the same route with the same key.
        The nine seconds it stands for are the stylesheet's animation."""
        self.build(pool=("1",))
        page = self.client.post(f"/rankings/boards/{BOARD}/items/remove",
                                json={"key": key("1")})
        toast = re.search(r'<div id="toastHost">(.*?)</div>\s*</div>', page.text, re.S).group(1)
        self.assertIn("is-offer", toast)
        self.assertIn(f'hx-post="/rankings/boards/{BOARD}/removals/restore"', toast)
        self.assertIn('hx-vals=\'{"key": "show:tmdb:1"}\'', toast)
        css = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".toast.is-flash.is-offer { animation-duration: 9s;", css)

    def test_a_restore_button_posts_itself_as_json(self):
        self.build(pool=("1",))
        self.remove("1")
        html = self.client.get(f"/rankings?board={BOARD}").text
        button = re.search(r'<button[^>]*data-restore-key="show:tmdb:1"[^>]*>', html, re.S).group(0)
        self.assertIn(f'hx-post="/rankings/boards/{BOARD}/removals/restore"', button)
        self.assertIn("hx-json-enc", button)
        self.assertIn('hx-vals=\'{"key": "show:tmdb:1"}\'', button)

    def test_no_request_attribute_sits_on_the_element_around_the_restore_buttons(self):
        """This app turns attribute inheritance on, and htmx looks a request's verb
        up through it — so an hx-get on the history's <details> would make every
        Restore button inside it send that GET instead of its own POST."""
        self.build(pool=("1",))
        html = self.client.get(f"/rankings?board={BOARD}").text
        details = re.search(r'<details class="ranker-removed"[^>]*>', html).group(0)
        self.assertNotRegex(details, r"hx-(get|post|trigger)")

    def test_an_empty_history_is_hidden_by_the_stylesheet(self):
        css = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".ranker-removed:not(:has(.ranker-removed-entry)) { display: none; }", css)
