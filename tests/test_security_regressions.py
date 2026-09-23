"""Regressions in abuse resistance and in what one account may see of another.

One class per defect, each naming the failure it exists to catch. They are kept
together, and at the top of tests/ rather than under one feature, because what
they have in common is not a module — it is that every one of them fails
SILENTLY: nothing looks broken, the wrong person is simply served, or a limit
simply stops limiting. A file per feature would file each of them next to the
tests that already pass while the defect is live.

  - A LOCKOUT THAT ENDS. Attempts made while already locked out are not counted,
    and a lockout that has served its window resets the counter instead of
    leaving it primed one failure below the threshold. Without both, a retry loop
    holds a lockout open forever — and because every user behind a reverse proxy
    shares one per-IP key, that is a whole-instance outage rather than one
    account's problem. The per-IP counter clearing on success is the same rule:
    other people's failures must not survive somebody proving they own an
    account.
  - THE SETTINGS SCREEN CAN DIAGNOSE THE PROXY, and the cookie policy is
    resolved from evidence at onboarding rather than guessed — including on a
    brand-new instance behind a proxy, which is the install that could not be
    set up at all.
  - ONE ACCOUNT'S THINGS ARE ITS OWN: admin session revocation is scoped to the
    account it was issued against, the network-emoji map is per user, the
    tracker's details route answers from the caller's own roster, and the
    account page and share panel disclose nothing an admin-only setting owns.
  - THE NARROW ONES: share tokens compare in constant time, the provider sign-in
    start routes are throttled, a retired slug keeps working while its name is
    freed, re-authorizing in Settings writes the token where the tracker reads
    it, and every mutating body goes through the one shared guard.

No network.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, authz, db, distrakt
from app.calendar import share_links
from app.config import (TRUSTED_PROXY_IPS_DEFAULT, Settings, load_settings,
                        save_settings)
from app.media import user_images
from app.main import app
from tests.support import AppTestCase, DetailsPanel

PASSWORD = "correct-horse-battery"
LOCK = {"max_attempts": auth.LOGIN_MAX_ATTEMPTS, "window_seconds": auth.LOGIN_WINDOW_SECONDS}


class RegressionTestCase(AppTestCase):
    def make_user(self, username="josh", password=PASSWORD, **flags):
        flags.setdefault("calendar_approved", True)
        return super().make_user(username, password, **flags)

    def login(self, username="josh", password=PASSWORD):
        return self.client.post("/login", json={"username": username, "password": password})

    def assertSignedIn(self, resp):
        """A sign-in that worked: nothing to swap, and the browser sent on."""
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertIn("HX-Redirect", resp.headers)

    def assertRefused(self, resp):
        """A sign-in that did not: the one sentence every failure draws, at 200."""
        from app.auth import routes as auth_routes
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.text.strip(), auth_routes.INVALID_CREDENTIALS)

    def fail_login(self, times, username="josh"):
        for _ in range(times):
            self.login(username, "wrong-password")

    def failures(self, key_type, key_value):
        return asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
            "AND succeeded = 0",
            (key_type, key_value), default=0,
        ))


class LockoutDoesNotSelfPerpetuateTests(RegressionTestCase):
    """The failure mode this whole change exists for: a lockout that never ends
    because the attempts rejected BY the lockout kept refilling its own window."""

    def test_attempts_made_while_locked_out_are_not_counted(self):
        self.make_user()
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS)
        counted = self.failures("username", "josh")
        # Ten more tries against a locked key must add nothing.
        self.fail_login(10)
        self.assertEqual(self.failures("username", "josh"), counted)

    def test_a_locked_out_attempt_still_answers_identically(self):
        """The refusal must not become an oracle for "you are locked out" — same
        status and same body as any other failure."""
        self.make_user()
        wrong = self.login("josh", "wrong-password")
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS)
        locked = self.login("josh", PASSWORD)  # the RIGHT password, while locked
        self.assertEqual(locked.status_code, wrong.status_code)
        self.assertEqual(locked.text, wrong.text)

    def test_lockout_expires_and_the_counter_starts_over(self):
        """Once the window has passed, the next single mistake must not re-lock
        the key — the old failures are dropped rather than left one short."""
        self.make_user()
        stale = db.now() - auth.LOGIN_WINDOW_SECONDS - 1
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("username", "josh", False, now=stale))
        # The lockout has lapsed, and checking it clears the primed counter.
        self.assertFalse(asyncio.run(auth.check_lockout("username", "josh", **LOCK)))
        self.assertEqual(self.failures("username", "josh"), 0)
        # So one fresh failure leaves the account usable rather than re-locked.
        self.assertRefused(self.login("josh", "wrong-password"))
        self.assertFalse(asyncio.run(auth.check_lockout("username", "josh", **LOCK)))
        self.assertSignedIn(self.login())

    def test_check_lockout_does_not_clear_a_counter_below_the_threshold(self):
        """Only a LAPSED lockout resets. Ordinary failures inside the window keep
        accumulating, or the limiter would never fire at all."""
        self.make_user()
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS - 1)
        self.assertFalse(asyncio.run(auth.check_lockout("username", "josh", **LOCK)))
        self.assertEqual(self.failures("username", "josh"), auth.LOGIN_MAX_ATTEMPTS - 1)
        # The next one still locks it.
        self.fail_login(1)
        self.assertTrue(asyncio.run(auth.check_lockout("username", "josh", **LOCK)))


class SharedProxyIpCounterTests(RegressionTestCase):
    """Behind a reverse proxy every user shares one per-IP key, so anything that
    leaves failures on it is an instance-wide outage waiting to happen."""

    def test_successful_login_clears_the_ip_counter_too(self):
        self.make_user()
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS - 1)
        self.assertGreater(self.failures("ip", "testclient"), 0)
        self.assertSignedIn(self.login())
        self.assertEqual(self.failures("ip", "testclient"), 0)
        self.assertEqual(self.failures("username", "josh"), 0)

    def test_one_users_failures_do_not_lock_out_another_after_a_success(self):
        """The scenario: several people share one apparent address. Four bad
        tries from one of them, then a good login from anyone, must not leave the
        instance one mistake away from locking everybody out."""
        self.make_user("josh")
        self.make_user("alice")
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS - 1, username="josh")
        self.assertSignedIn(self.login("alice"))
        # Alice's success cleared the shared key, so josh's next mistake is his
        # first, not his fifth.
        self.assertRefused(self.login("josh", "wrong-password"))
        self.assertSignedIn(self.login("alice"))


class ProxyDiagnosticsTests(RegressionTestCase):
    """The Settings panel has to be able to answer "what do I type here?" —
    guessing a container subnet is exactly how this gets left at the default."""

    PANEL = "/settings/fragments/panel"

    def admin_client(self):
        self.make_user("admin", is_admin=True)
        self.login("admin")
        return self.client

    def test_settings_reports_the_detected_peer_and_trust_state(self):
        body = self.admin_client().get(self.PANEL).text
        self.assertIn("This request came from testclient.", body)
        self.assertIn(f'value="{TRUSTED_PROXY_IPS_DEFAULT}"', body)
        self.assertNotIn("ARE arriving", body)

    def test_forwarded_headers_from_an_untrusted_peer_are_reported(self):
        """The silent misconfiguration: headers arriving, peer not trusted, so
        they are ignored and every user collapses onto one address."""
        client = self.admin_client()
        body = client.get(self.PANEL, headers={"X-Forwarded-For": "203.0.113.9"}).text
        self.assertIn("Forwarded headers ARE arriving but are being ignored", body)

    def test_trusted_proxy_ips_is_saveable_and_takes_effect(self):
        client = self.admin_client()
        resp = client.post("/settings", json={"trusted_proxy_ips": "127.0.0.1/32, 172.18.0.0/16"})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertIn('value="127.0.0.1/32, 172.18.0.0/16"', client.get(self.PANEL).text)
        # And the parsed value is what client_ip actually consults.
        nets = auth.parse_trusted_networks("127.0.0.1/32, 172.18.0.0/16")
        self.assertEqual(len(nets), 2)

    def test_a_secret_is_still_never_returned(self):
        """The diagnostics sit alongside the redaction that is the whole point of
        this surface; assert it did not regress."""
        client = self.admin_client()
        client.post("/settings", json={"tmdb_api_key": "super-secret-value"})
        body = client.get(self.PANEL).text
        self.assertIn('name="clear:tmdb_api_key"', body)
        self.assertNotIn("super-secret-value", body)


class AdminSessionScopingTests(RegressionTestCase):
    """An admin looking at one account's session list must not be able to act on
    a different account's session by posting its id."""

    def test_revoking_another_accounts_session_is_refused(self):
        admin_id = self.make_user("admin", is_admin=True)
        victim_id = self.make_user("victim")
        victim_session = asyncio.run(auth.create_session(victim_id))
        self.login("admin")

        resp = self.client.post(
            f"/api/admin/users/{admin_id}/sessions/revoke",
            json={"session_id": victim_session},
        )
        self.assertEqual(resp.status_code, 404)
        # Still alive: the id was real, it just wasn't on that account.
        self.assertIsNotNone(asyncio.run(auth.validate_session(victim_session)))

    def test_revoking_a_session_that_is_on_the_account_works(self):
        victim_id = self.make_user("victim")
        victim_session = asyncio.run(auth.create_session(victim_id))
        self.make_user("admin", is_admin=True)
        self.login("admin")

        resp = self.client.post(
            f"/api/admin/users/{victim_id}/sessions/revoke",
            json={"session_id": victim_session},
        )
        self.assertTrue(resp.json()["ok"])
        self.assertIsNone(asyncio.run(auth.validate_session(victim_session)))


