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
import shutil
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-ranker-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import auth, db, distrakt, posters, ranker, ranker_export  # noqa: E402
from app import ranker_import, ranker_routes, ranker_sources, trakt  # noqa: E402
from app import user_images  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.ranker_sources import Media, TitleRef  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"


def patched(module, name, replacement):
    """Swap one function on a module for the duration of a block. No network
    reaches a provider in this suite; the seams are driven with stand-ins."""
    return mock.patch.object(module, name, replacement)


def async_result(value):
    """An async stand-in that always answers `value`."""
    async def _call(*args, **kwargs):
        return value
    return _call


def _explode(reason: str):
    """An async stand-in that fails the test if it is reached at all."""
    async def _call(*args, **kwargs):
        raise AssertionError(reason)
    return _call


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
        # A fresh database has to mean a fresh disk as well. User ids restart
        # from the same numbers in every test, so an earlier test's generated
        # images would sit exactly where this one's account expects to find its
        # own — and be served back as a cache hit that never rendered anything.
        shutil.rmtree(user_images.USER_DATA_DIR, ignore_errors=True)
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
    # Derived rather than hardcoded: what these assert is "the runner lands on
    # the newest migration", not "the newest migration is number 14", and a
    # literal here means every later migration breaks this file for no reason.
    LATEST = max(version for version, _ in db.MIGRATIONS)

    def test_migration_is_idempotent_through_the_runner(self):
        self.assertEqual(asyncio.run(db.migrate()), self.LATEST)
        self.assertEqual(asyncio.run(db.migrate()), self.LATEST)

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

        self.assertEqual(asyncio.run(db.migrate()), self.LATEST)
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


class RatingsSeedTests(RankerTestCase):
    """A seed arranges; it does not synchronize. The tests that matter here are
    the ones about what it REFUSES to touch — somebody's own arrangement is the
    artifact, and a seed that reshuffled it would destroy the thing it was asked
    to help build."""

    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))

    def rated(self, match_id: str, score: int, media: str = "show", **extra) -> dict:
        return {"media": media, "match_source": "tmdb", "match_id": match_id,
                "tmdb": int(match_id), "title": f"Title {match_id}",
                "user_rating": score, **extra}

    def seed(self, entries, *, commit=True) -> dict:
        return asyncio.run(ranker.seed_ratings(self.user_id, "b1", entries, commit=commit))

    def test_scores_land_in_the_template_tiers(self):
        summary = self.seed([self.rated("1", 10), self.rated("2", 9), self.rated("3", 4)])
        self.assertEqual(summary["titles_added"], 3)
        self.assertEqual(summary["tiers_created"], 3)

        board = asyncio.run(ranker.fetch_board(self.user_id, "b1"))
        placed = {c["uid"]: [i["key"] for i in c["items"]] for c in board["categories"]}
        self.assertEqual(placed["tier-s"], ["show:tmdb:1"])
        self.assertEqual(placed["tier-a"], ["show:tmdb:2"])
        self.assertEqual(placed["tier-f"], ["show:tmdb:3"])
        self.assertEqual(board["pool"], [])

    def test_the_score_is_stored_for_display(self):
        self.seed([self.rated("1", 8)])
        self.assertEqual(self.value("SELECT user_rating FROM tier_items"), 8)
        self.assertEqual(self.value("SELECT added_from FROM tier_items"), "ratings")

    def test_a_title_the_user_has_already_placed_is_never_moved(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1")]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [{"uid": "mine", "label": "Mine", "items": ["show:tmdb:1"]}],
        }))
        summary = self.seed([self.rated("1", 10)])
        self.assertEqual(summary["already_placed"], 1)
        self.assertEqual(summary["titles_placed"], 0)

        board = asyncio.run(ranker.fetch_board(self.user_id, "b1"))
        self.assertEqual([c["uid"] for c in board["categories"]], ["mine"])
        self.assertEqual([i["key"] for i in board["categories"][0]["items"]], ["show:tmdb:1"])

    def test_a_pooled_title_is_placed_rather_than_duplicated(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1")]))
        summary = self.seed([self.rated("1", 10)])
        self.assertEqual((summary["titles_added"], summary["titles_placed"]), (0, 1))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 1)

    def test_seeding_twice_changes_nothing_the_second_time(self):
        first = self.seed([self.rated("1", 10), self.rated("2", 7)])
        second = self.seed([self.rated("1", 10), self.rated("2", 7)])
        self.assertEqual(second["titles_added"], 0)
        self.assertEqual(second["titles_placed"], 0)
        self.assertEqual(second["already_placed"], 2)
        self.assertEqual(second["tiers_created"], 0)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), first["tiers_created"])

    def test_a_preview_writes_nothing(self):
        summary = self.seed([self.rated("1", 10)], commit=False)
        self.assertEqual(summary["titles_added"], 1)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"), 0)
        self.assertEqual(self.value("SELECT version FROM tier_boards"), 0)

    def test_titles_outside_the_boards_scope_are_skipped_not_refused(self):
        asyncio.run(ranker.update_board(self.user_id, "b1", media_scope="movie"))
        summary = self.seed([self.rated("1", 10), self.rated("2", 10, media="movie")])
        self.assertEqual(summary["out_of_scope"], 1)
        self.assertEqual(summary["titles_added"], 1)
        self.assertEqual(self.value("SELECT media FROM tier_items"), "movie")

    def test_a_seeded_tier_appends_after_what_is_already_in_it(self):
        self.seed([self.rated("1", 10)])
        self.seed([self.rated("2", 10)])
        board = asyncio.run(ranker.fetch_board(self.user_id, "b1"))
        tier = next(c for c in board["categories"] if c["uid"] == "tier-s")
        self.assertEqual([i["key"] for i in tier["items"]], ["show:tmdb:1", "show:tmdb:2"])
        self.assertEqual([i["rank_in_category"] for i in tier["items"]], [0, 1])

    def test_a_score_outside_one_to_ten_is_refused(self):
        with self.assertRaises(ranker.ValidationError):
            self.seed([self.rated("1", 11)])
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)

    def test_the_whole_ladder_is_reachable(self):
        self.assertEqual(
            [ranker.tier_for_rating(score) for score in range(10, 0, -1)],
            ["tier-s", "tier-a", "tier-b", "tier-c", "tier-d",
             *["tier-f"] * 5],
        )


class ItemRouteTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))

    def test_titles_are_added_and_removed_through_the_routes(self):
        added = self.client.post("/api/rankings/boards/b1/items", json={
            "refs": [show_ref("1"), show_ref("2")]})
        self.assertEqual(added.json()["added"], 2)

        removed = self.client.request("DELETE", "/api/rankings/boards/b1/items",
                                      json={"keys": ["show:tmdb:1"]})
        self.assertEqual(removed.json()["removed"], 1)
        self.assertEqual(self.value("SELECT match_id FROM tier_items"), "2")

    def test_a_malformed_add_is_refused_whole(self):
        resp = self.client.post("/api/rankings/boards/b1/items", json={
            "refs": [show_ref("1"), {"media": "show", "match_source": "nope", "match_id": "2"}]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 0)


class SearchRouteTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)

    def search(self, **body):
        with patched(ranker_sources, "search_source", lambda: _CannedSearch()):
            return self.client.post("/api/rankings/search", json=body)

    def test_a_query_too_short_to_mean_anything_is_refused(self):
        self.assertEqual(self.search(query="a").status_code, 400)

    def test_an_unknown_media_type_is_refused(self):
        self.assertEqual(self.search(query="test", media="album").status_code, 400)

    def test_results_are_capped(self):
        results = self.search(query="test").json()["results"]
        self.assertEqual(len(results), ranker_routes.MAX_SEARCH_RESULTS)

    def test_the_budget_bounds_a_script_rather_than_a_person(self):
        for _ in range(ranker_routes.SEARCH_MAX_PER_WINDOW):
            self.assertEqual(self.search(query="test").status_code, 200)
        self.assertEqual(self.search(query="test").status_code, 429)

    def test_the_budget_is_per_account(self):
        for _ in range(ranker_routes.SEARCH_MAX_PER_WINDOW + 1):
            self.search(query="test")
        self.sign_in_as(self.ranker_user("someone_else"))
        self.assertEqual(self.search(query="test").status_code, 200)

    def test_the_budget_survives_a_restart(self):
        """The budget lives in `login_attempts`, not a process-local dict, so it
        is unaffected by the app restarting — a real behaviour change from the
        in-process counter this replaced, not a side effect."""
        self.search(query="test")
        self.assertEqual(
            self.value(
                "SELECT COUNT(*) FROM login_attempts WHERE key_type = 'ranker_search' "
                "AND key_value = ?", (str(self.user_id),)),
            1)


class _CannedSearch:
    """More results than the route is allowed to return, so the cap is visible."""

    async def search(self, query, media):
        return [
            TitleRef(media=media, title=f"Title {n}", ids={"tmdb": n})
            for n in range(1, ranker_routes.MAX_SEARCH_RESULTS + 10)
        ]


class WarmRouteTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1"), show_ref("2")]))

    def test_it_warms_the_titles_it_is_given(self):
        seen = {}

        async def _ensure(settings, pairs):
            seen["pairs"] = list(pairs)
            return len(seen["pairs"])

        with patched(posters, "ensure_posters", _ensure), \
             patched(posters, "cached_poster", lambda media, tmdb: Path("x.jpg")):
            resp = self.client.post("/api/rankings/boards/b1/warm",
                                    json={"keys": ["show:tmdb:1"]})
        self.assertEqual(seen["pairs"], [("show", 1)])
        self.assertEqual(resp.json(), {"ok": True, "generated": 1, "cached": 1, "missing": 0})

    def test_an_oversized_request_is_refused(self):
        resp = self.client.post("/api/rankings/boards/b1/warm",
                                json={"keys": ["show:tmdb:1"] * 251})
        self.assertEqual(resp.status_code, 400)

    def test_another_users_board_cannot_be_warmed(self):
        self.sign_in_as(self.ranker_user("someone_else"))
        resp = self.client.post("/api/rankings/boards/b1/warm", json={})
        self.assertEqual(resp.status_code, 404)


