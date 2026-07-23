"""The hardening pass over rate limiting, proxy configuration, and three narrower
holes found while verifying the build against the plan.

What this file pins down, and why each one matters:

  - A LOCKOUT THAT ENDS. Attempts made while already locked out are not counted,
    and a lockout that has served its window resets the counter instead of
    leaving it primed one failure below the threshold. Without both, a retry loop
    holds a lockout open forever — and because every user behind a reverse proxy
    shares one per-IP key, that is a whole-instance outage rather than one
    account's problem.
  - THE PER-IP COUNTER CLEARS ON SUCCESS. Same reason: one shared key means other
    people's failures must not survive somebody proving they own an account.
  - THE SETTINGS SCREEN CAN DIAGNOSE THE PROXY. The value to type is reported
    back rather than guessed at, and the silent misconfiguration (headers
    arriving from an untrusted peer) is called out explicitly.
  - The three narrow fixes: admin session revocation is scoped to the account it
    was issued against, share tokens compare in constant time, and the provider
    sign-in start routes are throttled.

No network. TRAKT_DATA_DIR points at a temp dir, set BEFORE importing app modules.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_hardening -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-hardening-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, distrakt, share_links  # noqa: E402
from app.config import Settings, load_settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

PASSWORD = "correct-horse-battery"
LOCK = {"max_attempts": auth.LOGIN_MAX_ATTEMPTS, "window_seconds": auth.LOGIN_WINDOW_SECONDS}


class HardeningTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        HardeningTestCase._counter += 1
        db.set_db_path(TMP / f"hardening-{HardeningTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        # https because the session cookie is Secure by default; Origin because
        # mutating requests without one are refused.
        self.client = TestClient(app, base_url="https://testserver",
                                 headers={"Origin": "https://testserver"})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def make_user(self, username="josh", *, password=PASSWORD, **kwargs):
        kwargs.setdefault("calendar_approved", True)
        return asyncio.run(auth.create_user(username=username, password=password, **kwargs))

    def login(self, username="josh", password=PASSWORD):
        return self.client.post("/login", json={"username": username, "password": password})

    def fail_login(self, times, username="josh"):
        for _ in range(times):
            self.login(username, "wrong-password")

    def failures(self, key_type, key_value):
        return asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
            "AND succeeded = 0",
            (key_type, key_value), default=0,
        ))


class LockoutDoesNotSelfPerpetuateTests(HardeningTestCase):
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
        self.assertEqual(locked.json(), wrong.json())

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
        self.assertEqual(self.login("josh", "wrong-password").status_code, 401)
        self.assertFalse(asyncio.run(auth.check_lockout("username", "josh", **LOCK)))
        self.assertEqual(self.login().status_code, 200)

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


class SharedProxyIpCounterTests(HardeningTestCase):
    """Behind a reverse proxy every user shares one per-IP key, so anything that
    leaves failures on it is an instance-wide outage waiting to happen."""

    def test_successful_login_clears_the_ip_counter_too(self):
        self.make_user()
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS - 1)
        self.assertGreater(self.failures("ip", "testclient"), 0)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.failures("ip", "testclient"), 0)
        self.assertEqual(self.failures("username", "josh"), 0)

    def test_one_users_failures_do_not_lock_out_another_after_a_success(self):
        """The scenario: several people share one apparent address. Four bad
        tries from one of them, then a good login from anyone, must not leave the
        instance one mistake away from locking everybody out."""
        self.make_user("josh")
        self.make_user("alice")
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS - 1, username="josh")
        self.assertEqual(self.login("alice").status_code, 200)
        # Alice's success cleared the shared key, so josh's next mistake is his
        # first, not his fifth.
        self.assertEqual(self.login("josh", "wrong-password").status_code, 401)
        self.assertEqual(self.login("alice").status_code, 200)


class ProxyDiagnosticsTests(HardeningTestCase):
    """The Settings screen has to be able to answer "what do I type here?" —
    guessing a container subnet is exactly how this gets left at the default."""

    def admin_client(self):
        self.make_user("admin", is_admin=True)
        self.login("admin")
        return self.client

    def test_settings_reports_the_detected_peer_and_trust_state(self):
        body = self.admin_client().get("/api/settings").json()
        self.assertIn("detected_peer_ip", body)
        self.assertIn("forwarded_headers_present", body)
        self.assertIn("peer_is_trusted_proxy", body)
        self.assertEqual(body["trusted_proxy_ips"], auth.TRUSTED_PROXY_IPS_DEFAULT)

    def test_forwarded_headers_from_an_untrusted_peer_are_reported(self):
        """The silent misconfiguration: headers arriving, peer not trusted, so
        they are ignored and every user collapses onto one address."""
        client = self.admin_client()
        body = client.get("/api/settings", headers={"X-Forwarded-For": "203.0.113.9"}).json()
        self.assertTrue(body["forwarded_headers_present"])
        self.assertFalse(body["peer_is_trusted_proxy"])

    def test_trusted_proxy_ips_is_saveable_and_takes_effect(self):
        client = self.admin_client()
        self.assertTrue(client.post("/api/settings", json={
            "trusted_proxy_ips": "127.0.0.1/32, 172.18.0.0/16",
        }).json()["ok"])
        body = client.get("/api/settings").json()
        self.assertEqual(body["trusted_proxy_ips"], "127.0.0.1/32, 172.18.0.0/16")
        # And the parsed value is what client_ip actually consults.
        nets = auth.parse_trusted_networks("127.0.0.1/32, 172.18.0.0/16")
        self.assertEqual(len(nets), 2)

    def test_a_secret_is_still_never_returned(self):
        """The new fields sit alongside the redaction that is the whole point of
        this endpoint; assert it did not regress."""
        client = self.admin_client()
        client.post("/api/settings", json={"tmdb_api_key": "super-secret-value"})
        body = client.get("/api/settings").json()
        self.assertNotIn("tmdb_api_key", body)
        self.assertTrue(body["secrets_set"]["tmdb_api_key"])
        self.assertNotIn("super-secret-value", str(body))


class AdminSessionScopingTests(HardeningTestCase):
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


class ShareTokenComparisonTests(HardeningTestCase):
    """§4.1: the one comparison this app makes against a secret is constant-time."""

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
        with patch("app.share_links.secrets.compare_digest", return_value=False) as spy:
            self.assertIsNone(asyncio.run(share_links.resolve_by_token(row["token"])))
        spy.assert_called_once()


class HandshakeStartThrottleTests(HardeningTestCase):
    """/auth/plex/start and /auth/trakt/start are unauthenticated, mint a row, and
    (for Plex) call out to plex.tv. They share one per-address budget so nobody
    gets a second one by alternating providers."""

    def setUp(self):
        super().setUp()
        # The first-run gate refuses everything but setup until an account
        # exists, which would otherwise mask what these tests are measuring.
        self.make_user("resident")

    def test_plex_start_is_throttled(self):
        with patch("app.plex_auth.request_pin",
                   return_value={"id": 1, "code": "ABCD"}) as pin:
            for _ in range(auth.HANDSHAKE_MAX_ATTEMPTS):
                self.assertEqual(self.client.get("/auth/plex/start").status_code, 200)
            blocked = self.client.get("/auth/plex/start")
        self.assertEqual(blocked.status_code, 429)
        # And the limit is applied BEFORE the outbound call, not after it.
        self.assertEqual(pin.call_count, auth.HANDSHAKE_MAX_ATTEMPTS)

    def test_the_budget_is_shared_across_providers(self):
        save_settings(Settings(
            trakt_client_id="cid", trakt_client_secret="sec",
            public_base_url="https://testserver",
        ))
        with patch("app.plex_auth.request_pin", return_value={"id": 1, "code": "ABCD"}):
            for _ in range(auth.HANDSHAKE_MAX_ATTEMPTS):
                self.client.get("/auth/plex/start")
        # Trakt's start route reads the same counter, so it is already spent.
        resp = self.client.get("/auth/trakt/start", follow_redirects=False)
        self.assertEqual(resp.status_code, 429)

    def test_a_normal_flow_is_nowhere_near_the_limit(self):
        """A person retrying a flaky popup a few times must never hit this."""
        with patch("app.plex_auth.request_pin", return_value={"id": 1, "code": "ABCD"}):
            for _ in range(5):
                self.assertEqual(self.client.get("/auth/plex/start").status_code, 200)


class CookiePolicyDetectionTests(HardeningTestCase):
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
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(load_settings().cookie_secure, "always")
        self.assertIn("__Host-tns_session", resp.headers.get("set-cookie", ""))

    def test_a_browser_on_plain_http_gets_a_usable_cookie(self):
        resp = self.onboard("http://localhost:8000", "http://localhost:8000")
        self.assertTrue(resp.json()["ok"])
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
        self.assertTrue(resp.json()["ok"])
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
        self.assertTrue(resp.json()["ok"])
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


class FreshProxiedInstallCanOnboardTests(HardeningTestCase):
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
            self.assertTrue(client.post("/onboarding", json=self.ONBOARD).json()["ok"])
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


class CookieMismatchWarningTests(HardeningTestCase):
    """Onboarding gets it right on its own, so a mismatch means the deployment
    moved or settings.json was hand-edited. Either way it must not present as a
    wrong password."""

    def test_login_page_carries_the_effective_cookie_policy(self):
        self.make_user()
        save_settings(Settings(cookie_secure="always"))
        body = self.client.get("/login").text
        self.assertIn("cookieWarning", body)
        self.assertIn("const cookieIsSecure = true", body)

    def test_login_page_reports_a_non_secure_policy_too(self):
        self.make_user()
        save_settings(Settings(cookie_secure="never"))
        self.assertIn("const cookieIsSecure = false", self.client.get("/login").text)

    def test_the_settings_screen_can_see_the_policy(self):
        """The Settings modal compares cookie_secure against the browser's own
        protocol, so the value has to be in the payload — and must never be
        mistaken for a secret and redacted away."""
        self.make_user("admin", is_admin=True)
        self.login("admin")
        body = self.client.get("/api/settings").json()
        self.assertEqual(body["cookie_secure"], "always")


class CookieSecureEditingTests(HardeningTestCase):
    """cookie_secure is editable in Settings now, with the one self-locking
    change refused rather than the whole field left hand-edited.

    The refusal is judged by main._cookie_secure_error on the BROWSER's scheme,
    so it is unit-tested with constructed requests the same way
    detect_cookie_secure is — a TestClient will not carry an authenticated Secure
    cookie over an http:// base_url, which is the very situation under test."""

    @staticmethod
    def _req(origin=None):
        from starlette.requests import Request as StarletteRequest
        headers = [(b"origin", origin.encode())] if origin else []
        return StarletteRequest({"type": "http", "method": "POST", "path": "/api/settings",
                                 "headers": headers})

    def _error(self, mode, origin):
        from app import main
        return main._cookie_secure_error(Settings(cookie_secure=mode), self._req(origin))

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
        resp = self.client.post("/api/settings", json={"cookie_secure": "auto"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(load_settings().cookie_secure, "auto")

    def test_an_unknown_value_is_rejected_through_the_route(self):
        self.make_user("admin", is_admin=True)
        self.login("admin")
        resp = self.client.post("/api/settings", json={"cookie_secure": "sometimes"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(load_settings().cookie_secure, "always")  # unchanged


class ShareLinkSelectorTests(HardeningTestCase):
    """The Share panel's dropdown is PRESENTATION: it picks which URL you are
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

    def active(self, kind):
        return self.client.post("/api/me/share/active", json={"kind": kind}).json()

    def test_selecting_a_form_changes_only_which_one_is_offered(self):
        body = self.active("username")
        self.assertEqual(body["preferred_kind"], "username")
        # Every form stays live; the selection is about what the panel shows.
        self.assertEqual(body["enabled"], {"token": True, "username": True, "slug": True})
        self.assertTrue(body["urls"]["username"])
        self.assertTrue(body["urls"]["token"])

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
        self.client.post("/api/me/share/slug", json={"slug": "my-shows"})
        body = self.client.get("/api/me/share").json()
        for kind, path in (("token", f"/s/{body['token']}"),
                           ("username", "/u/josh"),
                           ("slug", "/c/my-shows")):
            with self.subTest(kind=kind):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_an_unknown_kind_is_refused(self):
        self.assertEqual(
            self.client.post("/api/me/share/active", json={"kind": "carrier-pigeon"}).status_code,
            400,
        )
        self.assertEqual(self.client.post("/api/me/share/active", json={}).status_code, 400)

    def test_the_slug_form_needs_a_name_before_it_resolves(self):
        """Selecting "custom name" with no name saved yet is a normal state, not
        an error — the panel shows the name field and an empty link box."""
        body = self.active("slug")
        self.assertEqual(body["preferred_kind"], "slug")
        self.assertIsNone(body["urls"]["slug"])
        self.client.post("/api/me/share/slug", json={"slug": "my-shows"})
        body = self.client.get("/api/me/share").json()
        self.assertEqual(body["urls"]["slug"], "https://testserver/c/my-shows")
        self.assertEqual(self.client.get("/c/my-shows").status_code, 200)

    def test_it_needs_a_session_like_every_other_share_route(self):
        self.client.post("/logout", json={})
        self.assertIn(self.client.post("/api/me/share/active", json={"kind": "token"}).status_code,
                      (401, 403))


class RetiredSlugTests(HardeningTestCase):
    """Changing a slug frees the old name, and `/c/<old>` links are already out in
    the world by then — so the name has to be blocked from being claimed by
    somebody else, exactly as a deleted account's is."""

    def setUp(self):
        super().setUp()
        save_settings(Settings(public_base_url="https://testserver"))
        self.user_id = self.make_user("josh")
        self.login("josh")

    def set_slug(self, slug):
        return self.client.post("/api/me/share/slug", json={"slug": slug}).json()

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


class PerUserEmojiMapTests(HardeningTestCase):
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
        user_id = self.tracker("plain")
        self.login("plain")
        self.assertTrue(self.client.get("/api/distrakt/emojis").json()["ok"])
        body = self.client.post("/api/distrakt/emojis", json={
            "network_emojis": {"HBO": ":hbo:"}, "default_network_emoji": ":film:",
        }).json()
        self.assertTrue(body["ok"])
        from app import distrakt as store
        self.assertEqual(asyncio.run(store.get_emoji_prefs(user_id)),
                         ({"HBO": ":hbo:"}, ":film:"))

    def test_a_malformed_map_is_refused(self):
        self.tracker("plain")
        self.login("plain")
        self.assertEqual(self.client.post(
            "/api/distrakt/emojis", json={"network_emojis": "not-an-object"}).status_code, 400)


class DistraktDetailsTests(HardeningTestCase):
    """The tracker's own details modal. A separate route from /api/details because
    that one is CALENDAR_APPROVED — a tracker user need not be calendar approved —
    and because this one answers a question the calendar has no business knowing:
    which episodes THIS person has watched."""

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
        asyncio.run(distrakt.add_show(user_id, "2026-07", {
            "trakt_id": 7, "season": 3, "title": "Silo", "slug": "silo",
            "network": "Apple TV", "tmdb": 1, "media": "show",
        }))
        return user_id

    def details(self, watched="[1,2,3]"):
        if watched is not None:
            asyncio.run(db.execute(
                "INSERT OR REPLACE INTO distrakt_show_progress "
                "(user_id, trakt_id, season, watched_episodes_json) VALUES (?,?,?,?)",
                (self.user_id, 7, 3, watched)))
        self.login("josh")
        with patch("app.main.fetch_details", return_value=self.DETAILS):
            return self.client.get("/api/distrakt/details?trakt_id=7&season=3")

    def test_it_returns_the_episodes_and_this_users_watched_set(self):
        body = self.details().json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["episodes"]), 5)
        self.assertEqual(body["watched_episodes"], [1, 2, 3])

    def test_the_slug_comes_from_the_roster_not_the_caller(self):
        """The Trakt links are built from it, so a caller must not be able to
        point them somewhere else."""
        body = self.details().json()
        self.assertEqual(body["slug"], "silo")
        self.login("josh")
        with patch("app.main.fetch_details", return_value=self.DETAILS):
            spoofed = self.client.get(
                "/api/distrakt/details?trakt_id=7&season=3&slug=evil").json()
        self.assertEqual(spoofed["slug"], "silo")

    def test_one_users_watched_set_is_invisible_to_another(self):
        self.details()
        self.client.post("/logout", json={})
        other = self.tracker("other")
        self.login("other")
        with patch("app.main.fetch_details", return_value=self.DETAILS):
            body = self.client.get("/api/distrakt/details?trakt_id=7&season=3").json()
        self.assertEqual(body["watched_episodes"], [])

    def test_no_progress_row_is_an_empty_set_not_an_error(self):
        body = self.details(watched=None).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["watched_episodes"], [])

    def test_it_needs_the_tracker_grant(self):
        self.client.post("/logout", json={})
        self.make_user("plain", calendar_approved=True)
        self.login("plain")
        self.assertIn(
            self.client.get("/api/distrakt/details?trakt_id=7&season=3").status_code, (401, 403))

    def test_a_missing_season_is_refused(self):
        self.login("josh")
        self.assertEqual(
            self.client.get("/api/distrakt/details?trakt_id=7").status_code, 400)


class SiteHeaderTests(HardeningTestCase):
    """One header across the calendar, the month picker, and the tracker. They
    had drifted into three different bars, and the admin calendar's had swollen
    onto a second row."""

    PAGES = ("/pick", "/?month=7&year=2026", "/distrakt", "/me", "/admin")

    def setUp(self):
        super().setUp()
        save_settings(Settings(public_base_url="https://testserver"))
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
                body, head = self.header(path)
                self.assertIn('class="hero', head)
                self.assertIn("hero-actions", head)
                self.assertIn("/static/js/nav.js", body)

    def test_every_page_offers_account_and_sign_out(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                _, head = self.header(path)
                self.assertIn("nav-menu", head)
                self.assertIn('href="/me"', head)
                self.assertIn("signOut()", head)

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
        # All three destinations live inside the menu...
        for entry in ('href="/me"', "openSettings()", 'href="/admin"'):
            self.assertIn(entry, menu, entry)
        # ...and none of them is also a top-level pill in the bar itself.
        bar = actions.split("nav-menu-items", 1)[0]
        for entry in ('href="/me"', "openSettings()", 'href="/admin"'):
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


class SharedAddressLockoutTests(HardeningTestCase):
    """Five wrong passwords on ONE account used to lock out EVERY account from
    that address — administrator included, with the generic failure message and
    nothing in the log. On a single-user instance that is self-inflicted; behind a
    reverse proxy, where every user shares one apparent address, it is an outage."""

    def test_one_accounts_failures_do_not_lock_out_another(self):
        self.make_user("josh")
        self.make_user("test")
        # Well past the per-username limit for `test`.
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS + 3, username="test")
        self.assertEqual(self.login("test").status_code, 401)   # that account IS locked
        self.assertEqual(self.login("josh").status_code, 200)   # this one is not

    def test_the_per_username_limit_still_bites(self):
        self.make_user("josh")
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS)
        self.assertEqual(self.login().status_code, 401)

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
        self.assertEqual(self.login().status_code, 401)

    def test_a_lockout_is_logged_even_though_the_response_cannot_say_so(self):
        """Without this the operator sees "login is broken" and no explanation —
        which is exactly how this was reported."""
        self.make_user("josh")
        self.fail_login(auth.LOGIN_MAX_ATTEMPTS)
        with self.assertLogs("app.auth_routes", level="WARNING") as captured:
            self.login()
        self.assertTrue(any("locked out" in line for line in captured.output))