class ShareTokenComparisonTests(RegressionTestCase):
    """The one comparison this app makes against a secret is constant-time."""

    def test_a_valid_token_still_resolves(self):
        user_id = self.make_user()
        row = asyncio.run(share_links.get_or_create(user_id))
        resolved = asyncio.run(share_links.resolve_by_token(row["token"]))
        self.assertIsNotNone(resolved)
        self.assertEqual(int(resolved["user_id"]), user_id)

    def test_a_wrong_token_resolves_to_nothing(self):
        self.make_user()
        for candidate in ("", "not-a-token", "x" * 43):
            with self.subTest(token=candidate):
                self.assertIsNone(asyncio.run(share_links.resolve_by_token(candidate)))

    def test_resolution_goes_through_compare_digest(self):
        user_id = self.make_user()
        row = asyncio.run(share_links.get_or_create(user_id))
        with patch("app.calendar.share_links.secrets.compare_digest", return_value=False) as spy:
            self.assertIsNone(asyncio.run(share_links.resolve_by_token(row["token"])))
        spy.assert_called_once()


class HandshakeStartThrottleTests(RegressionTestCase):
    """/auth/plex/start and /auth/trakt/start are unauthenticated, mint a row, and
    (for Plex) call out to plex.tv. They share one per-address budget so nobody
    gets a second one by alternating providers."""

    def setUp(self):
        super().setUp()
        # The first-run gate refuses everything but setup until an account
        # exists, which would otherwise mask what these tests are measuring.
        self.make_user("resident")

    def test_plex_start_is_throttled(self):
        with patch("app.auth.plex.request_pin",
                   return_value={"id": 1, "code": "ABCD"}) as pin:
            for _ in range(auth.HANDSHAKE_MAX_ATTEMPTS):
                self.assertEqual(self.client.get("/auth/plex/start").status_code, 200)
            blocked = self.client.get("/auth/plex/start")
        self.assertEqual(blocked.status_code, 200)
        self.assertIn("Too many sign-in attempts", blocked.text)
        self.assertNotIn('hx-post="/auth/plex/poll"', blocked.text)
        # And the limit is applied BEFORE the outbound call, not after it.
        self.assertEqual(pin.call_count, auth.HANDSHAKE_MAX_ATTEMPTS)

    def test_the_budget_is_shared_across_providers(self):
        save_settings(Settings(
            trakt_client_id="cid", trakt_client_secret="sec",
            public_base_url="https://testserver",
        ))
        with patch("app.auth.plex.request_pin", return_value={"id": 1, "code": "ABCD"}):
            for _ in range(auth.HANDSHAKE_MAX_ATTEMPTS):
                self.client.get("/auth/plex/start")
        # Trakt's start route reads the same counter, so it is already spent.
        resp = self.client.get("/auth/trakt/start", follow_redirects=False)
        self.assertEqual(resp.status_code, 429)

    def test_a_normal_flow_is_nowhere_near_the_limit(self):
        """A person retrying a flaky popup a few times must never hit this."""
        with patch("app.auth.plex.request_pin", return_value={"id": 1, "code": "ABCD"}):
            for _ in range(5):
                self.assertEqual(self.client.get("/auth/plex/start").status_code, 200)


