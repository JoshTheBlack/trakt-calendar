"""Log in with Plex — the PIN-based flow.

Plex has no redirect callback: the popup approves the PIN entirely on
plex.tv's own page, and this app only learns about it by polling. That makes
the poll endpoint (POST /auth/plex/poll) carry the same account-takeover
concerns Trakt's callback carries, but re-checked on every call instead of
once — a handshake row is consumed only at the poll that finds the PIN
approved, and every poll before that re-validates the same handshake-cookie
and session binding.

No network: plex_auth.request_pin / poll_pin / fetch_account are patched.
TRAKT_DATA_DIR points at a temp dir (set BEFORE importing app modules) so this
suite never touches a real database file.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_plex_auth -v
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

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-plex-auth-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, auth_routes, db, plex_auth, plex_routes  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"

# The Plex account a patched poll resolves to unless a test says otherwise. An
# int, because the numeric account id is the only acceptable key for an
# identity row.
PLEX_ID = 778899
OTHER_PLEX_ID = 112233
PIN_ID = 555111


def _pin(pin_id=PIN_ID, code="ABCD") -> dict:
    return {"id": pin_id, "code": code}


class PlexAuthTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        PlexAuthTestCase._counter += 1
        db.set_db_path(TMP / f"plex-{PlexAuthTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        # https, because the session and handshake cookies are Secure by
        # default and a client honoring that won't send them back over plain
        # http.
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        # Something has to exist or the first-run gate answers every request
        # before any of this is reached.
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    # -- fixtures ------------------------------------------------------------

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

    def start(self, path="/auth/plex/start", *, pin=None, **params) -> str:
        """Begin a flow and return the `state` the app generated."""
        with patch.object(plex_auth, "request_pin", return_value=pin or _pin()):
            resp = self.client.get(path, params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["ok"], resp.text)
        self.assertTrue(data["state"])
        self.assertIn("popup_url", data)
        return data["state"]

    def pin_handshake_cookie(self, state) -> None:
        """Pretend the browser still holds the cookie for `state`.

        The cookie is the second half of the binding and is checked first, so
        tests that are about the handshake ROW have to satisfy it or they
        would pass for the wrong reason.
        """
        if state:
            self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)

    def poll(self, state, *, auth_token="plex-token-1", account=None, cookie=True, **body):
        """Poll once, with the PIN already approved and the account patched."""
        if cookie:
            self.pin_handshake_cookie(state)
        account = account if account is not None else {"id": PLEX_ID, "name": "Josh"}
        with patch.object(plex_auth, "poll_pin", return_value=auth_token), \
             patch.object(plex_auth, "fetch_account", return_value=account):
            return self.client.post("/auth/plex/poll", json={"state": state, **body})

    def poll_pending(self, state, *, cookie=True):
        """Poll once, with plex.tv reporting the PIN still unapproved."""
        if cookie:
            self.pin_handshake_cookie(state)
        with patch.object(plex_auth, "poll_pin", return_value=None) as mock_poll, \
             patch.object(plex_auth, "fetch_account") as mock_account:
            resp = self.client.post("/auth/plex/poll", json={"state": state})
        return resp, mock_poll, mock_account

    def identities(self):
        return asyncio.run(db.fetch_all("SELECT * FROM linked_identities"))

    def user_count(self) -> int:
        return int(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM users")))

    def handshake_consumed(self, state) -> bool:
        return asyncio.run(db.fetch_value(
            "SELECT consumed_at FROM auth_handshakes WHERE state = ?", (state,))) is not None


class ClientIdentifierTests(PlexAuthTestCase):
    def test_it_is_generated_once_and_persisted(self):
        first = asyncio.run(plex_auth.ensure_client_identifier())
        second = asyncio.run(plex_auth.ensure_client_identifier())
        self.assertEqual(first, second)
        stored = asyncio.run(db.get_meta(plex_auth.CLIENT_IDENTIFIER_META_KEY))
        self.assertEqual(stored, first)

    def test_a_race_to_generate_it_converges_on_one_value(self):
        barrier = Barrier(2)

        def _ensure():
            barrier.wait(timeout=5)
            try:
                return asyncio.run(plex_auth.ensure_client_identifier())
            finally:
                db.close_thread_connection()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result() for f in [pool.submit(_ensure), pool.submit(_ensure)]]
        self.assertEqual(results[0], results[1])


class HandshakeBindingTests(PlexAuthTestCase):
    """Every way of reaching the poll without having started (or owning) the
    flow being polled. This mirrors the Trakt callback suite, but re-run
    against repeated polls rather than a one-shot callback."""

    def test_a_completed_flow_consumes_its_handshake_exactly_once(self):
        state = self.start(invite=self.mint_invite())
        self.assertEqual(self.poll(state).status_code, 200)
        self.assertTrue(self.handshake_consumed(state))

    def test_a_poll_with_no_state_at_all_is_refused(self):
        self.start()  # a real handshake exists; the poll just isn't for it
        resp = self.client.post("/auth/plex/poll", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_an_unknown_state_is_refused(self):
        self.start()
        self.pin_handshake_cookie("state-that-was-never-issued")
        resp = self.client.post("/auth/plex/poll", json={"state": "state-that-was-never-issued"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(auth.HANDSHAKE_REJECTED, resp.text)
        self.assertEqual(self.identities(), [])

    def test_an_expired_state_is_refused(self):
        state = self.start()
        asyncio.run(db.execute(
            "UPDATE auth_handshakes SET expires_at = ? WHERE state = ?", (db.now() - 1, state)))
        resp = self.poll(state)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_an_already_consumed_state_is_refused(self):
        """A duplicate poll delivered after the flow already finished — e.g. a
        stray timer tick that fired before the client noticed. The first use
        is what wrote the identity; the second must change nothing."""
        state = self.start(invite=self.mint_invite())
        self.assertEqual(self.poll(state).status_code, 200)
        self.sign_out()
        resp = self.poll(state)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(self.identities()), 1)

    def test_a_foreign_session_cannot_complete_a_link_handshake(self):
        """THE TAKEOVER CASE. An attacker starts a link flow on their own
        account and hands the poll's `state` to a signed-in victim. The
        handshake is bound to the attacker's session, so the victim's browser
        cannot complete it — and the attacker's Plex account never touches
        the victim's account."""
        attacker = self.make_user("attacker", calendar_approved=True)
        victim = self.make_user("victim", calendar_approved=True)
        self.sign_in_as(attacker)
        state = self.start("/auth/plex/link")

        self.sign_out()
        self.sign_in_as(victim)
        # The victim's browser still carries the handshake cookie in this
        # test, which is the strictly harder case: even holding it, the
        # session bound into the row is what refuses.
        resp = self.poll(state)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_a_link_handshake_cannot_be_completed_signed_out(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        state = self.start("/auth/plex/link")
        self.client.cookies.delete(auth.COOKIE_NAME_SECURE)
        resp = self.poll(state)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])

    def test_revoking_the_session_kills_its_in_flight_link_handshake(self):
        user = self.make_user("linker", calendar_approved=True)
        session_id = self.sign_in_as(user)
        state = self.start("/auth/plex/link")
        asyncio.run(auth.revoke_session(session_id))
        self.assertIsNone(asyncio.run(db.fetch_one(
            "SELECT 1 FROM auth_handshakes WHERE state = ?", (state,))))

    def test_a_poll_for_a_pin_not_bound_to_the_callers_handshake_is_refused(self):
        """The one check unique to this flow: `state` names the caller's OWN
        handshake row, and the row is what carries the plex_pin_id — there is
        no way to ask about someone else's PIN without also holding the
        cookie for the handshake it belongs to. plex.tv is never even asked
        about the foreign PIN."""
        victim_state = self.start(pin=_pin(pin_id=PIN_ID, code="VICT"))
        attacker_state = self.start(pin=_pin(pin_id=PIN_ID + 1, code="ATCK"))
        # The attacker's browser holds ITS OWN handshake cookie (from its own
        # start), not the victim's — exactly what a real cross-browser
        # attempt would look like.
        self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, attacker_state)
        with patch.object(plex_auth, "poll_pin") as mock_poll:
            resp = self.client.post("/auth/plex/poll", json={"state": victim_state})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(auth.HANDSHAKE_REJECTED, resp.text)
        mock_poll.assert_not_called()
        self.assertFalse(self.handshake_consumed(victim_state))

    def test_a_poll_in_another_browser_is_refused(self):
        """The handshake cookie pins the flow to the browser that started it,
        so a `state` value forwarded to somebody else is worthless while its
        row is still unconsumed."""
        state = self.start()
        self.client.cookies.clear()  # a different browser
        with patch.object(plex_auth, "poll_pin") as mock_poll:
            resp = self.client.post("/auth/plex/poll", json={"state": state})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.identities(), [])
        mock_poll.assert_not_called()
        self.assertFalse(self.handshake_consumed(state))

    def test_a_trakt_handshake_cannot_be_completed_at_the_plex_poll(self):
        state = asyncio.run(auth.create_handshake(provider="trakt", purpose="login"))
        self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)
        with patch.object(plex_auth, "poll_pin") as mock_poll:
            resp = self.client.post("/auth/plex/poll", json={"state": state})
        self.assertEqual(resp.status_code, 400)
        mock_poll.assert_not_called()

    def test_plex_tv_is_never_asked_about_a_refused_poll(self):
        self.start()
        self.pin_handshake_cookie("nope")
        with patch.object(plex_auth, "poll_pin") as mock_poll:
            resp = self.client.post("/auth/plex/poll", json={"state": "nope"})
        self.assertEqual(resp.status_code, 400)
        mock_poll.assert_not_called()

    def test_every_refusal_reads_the_same(self):
        """Unknown, expired, consumed, and foreign-session must not be
        tellable apart, or the poll becomes a probe for which guess was
        closest."""
        expired = self.start()
        asyncio.run(db.execute("UPDATE auth_handshakes SET expires_at = ? WHERE state = ?",
                               (db.now() - 1, expired)))
        consumed_state = self.start(invite=self.mint_invite())
        self.assertEqual(self.poll(consumed_state).status_code, 200)

        bodies = set()
        for state in ("never-existed", expired, consumed_state):
            self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)
            resp = self.client.post("/auth/plex/poll", json={"state": state})
            self.assertEqual(resp.status_code, 400)
            bodies.add(resp.text)
        self.assertEqual(len(bodies), 1)

    def test_consuming_a_handshake_is_single_use_under_concurrency(self):
        """Two polls racing on the moment a PIN is approved resolve to exactly
        one success — the same guarantee Trakt's callback relies on, backing
        this flow's repeated polling instead of a one-shot callback."""
        state = asyncio.run(auth.create_handshake(provider="plex", purpose="login"))
        barrier = Barrier(2)

        def _consume():
            barrier.wait(timeout=5)
            try:
                asyncio.run(auth.consume_handshake(state, provider="plex"))
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
            asyncio.run(auth.create_handshake(provider="plex", purpose="link"))