class SharePanelDisclosureTests(HardeningTestCase):
    """public_base_url is an admin-only setting, so the "set a base URL" prompt is
    an unfixable complaint for everybody else."""

    def test_the_calendar_tells_the_page_whether_the_viewer_is_an_admin(self):
        # The panel gates the warning on this flag, which the calendar already
        # renders server-side from the session.
        self.make_user("plain")
        self.login("plain")
        self.assertIn("window.IS_ADMIN = false", self.client.get("/?month=1&year=2026").text)
        self.client.post("/logout", json={})
        self.make_user("boss", is_admin=True)
        self.login("boss")
        self.assertIn("window.IS_ADMIN = true", self.client.get("/?month=1&year=2026").text)

    def test_base_url_missing_is_still_reported_in_the_payload(self):
        """The flag stays in the API — it is the PANEL that decides who sees it,
        so the server keeps telling the truth."""
        save_settings(Settings(public_base_url=""))
        self.make_user("josh")
        self.login("josh")
        self.assertTrue(self.client.get("/api/me/share").json()["base_url_missing"])


class AccountPageDisclosureTests(HardeningTestCase):
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

    def test_a_tracker_user_without_a_trakt_link_is_told_to_link_one(self):
        """The exact dead end: approved for the tracker, no Trakt identity, so
        every tracker route refuses and nothing says why."""
        self.make_user("tracker", distrakt_approved=True)
        self.login("tracker")
        self.assertIn("Link your Trakt account", self.me())

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


