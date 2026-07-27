"""The tier ranker: its schema, its access model, and its board data layer.

THE POINT OF THIS FILE is the cross-tenant class of bug. A ranker board is a
piece of work somebody spent an evening arranging, and every identifier the
client sends back is untrusted. The tests in CrossTenantTests therefore assert on
what the DATABASE holds after a hostile request, not merely on the status code
that came back: a route that returns 404 and writes anyway is exactly the failure
a status-only assertion misses, and it is the failure that loses somebody else's
list.

The rest covers the schema this feature adds, who is allowed onto the page, the
caps that keep one account from filling the instance, and the version check that
decides which of two open tabs wins.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_ranker -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-ranker-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, ranker  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"


def show_ref(match_id: str, title: str = "A Show", **extra) -> dict:
    """One title in the shape add_titles() takes, which is what the provider
    adapters convert their own results into."""
    return {"media": "show", "match_source": "tmdb", "match_id": match_id,
            "tmdb": int(match_id), "title": title, **extra}


class RankerTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        RankerTestCase._counter += 1
        db.set_db_path(TMP / f"ranker-{RankerTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        # Something has to exist or the first-run gate answers every request
        # before the access levels are ever consulted.
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def make_user(self, username: str, **flags) -> int:
        return asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=Settings(), **flags))

    def ranker_user(self, username: str = "ranker") -> int:
        return self.make_user(username, ranker_approved=True)

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def rows(self, sql: str, params=()) -> list:
        return asyncio.run(db.fetch_all(sql, params))

    def value(self, sql: str, params=()):
        return asyncio.run(db.fetch_value(sql, params))


class SchemaTests(RankerTestCase):
    def test_migration_is_idempotent_through_the_runner(self):
        self.assertEqual(asyncio.run(db.migrate()), 13)
        self.assertEqual(asyncio.run(db.migrate()), 13)

    def test_the_new_tables_exist(self):
        names = {row["name"] for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual(
            {"show_posters", "tier_boards", "tier_categories", "tier_items"}, names)

    def test_the_grant_seeds_onto_existing_admins_and_nobody_else(self):
        """The seeding runs INSIDE the migration, over the accounts an instance
        already had when it upgraded. A plain default of 0 would lock the
        operator out of the feature they just deployed — nobody could reach the
        screen that hands the grant out — and granting it to everyone would hand
        a new feature to accounts nobody reviewed.
        """
        db.set_db_path(TMP / "upgrade-in-place.db")

        def _apply_through(conn, last: int) -> None:
            db._ensure_version_table(conn)
            for version, step in sorted(db.MIGRATIONS, key=lambda m: m[0]):
                if version > last:
                    return
                if callable(step):
                    step(conn)
                else:
                    db._run_script(conn, step)
                conn.execute("UPDATE schema_version SET version = ?", (version,))

        def _legacy_user(conn, username: str, is_admin: bool) -> int:
            # Written with the pre-migration column list on purpose: this row has
            # to look exactly like one an older release created.
            cur = conn.execute(
                "INSERT INTO users (username, is_admin, created_at, updated_at) "
                "VALUES (?, ?, 0, 0)", (username, int(is_admin)))
            return int(cur.lastrowid)

        asyncio.run(db.run(lambda conn: _apply_through(conn, 12)))
        admin_id = asyncio.run(db.run(lambda conn: _legacy_user(conn, "legacy_admin", True)))
        plain_id = asyncio.run(db.run(lambda conn: _legacy_user(conn, "legacy_plain", False)))

        self.assertEqual(asyncio.run(db.migrate()), 13)
        self.assertEqual(
            self.value("SELECT ranker_approved FROM users WHERE id = ?", (admin_id,)), 1)
        self.assertEqual(
            self.value("SELECT ranker_approved FROM users WHERE id = ?", (plain_id,)), 0)

    def test_a_new_account_starts_without_the_grant(self):
        """The seeding is a one-off for the upgrade; accounts made afterwards
        arrive at 0 and are granted deliberately, by an admin or by an invite."""
        plain = self.make_user("plain_user", calendar_approved=True)
        self.assertEqual(
            self.value("SELECT ranker_approved FROM users WHERE id = ?", (plain,)), 0)

    def test_deleting_an_account_takes_its_boards_with_it(self):
        """The cascade is only live because foreign_keys is ON per connection, so
        it is worth asserting rather than assuming."""
        user_id = self.ranker_user()
        asyncio.run(ranker.create_board(user_id, uid="b1", name="Top 2026"))
        asyncio.run(auth.delete_user(user_id, actor_user_id=self.admin_id))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"), 0)


class GatingTests(RankerTestCase):
    """Who reaches the page and the API. Enumerated route by route so a gap names
    the endpoint rather than the group."""

    GETS = ("/rankings", "/api/rankings/boards", "/api/rankings/boards/b1")
    POSTS = ("/api/rankings/boards", "/api/rankings/boards/b1/save")

    def _assert_all_refused(self, status: int, reason: str):
        for path in self.GETS:
            with self.subTest(method="GET", path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, status)
                self.assertEqual(resp.json()["reason"], reason)
        for path in self.POSTS:
            with self.subTest(method="POST", path=path):
                resp = self.client.post(path, json={})
                self.assertEqual(resp.status_code, status)
                self.assertEqual(resp.json()["reason"], reason)
        for method in ("PATCH", "DELETE"):
            with self.subTest(method=method, path="/api/rankings/boards/b1"):
                resp = self.client.request(method, "/api/rankings/boards/b1", json={})
                self.assertEqual(resp.status_code, status)
                self.assertEqual(resp.json()["reason"], reason)

    def test_signed_out_reaches_nothing(self):
        self._assert_all_refused(401, "login_required")

    def test_an_unapproved_account_reaches_nothing(self):
        self.sign_in_as(self.make_user("nobody", calendar_approved=True))
        self._assert_all_refused(403, "ranker_not_approved")

    def test_a_refused_browser_is_sent_to_its_account_page(self):
        """The same refusal the other gated pages give: a redirect to where the
        approval state is shown, not a stack trace."""
        self.sign_in_as(self.make_user("browsing", calendar_approved=True))
        resp = self.client.get("/rankings", headers={"Accept": "text/html"},
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")

    def test_an_approved_account_gets_the_page(self):
        self.sign_in_as(self.ranker_user())
        resp = self.client.get("/rankings")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Rankings", resp.text)

    def test_the_nav_entry_appears_only_for_approved_accounts(self):
        approved = self.ranker_user("nav_yes")
        unapproved = self.make_user("nav_no", calendar_approved=True)
        asyncio.run(auth.set_calendar_approved(approved, True))

        self.sign_in_as(approved)
        self.assertIn('href="/rankings"', self.client.get("/calendar").text)
        self.sign_in_as(unapproved)
        self.assertNotIn('href="/rankings"', self.client.get("/calendar").text)

    def test_admin_alone_is_not_the_grant(self):
        """Being an administrator does not imply the grant; the migration seeds
        it separately, and revoking it must actually shut the door."""
        asyncio.run(auth.set_ranker_approved(self.admin_id, False))
        self.sign_in_as(self.admin_id)
        self.assertEqual(self.client.get("/rankings").status_code, 403)


class AdminToggleTests(RankerTestCase):
    def test_the_toggle_flips_the_grant_both_ways(self):
        target = self.make_user("target", calendar_approved=True)
        self.sign_in_as(self.admin_id)
        for approved in (True, False, True):
            resp = self.client.post(f"/api/admin/users/{target}/approval",
                                    json={"ranker": approved})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                self.value("SELECT ranker_approved FROM users WHERE id = ?", (target,)),
                int(approved))

    def test_the_toggle_leaves_the_other_approvals_alone(self):
        """The three grants are independent; a request naming one must not move
        the others."""
        target = self.make_user("independent", calendar_approved=True, distrakt_approved=True)
        self.sign_in_as(self.admin_id)
        self.client.post(f"/api/admin/users/{target}/approval", json={"ranker": True})
        row = asyncio.run(auth.get_user(target))
        self.assertEqual((row["calendar_approved"], row["distrakt_approved"]), (1, 1))

    def test_a_non_admin_cannot_grant_it(self):
        target = self.make_user("victim", calendar_approved=True)
        self.sign_in_as(self.ranker_user("not_an_admin"))
        resp = self.client.post(f"/api/admin/users/{target}/approval", json={"ranker": True})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            self.value("SELECT ranker_approved FROM users WHERE id = ?", (target,)), 0)


class InviteGrantTests(RankerTestCase):
    """The invite flag, which is where a new account's grant actually comes
    from on a real instance."""

    def _invite(self, **flags) -> str:
        return asyncio.run(auth.create_invite(created_by=self.admin_id, **flags))["token"]

    def _register(self, username: str, token: str):
        return self.client.post("/register", json={
            "username": username, "password": "hunter2hunter2",
            "password_confirm": "hunter2hunter2", "invite": token,
        })

    def test_an_invite_with_the_flag_produces_an_approved_account(self):
        resp = self._register("invited_yes", self._invite(grants_ranker_on_accept=True))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            self.value("SELECT ranker_approved FROM users WHERE username = ?",
                       ("invited_yes",)), 1)

    def test_an_invite_without_it_does_not(self):
        resp = self._register("invited_no", self._invite(grants_ranker_on_accept=False))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            self.value("SELECT ranker_approved FROM users WHERE username = ?",
                       ("invited_no",)), 0)

    def test_invites_issued_before_this_feature_grant_nothing(self):
        """The column defaults to 0 precisely so an invite already in the wild
        does not silently start handing out a feature its issuer never chose."""
        token = self._invite()
        asyncio.run(db.execute(
            "UPDATE invites SET grants_ranker_on_accept = 0 WHERE token = ?", (token,)))
        self._register("legacy_invitee", token)
        self.assertEqual(
            self.value("SELECT ranker_approved FROM users WHERE username = ?",
                       ("legacy_invitee",)), 0)

    def test_the_api_defaults_the_flag_on(self):
        """The checkbox ships checked, so an issuer who does not think about it
        gets the same behaviour the calendar's flag already has."""
        self.sign_in_as(self.admin_id)
        resp = self.client.post("/api/admin/invites", json={"label": "default"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.value("SELECT grants_ranker_on_accept FROM invites WHERE id = ?",
                       (resp.json()["id"],)), 1)


class BoardTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)

    def test_board_crud_round_trip(self):
        created = self.client.post("/api/rankings/boards", json={
            "uid": "b1", "name": "Top Movies 2026", "year": 2026, "media_scope": "movie"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["board"]["name"], "Top Movies 2026")

        patched = self.client.patch("/api/rankings/boards/b1", json={"name": "Renamed"})
        self.assertEqual(patched.json()["board"]["name"], "Renamed")
        self.assertEqual(patched.json()["board"]["year"], 2026)

        listed = self.client.get("/api/rankings/boards").json()["boards"]
        self.assertEqual([b["uid"] for b in listed], ["b1"])

        self.assertEqual(
            self.client.request("DELETE", "/api/rankings/boards/b1", json={}).status_code, 200)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"), 0)

    def test_a_clone_copies_the_tiers_and_their_titles(self):
        asyncio.run(ranker.create_board(self.user_id, uid="src", name="Source"))
        asyncio.run(ranker.add_titles(self.user_id, "src", [show_ref("1"), show_ref("2")]))
        asyncio.run(ranker.save_layout(self.user_id, "src", {
            "version": 1,
            "categories": [{"uid": "s", "label": "S", "items": ["show:tmdb:1"]}],
            "pool": ["show:tmdb:2"],
        }))
        clone = asyncio.run(ranker.clone_board(self.user_id, "src", uid="dup"))
        self.assertEqual(clone["name"], "Source (copy)")

        board = asyncio.run(ranker.fetch_board(self.user_id, "dup"))
        self.assertEqual([c["uid"] for c in board["categories"]], ["s"])
        self.assertEqual([i["key"] for i in board["categories"][0]["items"]], ["show:tmdb:1"])
        self.assertEqual([i["key"] for i in board["pool"]], ["show:tmdb:2"])
        # The source is untouched: a clone reads it, it never moves anything.
        source = asyncio.run(ranker.fetch_board(self.user_id, "src"))
        self.assertEqual([i["key"] for i in source["categories"][0]["items"]], ["show:tmdb:1"])

    def test_the_same_title_may_sit_in_two_boards(self):
        """Board-scoped rather than user-scoped uniqueness: the same film in both
        "Top 2026" and "All-Time" is a normal thing to want."""
        for uid in ("b1", "b2"):
            asyncio.run(ranker.create_board(self.user_id, uid=uid))
            added = asyncio.run(ranker.add_titles(self.user_id, uid, [show_ref("550")]))
            self.assertEqual(added, 1)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 2)

    def test_the_same_title_twice_in_one_board_is_a_no_op(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("550")]))
        again = asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("550", "Renamed")]))
        self.assertEqual(again, 0)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 1)

    def test_a_movie_and_a_show_with_the_same_id_are_different_titles(self):
        """TMDB ids are namespaced per media type, so the identity is the pair."""
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [
            show_ref("550"), {**show_ref("550"), "media": "movie"}]))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 2)

    def test_deleting_a_tier_returns_its_titles_to_the_pool(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1"), show_ref("2")]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [{"uid": "s", "label": "S",
                            "items": ["show:tmdb:1", "show:tmdb:2"]}],
        }))
        returned = asyncio.run(ranker.delete_category(self.user_id, "b1", "s"))

        self.assertEqual(returned, 2)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 2)
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM tier_items WHERE category_id IS NULL"), 2)

    def test_removing_a_title_is_the_only_true_delete(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1"), show_ref("2")]))
        removed = asyncio.run(ranker.remove_items(self.user_id, "b1", ["show:tmdb:1"]))
        self.assertEqual(removed, 1)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 1)

    def test_fetching_a_board_costs_two_queries_regardless_of_tier_count(self):
        """A query per category would make a 30-tier board 31 round trips just to
        draw it, so the grouping is done in Python over two reads."""
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(
            self.user_id, "b1", [show_ref(str(n)) for n in range(1, 11)]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [
                {"uid": f"t{n}", "label": f"T{n}", "items": [f"show:tmdb:{n}"]}
                for n in range(1, 11)
            ],
        }))
        def _counted(conn):
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            try:
                return ranker.read_board(conn, self.user_id, "b1"), statements
            finally:
                conn.set_trace_callback(None)

        board, statements = asyncio.run(db.run(_counted))

        self.assertEqual(len(board["categories"]), 10)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        # One to resolve the board — which is also the ownership check — then one
        # for its categories and one for every item on it.
        self.assertEqual(len(selects), 3, selects)


class ValidationTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)

    def test_an_over_long_board_name_is_refused_not_truncated(self):
        resp = self.client.post("/api/rankings/boards",
                                json={"uid": "b1", "name": "x" * 61})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"), 0)

    def test_the_per_user_board_cap_holds(self):
        for n in range(ranker.MAX_BOARDS_PER_USER):
            asyncio.run(ranker.create_board(self.user_id, uid=f"b{n}"))
        resp = self.client.post("/api/rankings/boards", json={"uid": "one-too-many"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"),
                         ranker.MAX_BOARDS_PER_USER)

    def test_the_per_year_board_cap_holds(self):
        for n in range(ranker.MAX_BOARDS_PER_YEAR):
            asyncio.run(ranker.create_board(self.user_id, uid=f"y{n}", year=2026))
        resp = self.client.post("/api/rankings/boards", json={"uid": "extra", "year": 2026})
        self.assertEqual(resp.status_code, 400)
        # A different year still has room, so the cap is per year and not a
        # second spelling of the per-user one.
        self.assertEqual(
            self.client.post("/api/rankings/boards",
                             json={"uid": "other", "year": 2025}).status_code, 200)

    def test_the_item_cap_refuses_the_whole_request(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.add_titles(self.user_id, "b1", [
                show_ref(str(n)) for n in range(ranker.MAX_ITEMS_PER_BOARD + 1)]))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)

    def test_the_tier_cap_holds_and_writes_nothing(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        resp = self.client.post("/api/rankings/boards/b1/save", json={
            "version": 0,
            "categories": [{"uid": f"t{n}", "label": "T"}
                           for n in range(ranker.MAX_CATEGORIES_PER_BOARD + 1)],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), 0)

    def test_a_bad_colour_or_priority_is_refused(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        for bad in ({"colour": "red"}, {"colour": "#GGGGGG"}, {"rank_priority": 1001},
                    {"rank_priority": -1}, {"label": "x" * 41}):
            with self.subTest(bad=bad):
                resp = self.client.post("/api/rankings/boards/b1/save", json={
                    "version": 0, "categories": [{"uid": "t1", **bad}]})
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), 0)

    def test_the_server_renormalizes_ordering_rather_than_trusting_it(self):
        """Client numbering is a hint about ORDER, never the stored value: the
        arrangement has to come out a dense 0..N-1 or an export's tie-breaks stop
        being reproducible."""
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(
            self.user_id, "b1", [show_ref("1"), show_ref("2"), show_ref("3")]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [
                {"uid": "a", "label": "A", "sort_order": 900,
                 "items": ["show:tmdb:3", "show:tmdb:1"]},
                {"uid": "b", "label": "B", "sort_order": 900,
                 "items": ["show:tmdb:2"]},
            ],
        }))
        orders = [row["sort_order"] for row in self.rows(
            "SELECT sort_order FROM tier_categories ORDER BY sort_order")]
        self.assertEqual(orders, [0, 1])
        ranks = [(row["match_id"], row["rank_in_category"]) for row in self.rows(
            "SELECT match_id, rank_in_category FROM tier_items "
            " WHERE category_id IS NOT NULL ORDER BY category_id, rank_in_category")]
        self.assertEqual(ranks, [("3", 0), ("1", 1), ("2", 0)])

    def test_a_title_named_twice_in_one_layout_is_refused(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1")]))
        resp = self.client.post("/api/rankings/boards/b1/save", json={
            "version": 1,
            "categories": [{"uid": "a", "items": ["show:tmdb:1"]},
                           {"uid": "b", "items": ["show:tmdb:1"]}],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), 0)

    def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        resp = self.client.post(
            "/api/rankings/boards/b1/save",
            content=b'{"version": 1, "pad": "' + b"x" * (1024 * 1024 + 64) + b'"}',
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 413)

    def test_a_form_encoded_save_is_refused_by_the_request_shape_rule(self):
        """The anti-CSRF control the whole feature's client has to live within:
        anything but application/json is refused, so a boosted form POST would
        never reach a handler here."""
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        resp = self.client.post("/api/rankings/boards/b1/save", data={"version": "1"})
        self.assertEqual(resp.status_code, 415)


class VersionConflictTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1"), show_ref("2")]))

    def test_a_stale_save_is_refused_and_changes_nothing(self):
        """Two tabs: the first save wins, and the second is told to reload rather
        than allowed to clobber an arrangement it never saw."""
        first = self.client.post("/api/rankings/boards/b1/save", json={
            "version": 1,
            "categories": [{"uid": "a", "label": "First",
                            "items": ["show:tmdb:1", "show:tmdb:2"]}],
        })
        self.assertEqual(first.status_code, 200)
        stored_version = first.json()["version"]

        stale = self.client.post("/api/rankings/boards/b1/save", json={
            "version": 1,
            "categories": [{"uid": "a", "label": "Second", "items": ["show:tmdb:2"]}],
        })
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["reason"], "version_conflict")

        self.assertEqual(self.value("SELECT label FROM tier_categories"), "First")
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM tier_items WHERE category_id IS NOT NULL"), 2)
        self.assertEqual(self.value("SELECT version FROM tier_boards"), stored_version)

    def test_a_layout_missing_a_tier_the_board_has_is_refused(self):
        """A payload holding the current version by definition knows the full set
        of tiers, so one that omits a tier was not built from a current read."""
        self.client.post("/api/rankings/boards/b1/save", json={
            "version": 1, "categories": [{"uid": "a"}, {"uid": "b"}]})
        version = self.value("SELECT version FROM tier_boards")
        resp = self.client.post("/api/rankings/boards/b1/save", json={
            "version": version, "categories": [{"uid": "a"}]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), 2)


