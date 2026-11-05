"""Unit tests for first-run setup, sign-in and the account page's own controls
(app/auth/routes.py).

Three things these routes do carry the weight: the first-run race guard, the
upgrade path for an instance that already has data (adopting the Trakt token from
settings.json, seeding view preferences), and the JSON-only body rule. Those are
what this file pins down, together with the shape every form on these pages is
answered in: a refusal is a sentence drawn at 200, and a success that changes
which page somebody should be on is 204 with HX-Redirect.

No network — the Trakt account lookup is mocked.
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from fastapi.testclient import TestClient
from markupsafe import escape

from app import auth, config, db
from app.sources import prefs as source_prefs
from app.auth import routes as auth_routes
from app.config import Settings
from tests.support import load_settings, save_settings, settling
from app.main import app
from tests.support import migrated_db

TRAKT_ACCOUNT_ID = 424242


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        migrated_db("routes")
        save_settings(Settings())
        # https, because the session cookie is Secure by default and a client
        # that honors that (as every browser does) won't send it back over plain
        # http. Constructed without entering the lifespan, which would start the
        # heartbeat loop and reach for the network.
        #
        # The Origin header is what a browser sends on any fetch() that changes
        # something, and mutating requests without one are refused; the test
        # client sends no headers it isn't told to.
        self.client = TestClient(settling(app), base_url="https://testserver",
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

    def sign_in(self, username="josh", password="hunter2hunter2"):
        return self.client.post("/login", json={"username": username, "password": password})

    # -- the two answers a form on these pages can get -------------------------

    def assertGoesTo(self, resp, target: str):
        """A success: no body to swap, and a header telling the browser where to go."""
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(resp.headers.get("HX-Redirect"), target)

    def assertRefused(self, resp, message: str | None = None):
        """A refusal: drawn at 200, going nowhere, saying why."""
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("HX-Redirect", resp.headers)
        if message is not None:
            self.assertEqual(resp.text.strip(), message)


class FirstRunTests(RouteTestCase):
    def test_creates_the_first_administrator_and_signs_them_in(self):
        self.assertGoesTo(self.setup_account(), "/")

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
        is covered in tests/kernel/test_db.py."""
        self.assertGoesTo(self.setup_account(), "/")
        self.assertRefused(self.setup_account(username="mallory"), auth_routes.ALREADY_SET_UP)
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")), 1)

    def test_page_redirects_once_an_account_exists(self):
        self.setup_account()
        resp = self.client.get("/onboarding", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_a_bad_value_is_drawn_rather_than_statused(self):
        for label, attempt in (("too short", {"username": "a"}),
                               ("reserved", {"username": "admin"}),
                               ("short password", {"password": "short"}),
                               ("mismatch", {"confirm": "different-password"})):
            with self.subTest(label):
                self.assertRefused(self.setup_account(**attempt))
        self.assertRefused(self.setup_account(password="short"),
                           f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")), 0)

    def test_form_encoded_body_is_rejected(self):
        """A form-encoded cross-origin POST needs no CORS preflight, so accepting
        one would leave these endpoints defended by the cookie's SameSite
        attribute alone."""
        resp = self.client.post("/onboarding", data={"username": "josh", "password": "x"})
        self.assertEqual(resp.status_code, 415)
        self.assertEqual(self.client.post("/login", data={"username": "a"}).status_code, 415)
        self.assertEqual(self.client.post("/logout", data={}).status_code, 415)
        self.assertEqual(self.client.post("/register", data={"username": "a"}).status_code, 415)

    def test_a_body_that_is_not_an_object_is_refused(self):
        # With an account in place, so the first-run gate lets /register through.
        self.setup_account()
        for path in ("/onboarding", "/login", "/register"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=["josh"]).status_code, 400)


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
        self.assertEqual(prefs["tv_genres"], "-anime,-music")
        self.assertEqual(prefs["tv_countries"], "us,gb")
        self.assertEqual(prefs["network_filter_json"], '["HBO"]')

    def test_adopts_an_existing_trakt_token_as_a_linked_identity(self):
        save_settings(Settings(trakt_client_id="cid", trakt_access_token="tok",
                               trakt_refresh_token="ref", trakt_token_expires_at=1800000000))
        with patch.object(auth_routes, "_fetch_trakt_identity",
                          return_value={"id": TRAKT_ACCOUNT_ID, "name": "Josh"}):
            self.assertGoesTo(self.setup_account(), "/")
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
            self.assertGoesTo(self.setup_account(), "/")
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

        with patch("app.auth.trakt.httpx.AsyncClient", return_value=_Client()):
            self.assertGoesTo(self.setup_account(), "/")
        self.assertEqual(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM linked_identities")), 0)


class SignInTests(RouteTestCase):
    def test_sign_in_and_out_round_trip(self):
        self.setup_account()
        self.client.cookies.clear()

        self.assertGoesTo(self.sign_in(), "/")
        self.assertIn(auth.COOKIE_NAME_SECURE, self.client.cookies)
        self.assertEqual(self.client.get("/me").status_code, 200)

        # Setup left a session of its own behind, so signing out must delete
        # exactly this one rather than everything the account has.
        before = asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions"))
        # THE SESSION ROW IS WHAT IS INVALIDATED, and the answer is a header
        # telling the browser where to go rather than a body to swap: the control
        # is a button that posts itself, and htmx follows a redirect STATUS and
        # swaps what it finds, which would put the sign-in page inside the menu.
        out = self.client.post("/logout", json={})
        self.assertEqual(out.status_code, 204)
        self.assertEqual(out.headers["HX-Redirect"], "/login")
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM sessions")),
                         before - 1)
        self.assertEqual(self.client.get("/me").status_code, 401)

    def test_failures_are_indistinguishable(self):
        """Unknown username, wrong password, and disabled account must answer with
        the same status and the same sentence, or the answer becomes a way to test
        whether an account exists."""
        self.setup_account()
        self.client.cookies.clear()
        unknown = self.sign_in("nobody", "x" * 12)
        wrong = self.sign_in("josh", "x" * 12)
        asyncio.run(db.execute("UPDATE users SET is_disabled = 1"))
        disabled = self.sign_in()
        for resp in (unknown, wrong, disabled):
            self.assertRefused(resp, auth_routes.INVALID_CREDENTIALS)
        self.assertEqual(len({unknown.text, wrong.text, disabled.text}), 1)
        self.assertNotIn(auth.COOKIE_NAME_SECURE, self.client.cookies)

    def test_sign_in_is_case_insensitive(self):
        self.setup_account()
        self.client.cookies.clear()
        self.assertGoesTo(self.sign_in("JOSH"), "/")

    def test_unapproved_user_lands_on_the_account_page(self):
        self.setup_account()
        asyncio.run(auth.create_user(username="bob", password="hunter2hunter2",
                                     settings=Settings()))
        self.client.cookies.clear()
        self.assertGoesTo(self.sign_in("bob"), "/me")
        self.assertIn("awaiting admin approval", self.client.get("/me").text)

    def test_sign_in_page_redirects_to_setup_on_a_fresh_instance(self):
        resp = self.client.get("/login", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/onboarding")

    def test_me_requires_a_session(self):
        self.assertEqual(self.client.get("/me").status_code, 401)

    def test_the_sign_in_pages_are_forms_that_post_themselves(self):
        """No script of their own: Enter, the refusal line and the redirect are the
        form's and htmx's."""
        self.setup_account()
        self.client.cookies.clear()
        invite = asyncio.run(auth.create_invite(
            created_by=asyncio.run(auth.find_user_by_username("josh"))["id"]))
        for path, action in (("/login", "/login"),
                             (f"/register?invite={invite['token']}", "/register")):
            with self.subTest(path=path):
                page = self.client.get(path).text
                self.assertNotIn("<script>", page)
                form = re.search(r'<form class="auth-form"[^>]*>', page).group(0)
                self.assertIn(f'hx-post="{action}"', form)
                self.assertIn("hx-json-enc", form)
                self.assertIn('hx-boost="false"', form)
                self.assertIn('name="username"', page)
                self.assertIn('name="password"', page)


class CookieWarningTests(RouteTestCase):
    """GET /login/cookie-check: whether the browser kept a cookie carrying the
    session cookie's Secure policy. The page offers the cookie; the check asks
    afterwards whether it came back."""

    def setUp(self):
        super().setUp()
        self.setup_account()
        self.client.cookies.clear()

    def test_a_browser_that_keeps_the_cookie_is_told_nothing(self):
        self.client.get("/login")
        self.assertIn(auth.COOKIE_PROBE, self.client.cookies)
        self.assertEqual(self.client.get("/login/cookie-check").text.strip(), "")

    def test_a_browser_that_dropped_it_is_warned(self):
        """The test client keeps a Secure cookie over any scheme, so the browser
        refusing it is the jar being cleared between the page and the check —
        which is exactly what the check sees from a browser on plain http://."""
        page = self.client.get("/login")
        self.assertIn("secure", page.headers["set-cookie"].lower())
        self.client.cookies.clear()
        warning = self.client.get("/login/cookie-check").text
        self.assertIn('id="cookieWarning"', warning)
        self.assertIn("cookie_secure", warning)

    def test_a_policy_that_is_not_secure_is_never_warned_about(self):
        settings = load_settings()
        settings.cookie_secure = "never"
        save_settings(settings)
        self.assertEqual(self.client.get("/login/cookie-check").text.strip(), "")

    def test_the_page_asks_rather_than_guessing_from_the_address_bar(self):
        page = self.client.get("/login").text
        self.assertIn('hx-get="/login/cookie-check"', page)
        self.assertNotIn("location.protocol", page)


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

        self.assertEqual(sorted(r.status_code for r in results), [200, 204])
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")), 1)


class LoginRateLimitTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.setup_account()  # bootstrap admin "josh"
        self.client.cookies.clear()

    def _fail(self, username="josh", password="wrong-password-here"):
        return self.sign_in(username, password)

    def test_locks_out_after_five_failures_even_with_the_right_password(self):
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self.assertRefused(self._fail(), auth_routes.INVALID_CREDENTIALS)
        self.assertRefused(self._fail(password="hunter2hunter2"), auth_routes.INVALID_CREDENTIALS)
        self.assertNotIn(auth.COOKIE_NAME_SECURE, self.client.cookies)

    def test_lockout_is_not_an_enumeration_oracle(self):
        """A lockout must be indistinguishable — status, body, AND timing class —
        from an ordinary failure, or the response itself reveals which usernames
        exist. Status and body are asserted directly; the timing class is what
        auth.burn_dummy_verify (exercised on every branch here) exists to hold,
        and is covered at the unit level in tests/auth/test_accounts.py."""
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self._fail()
        locked_real_account = self._fail(username="josh", password="hunter2hunter2")
        locked_unknown_account = self._fail(username="nobody-at-all", password="x" * 12)
        self.assertEqual(locked_real_account.status_code, locked_unknown_account.status_code)
        self.assertEqual(locked_real_account.text, locked_unknown_account.text)

    def test_lockout_expires(self):
        stale = db.now() - auth.LOGIN_WINDOW_SECONDS - 5
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("username", "josh", False, now=stale))
        self.assertFalse(asyncio.run(auth.is_locked_out(
            "username", "josh",
            max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS)))
        self.assertGoesTo(self.sign_in(), "/")

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
        self.assertGoesTo(self.sign_in(), "/")
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
        """As the registration form posts it: the invite rides in the body."""
        return self.client.post("/register", json={
            "invite": token, "username": username,
            "password": password, "password_confirm": password,
        })

    def test_an_invite_is_required_on_every_registration_path(self):
        self.assertRefused(self._register("", "newbie"), auth_routes.INVALID_INVITE)
        self.assertIsNone(asyncio.run(auth.find_user_by_username("newbie")))

    def test_an_invite_in_the_query_string_still_counts(self):
        invite = self._make_invite()
        resp = self.client.post(f"/register?invite={invite['token']}", json={
            "username": "linkuser", "password": "hunter2hunter2",
            "password_confirm": "hunter2hunter2"})
        self.assertGoesTo(resp, "/")

    def test_a_default_invite_grants_calendar_but_never_distrakt(self):
        invite = self._make_invite()
        self.assertGoesTo(self._register(invite["token"], "newbie"), "/")
        user = asyncio.run(auth.find_user_by_username("newbie"))
        self.assertTrue(user["calendar_approved"])
        self.assertFalse(user["distrakt_approved"])

    def test_an_invite_without_the_calendar_grant_lands_on_the_account_page(self):
        invite = self._make_invite(grants_calendar_on_accept=False)
        self.assertGoesTo(self._register(invite["token"], "newbie"), "/me")
        user = asyncio.run(auth.find_user_by_username("newbie"))
        self.assertFalse(user["calendar_approved"])
        self.assertFalse(user["distrakt_approved"])

    def test_invalid_expired_revoked_exhausted_and_unknown_invites_are_byte_identical(self):
        revoked = self._make_invite()
        asyncio.run(auth.revoke_invite(revoked["id"]))
        expired = self._make_invite(expires_at=db.now() - 10)
        exhausted = self._make_invite(max_uses=1)
        self.assertGoesTo(self._register(exhausted["token"], "user-zero"), "/")
        self.client.cookies.clear()

        bodies = set()
        for token in (revoked["token"], expired["token"], exhausted["token"], "not-a-real-token", ""):
            resp = self._register(token, f"race-{token or 'blank'}")
            self.assertRefused(resp)
            bodies.add(resp.text)
        self.assertEqual(len(bodies), 1)

    def test_quota_and_expiry_are_enforced(self):
        exhausted = self._make_invite(max_uses=1)
        self.assertGoesTo(self._register(exhausted["token"], "user-a"), "/")
        self.client.cookies.clear()
        self.assertRefused(self._register(exhausted["token"], "user-b"), auth_routes.INVALID_INVITE)
        self.assertIsNone(asyncio.run(auth.find_user_by_username("user-b")))

        expired = self._make_invite(expires_at=db.now() - 1)
        self.assertRefused(self._register(expired["token"], "user-c"), auth_routes.INVALID_INVITE)
        self.assertIsNone(asyncio.run(auth.find_user_by_username("user-c")))

    def test_a_taken_username_is_revealed(self):
        """The one accepted enumeration exception — unrelated to invite validity,
        which never reveals anything."""
        invite = self._make_invite()
        self.assertRefused(self._register(invite["token"], "josh"), "That username is taken.")

    def test_registration_stays_rate_limited(self):
        """The throttle answers in a sentence now, and it is still a throttle: the
        attempt after the limit is refused even with a usable invite."""
        invite = self._make_invite()
        for i in range(auth.REGISTER_MAX_ATTEMPTS):
            self._register("not-a-real-token", f"spray-{i}")
        self.assertRefused(self._register(invite["token"], "real-user"),
                           "Too many attempts from this address. Try again later.")
        self.assertIsNone(asyncio.run(auth.find_user_by_username("real-user")))

    def test_open_registration_bypasses_the_invite_requirement(self):
        settings = load_settings()
        settings.allow_open_registration = True
        save_settings(settings)
        self.assertGoesTo(self._register("", "openuser"), "/me")
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
        self.assertIn(f'name="invite" value="{invite["token"]}"', resp.text)


class AccountTestCase(RouteTestCase):
    """Signed in as the bootstrap administrator "josh"."""

    def setUp(self):
        super().setUp()
        self.setup_account()

    def user_id(self, username="josh") -> int:
        return int(asyncio.run(auth.find_user_by_username(username))["id"])

    def link(self, provider: str, provider_user_id: int) -> None:
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id(), provider=provider,
            provider_user_id=provider_user_id)))


