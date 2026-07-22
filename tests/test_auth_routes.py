"""Unit tests for first-run setup and sign-in (app/auth_routes).

The UI these routes render is placeholder and will be replaced, but three things
they do are not: the first-run race guard, the upgrade path for an instance that
already has data (adopting the Trakt token from settings.json, seeding view
preferences), and the JSON-only body rule. Those are what this file pins down.

No network — the Trakt account lookup is mocked. TRAKT_DATA_DIR points at a temp
dir (set BEFORE importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_auth_routes -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-routes-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, auth_routes, db  # noqa: E402
from app.config import Settings, load_settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

TRAKT_ACCOUNT_ID = 424242


class RouteTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        RouteTestCase._counter += 1
        db.set_db_path(TMP / f"routes-{RouteTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        # https, because the session cookie is Secure by default and a client
        # that honors that (as every browser does) won't send it back over plain
        # http. Constructed without entering the lifespan, which would start the
        # heartbeat loop and reach for the network.
        #
        # The Origin header is what a browser sends on any fetch() that changes
        # something, and mutating requests without one are refused; the test
        # client sends no headers it isn't told to.
        self.client = TestClient(app, base_url="https://testserver",
                                 headers={"Origin": "https://testserver"})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def setup_account(self, username="josh", password="hunter2hunter2", confirm=None):
        return self.client.post("/onboarding", json={
            "username": username,
            "password": password,
            "password_confirm": password if confirm is None else confirm,
        })


class FirstRunTests(RouteTestCase):
    def test_creates_the_first_administrator_and_signs_them_in(self):
        resp = self.setup_account()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["ok"])

        user = asyncio.run(auth.find_user_by_username("josh"))
        self.assertTrue(user["is_admin"])
        self.assertTrue(user["is_bootstrap"])
        self.assertTrue(user["calendar_approved"])
        self.assertTrue(user["distrakt_approved"])
        self.assertIsNotNone(user["password_hash"])
        self.assertIn(auth.COOKIE_NAME_SECURE, self.client.cookies)
        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM user_prefs")), 1)

    def test_a_second_setup_request_is_refused(self):
        """The application-level half of the race guard. The database-level half
        (the partial unique index that catches genuinely simultaneous requests)
        is covered in tests/test_db.py."""
        self.assertEqual(self.setup_account().status_code, 200)
        resp = self.setup_account(username="mallory")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")), 1)

    def test_page_redirects_once_an_account_exists(self):
        self.setup_account()
        resp = self.client.get("/onboarding", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_validation(self):
        self.assertEqual(self.setup_account(username="a").status_code, 400)      # too short
        self.assertEqual(self.setup_account(username="admin").status_code, 400)  # reserved
        self.assertEqual(self.setup_account(password="short").status_code, 400)
        self.assertEqual(self.setup_account(confirm="different-password").status_code, 400)
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")), 0)

    def test_form_encoded_body_is_rejected(self):
        """A form-encoded cross-origin POST needs no CORS preflight, so accepting
        one would leave these endpoints defended by the cookie's SameSite
        attribute alone."""
        resp = self.client.post("/onboarding", data={"username": "josh", "password": "x"})
        self.assertEqual(resp.status_code, 415)
        self.assertEqual(self.client.post("/login", data={"username": "a"}).status_code, 415)
        self.assertEqual(self.client.post("/logout", data={}).status_code, 415)


class UpgradePathTests(RouteTestCase):
    """Setting up an instance that already has single-user data."""

    def test_seeds_prefs_and_timezone_from_settings_json(self):
        save_settings(Settings(endpoint="shows/premieres", card_style="poster",
                               day_packing="packed", genres="-anime,-music",
                               countries="us,gb", timezone="America/New_York",
                               network_filter=["HBO"]))
        self.setup_account()
        user = asyncio.run(auth.find_user_by_username("josh"))
        prefs = asyncio.run(db.fetch_one("SELECT * FROM user_prefs WHERE user_id = ?",
                                         (user["id"],)))
        self.assertEqual(user["timezone"], "America/New_York")
        self.assertEqual(prefs["endpoint"], "shows/premieres")
        self.assertEqual(prefs["genres"], "-anime,-music")
        self.assertEqual(prefs["countries"], "us,gb")
        self.assertEqual(prefs["network_filter_json"], '["HBO"]')

    def test_adopts_an_existing_trakt_token_as_a_linked_identity(self):
        save_settings(Settings(trakt_client_id="cid", trakt_access_token="tok",
                               trakt_refresh_token="ref", trakt_token_expires_at=1800000000))
        with patch.object(auth_routes, "_fetch_trakt_identity",
                          return_value={"id": TRAKT_ACCOUNT_ID, "name": "Josh"}):
            resp = self.setup_account()
        self.assertTrue(resp.json()["trakt_adopted"])
        identity = asyncio.run(db.fetch_one("SELECT * FROM linked_identities"))
        # Keyed on the immutable numeric account id, never the username or slug.
        self.assertEqual(identity["provider"], "trakt")
        self.assertEqual(identity["provider_user_id"], str(TRAKT_ACCOUNT_ID))
        self.assertEqual(identity["display_name"], "Josh")
        self.assertEqual(identity["access_token"], "tok")
        self.assertEqual(identity["refresh_token"], "ref")

    def test_setup_still_succeeds_when_the_account_lookup_fails(self):
        """Never block first-run setup on a third-party call, and never store an
        identity row keyed on a guess."""
        save_settings(Settings(trakt_client_id="cid", trakt_access_token="tok"))
        with patch.object(auth_routes, "_fetch_trakt_identity", return_value=None):
            resp = self.setup_account()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["trakt_adopted"])
        self.assertEqual(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM linked_identities")), 0)
        # ...and Settings gets a notice to prompt a reconnect.
        self.assertEqual(asyncio.run(db.get_meta(auth_routes.TRAKT_RECONNECT_NOTICE)), "1")

    def test_no_token_means_no_identity_and_no_notice(self):
        self.setup_account()
        self.assertEqual(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM linked_identities")), 0)
        self.assertIsNone(asyncio.run(db.get_meta(auth_routes.TRAKT_RECONNECT_NOTICE)))

    def test_an_account_response_without_a_numeric_id_is_refused(self):
        """A slug or username is not an acceptable identity key: their owner can
        change them, and somebody else can then register the old one."""
        save_settings(Settings(trakt_client_id="cid", trakt_access_token="tok"))

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"username": "josh", "ids": {"slug": "josh"}}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *a, **kw):
                return _Resp()

        with patch("app.trakt_auth.httpx.AsyncClient", return_value=_Client()):
            resp = self.setup_account()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["trakt_adopted"])


class SignInTests(RouteTestCase):
    def test_sign_in_and_out_round_trip(self):
        self.setup_account()
        self.client.cookies.clear()

        resp = self.client.post("/login", json={"username": "josh", "password": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["redirect"], "/")
        self.assertIn(auth.COOKIE_NAME_SECURE, self.client.cookies)
        self.assertEqual(self.client.get("/me").status_code, 200)

        # Setup left a session of its own behind, so signing out must delete
        # exactly this one rather than everything the account has.
        before = asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions"))
        self.assertEqual(self.client.post("/logout", json={}).status_code, 200)
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions")),
                         before - 1)
        self.assertEqual(self.client.get("/me").status_code, 401)

    def test_failures_are_indistinguishable(self):
        """Unknown username, wrong password, and disabled account must return the
        same status and the same message, or the response becomes a way to test
        whether an account exists."""
        self.setup_account()
        unknown = self.client.post("/login", json={"username": "nobody", "password": "x" * 12})
        wrong = self.client.post("/login", json={"username": "josh", "password": "x" * 12})
        asyncio.run(db.execute("UPDATE users SET is_disabled = 1"))
        disabled = self.client.post("/login",
                                    json={"username": "josh", "password": "hunter2hunter2"})
        for resp in (unknown, wrong, disabled):
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(resp.json()["error"], auth_routes.INVALID_CREDENTIALS)

    def test_sign_in_is_case_insensitive(self):
        self.setup_account()
        self.client.cookies.clear()
        resp = self.client.post("/login", json={"username": "JOSH", "password": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_unapproved_user_lands_on_the_account_page(self):
        self.setup_account()
        asyncio.run(auth.create_user(username="bob", password="hunter2hunter2",
                                     settings=Settings()))
        self.client.cookies.clear()
        resp = self.client.post("/login", json={"username": "bob", "password": "hunter2hunter2"})
        self.assertEqual(resp.json()["redirect"], "/me")
        self.assertIn("awaiting admin approval", self.client.get("/me").text)

    def test_sign_in_page_redirects_to_setup_on_a_fresh_instance(self):
        resp = self.client.get("/login", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/onboarding")

    def test_me_requires_a_session(self):
        self.assertEqual(self.client.get("/me").status_code, 401)


class OnboardingConcurrencyTests(RouteTestCase):
    def test_a_second_setup_request_is_refused_under_genuine_concurrency(self):
        """The application-level count check (covered above) only proves the
        race guard works when the two requests are strictly ordered. Real
        concurrent requests are what BEGIN IMMEDIATE plus the partial unique
        index actually exist for."""
        barrier = Barrier(2)

        def attempt(username: str):
            barrier.wait(timeout=5)
            return self.client.post("/onboarding", json={
                "username": username, "password": "hunter2hunter2",
                "password_confirm": "hunter2hunter2",
            })

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["josh", "mallory"]))

        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")), 1)


class LoginRateLimitTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.setup_account()  # bootstrap admin "josh"
        self.client.cookies.clear()

    def _fail(self, username="josh", password="wrong-password-here"):
        return self.client.post("/login", json={"username": username, "password": password})

    def test_locks_out_after_five_failures_even_with_the_right_password(self):
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(self._fail().status_code, 401)
        locked = self._fail(password="hunter2hunter2")
        self.assertEqual(locked.status_code, 401)
        self.assertEqual(locked.json()["error"], auth_routes.INVALID_CREDENTIALS)

    def test_lockout_is_not_an_enumeration_oracle(self):
        """A lockout must be indistinguishable — status, body, AND timing class —
        from an ordinary failure, or the response itself reveals which usernames
        exist. Status and body are asserted directly; the timing class is what
        auth.burn_dummy_verify (exercised on every branch here) exists to hold,
        and is covered at the unit level in tests/test_auth.py."""
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self._fail()
        locked_real_account = self._fail(username="josh", password="hunter2hunter2")
        locked_unknown_account = self._fail(username="nobody-at-all", password="x" * 12)
        self.assertEqual(locked_real_account.status_code, locked_unknown_account.status_code)
        self.assertEqual(locked_real_account.json(), locked_unknown_account.json())

    def test_lockout_expires(self):
        stale = db.now() - auth.LOGIN_WINDOW_SECONDS - 5
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("username", "josh", False, now=stale))
        self.assertFalse(asyncio.run(auth.is_locked_out(
            "username", "josh",
            max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS)))
        resp = self.client.post("/login", json={"username": "josh", "password": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_username_and_ip_lockouts_are_counted_independently(self):
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("username", "alice", False))
        self.assertTrue(asyncio.run(auth.is_locked_out(
            "username", "alice",
            max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS)))
        # Neither an unrelated username nor an unrelated IP inherited the lock.
        self.assertFalse(asyncio.run(auth.is_locked_out(
            "username", "bob",
            max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS)))
        self.assertFalse(asyncio.run(auth.is_locked_out(
            "ip", "203.0.113.9",
            max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS)))

    def test_successful_login_clears_the_username_counter(self):
        for _ in range(auth.LOGIN_MAX_ATTEMPTS - 1):
            self._fail()
        ok = self.client.post("/login", json={"username": "josh", "password": "hunter2hunter2"})
        self.assertEqual(ok.status_code, 200, ok.text)
        remaining = asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = 'username' AND key_value = 'josh'"))
        self.assertEqual(remaining, 0)


class RegistrationTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.setup_account()  # bootstrap admin "josh"
        self.client.cookies.clear()

    def _admin_id(self) -> int:
        return int(asyncio.run(auth.find_user_by_username("josh"))["id"])

    def _make_invite(self, **kw):
        return asyncio.run(auth.create_invite(created_by=self._admin_id(), **kw))

    def _register(self, token: str, username: str, password: str = "hunter2hunter2"):
        suffix = f"?invite={token}" if token else ""
        return self.client.post(f"/register{suffix}", json={
            "username": username, "password": password, "password_confirm": password,
        })

    def test_an_invite_is_required_on_every_registration_path(self):
        resp = self._register("", "newbie")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], auth_routes.INVALID_INVITE)
        self.assertIsNone(asyncio.run(auth.find_user_by_username("newbie")))

    def test_a_default_invite_grants_calendar_but_never_distrakt(self):
        invite = self._make_invite()
        resp = self._register(invite["token"], "newbie")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["redirect"], "/")
        user = asyncio.run(auth.find_user_by_username("newbie"))
        self.assertTrue(user["calendar_approved"])
        self.assertFalse(user["distrakt_approved"])

    def test_an_invite_without_the_calendar_grant_lands_on_the_account_page(self):
        invite = self._make_invite(grants_calendar_on_accept=False)
        resp = self._register(invite["token"], "newbie")
        self.assertEqual(resp.json()["redirect"], "/me")
        user = asyncio.run(auth.find_user_by_username("newbie"))
        self.assertFalse(user["calendar_approved"])
        self.assertFalse(user["distrakt_approved"])

    def test_invalid_expired_revoked_exhausted_and_unknown_invites_are_byte_identical(self):
        revoked = self._make_invite()
        asyncio.run(auth.revoke_invite(revoked["id"]))
        expired = self._make_invite(expires_at=db.now() - 10)
        exhausted = self._make_invite(max_uses=1)
        self.assertEqual(self._register(exhausted["token"], "user-zero").status_code, 200)

        bodies = set()
        statuses = set()
        for token in (revoked["token"], expired["token"], exhausted["token"], "not-a-real-token", ""):
            resp = self._register(token, f"race-{token or 'blank'}")
            statuses.add(resp.status_code)
            bodies.add(resp.text)
        self.assertEqual(statuses, {400})
        self.assertEqual(len(bodies), 1)

    def test_quota_and_expiry_are_enforced(self):
        exhausted = self._make_invite(max_uses=1)
        self.assertEqual(self._register(exhausted["token"], "user-a").status_code, 200)
        second = self._register(exhausted["token"], "user-b")
        self.assertEqual(second.status_code, 400)
        self.assertIsNone(asyncio.run(auth.find_user_by_username("user-b")))

        expired = self._make_invite(expires_at=db.now() - 1)
        resp = self._register(expired["token"], "user-c")
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(asyncio.run(auth.find_user_by_username("user-c")))

    def test_a_taken_username_is_revealed(self):
        """The one accepted enumeration exception — unrelated to invite validity,
        which never reveals anything."""
        invite = self._make_invite()
        resp = self._register(invite["token"], "josh")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "That username is taken.")

    def test_open_registration_bypasses_the_invite_requirement(self):
        settings = load_settings()
        settings.allow_open_registration = True
        save_settings(settings)
        resp = self._register("", "openuser")
        self.assertEqual(resp.status_code, 200, resp.text)
        user = asyncio.run(auth.find_user_by_username("openuser"))
        # Still not calendar-approved: approval gating stays in effect even when
        # the invite requirement is off.
        self.assertFalse(user["calendar_approved"])

    def test_invite_page_shows_the_invalid_page_for_every_bad_reason(self):
        for token in ("not-a-real-token", ""):
            resp = self.client.get(f"/register?invite={token}" if token else "/register")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("not valid", resp.text)

    def test_invite_page_shows_the_form_for_a_usable_invite(self):
        invite = self._make_invite()
        resp = self.client.get(f"/register?invite={invite['token']}")
        self.assertIn("Create your account", resp.text)


class AdminInviteEndpointTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.setup_account()  # bootstrap admin "josh", already signed in

    def test_admin_can_mint_an_invite(self):
        resp = self.client.post("/api/admin/invites", json={"label": "friend"})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["token"])
        invite = asyncio.run(auth.find_invite_by_token(data["token"]))
        self.assertEqual(invite["label"], "friend")
        self.assertTrue(invite["grants_calendar_on_accept"])
        self.assertIsNone(invite["max_uses"])
        self.assertIsNone(invite["expires_at"])

    def test_a_minted_invite_actually_registers_someone(self):
        token = self.client.post("/api/admin/invites", json={}).json()["token"]
        self.client.cookies.clear()
        resp = self.client.post(f"/register?invite={token}", json={
            "username": "invitee", "password": "hunter2hunter2", "password_confirm": "hunter2hunter2",
        })
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_non_admin_cannot_mint_an_invite(self):
        asyncio.run(auth.create_user(username="bob", password="hunter2hunter2",
                                     settings=Settings(), calendar_approved=True))
        self.client.cookies.clear()
        self.client.post("/login", json={"username": "bob", "password": "hunter2hunter2"})
        resp = self.client.post("/api/admin/invites", json={})
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
