"""Unit tests for the admin screen (app/auth/admin_routes.py) and its business logic
in app/auth/admin.py.

Two things carry the most weight here and get the most direct coverage:

  - Deleting an account must leave zero orphan rows in EVERY table that
    references `users`, discovered from the schema itself (PRAGMA
    foreign_key_list) rather than a hand-maintained list, so a table added
    later is caught automatically — with the one deliberate exception,
    invites.created_by, which is ON DELETE SET NULL so that deleting the admin
    who issued an invite doesn't revoke it out from under someone mid-
    redemption.
  - The last-admin guard on demote/disable/delete, and the "can't delete
    yourself" guard, since either one failing silently would be how an
    instance actually gets locked out.
"""
from __future__ import annotations

import asyncio
import re
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from markupsafe import escape

from app import auth, db
from app.main import app
from tests.support import AppTestCase


class AdminTestCase(AppTestCase):
    # -- helpers --------------------------------------------------------

    def make_admin(self, username="admin1", password="hunter2hunter2") -> int:
        resp = self.client.post("/onboarding", json={
            "username": username, "password": password, "password_confirm": password,
        })
        self.assertEqual(resp.status_code, 204, resp.text)
        return asyncio.run(auth.find_user_by_username(username))["id"]

    def make_user(self, username="member", *, calendar_approved=False,
                  distrakt_approved=False, is_admin=False) -> int:
        return asyncio.run(auth.create_user(
            username=username, password="memberpass1", calendar_approved=calendar_approved,
            distrakt_approved=distrakt_approved, is_admin=is_admin,
        ))

    def login_as(self, client: TestClient, username: str, password: str = "hunter2hunter2"):
        resp = client.post("/login", json={"username": username, "password": password})
        self.assertEqual(resp.status_code, 204, resp.text)

    def assertDrawnIn(self, resp, section: str, message: str):
        """An admin action's answer: the whole body redrawn at 200, with the
        sentence in the section the action came from."""
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('id="adminShell"', resp.text)
        self.assertIn(str(escape(message)), resp.text)
        # A SUCCESS IS A TOAST, out of band into the page's host; only a refusal
        # is drawn in its section.
        toast = resp.text.split('id="toastHost" hx-swap-oob="beforeend"', 1)
        if len(toast) == 2 and str(escape(message)) in toast[1]:
            return
        heading = {"users": "Accounts", "invites": "Invites", "retired": "Retired identifiers"}[section]
        after_heading = resp.text.split(f"<h2>{heading}</h2>", 1)[1]
        self.assertLess(after_heading.index(str(escape(message))), after_heading.index("<table"))


# ---------------------------------------------------------------------------
# delete: zero orphan rows, discovered from the schema
# ---------------------------------------------------------------------------