class CookiePolicyDetectionTests(RegressionTestCase):
    """cookie_secure is resolved from evidence at onboarding rather than asked as
    a question, because the operator most likely to answer it wrong is the one
    behind a TLS-terminating proxy — where the app's own view of the scheme is
    plain HTTP and the browser's is not."""

    ONBOARD = {"username": "josh", "password": PASSWORD, "password_confirm": PASSWORD}

    def onboard(self, base_url, origin):
        client = TestClient(app, base_url=base_url, headers={"Origin": origin})
        try:
            resp = client.post("/onboarding", json=self.ONBOARD)
        finally:
            client.close()
        return resp

    def test_a_browser_on_https_gets_secure_cookies(self):
        resp = self.onboard("https://shows.example.com", "https://shows.example.com")
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(load_settings().cookie_secure, "always")
        self.assertIn("__Host-tns_session", resp.headers.get("set-cookie", ""))

    def test_a_browser_on_plain_http_gets_a_usable_cookie(self):
        resp = self.onboard("http://localhost:8000", "http://localhost:8000")
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(load_settings().cookie_secure, "never")
        cookie = resp.headers.get("set-cookie", "")
        self.assertIn("tns_session", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertNotIn("__Host-", cookie)

    def test_a_tls_terminating_proxy_is_detected_through_a_plain_http_hop(self):
        """THE CASE THIS EXISTS FOR. The request reaches the app over plain HTTP —
        request.url.scheme says "http" and X-Forwarded-Proto is ignored because
        trusted_proxy_ips is still at its default — but the browser's Origin says
        https, so the cookie must stay Secure."""
        # Traefik forwards the original Host, so the host matches and only the
        # scheme differs — which is the whole signature of TLS termination.
        client = TestClient(app, base_url="http://shows.example.com", headers={
            "Origin": "https://shows.example.com",
            "X-Forwarded-Proto": "https",
        })
        try:
            resp = client.post("/onboarding", json=self.ONBOARD)
        finally:
            client.close()
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(load_settings().cookie_secure, "always")
        self.assertIn("Secure", resp.headers.get("set-cookie", ""))

    def test_an_unreported_scheme_fails_closed(self):
        """No Origin and no Referer — the middleware still admits the request on
        Sec-Fetch-Site, and an unknown scheme must resolve to the safe answer."""
        client = TestClient(app, base_url="http://localhost:8000",
                            headers={"Sec-Fetch-Site": "same-origin"})
        try:
            resp = client.post("/onboarding", json=self.ONBOARD)
        finally:
            client.close()
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(load_settings().cookie_secure, "always")

    def test_the_first_cookie_already_carries_the_resolved_policy(self):
        """Resolved BEFORE the session is issued, or the operator's very first
        cookie would be the wrong one and they would be locked out anyway."""
        resp = self.onboard("http://localhost:8000", "http://localhost:8000")
        self.assertNotIn("Secure", resp.headers.get("set-cookie", ""))

    def test_detection_is_a_pure_function_of_the_headers(self):
        from starlette.requests import Request as StarletteRequest

        def req(headers):
            scope = {"type": "http", "method": "GET", "path": "/", "headers": [
                (k.lower().encode(), v.encode()) for k, v in headers.items()
            ]}
            return StarletteRequest(scope)

        cases = [
            ({"origin": "http://localhost:8000"}, "never"),
            ({"origin": "https://shows.example.com"}, "always"),
            # Origin wins over Referer when both are present.
            ({"origin": "https://a.example", "referer": "http://b.example/x"}, "always"),
            ({"referer": "http://localhost:8000/login"}, "never"),
            # A stripped Origin arrives as the literal "null" and says nothing.
            ({"origin": "null"}, "always"),
            ({}, "always"),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(auth.detect_cookie_secure(req(headers)), expected)


class FreshProxiedInstallCanOnboardTests(RegressionTestCase):
    """A brand-new instance behind Traefik could not be set up at all: the origin
    check compared the browser's https Origin against an expected origin built
    with the scheme the app saw (http), so every mutating request was refused —
    and the `public_base_url` that would have fixed it lives behind the admin
    login that onboarding was supposed to create."""

    ONBOARD = {"username": "josh", "password": PASSWORD, "password_confirm": PASSWORD}

    def proxied_client(self, **headers):
        base = {
            "Origin": "https://shows.example.com",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.7",
        }
        base.update(headers)
        return TestClient(app, base_url="http://shows.example.com", headers=base)

    def test_onboarding_succeeds_behind_an_unconfigured_proxy(self):
        client = self.proxied_client()
        try:
            self.assertEqual(client.post("/onboarding", json=self.ONBOARD).status_code, 204)
        finally:
            client.close()

    def test_a_genuinely_foreign_origin_is_still_refused(self):
        """The host comparison is what does the work, and it is untouched."""
        client = self.proxied_client(Origin="https://evil.example.com")
        try:
            resp = client.post("/onboarding", json=self.ONBOARD)
        finally:
            client.close()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["reason"], "cross_origin")

    def test_a_configured_base_url_is_still_exact(self):
        """Setting public_base_url tightens the check back to one origin — scheme
        included. That is the reason to set it."""
        save_settings(Settings(public_base_url="https://shows.example.com"))
        from starlette.requests import Request as StarletteRequest
        from app import authz

        def req(host):
            return StarletteRequest({"type": "http", "method": "POST", "path": "/",
                                     "headers": [(b"host", host.encode())]})

        settings = load_settings()
        self.assertEqual(
            authz.acceptable_origins(req("shows.example.com"), settings),
            {"https://shows.example.com"},
        )
        # And a plain-http origin on the same host no longer passes.
        self.assertIsNotNone(authz.cross_origin_reason(
            StarletteRequest({"type": "http", "method": "POST", "path": "/", "headers": [
                (b"host", b"shows.example.com"),
                (b"origin", b"http://shows.example.com"),
            ]}), settings,
        ))

    def test_an_unconfigured_instance_accepts_either_scheme_for_its_own_host(self):
        from starlette.requests import Request as StarletteRequest
        from app import authz

        request = StarletteRequest({"type": "http", "method": "POST", "path": "/",
                                    "headers": [(b"host", b"shows.example.com")]})
        self.assertEqual(
            authz.acceptable_origins(request, load_settings()),
            {"http://shows.example.com", "https://shows.example.com"},
        )


class CookieMismatchWarningTests(RegressionTestCase):
    """Onboarding gets it right on its own, so a mismatch means the deployment
    moved or settings.json was hand-edited. Either way it must not present as a
    wrong password."""

    def test_a_secure_cookie_the_browser_dropped_is_reported(self):
        self.make_user()
        save_settings(Settings(cookie_secure="always"))
        body = self.client.get("/login").text
        self.assertIn('hx-get="/login/cookie-check"', body)
        # The browser kept the page's probe: nothing to say.
        self.assertNotIn("cookieWarning", self.client.get("/login/cookie-check").text)
        # It did not: the warning, before anybody types a password.
        self.client.cookies.clear()
        self.assertIn("cookieWarning", self.client.get("/login/cookie-check").text)

    def test_a_non_secure_policy_is_never_reported(self):
        self.make_user()
        save_settings(Settings(cookie_secure="never"))
        self.client.cookies.clear()
        self.assertNotIn("cookieWarning", self.client.get("/login/cookie-check").text)

    def test_the_settings_panel_draws_the_policy(self):
        """The Settings panel warns when cookie_secure disagrees with the scheme
        the browser is on, so the value has to be drawn — and must never be
        mistaken for a secret and left out."""
        self.make_user("admin", is_admin=True)
        self.login("admin")
        body = self.client.get("/settings/fragments/panel").text
        self.assertRegex(body, r'<option value="always" selected>Always')


class CookieSecureEditingTests(RegressionTestCase):
    """cookie_secure is editable in Settings now, with the one self-locking
    change refused rather than the whole field left hand-edited.

    The refusal is judged by settings_routes._cookie_secure_error on the BROWSER's scheme,
    so it is unit-tested with constructed requests the same way
    detect_cookie_secure is — a TestClient will not carry an authenticated Secure
    cookie over an http:// base_url, which is the very situation under test."""

    @staticmethod
    def _req(origin=None):
        from starlette.requests import Request as StarletteRequest
        headers = [(b"origin", origin.encode())] if origin else []
        return StarletteRequest({"type": "http", "method": "POST", "path": "/settings",
                                 "headers": headers})

    def _error(self, mode, origin):
        from app import settings_routes
        return settings_routes._cookie_secure_error(Settings(cookie_secure=mode), self._req(origin))

    def test_always_from_a_genuinely_http_browser_is_refused(self):
        """The lockout: a Secure cookie is discarded by an http browser, so the
        admin's next request has no session and they can't undo it."""
        err = self._error("always", "http://box.local:8000")
        self.assertIsNotNone(err)
        self.assertIn("http://", err)

    def test_always_behind_a_tls_proxy_is_allowed(self):
        """The browser's Origin is https even though the app sees http on the
        internal hop — the reverse-proxy case "always" exists for."""
        self.assertIsNone(self._error("always", "https://shows.example.com"))

    def test_always_with_no_origin_is_allowed(self):
        """Can't prove the browser is on http, so don't block — though a real save
        from the Settings screen always carries an Origin."""
        self.assertIsNone(self._error("always", None))

    def test_auto_and_never_over_http_are_allowed(self):
        """Neither yields a Secure cookie, so neither can lock anyone out."""
        self.assertIsNone(self._error("auto", "http://box.local:8000"))
        self.assertIsNone(self._error("never", "http://box.local:8000"))

    def test_an_unknown_value_is_rejected(self):
        self.assertIsNotNone(self._error("sometimes", "https://shows.example.com"))

    def test_a_valid_change_saves_through_the_route(self):
        """End to end on the authenticated https client — the normal path."""
        self.make_user("admin", is_admin=True)
        self.login("admin")
        resp = self.client.post("/settings", json={"cookie_secure": "auto"})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(load_settings().cookie_secure, "auto")

    def test_an_unknown_value_is_rejected_through_the_route(self):
        self.make_user("admin", is_admin=True)
        self.login("admin")
        resp = self.client.post("/settings", json={"cookie_secure": "sometimes"})
        # Drawn beside Save at 200: a statused refusal is never swapped.
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Session cookie security must be one of", resp.text)
        self.assertEqual(load_settings().cookie_secure, "always")  # unchanged


class ShareLinkSelectorTests(RegressionTestCase):
    """The Share panel's link select is PRESENTATION: it picks which URL you are
    handed. Every published form keeps answering, because a link already given to
    somebody must not break because its owner later looked at a different one."""

    def setUp(self):
        super().setUp()
        # Must match the test client's own origin: a configured public_base_url
        # pins the same-origin check to exactly that one, which is the point of
        # setting it.
        save_settings(Settings(public_base_url="https://testserver"))
        self.user_id = self.make_user("josh")
        self.login("josh")

    def row(self):
        return asyncio.run(share_links.get_or_create(self.user_id))

    def urls(self):
        return share_links.generated_urls(self.row(), "josh", "https://testserver")

    def active(self, kind):
        resp = self.client.post("/calendar/share/kind", json={"kind": kind})
        self.assertEqual(resp.status_code, 200, resp.text)
        return self.row()

    def test_selecting_a_form_changes_only_which_one_is_offered(self):
        row = self.active("username")
        self.assertEqual(row["preferred_kind"], "username")
        # Every form stays live; the selection is about what the panel shows.
        self.assertEqual((row["enabled_token"], row["enabled_username"], row["enabled_slug"]),
                         (1, 1, 1))
        self.assertTrue(self.urls()["username"])
        self.assertTrue(self.urls()["token"])

    def test_switching_forms_does_not_break_a_link_already_shared(self):
        """The regression this guards: handing someone a token link, then
        switching the panel to the username form, must not 404 the link they
        already have."""
        token = self.active("token")["token"]
        self.assertEqual(self.client.get(f"/s/{token}").status_code, 200)
        self.active("username")
        self.assertEqual(self.client.get(f"/s/{token}").status_code, 200)
        self.assertEqual(self.client.get("/u/josh").status_code, 200)

    def test_every_form_answers_once_it_can_resolve(self):
        self.active("token")
        self.client.post("/calendar/share/slug", json={"slug": "my-shows"})
        token = self.row()["token"]
        for kind, path in (("token", f"/s/{token}"),
                           ("username", "/u/josh"),
                           ("slug", "/c/my-shows")):
            with self.subTest(kind=kind):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_an_unknown_kind_is_refused(self):
        for body in ({"kind": "carrier-pigeon"}, {}):
            with self.subTest(body=body):
                resp = self.client.post("/calendar/share/kind", json=body)
                self.assertIn("That is not a kind of link.", resp.text)
        self.assertEqual(self.row()["preferred_kind"], "token")

    def test_the_slug_form_needs_a_name_before_it_resolves(self):
        """Selecting "custom name" with no name saved yet is a normal state, not
        an error — the panel shows the name field and an empty link box."""
        self.assertEqual(self.active("slug")["preferred_kind"], "slug")
        self.assertIsNone(self.urls()["slug"])
        self.client.post("/calendar/share/slug", json={"slug": "my-shows"})
        self.assertEqual(self.urls()["slug"], "https://testserver/c/my-shows")
        self.assertEqual(self.client.get("/c/my-shows").status_code, 200)

    def test_it_needs_a_session_like_every_other_share_route(self):
        self.client.post("/logout", json={})
        self.assertIn(self.client.post("/calendar/share/kind", json={"kind": "token"}).status_code,
                      (401, 403))


class RetiredSlugTests(RegressionTestCase):
    """Changing a slug frees the old name, and `/c/<old>` links are already out in
    the world by then — so the name has to be blocked from being claimed by
    somebody else, exactly as a deleted account's is."""

    def setUp(self):
        super().setUp()
        save_settings(Settings(public_base_url="https://testserver"))
        self.user_id = self.make_user("josh")
        self.login("josh")

    def set_slug(self, slug):
        """A name set through the Share panel's own route, answered as {ok, error,
        custom_slug} so the assertions below read the same as they always have.

        CLEARING goes through the data layer instead: the panel's name box writes
        nothing when it is emptied, so a cleared slug is
        share_links.set_custom_slug's rule to hold rather than a route's."""
        if not slug:
            error = asyncio.run(share_links.set_custom_slug(self.user_id, ""))
        else:
            text = self.client.post("/calendar/share/slug", json={"slug": slug}).text
            error = None if "Saved." in text else text
        row = asyncio.run(share_links.get(self.user_id))
        return {"ok": error is None, "error": error, "custom_slug": row["custom_slug"]}

    def retired(self):
        rows = asyncio.run(db.fetch_all(
            "SELECT kind, value, user_id FROM retired_identifiers ORDER BY value"))
        return [(r["kind"], r["value"], r["user_id"]) for r in rows]

    def test_replacing_a_slug_retires_the_old_one(self):
        self.set_slug("first-name")
        self.set_slug("second-name")
        self.assertIn(("slug", "first-name", self.user_id), self.retired())
        self.assertEqual(self.client.get("/c/second-name").status_code, 200)

    def test_a_retired_slug_keeps_working_for_its_owner(self):
        """404ing links that are already circulating is the same harm retiring
        them was meant to prevent — so an old /c/ link follows its owner."""
        self.set_slug("first-name")
        self.set_slug("second-name")
        old = self.client.get("/c/first-name")
        self.assertEqual(old.status_code, 200)
        # And it is the same calendar the current slug serves, not a stale copy.
        self.assertEqual(old.text, self.client.get("/c/second-name").text)

    def test_several_retired_slugs_all_keep_working(self):
        for name in ("one-name", "two-name", "three-name"):
            self.set_slug(name)
        for name in ("one-name", "two-name", "three-name"):
            with self.subTest(slug=name):
                self.assertEqual(self.client.get(f"/c/{name}").status_code, 200)

    def test_a_deleted_accounts_slug_resolves_to_nothing(self):
        """Retirement without an owner is a pure block: there is nobody left to
        follow, and the name must not fall through to somebody else."""
        self.set_slug("first-name")
        asyncio.run(db.execute(
            "INSERT OR REPLACE INTO retired_identifiers (kind, value, retired_at, user_id) "
            "VALUES ('slug', 'gone-name', ?, NULL)", (db.now(),),
        ))
        self.assertEqual(self.client.get("/c/gone-name").status_code, 404)

    def test_another_account_cannot_claim_a_retired_slug(self):
        """The whole point: otherwise the next claimant inherits an audience."""
        self.set_slug("first-name")
        self.set_slug("second-name")
        self.client.post("/logout", json={})
        self.make_user("someone-else")
        self.login("someone-else")
        body = self.set_slug("first-name")
        self.assertFalse(body["ok"])
        self.assertIn("taken", body["error"].lower())

    def test_the_owner_can_take_their_own_slug_back(self):
        """Switching away and back must not permanently cost someone their own
        name — the block is against everybody else."""
        self.set_slug("first-name")
        self.set_slug("second-name")
        body = self.set_slug("first-name")
        self.assertTrue(body["ok"], body.get("error"))
        self.assertEqual(body["custom_slug"], "first-name")
        self.assertEqual(self.client.get("/c/first-name").status_code, 200)
        # And the block is gone, not left contradicting the live slug.
        self.assertNotIn(("slug", "first-name", self.user_id), self.retired())

    def test_clearing_a_slug_retires_it_and_stops_serving(self):
        """Clearing turns the slug form OFF, so nothing resolves — but the name
        stays blocked so nobody else inherits the links."""
        self.set_slug("first-name")
        self.set_slug("")
        self.assertIn(("slug", "first-name", self.user_id), self.retired())
        self.assertEqual(self.client.get("/c/first-name").status_code, 404)

    def test_setting_the_same_slug_again_retires_nothing(self):
        self.set_slug("first-name")
        self.set_slug("first-name")
        self.assertEqual(self.retired(), [])


class PerUserEmojiMapTests(RegressionTestCase):
    """The map renders into ONE account's Discord posts. It was app-wide, so
    importing a roster on any tracker account registered its networks into the
    operator's map and one person's emoji went out in everybody's posts."""

    def tracker(self, name):
        user_id = self.make_user(name, distrakt_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=f"uuid-{name}")))
        return user_id

    def test_a_new_account_gets_an_empty_map_not_an_inherited_one(self):
        from app import distrakt as store
        # Even with an app-wide-looking value present, nothing seeds from it.
        emojis, default_emoji = asyncio.run(store.get_emoji_prefs(self.tracker("fresh")))
        self.assertEqual(emojis, {})
        self.assertEqual(default_emoji, store.DEFAULT_EMOJI)

    def test_registering_networks_writes_only_the_acting_users_map(self):
        from app import distrakt as store
        mine, theirs = self.tracker("mine"), self.tracker("theirs")
        asyncio.run(store.register_networks(mine, ["HBO", "AMC", ""]))
        self.assertEqual(
            set(asyncio.run(store.get_emoji_prefs(mine))[0]), {"HBO", "AMC"})
        # The other account is untouched — the actual reported bug.
        self.assertEqual(asyncio.run(store.get_emoji_prefs(theirs))[0], {})

    def test_settings_no_longer_carries_the_map_at_all(self):
        """Removed rather than left unused: a lingering app-wide field is
        something a later caller reaches for by mistake."""
        self.assertNotIn("network_emojis", Settings().to_dict())
        self.assertNotIn("default_network_emoji", Settings().to_dict())

    def test_the_map_round_trips_through_a_backup(self):
        from app import distrakt as store
        user_id = self.tracker("owner")
        asyncio.run(store.set_emoji_prefs(user_id, {"HBO": ":hbo:"}, ":film:"))
        doc = asyncio.run(store.export_user_data(user_id))
        asyncio.run(store.set_emoji_prefs(user_id, {}, ":tv:"))
        asyncio.run(store.restore_user_data(user_id, doc))
        self.assertEqual(asyncio.run(store.get_emoji_prefs(user_id)),
                         ({"HBO": ":hbo:"}, ":film:"))

    def test_an_older_backup_leaves_the_map_alone(self):
        """A version-1 export predates the map and says nothing about it. Reading
        that silence as "delete it" would destroy data the file never described."""
        from app import distrakt as store
        user_id = self.tracker("owner")
        asyncio.run(store.set_emoji_prefs(user_id, {"HBO": ":hbo:"}, ":film:"))
        doc = asyncio.run(store.export_user_data(user_id))
        doc["schema"] = 1
        del doc["distrakt_prefs"]
        asyncio.run(store.restore_user_data(user_id, doc))
        self.assertEqual(asyncio.run(store.get_emoji_prefs(user_id)),
                         ({"HBO": ":hbo:"}, ":film:"))

    def test_the_editor_is_reachable_without_being_an_admin(self):
        """The editor is a FORM now: its rows travel as one flat field per value,
        named by position, and the answer is the rows the server made of them.
        There is no GET beside it any more — the rows come with the page."""
        user_id = self.tracker("plain")
        self.login("plain")
        resp = self.client.post("/api/distrakt/emojis", json={
            "network-0": "HBO", "emoji-0": ":hbo:", "default_network_emoji": ":film:",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="network-0" value="HBO"', resp.text)
        from app import distrakt as store
        self.assertEqual(asyncio.run(store.get_emoji_prefs(user_id)),
                         ({"HBO": ":hbo:"}, ":film:"))

    def test_a_row_nobody_filled_in_is_dropped_rather_than_stored(self):
        """The editor always draws a blank row to type into, and one left
        untouched is not an error to report — it is a row nobody filled in. A
        map cannot be malformed any more: it arrives as flat fields, so there is
        no shape for a caller to get wrong."""
        user_id = self.tracker("plain")
        self.login("plain")
        self.client.post("/api/distrakt/emojis", json={
            "network-0": "HBO", "emoji-0": ":hbo:",
            "network-1": "  ", "emoji-1": ":nobody:",
        })
        from app import distrakt as store
        self.assertEqual(asyncio.run(store.get_emoji_prefs(user_id))[0], {"HBO": ":hbo:"})


class DistraktDetailsTests(RegressionTestCase):
    """The tracker's own details panel. A separate route from the calendar's
    because that one is CALENDAR_APPROVED — a tracker user need not be calendar
    approved — and because this one answers a question the calendar has no
    business knowing: which episodes THIS person has watched.

    It answers with the panel itself (templates/_details.html), so what is
    asserted below is what the viewer is shown; tests.support.DetailsPanel reads
    the ticks and the links back out of the markup."""

    DETAILS = {
        "title": "Silo", "year": 2026, "overview": "o", "status": "Returning",
        "network": "Apple TV", "runtime": 50, "genres": ["Drama"], "rating": 8.2,
        "trailer": "", "homepage": "", "season": 3, "cast": [],
        "episodes": [{"number": n, "title": f"Ep {n}", "rating": None, "air_display": "Jul 1"}
                     for n in range(1, 6)],
    }

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            public_base_url="https://testserver", trakt_client_id="cid", trakt_access_token="tok",
        ))
        self.user_id = self.tracker("josh")

    def tracker(self, name):
        user_id = self.make_user(name, distrakt_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=f"uuid-{name}",
            access_token="user-token")))
        # On the viewer's own list rather than on a month: that is where a season
        # part-way through lives, and it is the first place the route looks.
        asyncio.run(distrakt.add_user_record(user_id, {
            "ids": {"trakt": 7, "tmdb": 1, "slug": "silo"}, "season": 3, "title": "Silo",
            "network": "Apple TV", "media": "show", "kind": distrakt.RecordKind.KEEPUP,
        }))
        return user_id

    # The roster row is keyed on its shared id (tmdb 1), which is also how a
    # request names it — NOT by the Trakt id the lookup is then placed with.
    KEY = "show:tmdb:1"

    # The shape the column actually holds since progress started carrying the
    # date each episode was watched. Writing the pre-dates list here instead is
    # what let this route return an empty set to every real user while its tests
    # stayed green.
    WATCHED = '{"1": "2026-07-01T00:00:00Z", "2": "2026-07-02T00:00:00Z", "3": ""}'

    def details(self, watched=WATCHED):
        if watched is not None:
            asyncio.run(db.execute(
                "INSERT OR REPLACE INTO distrakt_show_progress "
                "(user_id, media, match_source, match_id, season, watched_episodes_json, "
                "trakt_id) VALUES (?,?,?,?,?,?,?)",
                (self.user_id, "show", "tmdb", "1", 3, watched, 7)))
        self.login("josh")
        with patch("app.providers.trakt.detail.fetch_details", return_value=self.DETAILS):
            return DetailsPanel(self.client.get(
                f"/distrakt/fragments/details?key={self.KEY}&season=3").text)

    def test_it_returns_the_episodes_and_this_users_watched_set(self):
        panel = self.details()
        self.assertEqual(panel.episodes, [1, 2, 3, 4, 5])
        self.assertEqual(panel.watched, [1, 2, 3])

    def test_a_pre_dates_progress_row_still_reads(self):
        """Rows written before episodes carried dates hold a bare list. They are
        read as dates-unknown everywhere else; this route is not an exception."""
        self.assertEqual(self.details(watched="[1,2,3]").watched, [1, 2, 3])

    def test_the_links_are_built_from_the_roster_and_not_from_the_caller(self):
        """A tick opens the service holding the record, and the caller must not
        be able to point it somewhere else: everything a link is built from comes
        off the row this account can already see."""
        self.assertEqual(
            self.details().tick_links["trakt"],
            "https://app.trakt.tv/shows/7?season=3&view=episode&episode=1")
        self.login("josh")
        with patch("app.providers.trakt.detail.fetch_details", return_value=self.DETAILS):
            spoofed = DetailsPanel(self.client.get(
                f"/distrakt/fragments/details?key={self.KEY}&season=3&slug=evil").text)
        self.assertNotIn("evil", spoofed.html)
        self.assertEqual(spoofed.tick_links["trakt"],
                         "https://app.trakt.tv/shows/7?season=3&view=episode&episode=1")

    def test_one_users_watched_set_is_invisible_to_another(self):
        self.details()
        self.client.post("/logout", json={})
        self.tracker("other")
        self.login("other")
        with patch("app.providers.trakt.detail.fetch_details", return_value=self.DETAILS):
            panel = DetailsPanel(self.client.get(
                f"/distrakt/fragments/details?key={self.KEY}&season=3").text)
        self.assertEqual(panel.watched, [])

    def test_no_progress_row_is_an_empty_set_not_an_error(self):
        panel = self.details(watched=None)
        self.assertEqual(panel.episodes, [1, 2, 3, 4, 5])
        self.assertEqual(panel.watched, [])

    def test_it_needs_the_tracker_grant(self):
        self.client.post("/logout", json={})
        self.make_user("plain", calendar_approved=True)
        self.login("plain")
        self.assertIn(
            self.client.get(
                f"/distrakt/fragments/details?key={self.KEY}&season=3").status_code,
            (401, 403))

    def test_a_missing_season_is_refused(self):
        """DRAWN rather than signalled with a status, like every fragment here."""
        self.login("josh")
        self.assertIn("Missing season", self.client.get(
            f"/distrakt/fragments/details?key={self.KEY}").text)