class DisplayNameTests(AccountTestCase):
    """A display name is a LABEL, not an identifier. The username stays the
    lowercase, unique, case-insensitive thing you sign in with and that share
    links are built from; this is the free-text thing you are called on screen.
    The two must not become each other."""

    def _set(self, value):
        return self.client.post("/me/display-name", json={"display_name": value})

    def _stored(self):
        return asyncio.run(auth.find_user_by_username("josh"))["display_name"]

    def test_a_new_account_has_no_display_name(self):
        self.assertIsNone(self._stored())

    def test_case_is_preserved_exactly(self):
        """The whole reason the column is separate from username, which is
        NOCASE and would treat these as the same string."""
        self.assertEqual(self._set("JoSh BlAck").status_code, 200)
        self.assertEqual(self._stored(), "JoSh BlAck")

    def test_spaces_are_allowed(self):
        self.assertEqual(self._set("Josh Black").status_code, 200)
        self.assertEqual(self._stored(), "Josh Black")

    def test_surrounding_and_repeated_whitespace_is_collapsed(self):
        """Two names that are indistinguishable on screen must not be stored as
        different strings — and the answer draws what was STORED."""
        resp = self._set("  Josh   Black  ")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('value="Josh Black"', resp.text)
        self.assertEqual(self._stored(), "Josh Black")

    def test_the_answer_redraws_the_heading_out_of_band(self):
        resp = self._set("Josh Black")
        self.assertIn('id="accountNames"', resp.text)
        self.assertRegex(resp.text, r'<h1 id="accountHeading" hx-swap-oob="outerHTML">Josh Black</h1>')

    def test_an_empty_value_clears_it(self):
        self._set("Josh Black")
        resp = self._set("")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Using your username", resp.text)
        self.assertIsNone(self._stored())

    def test_it_is_length_capped(self):
        resp = self._set("x" * (auth.DISPLAY_NAME_MAX + 1))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(escape(auth.display_name_error("x" * (auth.DISPLAY_NAME_MAX + 1))),
                      resp.text)
        self.assertNotIn("hx-swap-oob", resp.text)
        self.assertIsNone(self._stored())

    def test_control_and_bidi_characters_are_refused(self):
        """A name that can reorder or hide the text rendered beside it.

        Written as escapes, not literals, so this file stays reviewable and
        cannot pick up a stray NUL that would stop it compiling.
        """
        for label, bad in (("RLO", "Josh\u202eBlack"),
                           ("NUL", "Josh\x00Black"),
                           ("ZWSP", "Josh\u200bBlack"),
                           ("word joiner", "Josh\u2060Black"),
                           ("BOM", "Josh\ufeffBlack")):
            with self.subTest(character=label):
                resp = self._set(bad)
                self.assertEqual(resp.status_code, 200)
                self.assertIn(escape(auth.display_name_error(bad)), resp.text)
                self.assertIsNone(self._stored())

    def test_a_newline_is_collapsed_rather_than_refused(self):
        """Unlike the invisibles above. A newline is ordinary whitespace to the
        normalizer, so it becomes a space — and what matters is the property that
        the STORED value can never contain one, not that the request is refused.
        """
        self.assertEqual(self._set("Josh\nBlack").status_code, 200)
        self.assertEqual(self._stored(), "Josh Black")

    def test_it_does_not_have_to_be_unique(self):
        """Unlike the username. Nothing keys off a display name, so two people
        may call themselves the same thing."""
        asyncio.run(auth.create_user(username="bob", password="hunter2hunter2",
                                     settings=Settings(), calendar_approved=True))
        self._set("Josh Black")
        self.client.cookies.clear()
        self.sign_in("bob")
        self.assertEqual(self._set("Josh Black").status_code, 200)
        self.assertEqual(
            asyncio.run(auth.find_user_by_username("bob"))["display_name"], "Josh Black")

    def test_it_does_not_change_what_you_sign_in_with(self):
        self._set("Josh Black")
        self.client.cookies.clear()
        # The display name is not a login identifier...
        self.assertRefused(self.sign_in("Josh Black"), auth_routes.INVALID_CREDENTIALS)
        # ...and the username still is.
        self.assertGoesTo(self.sign_in(), "/")

    def test_the_account_page_shows_both_names(self):
        self._set("Josh Black")
        body = self.client.get("/me").text
        self.assertIn("Josh Black", body)      # the display name
        self.assertIn(">josh<", body)          # and the username, in its own row

    def _card_heading(self):
        """The account card's own <h1>, not the shared header's — that one says
        "Account" on every page and is a different element entirely."""
        body = self.client.get("/me").text
        return re.search(r'<h1 id="accountHeading">(.*?)</h1>', body, re.S).group(1).strip()

    def test_the_display_name_is_the_account_page_heading(self):
        self._set("Josh Black")
        self.assertEqual(self._card_heading(), "Josh Black")

    def test_the_heading_falls_back_to_the_username(self):
        self.assertEqual(self._card_heading(), "josh")

    def test_it_needs_a_session(self):
        self.client.cookies.clear()
        self.assertEqual(self._set("Nobody").status_code, 401)


