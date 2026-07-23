"""Log in with Trakt — handshake binding, the sign-in/register/link outcomes,
identity keying, and token-refresh serialization.

THE HANDSHAKE TESTS ARE THE POINT OF THIS FILE. The callback is a top-level GET
navigation, which SameSite=Lax deliberately sends cookies on, so an unbound one
is an account-takeover vector rather than a CSRF nit: a callback carrying an
attacker's Trakt identity, completed in a signed-in victim's browser, would link
that identity to the victim's account permanently. Every way of arriving at the
callback without having legitimately started the flow is asserted to be refused
here, and none of them may ever fall back to "no state, so assume a sign-in".

No network: the code exchange and the account lookup are patched. TRAKT_DATA_DIR
points at a temp dir (set BEFORE importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_trakt_oauth -v
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
from urllib.parse import parse_qs, urlsplit

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-trakt-oauth-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, auth_routes, db, trakt_auth, trakt_routes  # noqa: E402
from app.config import Settings, public_base_url_error, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"

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


class TraktOAuthTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        TraktOAuthTestCase._counter += 1
        db.set_db_path(TMP / f"oauth-{TraktOAuthTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(_settings())
        # https, because the session and handshake cookies are Secure by default
        # and a client honoring that won't send them back over plain http.
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        # Something has to exist or the first-run gate answers every request
        # before any of this is reached.
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    # -- fixtures ----------------------------------------------------------

    def make_user(self, username, **flags) -> int:
        return asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=Settings(), **flags))

    def sign_in_as(self, user_id: int) -> str:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)
        return session_id

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

    def callback(self, state, *, code="auth-code", trakt_id=TRAKT_ID, name="Josh",
                 token=None, pin=True, **params):
        """Complete a callback with the authorization patched out."""
        if pin:
            self.pin_handshake_cookie(state)
        with patch.object(trakt_auth, "exchange_code", return_value=token or _Token()), \
             patch.object(trakt_auth, "fetch_account",
                          return_value={"id": trakt_id, "name": name}):
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

    def test_a_callback_with_no_state_at_all_is_refused(self):
        """There is deliberately no "no state, so assume a sign-in" path — that
        would restore the exact hole the handshake table exists to close."""
        self.start()
        with patch.object(trakt_auth, "exchange_code", return_value=_Token()):
            resp = self.client.get("/auth/trakt/callback", params={"code": "auth-code"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_an_unknown_state_is_refused(self):
        self.start()  # a real handshake exists; the callback just isn't for it
        resp = self.callback("state-that-was-never-issued")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(auth.HANDSHAKE_REJECTED, resp.text)
        self.assertEqual(self.identities(), [])

    def test_an_expired_state_is_refused(self):
        state = self.start()
        asyncio.run(db.execute(
            "UPDATE auth_handshakes SET expires_at = ? WHERE state = ?",
            (db.now() - 1, state)))
        self.assertEqual(self.callback(state).status_code, 400)
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

    def test_a_callback_in_another_browser_is_refused(self):
        """The handshake cookie pins the flow to the browser that started it, so
        a callback URL forwarded to somebody else is worthless even while its
        state row is still unconsumed."""
        state = self.start()
        self.client.cookies.clear()  # a different browser
        resp = self.callback(state, pin=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])
        # ...and the handshake was not spent, so the visitor who started it can
        # still finish.
        self.assertIsNone(asyncio.run(db.fetch_value(
            "SELECT consumed_at FROM auth_handshakes WHERE state = ?", (state,))))

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

    def test_every_refusal_reads_the_same(self):
        """Unknown, expired, consumed, and foreign-session must not be tellable
        apart, or the callback becomes a probe for which guess was closest."""
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

    def test_the_redirect_uri_ignores_a_spoofed_host_header(self):
        resp = self.client.get("/auth/trakt/start", follow_redirects=False,
                               headers={"Host": "evil.example.com"})
        query = parse_qs(urlsplit(resp.headers["location"]).query)
        self.assertEqual(query["redirect_uri"], [f"{ORIGIN}/auth/trakt/callback"])

    def test_start_is_unavailable_until_the_instance_is_configured(self):
        for missing in ("trakt_client_id", "trakt_client_secret", "public_base_url"):
            save_settings(_settings(**{missing: ""}))
            resp = self.client.get("/auth/trakt/start", follow_redirects=False)
            self.assertEqual(resp.status_code, 503, missing)
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM auth_handshakes"))), 0)

    def test_link_requires_a_session(self):
        self.sign_out()
        resp = self.client.get("/auth/trakt/link", follow_redirects=False,
                               headers={"Accept": "application/json"})
        self.assertEqual(resp.status_code, 401)

    def test_the_invite_travels_in_the_handshake_row_not_the_redirect(self):
        token = self.mint_invite()
        state = self.start(invite=token)
        row = asyncio.run(db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertEqual(row["invite_token"], token)
        self.assertIsNone(row["session_id"])


class SignInOutcomeTests(TraktOAuthTestCase):
    """The register-or-sign-in-or-link matrix."""

    def test_a_known_identity_signs_its_owner_in(self):
        user = self.make_user("known", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="trakt", provider_user_id=TRAKT_ID,
            display_name="Old Name")))
        self.sign_out()

        resp = self.callback(self.start(), name="New Name")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")
        me = self.client.get("/me")
        self.assertEqual(me.status_code, 200)
        self.assertIn("known", me.text)
        # One identity still, with its display name refreshed and its token
        # written — the name is display-only, so keeping it current is free.
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "New Name")
        self.assertEqual(rows[0]["access_token"], "access-1")

    def test_an_unknown_identity_with_no_invite_creates_no_account(self):
        """Registration through the provider path is invite-gated exactly as
        registration with a password is — a Trakt sign-in proves only that
        somebody controls some Trakt account."""
        self.sign_out()
        before = self.user_count()
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 403)
        self.assertIn(auth_routes.INVALID_INVITE, resp.text)
        self.assertEqual(self.user_count(), before)
        self.assertEqual(self.identities(), [])

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

    def test_a_valid_invite_registers_and_grants_calendar(self):
        self.sign_out()
        token = self.mint_invite()
        resp = self.callback(self.start(invite=token))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")

        user = asyncio.run(db.fetch_one(
            "SELECT u.* FROM users u JOIN linked_identities li ON li.user_id = u.id "
            "WHERE li.provider_user_id = ?", (str(TRAKT_ID),)))
        self.assertIsNotNone(user)
        self.assertIsNone(user["username"])
        self.assertIsNone(user["password_hash"])
        self.assertTrue(user["calendar_approved"])
        # An invite never grants distrakt: that one exposes a user's private
        # watch history and is always a separate manual grant.
        self.assertFalse(user["distrakt_approved"])
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT used_count FROM invites WHERE token = ?", (token,)))), 1)
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM invite_redemptions"))), 1)

    def test_an_invite_without_the_calendar_grant_lands_on_the_account_page(self):
        self.sign_out()
        token = self.mint_invite(grants_calendar_on_accept=False)
        resp = self.callback(self.start(invite=token))
        self.assertEqual(resp.headers["location"], "/me")

    def test_open_registration_needs_no_invite_but_grants_nothing(self):
        save_settings(_settings(allow_open_registration=True))
        self.sign_out()
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")
        user = asyncio.run(db.fetch_one(
            "SELECT u.* FROM users u JOIN linked_identities li ON li.user_id = u.id "
            "WHERE li.provider_user_id = ?", (str(TRAKT_ID),)))
        self.assertFalse(user["calendar_approved"])

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

    def test_registration_through_the_provider_path_is_rate_limited(self):
        self.sign_out()
        asyncio.run(_fill_attempts("register_ip", "testclient", auth.REGISTER_MAX_ATTEMPTS))
        resp = self.callback(self.start(invite=self.mint_invite()))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self.user_count(), 1)


class LinkOutcomeTests(TraktOAuthTestCase):
    def test_linking_attaches_the_identity_to_the_signed_in_account(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.callback(self.start("/auth/trakt/link"))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["user_id"]), user)
        self.assertEqual(rows[0]["provider_user_id"], str(TRAKT_ID))

    def test_an_identity_linked_elsewhere_is_refused_never_moved(self):
        """Whoever authorizes last must not be able to take an identity away
        from the account already holding it."""
        owner = self.make_user("owner", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=owner, provider="trakt", provider_user_id=TRAKT_ID)))
        interloper = self.make_user("interloper", calendar_approved=True)
        self.sign_in_as(interloper)

        resp = self.callback(self.start("/auth/trakt/link"))
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already linked to another user", resp.text)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["user_id"]), owner)

    def test_relinking_the_same_account_refreshes_the_stored_token(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        self.callback(self.start("/auth/trakt/link"),
                      token=_Token(access="access-2", refresh="refresh-2"))
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["access_token"], "access-2")
        self.assertEqual(rows[0]["refresh_token"], "refresh-2")

    def test_linking_makes_distrakt_reachable_for_an_approved_user(self):
        """The Trakt half of the distrakt gate has never had a flow that
        satisfies it until now."""
        user = self.make_user("watcher", calendar_approved=True, distrakt_approved=True)
        self.sign_in_as(user)
        self.assertEqual(self.client.get("/api/distrakt/months").status_code, 403)
        self.callback(self.start("/auth/trakt/link"))
        self.assertEqual(self.client.get("/api/distrakt/months").status_code, 200)

    def test_a_fresh_link_is_refused_while_encryption_is_unhealthy(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        with patch("app.encryption_flow.secret_writes_blocked", return_value=True):
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

        with patch("app.encryption_flow.secret_writes_blocked", return_value=True):
            resp = self.callback(self.start("/auth/trakt/link"),
                                 token=_Token(access="access-2", refresh="refresh-2"))
        self.assertEqual(resp.status_code, 409)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["access_token"], original_access)


class IdentityKeyTests(TraktOAuthTestCase):
    def test_the_identity_is_keyed_on_the_numeric_id_not_the_display_name(self):
        """A Trakt username or slug can be changed by its owner and re-registered
        by somebody else, so keying on one would let a released name inherit the
        linked account. Renaming must resolve to the SAME identity row."""
        user = self.make_user("renamer", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"), name="Before")
        self.callback(self.start("/auth/trakt/link"), name="After")

        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "After")
        self.assertEqual(rows[0]["provider_user_id"], str(TRAKT_ID))

    def test_a_different_numeric_id_is_a_different_identity(self):
        user = self.make_user("collector", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"), trakt_id=TRAKT_ID, name="Same Name")
        self.callback(self.start("/auth/trakt/link"), trakt_id=OTHER_TRAKT_ID,
                      name="Same Name")
        self.assertEqual(len(self.identities()), 2)

    def test_an_account_response_without_a_numeric_id_is_refused(self):
        state = self.start()
        with patch.object(trakt_auth, "exchange_code", return_value=_Token()), \
             patch.object(trakt_auth, "fetch_account",
                          side_effect=trakt_auth.AccountLookupError("no numeric id")):
            resp = self.client.get("/auth/trakt/callback",
                                   params={"state": state, "code": "c"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.identities(), [])

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
        with patch("app.trakt_auth.httpx.AsyncClient", return_value=client):
            account = asyncio.run(trakt_auth.fetch_account("cid", "token"))
        self.assertEqual(account, {"id": "30ee8617b5f3f670f90d88012b30adf4",
                                   "name": "JoshTheBlack"})
        # /users/me cannot answer this — it returns ids:{slug} and nothing else.
        self.assertTrue(seen["url"].endswith("/users/settings"))

    def test_fetch_account_ignores_the_unrelated_account_token(self):
        """The settings response carries a Trakt-internal `account.token`. It must
        never end up on the identity row or anywhere near a log."""
        client, _ = self._stub_client(self.SETTINGS_BODY)
        with patch("app.trakt_auth.httpx.AsyncClient", return_value=client):
            account = asyncio.run(trakt_auth.fetch_account("cid", "token"))
        self.assertNotIn("unrelated-internal-value", str(account))

    def test_fetch_account_refuses_a_response_with_no_uuid(self):
        """A slug is reassignable, so a body carrying only one is refused rather
        than keyed on."""
        for body in ({"user": {"ids": {"slug": "josh"}}}, {"user": {}}, {}):
            with self.subTest(body=body):
                client, _ = self._stub_client(body)
                with patch("app.trakt_auth.httpx.AsyncClient", return_value=client):
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

    def test_a_token_that_is_still_valid_is_not_refreshed(self):
        user = self.make_user("watcher", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="trakt", provider_user_id=TRAKT_ID,
            access_token="fine", token_expires_at=db.now() + 3600)))

        def _explode(*a, **kw):  # pragma: no cover — asserting it isn't called
            raise AssertionError("refreshed a token that had not expired")

        with patch.object(trakt_auth, "refresh_access_token", side_effect=_explode):
            self.assertEqual(asyncio.run(trakt_routes.access_token_for_user(user)), "fine")

    def test_a_user_with_no_linked_trakt_account_has_no_token(self):
        user = self.make_user("plain")
        self.assertIsNone(asyncio.run(trakt_routes.access_token_for_user(user)))


class UnlinkTests(TraktOAuthTestCase):
    """Unlinking now also asks Trakt to forget the authorization, so every test
    here patches that call — without it they reach the real api.trakt.tv."""

    def setUp(self):
        super().setUp()
        self.revoked: list[str] = []

        async def _revoke(client_id, client_secret, access_token):
            self.revoked.append(access_token)

        patcher = patch.object(trakt_auth, "revoke_token", side_effect=_revoke)
        self.revoke_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def test_unlinking_revokes_the_token_at_trakt(self):
        user = self.make_user("revoker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        stored = self.identities()[0]["access_token"]
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, [stored])
        self.assertIsNone(resp.json()["warning"])

    def test_a_refused_unlink_does_not_revoke_the_token(self):
        """The account keeps the identity, so the token it holds has to keep
        working — revoking on the way to a refusal would leave the only way in
        pointing at a dead credential."""
        self.sign_out()
        self.callback(self.start(invite=self.mint_invite()))
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.revoked, [])
        self.assertEqual(len(self.identities()), 1)

    def test_a_failed_revocation_still_unlinks_and_says_so(self):
        """Trakt being unreachable is not a reason to refuse someone the removal
        of their own link — but they are told, because finishing the job on
        trakt.tv is something only they can do."""
        user = self.make_user("warned", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        self.revoke_mock.side_effect = httpx.HTTPError("down")
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.identities(), [])
        self.assertIn("trakt.tv", resp.json()["warning"])

    def test_a_user_with_a_password_can_unlink(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start("/auth/trakt/link"))
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.identities(), [])

    def test_the_last_login_method_cannot_be_unlinked(self):
        """An account with no password and no identities cannot be signed in to
        by anybody, including its owner, and there is no self-service way back."""
        self.sign_out()
        self.callback(self.start(invite=self.mint_invite()))
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(self.identities()), 1)

    def test_unlinking_something_that_is_not_linked_is_a_404(self):
        user = self.make_user("plain", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "plex"})
        self.assertEqual(resp.status_code, 404)

    def test_an_unknown_provider_is_rejected(self):
        user = self.make_user("plain", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "netflix"})
        self.assertEqual(resp.status_code, 400)

    def test_unlink_requires_a_session(self):
        self.sign_out()
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 401)


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
        resp = self.client.post("/api/settings",
                                json={"public_base_url": "https://shows.example.com/app"})
        self.assertEqual(resp.status_code, 400)
        # Nothing was written: the stored value is still the valid one.
        self.assertEqual(self.client.get("/api/settings").json()["public_base_url"], ORIGIN)

    def test_a_hand_edited_trailing_slash_is_normalized(self):
        self.assertEqual(
            Settings.from_dict({"public_base_url": "https://shows.example.com/"}).public_base_url,
            "https://shows.example.com")

    def test_the_configured_origin_beats_the_host_header(self):
        """The origin check falls back to the request's Host only while no base
        URL is configured; setting one has to make it authoritative."""
        from app import authz

        save_settings(_settings(public_base_url="https://real.example.com"))
        self.sign_in_as(self.admin_id)
        # The client's own Origin (https://testserver) now disagrees with the
        # configured one, and that is what a mutating request is judged against.
        resp = self.client.post("/api/settings", json={})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["reason"], "cross_origin")

        resp = self.client.post("/api/settings", json={},
                                headers={"Origin": "https://real.example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(hasattr(Settings(), "public_base_url"))
        self.assertIsNotNone(authz.expected_origin)

    def test_the_settings_response_reports_the_redirect_uri_to_register(self):
        self.sign_in_as(self.admin_id)
        data = self.client.get("/api/settings").json()
        self.assertTrue(data["trakt_login_configured"])
        self.assertEqual(data["trakt_redirect_uri"], f"{ORIGIN}/auth/trakt/callback")
        self.assertFalse(data["trakt_reconnect_notice"])


class ReconnectNoticeTests(TraktOAuthTestCase):
    def setUp(self):
        super().setUp()
        # The notice is about a token this instance HAS but could not resolve,
        # so every test here needs one saved.
        save_settings(_settings(trakt_access_token="app-wide-token"))

    def test_the_notice_is_surfaced_and_cleared_by_an_admin_linking(self):
        asyncio.run(db.set_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "1"))
        self.sign_in_as(self.admin_id)
        self.assertTrue(self.client.get("/api/settings").json()["trakt_reconnect_notice"])

        self.callback(self.start("/auth/trakt/link"))
        self.assertFalse(self.client.get("/api/settings").json()["trakt_reconnect_notice"])

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
            resp = self.client.post("/api/auth/trakt/adopt", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(self.client.get("/api/settings").json()["trakt_reconnect_notice"])

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
            resp = self.client.post("/api/auth/trakt/adopt", json={})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn("squatter", resp.json()["error"])
        # And it is still up, because this login is still unlinked.
        self.assertTrue(self.client.get("/api/settings").json()["trakt_reconnect_notice"])

    def test_a_token_trakt_will_not_resolve_reports_that_instead(self):
        asyncio.run(db.set_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "1"))
        self.sign_in_as(self.admin_id)
        with patch.object(trakt_auth, "fetch_account",
                          side_effect=trakt_auth.AccountLookupError("HTTP 401")):
            resp = self.client.post("/api/auth/trakt/adopt", json={})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn("HTTP 401", resp.json()["error"])


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

    def test_the_account_page_offers_connect_then_unlink(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.assertIn('href="/auth/trakt/link"', self.client.get("/me").text)
        self.callback(self.start("/auth/trakt/link"))
        body = self.client.get("/me").text
        self.assertIn('data-unlink="trakt"', body)
        self.assertNotIn('href="/auth/trakt/link"', body)

    def test_an_oauth_only_account_is_not_offered_an_unlink_it_cannot_use(self):
        self.sign_out()
        self.callback(self.start(invite=self.mint_invite()))
        self.assertNotIn('data-unlink="trakt"', self.client.get("/me").text)

    def test_the_registration_page_carries_the_invite_into_the_provider_flow(self):
        self.sign_out()
        token = self.mint_invite()
        body = self.client.get("/register", params={"invite": token}).text
        self.assertIn(f"/auth/trakt/start?invite={token}", body)


async def _fill_attempts(key_type: str, key_value: str, count: int) -> None:
    for _ in range(count):
        await auth.record_attempt(key_type, key_value, False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
