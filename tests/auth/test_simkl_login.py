"""Log in with Simkl v2 — handshake binding with PKCE, the sign-in/register/link
outcomes, identity keying, the scope check, legacy-token handling, and
token-refresh serialization.

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
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from app import auth, db
from app.auth import routes as auth_routes, simkl as simkl_auth, trakt_routes
from app.auth import simkl_routes
from app.config import Settings
from tests.support import save_settings
from tests.auth._redirect_login import (SharedRedirectSignInTests,
                                        SharedRedirectUnlinkTests, fill_attempts)
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
    """A Simkl v2 token payload — access AND refresh, both scopes granted."""

    def __init__(self, access="simkl_at_access-1", refresh="simkl_rt_refresh-1",
                expires_in=604_800, scope="media:read media:write"):
        super().__init__(access_token=access, refresh_token=refresh, token_type="Bearer",
                         scope=scope, expires_in=expires_in)


class SimklOAuthTestCase(AppTestCase):
    # What tests/auth/_redirect_login.py's shared policy tests need to know about
    # this provider; its docstring lists them.
    PROVIDER = "simkl"
    ACCOUNT_ID, OTHER_ACCOUNT_ID = SIMKL_ID, OTHER_SIMKL_ID
    ACCESS_PREFIX = simkl_auth.V2_ACCESS_TOKEN_PREFIX
    REFRESH_PREFIX = "simkl_rt_"
    CONFIG_FIELDS = ("simkl_client_id", "simkl_client_secret", "public_base_url")
    SERVICE_HOST = "simkl.com"
    auth_module, routes_module = simkl_auth, simkl_routes
    Token = _Token

    def settings_with(self, **overrides) -> Settings:
        return _settings(**overrides)

    def revoke_failure(self) -> Exception:
        return simkl_transport().SimklError("down")

    def make_settings(self):
        return _settings()

    def setUp(self):
        super().setUp()
        # Something has to exist or the first-run gate answers every request
        # before any of this is reached.
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)
        # Every revocation is recorded rather than sent: an unlink revokes, and so
        # does every refused completion (provider_login.end_unkept_grant) —
        # without this they reach the real api.simkl.com.
        self.revoked: list[str] = []

        async def _revoke(client_id, client_secret, access_token):
            self.revoked.append(access_token)

        self.real_revoke_token = simkl_auth.revoke_token
        patcher = patch.object(simkl_auth, "revoke_token", side_effect=_revoke)
        self.revoke_mock = patcher.start()
        self.addCleanup(patcher.stop)

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

    def callback(self, state, *, code="auth-code", account_id=SIMKL_ID, name="Josh",
                 token=None, pin=True, **params):
        """Complete a callback with the authorization patched out."""
        if pin:
            self.pin_handshake_cookie(state)
        with patch.object(simkl_auth, "exchange_code", return_value=token or _Token()), \
             patch.object(simkl_auth, "fetch_account",
                          return_value={"id": account_id, "name": name}):
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
        self.assertEqual(query["scope"], [simkl_auth.SCOPE])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        # The verifier itself must never appear here — only its S256 challenge.
        challenge = query["code_challenge"][0]
        state = query["state"][0]
        row = asyncio.run(db.fetch_one(
            "SELECT pkce_verifier FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertNotEqual(challenge, row["pkce_verifier"])
        self.assertNotIn(row["pkce_verifier"], resp.headers["location"])

class SignInOutcomeTests(SimklOAuthTestCase):
    """The register-or-sign-in matrix, answered entirely by the shared
    completion in app/auth/provider_login.py."""


    def test_a_returning_identity_is_not_throttled(self):
        """Only a REGISTRATION spends the throttle budget. A household behind one
        address must not be locked out of signing in because somebody there
        registered earlier."""
        user = self.make_user("regular", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="simkl", provider_user_id=SIMKL_ID)))
        self.sign_out()
        asyncio.run(fill_attempts("register_ip", "testclient", auth.REGISTER_MAX_ATTEMPTS))
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
        # A GET, per Simkl's API reference (measured 2026-09-27: it also answers
        # on a V1 token), on the pool that serializes and paces /users/ traffic.
        self.assertEqual(seen["method"], "GET")
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
            seen["data"] = kwargs.get("data")
            return _StubResponse(_Token())

        with patch("app.providers.simkl.transport.send", side_effect=_send):
            token = asyncio.run(simkl_auth.exchange_code(
                "cid", "secret", "the-code", ORIGIN, "the-verifier"))
        self.assertEqual(token["access_token"], "simkl_at_access-1")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], simkl_auth.TOKEN_URL)
        # The pacer and the 412 breaker cover the sign-in exchange too, which is
        # the whole reason it goes through the source's transport.
        self.assertIs(seen["pool"], simkl_transport().SYNC_POOL)
        # FORM-ENCODED, per RFC 6749 and Simkl's own examples — sent as `data`,
        # never `json`, so httpx sets application/x-www-form-urlencoded itself.
        self.assertEqual(seen["data"]["redirect_uri"], f"{ORIGIN}/auth/simkl/callback")
        self.assertEqual(seen["data"]["grant_type"], "authorization_code")
        self.assertEqual(seen["data"]["code_verifier"], "the-verifier")

    def test_refresh_access_token_is_also_form_encoded_and_on_the_sync_pool(self):
        seen = {}

        async def _send(client, method, url, **kwargs):
            seen["url"] = url
            seen["data"] = kwargs.get("data")
            seen["pool"] = kwargs.get("pool")
            return _StubResponse(_Token(access="refreshed"))

        with patch("app.providers.simkl.transport.send", side_effect=_send):
            token = asyncio.run(simkl_auth.refresh_access_token(
                "cid", "secret", "old-refresh"))
        self.assertEqual(token["access_token"], "refreshed")
        self.assertEqual(seen["url"], simkl_auth.TOKEN_URL)
        self.assertEqual(seen["data"]["grant_type"], "refresh_token")
        self.assertEqual(seen["data"]["refresh_token"], "old-refresh")
        self.assertIs(seen["pool"], simkl_transport().SYNC_POOL)

    def test_revoke_token_posts_to_the_v2_revoke_endpoint(self):
        seen = {}

        async def _send(client, method, url, **kwargs):
            seen["url"] = url
            seen["data"] = kwargs.get("data")
            return _StubResponse({})

        with patch("app.providers.simkl.transport.send", side_effect=_send):
            asyncio.run(self.real_revoke_token("cid", "secret", "a-token"))
        self.assertEqual(seen["url"], simkl_auth.REVOKE_URL)
        self.assertEqual(seen["data"]["token"], "a-token")

    def test_generate_pkce_pair_challenge_is_s256_of_the_verifier(self):
        import base64
        import hashlib

        verifier, challenge = simkl_auth.generate_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self.assertEqual(challenge, expected)
        self.assertNotEqual(verifier, challenge)
        # Two calls never collide — this is what makes a stolen code useless
        # without the verifier that produced its challenge.
        other_verifier, _ = simkl_auth.generate_pkce_pair()
        self.assertNotEqual(verifier, other_verifier)


class ScopeRefusalTests(SimklOAuthTestCase):
    """Simkl grants access on request SHAPE, and a scope missing media:write is
    refused before anything is stored — see simkl_routes.simkl_callback."""

    def test_a_read_only_grant_is_refused_and_nothing_is_stored(self):
        resp = self.callback(self.start(), token=_Token(scope="media:read"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn(simkl_routes.SCOPE_REFUSED, resp.text)
        self.assertEqual(self.identities(), [])

    def test_a_missing_scope_field_entirely_is_also_refused(self):
        token = _Token()
        del token["scope"]
        resp = self.callback(self.start(), token=token)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_a_link_refusal_sends_the_visitor_back_to_their_account(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.callback(self.start("/auth/simkl/link"), token=_Token(scope="media:read"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('href="/me?tab=sign-in"', resp.text)


class LegacyTokenTests(SimklOAuthTestCase):
    """A V1 token predates the move to Simkl's AUTH V2, which kept no V1
    registration alive to send it with, and is told apart from a V2 one purely
    by shape — Simkl gives no other
    signal. It is read as "reconnect needed" everywhere, and the identity row
    is KEPT rather than dropped."""

    def _v1_shaped_identity(self, user_id: int) -> int:
        def _insert(conn):
            return auth.insert_linked_identity(
                conn, user_id=user_id, provider="simkl", provider_user_id=SIMKL_ID,
                access_token="a" * 64, refresh_token=None, token_expires_at=None)

        return asyncio.run(db.transaction(_insert))

    def test_a_legacy_token_reads_as_reconnect_needed(self):
        user = self.make_user("legacy", calendar_approved=True)
        self._v1_shaped_identity(user)
        self.assertIsNone(asyncio.run(simkl_routes.access_token_for_user(user)))

    def test_a_legacy_token_is_never_sent_through_refresh(self):
        """No refresh_token sits beside a V1-shaped access token (it predates V2
        entirely), so a refresh attempt would only fail — but the point is that
        _is_v2_token stops it before that failure is even attempted."""
        user = self.make_user("legacy", calendar_approved=True)
        self._v1_shaped_identity(user)

        def _explode(*a, **kw):  # pragma: no cover — asserting it isn't called
            raise AssertionError("a legacy Simkl token reached the v2 refresh endpoint")

        with patch.object(simkl_auth, "refresh_access_token", side_effect=_explode):
            self.assertIsNone(asyncio.run(simkl_routes.access_token_for_user(user)))

    def test_reconnecting_replaces_the_token_on_the_same_row(self):
        user = self.make_user("legacy", calendar_approved=True)
        identity_id = self._v1_shaped_identity(user)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["id"]), identity_id)
        self.assertEqual(rows[0]["access_token"], "simkl_at_access-1")

    def test_the_account_page_offers_reconnect_not_linked(self):
        """Neutral wording: a 401 can also mean the person revoked the app on
        simkl.com, so the page never claims a specific cause — only that
        reconnecting is needed. A working link (RefreshSerializationTests'
        shape) draws the ordinary "Linked" badge instead — see that class."""
        user = self.make_user("legacy", calendar_approved=True)
        self._v1_shaped_identity(user)
        self.sign_in_as(user)
        body = self.client.get("/me").text
        self.assertIn("Needs reconnecting", body)
        self.assertIn('href="/auth/simkl/link"', body)
        self.assertNotIn(">Linked<", body)


class RefreshSerializationTests(SimklOAuthTestCase):
    """Simkl v2 issues a real refresh token (180 days, sliding, non-rotating)
    where v1 issued none — see app/auth/simkl.py's module docstring. One grant
    is one live access token, so two concurrent refreshes of the same identity
    would each succeed and then cut each other off; the identity row's lease
    (the same one Trakt's refresh already serializes through) is what stops
    that."""

    def _expired_identity(self) -> int:
        user = self.make_user("watcher", calendar_approved=True)

        def _insert(conn):
            return auth.insert_linked_identity(
                conn, user_id=user, provider="simkl", provider_user_id=SIMKL_ID,
                access_token=simkl_auth.V2_ACCESS_TOKEN_PREFIX + "old",
                refresh_token="old-refresh", token_expires_at=db.now() - 10)

        asyncio.run(db.transaction(_insert))
        return user

    def test_only_one_of_two_concurrent_refreshes_spends_the_token(self):
        user = self._expired_identity()
        identity_id = int(asyncio.run(db.fetch_value("SELECT id FROM linked_identities")))
        barrier = Barrier(2)
        spent = []

        async def _refresh(*a, **kw):
            spent.append(a)
            # Holds the lease while the other request tries to take it — without
            # the pause the winner would finish and release before the loser
            # even looked, and the test would pass whether or not the lease
            # existed at all.
            await asyncio.sleep(0.5)
            return _Token(access=simkl_auth.V2_ACCESS_TOKEN_PREFIX + "new",
                         refresh="new-refresh")

        def _work():
            barrier.wait(timeout=5)
            try:
                with patch.object(simkl_auth, "refresh_access_token", side_effect=_refresh):
                    return asyncio.run(simkl_routes.access_token_for_user(user))
            finally:
                db.close_thread_connection()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = sorted(f.result() for f in [pool.submit(_work), pool.submit(_work)])

        self.assertEqual(len(spent), 1)
        self.assertEqual(tokens, sorted([simkl_auth.V2_ACCESS_TOKEN_PREFIX + "new",
                                         simkl_auth.V2_ACCESS_TOKEN_PREFIX + "old"]))
        row = asyncio.run(db.fetch_one("SELECT * FROM linked_identities WHERE id = ?",
                                       (identity_id,)))
        self.assertEqual(row["access_token"], simkl_auth.V2_ACCESS_TOKEN_PREFIX + "new")
        self.assertEqual(row["refresh_token"], "new-refresh")
        self.assertIsNone(row["refreshing_until"])

    def test_a_failed_refresh_leaves_reconnect_needed_and_never_loops(self):
        """Trakt's equivalent falls back to the stale access token; Simkl's does
        not, DELIBERATELY — a revoked v2 grant does not recover by being asked
        again, and returning the stale token would let every caller keep
        retrying a dead credential instead of the tracker's "reconnect" path
        taking over."""
        user = self._expired_identity()
        with patch.object(simkl_auth, "refresh_access_token",
                          side_effect=httpx.ConnectError("down")):
            self.assertIsNone(asyncio.run(simkl_routes.access_token_for_user(user)))
        self.assertIsNone(asyncio.run(db.fetch_value(
            "SELECT refreshing_until FROM linked_identities")))

class PerUserTokenTests(SimklOAuthTestCase):
    """Reading back the token the tracker authenticates one person's reads
    with. A linked account's stored token round-trips through sealing exactly
    the way Trakt's does — see tests/encryption/ for the sealed round trip
    itself, which is provider-agnostic."""

    def test_a_linked_account_reads_back_its_own_token(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        self.assertEqual(asyncio.run(simkl_routes.access_token_for_user(user)),
                         "simkl_at_access-1")

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


class UnlinkTests(SharedRedirectUnlinkTests, SimklOAuthTestCase):
    """Unlinking also asks Simkl to forget the authorization (v2 documents
    POST /oauth2/revoke where v1 documented none); the base case records that
    call in `self.revoked`.

    Answered with the account card at 200; the sentence, if any, sits under the
    linked services."""

    def said(self, resp) -> str:
        self.assertEqual(resp.status_code, 200, resp.text)
        identities = resp.text.split('id="accountIdentities"', 1)[1]
        return re.search(r'<p class="auth-error[^"]*" role="\w+">(.*?)</p>', identities, re.S).group(1)

    def unlink(self):
        return self.client.post("/me/identities/unlink", json={"provider": "simkl"})

    def test_unlinking_revokes_the_token_at_simkl(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        stored = self.identities()[0]["access_token"]
        resp = self.unlink()
        self.assertEqual(self.identities(), [])
        self.assertEqual(self.said(resp), "")
        self.assertEqual(self.revoked, [stored])

    def test_a_legacy_token_is_not_sent_to_the_v2_revoke_endpoint(self):
        """A V1-shaped token was never issued by the v2 endpoint this revoke
        call is for, and the app already treats it as unusable everywhere else — so
        there is nothing to send, and nothing to warn the visitor about either."""
        user = self.make_user("legacy", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="simkl", provider_user_id=SIMKL_ID,
            access_token="a" * 64)))
        self.sign_in_as(user)
        resp = self.unlink()
        self.assertEqual(self.identities(), [])
        self.assertEqual(self.said(resp), "")
        self.assertEqual(self.revoked, [])


    def test_a_linked_simkl_account_can_be_unlinked(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/simkl/link"))
        resp = self.unlink()
        self.assertEqual(self.identities(), [])
        self.assertEqual(self.said(resp), "")

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


    def test_the_settings_panel_shows_the_redirect_uri_to_register(self):
        """Simkl compares the registered redirect URI byte for byte and offers no
        device-code fallback for an operator who gets it wrong, so the exact
        value has to be readable off the screen."""
        self.sign_in_as(self.admin_id)
        body = self.client.get("/settings/fragments/panel").text
        self.assertIn(f'value="{ORIGIN}/auth/simkl/callback"', body)
        self.assertIn('data-copy="#s_simkl_redirect_uri"', body)

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


class SharedSignInTests(SharedRedirectSignInTests, SimklOAuthTestCase):
    """The redirect sign-in policy Simkl shares with Trakt, run through Simkl's
    own routes — see tests/auth/_redirect_login.py."""

    def test_the_pkce_verifier_rides_the_handshake_row(self):
        """Simkl's own addition to the shared start: PKCE is mandatory under its
        OAuth v2, and the verifier is generated fresh per handshake and kept
        server-side on the row, never in the redirect."""
        state = self.start(invite=self.mint_invite())
        row = asyncio.run(db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertTrue(row["pkce_verifier"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