class StartRouteTests(PlexAuthTestCase):
    def test_start_returns_a_popup_url_and_pins_the_handshake_cookie(self):
        with patch.object(plex_auth, "request_pin", return_value=_pin(code="WXYZ")):
            resp = self.client.get("/auth/plex/start")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("WXYZ", data["popup_url"])
        self.assertEqual(self.client.cookies.get(auth.HANDSHAKE_COOKIE_SECURE), data["state"])

    def test_start_is_unavailable_when_the_pin_request_fails(self):
        with patch.object(plex_auth, "request_pin", side_effect=plex_auth.PinError("down")):
            resp = self.client.get("/auth/plex/start")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM auth_handshakes"))), 0)

    def test_link_requires_a_session(self):
        self.sign_out()
        resp = self.client.get("/auth/plex/link", headers={"Accept": "application/json"})
        self.assertEqual(resp.status_code, 401)

    def test_the_invite_travels_in_the_handshake_row_not_the_response(self):
        token = self.mint_invite()
        state = self.start(invite=token)
        row = asyncio.run(db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertEqual(row["invite_token"], token)
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["plex_pin_id"], str(PIN_ID))

    def test_the_pin_id_is_recorded_on_the_handshake_row(self):
        state = self.start(pin=_pin(pin_id=999888))
        row = asyncio.run(db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertEqual(row["plex_pin_id"], "999888")


class StillPendingTests(PlexAuthTestCase):
    def test_polling_before_approval_reports_pending_and_writes_nothing(self):
        state = self.start()
        resp, mock_poll, mock_account = self.poll_pending(state)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "status": "pending"})
        mock_poll.assert_called_once()
        mock_account.assert_not_called()
        self.assertFalse(self.handshake_consumed(state))
        self.assertEqual(self.identities(), [])
        self.assertEqual(self.client.cookies.get(auth.COOKIE_NAME_SECURE), None)

    def test_repeated_pending_polls_do_not_consume_the_handshake(self):
        state = self.start()
        for _ in range(3):
            resp, _, _ = self.poll_pending(state)
            self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.handshake_consumed(state))

    def test_a_pin_expired_at_plex_is_an_upstream_failure_not_a_binding_refusal(self):
        state = self.start()
        self.pin_handshake_cookie(state)
        with patch.object(plex_auth, "poll_pin", side_effect=plex_auth.PinError("gone")):
            resp = self.client.post("/auth/plex/poll", json={"state": state})
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(self.handshake_consumed(state))