class SiteHeaderTests(RegressionTestCase):
    """One header across the calendar, the month picker, and the tracker. They
    had drifted into three different bars, and the admin calendar's had swollen
    onto a second row."""

    PAGES = ("/", "/?month=7&year=2026", "/distrakt", "/me", "/admin")

    def setUp(self):
        super().setUp()
        # One source's calendar is public files needing no credential, so it is
        # readable on an instance with nothing else set up — and these tests
        # render the calendar page for its HEADER. Switched off so the page is
        # drawn without going and reading a month.
        save_settings(Settings(public_base_url="https://testserver",
                               simkl_public_calendar_enabled=False))
        self.user_id = self.make_user("josh", is_admin=True, distrakt_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id="uuid-josh")))
        self.login("josh")

    def header(self, path):
        body = self.client.get(path, headers={"accept": "text/html"}).text
        self.assertIn("<header", body, path)
        return body, body.split("<header", 1)[1].split("</header>", 1)[0]

    def test_every_page_uses_the_same_header_component(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                _, head = self.header(path)
                self.assertIn('class="hero', head)
                self.assertIn("hero-actions", head)
                # The menu is a popover the browser drives, so there is no
                # header script to look for any more — the button and the
                # panel it names are the whole of it.
                self.assertIn('popovertarget="navMenuItems"', head)
                self.assertIn('id="navMenuItems" popover', head)

    def test_every_page_offers_account_and_sign_out(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                _, head = self.header(path)
                self.assertIn("nav-menu", head)
                self.assertIn('href="/me"', head)
                self.assertIn('hx-post="/logout"', head)

    def test_admin_only_appears_for_admins(self):
        for path in self.PAGES:
            with self.subTest(path=path, admin=True):
                self.assertIn('href="/admin"', self.header(path)[1])
        self.client.post("/logout", json={})
        self.make_user("plain", distrakt_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=asyncio.run(auth.find_user_by_username("plain"))["id"],
            provider="trakt", provider_user_id="uuid-plain")))
        self.login("plain")
        # /admin is excluded: it is not a page a non-admin can reach at all, and
        # following its redirect would test /me twice rather than /admin once.
        self.assertEqual(
            self.client.get("/admin", headers={"accept": "text/html"},
                            follow_redirects=False).status_code, 303)
        for path in (p for p in self.PAGES if p != "/admin"):
            with self.subTest(path=path, admin=False):
                self.assertNotIn('href="/admin"', self.header(path)[1])

    def test_the_admin_calendar_bar_carries_one_menu_not_three_buttons(self):
        """Account + Settings + Admin as separate pills is what pushed the header
        onto a second row for an administrator."""
        _, head = self.header("/?month=7&year=2026")
        actions = head.split('class="hero-actions"', 1)[1]
        menu = actions.split("nav-menu-items", 1)[1]
        entries = ('href="/me"', 'hx-get="/settings/fragments/panel"', 'href="/admin"')
        # All three destinations live inside the menu...
        for entry in entries:
            self.assertIn(entry, menu, entry)
        # ...and none of them is also a top-level pill in the bar itself.
        bar = actions.split("nav-menu-items", 1)[0]
        for entry in entries:
            self.assertNotIn(entry, bar, entry)

    def test_the_tracker_links_back_to_the_calendar_from_the_right(self):
        """Mirrors where 🧵 Distrakt sits on the calendar."""
        body, head = self.header("/distrakt")
        self.assertIn("📅 Calendar", head)
        self.assertLess(head.index("hero-actions"), head.index("📅 Calendar"))

    def test_endpoint_labels_are_short_enough_for_one_line(self):
        """The selector sizes to its longest option, and the old label was half
        again as long as any other."""
        from app.endpoints import endpoint_choices
        for ep in endpoint_choices():
            with self.subTest(endpoint=ep.key):
                self.assertLessEqual(len(ep.label), 16, ep.label)