class OpenRegistrationSettingTests(RouteTestCase):
    """Open registration had no way in but hand-editing settings.json. It is now
    on the Settings screen — but it is still a RECOVERY FIELD, so saving it from
    the UI must keep writing the file an operator can edit to get back in."""

    def setUp(self):
        super().setUp()
        self.setup_account()  # bootstrap admin "josh", already signed in

    def _save(self, value):
        # As the panel's select sends it: the string, not a JSON boolean.
        return self.client.post("/settings", json={"allow_open_registration": str(value).lower()})

    def _drawn_open(self) -> bool:
        body = self.client.get("/settings/fragments/panel").text
        return '<option value="true" selected>Open' in body

    def test_it_defaults_to_invite_only(self):
        self.assertFalse(load_settings().allow_open_registration)
        self.assertFalse(self._drawn_open())

    def test_an_admin_can_turn_it_on_and_off_again(self):
        self.assertEqual(self._save(True).status_code, 204)
        self.assertTrue(load_settings().allow_open_registration)
        self.assertTrue(self._drawn_open())

        self.assertEqual(self._save(False).status_code, 204)
        self.assertFalse(load_settings().allow_open_registration)

    def test_it_still_persists_to_the_settings_file(self):
        """The whole point of a recovery field: an operator locked out of this
        screen can still reach the value with a text editor."""
        self._save(True)
        # Asked of config rather than rebuilt from TMP: the module resolves its
        # data directory once at import, and whichever test module imported last
        # decides it for the whole run — so a path reassembled here is right
        # alone and wrong in the full suite.
        written = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertTrue(written["allow_open_registration"])

    def test_turning_it_on_actually_opens_registration(self):
        """The setting is only worth a control if it reaches the gate."""
        self.client.cookies.clear()
        walkup = {"username": "walkup", "password": "hunter2hunter2",
                  "password_confirm": "hunter2hunter2"}
        self.assertRefused(self.client.post("/register", json=walkup), auth_routes.INVALID_INVITE)

        self.sign_in()
        self._save(True)
        self.client.cookies.clear()
        self.assertGoesTo(self.client.post("/register", json=walkup), "/me")
        # Open to signing up is NOT open to the data: approval is still required.
        self.assertFalse(
            asyncio.run(auth.find_user_by_username("walkup"))["calendar_approved"])

    def test_a_non_admin_cannot_change_it(self):
        asyncio.run(auth.create_user(username="bob", password="hunter2hunter2",
                                     settings=Settings(), calendar_approved=True))
        self.client.cookies.clear()
        self.sign_in("bob")
        self.assertEqual(self._save(True).status_code, 403)
        self.assertFalse(load_settings().allow_open_registration)


