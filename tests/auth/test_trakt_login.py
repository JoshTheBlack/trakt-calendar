"""Log in with Trakt — handshake binding, the sign-in/register/link outcomes,
identity keying, and token-refresh serialization.

THE HANDSHAKE TESTS ARE THE POINT OF THIS FILE. The callback is a top-level GET
navigation, which SameSite=Lax deliberately sends cookies on, so an unbound one
is an account-takeover vector rather than a CSRF nit: a callback carrying an
attacker's Trakt identity, completed in a signed-in victim's browser, would link
that identity to the victim's account permanently. Every way of arriving at the
callback without having legitimately started the flow is asserted to be refused
here, and none of them may ever fall back to "no state, so assume a sign-in".

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
from app.auth import routes as auth_routes, trakt as trakt_auth, trakt_routes
from app.config import Settings, public_base_url_error
from tests.support import load_settings, save_settings
from app.main import app
from tests.auth._redirect_login import SharedRedirectSignInTests, SharedRedirectUnlinkTests
from tests.support import AppTestCase, ORIGIN

# The Trakt account the patched authorization returns unless a test says
# otherwise. An int, because the numeric account id is the only acceptable key
# for an identity row.
TRAKT_ID = 998877
OTHER_TRAKT_ID = 112233


def _settings(**overrides) -> Settings:
    """Settings with the Trakt redirect flow fully configured."""
    base = {
        "trakt_client_id": "client-id",
        "trakt_client_secret": "client-secret",
        "public_base_url": ORIGIN,
    }
    base.update(overrides)
    return Settings(**base)


class _Token(dict):
    """A Trakt token payload."""

    def __init__(self, access="access-1", refresh="refresh-1", expires_in=7776000,
                 created_at=1_700_000_000):
        super().__init__(access_token=access, refresh_token=refresh,
                         expires_in=expires_in, created_at=created_at)


class TraktOAuthTestCase(AppTestCase):
    # What tests/auth/_redirect_login.py's shared policy tests need to know about
    # this provider; its docstring lists them.
    PROVIDER = "trakt"
    ACCOUNT_ID, OTHER_ACCOUNT_ID = TRAKT_ID, OTHER_TRAKT_ID
    ACCESS_PREFIX = REFRESH_PREFIX = ""
    CONFIG_FIELDS = ("trakt_client_id", "trakt_client_secret", "public_base_url")
    SERVICE_HOST = "trakt.tv"
    auth_module, routes_module = trakt_auth, trakt_routes
    Token = _Token

    def settings_with(self, **overrides) -> Settings:
        return _settings(**overrides)

    def revoke_failure(self) -> Exception:
        return httpx.HTTPError("down")

    def make_settings(self):
        return _settings()

    def setUp(self):
        super().setUp()
        # Something has to exist or the first-run gate answers every request
        # before any of this is reached.
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)
        # Every revocation is recorded rather than sent: an unlink revokes, and so
        # does every refused completion (provider_login.end_unkept_grant) —
        # without this they reach the real api.trakt.tv.
        self.revoked: list[str] = []

        async def _revoke(client_id, client_secret, access_token):
            self.revoked.append(access_token)

        patcher = patch.object(trakt_auth, "revoke_token", side_effect=_revoke)
        self.revoke_mock = patcher.start()
        self.addCleanup(patcher.stop)

    # -- fixtures ----------------------------------------------------------

    def sign_out(self) -> None:
        self.client.cookies.clear()

    def mint_invite(self, **kwargs) -> str:
        return asyncio.run(auth.create_invite(created_by=self.admin_id, **kwargs))["token"]

    def start(self, path="/auth/trakt/start", **params) -> str:
        """Begin a flow and return the `state` the app generated."""
        resp = self.client.get(path, params=params, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        query = parse_qs(urlsplit(resp.headers["location"]).query)
        return query["state"][0]

    def pin_handshake_cookie(self, state) -> None:
        """Pretend the browser still holds the cookie for `state`.

        The cookie is the second half of the binding and is checked first, so
        tests that are about the handshake ROW have to satisfy it or they would
        pass for the wrong reason.
        """
        if state:
            self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)

    def callback(self, state, *, code="auth-code", account_id=TRAKT_ID, name="Josh",
                 token=None, pin=True, **params):
        """Complete a callback with the authorization patched out."""
        if pin:
            self.pin_handshake_cookie(state)
        with patch.object(trakt_auth, "exchange_code", return_value=token or _Token()), \
             patch.object(trakt_auth, "fetch_account",
                          return_value={"id": account_id, "name": name}):
            return self.client.get("/auth/trakt/callback",
                                   params={"state": state, "code": code, **params},
                                   follow_redirects=False)

    def identities(self):
        return asyncio.run(db.fetch_all("SELECT * FROM linked_identities"))

    def user_count(self) -> int:
        return int(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")))


class HandshakeBindingTests(TraktOAuthTestCase):
    """Every way of reaching the callback without having started the flow."""

    def test_a_completed_flow_consumes_its_handshake_exactly_once(self):
        state = self.start(invite=self.mint_invite())
        self.assertEqual(self.callback(state).status_code, 303)
        self.assertIsNotNone(asyncio.run(db.fetch_value(
            "SELECT consumed_at FROM auth_handshakes WHERE state = ?", (state,))))


    def test_an_unknown_state_is_refused(self):
        self.start()  # a real handshake exists; the callback just isn't for it
        resp = self.callback("state-that-was-never-issued")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(auth.HANDSHAKE_REJECTED, resp.text)
        self.assertEqual(self.identities(), [])


    def test_an_already_consumed_state_is_refused(self):
        """Replay: the same callback URL delivered a second time. The first use
        is what wrote the identity; the second must change nothing."""
        state = self.start(invite=self.mint_invite())
        self.assertEqual(self.callback(state).status_code, 303)
        self.sign_out()
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(self.identities()), 1)

    def test_a_foreign_session_cannot_complete_a_link_handshake(self):
        """THE TAKEOVER CASE. An attacker starts a link flow on their own
        account and hands the callback URL to a signed-in victim. The handshake
        is bound to the attacker's session, so the victim's browser cannot
        complete it — and the attacker's Trakt account never touches the
        victim's account."""
        attacker = self.make_user("attacker", calendar_approved=True)
        victim = self.make_user("victim", calendar_approved=True)
        self.sign_in_as(attacker)
        state = self.start("/auth/trakt/link")

        self.sign_out()
        self.sign_in_as(victim)
        # The victim's browser still carries the handshake cookie in this test,
        # which is the strictly harder case: even holding it, the session bound
        # into the row is what refuses.
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_a_link_handshake_cannot_be_completed_signed_out(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        state = self.start("/auth/trakt/link")
        self.client.cookies.delete(auth.COOKIE_NAME_SECURE)
        self.assertEqual(self.callback(state).status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_revoking_the_session_kills_its_in_flight_link_handshake(self):
        user = self.make_user("linker", calendar_approved=True)
        session_id = self.sign_in_as(user)
        state = self.start("/auth/trakt/link")
        asyncio.run(auth.revoke_session(session_id))
        self.assertIsNone(asyncio.run(db.fetch_one(
            "SELECT 1 FROM auth_handshakes WHERE state = ?", (state,))))


    def test_a_plex_handshake_cannot_be_completed_at_the_trakt_callback(self):
        state = asyncio.run(auth.create_handshake(provider="plex", purpose="login"))
        self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)
        self.assertEqual(self.callback(state).status_code, 400)

    def test_the_exchange_never_runs_for_a_refused_callback(self):
        """The handshake is validated BEFORE the authorization code is spent, so
        a forged callback costs one lookup and never reaches Trakt."""
        self.start()
        self.pin_handshake_cookie("nope")

        def _explode(*a, **kw):  # pragma: no cover — the assertion is that it isn't called
            raise AssertionError("the authorization code was exchanged for a bad state")

        with patch.object(trakt_auth, "exchange_code", side_effect=_explode):
            resp = self.client.get("/auth/trakt/callback",
                                   params={"state": "nope", "code": "c"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 400)

    def test_a_denial_from_trakt_changes_nothing(self):
        state = self.start()
        resp = self.client.get("/auth/trakt/callback",
                               params={"state": state, "error": "access_denied"},
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])


    def test_consuming_a_handshake_is_single_use_under_concurrency(self):
        """Two callbacks racing on one state resolve to exactly one success."""
        state = asyncio.run(auth.create_handshake(provider="trakt", purpose="login"))
        barrier = Barrier(2)

        def _consume():
            barrier.wait(timeout=5)
            try:
                asyncio.run(auth.consume_handshake(state, provider="trakt"))
                return "ok"
            except auth.HandshakeError:
                return "refused"
            finally:
                db.close_thread_connection()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(f.result() for f in [pool.submit(_consume), pool.submit(_consume)])
        self.assertEqual(results, ["ok", "refused"])

    def test_a_link_handshake_requires_a_session_to_create(self):
        with self.assertRaises(ValueError):
            asyncio.run(auth.create_handshake(provider="trakt", purpose="link"))


class StartRouteTests(TraktOAuthTestCase):
    def test_start_redirects_to_trakt_with_the_configured_redirect_uri(self):
        resp = self.client.get("/auth/trakt/start", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        target = urlsplit(resp.headers["location"])
        query = parse_qs(target.query)
        self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}",
                         trakt_auth.AUTHORIZE_URL)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["client-id"])
        # Built from the configured origin, never from the Host header — Trakt
        # compares it against the registered value exactly.
        self.assertEqual(query["redirect_uri"], [f"{ORIGIN}/auth/trakt/callback"])