class SharedAddressLockoutTests(RegressionTestCase):
    """Five wrong passwords on ONE account used to lock out EVERY account from
    that address — administrator included, with the generic failure message and
    nothing in the log. On a single-user instance that is self-inflicted; behind a
    reverse proxy, where every user shares one apparent address, it is an outage."""

    def test_one_accounts_failures_do_not_lock_out_another(self):
        self.make_user("josh")
        self.make_user("test")
        # Well past the per-username limit for `test`.
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS + 3, username="test")
        self.assertRefused(self.login("test"))    # that account IS locked
        self.assertSignedIn(self.login("josh"))   # this one is not

    def test_the_per_username_limit_still_bites(self):
        self.make_user("josh")
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS)
        self.assertRefused(self.login())

    def test_the_address_limit_still_exists_for_spraying(self):
        """Raising the threshold must not remove it: many usernames tried from one
        address is the attack it is actually for."""
        self.make_user("josh")
        for n in range(auth.LOGIN_IP_MAX_ATTEMPTS):
            self.login(f"nobody{n}", "wrong-password")
        self.assertTrue(asyncio.run(auth.check_lockout(
            "ip", "testclient",
            max_attempts=auth.LOGIN_IP_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS,
        )))
        self.assertRefused(self.login())

    def test_a_lockout_is_logged_even_though_the_response_cannot_say_so(self):
        """Without this the operator sees "login is broken" and no explanation —
        which is exactly how this was reported."""
        self.make_user("josh")
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS)
        with self.assertLogs("app.auth.routes", level="WARNING") as captured:
            self.login()
        self.assertTrue(any("locked out" in line for line in captured.output))