class TrackerImportTests(RankerTestCase):
    """The optional import: what counts as finished, and who is even told it
    exists. The availability rules are as much the point as the aggregation —
    an account that cannot use this must not learn from the app that it is
    there."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("importer", ranker_approved=True, distrakt_approved=True)
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1"))

    def month(self, month: str, *, closed: bool = False) -> None:
        asyncio.run(db.execute(
            "INSERT INTO distrakt_months (user_id, month, closed, created_at) VALUES (?, ?, ?, 0)",
            (self.user_id, month, int(closed)),
        ))

    def show(self, month: str, trakt_id: int, season: int, *, watched: int, total: int,
             bucket: str | None = None, title: str = "A Show", tmdb: int | None = 100) -> None:
        asyncio.run(db.execute(
            "INSERT INTO distrakt_shows (user_id, month, trakt_id, tmdb, slug, title, season, "
            "network, watched, total, bucket, started_airing, finished_airing) "
            "VALUES (?, ?, ?, ?, 'a-show', ?, ?, 'HBO', ?, ?, ?, 1, 1)",
            (self.user_id, month, trakt_id, tmdb, title, season, watched, total, bucket),
        ))

    def movie(self, trakt_id: int, watched_at: str, title: str = "A Film") -> None:
        asyncio.run(db.execute(
            "INSERT INTO distrakt_movie_watches (user_id, trakt_id, watched_at, title, year) "
            "VALUES (?, ?, ?, ?, 2026)",
            (self.user_id, trakt_id, watched_at, title),
        ))

    def finished(self, media=Media.SHOW, year=None):
        source = ranker_import.finished_titles_source()
        return asyncio.run(source.finished_titles(self.user_id, media=media, year=year))

    def linked_account(self):
        """An open month needs the caller's own credential to be worked out at
        all, so a test about one has to supply it."""
        return patched(ranker_sources, "user_trakt_settings",
                       async_result(Settings(trakt_client_id="c", trakt_access_token="t")))

    def test_a_closed_month_uses_its_stored_verdict(self):
        """A frozen month's counts stopped being refreshed the moment it froze,
        so recomputing from them would silently unfinish finished shows."""
        self.month("2026-01", closed=True)
        self.show("2026-01", 10, 1, watched=0, total=0, bucket="completed")
        self.show("2026-01", 11, 1, watched=0, total=0, bucket="cleanup")
        self.assertEqual([ref.ids["trakt"] for ref in self.finished()], [10])

    def test_an_open_month_is_worked_out_the_same_way_the_other_feature_does_it(self):
        """THE STORED ROWS OF AN OPEN MONTH ARE NOT ITS LIVE COUNTS. They are
        only written back when the month freezes, so a season finished this
        month sits at 0/0 in the database while the other feature's own screen
        shows it completed. Reading those rows made this import report nothing
        for an account looking at six finished shows, so it asks that feature to
        work the month out instead — one rule, one answer.
        """
        self.month("2026-07")
        self.show("2026-07", 10, 1, watched=0, total=0, title="Finished")
        self.show("2026-07", 11, 1, watched=0, total=0, title="Halfway")
        live = [
            {"trakt_id": 10, "season": 1, "title": "Finished", "network": "HBO",
             "tmdb": 100, "slug": "a-show", "total": 8, "bucket": "completed"},
            {"trakt_id": 11, "season": 1, "title": "Halfway", "network": "HBO",
             "tmdb": 101, "slug": "b-show", "total": 8, "bucket": "keepup"},
        ]
        with self.linked_account(), patched(distrakt, "compute_live_shows", async_result(live)):
            refs = self.finished()
        self.assertEqual([ref.title for ref in refs], ["Finished"])
        self.assertEqual(refs[0].episode_count, 8)

    def test_an_open_month_is_skipped_rather_than_guessed_at_without_a_credential(self):
        """Its live counts cannot be obtained, and the stored ones would answer
        "nothing is finished" — which is a wrong answer, not a missing one."""
        self.month("2026-07")
        self.show("2026-07", 10, 1, watched=0, total=0)
        with patched(distrakt, "compute_live_shows",
                     _explode("the open month was computed with no credential")):
            self.assertEqual(self.finished(), [])

    def test_only_the_open_month_costs_anything(self):
        """THE COST RULE. A closed month is answered from its stored verdict, so
        an account with years of history spends nothing on any of it — only the
        single month still open is worked out live, however much came before.
        """
        for month in ("2024-01", "2024-02", "2025-06", "2025-07", "2026-01"):
            self.month(month, closed=True)
            self.show(month, 10, 1, watched=0, total=5, bucket="completed")
        with patched(distrakt, "compute_live_shows",
                     _explode("a frozen month was recomputed")), \
             patched(distrakt, "load_month", _explode("a frozen month was reloaded")):
            self.assertEqual(len(self.finished()), 1)

    def test_counts_describe_how_much_was_finished(self):
        self.month("2026-01", closed=True)
        self.show("2026-01", 10, 1, watched=0, total=10, bucket="completed")
        self.show("2026-01", 10, 2, watched=0, total=12, bucket="completed")
        self.show("2026-01", 10, 3, watched=0, total=6, bucket="keepup")
        ref, = self.finished()
        self.assertEqual((ref.season_count, ref.episode_count), (2, 22))

    def test_the_year_filter_is_when_it_was_watched(self):
        self.month("2025-06", closed=True)
        self.month("2026-06", closed=True)
        self.show("2025-06", 10, 1, watched=0, total=5, bucket="completed", title="Older")
        self.show("2026-06", 11, 1, watched=0, total=5, bucket="completed", title="Newer")
        self.assertEqual([r.title for r in self.finished(year=2026)], ["Newer"])
        self.assertEqual(
            asyncio.run(ranker_import.available_years(self.user_id, Media.SHOW)), [2026, 2025])

    def test_a_movie_is_resolved_to_a_real_id_map(self):
        """Those records carry no tmdb, so each needs one summary lookup — and
        the poster URL that comes back with it is kept for later."""
        self.movie(77, "2026-03-04T00:00:00.000Z")
        summary = {"title": "A Film", "year": 2025, "runtime": 101,
                   "ids": {"trakt": 77, "tmdb": 550, "imdb": "tt0137523", "slug": ""},
                   "images": {"poster": ["image.tmdb.org/p/w500/x.jpg"]}}
        with patched(trakt, "fetch_movie_summary", async_result(summary)):
            ref, = self.finished(Media.MOVIE)
        self.assertEqual(ref.identity(), ("tmdb", "550"))
        self.assertEqual(ref.ids["imdb"], "tt0137523")
        self.assertEqual(ref.runtime, 101)
        self.assertEqual(
            self.value("SELECT url FROM show_posters WHERE media = 'movie' AND tmdb = 550"),
            "https://image.tmdb.org/p/w500/x.jpg")

    def test_a_movie_whose_lookup_fails_still_imports(self):
        self.movie(77, "2026-03-04T00:00:00.000Z")

        async def _boom(*args, **kwargs):
            raise trakt.TraktError("Could not reach Trakt", 502)

        with patched(trakt, "fetch_movie_summary", _boom):
            ref, = self.finished(Media.MOVIE)
        # It keeps the id it had and simply has no artwork key, which the
        # renderer answers with a placeholder rather than a missing title.
        self.assertEqual(ref.ids, {"trakt": 77})
        self.assertIsNone(ref.identity())

    def test_the_import_route_adds_to_the_pool(self):
        self.month("2026-01", closed=True)
        self.show("2026-01", 10, 1, watched=0, total=5, bucket="completed")
        resp = self.client.post("/api/rankings/boards/b1/import/tracker", json={"media": "show"})
        self.assertEqual(resp.json(), {"ok": True, "found": 1, "added": 1})
        self.assertEqual(self.value("SELECT added_from FROM tier_items"), "tracker")
        self.assertIsNone(self.value("SELECT category_id FROM tier_items"))

    def test_an_account_with_no_data_is_not_offered_it(self):
        self.assertFalse(asyncio.run(ranker_import.tracker_available(self.user_id)))
        self.assertNotIn("import", self.client.get("/api/rankings/sources").json()["sources"])

    def test_an_unapproved_account_is_told_nothing(self):
        """404, not a 403 explaining what they are missing: the route answers as
        though the source does not exist, because for that account it does not."""
        other = self.make_user("ranker_only", ranker_approved=True)
        asyncio.run(ranker.create_board(other, uid="b2"))
        self.sign_in_as(other)
        resp = self.client.post("/api/rankings/boards/b2/import/tracker", json={"media": "show"})
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("import", resp.text.lower().replace("no such source.", ""))


def a_board(*, categories, pool=()) -> dict:
    """A board in the shape the data layer returns one, for testing the
    consolidation rules without a database.

    Items arrive from `read_board` already ordered by `rank_in_category, id` and
    categories by `sort_order, id`, so these fixtures are written in that order
    too — the tie-break is the SQL's, and consolidation inherits it rather than
    re-deriving it.
    """
    return {
        "uid": "b1", "name": "A Board", "year": 2026, "version": 1,
        "categories": [
            {"uid": uid, "label": label, "rank_priority": priority,
             "is_isolated": isolated, "sort_order": order, "colour": colour,
             "items": [dict(show_ref(str(n)), title=f"Title {n}") for n in items]}
            for order, (uid, label, priority, isolated, colour, items)
            in enumerate(categories)
        ],
        "pool": [dict(show_ref(str(n)), title=f"Pooled {n}") for n in pool],
    }


class ConsolidationTests(unittest.TestCase):
    """The one ordering every export shares. It has to be TOTAL and STABLE: two
    exports of an unchanged board that disagree about who came fourth would make
    the image and the text block pasted beside it contradict each other."""

    def test_priority_orders_the_tiers(self):
        board = a_board(categories=[
            ("low", "B", 50, False, None, [3, 4]),
            ("high", "A", 100, False, None, [1, 2]),
        ])
        ranked = ranker_export.consolidate(board)
        self.assertEqual([e.match_id for e in ranked], ["1", "2", "3", "4"])
        self.assertEqual([e.rank for e in ranked], [1, 2, 3, 4])

    def test_equal_priorities_fall_back_to_the_stored_order(self):
        """sort_order then id, which the data layer's ORDER BY already applied —
        so a stable sort on priority alone is the whole rule, not half of it."""
        board = a_board(categories=[
            ("first", "A", 10, False, None, [1]),
            ("second", "B", 10, False, None, [2]),
            ("third", "C", 10, False, None, [3]),
        ])
        self.assertEqual([e.match_id for e in ranker_export.consolidate(board)],
                         ["1", "2", "3"])

    def test_an_isolated_tier_is_left_out_of_the_global_ranking(self):
        board = a_board(categories=[
            ("main", "A", 50, False, None, [1, 2]),
            ("silo", "Anime", 100, True, None, [8, 9]),
        ])
        self.assertEqual([e.match_id for e in ranker_export.consolidate(board)], ["1", "2"])

    def test_one_tier_can_be_exported_on_its_own_isolated_or_not(self):
        board = a_board(categories=[
            ("main", "A", 50, False, None, [1, 2]),
            ("silo", "Anime", 100, True, None, [8, 9]),
        ])
        for uid, expected in (("main", ["1", "2"]), ("silo", ["8", "9"])):
            with self.subTest(uid=uid):
                ranked = ranker_export.consolidate(
                    board, scope="category", category_uid=uid)
                self.assertEqual([e.match_id for e in ranked], expected)
                # An isolated tier keeps its OWN 1..X numbering.
                self.assertEqual([e.rank for e in ranked], [1, 2])

    def test_pool_titles_are_never_exported(self):
        board = a_board(categories=[("main", "A", 50, False, None, [1])], pool=[7, 8])
        ranked = ranker_export.consolidate(board)
        self.assertEqual([e.match_id for e in ranked], ["1"])

    def test_top_x_slices_after_ordering(self):
        board = a_board(categories=[
            ("low", "B", 10, False, None, [3, 4]),
            ("high", "A", 90, False, None, [1, 2]),
        ])
        ranked = ranker_export.consolidate(board, top_x=3)
        self.assertEqual([e.match_id for e in ranked], ["1", "2", "3"])

    def test_a_tier_that_is_not_on_the_board_is_refused(self):
        board = a_board(categories=[("main", "A", 50, False, None, [1])])
        with self.assertRaises(ranker_export.ExportError):
            ranker_export.consolidate(board, scope="category", category_uid="nope")

    def test_a_board_with_nothing_tiered_is_refused_rather_than_rendered_empty(self):
        board = a_board(categories=[], pool=[1, 2])
        with self.assertRaises(ranker_export.ExportError):
            ranker_export.consolidate(board)

    def test_the_tier_colour_travels_with_each_title(self):
        board = a_board(categories=[("main", "S", 50, False, "#FF7F7F", [1])])
        entry = ranker_export.consolidate(board)[0]
        self.assertEqual(entry.colour, "#FF7F7F")
        self.assertEqual(entry.tier_label, "S")


class ExportRouteTests(RankerTestCase):
    """The export endpoints: what they refuse before rendering, what they only
    render once, and how often one account may ask."""

    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1", name="Top 2026", year=2026))

    def fill(self, count: int, *, tier: str = "S", priority: int = 50,
             isolated: bool = False) -> None:
        asyncio.run(ranker.add_titles(
            self.user_id, "b1", [show_ref(str(n), f"Title {n}") for n in range(count)]))
        version = asyncio.run(ranker.fetch_board(self.user_id, "b1"))["version"]
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": version,
            "categories": [{"uid": "t1", "label": tier, "rank_priority": priority,
                            "is_isolated": isolated,
                            "items": [f"show:tmdb:{n}" for n in range(count)]}],
        }))

    def export(self, **body):
        return self.client.post("/api/rankings/boards/b1/export",
                                json={"top_x": 25, "columns": 5, **body})

    def test_an_export_comes_back_as_an_attachment_named_after_the_board(self):
        self.fill(6)
        resp = self.export(top_x=6, columns=3, fmt="jpeg", title="Top Shows",
                           username="someone")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/jpeg")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("Top 2026 Top Shows.jpeg", resp.headers["content-disposition"])
        self.assertGreater(len(resp.content), 1000)

    def test_a_hundred_titles_in_three_columns_is_refused_for_webp_and_allowed_for_jpeg(self):
        """The measured trap: WebP hard-fails above 16383 pixels, and the canvas
        this asks for is 27748 tall. The refusal has to arrive BEFORE the render,
        which is why it names a size nothing has drawn yet."""
        self.fill(100)
        refused = self.export(top_x=100, columns=3, fmt="webp")
        self.assertEqual(refused.status_code, 400)
        body = refused.json()
        self.assertEqual(body["reason"], "canvas_too_large")
        self.assertEqual((body["width"], body["height"]), (1500, 27748))
        self.assertEqual(body["limit"], 16383)
        self.assertIn("16383", body["error"])

        allowed = self.export(top_x=100, columns=3, fmt="jpeg")
        self.assertEqual(allowed.status_code, 200)

    def test_the_size_check_runs_before_anything_is_rendered(self):
        self.fill(100)
        with patched(ranker_routes, "_render", _explode("nothing may be rendered")):
            self.assertEqual(self.export(top_x=100, columns=3, fmt="webp").status_code, 400)

    def test_an_identical_second_export_is_served_from_the_cache(self):
        self.fill(6)
        first = self.export(top_x=6, columns=3, fmt="jpeg", title="Top")
        self.assertEqual(first.status_code, 200)
        # A second render would also be refused by the cooldown; proving the
        # cache means proving neither happened.
        with patched(ranker_routes, "_render", _explode("this was already rendered")):
            second = self.export(top_x=6, columns=3, fmt="jpeg", title="Top")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, first.content)

    def test_a_changed_title_or_header_image_misses_the_cache(self):
        self.fill(6)
        self.assertEqual(self.export(top_x=6, columns=3, fmt="jpeg", title="Top").status_code, 200)
        asyncio.run(db.execute("DELETE FROM login_attempts"))
        changed = self.export(top_x=6, columns=3, fmt="jpeg", title="Different")
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(
            len(list((_generated_root(self.user_id)).rglob("*.jpeg"))), 2)

    def test_a_second_export_inside_the_cooldown_is_refused(self):
        self.fill(6)
        self.assertEqual(self.export(top_x=6, columns=3, fmt="jpeg", title="A").status_code, 200)
        refused = self.export(top_x=6, columns=3, fmt="jpeg", title="B")
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.json()["reason"], "export_cooldown")

    def test_a_refused_export_says_how_long_is_left(self):
        self.fill(6)
        self.export(top_x=6, columns=3, fmt="jpeg", title="A")
        body = self.export(top_x=6, columns=3, fmt="jpeg", title="B").json()
        self.assertTrue(0 < body["retry_after"] <= ranker_routes.EXPORT_COOLDOWN_SECONDS)
        self.assertIn(f"{body['retry_after']} second", body["error"])

    def test_being_refused_does_not_restart_the_wait(self):
        """A limiter that recorded its own refusals would push the window forward
        on every rejected click, so somebody pressing the button while they wait
        would never be let through — the countdown has to count down."""
        self.fill(6)
        self.export(top_x=6, columns=3, fmt="jpeg", title="A")
        first = self.export(top_x=6, columns=3, fmt="jpeg", title="B").json()["retry_after"]

        # Age the recorded render so a few seconds appear to have passed, then
        # spend several attempts against the closed door.
        asyncio.run(db.execute(
            "UPDATE login_attempts SET attempted_at = attempted_at - 5 "
            " WHERE key_type = 'ranker_export'"))
        for _ in range(3):
            refused = self.export(top_x=6, columns=3, fmt="jpeg", title="B")
        self.assertEqual(refused.status_code, 429)
        self.assertLess(refused.json()["retry_after"], first)
        # And the attempts themselves left nothing behind to extend it with.
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM login_attempts WHERE key_type = 'ranker_export'"),
            1)

    def test_downloading_the_same_image_again_is_exempt_from_the_cooldown(self):
        """The cooldown exists to bound RENDERING. Refusing a cached download
        would punish somebody for clicking the button they were given twice."""
        self.fill(6)
        self.assertEqual(self.export(top_x=6, columns=3, fmt="jpeg", title="A").status_code, 200)
        self.assertEqual(self.export(top_x=6, columns=3, fmt="jpeg", title="A").status_code, 200)

    def test_the_cooldown_is_stored_where_every_other_volume_limit_is(self):
        self.fill(6)
        self.export(top_x=6, columns=3, fmt="jpeg", title="A")
        self.assertEqual(
            self.value("SELECT key_value FROM login_attempts WHERE key_type = 'ranker_export'"),
            str(self.user_id))

    def test_the_cooldown_is_per_account(self):
        self.fill(6)
        self.export(top_x=6, columns=3, fmt="jpeg", title="A")
        other = self.ranker_user("someone_else")
        asyncio.run(ranker.create_board(other, uid="b1"))
        asyncio.run(ranker.add_titles(other, "b1", [show_ref("1")]))
        version = asyncio.run(ranker.fetch_board(other, "b1"))["version"]
        asyncio.run(ranker.save_layout(other, "b1", {
            "version": version,
            "categories": [{"uid": "t1", "label": "S", "items": ["show:tmdb:1"]}]}))
        self.sign_in_as(other)
        self.assertEqual(self.export(top_x=1, columns=3, fmt="jpeg").status_code, 200)

    def test_a_generated_image_lands_under_the_account_that_made_it(self):
        """So that deleting the account sweeps it, with nothing of theirs living
        outside their own directory."""
        self.fill(6)
        self.export(top_x=6, columns=3, fmt="jpeg", title="A")
        made = list(_generated_root(self.user_id).rglob("*.jpeg"))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].parent.name, "2026")
        self.assertTrue(made[0].name.startswith("b1-"))

    def test_the_render_cache_is_capped(self):
        self.fill(6)
        for n in range(ranker_export.MAX_CACHED_RENDERS + 3):
            asyncio.run(db.execute("DELETE FROM login_attempts"))
            self.assertEqual(
                self.export(top_x=6, columns=3, fmt="jpeg", title=f"Take {n}").status_code, 200)
        self.assertLessEqual(len(list(_generated_root(self.user_id).rglob("*.jpeg"))),
                             ranker_export.MAX_CACHED_RENDERS)

    def test_an_out_of_range_option_is_refused(self):
        self.fill(6)
        for bad in ({"top_x": 0}, {"top_x": 101}, {"columns": 2}, {"columns": 7},
                    {"scale": 0.25}, {"fmt": "gif"}, {"scope": "everything"},
                    {"title": "x" * 81}, {"username": "x" * 81},
                    {"scope": "category"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.export(**bad).status_code, 400)

    def test_a_tier_on_somebody_elses_board_cannot_be_exported(self):
        self.fill(6)
        other = self.ranker_user("neighbour")
        asyncio.run(ranker.create_board(other, uid="theirs"))
        resp = self.client.post("/api/rankings/boards/theirs/export",
                                json={"top_x": 5, "columns": 5})
        self.assertEqual(resp.status_code, 404)

    def test_a_board_with_nothing_tiered_says_so(self):
        asyncio.run(ranker.add_titles(self.user_id, "b1", [show_ref("1")]))
        resp = self.export(top_x=5, columns=5)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nothing tiered", resp.json()["error"])

    def test_a_missing_poster_does_not_stop_an_export(self):
        """Nothing in these fixtures has a cached poster, so every tile in every
        export above is the placeholder — which is the point: the render path
        never goes looking for artwork."""
        self.fill(3)
        with patched(posters, "ensure_poster", _explode("the render path is offline")):
            self.assertEqual(self.export(top_x=3, columns=3, fmt="jpeg").status_code, 200)


class PreviewRouteTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1", name="Top"))
        asyncio.run(ranker.add_titles(
            self.user_id, "b1", [show_ref(str(n)) for n in range(6)]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [{"uid": "t1", "label": "S",
                            "items": [f"show:tmdb:{n}" for n in range(6)]}]}))

    def preview(self, **body):
        return self.client.post("/api/rankings/boards/b1/preview",
                                json={"top_x": 6, "columns": 3, **body})

    def test_a_preview_is_a_small_image_of_the_same_ranking(self):
        resp = self.preview()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/webp")
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.width, ranker_routes.PREVIEW_WIDTH)

    def test_previews_are_exempt_from_the_export_cooldown(self):
        for _ in range(4):
            self.assertEqual(self.preview().status_code, 200)
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM login_attempts WHERE key_type = 'ranker_export'"),
            0)

    def test_a_preview_is_not_kept_on_disk(self):
        self.preview()
        self.assertFalse(list(_generated_root(self.user_id).rglob("*")))


class MarkdownExportTests(RankerTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid="b1", name="Top 2026"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [
            dict(show_ref("1", "Alpha"), network="HBO"),
            dict(show_ref("2", "Beta"), network="HBO"),
        ]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [{"uid": "t1", "label": "S",
                            "items": ["show:tmdb:1", "show:tmdb:2"]}]}))

    def markdown(self, **body):
        return self.client.post("/api/rankings/boards/b1/export/markdown",
                                json={"top_x": 25, "columns": 5, **body})

    def test_the_block_is_the_same_ranking_as_the_image(self):
        body = self.markdown(title="Top Shows").json()
        self.assertIn("# Top Shows", body["markdown"])
        self.assertIn("1. **Alpha**", body["markdown"])
        self.assertIn("2. **Beta**", body["markdown"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["filename"], "Top 2026 Top Shows.md")

    def test_top_x_applies_to_the_text_export_too(self):
        body = self.markdown(top_x=1).json()
        self.assertIn("Alpha", body["markdown"])
        self.assertNotIn("Beta", body["markdown"])

    def test_an_account_with_no_emoji_preferences_gets_plain_text(self):
        """Nothing in this feature may require the other one. An account that
        has never had emoji preferences gets a list, not an error."""
        markdown = self.markdown().json()["markdown"]
        self.assertIn("**Alpha**", markdown)

    def test_the_markdown_export_costs_no_cooldown(self):
        self.markdown()
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM login_attempts WHERE key_type = 'ranker_export'"),
            0)


class BackupTests(RankerTestCase):
    """Backup and restore, and the reason the document is keyed by uid.

    THE ROUND TRIP THAT MATTERS restores into a DIFFERENT database, where every
    autoincrement id has come out different from the one the file was written
    against. An export carrying integer ids would restore a board whose titles
    point at unrelated tiers or at nothing; the uid keying is what survives it.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self.ranker_user()
        self.sign_in_as(self.user_id)
        self.build_source_board()

    def build_source_board(self) -> None:
        """One board with two tiers, an isolated one, and a title left in the
        pool — every kind of linkage the document has to carry."""
        asyncio.run(ranker.create_board(
            self.user_id, uid="b1", name="Top 2026", year=2026, media_scope="mixed"))
        asyncio.run(ranker.add_titles(self.user_id, "b1", [
            dict(show_ref("1", "Alpha"), network="HBO"),
            dict(show_ref("2", "Beta")),
            dict(show_ref("3", "Gamma")),
            {"media": "movie", "match_source": "tmdb", "match_id": "550",
             "tmdb": 550, "title": "Fight Club", "year": 1999, "runtime": 139},
        ]))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [
                {"uid": "tier-s", "label": "S", "rank_priority": 60,
                 "colour": "#FF7F7F",
                 "items": ["show:tmdb:2", "show:tmdb:1"]},
                {"uid": "tier-x", "label": "Rewatches", "rank_priority": 40,
                 "is_isolated": True, "items": ["movie:tmdb:550"]},
            ],
            "pool": ["show:tmdb:3"],
        }))

    def fresh_database(self, decoys: int = 3) -> int:
        """A second, unrelated database with a signed-in ranker account whose
        rows start at DIFFERENT ids — which is the whole point of the exercise.

        The decoy account is built first and holds boards, tiers and titles of
        its own, so no id in the restored data can coincidentally match the id
        the same row had in the database the backup came from.
        """
        RankerTestCase._counter += 1
        db.close_thread_connection()
        db.set_db_path(TMP / f"ranker-restore-{RankerTestCase._counter}.db")
        asyncio.run(db.migrate())
        self.client.cookies.clear()
        self.make_user("admin_user", is_admin=True)
        decoy = self.ranker_user("decoy")
        for n in range(decoys):
            asyncio.run(ranker.create_board(decoy, uid=f"decoy{n}", name=f"Decoy {n}"))
            asyncio.run(ranker.add_titles(decoy, f"decoy{n}", [show_ref(str(900 + n))]))
            asyncio.run(ranker.save_layout(decoy, f"decoy{n}", {
                "version": 1,
                "categories": [{"uid": "d", "label": "D",
                                "items": [f"show:tmdb:{900 + n}"]}]}))
        self.decoy_id = decoy
        user_id = self.ranker_user("restorer")
        self.sign_in_as(user_id)
        return user_id

    def test_the_round_trip_survives_a_database_with_different_ids(self):
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        source = asyncio.run(ranker.fetch_board(self.user_id, "b1"))

        user_id = self.fresh_database()
        self.assertEqual(asyncio.run(ranker.restore_user_data(user_id, doc)), 1)
        restored = asyncio.run(ranker.fetch_board(user_id, "b1"))

        # The ids really did come out different, or this test proves nothing:
        # the decoy account built first has already taken the low ones, so the
        # restored board, its tiers and its titles all landed on numbers the
        # document never mentioned.
        self.assertGreater(self.value("SELECT id FROM tier_boards WHERE uid = 'b1'"), 1)
        self.assertGreater(
            self.value("SELECT MIN(id) FROM tier_categories WHERE uid = 'tier-s'"), 1)

        self.assertEqual(restored["name"], "Top 2026")
        self.assertEqual(restored["year"], 2026)
        self.assertEqual(restored["media_scope"], "mixed")
        self.assertEqual([c["uid"] for c in restored["categories"]],
                         [c["uid"] for c in source["categories"]])
        for was, now in zip(source["categories"], restored["categories"]):
            self.assertEqual(now["label"], was["label"])
            self.assertEqual(now["rank_priority"], was["rank_priority"])
            self.assertEqual(now["is_isolated"], was["is_isolated"])
            self.assertEqual(now["colour"], was["colour"])
            # Order within a tier is the artifact this feature produces.
            self.assertEqual([i["key"] for i in now["items"]],
                             [i["key"] for i in was["items"]])
        self.assertEqual([i["key"] for i in restored["pool"]],
                         [i["key"] for i in source["pool"]])

    def test_a_restored_title_keeps_everything_the_row_held(self):
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        user_id = self.fresh_database()
        asyncio.run(ranker.restore_user_data(user_id, doc))

        board = asyncio.run(ranker.fetch_board(user_id, "b1"))
        movie = board["categories"][1]["items"][0]
        self.assertEqual(movie["media"], "movie")
        self.assertEqual(movie["title"], "Fight Club")
        self.assertEqual(movie["year"], 1999)
        self.assertEqual(movie["runtime"], 139)
        self.assertEqual(movie["tmdb"], 550)
        alpha = next(i for i in board["categories"][0]["items"] if i["title"] == "Alpha")
        self.assertEqual(alpha["network"], "HBO")

    def test_the_restored_board_belongs_to_the_session_user_not_the_file(self):
        """A document is untrusted input. Any owner named in it is ignored, so a
        file cannot write into somebody else's account however it was edited."""
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        user_id = self.fresh_database()
        doc["user_id"] = self.decoy_id
        doc["boards"][0]["user_id"] = self.decoy_id
        doc["boards"][0]["id"] = 1

        asyncio.run(ranker.restore_user_data(user_id, doc))

        owner = self.value("SELECT user_id FROM tier_boards WHERE uid = 'b1'")
        self.assertEqual(owner, user_id)
        # The decoy still has exactly the boards it had.
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM tier_boards WHERE user_id = ?", (self.decoy_id,)),
            3)

    def test_a_restore_replaces_rather_than_merging(self):
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        asyncio.run(ranker.create_board(self.user_id, uid="scratch", name="Scratch"))
        asyncio.run(ranker.add_titles(self.user_id, "scratch", [show_ref("77")]))

        asyncio.run(ranker.restore_user_data(self.user_id, doc))

        boards = asyncio.run(ranker.fetch_boards(self.user_id))
        self.assertEqual([b["uid"] for b in boards], ["b1"])
        # The replaced board's titles went with it rather than being orphaned.
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 4)

    def test_a_malformed_document_writes_nothing_at_all(self):
        """Validation happens before the delete, so a file that turns out to be
        unreadable halfway through does not cost the boards already here."""
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        doc["boards"][0]["items"][-1]["category_uid"] = "tier-that-is-not-here"

        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.restore_user_data(self.user_id, doc))

        board = asyncio.run(ranker.fetch_board(self.user_id, "b1"))
        self.assertEqual(len(board["categories"]), 2)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 4)

    def test_an_unknown_schema_version_is_refused(self):
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.restore_user_data(self.user_id, {"schema": 99, "boards": []}))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"), 1)

    def test_the_document_carries_no_integer_ids(self):
        """The linkage is uid-based by construction, not by luck: an id in the
        file would be a number that means something only in the database it came
        from."""
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        board = doc["boards"][0]
        self.assertNotIn("id", board)
        self.assertNotIn("user_id", board)
        for category in board["categories"]:
            self.assertNotIn("id", category)
            self.assertNotIn("board_id", category)
        for item in board["items"]:
            self.assertNotIn("id", item)
            self.assertNotIn("category_id", item)
        self.assertEqual({i["category_uid"] for i in board["items"]},
                         {"tier-s", "tier-x", None})

    def test_a_backup_of_an_account_with_nothing_is_an_empty_document(self):
        empty = self.ranker_user("empty")
        doc = asyncio.run(ranker.export_user_data(empty))
        self.assertEqual(doc["boards"], [])
        # And restoring it is a legal way to clear the boards you have.
        self.assertEqual(asyncio.run(ranker.restore_user_data(self.user_id, doc)), 0)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"), 0)

    def test_caps_apply_to_a_document_exactly_as_they_do_to_an_edit(self):
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        template = doc["boards"][0]
        doc["boards"] = [dict(template, uid=f"b{n}", year=2026)
                         for n in range(ranker.MAX_BOARDS_PER_YEAR + 1)]
        with self.assertRaises(ranker.ValidationError):
            asyncio.run(ranker.restore_user_data(self.user_id, doc))
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_boards"), 1)

    def test_the_routes_round_trip_the_file(self):
        downloaded = self.client.get("/api/rankings/backup")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertIn("rankings-backup-", downloaded.headers["content-disposition"])
        doc = downloaded.json()

        user_id = self.fresh_database()
        restored = self.client.post("/api/rankings/restore", json=doc)

        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["boards"], 1)
        board = self.client.get("/api/rankings/boards/b1").json()["board"]
        self.assertEqual([i["key"] for i in board["categories"][0]["items"]],
                         ["show:tmdb:2", "show:tmdb:1"])
        self.assertEqual([i["key"] for i in board["pool"]], ["show:tmdb:3"])
        self.assertEqual(board["version"], 0)
        self.assertEqual(
            self.value("SELECT user_id FROM tier_boards WHERE uid = 'b1'"), user_id)

    def test_the_restore_route_refuses_a_broken_file_with_a_readable_message(self):
        resp = self.client.post("/api/rankings/restore", json={"schema": 4, "boards": []})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("backup", resp.json()["error"].lower())

    def test_both_routes_need_the_grant(self):
        self.sign_in_as(self.make_user("plain"))
        self.assertEqual(self.client.get("/api/rankings/backup").status_code, 403)
        self.assertEqual(
            self.client.post("/api/rankings/restore", json={"schema": 1, "boards": []}).status_code,
            403)


def _generated_root(user_id: int) -> Path:
    """Where this account's finished renders are kept, whatever year they were
    filed under."""
    return user_images.generated_dir(user_id, 2000).parent


if __name__ == "__main__":
    unittest.main()
