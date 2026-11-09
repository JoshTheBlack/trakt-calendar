"""Connect TMDB — the link on the account page, its callback, the stored grant,
the unlink that revokes it, and that a TMDB link is never a way in.

TMDB's side is the transport's private sender, replaced by a fake that answers
each step the way the live service was measured to (app/auth/tmdb.py's
docstring): a request token, an exchange, the account read, and revocation.

THE CALLBACK IS THE SENSITIVE PART, for the reason every provider callback is
(app/auth/handshakes.py): it is a top-level GET a signed-in victim's browser can
be sent to. A callback that could hand over SOMEBODY ELSE'S approved request
token would link their TMDB account to the victim's — and the victim's tracker
would then write to the attacker's watchlist. So nothing about the grant comes
from the address the browser arrives at but the state.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from app import auth, db, providers, secrets_box
from app.auth import revocation, tmdb_routes, viewer
from app.config import Settings
from app.providers import tmdb as tmdb_provider
from app.providers.base import Source
from app.providers.tmdb import transport
from tests.support import ORIGIN, AppTestCase, load_settings, save_settings

READ_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.instance-read-token"
REQUEST_TOKEN = "eyJ.request-token-approved"
USER_TOKEN = "eyJ.the-viewers-own-user-token"
ACCOUNT = "6a5edaa7df66e2fc7c922663"


class FakeTmdb:
    """TMDB's account API at the transport's private sender: every request
    recorded (method, path, the bearer it carried, its body), each step
    answered as measured."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.approved = True
        self.account_ok = True
        self.revoke_status = 200
        self.revoked: set[str] = set()
        self.account_id = ACCOUNT

    async def send_private(self, method, url, path, *, token, body=None):
        self.calls.append((method, urlsplit(url).path, token, body))
        if path == "auth/request_token":
            return httpx.Response(200, json={"success": True, "status_code": 1,
                                             "request_token": REQUEST_TOKEN})
        if path == "auth/access_token" and method == "POST":
            if not self.approved or body.get("request_token") != REQUEST_TOKEN:
                return httpx.Response(401, json={"success": False, "status_code": 36})
            return httpx.Response(200, json={"success": True, "status_code": 1,
                                             "account_id": self.account_id,
                                             "access_token": USER_TOKEN})
        if path == "auth/access_token" and method == "DELETE":
            self.revoked.add(body["access_token"])
            return httpx.Response(self.revoke_status, json={"success": True, "status_code": 13})
        if path == "account":
            if not self.account_ok:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"id": 23466253, "username": "JoshTheBlack"})
        raise AssertionError(f"unexpected TMDB call {method} {path}")

    def bearers(self, path_end: str) -> list[str]:
        return [token for _method, path, token, _body in self.calls if path.endswith(path_end)]