class SharePanelDisclosureTests(RegressionTestCase):
    """public_base_url is an admin-only setting, so the "set a base URL" prompt is
    an unfixable complaint for everybody else."""

    PROMPT = "Set a public base URL in Settings before sharing a link"
    PANEL = "/calendar/fragments/share"

    def test_only_an_administrator_is_told_to_set_a_base_url(self):
        # The panel is drawn by the server from the session, so the decision is
        # made there rather than by a flag the page hands a script.
        save_settings(Settings(public_base_url=""))
        self.make_user("plain")
        self.login("plain")
        self.assertNotIn(self.PROMPT, self.client.get(self.PANEL).text)
        self.client.post("/logout", json={})
        self.make_user("boss", is_admin=True)
        self.login("boss")
        self.assertIn(self.PROMPT, self.client.get(self.PANEL).text)

    def test_everybody_is_told_why_the_link_box_is_empty(self):
        """The panel keeps telling the truth about a missing base URL — the empty
        box says why — and only the fix is an administrator's to be told about."""
        save_settings(Settings(public_base_url=""))
        self.make_user("josh")
        self.login("josh")
        self.assertIn('placeholder="No public base URL set"', self.client.get(self.PANEL).text)


class AccountPageDisclosureTests(RegressionTestCase):
    """/me is now linked from the calendar, so what it renders is read by every
    user rather than only whoever knew the URL."""

    def me(self):
        return self.client.get("/me").text

    def test_distrakt_is_not_mentioned_to_someone_without_the_grant(self):
        self.make_user("plain")
        self.login("plain")
        body = self.me()
        self.assertNotIn("distrakt", body.lower())

    def test_distrakt_is_shown_to_someone_who_has_the_grant(self):
        self.make_user("tracker", distrakt_approved=True)
        self.login("tracker")
        self.assertIn("distrakt", self.me().lower())

    def test_a_tracker_user_with_nothing_linked_is_told_what_to_connect(self):
        """The exact dead end: approved for the tracker, nothing that can read a
        watch history linked, so every tracker route refuses and nothing says why.

        THE SERVICES ARE NAMED FROM THE REGISTRY, so the sentence stays true as
        sources are added. It named Trakt alone while Trakt was the only service
        that could answer, and that sentence became wrong the moment a second one
        could: it told somebody signed in with the other service to go and link a
        service they may have chosen against.
        """
        self.make_user("tracker", distrakt_approved=True)
        self.login("tracker")
        body = self.me()
        self.assertIn("to finish setting up distrakt", body)
        self.assertIn("Trakt or Simkl", body)

    def test_the_notice_is_gone_once_any_of_them_is_linked(self):
        """It asks for A service, not for a particular one — so linking either
        answers it. Without this the page would go on demanding a link somebody
        has already made, from a service that is already answering."""
        user_id = self.make_user("tracker", distrakt_approved=True)
        self.link_identity(user_id, "simkl", 901, "simkl-token")
        self.login("tracker")
        self.assertNotIn("to finish setting up distrakt", self.me())

    def test_administrator_is_not_advertised_to_non_admins(self):
        self.make_user("plain")
        self.login("plain")
        self.assertNotIn("Administrator", self.me())
        self.client.post("/logout", json={})
        self.make_user("boss", is_admin=True)
        self.login("boss")
        self.assertIn("Administrator", self.me())

    def test_the_calendar_links_to_the_account_page_for_everyone(self):
        self.make_user("plain")
        self.login("plain")
        self.assertIn('href="/me"', self.client.get("/?month=1&year=2026").text)