class AutoApproveSettingTests(RouteTestCase):
    """Approving new accounts automatically is its OWN setting, independent of
    open registration. The two are different decisions — who may create an
    account, and who may see the data — and the pairing is what these pin down.
    """

    def setUp(self):
        super().setUp()
        self.setup_account()  # bootstrap admin "josh", already signed in

    def _configure(self, *, open_reg, auto):
        resp = self.client.post("/settings", json={
            "allow_open_registration": str(open_reg).lower(),
            "auto_approve_calendar": str(auto).lower()})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.client.cookies.clear()

    def _register(self, username, invite=""):
        return self.client.post("/register", json={
            "invite": invite, "username": username, "password": "hunter2hunter2",
            "password_confirm": "hunter2hunter2"})

    def _approved(self, username):
        return bool(asyncio.run(auth.find_user_by_username(username))["calendar_approved"])

    def test_it_defaults_off(self):
        self.assertFalse(load_settings().auto_approve_calendar)

    def test_open_registration_alone_does_not_approve(self):
        """The reason it is a separate switch: opening sign-ups must not
        silently open the calendar."""
        self._configure(open_reg=True, auto=False)
        self.assertGoesTo(self._register("waiting"), "/me")
        self.assertFalse(self._approved("waiting"))

    def test_open_registration_with_auto_approve_grants_the_calendar(self):
        self._configure(open_reg=True, auto=True)
        self.assertGoesTo(self._register("welcomed"), "/")
        self.assertTrue(self._approved("welcomed"))

    def test_it_applies_to_invite_sign_ups_too(self):
        """So it is also the way to approve on accept regardless of what a
        particular invite happened to grant."""
        self.client.post("/admin/invites", json={
            "grants_calendar_on_accept": "0", "grants_ranker_on_accept": "0"})
        token = asyncio.run(auth.list_invites())[0]["token"]
        self.client.post("/settings", json={"auto_approve_calendar": "true"})
        self.client.cookies.clear()
        self.assertGoesTo(self._register("invited", token), "/")
        self.assertTrue(self._approved("invited"))

    def test_it_does_not_hand_out_the_other_grants(self):
        """Calendar only. distrakt and the ranker stay manual."""
        self._configure(open_reg=True, auto=True)
        self._register("calendar_only")
        row = asyncio.run(auth.find_user_by_username("calendar_only"))
        self.assertTrue(row["calendar_approved"])
        self.assertFalse(row["distrakt_approved"])
        self.assertFalse(row["ranker_approved"])
        self.assertFalse(row["is_admin"])