class DeleteOrphanTests(AdminTestCase):
    def _foreign_keys_into_users(self) -> list[tuple[str, str, str]]:
        """(table, column, on_delete) for every FK referencing users(id),
        discovered live from the schema rather than hardcoded — so a table a
        added later is caught automatically."""
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

        resp = self.client.post(f"/admin/users/{admin_id}/admin", json={"value": "0"})
        self.assertDrawnIn(resp, "users", "The last remaining administrator can't be demoted.")

        resp = self.client.post(f"/admin/users/{admin_id}/disabled", json={"value": "1"})
        self.assertDrawnIn(resp, "users", "The last remaining administrator can't be disabled.")

        resp = self.client.post(f"/admin/users/{admin_id}/delete",
                                json={"confirm_username": "admin1"})
        self.assertDrawnIn(resp, "users", "You can't delete your own account.")
        row = asyncio.run(auth.get_user(admin_id))
        self.assertTrue(row["is_admin"])
        self.assertFalse(row["is_disabled"])

    def test_the_page_draws_no_delete_for_yourself(self):
        admin_id = self.make_admin()
        self.make_user("someone-else")
        page = self.client.get("/admin").text
        self.assertNotIn(f'hx-post="/admin/users/{admin_id}/delete"', page)
        self.assertIn('/delete"', page)


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
        page = self.client.get("/admin").text
        # Asked first, through the markup confirmation, and the sentence names it.
        self.assertIn('Release &#34;reclaimable&#34;?', page)
        resp = self.client.post("/admin/retired/release",
                                json={"kind": "username", "value": "reclaimable"})
        self.assertDrawnIn(resp, "retired", "Released reclaimable.")
        self.assertIn("Nothing retired.", resp.text)

        self.assertFalse(asyncio.run(auth.identifier_is_retired("username", "reclaimable")))
        self.assertIsNone(asyncio.run(auth.username_availability_error("reclaimable")))

    def test_tokens_are_never_releasable(self):
        asyncio.run(db.execute(
            "INSERT INTO retired_identifiers (kind, value, retired_at) VALUES ('token', 'abc123', ?)",
            (db.now(),),
        ))
        with self.assertRaises(ValueError):
            asyncio.run(auth.release_retired_identifier("token", "abc123"))

    def test_releasing_an_unknown_identifier_is_drawn_as_a_refusal(self):
        self.make_admin()
        self.login_as(self.client, "admin1")
        resp = self.client.post("/admin/retired/release",
                                json={"kind": "username", "value": "never-existed"})
        self.assertDrawnIn(resp, "retired", "That identifier isn't retired.")
        resp = self.client.post("/admin/retired/release", json={"kind": "token", "value": "abc"})
        self.assertDrawnIn(resp, "retired", "Only usernames and slugs can be released.")


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

    def test_the_form_issues_lists_revokes_and_shows_redemptions(self):
        admin_id = self.make_admin()
        self.login_as(self.client, "admin1")

        # As the page's form posts: blank numbers, and each box after its "0".
        resp = self.client.post("/admin/invites", json={
            "label": "friends", "max_uses": "", "expires_in_hours": "",
            "grants_calendar_on_accept": ["0", "1"], "grants_ranker_on_accept": "0"})
        self.assertDrawnIn(resp, "invites", "Invite issued. Copy its link below.")
        row = asyncio.run(auth.list_invites())[0]
        invite_id, token = row["id"], row["token"]
        self.assertEqual(row["label"], "friends")
        self.assertIsNone(row["max_uses"])
        self.assertIsNone(row["expires_at"])
        self.assertTrue(row["grants_calendar_on_accept"])
        self.assertFalse(row["grants_ranker_on_accept"])
        # The copyable link is spelled by the server.
        self.assertIn(f'value="https://testserver/register?invite={token}"', resp.text)

        def _redeem(conn):
            auth.redeem_invite(conn, invite=asyncio.run(auth.find_invite_by_token(token)),
                               user_id=admin_id)
        asyncio.run(db.transaction(_redeem))

        redemptions = self.client.get(f"/admin/fragments/invites/{invite_id}/redemptions")
        self.assertEqual(redemptions.status_code, 200)
        self.assertEqual(redemptions.text.count("<li>"), 1)
        self.assertIn("admin1", redemptions.text)

        revoke = self.client.post(f"/admin/invites/{invite_id}/revoke", json={})
        self.assertDrawnIn(revoke, "invites", "Invite revoked.")
        self.assertTrue(asyncio.run(auth.find_invite_by_token(token))["revoked"])

        again = self.client.post(f"/admin/invites/{invite_id}/revoke", json={})
        self.assertDrawnIn(again, "invites", "Invite revoked.")  # idempotent, not an error

        missing = self.client.post("/admin/invites/999999/revoke", json={})
        self.assertDrawnIn(missing, "invites", "No such invite.")

    def test_an_invite_number_that_is_not_one_is_drawn_and_issues_nothing(self):
        self.make_admin()
        self.login_as(self.client, "admin1")
        for field, value, sentence in (("max_uses", "lots", "Max uses must be a number."),
                                       ("max_uses", "0", "Max uses must be more than nought."),
                                       ("expires_in_hours", "-2",
                                        "Expires (hours) must be more than nought.")):
            with self.subTest(field=field, value=value):
                resp = self.client.post("/admin/invites", json={field: value})
                self.assertDrawnIn(resp, "invites", sentence)
        self.assertEqual(asyncio.run(auth.list_invites()), [])

    def test_a_limited_invite_keeps_its_limits(self):
        self.make_admin()
        self.login_as(self.client, "admin1")
        before = db.now()
        self.client.post("/admin/invites", json={"max_uses": "3", "expires_in_hours": "1.5"})
        row = asyncio.run(auth.list_invites())[0]
        self.assertEqual(row["max_uses"], 3)
        self.assertAlmostEqual(row["expires_at"] - before, 5400, delta=5)


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
        resp = self.client.post(f"/admin/users/{victim_id}/password", json={"password": "brand-new-pass1"})
        self.assertDrawnIn(resp, "users", "Every session on that account has been signed out.")
        self.assertRegex(resp.text, r'id="temporaryPassword"[^>]*value="brand-new-pass1"')
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (victim_id,))), 0,
        )
        user = asyncio.run(auth.get_user(victim_id))
        self.assertIsNotNone(user["password_changed_at"])

        verified = asyncio.run(auth.verify_password(user["password_hash"], "brand-new-pass1"))
        self.assertTrue(verified.ok)

    def test_a_generated_password_is_drawn_once(self):
        self.make_admin()
        victim_id = self.make_user("autogen")
        self.login_as(self.client, "admin1")
        resp = self.client.post(f"/admin/users/{victim_id}/password", json={"password": ""})
        self.assertEqual(resp.status_code, 200, resp.text)
        password = re.search(r'id="temporaryPassword"[^>]*value="([^"]+)"', resp.text).group(1)
        self.assertGreaterEqual(len(password), auth.MIN_PASSWORD_LENGTH)
        verified = asyncio.run(auth.verify_password(
            asyncio.run(auth.get_user(victim_id))["password_hash"], password))
        self.assertTrue(verified.ok)
        # Nothing else ever draws it again.
        self.assertNotIn(password, self.client.get("/admin").text)

    def test_a_short_password_is_refused_and_changes_nothing(self):
        self.make_admin()
        victim_id = self.make_user("keepme", calendar_approved=True)
        self.login_as(self.client, "admin1")
        before = asyncio.run(auth.get_user(victim_id))["password_hash"]
        resp = self.client.post(f"/admin/users/{victim_id}/password", json={"password": "short"})
        self.assertDrawnIn(resp, "users",
                           f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
        self.assertNotIn('id="temporaryPassword"', resp.text)
        self.assertEqual(asyncio.run(auth.get_user(victim_id))["password_hash"], before)

    def test_reset_can_also_set_a_username_for_an_oauth_only_account(self):
        admin_id = self.make_admin()
        oauth_only_id = asyncio.run(auth.create_user(username=None, password=None))

        def _link(conn):
            auth.insert_linked_identity(
                conn, user_id=oauth_only_id, provider="trakt", provider_user_id="777",
            )
        asyncio.run(db.transaction(_link))

        self.login_as(self.client, "admin1")
        # The form offers the username field for exactly this account.
        page = self.client.get("/admin").text
        form = page.split(f'hx-post="/admin/users/{oauth_only_id}/password"', 1)[1].split("</form>")[0]
        self.assertIn('name="username"', form)
        resp = self.client.post(
            f"/admin/users/{oauth_only_id}/password",
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
            f"/admin/users/{oauth_only_id}/identities/unlink", json={"provider": "trakt"},
        )
        self.assertDrawnIn(resp, "users", "This is that account's only way to sign in.")
        # The answer asks the second question, and pressing it is what sends force.
        self.assertIn("Unlink anyway", resp.text)
        self.assertIn(f'hx-post="/admin/users/{oauth_only_id}/identities/unlink"', resp.text)
        self.assertIn('&#34;force&#34;: &#34;1&#34;', resp.text.replace('"force": "1"', '&#34;force&#34;: &#34;1&#34;'))
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
            f"/admin/users/{oauth_only_id}/identities/unlink",
            json={"provider": "trakt", "force": "1"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("Unlink anyway", resp.text)
        self.assertEqual(len(asyncio.run(auth.list_identities(oauth_only_id))), 0)


    def test_a_simkl_link_can_be_unlinked_and_is_revoked(self):
        """Every sign-in service, from app/auth/revocation.py's one table. This
        route used to accept two of the three, so an administrator could not
        remove somebody's Simkl link at all."""
        self.make_admin()
        member_id = asyncio.run(auth.create_user(username="m1", password="a-long-password-1"))

        def _link(conn):
            auth.insert_linked_identity(
                conn, user_id=member_id, provider="simkl", provider_user_id="654",
            )
        asyncio.run(db.transaction(_link))

        self.login_as(self.client, "admin1")
        with patch("app.auth.simkl_routes.stored_access_token",
                   AsyncMock(return_value="the-token")),              patch("app.auth.simkl_routes.revoke_token_value",
                   AsyncMock(return_value=None)) as revoke:
            resp = self.client.post(f"/admin/users/{member_id}/identities/unlink",
                                    json={"provider": "simkl"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(asyncio.run(auth.list_identities(member_id)), [])
        revoke.assert_awaited_once_with("the-token")
        # Said as a toast, as a success is everywhere else in the app.
        self.assertRegex(resp.text, r'id="toastHost" hx-swap-oob="beforeend">\s*<div class="toast ok[^>]*>'
                                    r'Unlinked Simkl\.')

    def test_a_name_that_is_not_a_sign_in_service_is_refused(self):
        self.make_admin()
        self.login_as(self.client, "admin1")
        resp = self.client.post("/admin/users/1/identities/unlink", json={"provider": "tmdb"})
        self.assertDrawnIn(resp, "users", "Unknown provider.")


# ---------------------------------------------------------------------------
# approval and account listing
# ---------------------------------------------------------------------------

class ApprovalAndListingTests(AdminTestCase):
    def test_approve_calendar_and_distrakt_independently(self):
        self.make_admin()
        member_id = self.make_user("member1")
        self.login_as(self.client, "admin1")

        resp = self.client.post(f"/admin/users/{member_id}/approval",
                                json={"grant": "calendar", "value": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Revoke calendar", resp.text)
        user = asyncio.run(auth.get_user(member_id))
        self.assertTrue(user["calendar_approved"])
        self.assertFalse(user["distrakt_approved"])

        resp = self.client.post(f"/admin/users/{member_id}/approval",
                                json={"grant": "distrakt", "value": "1"})
        self.assertEqual(resp.status_code, 200)
        user = asyncio.run(auth.get_user(member_id))
        self.assertTrue(user["distrakt_approved"])

        resp = self.client.post(f"/admin/users/{member_id}/approval",
                                json={"grant": "is_admin", "value": "1"})
        self.assertDrawnIn(resp, "users", "There is no such approval.")
        self.assertFalse(asyncio.run(auth.get_user(member_id))["is_admin"])

    def test_times_are_spelled_by_the_server_in_the_administrators_zone(self):
        admin_id = self.make_admin()
        asyncio.run(db.execute("UPDATE users SET timezone = 'Asia/Tokyo', created_at = 0 "
                               "WHERE id = ?", (admin_id,)))
        member = self.make_user("dated")
        asyncio.run(db.execute("UPDATE users SET created_at = 1767225600 WHERE id = ?", (member,)))
        page = self.client.get("/admin").text
        # 2026-01-01 00:00 UTC is 09:00 in Tokyo.
        self.assertIn('title="2026-01-01 09:00:00 JST">Jan 1 2026, 09:00</time>', page)
        self.assertNotIn("data-ts", page)
        self.assertNotIn("DateTimeFormat", page)

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
        target = self.make_user("plain", calendar_approved=True)
        self.login_as(self.client, "plain", password="memberpass1")
        # The two reads left. The page is where the accounts, the invites and the
        # retired identifiers are drawn now — each had a JSON read of its own under
        # /api/admin, which nothing called and which is gone.
        for method, path in [
            ("get", "/admin"),
            ("get", "/admin/fragments/invites/1/redemptions"),
        ]:
            resp = getattr(self.client, method)(path)
            self.assertIn(resp.status_code, (401, 403), f"{method} {path}")
        for path in ADMIN_POSTS(target):
            with self.subTest(path=path):
                resp = self.client.post(path, json={"value": "1", "grant": "calendar",
                                                    "provider": "trakt", "kind": "username"})
                self.assertEqual(resp.status_code, 403)
        self.assertFalse(asyncio.run(auth.get_user(target))["is_admin"])


def ADMIN_POSTS(user_id: int) -> tuple[str, ...]:
    """Every write the admin page makes, for the refusals every one of them owes."""
    base = f"/admin/users/{user_id}"
    return (f"{base}/approval", f"{base}/admin", f"{base}/disabled", f"{base}/password",
            f"{base}/wipe", f"{base}/delete", f"{base}/sessions/revoke-all",
            f"{base}/identities/unlink", "/admin/invites", "/admin/invites/1/revoke",
            "/admin/retired/release")


class AdminRefusalTests(AdminTestCase):
    """Each write refused once for each way it can be: a body that is not JSON, a
    body that is not an object, and nobody signed in. The fourth — a caller who is
    not an administrator — is ApprovalAndListingTests' table above."""

    def setUp(self):
        super().setUp()
        self.make_admin()
        self.target = self.make_user("target")


    def test_a_body_that_is_not_an_object_is_refused(self):
        for path in ADMIN_POSTS(self.target):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=["1"]).status_code, 400)
        self.assertFalse(asyncio.run(auth.get_user(self.target))["is_admin"])

    def test_no_session_is_refused(self):
        self.client.cookies.clear()
        for path in ADMIN_POSTS(self.target):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 401)

    def test_every_control_that_writes_carries_the_encoder_and_asks_where_it_should(self):
        self.make_user("second-admin", is_admin=True)
        page = self.client.get("/admin").text
        for element in re.findall(r"<(?:form|button)\b[^>]*hx-post[^>]*>", page):
            with self.subTest(element=element[:80]):
                self.assertIn("hx-json-enc", element)
        # The destructive ones are behind the markup confirmation.
        for question in ("Remove administrator access from this account?",
                         "Disable this account?", "Log this account out everywhere?",
                         "Wipe this account", "This permanently deletes the account."):
            with self.subTest(question=question):
                self.assertIn(question, page)
        self.assertNotIn("confirmInline", page)


if __name__ == "__main__":
    unittest.main()