class DeviceAuthAdoptsTheTokenTests(RegressionTestCase):
    """Re-authorizing in Settings renewed the app-wide token but never wrote a
    linked identity, so the "reconnect your Trakt account" notice could not be
    cleared by the one button that looked like it should clear it — and the
    tracker kept refusing the account for want of a Trakt link."""

    CODE = {"device_code": "dc", "user_code": "ABCD-1234",
            "verification_url": "https://trakt.tv/activate", "expires_in": 600, "interval": 5}
    TOKEN = {"access_token": "fresh-token", "refresh_token": "fresh-refresh",
             "expires_in": 7776000, "created_at": 1_700_000_000}
    ACCOUNT = {"id": 424242, "name": "Josh"}

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            public_base_url="https://testserver",
            trakt_client_id="cid", trakt_client_secret="sec",
        ))
        self.user_id = self.make_user("boss", is_admin=True)
        self.login("boss")
        asyncio.run(db.set_meta("trakt_reconnect_notice", "1"))

    def poll(self, lookup=None):
        """Start a pairing, then have Trakt approve it — both through the Settings
        panel's own routes, with Trakt stood in for."""
        with patch("app.auth.trakt.request_device_code", return_value=self.CODE):
            self.client.post("/settings/trakt/device/start", json={})
        lookup = lookup or patch("app.auth.trakt.fetch_account", return_value=self.ACCOUNT)
        with patch("app.auth.trakt.poll_device_token", return_value=self.TOKEN), lookup:
            return self.client.post("/settings/trakt/device/poll", json={})

    def notice_shown(self) -> bool:
        body = self.client.get("/settings/fragments/panel").text
        return 'id="traktReconnect" class="field" hidden' not in body

    def test_a_successful_authorization_links_the_admin_and_clears_the_notice(self):
        body = self.poll().text
        self.assertIn("Authorized, and linked to your account.", body)
        self.assertEqual(asyncio.run(db.get_meta("trakt_reconnect_notice")), "")
        rows = asyncio.run(auth.list_identities(self.user_id))
        self.assertEqual([r["provider"] for r in rows], ["trakt"])
        self.assertEqual(rows[0]["provider_user_id"], "424242")

    def test_the_linked_account_unblocks_the_tracker(self):
        """has_trakt_identity is the half of the distrakt gate that was missing."""
        # As a browser asks for it: the refusal is a redirect to /me for a
        # navigation, which is exactly the symptom — the tracker bouncing to the
        # account page with no explanation.
        browser = {"accept": "text/html"}
        asyncio.run(auth.set_distrakt_approved(self.user_id, True))
        self.login("boss")  # a fresh session, so the flag is re-read
        resp = self.client.get("/distrakt", headers=browser, follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/me"))
        self.poll()
        self.login("boss")
        self.assertEqual(self.client.get("/distrakt", headers=browser).status_code, 200)

    def test_a_lookup_failure_leaves_the_notice_up_and_still_saves_the_token(self):
        """Adoption is best effort — it must never fail an authorization that
        actually succeeded."""
        from app.auth import trakt as trakt_auth
        body = self.poll(patch("app.auth.trakt.fetch_account",
                               side_effect=trakt_auth.AccountLookupError("no"))).text
        self.assertIn("Authorized, but not linked to your login", body)
        self.assertEqual(asyncio.run(db.get_meta("trakt_reconnect_notice")), "1")
        self.assertEqual(load_settings().trakt_access_token, "fresh-token")

    def test_the_notice_is_derived_from_the_link_not_a_sticky_flag(self):
        """The prompt asks for a linked Trakt identity. Once the caller has one it
        must go away, whichever route did the linking and whether or not that
        route remembered to clear the stored flag — a "do this" that stays up
        after the thing is done is worse than no prompt at all."""
        self.assertTrue(self.notice_shown())
        # Link by a path that leaves the flag alone.
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id="30ee8617",
        )))
        self.assertEqual(asyncio.run(db.get_meta("trakt_reconnect_notice")), "1")
        self.login("boss")  # a fresh session re-reads has_trakt_identity
        self.assertFalse(self.notice_shown())

    def test_an_account_linked_elsewhere_is_never_stolen(self):
        other = self.make_user("someone-else")
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=other, provider="trakt", provider_user_id="424242",
        )))
        body = self.poll().text
        self.assertIn("not linked to your login", body)
        # Still on the original owner, and the notice stays up.
        rows = asyncio.run(auth.list_identities(other))
        self.assertEqual(len(rows), 1)
        self.assertEqual(asyncio.run(auth.list_identities(self.user_id)), [])
        self.assertEqual(asyncio.run(db.get_meta("trakt_reconnect_notice")), "1")