class SignInOutcomeTests(TraktOAuthTestCase):
    """The register-or-sign-in-or-link matrix."""


    def test_an_unusable_invite_creates_no_account(self):
        self.sign_out()
        expired = self.mint_invite(expires_at=db.now() - 1)
        exhausted = self.mint_invite(max_uses=1)
        asyncio.run(db.execute("UPDATE invites SET used_count = 1 WHERE token = ?", (exhausted,)))
        revoked = self.mint_invite()
        asyncio.run(db.execute("UPDATE invites SET revoked = 1 WHERE token = ?", (revoked,)))

        bodies = set()
        for token in (expired, exhausted, revoked, "never-existed"):
            resp = self.callback(self.start(invite=token))
            self.assertEqual(resp.status_code, 403, token)
            bodies.add(resp.text)
        # Expired, exhausted, revoked, and unknown are one answer, not four.
        self.assertEqual(len(bodies), 1)
        self.assertEqual(self.user_count(), 1)


    def test_an_invite_without_the_calendar_grant_lands_on_the_account_page(self):
        self.sign_out()
        token = self.mint_invite(grants_calendar_on_accept=False)
        resp = self.callback(self.start(invite=token))
        self.assertEqual(resp.headers["location"], "/me")


    def test_a_disabled_account_cannot_sign_in(self):
        user = self.make_user("banned")
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="trakt", provider_user_id=TRAKT_ID)))
        asyncio.run(db.execute("UPDATE users SET is_disabled = 1 WHERE id = ?", (user,)))
        self.sign_out()
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 403)
        # Reported exactly like a wrong password, so the callback is not an
        # oracle for whether an account exists or has been disabled.
        self.assertIn(auth_routes.INVALID_CREDENTIALS, resp.text)