class CrossTenantTests(RankerTestCase):
    """The control this whole feature's access model rests on.

    Every assertion here reads the database AFTER the hostile request. A route
    that answers 404 and writes anyway passes a status-only test and still loses
    the victim's work, which is the failure mode being tested for.
    """

    def setUp(self):
        super().setUp()
        self.victim = self.ranker_user("victim")
        self.attacker = self.ranker_user("attacker")
        asyncio.run(ranker.create_board(self.victim, uid="secret", name="Victim's board"))
        asyncio.run(ranker.add_titles(self.victim, "secret", [show_ref("1"), show_ref("2")]))
        asyncio.run(ranker.save_layout(self.victim, "secret", {
            "version": 1,
            "categories": [{"uid": "top", "label": "Top",
                            "items": ["show:tmdb:1", "show:tmdb:2"]}],
        }))
        self.baseline = self._snapshot()
        self.sign_in_as(self.attacker)

    def _snapshot(self) -> dict:
        """Everything about the VICTIM's data that a hostile write could move.

        Scoped to the victim's own rows, so the attacker legitimately creating
        boards of their own — which they may — does not read as tampering.
        """
        boards = self.rows(
            "SELECT id, uid, name, version FROM tier_boards WHERE user_id = ? ORDER BY id",
            (self.victim,))
        categories = self.rows(
            "SELECT c.uid, c.label, c.rank_priority, c.colour, c.sort_order "
            "  FROM tier_categories c JOIN tier_boards b ON b.id = c.board_id "
            " WHERE b.user_id = ? ORDER BY c.uid", (self.victim,))
        items = self.rows(
            "SELECT i.match_id, i.category_id, i.rank_in_category "
            "  FROM tier_items i JOIN tier_boards b ON b.id = i.board_id "
            " WHERE b.user_id = ? ORDER BY i.match_id", (self.victim,))
        return {
            "boards": [tuple(r) for r in boards],
            "categories": [tuple(r) for r in categories],
            "items": [tuple(r) for r in items],
        }

    def assertVictimUntouched(self):
        self.assertEqual(self._snapshot(), self.baseline)

    def test_another_users_board_cannot_be_read(self):
        resp = self.client.get("/api/rankings/boards/secret")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("Victim's board", resp.text)

    def test_another_users_board_does_not_appear_in_the_listing(self):
        self.assertEqual(self.client.get("/api/rankings/boards").json()["boards"], [])

    def test_another_users_board_cannot_be_renamed(self):
        resp = self.client.patch("/api/rankings/boards/secret", json={"name": "Owned"})
        self.assertEqual(resp.status_code, 404)
        self.assertVictimUntouched()

    def test_another_users_board_cannot_be_deleted(self):
        resp = self.client.request("DELETE", "/api/rankings/boards/secret", json={})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"), 1)
        self.assertVictimUntouched()

    def test_another_users_board_cannot_be_reordered(self):
        resp = self.client.post("/api/rankings/boards/secret/save", json={
            "version": 1,
            "categories": [{"uid": "top", "label": "Owned", "colour": "#FF0000",
                            "items": ["show:tmdb:2", "show:tmdb:1"]}],
        })
        self.assertEqual(resp.status_code, 404)
        self.assertVictimUntouched()

    def test_another_users_board_cannot_be_cloned_into_this_account(self):
        """A clone reads every row of the source, so it is the one operation that
        would copy somebody else's list wholesale if it skipped the check."""
        resp = self.client.post("/api/rankings/boards",
                                json={"uid": "stolen", "clone_of": "secret"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM tier_boards WHERE user_id = ?", (self.attacker,)), 0)
        self.assertVictimUntouched()

    def test_a_uid_collision_across_accounts_keeps_the_boards_separate(self):
        """uids are client-generated, so two accounts naming a board the same
        thing is ordinary rather than exceptional. Each must see only its own."""
        self.client.post("/api/rankings/boards", json={"uid": "secret", "name": "Mine"})
        mine = self.client.get("/api/rankings/boards/secret").json()["board"]
        self.assertEqual(mine["name"], "Mine")
        self.assertEqual(mine["categories"], [])
        self.assertEqual(mine["pool"], [])
        self.assertVictimUntouched()

    def test_a_tier_on_another_users_board_cannot_be_deleted(self):
        """The category uid is real and does exist — just not on any board this
        caller owns. Resolving it through the board is what refuses it."""
        with self.assertRaises(ranker.BoardNotFound):
            asyncio.run(ranker.delete_category(self.attacker, "secret", "top"))
        self.assertVictimUntouched()

    def test_titles_cannot_be_added_to_another_users_board(self):
        with self.assertRaises(ranker.BoardNotFound):
            asyncio.run(ranker.add_titles(self.attacker, "secret", [show_ref("999")]))
        self.assertVictimUntouched()

    def test_titles_cannot_be_removed_from_another_users_board(self):
        with self.assertRaises(ranker.BoardNotFound):
            asyncio.run(ranker.remove_items(self.attacker, "secret", ["show:tmdb:1"]))
        self.assertVictimUntouched()

    def test_a_title_from_another_board_cannot_be_pulled_into_this_one(self):
        """Item keys are resolved against the board being written, so naming a
        title that exists elsewhere refuses rather than reaching across."""
        asyncio.run(ranker.create_board(self.attacker, uid="mine"))
        resp = self.client.post("/api/rankings/boards/mine/save", json={
            "version": 0,
            "categories": [{"uid": "a", "items": ["show:tmdb:1"]}],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value(
            "SELECT COUNT(*) FROM tier_items WHERE board_id = "
            "(SELECT id FROM tier_boards WHERE uid = 'mine')"), 0)
        self.assertVictimUntouched()


if __name__ == "__main__":
    unittest.main()