class StatingWhoseDescriptionIsReadTests(AccountTestCase):
    """POST /me/order/metadata_order — a press moves one service a place, and the
    server saves the list it drew. The tracker's own lists (which linked tracker
    decides, whose numbers count, where an episode total is asked of) are moved
    from the tracker's settings dialog, and this page no longer offers them."""

    def _stored(self):
        return asyncio.run(source_prefs.load(self.user_id()))

    def _move(self, source, direction, kind="metadata_order"):
        return self.client.post(f"/me/order/{kind}",
                                json={"source": source, "direction": direction})

    def test_the_metadata_order_is_the_same_control_over_its_own_column(self):
        settings = load_settings()
        settings.simkl_public_calendar_enabled = True
        save_settings(settings)
        page = self.client.get("/me").text
        names = re.findall(r'id="metadata_order-(\w+)-(?:up|down)"', page)
        if len(set(names)) < 2:
            self.skipTest("this instance offers one describing service")
        last = re.findall(r'<ol class="tracker-priority-list">(.*?)</ol>', page, re.S)[0]
        second = re.findall(r'data-source="(\w+)"', last)[1]
        self._move(second, "up")
        self.assertEqual(self._stored().metadata_order[0], second)
        self.assertEqual(self._stored().tracker_priority, [])

    def test_a_move_is_answered_with_the_list(self):
        settings = load_settings()
        settings.simkl_public_calendar_enabled = True
        save_settings(settings)
        page = self.client.get("/me").text
        if len(set(re.findall(r'id="metadata_order-(\w+)-(?:up|down)"', page))) < 2:
            self.skipTest("this instance offers one describing service")
        first, second = re.findall(r'data-source="(\w+)"', page)[:2]
        resp = self._move(second, "up")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(re.findall(r'data-source="(\w+)"', resp.text)[:2], [second, first])
        self.assertRegex(resp.text, rf'id="metadata_order-{second}-down"[^>]*autofocus')

    def test_a_move_that_cannot_happen_is_drawn_and_stores_nothing(self):
        for source, direction in (("letterboxd", "down"), ("simkl", "sideways")):
            with self.subTest(source=source, direction=direction):
                resp = self._move(source, direction)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("cannot move that way", resp.text)
        self.assertEqual(self._stored().metadata_order, [])

    def test_the_trackers_lists_are_not_this_pages_to_move(self):
        resp = self._move("simkl", "up", kind="tracker_order")
        self.assertIn("There is no such list.", resp.text)
        self.assertEqual(self._stored().tracker_priority, [])
        self.assertEqual(self.client.post("/me/tracker-retired", json={}).status_code, 404)

    def test_tmdb_is_in_the_metadata_order(self):
        """TMDB describes titles and publishes no calendar: with its token here it
        is offered in whose-description-is-read, first by default."""
        settings = load_settings()
        settings.tmdb_api_key = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJtZSJ9.c2ln"
        save_settings(settings)
        page = self.client.get("/me").text
        self.assertIn('id="metadata_order-tmdb-down"', page)
        self.assertNotIn('id="tracker_order-', page)
        self._move("tmdb", "down")
        self.assertNotEqual(self._stored().metadata_order[0], "tmdb")