class LinkOutcomeTests(TraktOAuthTestCase):

    def test_linking_makes_distrakt_reachable_for_an_approved_user(self):
        """The Trakt half of the distrakt gate has never had a flow that
        satisfies it until now. Probed through a tracker read that touches
        neither the database nor the network, so the answer is the gate's."""
        probe = "/distrakt/fragments/day-label"
        user = self.make_user("watcher", calendar_approved=True, distrakt_approved=True)
        self.sign_in_as(user)
        self.assertEqual(self.client.get(probe).status_code, 403)
        self.callback(self.start("/auth/trakt/link"))
        self.assertEqual(self.client.get(probe).status_code, 200)

    def test_a_fresh_link_is_refused_while_encryption_is_unhealthy(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        with patch("app.auth.encryption_flow.secret_writes_blocked", return_value=True):
            resp = self.callback(self.start("/auth/trakt/link"))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.identities(), [])

    def test_relinking_is_refused_while_encryption_is_unhealthy(self):
        """A relink overwrites the row's tokens outright (see _refresh_identity)
        — the same overwrite save_settings() already refuses for app secrets
        while the key is missing or wrong, and for the same reason: with no
        working key, sealing is a pass-through, so the fresh tokens would land
        as plaintext over ciphertext the original key could still recover."""
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        original_access = self.identities()[0]["access_token"]

        with patch("app.auth.encryption_flow.secret_writes_blocked", return_value=True):
            resp = self.callback(self.start("/auth/trakt/link"),
                                 token=_Token(access="access-2", refresh="refresh-2"))
        self.assertEqual(resp.status_code, 409)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["access_token"], original_access)