class TmdbLinkTestCase(AppTestCase):

    def make_settings(self):
        return Settings(simkl_public_calendar_enabled=False, tmdb_api_key=READ_TOKEN,
                        public_base_url=ORIGIN)

    def setUp(self):
        super().setUp()
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)
        self.user_id = self.make_user("viewer", calendar_approved=True)
        self.session_id = self.sign_in_as(self.user_id)
        self.tmdb = FakeTmdb()
        sender = patch.object(transport, "send_private", self.tmdb.send_private)
        sender.start()
        self.addCleanup(sender.stop)

    def start(self) -> tuple[str, str]:
        """Press Connect: (the state the app made, the address TMDB was sent)."""
        resp = self.client.get("/auth/tmdb/link", follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        approve = resp.headers["location"]
        method, path, token, body = self.tmdb.calls[0]
        self.assertEqual((method, path, token), ("POST", "/4/auth/request_token", READ_TOKEN))
        state = parse_qs(urlsplit(body["redirect_to"]).query)["state"][0]
        return state, approve

    def callback(self, state: str, **extra):
        return self.client.get("/auth/tmdb/callback", params={"state": state, **extra},
                               follow_redirects=False)

    def stored(self):
        return asyncio.run(db.fetch_one(
            "SELECT * FROM linked_identities WHERE user_id = ? AND provider = 'tmdb'",
            (self.user_id,)))


class TheLinkTests(TmdbLinkTestCase):

    def test_connect_sends_the_browser_to_tmdb_to_approve(self):
        state, approve = self.start()
        self.assertEqual(approve, "https://www.themoviedb.org/auth/access?request_token="
                                  + REQUEST_TOKEN.replace(".", "."))
        self.assertTrue(state)

    def test_redirect_to_is_this_instances_own_address_and_nothing_from_the_request(self):
        """No open redirect: the address TMDB sends the approval back to is the
        configured public base URL's callback, whatever Host the request named."""
        self.client.get("/auth/tmdb/link", follow_redirects=False,
                        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
        redirect_to = self.tmdb.calls[0][3]["redirect_to"]
        parts = urlsplit(redirect_to)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}",
                         f"{ORIGIN}/auth/tmdb/callback")
        self.assertEqual(set(parse_qs(parts.query)), {"state"})

    def test_the_request_token_is_kept_sealed_on_the_handshake_not_in_a_cookie(self):
        state, _ = self.start()
        row = asyncio.run(db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertEqual((row["provider"], row["purpose"], row["session_id"]),
                         ("tmdb", "link", self.session_id))
        self.assertEqual(secrets_box.open_(row["pkce_verifier"]), REQUEST_TOKEN)
        self.assertNotIn(REQUEST_TOKEN, " ".join(self.client.cookies.values()))

    def test_an_approved_link_stores_the_grant_and_lands_on_the_account_page(self):
        state, _ = self.start()
        resp = self.callback(state)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/me"))
        row = self.stored()
        self.assertEqual((row["provider_user_id"], row["display_name"]),
                         (ACCOUNT, "JoshTheBlack"))
        self.assertEqual(secrets_box.open_(row["access_token"]), USER_TOKEN)
        self.assertIsNone(row["refresh_token"])
        # The exchange spent the row's token with the instance's credential; the
        # account read carried the person's own.
        self.assertEqual(self.tmdb.bearers("/4/auth/access_token"), [READ_TOKEN])
        self.assertEqual(self.tmdb.bearers("/3/account"), [USER_TOKEN])

    def test_the_grant_is_sealed_at_rest_when_a_key_is_set(self):
        from tests.encryption.test_sealing import KEY, _KeyEnv
        with _KeyEnv(KEY):
            state, _ = self.start()
            self.assertEqual(self.callback(state).status_code, 303)
            stored = self.stored()["access_token"]
            self.assertTrue(stored.startswith(secrets_box.PREFIX))
            self.assertNotIn(USER_TOKEN, stored)
            self.assertEqual(secrets_box.open_(stored), USER_TOKEN)

    def test_the_callback_trades_the_rows_token_never_one_from_the_address(self):
        """A request token in the address is ignored: the exchange is of the one
        kept on the handshake, so a callback cannot link an account somebody
        else approved."""
        state, _ = self.start()
        self.callback(state, request_token="eyJ.an-attackers-approved-token")
        exchanged = [body["request_token"] for method, path, _t, body in self.tmdb.calls
                     if method == "POST" and path == "/4/auth/access_token"]
        self.assertEqual(exchanged, [REQUEST_TOKEN])

    def test_a_callback_in_another_browser_is_refused_before_tmdb_is_asked(self):
        state, _ = self.start()
        self.client.cookies.delete(auth.HANDSHAKE_COOKIE_SECURE)
        calls = len(self.tmdb.calls)
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 400)
        self.assertIn(auth.HANDSHAKE_REJECTED, resp.text)
        self.assertEqual(len(self.tmdb.calls), calls)
        self.assertIsNone(self.stored())

    def test_a_callback_under_another_session_is_refused(self):
        state, _ = self.start()
        other = self.make_user("someone_else", calendar_approved=True)
        self.sign_in_as(other)
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self.stored())

    def test_a_callback_is_used_once(self):
        state, _ = self.start()
        self.assertEqual(self.callback(state).status_code, 303)
        self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, state)
        self.assertEqual(self.callback(state).status_code, 400)

    def test_an_unknown_state_is_refused(self):
        self.start()
        self.client.cookies.set(auth.HANDSHAKE_COOKIE_SECURE, "made-up")
        self.assertEqual(self.callback("made-up").status_code, 400)

    def test_a_declined_approval_links_nothing(self):
        self.tmdb.approved = False
        state, _ = self.start()
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("approve the connection, so nothing has changed", resp.text)
        self.assertIsNone(self.stored())

    def test_a_grant_that_cannot_be_kept_is_revoked_at_tmdb(self):
        """The account read failing leaves nothing linked — and the grant TMDB
        has just minted is ended there rather than left standing unused."""
        self.tmdb.account_ok = False
        state, _ = self.start()
        self.assertEqual(self.callback(state).status_code, 502)
        self.assertIsNone(self.stored())
        self.assertEqual(self.tmdb.revoked, {USER_TOKEN})

    def test_an_account_linked_to_somebody_else_is_refused_and_the_grant_revoked(self):
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.admin_id, provider="tmdb", provider_user_id=ACCOUNT,
            access_token="theirs")))
        state, _ = self.start()
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 409)
        self.assertIn(tmdb_routes.ALREADY_LINKED, resp.text)
        self.assertIsNone(self.stored())
        self.assertEqual(self.tmdb.revoked, {USER_TOKEN})

    def test_connect_needs_a_signed_in_session(self):
        self.client.cookies.clear()
        resp = self.client.get("/auth/tmdb/link", follow_redirects=False)
        self.assertNotEqual(resp.status_code, 303)
        self.assertEqual(self.tmdb.calls, [])

    def test_connect_is_not_offered_without_a_public_base_url(self):
        save_settings(Settings(simkl_public_calendar_enabled=False, tmdb_api_key=READ_TOKEN))
        resp = self.client.get("/auth/tmdb/link", follow_redirects=False)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(self.tmdb.calls, [])
        page = self.client.get("/me").text
        self.assertNotIn('href="/auth/tmdb/link"', page)

    def test_the_account_page_offers_connect(self):
        self.assertIn('href="/auth/tmdb/link"', self.client.get("/me").text)

    def test_no_token_reaches_a_log_line(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("app").debug("start")
            self.tmdb.account_ok = False
            state, _ = self.start()
            self.callback(state)
        text = "\n".join(logs.output)
        for secret in (USER_TOKEN, REQUEST_TOKEN, READ_TOKEN):
            self.assertNotIn(secret, text)


class TheAccountIdTests(TmdbLinkTestCase):

    def test_an_account_id_of_an_unknown_shape_links_nothing(self):
        """The id becomes the identity's key and part of every account address,
        so one that is not an object id is refused at the exchange."""
        self.tmdb.account_id = "../../3/movie/550"
        state, _ = self.start()
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self.stored())
        self.assertEqual(self.tmdb.bearers("/3/account"), [])


