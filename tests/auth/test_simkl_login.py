"""Log in with Simkl — handshake binding, the sign-in/register/link outcomes,
identity keying, and the absence of any refresh path.

THIS FILE IS ALSO THE FIRST REAL TEST OF app/auth/provider_login.py. The shared
completion was extracted from two near-identical copies and, at that point,
could only be ASSERTED to be provider-neutral: both callers were the ones it was
carved out of. Simkl is the first caller that was written against the seam
rather than folded into it, so every outcome below — the throttle, the invite
gate, the identity already in use, the disabled account reported exactly like a
wrong password — is the shared policy answering for a provider it was not
written from.

THE HANDSHAKE TESTS MATTER FOR THE SAME REASON THEY DO AT TRAKT. The callback is
a top-level GET navigation, which SameSite=Lax deliberately sends cookies on, so
an unbound one is an account-takeover vector: a callback carrying an attacker's
Simkl identity, completed in a signed-in victim's browser, would link that
identity to the victim's account permanently.

No network: the code exchange and the account lookup are patched.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import auth, db
from app.auth import routes as auth_routes, simkl as simkl_auth, trakt_routes
from app.auth import simkl_routes
from app.config import Settings, save_settings
from tests.support import AppTestCase, ORIGIN

# The Simkl account the patched authorization returns unless a test says
# otherwise. An int, because the immutable numeric account id is the only
# acceptable key for an identity row.
SIMKL_ID = 445566
OTHER_SIMKL_ID = 778899


def _settings(**overrides) -> Settings:
    """Settings with the Simkl redirect flow fully configured."""
    base = {
        "simkl_client_id": "simkl-client-id",
        "simkl_client_secret": "simkl-client-secret",
        "public_base_url": ORIGIN,
    }
    base.update(overrides)
    return Settings(**base)


class _Token(dict):
    """A Simkl token payload. No refresh_token, because Simkl issues none — the
    field is absent from the real response rather than null."""

    def __init__(self, access="simkl-access-1", expires_in=157_680_000):
        super().__init__(access_token=access, token_type="bearer", scope="public",
                         expires_in=expires_in)


class SimklOAuthTestCase(AppTestCase):
    def make_settings(self):
        return _settings()

    def setUp(self):
        super().setUp()
        # Something has to exist or the first-run gate answers every request
        # before any of this is reached.
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)

    # -- fixtures ----------------------------------------------------------

    def sign_out(self) -> None:
        self.client.cookies.clear()

    def mint_invite(self, **kwargs) -> str:
        return asyncio.run(auth.create_invite(created_by=self.admin_id, **kwargs))["token"]

    def start(self, path="/auth/simkl/start", **params) -> str:
        """Begin a flow and return the `state` the app generated."""
        resp = self.client.get(path, params=params, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        query = parse_qs(urlsplit(resp.headers["location"]).query)
        return query["state"][0]

    def pin_handshake_cookie(self, state) -> None:
        """Pretend the browser still holds the cookie for `state`. It is checked
        before the row is, so a test about the ROW has to satisfy it first or it
        would pass for the wrong reason."""
        if state:
            self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)

    def callback(self, state, *, code="auth-code", simkl_id=SIMKL_ID, name="Josh",
                 token=None, pin=True, **params):
        """Complete a callback with the authorization patched out."""
        if pin:
            self.pin_handshake_cookie(state)
        with patch.object(simkl_auth, "exchange_code", return_value=token or _Token()), \
             patch.object(simkl_auth, "fetch_account",
                          return_value={"id": simkl_id, "name": name}):
            return self.client.get("/auth/simkl/callback",
                                   params={"state": state, "code": code, **params},
                                   follow_redirects=False)

    def identities(self):
        return asyncio.run(db.fetch_all("SELECT * FROM linked_identities"))

    def user_count(self) -> int:
        return int(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")))


class HandshakeBindingTests(SimklOAuthTestCase):
    """Every way of reaching the callback without having started the flow."""

    def test_a_completed_flow_consumes_its_handshake_exactly_once(self):
        state = self.start(invite=self.mint_invite())
        self.assertEqual(self.callback(state).status_code, 303)
        self.assertIsNotNone(asyncio.run(db.fetch_value(
            "SELECT consumed_at FROM auth_handshakes WHERE state = ?", (state,))))

    def test_a_forged_state_is_refused_before_any_outbound_call(self):
        """The handshake is validated BEFORE the authorization code is spent, so
        a forged callback costs one lookup and never reaches Simkl."""
        self.start()
        self.pin_handshake_cookie("nope")

        def _explode(*a, **kw):  # pragma: no cover — the assertion is that it isn't called
            raise AssertionError("the authorization code was exchanged for a bad state")

        with patch.object(simkl_auth, "exchange_code", side_effect=_explode), \
             patch.object(simkl_auth, "fetch_account", side_effect=_explode):
            resp = self.client.get("/auth/simkl/callback",
                                   params={"state": "nope", "code": "c"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_a_replayed_state_is_refused_before_any_outbound_call(self):
        """The same callback URL delivered a second time. The first use is what
        wrote the identity; the second must change nothing, and must not spend a
        second exchange finding that out."""
        state = self.start(invite=self.mint_invite())
        self.assertEqual(self.callback(state).status_code, 303)
        self.sign_out()

        def _explode(*a, **kw):  # pragma: no cover — asserting it isn't called
            raise AssertionError("a replayed callback reached Simkl")

        self.pin_handshake_cookie(state)
        with patch.object(simkl_auth, "exchange_code", side_effect=_explode), \
             patch.object(simkl_auth, "fetch_account", side_effect=_explode):
            resp = self.client.get("/auth/simkl/callback",
                                   params={"state": state, "code": "auth-code"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(self.identities()), 1)

    def test_a_callback_with_no_state_at_all_is_refused(self):
        self.start()
        resp = self.client.get("/auth/simkl/callback", params={"code": "auth-code"},
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_an_expired_state_is_refused(self):
        state = self.start()
        asyncio.run(db.execute(
            "UPDATE auth_handshakes SET expires_at = ? WHERE state = ?",
            (db.now() - 1, state)))
        self.assertEqual(self.callback(state).status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_a_callback_in_another_browser_is_refused(self):
        state = self.start()
        self.client.cookies.clear()  # a different browser
        resp = self.callback(state, pin=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])
        # ...and the handshake was not spent, so the visitor who started it can
        # still finish.
        self.assertIsNone(asyncio.run(db.fetch_value(
            "SELECT consumed_at FROM auth_handshakes WHERE state = ?", (state,))))

    def test_a_trakt_handshake_cannot_be_completed_at_the_simkl_callback(self):
        state = asyncio.run(auth.create_handshake(provider="trakt", purpose="login"))
        self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)
        self.assertEqual(self.callback(state).status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_a_foreign_session_cannot_complete_a_link_handshake(self):
        """THE TAKEOVER CASE. An attacker starts a link flow on their own
        account and hands the callback URL to a signed-in victim."""
        attacker = self.make_user("attacker", calendar_approved=True)
        victim = self.make_user("victim", calendar_approved=True)
        self.sign_in_as(attacker)
        state = self.start("/auth/simkl/link")

        self.sign_out()
        self.sign_in_as(victim)
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_the_deny_branch_changes_nothing(self):
        """The user pressed "deny" on Simkl's screen. Nothing was authorized, so
        there is nothing to undo — and the handshake is left for the flow that
        never happened rather than being treated as a sign-in."""
        state = self.start()

        def _explode(*a, **kw):  # pragma: no cover — asserting it isn't called
            raise AssertionError("a denied authorization reached Simkl")

        with patch.object(simkl_auth, "exchange_code", side_effect=_explode), \
             patch.object(simkl_auth, "fetch_account", side_effect=_explode):
            resp = self.client.get("/auth/simkl/callback",
                                   params={"state": state, "error": "access_denied"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])
        self.assertEqual(self.user_count(), 1)

    def test_every_refusal_reads_the_same(self):
        """Unknown, expired and consumed must not be tellable apart, or the
        callback becomes a probe for which guess was closest."""
        expired = self.start()
        asyncio.run(db.execute("UPDATE auth_handshakes SET expires_at = ? WHERE state = ?",
                               (db.now() - 1, expired)))
        consumed = self.start()
        asyncio.run(db.execute("UPDATE auth_handshakes SET consumed_at = ? WHERE state = ?",
                               (db.now(), consumed)))
        bodies = set()
        for state in ("never-existed", expired, consumed):
            self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)
            resp = self.callback(state)
            self.assertEqual(resp.status_code, 400)
            bodies.add(resp.text)
        self.assertEqual(len(bodies), 1)


class StartRouteTests(SimklOAuthTestCase):
    def test_start_redirects_to_simkl_with_the_configured_redirect_uri(self):
        resp = self.client.get("/auth/simkl/start", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        target = urlsplit(resp.headers["location"])
        query = parse_qs(target.query)
        self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}",
                         simkl_auth.AUTHORIZE_URL)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["simkl-client-id"])
        # Built from the configured origin, never from the Host header — Simkl
        # compares it against the registered value byte for byte.
        self.assertEqual(query["redirect_uri"], [f"{ORIGIN}/auth/simkl/callback"])

    def test_the_redirect_uri_ignores_a_spoofed_host_header(self):
        resp = self.client.get("/auth/simkl/start", follow_redirects=False,
                               headers={"Host": "evil.example.com"})
        query = parse_qs(urlsplit(resp.headers["location"]).query)
        self.assertEqual(query["redirect_uri"], [f"{ORIGIN}/auth/simkl/callback"])

    def test_start_is_unavailable_until_the_instance_is_configured(self):
        for missing in ("simkl_client_id", "simkl_client_secret", "public_base_url"):
            # Cleared explicitly: emptying a field on a Settings object no longer
            # removes the stored credential on its own — see config.save_settings.
            save_settings(_settings(**{missing: ""}), clear_unset_secrets=True)
            resp = self.client.get("/auth/simkl/start", follow_redirects=False)
            self.assertEqual(resp.status_code, 503, missing)
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM auth_handshakes"))), 0)

    def test_link_requires_a_session(self):
        self.sign_out()
        resp = self.client.get("/auth/simkl/link", follow_redirects=False,
                               headers={"Accept": "application/json"})
        self.assertEqual(resp.status_code, 401)

    def test_the_invite_travels_in_the_handshake_row_not_the_redirect(self):
        token = self.mint_invite()
        state = self.start(invite=token)
        row = asyncio.run(db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertEqual(row["invite_token"], token)
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["provider"], "simkl")


class SignInOutcomeTests(SimklOAuthTestCase):
    """The register-or-sign-in matrix, answered entirely by the shared
    completion in app/auth/provider_login.py."""

    def test_a_known_identity_signs_its_owner_in(self):
        user = self.make_user("known", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="simkl", provider_user_id=SIMKL_ID,
            display_name="Old Name")))
        self.sign_out()

        resp = self.callback(self.start(), name="New Name")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")
        me = self.client.get("/me")
        self.assertEqual(me.status_code, 200)
        self.assertIn("known", me.text)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "New Name")
        self.assertEqual(rows[0]["access_token"], "simkl-access-1")

    def test_an_unknown_identity_with_no_invite_creates_no_account(self):
        """Registration through the provider path is invite-gated exactly as
        registration with a password is — a Simkl sign-in proves only that
        somebody controls some Simkl account."""
        self.sign_out()
        before = self.user_count()
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 403)
        self.assertIn(auth_routes.INVALID_INVITE, resp.text)
        self.assertEqual(self.user_count(), before)
        self.assertEqual(self.identities(), [])

    def test_a_valid_invite_registers_and_grants_calendar(self):
        self.sign_out()
        token = self.mint_invite()
        resp = self.callback(self.start(invite=token))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")

        user = asyncio.run(db.fetch_one(
            "SELECT u.* FROM users u JOIN linked_identities li ON li.user_id = u.id "
            "WHERE li.provider_user_id = ?", (str(SIMKL_ID),)))
        self.assertIsNotNone(user)
        self.assertIsNone(user["username"])
        self.assertIsNone(user["password_hash"])
        self.assertTrue(user["calendar_approved"])
        # An invite never grants the private-history feature: that one is always
        # a separate manual grant.
        self.assertFalse(user["distrakt_approved"])
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT used_count FROM invites WHERE token = ?", (token,)))), 1)

    def test_open_registration_needs_no_invite_but_grants_nothing(self):
        save_settings(_settings(allow_open_registration=True))
        self.sign_out()
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")
        user = asyncio.run(db.fetch_one(
            "SELECT u.* FROM users u JOIN linked_identities li ON li.user_id = u.id "
            "WHERE li.provider_user_id = ?", (str(SIMKL_ID),)))
        self.assertFalse(user["calendar_approved"])

    def test_registration_through_the_provider_path_is_rate_limited(self):
        self.sign_out()
        asyncio.run(_fill_attempts("register_ip", "testclient", auth.REGISTER_MAX_ATTEMPTS))
        resp = self.callback(self.start(invite=self.mint_invite()))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self.user_count(), 1)
        self.assertEqual(self.identities(), [])

    def test_a_returning_identity_is_not_throttled(self):
        """Only a REGISTRATION spends the throttle budget. A household behind one
        address must not be locked out of signing in because somebody there
        registered earlier."""
        user = self.make_user("regular", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="simkl", provider_user_id=SIMKL_ID)))
        self.sign_out()
        asyncio.run(_fill_attempts("register_ip", "testclient", auth.REGISTER_MAX_ATTEMPTS))
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 303)

    def test_a_disabled_account_is_reported_exactly_like_a_wrong_password(self):
        """A completion that said "this account exists but is disabled" would be
        an oracle for account state to anyone who can authorize at Simkl, which
        is anyone."""
        user = self.make_user("banned")
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="simkl", provider_user_id=SIMKL_ID)))
        asyncio.run(db.execute("UPDATE users SET is_disabled = 1 WHERE id = ?", (user,)))
        self.sign_out()
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 403)
        self.assertIn(auth_routes.INVALID_CREDENTIALS, resp.text)
        # And no session was issued along the way.
        self.assertNotIn(auth.COOKIE_NAME_SECURE, resp.cookies)


class LinkOutcomeTests(SimklOAuthTestCase):
    def test_linking_attaches_the_identity_to_the_signed_in_account(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.callback(self.start("/auth/simkl/link"))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["user_id"]), user)
        self.assertEqual(rows[0]["provider"], "simkl")
        self.assertEqual(rows[0]["provider_user_id"], str(SIMKL_ID))

    def test_a_simkl_identity_coexists_with_a_trakt_one_on_the_same_account(self):
        """The point of the whole build: two sources, one account. Migration 21
        removed the closed provider set that would have refused this."""
        user = self.make_user("both", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="trakt", provider_user_id="trakt-uuid")))
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        providers = sorted(row["provider"] for row in self.identities())
        self.assertEqual(providers, ["simkl", "trakt"])

    def test_an_identity_linked_elsewhere_is_refused_never_moved(self):
        """Whoever authorizes last must not be able to take an identity away
        from the account already holding it."""
        owner = self.make_user("owner", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=owner, provider="simkl", provider_user_id=SIMKL_ID)))
        interloper = self.make_user("interloper", calendar_approved=True)
        self.sign_in_as(interloper)

        resp = self.callback(self.start("/auth/simkl/link"))
        self.assertEqual(resp.status_code, 409)
        self.assertIn(simkl_routes.ALREADY_LINKED, resp.text)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["user_id"]), owner)

    def test_relinking_the_same_account_refreshes_the_stored_token(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        self.callback(self.start("/auth/simkl/link"), token=_Token(access="simkl-access-2"))
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["access_token"], "simkl-access-2")

    def test_a_link_is_refused_while_encryption_is_unhealthy(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        with patch("app.auth.encryption_flow.secret_writes_blocked", return_value=True):
            resp = self.callback(self.start("/auth/simkl/link"))
        self.assertEqual(resp.status_code, 409)
        # Matched on a fragment rather than the whole constant: the page renders
        # it HTML-escaped, so its apostrophes come back as entities.
        self.assertIn("ENCRYPTION_KEY", resp.text)
        self.assertEqual(self.identities(), [])


class IdentityKeyTests(SimklOAuthTestCase):
    def test_the_identity_is_keyed_on_the_numeric_id_not_the_display_name(self):
        """A Simkl username can be changed by its owner and re-registered by
        somebody else, so keying on one would let a released name inherit the
        linked account."""
        user = self.make_user("renamer", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"), name="Before")
        self.callback(self.start("/auth/simkl/link"), name="After")

        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "After")
        self.assertEqual(rows[0]["provider_user_id"], str(SIMKL_ID))

    def test_a_different_numeric_id_is_a_different_identity(self):
        user = self.make_user("collector", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"), simkl_id=SIMKL_ID, name="Same Name")
        self.callback(self.start("/auth/simkl/link"), simkl_id=OTHER_SIMKL_ID,
                      name="Same Name")
        self.assertEqual(len(self.identities()), 2)

    def test_an_account_response_without_a_numeric_id_is_refused(self):
        state = self.start()
        with patch.object(simkl_auth, "exchange_code", return_value=_Token()), \
             patch.object(simkl_auth, "fetch_account",
                          side_effect=simkl_auth.AccountLookupError("no account id")):
            resp = self.client.get("/auth/simkl/callback",
                                   params={"state": state, "code": "c"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.identities(), [])

    # The real /users/settings body, trimmed to what fetch_account reads. The
    # numeric id is under `account`; `user.name` is display-only.
    SETTINGS_BODY = {
        "user": {"name": "Josh", "joined_at": "2015-02-01T00:00:00Z"},
        "account": {"id": SIMKL_ID, "timezone": "America/New_York", "type": "free"},
        "connections": {"facebook": False},
    }

    def test_fetch_account_reads_the_numeric_id_from_users_settings(self):
        seen = {}

        async def _send(client, method, url, **kwargs):
            seen["method"] = method
            seen["url"] = url
            seen["pool"] = kwargs.get("pool")
            seen["headers"] = kwargs.get("headers") or {}
            return _StubResponse(self.SETTINGS_BODY)

        with patch("app.providers.simkl.transport.send", side_effect=_send):
            account = asyncio.run(simkl_auth.fetch_account("cid", "the-token"))
        # By name rather than by whole-dict equality — see the note on the Trakt
        # equivalent: the shape now also carries a display-only `avatar`.
        self.assertEqual(account["id"], str(SIMKL_ID))
        self.assertEqual(account["name"], "Josh")
        # A POST with no body, which is what Simkl documents for this endpoint,
        # on the pool that serializes and paces /users/ traffic.
        self.assertEqual(seen["method"], "POST")
        self.assertIn("/users/settings", seen["url"])
        self.assertIs(seen["pool"], simkl_transport().SYNC_POOL)
        self.assertEqual(seen["headers"]["Authorization"], "Bearer the-token")

    def test_fetch_account_refuses_a_response_with_no_numeric_id(self):
        for body in ({"user": {"name": "Josh"}}, {"account": {}}, {}):
            with self.subTest(body=body):
                async def _send(*a, **kw):
                    return _StubResponse(body)

                with patch("app.providers.simkl.transport.send", side_effect=_send):
                    with self.assertRaises(simkl_auth.AccountLookupError):
                        asyncio.run(simkl_auth.fetch_account("cid", "token"))

    def test_the_exchange_posts_the_configured_redirect_uri(self):
        seen = {}

        async def _send(client, method, url, **kwargs):
            seen["method"] = method
            seen["url"] = url
            seen["pool"] = kwargs.get("pool")
            seen["json"] = kwargs.get("json")
            return _StubResponse(_Token())

        with patch("app.providers.simkl.transport.send", side_effect=_send):
            token = asyncio.run(simkl_auth.exchange_code(
                "cid", "secret", "the-code", ORIGIN))
        self.assertEqual(token["access_token"], "simkl-access-1")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], simkl_auth.TOKEN_URL)
        # The pacer and the 412 breaker cover the sign-in exchange too, which is
        # the whole reason it goes through the source's transport.
        self.assertIs(seen["pool"], simkl_transport().SYNC_POOL)
        self.assertEqual(seen["json"]["redirect_uri"], f"{ORIGIN}/auth/simkl/callback")
        self.assertEqual(seen["json"]["grant_type"], "authorization_code")


class NoRefreshTests(SimklOAuthTestCase):
    """Simkl issues no refresh token, so nothing may ever try to renew one.

    The failure this guards against is not loud: a renewal attempt against a
    Simkl identity would post a NULL refresh token to Trakt's token endpoint and
    get back a refusal, and the visible symptom would be a working link that
    stops working for no reason a log explains.
    """

    def test_a_simkl_identity_stores_no_refresh_token_and_no_expiry(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        row = self.identities()[0]
        self.assertEqual(row["access_token"], "simkl-access-1")
        self.assertIsNone(row["refresh_token"])
        # The exchange DOES report an expires_in of about five years. It is
        # deliberately not stored: there is no renewal path it could trigger, so
        # recording it would only invent a deadline nothing can meet.
        self.assertIsNone(row["token_expires_at"])

    def test_the_simkl_login_module_offers_no_refresh_entry_point(self):
        """There is no refresh_access_token here, and its absence is the design
        rather than an oversight — see the module docstring."""
        self.assertFalse([name for name in dir(simkl_auth) if "refresh" in name])

    def test_the_refresh_path_never_touches_a_simkl_identity(self):
        """The only token renewal in this app is Trakt's, and it is scoped to
        Trakt rows. An account with a Simkl link and no Trakt one has no token
        there and nothing is exchanged looking for one."""
        user = self.make_user("simkl_only", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        # Make the row look as expired as an identity can, so any renewal that
        # was going to happen would happen now.
        asyncio.run(db.execute(
            "UPDATE linked_identities SET token_expires_at = ?, refresh_token = ?",
            (db.now() - 3600, "not-a-real-refresh-token")))

        from app.auth import trakt as trakt_auth

        def _explode(*a, **kw):  # pragma: no cover — asserting it isn't called
            raise AssertionError("a Simkl identity was sent through Trakt's refresh")

        with patch.object(trakt_auth, "refresh_access_token", side_effect=_explode):
            self.assertIsNone(asyncio.run(trakt_routes.access_token_for_user(user)))
        self.assertEqual(self.identities()[0]["refresh_token"], "not-a-real-refresh-token")


class PerUserTokenTests(SimklOAuthTestCase):
    """Reading back the token the tracker authenticates one person's reads with.

    The whole of it is a row lookup: no expiry check, no lease, no refresh. A
    branch on `token_expires_at` could only ever take the "no expiry" path,
    because it is NULL on every Simkl row this app writes, and dead code that
    looks like a safety check is worse than the plain read.
    """

    def test_a_linked_account_reads_back_its_own_token(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        self.assertEqual(asyncio.run(simkl_routes.access_token_for_user(user)),
                         "simkl-access-1")

    def test_an_account_with_no_simkl_link_has_no_token(self):
        """Which is what makes `simkl_configured` false on that request's
        settings, and the Simkl port simply never asked."""
        self.assertIsNone(asyncio.run(
            simkl_routes.access_token_for_user(self.make_user("plain"))))

    def test_one_persons_token_is_never_another_persons(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        other = self.make_user("stranger", calendar_approved=True)
        self.assertIsNone(asyncio.run(simkl_routes.access_token_for_user(other)))


class UnlinkTests(SimklOAuthTestCase):
    def test_a_linked_simkl_account_can_be_unlinked(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "simkl"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.identities(), [])
        # Simkl documents no revocation endpoint, so there is nothing this app
        # could have failed to do and nothing to warn about.
        self.assertIsNone(resp.json()["warning"])

    def test_the_last_login_method_cannot_be_unlinked(self):
        self.sign_out()
        self.callback(self.start(invite=self.mint_invite()))
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "simkl"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(self.identities()), 1)

    def test_unlinking_something_that_is_not_linked_is_a_404(self):
        user = self.make_user("plain", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "simkl"})
        self.assertEqual(resp.status_code, 404)


class ProviderButtonTests(SimklOAuthTestCase):
    def test_the_sign_in_page_offers_simkl_once_it_is_configured(self):
        self.sign_out()
        self.assertIn('href="/auth/simkl/start"', self.client.get("/login").text)

    def test_the_button_is_inert_until_it_is_configured(self):
        save_settings(_settings(public_base_url=""), clear_unset_secrets=True)
        self.sign_out()
        body = self.client.get("/login").text
        self.assertNotIn('href="/auth/simkl/start"', body)
        self.assertIn("Continue with Simkl", body)

    def test_the_account_page_offers_connect_then_unlink(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.assertIn('href="/auth/simkl/link"', self.client.get("/me").text)
        self.callback(self.start("/auth/simkl/link"))
        body = self.client.get("/me").text
        self.assertIn('data-unlink="simkl"', body)
        self.assertNotIn('href="/auth/simkl/link"', body)

    def test_the_settings_response_reports_the_redirect_uri_to_register(self):
        """Simkl compares the registered redirect URI byte for byte and offers no
        device-code fallback for an operator who gets it wrong, so the exact
        value has to be readable off the screen."""
        self.sign_in_as(self.admin_id)
        data = self.client.get("/api/settings").json()
        self.assertTrue(data["simkl_login_configured"])
        self.assertEqual(data["simkl_redirect_uri"], f"{ORIGIN}/auth/simkl/callback")

    def test_the_registration_page_carries_the_invite_into_the_provider_flow(self):
        self.sign_out()
        token = self.mint_invite()
        body = self.client.get("/register", params={"invite": token}).text
        self.assertIn(f"/auth/simkl/start?invite={token}", body)


class _StubResponse:
    """The two fields app/auth/simkl.py reads off a response, and a
    raise_for_status that never fires — these stand in for a 200."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def simkl_transport():
    from app.providers.simkl import transport

    return transport


async def _fill_attempts(key_type: str, key_value: str, count: int) -> None:
    for _ in range(count):
        await auth.record_attempt(key_type, key_value, False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