class DeviceAuthAdoptsTheTokenTests(HardeningTestCase):
    """Re-authorizing in Settings renewed the app-wide token but never wrote a
    linked identity, so the "reconnect your Trakt account" notice could not be
    cleared by the one button that looked like it should clear it — and the
    tracker kept refusing the account for want of a Trakt link."""

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

    def poll(self):
        with patch("app.trakt_auth.poll_device_token", return_value=self.TOKEN), \
             patch("app.trakt_auth.fetch_account", return_value=self.ACCOUNT):
            return self.client.post("/api/auth/device/poll", json={"device_code": "dc"})

    def test_a_successful_authorization_links_the_admin_and_clears_the_notice(self):
        body = self.poll().json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["trakt_linked"])
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
        from app import trakt_auth
        with patch("app.trakt_auth.poll_device_token", return_value=self.TOKEN), \
             patch("app.trakt_auth.fetch_account",
                   side_effect=trakt_auth.AccountLookupError("no")):
            body = self.client.post("/api/auth/device/poll", json={"device_code": "dc"}).json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["trakt_linked"])
        self.assertEqual(asyncio.run(db.get_meta("trakt_reconnect_notice")), "1")
        self.assertEqual(load_settings().trakt_access_token, "fresh-token")

    def test_the_notice_is_derived_from_the_link_not_a_sticky_flag(self):
        """The prompt asks for a linked Trakt identity. Once the caller has one it
        must go away, whichever route did the linking and whether or not that
        route remembered to clear the stored flag — a "do this" that stays up
        after the thing is done is worse than no prompt at all."""
        self.assertTrue(self.client.get("/api/settings").json()["trakt_reconnect_notice"])
        # Link by a path that leaves the flag alone.
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id="30ee8617",
        )))
        self.assertEqual(asyncio.run(db.get_meta("trakt_reconnect_notice")), "1")
        self.login("boss")  # a fresh session re-reads has_trakt_identity
        self.assertFalse(self.client.get("/api/settings").json()["trakt_reconnect_notice"])

    def test_an_account_linked_elsewhere_is_never_stolen(self):
        other = self.make_user("someone-else")
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=other, provider="trakt", provider_user_id="424242",
        )))
        body = self.poll().json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["trakt_linked"])
        # Still on the original owner, and the notice stays up.
        rows = asyncio.run(auth.list_identities(other))
        self.assertEqual(len(rows), 1)
        self.assertEqual(asyncio.run(auth.list_identities(self.user_id)), [])
        self.assertEqual(asyncio.run(db.get_meta("trakt_reconnect_notice")), "1")


class PlexPopupUrlTests(HardeningTestCase):
    """The popup URL is a fragment parsed by Plex's own single-page app, so it has
    to match what known-working Plex clients emit rather than merely being
    well-formed."""

    def test_it_carries_the_client_id_the_code_and_the_product(self):
        from app import plex_auth
        url = plex_auth.popup_url("abc123", "PINCODE")
        self.assertTrue(url.startswith("https://app.plex.tv/auth#?"))
        self.assertIn("clientID=abc123", url)
        self.assertIn("code=PINCODE", url)
        self.assertIn("context%5Bdevice%5D%5Bproduct%5D=", url)

    def test_spaces_are_percent_encoded_not_plus_encoded(self):
        """`+`-means-space is a form-encoding convention; this is a URL fragment,
        and every working Plex client builds it with encodeURIComponent."""
        from app import plex_auth
        url = plex_auth.popup_url("abc123", "PINCODE")
        self.assertIn("%20", url)
        self.assertNotIn("+", url)


class CacheIsOffTheEventLoopTests(HardeningTestCase):
    """§1.1a: every database call goes through a worker thread. app/cache was
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