class OneLinkPerAccountTests(TmdbLinkTestCase):
    """A TMDB grant has no clock, so a second Connect has nothing to refresh: it
    is refused, at the start and again at the callback, rather than overwriting
    a grant nothing would then revoke or adding a second TMDB row."""

    def link(self):
        state, _ = self.start()
        self.assertEqual(self.callback(state).status_code, 303)
        self.tmdb.calls.clear()

    def test_connect_while_linked_is_refused_and_asks_tmdb_nothing(self):
        self.link()
        resp = self.client.get("/auth/tmdb/link", follow_redirects=False)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already connected", resp.text)
        self.assertEqual(self.tmdb.calls, [])

    def test_a_link_made_while_this_one_was_at_tmdb_is_refused_before_the_exchange(self):
        state, _ = self.start()
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="tmdb",
            provider_user_id="0123456789abcdef01234567", display_name="Other",
            access_token="another-grant")))
        self.tmdb.calls.clear()
        resp = self.callback(state)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.tmdb.calls, [])
        rows = asyncio.run(db.fetch_all(
            "SELECT * FROM linked_identities WHERE user_id = ? AND provider = 'tmdb'",
            (self.user_id,)))
        self.assertEqual(len(rows), 1)

    def test_a_second_account_past_both_checks_is_refused_at_the_write_and_revoked(self):
        """The rule every service's link is held to stands behind TMDB's own:
        a second TMDB account that raced past the route's checks is refused
        where the row would be written, and its fresh grant ended at TMDB."""
        state, _ = self.start()
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="tmdb",
            provider_user_id="0123456789abcdef01234567", display_name="Other",
            access_token="another-grant")))
        with patch.object(tmdb_routes, "_holds_a_link", return_value=False):
            resp = self.callback(state)
        self.assertEqual(resp.status_code, 409)
        self.assertIn(tmdb_routes.ALREADY_CONNECTED, resp.text)
        self.assertIn(USER_TOKEN, self.tmdb.revoked)
        rows = asyncio.run(db.fetch_all(
            "SELECT provider_user_id FROM linked_identities WHERE user_id = ? "
            "AND provider = 'tmdb'", (self.user_id,)))
        self.assertEqual([row["provider_user_id"] for row in rows],
                         ["0123456789abcdef01234567"])

    def test_after_an_unlink_connect_works_again(self):
        self.link()
        self.client.post("/me/identities/unlink", json={"provider": "tmdb"})
        self.assertIsNone(self.stored())
        self.tmdb.calls.clear()
        self.start()