class PlexPopupUrlTests(RegressionTestCase):
    """The popup URL is a fragment parsed by Plex's own single-page app, so it has
    to match what known-working Plex clients emit rather than merely being
    well-formed."""

    def test_it_carries_the_client_id_the_code_and_the_product(self):
        from app.auth import plex as plex_auth
        url = plex_auth.popup_url("abc123", "PINCODE")
        self.assertTrue(url.startswith("https://app.plex.tv/auth#?"))
        self.assertIn("clientID=abc123", url)
        self.assertIn("code=PINCODE", url)
        self.assertIn("context%5Bdevice%5D%5Bproduct%5D=", url)

    def test_spaces_are_percent_encoded_not_plus_encoded(self):
        """`+`-means-space is a form-encoding convention; this is a URL fragment,
        and every working Plex client builds it with encodeURIComponent.

        THE PRODUCT NAME IS PATCHED RATHER THAN RELIED ON, because it is the only
        value in this URL that has ever contained a space and it no longer does —
        the app was renamed to a single word. Left as it was, this test would have
        gone on passing `assertNotIn("+")` while proving nothing about the
        encoding, which is the quiet way a regression test stops being one. The
        rule it guards is about how a value with a space is encoded, so the test
        supplies one.
        """
        from app.auth import plex as plex_auth
        with patch.object(plex_auth, "PRODUCT", "Two Words"):
            url = plex_auth.popup_url("abc123", "PINCODE")
        self.assertIn("Two%20Words", url)
        self.assertNotIn("+", url)


class CacheIsOffTheEventLoopTests(RegressionTestCase):
    """Every database call goes through a worker thread. app/cache was
    reading and zlib-decompressing ~200 KB blobs inline on the event loop."""

    def test_get_and_set_are_coroutines(self):
        from app import cache
        self.assertTrue(asyncio.iscoroutinefunction(cache.get))
        self.assertTrue(asyncio.iscoroutinefunction(cache.set))

    def test_round_trip_still_works(self):
        from app import cache
        payload = {"hello": ["world", 1, 2]}
        asyncio.run(cache.set("http://x/y", payload))
        self.assertEqual(asyncio.run(cache.get("http://x/y", 3600)), payload)
        self.assertIsNone(asyncio.run(cache.get("http://x/y", 0)))
        self.assertIsNone(asyncio.run(cache.get("http://missing", 3600)))

    def test_no_database_work_happens_on_the_event_loop_thread(self):
        """The real assertion: the connection used by a cache call belongs to a
        worker, not to the thread running the loop."""
        from app import cache
        threads = []
        real_connection = db.connection

        def spy():
            import threading
            threads.append(threading.current_thread().name)
            return real_connection()

        async def exercise():
            import threading
            loop_thread = threading.current_thread().name
            with patch("app.db.connection", side_effect=spy):
                await cache.set("http://x/y", {"a": 1})
                await cache.get("http://x/y", 3600)
            return loop_thread

        loop_thread = asyncio.run(exercise())
        self.assertTrue(threads, "the cache never touched the database")
        for name in threads:
            self.assertNotEqual(name, loop_thread)


class SharedBodyGuardTests(RegressionTestCase):
    """The one guard every mutating route reads its body through.

    It replaced six near-copies with five different contracts, so what matters is
    that the rules now hold at EVERY route rather than at whichever ones happened
    to inherit them. Each case below is asserted on a route from a DIFFERENT
    module, chosen so a rule that quietly stopped applying somewhere would fail
    here rather than in whichever module's tests happen to be thorough.
    """

    def setUp(self):
        super().setUp()
        self.user = self.make_user(is_admin=True)
        self.login()

    def test_malformed_json_is_refused_everywhere(self):
        for path in ("/admin/invites", "/api/state", "/settings"):
            resp = self.client.post(path, content=b"{not json",
                                    headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status_code, 400, path)

    def test_a_body_that_is_not_an_object_is_refused_everywhere(self):
        """A route reads its body with .get(); a list or a string has no .get,
        and the copies that returned {} or None for one only moved the failure."""
        for path in ("/admin/invites", "/api/state", "/settings"):
            resp = self.client.post(path, json=[1, 2, 3])
            self.assertEqual(resp.status_code, 400, path)

    def test_the_size_cap_applies_outside_the_ranker(self):
        """The cap used to exist in exactly one module. This asserts it on a
        route in a different one, which is the whole point of merging them."""
        oversize = b"x" * (authz.MAX_BODY_BYTES + 64)
        resp = self.client.post("/api/state",
                                content=b'{"pad": "' + oversize + b'"}',
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 413)

    def test_the_cap_leaves_room_for_an_image_upload(self):
        """/me/avatar and /me/images carry an image as base64 in the JSON
        body, and user_images allows 4 MB DECODED — about 5.6 MB encoded. A cap
        below that would refuse every ordinary upload with a 413 that says
        nothing about images."""
        self.assertGreater(authz.MAX_BODY_BYTES, user_images.MAX_UPLOAD_BYTES * 4 / 3)
        # The route's own ceiling sits between the two: above any legitimate
        # image, below the app-wide backstop, so it is the one that answers.
        self.assertGreater(user_images.MAX_UPLOAD_BODY_BYTES,
                           user_images.MAX_UPLOAD_BYTES * 4 / 3)
        self.assertLess(user_images.MAX_UPLOAD_BODY_BYTES, authz.MAX_BODY_BYTES)

    def test_an_oversized_image_is_refused_in_the_image_rules_words(self):
        """The app-wide guard can only describe the REQUEST — it does not know
        the body was going to be an image — so past a certain size the refusal
        used to stop naming a limit anyone could act on and start saying "that
        request is too large". The two image routes carry their own ceiling, so
        the wording no longer changes with the size of the file."""
        pad = b"x" * user_images.MAX_UPLOAD_BODY_BYTES
        # The routes the account page's own forms post to: the profile picture
        # and the saved-picture library are both markup the server draws now.
        # Their ceiling is the same one.
        for path in ("/me/avatar", "/me/images"):
            with self.subTest(path=path):
                resp = self.client.post(
                    path, content=b'{"image_b64": "' + pad + b'"}',
                    headers={"Content-Type": "application/json"})
                self.assertEqual(resp.status_code, 413)
                self.assertIn(user_images.TOO_LARGE_MESSAGE, resp.json()["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
