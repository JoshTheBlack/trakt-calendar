"""Unit tests for the admin screen (app/admin_routes.py) and its business logic
in app/auth.py.

Two things carry the most weight here and get the most direct coverage:

  - Deleting an account must leave zero orphan rows in EVERY table that
    references `users`, discovered from the schema itself (PRAGMA
    foreign_key_list) rather than a hand-maintained list, so a table a later
    chat adds is caught automatically — with the one deliberate exception,
    invites.created_by, which is ON DELETE SET NULL so that deleting the admin
    who issued an invite doesn't revoke it out from under someone mid-
    redemption.
  - The last-admin guard on demote/disable/delete, and the "can't delete
    yourself" guard, since either one failing silently would be how an
    instance actually gets locked out.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_admin -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-admin-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])


class AdminTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        AdminTestCase._counter += 1
        db.set_db_path(TMP / f"admin-{AdminTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        # See tests/test_auth_routes.py: https + a default Origin header, both
        # required for the session cookie and the CSRF middleware respectively.
        self.client = TestClient(app, base_url="https://testserver",
                                 headers={"Origin": "https://testserver"})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    # -- helpers --------------------------------------------------------

    def make_admin(self, username="admin1", password="hunter2hunter2") -> int:
        resp = self.client.post("/onboarding", json={
            "username": username, "password": password, "password_confirm": password,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return asyncio.run(auth.find_user_by_username(username))["id"]

    def make_user(self, username="member", *, calendar_approved=False,
                  distrakt_approved=False, is_admin=False) -> int:
        return asyncio.run(auth.create_user(
            username=username, password="memberpass1", calendar_approved=calendar_approved,
            distrakt_approved=distrakt_approved, is_admin=is_admin,
        ))

    def login_as(self, client: TestClient, username: str, password: str = "hunter2hunter2"):
        resp = client.post("/login", json={"username": username, "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# delete: zero orphan rows, discovered from the schema
# ---------------------------------------------------------------------------

class DeleteOrphanTests(AdminTestCase):
    def _foreign_keys_into_users(self) -> list[tuple[str, str, str]]:
        """(table, column, on_delete) for every FK referencing users(id),
        discovered live from the schema rather than hardcoded — so a table a
        later chat adds is caught automatically."""
        tables = [r["name"] for r in asyncio.run(db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT IN ('schema_version', 'users')"
        ))]
        refs = []
        for table in tables:
            for row in asyncio.run(db.fetch_all(f"PRAGMA foreign_key_list({table})")):
                if row["table"] == "users":
                    refs.append((table, row["from"], (row["on_delete"] or "").upper()))
        return refs

    def test_deleting_a_user_leaves_zero_orphan_rows_everywhere(self):
        admin_id = self.make_admin()
        victim_id = self.make_user("victim", calendar_approved=True)

        # Populate every FK-into-users table this schema currently has.
        asyncio.run(auth.create_session(victim_id))

        def _link(conn):
            auth.insert_linked_identity(
                conn, user_id=victim_id, provider="trakt", provider_user_id="999",
            )
        asyncio.run(db.transaction(_link))

        asyncio.run(auth.create_invite(created_by=victim_id, label="victim's invite"))

        other_invite = asyncio.run(auth.create_invite(created_by=admin_id))
        invite_row = asyncio.run(auth.find_invite_by_token(other_invite["token"]))

        def _redeem(conn):
            auth.redeem_invite(conn, invite=invite_row, user_id=victim_id)
        asyncio.run(db.transaction(_redeem))

        refs = self._foreign_keys_into_users()
        self.assertTrue(refs, "expected at least one FK into users() to exist")

        asyncio.run(auth.delete_user(victim_id, actor_user_id=admin_id))

        self.assertIsNone(asyncio.run(auth.get_user(victim_id)))

        for table, column, on_delete in refs:
            remaining = asyncio.run(db.fetch_value(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (victim_id,), default=0,
            ))
            if table == "invites" and column == "created_by":
                # SET NULL, deliberately: deleting the issuing admin must not
                # revoke invites someone else is mid-redemption on. This is the
                # one carve-out — the row survives with created_by cleared,
                # not gone.
                self.assertEqual(remaining, 0)
                self.assertEqual(on_delete, "SET NULL")
                still_there = asyncio.run(db.fetch_value(
                    "SELECT COUNT(*) FROM invites WHERE label = 'victim''s invite' "
                    "AND created_by IS NULL"
                ))
                self.assertEqual(still_there, 1)
            else:
                self.assertEqual(
                    remaining, 0, f"orphan row(s) left in {table}.{column} (on_delete={on_delete})",
                )

    def test_delete_records_the_username_as_retired(self):
        admin_id = self.make_admin()
        victim_id = self.make_user("gone-user")
        asyncio.run(auth.delete_user(victim_id, actor_user_id=admin_id))
        self.assertTrue(asyncio.run(auth.identifier_is_retired("username", "gone-user")))


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

class GuardTests(AdminTestCase):
    def test_cannot_demote_the_last_admin(self):
        admin_id = self.make_admin()
        with self.assertRaises(auth.LastAdmin):
            asyncio.run(auth.set_admin(admin_id, False))
        self.assertTrue(asyncio.run(auth.get_user(admin_id))["is_admin"])

    def test_cannot_disable_the_last_admin(self):
        admin_id = self.make_admin()
        with self.assertRaises(auth.LastAdmin):
            asyncio.run(auth.set_disabled(admin_id, True))
        self.assertFalse(asyncio.run(auth.get_user(admin_id))["is_disabled"])

    def test_cannot_delete_the_last_admin(self):
        admin_id = self.make_admin()
        second_admin = self.make_user("second-admin", is_admin=True)
        # With two admins, demoting one and then deleting it is fine...
        asyncio.run(auth.set_admin(second_admin, False))
        # ...but now there is only one admin left again, so it can't go.
        with self.assertRaises(auth.LastAdmin):
            asyncio.run(auth.delete_user(admin_id, actor_user_id=second_admin))

    def test_demoting_when_not_the_last_admin_succeeds(self):
        admin_id = self.make_admin()
        second_admin = self.make_user("second-admin", is_admin=True)
        asyncio.run(auth.set_admin(second_admin, False))
        self.assertFalse(asyncio.run(auth.get_user(second_admin))["is_admin"])

    def test_cannot_delete_yourself(self):
        admin_id = self.make_admin()
        with self.assertRaises(auth.CannotDeleteSelf):
            asyncio.run(auth.delete_user(admin_id, actor_user_id=admin_id))

    def test_route_level_last_admin_and_self_delete_guards(self):
        self.make_admin()
        self.login_as(self.client, "admin1")
        admin_id = asyncio.run(auth.find_user_by_username("admin1"))["id"]

        resp = self.client.post(f"/api/admin/users/{admin_id}/admin", json={"is_admin": False})
        self.assertEqual(resp.status_code, 409)

        resp = self.client.post(f"/api/admin/users/{admin_id}/disabled", json={"disabled": True})
        self.assertEqual(resp.status_code, 409)

        resp = self.client.post(f"/api/admin/users/{admin_id}/delete",
                                json={"confirm_username": "admin1"})
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["ok"])


# ---------------------------------------------------------------------------
# retired identifiers
# ---------------------------------------------------------------------------

class RetiredIdentifierTests(AdminTestCase):
    def test_retired_username_blocks_reuse_until_released(self):
        admin_id = self.make_admin()
        victim_id = self.make_user("reclaimable")
        asyncio.run(auth.delete_user(victim_id, actor_user_id=admin_id))

        self.assertIsNotNone(asyncio.run(auth.username_availability_error("reclaimable")))

        self.login_as(self.client, "admin1")
        resp = self.client.post("/api/admin/retired/release",
                                json={"kind": "username", "value": "reclaimable"})
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertFalse(asyncio.run(auth.identifier_is_retired("username", "reclaimable")))
        self.assertIsNone(asyncio.run(auth.username_availability_error("reclaimable")))

    def test_tokens_are_never_releasable(self):
        asyncio.run(db.execute(
            "INSERT INTO retired_identifiers (kind, value, retired_at) VALUES ('token', 'abc123', ?)",
            (db.now(),),
        ))
        with self.assertRaises(ValueError):
            asyncio.run(auth.release_retired_identifier("token", "abc123"))

    def test_releasing_an_unknown_identifier_via_the_route_is_404(self):
        self.make_admin()
        self.login_as(self.client, "admin1")
        resp = self.client.post("/api/admin/retired/release",
                                json={"kind": "username", "value": "never-existed"})
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# wipe vs delete
# ---------------------------------------------------------------------------

class WipeDataTests(AdminTestCase):
    def test_wipe_keeps_the_account_and_identities_but_disables_and_signs_out(self):
        admin_id = self.make_admin()
        victim_id = self.make_user("wipeme", calendar_approved=True, distrakt_approved=True)

        def _link(conn):
            auth.insert_linked_identity(
                conn, user_id=victim_id, provider="trakt", provider_user_id="555",
            )
        asyncio.run(db.transaction(_link))
        asyncio.run(auth.create_session(victim_id))

        asyncio.run(auth.wipe_user_data(victim_id))

        user = asyncio.run(auth.get_user(victim_id))
        self.assertIsNotNone(user)  # the account itself survives
        self.assertEqual(user["username"], "wipeme")  # username/slug survives
        self.assertTrue(user["is_disabled"])
        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (victim_id,))), 0,
        )
        self.assertEqual(len(asyncio.run(auth.list_identities(victim_id))), 1)  # identity survives

    def test_wipe_is_reversible_by_re_enabling(self):
        admin_id = self.make_admin()
        victim_id = self.make_user("comeback")
        asyncio.run(auth.wipe_user_data(victim_id))
        asyncio.run(auth.set_disabled(victim_id, False))
        self.assertFalse(asyncio.run(auth.get_user(victim_id))["is_disabled"])
        # Nothing about the identifier was retired — wipe never touches
        # retired_identifiers, unlike delete.
        self.assertFalse(asyncio.run(auth.identifier_is_retired("username", "comeback")))


# ---------------------------------------------------------------------------
# invites
# ---------------------------------------------------------------------------

class InviteAdminTests(AdminTestCase):
    def test_default_invite_grants_calendar_but_never_distrakt(self):
        admin_id = self.make_admin()
        invite = asyncio.run(auth.create_invite(created_by=admin_id))
        row = asyncio.run(auth.find_invite_by_token(invite["token"]))
        self.assertTrue(row["grants_calendar_on_accept"])
        # There is deliberately no distrakt-grant column on the invite at all.
        self.assertNotIn("grants_distrakt_on_accept", row.keys())

    def test_expiry_quota_and_revocation_each_make_an_invite_unusable(self):
        admin_id = self.make_admin()

        expired = asyncio.run(auth.create_invite(created_by=admin_id, expires_at=db.now() - 10))
        self.assertFalse(auth.invite_is_usable(asyncio.run(auth.find_invite_by_token(expired["token"]))))

        exhausted = asyncio.run(auth.create_invite(created_by=admin_id, max_uses=1))
        row = asyncio.run(auth.find_invite_by_token(exhausted["token"]))
        self.assertTrue(auth.invite_is_usable(row))

        def _use(conn):
            auth.redeem_invite(conn, invite=row, user_id=admin_id)
        asyncio.run(db.transaction(_use))
        exhausted_row = asyncio.run(auth.find_invite_by_token(exhausted["token"]))
        self.assertFalse(auth.invite_is_usable(exhausted_row))

        revocable = asyncio.run(auth.create_invite(created_by=admin_id))
        self.assertTrue(asyncio.run(auth.revoke_invite(revocable["id"])))
        revoked_row = asyncio.run(auth.find_invite_by_token(revocable["token"]))
        self.assertFalse(auth.invite_is_usable(revoked_row))

    def test_route_lists_and_revokes_and_shows_redemptions(self):
        admin_id = self.make_admin()
        self.login_as(self.client, "admin1")

        resp = self.client.post("/api/admin/invites", json={"label": "friends"})
        self.assertEqual(resp.status_code, 200, resp.text)
        invite_id, token = resp.json()["id"], resp.json()["token"]

        listing = self.client.get("/api/admin/invites")
        self.assertEqual(listing.status_code, 200)
        labels = [i["label"] for i in listing.json()["invites"]]
        self.assertIn("friends", labels)

        row = asyncio.run(auth.find_invite_by_token(token))

        def _redeem(conn):
            auth.redeem_invite(conn, invite=row, user_id=admin_id)
        asyncio.run(db.transaction(_redeem))

        redemptions = self.client.get(f"/api/admin/invites/{invite_id}/redemptions")
        self.assertEqual(redemptions.status_code, 200)
        self.assertEqual(len(redemptions.json()["redemptions"]), 1)
        self.assertEqual(redemptions.json()["redemptions"][0]["username"], "admin1")

        revoke = self.client.post(f"/api/admin/invites/{invite_id}/revoke", json={})
        self.assertEqual(revoke.status_code, 200)
        self.assertTrue(asyncio.run(auth.find_invite_by_token(token))["revoked"])

        again = self.client.post(f"/api/admin/invites/{invite_id}/revoke", json={})
        self.assertEqual(again.status_code, 200)  # idempotent, not an error

        missing = self.client.post("/api/admin/invites/999999/revoke", json={})
        self.assertEqual(missing.status_code, 404)


# ---------------------------------------------------------------------------
# password reset
# ---------------------------------------------------------------------------

class PasswordResetTests(AdminTestCase):
    def test_reset_revokes_every_session_and_stamps_the_change(self):
        admin_id = self.make_admin()
        victim_id = self.make_user("resetme", calendar_approved=True)
        asyncio.run(auth.create_session(victim_id))
        asyncio.run(auth.create_session(victim_id))
        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (victim_id,))), 2,
        )

        self.login_as(self.client, "admin1")
        resp = self.client.post(f"/api/admin/users/{victim_id}/password", json={"password": "brand-new-pass1"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["password"], "brand-new-pass1")
        self.assertFalse(resp.json()["generated"])

        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (victim_id,))), 0,
        )
        user = asyncio.run(auth.get_user(victim_id))
        self.assertIsNotNone(user["password_changed_at"])

        verified = asyncio.run(auth.verify_password(user["password_hash"], "brand-new-pass1"))
        self.assertTrue(verified.ok)

    def test_a_generated_password_is_returned_once(self):
        self.make_admin()
        victim_id = self.make_user("autogen")
        self.login_as(self.client, "admin1")
        resp = self.client.post(f"/api/admin/users/{victim_id}/password", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["generated"])
        self.assertGreaterEqual(len(resp.json()["password"]), auth.MIN_PASSWORD_LENGTH)

    def test_reset_can_also_set_a_username_for_an_oauth_only_account(self):
        admin_id = self.make_admin()
        oauth_only_id = asyncio.run(auth.create_user(username=None, password=None))

        def _link(conn):
            auth.insert_linked_identity(
                conn, user_id=oauth_only_id, provider="trakt", provider_user_id="777",
            )
        asyncio.run(db.transaction(_link))

        self.login_as(self.client, "admin1")
        resp = self.client.post(
            f"/api/admin/users/{oauth_only_id}/password",
            json={"username": "newname", "password": "a-real-password1"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        user = asyncio.run(auth.get_user(oauth_only_id))
        self.assertEqual(user["username"], "newname")
        self.assertIsNotNone(user["password_hash"])


# ---------------------------------------------------------------------------
# identity unlink with the force option
# ---------------------------------------------------------------------------

class AdminUnlinkTests(AdminTestCase):
    def test_unlinking_the_last_login_method_is_refused_without_force(self):
        admin_id = self.make_admin()
        oauth_only_id = asyncio.run(auth.create_user(username=None, password=None))

        def _link(conn):
            auth.insert_linked_identity(
                conn, user_id=oauth_only_id, provider="trakt", provider_user_id="321",
            )
        asyncio.run(db.transaction(_link))

        self.login_as(self.client, "admin1")
        resp = self.client.post(
            f"/api/admin/users/{oauth_only_id}/identities/unlink", json={"provider": "trakt"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["orphan_warning"])
        self.assertEqual(len(asyncio.run(auth.list_identities(oauth_only_id))), 1)

    def test_forcing_the_unlink_orphans_the_account_deliberately(self):
        admin_id = self.make_admin()
        oauth_only_id = asyncio.run(auth.create_user(username=None, password=None))

        def _link(conn):
            auth.insert_linked_identity(
                conn, user_id=oauth_only_id, provider="trakt", provider_user_id="321",
            )
        asyncio.run(db.transaction(_link))

        self.login_as(self.client, "admin1")
        resp = self.client.post(
            f"/api/admin/users/{oauth_only_id}/identities/unlink",
            json={"provider": "trakt", "force": True},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(asyncio.run(auth.list_identities(oauth_only_id))), 0)


# ---------------------------------------------------------------------------
# approval and account listing
# ---------------------------------------------------------------------------

class ApprovalAndListingTests(AdminTestCase):
    def test_approve_calendar_and_distrakt_independently(self):
        self.make_admin()
        member_id = self.make_user("member1")
        self.login_as(self.client, "admin1")

        resp = self.client.post(f"/api/admin/users/{member_id}/approval", json={"calendar": True})
        self.assertEqual(resp.status_code, 200)
        user = asyncio.run(auth.get_user(member_id))
        self.assertTrue(user["calendar_approved"])
        self.assertFalse(user["distrakt_approved"])

        resp = self.client.post(f"/api/admin/users/{member_id}/approval", json={"distrakt": True})
        self.assertEqual(resp.status_code, 200)
        user = asyncio.run(auth.get_user(member_id))
        self.assertTrue(user["distrakt_approved"])

    def test_account_list_shows_display_name_providers_and_activity(self):
        self.make_admin()
        member_id = self.make_user("listed-user", calendar_approved=True)
        asyncio.run(auth.create_session(member_id))

        overview = asyncio.run(auth.list_users_overview())
        row = next(u for u in overview if u["id"] == member_id)
        self.assertEqual(row["display_name"], "listed-user")
        self.assertTrue(row["calendar_approved"])
        self.assertIsNotNone(row["last_session_at"])

    def test_non_admin_gets_403_from_every_admin_route(self):
        self.make_admin()
        self.make_user("plain", calendar_approved=True)
        self.login_as(self.client, "plain", password="memberpass1")
        for method, path in [
            ("get", "/admin"), ("get", "/api/admin/users"),
            ("get", "/api/admin/invites"), ("get", "/api/admin/retired"),
        ]:
            resp = getattr(self.client, method)(path)
            self.assertIn(resp.status_code, (401, 403), f"{method} {path}")


if __name__ == "__main__":
    unittest.main()