class IdentityKeyTests(TraktOAuthTestCase):

    @staticmethod
    def _stub_client(payload, status=200):
        """An httpx.AsyncClient stand-in returning one canned response, and
        recording the URL it was asked for."""
        seen = {}

        class _Resp:
            status_code = status

            @staticmethod
            def json():
                return payload

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, *a, **kw):
                seen["url"] = url
                return _Resp()

        return _Client(), seen

    # The real /users/settings body, trimmed to what fetch_account reads. Trakt
    # users have NO numeric id: `ids` is {slug, uuid}, and the UUID is the only
    # stable handle there is.
    SETTINGS_BODY = {
        "user": {
            "username": "JoshTheBlack",
            "name": "JoshTheBlack",
            "ids": {"slug": "joshtheblack", "uuid": "30ee8617b5f3f670f90d88012b30adf4"},
        },
        "account": {"timezone": "America/New_York", "token": "unrelated-internal-value"},
    }

    def test_fetch_account_reads_the_uuid_from_users_settings(self):
        client, seen = self._stub_client(self.SETTINGS_BODY)
        with patch("app.auth.trakt.httpx.AsyncClient", return_value=client):
            account = asyncio.run(trakt_auth.fetch_account("cid", "token"))
        # The IDENTITY keys, asserted by name rather than by comparing the whole
        # dict: the shape grew an `avatar` key for profile-picture seeding, and a
        # test of "which id does this read" should not fail because a display-only
        # field was added beside it.
        self.assertEqual(account["id"], "30ee8617b5f3f670f90d88012b30adf4")
        self.assertEqual(account["name"], "JoshTheBlack")
        # /users/me cannot answer this — it returns ids:{slug} and nothing else.
        self.assertTrue(seen["url"].endswith("/users/settings"))

    def test_fetch_account_ignores_the_unrelated_account_token(self):
        """The settings response carries a Trakt-internal `account.token`. It must
        never end up on the identity row or anywhere near a log."""
        client, _ = self._stub_client(self.SETTINGS_BODY)
        with patch("app.auth.trakt.httpx.AsyncClient", return_value=client):
            account = asyncio.run(trakt_auth.fetch_account("cid", "token"))
        self.assertNotIn("unrelated-internal-value", str(account))

    def test_fetch_account_refuses_a_response_with_no_uuid(self):
        """A slug is reassignable, so a body carrying only one is refused rather
        than keyed on."""
        for body in ({"user": {"ids": {"slug": "josh"}}}, {"user": {}}, {}):
            with self.subTest(body=body):
                client, _ = self._stub_client(body)
                with patch("app.auth.trakt.httpx.AsyncClient", return_value=client):
                    with self.assertRaises(trakt_auth.AccountLookupError):
                        asyncio.run(trakt_auth.fetch_account("cid", "token"))