class TheUnlinkTests(TmdbLinkTestCase):

    def link(self):
        state, _ = self.start()
        self.assertEqual(self.callback(state).status_code, 303)

    def test_unlinking_revokes_the_grant_at_tmdb(self):
        self.link()
        resp = self.client.post("/me/identities/unlink", json={"provider": "tmdb"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.stored())
        deletes = [(token, body) for method, path, token, body in self.tmdb.calls
                   if method == "DELETE"]
        self.assertEqual(deletes, [(READ_TOKEN, {"access_token": USER_TOKEN})])

    def test_a_revoke_tmdb_cannot_hear_still_unlinks_and_says_so(self):
        self.link()
        self.tmdb.revoke_status = 503
        resp = self.client.post("/me/identities/unlink", json={"provider": "tmdb"})
        self.assertIsNone(self.stored())
        self.assertIn("could not tell TMDB", resp.text)

    def test_a_grant_tmdb_already_dropped_is_success(self):
        """404 / status_code 34: TMDB holds no such token — already ended."""
        self.link()
        self.tmdb.revoke_status = 404

        async def gone(method, url, path, *, token, body=None):
            return httpx.Response(404, json={"success": False, "status_code": 34})

        with patch.object(transport, "send_private", gone):
            resp = self.client.post("/me/identities/unlink", json={"provider": "tmdb"})
        self.assertNotIn("could not tell TMDB", resp.text)
        self.assertIsNone(self.stored())

    def test_a_tmdb_link_is_never_an_accounts_way_in(self):
        """An account with no password, signed in by Trakt and holding a TMDB
        link: the Trakt link is its only way in and may not go, however many
        TMDB links sit beside it — and the TMDB one may."""
        user = self.make_user("passwordless", calendar_approved=True)
        asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user,)))
        self.link_identity(user, "trakt", 42, access_token="trakt-token")
        self.link_identity(user, "tmdb", ACCOUNT, access_token=USER_TOKEN)
        with self.assertRaises(auth.LastLoginMethod):
            asyncio.run(auth.unlink_identity(user, "trakt"))
        self.assertTrue(asyncio.run(auth.unlink_identity(user, "tmdb")))

    def test_the_account_page_offers_the_tmdb_unlink_but_not_the_last_way_in(self):
        user = self.make_user("passwordless2", calendar_approved=True)
        asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user,)))
        self.link_identity(user, "trakt", 43, access_token="trakt-token")
        self.link_identity(user, "tmdb", ACCOUNT, access_token=USER_TOKEN)
        self.sign_in_as(user)
        page = self.client.get("/me").text
        self.assertIn("unlink-tmdb", page)
        self.assertNotIn("unlink-trakt", page)