class SignInOutcomeTests(PlexAuthTestCase):
    """The register-or-sign-in-or-link matrix, exercised through the poll."""

    def test_a_known_identity_signs_its_owner_in(self):
        user = self.make_user("known", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="plex", provider_user_id=PLEX_ID,
            display_name="Old Name")))
        self.sign_out()

        resp = self.poll(self.start(), account={"id": PLEX_ID, "name": "New Name"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["redirect"], "/")
        me = self.client.get("/me")
        self.assertEqual(me.status_code, 200)
        self.assertIn("known", me.text)
        # One identity still, with its display name refreshed and its token
        # written — the name is display-only, so keeping it current is free.
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "New Name")
        self.assertEqual(rows[0]["access_token"], "plex-token-1")

    def test_an_unknown_identity_with_no_invite_creates_no_account(self):
        """Registration through the provider path is invite-gated exactly as
        registration with a password is — a Plex sign-in proves only that
        somebody controls some plex.tv account."""
        self.sign_out()
        before = self.user_count()
        resp = self.poll(self.start())
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
        for i, token in enumerate((expired, exhausted, revoked, "never-existed")):
            resp = self.poll(self.start(invite=token, pin=_pin(pin_id=PIN_ID + 10 + i)))
            self.assertEqual(resp.status_code, 403, token)
            bodies.add(resp.text)
        # Expired, exhausted, revoked, and unknown are one answer, not four.
        self.assertEqual(len(bodies), 1)
        self.assertEqual(self.user_count(), 1)

    def test_a_valid_invite_registers_and_grants_calendar_never_distrakt(self):
        self.sign_out()
        token = self.mint_invite()
        resp = self.poll(self.start(invite=token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["redirect"], "/")

        user = asyncio.run(db.fetch_one(
            "SELECT u.* FROM users u JOIN linked_identities li ON li.user_id = u.id "
            "WHERE li.provider_user_id = ?", (str(PLEX_ID),)))
        self.assertIsNotNone(user)
        self.assertIsNone(user["username"])
        self.assertIsNone(user["password_hash"])
        self.assertTrue(user["calendar_approved"])
        self.assertFalse(user["distrakt_approved"])
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT used_count FROM invites WHERE token = ?", (token,)))), 1)

    def test_an_invite_without_the_calendar_grant_lands_on_me(self):
        self.sign_out()
        token = self.mint_invite(grants_calendar_on_accept=False)
        resp = self.poll(self.start(invite=token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["redirect"], "/me")

    def test_a_disabled_account_is_refused_like_a_wrong_password(self):
        user = self.make_user("disabled_user", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="plex", provider_user_id=PLEX_ID)))
        asyncio.run(db.execute("UPDATE users SET is_disabled = 1 WHERE id = ?", (user,)))
        self.sign_out()

        resp = self.poll(self.start())
        self.assertEqual(resp.status_code, 403)
        self.assertIn(auth_routes.INVALID_CREDENTIALS, resp.text)

    def test_open_registration_needs_no_invite_but_grants_nothing(self):
        self.sign_out()
        save_settings(Settings(allow_open_registration=True))
        resp = self.poll(self.start())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["redirect"], "/me")
        user = asyncio.run(db.fetch_one(
            "SELECT u.* FROM users u JOIN linked_identities li ON li.user_id = u.id "
            "WHERE li.provider_user_id = ?", (str(PLEX_ID),)))
        self.assertFalse(user["calendar_approved"])

    def test_registration_through_this_path_is_rate_limited(self):
        self.sign_out()
        save_settings(Settings(allow_open_registration=True))
        for i in range(auth.REGISTER_MAX_ATTEMPTS):
            resp = self.poll(self.start(pin=_pin(pin_id=PIN_ID + i)),
                             account={"id": PLEX_ID + 1 + i, "name": f"user{i}"})
            self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.poll(self.start(pin=_pin(pin_id=PIN_ID + 999)),
                         account={"id": PLEX_ID + 999, "name": "one-too-many"})
        self.assertEqual(resp.status_code, 429)