class RefreshSerializationTests(TraktOAuthTestCase):
    """Trakt issues a new refresh token every time one is spent and invalidates
    the old one, so two concurrent refreshes of the same identity would each
    succeed and then overwrite each other — leaving the row holding a token that
    no longer works."""

    def _expired_identity(self) -> int:
        user = self.make_user("watcher", calendar_approved=True)

        def _insert(conn):
            return auth.insert_linked_identity(
                conn, user_id=user, provider="trakt", provider_user_id=TRAKT_ID,
                access_token="old-access", refresh_token="old-refresh",
                token_expires_at=db.now() - 10)

        asyncio.run(db.transaction(_insert))
        return user

    def test_only_one_of_two_concurrent_refreshes_spends_the_token(self):
        user = self._expired_identity()
        identity_id = int(asyncio.run(db.fetch_value("SELECT id FROM linked_identities")))
        barrier = Barrier(2)
        spent = []

        async def _refresh(*a, **kw):
            spent.append(a)
            # Holds the lease while the other request tries to take it. Without
            # the pause the winner would finish and release before the loser
            # even looked, and the test would pass whether or not the lease
            # existed at all.
            await asyncio.sleep(0.5)
            return _Token(access="new-access", refresh="new-refresh")

        def _work():
            barrier.wait(timeout=5)
            try:
                with patch.object(trakt_auth, "refresh_access_token", side_effect=_refresh):
                    return asyncio.run(trakt_routes.access_token_for_user(user))
            finally:
                db.close_thread_connection()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = sorted(f.result() for f in [pool.submit(_work), pool.submit(_work)])

        # Exactly one request exchanged the refresh token; the other fell back to
        # the token already stored rather than spending it a second time.
        self.assertEqual(len(spent), 1)
        self.assertEqual(tokens, ["new-access", "old-access"])
        row = asyncio.run(db.fetch_one("SELECT * FROM linked_identities WHERE id = ?",
                                       (identity_id,)))
        self.assertEqual(row["access_token"], "new-access")
        self.assertEqual(row["refresh_token"], "new-refresh")
        self.assertIsNone(row["refreshing_until"])

    def test_the_lease_is_released_when_a_refresh_fails(self):
        import httpx

        user = self._expired_identity()
        with patch.object(trakt_auth, "refresh_access_token",
                          side_effect=httpx.ConnectError("down")):
            self.assertEqual(asyncio.run(trakt_routes.access_token_for_user(user)),
                             "old-access")
        self.assertIsNone(asyncio.run(db.fetch_value(
            "SELECT refreshing_until FROM linked_identities")))

    def test_a_lease_taken_by_a_dead_process_expires(self):
        identity_id = 1
        self._expired_identity()
        self.assertTrue(asyncio.run(auth.claim_identity_refresh(identity_id, now=1000)))
        self.assertFalse(asyncio.run(auth.claim_identity_refresh(identity_id, now=1001)))
        self.assertTrue(asyncio.run(auth.claim_identity_refresh(
            identity_id, now=1000 + auth.REFRESH_LEASE_SECONDS)))


    def test_a_user_with_no_linked_trakt_account_has_no_token(self):
        user = self.make_user("plain")
        self.assertIsNone(asyncio.run(trakt_routes.access_token_for_user(user)))