class PublishingWatchMarksOnASharedCalendarTests(AccountTestCase):
    """POST /me/share-watch-history — the one switch over a public fact.

    IT IS THE ACCOUNT'S ANSWER, NOT A LINK'S, and that is what this file can
    prove: the value is stored against the user, so every link that account has
    already handed out changes with it. That is the whole reason it lives here
    and not beside the per-link view options in the Share panel — somebody who
    changes their mind about publishing a viewing history needs it to reach the
    links they shared months ago, not just the next one.
    """

    def _stored(self) -> bool:
        return bool(asyncio.run(auth.get_user_prefs(self.user_id()))["share_watch_history"])

    def _post(self, enabled):
        return self.client.post("/me/share-watch-history", json={"enabled": enabled})

    def test_it_starts_on(self):
        """ON BY DEFAULT, which is only defensible because it can be turned off
        in the same change that turned it on."""
        self.assertTrue(self._stored())

    def test_turning_it_off_is_stored_and_said(self):
        resp = self._post("0")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(self._stored())
        self.assertIn("will no longer show your watched episodes", resp.text)
        self.assertIn('hx-swap-oob="beforeend"', resp.text)

    def test_turning_it_back_on_is_stored(self):
        self._post("0")
        self._post(["0", "1"])
        self.assertTrue(self._stored())

    def test_it_refuses_anything_but_the_boxs_own_two_spellings(self):
        """A value that happens to read as truthy must not decide whether a
        viewing history is published."""
        self._post("0")
        for value in ("true", "yes", "on", 1, True, None, ""):
            with self.subTest(value=value):
                resp = self._post(value)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertIn("has to be on or off", resp.text)
        self.assertFalse(self._stored())

    def test_it_needs_a_session(self):
        self.client.cookies.clear()
        self.assertIn(self._post("0").status_code, (401, 403))

    def test_the_account_page_draws_the_switch_in_its_stored_state(self):
        self.assertRegex(self.client.get("/me").text, r'id="shareWatchHistoryBox"[^>]*checked')
        self._post("0")
        self.assertNotRegex(self.client.get("/me").text, r'id="shareWatchHistoryBox"[^>]*checked')


if __name__ == "__main__":
    unittest.main()