class NeverASignInTests(TmdbLinkTestCase):

    def test_there_is_no_tmdb_sign_in_route(self):
        self.client.cookies.clear()
        for path in ("/auth/tmdb/start", "/auth/tmdb/login"):
            self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 404)

    def test_a_tmdb_identity_cannot_sign_anybody_in(self):
        identity = auth.ProviderIdentity(provider="tmdb", provider_user_id=ACCOUNT,
                                         access_token=USER_TOKEN)
        with self.assertRaises(ValueError):
            asyncio.run(auth.login_with_provider_identity(identity=identity))

    def test_a_tmdb_link_is_not_a_tracker_identity_and_opens_no_gate(self):
        """The tracker's access gate admits a linked service that keeps watch
        history (providers.tracker_sources); TMDB keeps none."""
        self.link_identity(self.user_id, "tmdb", ACCOUNT, access_token=USER_TOKEN)
        user = asyncio.run(auth.validate_session(self.session_id))
        self.assertIn("tmdb", user.linked_providers)
        self.assertFalse(user.has_tracker_identity)
        self.assertNotIn("tmdb", providers.tracker_sources())


class TheViewersGrantTests(TmdbLinkTestCase):
    """auth.viewer.settings_for_user carries the grant, and nothing else does."""

    def test_the_viewers_settings_carry_the_grant(self):
        self.link_identity(self.user_id, "tmdb", ACCOUNT, access_token=USER_TOKEN)
        settings = asyncio.run(viewer.settings_for_user(self.user_id))
        self.assertEqual((settings.tmdb_user_token, settings.tmdb_account_id),
                         (USER_TOKEN, ACCOUNT))
        self.assertTrue(tmdb_provider._TmdbProvider().is_configured(settings))

    def test_a_viewer_who_never_linked_carries_none(self):
        settings = asyncio.run(viewer.settings_for_user(self.user_id))
        self.assertEqual((settings.tmdb_user_token, settings.tmdb_account_id), ("", ""))
        self.assertNotIn(Source.TMDB, dict(providers.for_watchlist(settings)))

    def test_a_viewers_grant_is_never_saved_as_an_instance_setting(self):
        """A save of a per-viewer Settings object writes no TMDB grant anywhere,
        and a load never carries one — or every viewer would write to one
        person's TMDB list."""
        self.link_identity(self.user_id, "tmdb", ACCOUNT, access_token=USER_TOKEN)
        settings = asyncio.run(viewer.settings_for_user(self.user_id))
        save_settings(settings)
        rows = asyncio.run(db.fetch_all("SELECT name FROM app_secrets"))
        self.assertNotIn("tmdb_user_token", {row["name"] for row in rows})
        stored = asyncio.run(db.fetch_value("SELECT value FROM app_settings WHERE name = 'app'"))
        self.assertNotIn(ACCOUNT, stored)
        self.assertNotIn("tmdb_account_id", stored)
        loaded = load_settings()
        self.assertEqual((loaded.tmdb_user_token, loaded.tmdb_account_id), ("", ""))

    def test_a_stray_stored_row_is_never_loaded(self):
        asyncio.run(db.execute("INSERT INTO app_secrets (name, value) VALUES (?, ?)",
                               ("tmdb_user_token", secrets_box.seal(USER_TOKEN))))
        loaded = load_settings()
        self.assertEqual(loaded.tmdb_user_token, "")
        self.assertFalse(loaded.tmdb_account_configured)

    def test_the_revocation_table_holds_tmdb(self):
        self.assertIs(revocation.revoker("tmdb"), tmdb_routes)