class LinkOutcomeTests(PlexAuthTestCase):
    def test_linking_attaches_the_identity(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.poll(self.start("/auth/plex/link"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["redirect"], "/me")
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], user)
        self.assertEqual(rows[0]["provider_user_id"], str(PLEX_ID))

    def test_an_identity_linked_to_another_account_is_refused_and_not_moved(self):
        owner = self.make_user("owner", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=owner, provider="plex", provider_user_id=PLEX_ID)))

        other = self.make_user("other", calendar_approved=True)
        self.sign_in_as(other)
        resp = self.poll(self.start("/auth/plex/link"))
        self.assertEqual(resp.status_code, 409)

        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], owner)

    def test_relinking_refreshes_the_token(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.assertEqual(self.poll(self.start("/auth/plex/link"),
                                   auth_token="first-token").status_code, 200)
        self.assertEqual(self.poll(self.start("/auth/plex/link"),
                                   auth_token="second-token").status_code, 200)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["access_token"], "second-token")


class IdentityKeyingTests(PlexAuthTestCase):
    def test_a_display_name_change_resolves_to_the_same_row(self):
        self.sign_out()
        token = self.mint_invite()
        self.assertEqual(
            self.poll(self.start(invite=token), account={"id": PLEX_ID, "name": "First"}).status_code,
            200)
        self.sign_out()
        self.assertEqual(
            self.poll(self.start(), account={"id": PLEX_ID, "name": "Renamed"}).status_code, 200)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "Renamed")

    def test_a_different_numeric_id_with_the_same_name_is_a_second_identity(self):
        self.sign_out()
        self.assertEqual(self.poll(self.start(invite=self.mint_invite()),
                                   account={"id": PLEX_ID, "name": "Same Name"}).status_code, 200)
        self.sign_out()
        self.assertEqual(self.poll(self.start(invite=self.mint_invite(), pin=_pin(pin_id=PIN_ID + 1)),
                                   account={"id": OTHER_PLEX_ID, "name": "Same Name"}).status_code,
                         200)
        self.assertEqual(len(self.identities()), 2)

    def test_an_account_lookup_failure_aborts_the_poll_with_no_row_written(self):
        state = self.start()
        self.pin_handshake_cookie(state)
        with patch.object(plex_auth, "poll_pin", return_value="a-token"), \
             patch.object(plex_auth, "fetch_account",
                          side_effect=plex_auth.AccountLookupError("no numeric id")):
            resp = self.client.post("/auth/plex/poll", json={"state": state})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.identities(), [])
        # The handshake was never actually spent — the account lookup failed
        # before consume_handshake was ever called.
        self.assertFalse(self.handshake_consumed(state))


class ProviderButtonTests(PlexAuthTestCase):
    """§7.6 has its own gate for distrakt; this is just making sure chat C's
    disabled placeholder is actually gone."""

    def test_the_login_page_offers_a_working_plex_button(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('class="auth-provider-btn ready" id="plexBtn"', resp.text)

    def test_the_register_page_offers_a_working_plex_button(self):
        token = self.mint_invite()
        resp = self.client.get(f"/register?invite={token}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="plexBtn"', resp.text)

    def test_the_account_page_offers_a_connect_button_when_unlinked(self):
        user = self.make_user("connectme", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.client.get("/me")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="plexConnect"', resp.text)

    def test_the_account_page_offers_unlink_once_linked(self):
        user = self.make_user("unlinkme", calendar_approved=True)
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user, provider="plex", provider_user_id=PLEX_ID, display_name="Me")))
        self.sign_in_as(user)
        resp = self.client.get("/me")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('data-unlink="plex"', resp.text)


if __name__ == "__main__":
    unittest.main()