class UnlinkTests(SharedRedirectUnlinkTests, TraktOAuthTestCase):
    """Unlinking also asks Trakt to forget the authorization; the base case
    records that call in `self.revoked`.

    The answer is the whole account card at 200 (unlinking changes several of its
    sections), with any sentence in the line under the linked services."""

    def unlink(self, provider="trakt"):
        return self.client.post("/me/identities/unlink", json={"provider": provider})

    def said(self, resp) -> str:
        """The sentence under the linked services."""
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('id="accountCard"', resp.text)
        identities = resp.text.split('id="accountIdentities"', 1)[1]
        return re.search(r'<p class="auth-error[^"]*" role="\w+">(.*?)</p>', identities, re.S).group(1)

    def test_unlinking_revokes_the_token_at_trakt(self):
        user = self.make_user("revoker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        stored = self.identities()[0]["access_token"]
        resp = self.unlink()
        self.assertEqual(self.said(resp), "")
        self.assertEqual(self.revoked, [stored])
        # The card redrawn without the row's unlink and with its Connect back.
        self.assertIn('href="/auth/trakt/link"', resp.text)

    def test_a_refused_unlink_does_not_revoke_the_token(self):
        """The account keeps the identity, so the token it holds has to keep
        working — revoking on the way to a refusal would leave the only way in
        pointing at a dead credential."""
        self.sign_out()
        self.callback(self.start(invite=self.mint_invite()))
        self.assertIn("only way you can sign in", self.said(self.unlink()))
        self.assertEqual(self.revoked, [])
        self.assertEqual(len(self.identities()), 1)


    def test_a_user_with_a_password_can_unlink(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        self.assertEqual(self.said(self.unlink()), "")
        self.assertEqual(self.identities(), [])


    def test_an_unknown_provider_is_rejected(self):
        user = self.make_user("plain", calendar_approved=True)
        self.sign_in_as(user)
        self.assertEqual(self.said(self.unlink("netflix")), "Unknown provider.")

    def test_unlink_requires_a_session(self):
        self.sign_out()
        self.assertEqual(self.unlink().status_code, 401)

    def test_unlink_refuses_a_form_encoded_body(self):
        user = self.make_user("formpost", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        resp = self.client.post("/me/identities/unlink", data={"provider": "trakt"})
        self.assertEqual(resp.status_code, 415)
        self.assertEqual(len(self.identities()), 1)


class PublicBaseUrlTests(TraktOAuthTestCase):
    def test_validation(self):
        self.assertIsNone(public_base_url_error(""))
        self.assertIsNone(public_base_url_error("https://shows.example.com"))
        self.assertIsNone(public_base_url_error("http://localhost:8000"))
        for bad in ("shows.example.com", "ftp://shows.example.com",
                    "https://shows.example.com/", "https://shows.example.com/app",
                    "https://shows.example.com?x=1"):
            self.assertIsNotNone(public_base_url_error(bad), bad)

    def test_the_settings_endpoint_refuses_an_invalid_base_url(self):
        self.sign_in_as(self.admin_id)
        resp = self.client.post("/settings",
                                json={"public_base_url": "https://shows.example.com/app"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("origin only", resp.text)
        # Nothing was written: the stored value is still the valid one.
        self.assertEqual(load_settings().public_base_url, ORIGIN)

    def test_a_hand_edited_trailing_slash_is_normalized(self):
        self.assertEqual(
            Settings.from_dict({"public_base_url": "https://shows.example.com/"}).public_base_url,
            "https://shows.example.com")

    def test_the_configured_origin_beats_the_host_header(self):
        """The origin check falls back to the request's Host only while no base
        URL is configured; setting one has to make it authoritative."""
        save_settings(_settings(public_base_url="https://real.example.com"))
        self.sign_in_as(self.admin_id)
        # The client's own Origin (https://testserver) now disagrees with the
        # configured one, and that is what a mutating request is judged against.
        resp = self.client.post("/settings", json={})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["reason"], "cross_origin")

        resp = self.client.post("/settings", json={},
                                headers={"Origin": "https://real.example.com"})
        self.assertEqual(resp.status_code, 204)

    def test_the_settings_panel_shows_the_redirect_uri_to_register(self):
        self.sign_in_as(self.admin_id)
        body = self.client.get("/settings/fragments/panel").text
        self.assertIn(f'value="{ORIGIN}/auth/trakt/callback"', body)
        self.assertIn('id="traktReconnect" class="field" hidden', body)


class ReconnectNoticeTests(TraktOAuthTestCase):
    def setUp(self):
        super().setUp()
        # The notice is about a token this instance HAS but could not resolve,
        # so every test here needs one saved.
        save_settings(_settings(trakt_access_token="app-wide-token"))

    def notice_shown(self) -> bool:
        """Whether the Settings panel draws the reconnect notice, which is always
        in the markup and hidden when there is nothing to ask."""
        body = self.client.get("/settings/fragments/panel").text
        self.assertIn('id="traktReconnect"', body)
        return 'id="traktReconnect" class="field" hidden' not in body

    def test_the_notice_is_surfaced_and_cleared_by_an_admin_linking(self):
        asyncio.run(db.set_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "1"))
        self.sign_in_as(self.admin_id)
        self.assertTrue(self.notice_shown())

        self.callback(self.start("/auth/trakt/link"))
        self.assertFalse(self.notice_shown())

    def test_a_non_admin_linking_leaves_the_notice_up(self):
        asyncio.run(db.set_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "1"))
        user = self.make_user("plain", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        self.assertEqual(asyncio.run(db.get_meta(auth_routes.TRAKT_RECONNECT_NOTICE)), "1")

    def test_the_notice_can_be_cleared_by_adopting_the_saved_token(self):
        """The button the notice offers: link the token this instance already
        has, with no second trip through Trakt."""
        asyncio.run(db.set_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "1"))
        self.sign_in_as(self.admin_id)
        with patch.object(trakt_auth, "fetch_account",
                          return_value={"id": TRAKT_ID, "name": "Josh"}):
            resp = self.client.post("/settings/trakt/adopt", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        # The answer is the notice itself, taken down.
        self.assertIn('id="traktReconnect" class="field" hidden', resp.text)
        self.assertFalse(self.notice_shown())

    def test_adoption_blocked_by_another_login_says_which_one(self):
        """The failure that re-authorizing can never fix. Reporting it is the
        whole point: the notice used to stay up saying nothing, so the only
        remedy on offer was the one that could not work."""
        asyncio.run(db.set_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "1"))
        squatter = self.make_user("squatter", calendar_approved=True)
        self.sign_in_as(squatter)
        self.callback(self.start("/auth/trakt/link"))

        self.sign_in_as(self.admin_id)
        with patch.object(trakt_auth, "fetch_account",
                          return_value={"id": TRAKT_ID, "name": "Josh"}):
            resp = self.client.post("/settings/trakt/adopt", json={})
        # Drawn where the notice is, at 200: a refusal statused 409 is never swapped.
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("squatter", resp.text)
        # And it is still up, because this login is still unlinked.
        self.assertTrue(self.notice_shown())

    def test_adopting_a_token_of_another_account_than_the_one_linked_is_refused(self):
        """One account of a service per login: an administrator who linked one
        Trakt account is not given the app-wide token's other one beside it."""
        self.sign_in_as(self.admin_id)
        self.link_identity(self.admin_id, "trakt", TRAKT_ID)
        with patch.object(trakt_auth, "fetch_account",
                          return_value={"id": OTHER_TRAKT_ID, "name": "Other"}):
            linked, why = asyncio.run(trakt_routes.adopt_app_token(
                self.admin_id, load_settings()))
        self.assertFalse(linked)
        self.assertIn("different Trakt account", why)
        rows = asyncio.run(db.fetch_all(
            "SELECT provider_user_id FROM linked_identities WHERE user_id = ?", (self.admin_id,)))
        self.assertEqual([row["provider_user_id"] for row in rows], [str(TRAKT_ID)])

    def test_a_token_trakt_will_not_resolve_reports_that_instead(self):
        asyncio.run(db.set_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "1"))
        self.sign_in_as(self.admin_id)
        with patch.object(trakt_auth, "fetch_account",
                          side_effect=trakt_auth.AccountLookupError("HTTP 401")):
            resp = self.client.post("/settings/trakt/adopt", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("HTTP 401", resp.text)


class ProviderButtonTests(TraktOAuthTestCase):
    def test_the_sign_in_page_offers_trakt_once_it_is_configured(self):
        self.sign_out()
        self.assertIn('href="/auth/trakt/start"', self.client.get("/login").text)

    def test_the_button_is_inert_until_it_is_configured(self):
        save_settings(_settings(public_base_url=""))
        self.sign_out()
        body = self.client.get("/login").text
        self.assertNotIn('href="/auth/trakt/start"', body)
        self.assertIn("disabled", body)


    def test_an_oauth_only_account_is_not_offered_an_unlink_it_cannot_use(self):
        self.sign_out()
        self.callback(self.start(invite=self.mint_invite()))
        self.assertNotIn('id="confirm-unlink-trakt"', self.client.get("/me").text)

class SharedSignInTests(SharedRedirectSignInTests, TraktOAuthTestCase):
    """The redirect sign-in policy Trakt shares with Simkl, run through Trakt's
    own routes — see tests/auth/_redirect_login.py."""


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
