"""The sign-in policy every REDIRECT provider owes, written once and run per
provider.

Trakt and Simkl both sign in through an OAuth redirect — start, the service's
approval page, a callback carrying `state` and `code` — and both hand the result
to app/auth/provider_login.py, which states the policy once: an invite gates
registration, a known identity signs its owner in, an identity is keyed on the
service's immutable id and never moved between accounts, a handshake is bound to
the browser that began it and used once. These tests had been written twice,
once per provider, and had begun to drift apart in what they bothered to assert.

WRITTEN ONCE, RUN THROUGH EACH PROVIDER'S OWN ROUTES. A mixin rather than one
test against provider_login directly, because what can break is each provider's
WIRING into the shared completion — its callback reading `state`, its exchange
being patched where the route calls it, its start route refusing an
unconfigured instance — and only driving each provider's real routes reaches
that. Each provider's test file combines these with its own base case, which
supplies the handful of values below; what is genuinely different about a
provider (Simkl's PKCE verifier and scope check, Trakt's device flow, Plex's
polled PIN) stays in that provider's own file.

What a provider's base case must supply:
  PROVIDER                 "trakt" / "simkl" — the route prefix and identity column
  ACCOUNT_ID / OTHER_ACCOUNT_ID   the immutable ids its patched lookup answers with
  ACCESS_PREFIX / REFRESH_PREFIX  how its real tokens are spelled ("" for Trakt)
  CONFIG_FIELDS            the settings a start route needs before it may begin
  SERVICE_HOST             where a person finishes a revocation themselves
  auth_module / routes_module     app.auth.<provider> and app.auth.<provider>_routes
  Token                    the provider's token-payload class (access=, refresh=)
  settings_with(**kw)      a fully configured Settings with `kw` applied
  revoke_failure()         the exception its transport raises when unreachable
and the helpers every provider base case already has: start, callback (with
`account_id=`), pin_handshake_cookie, mint_invite, sign_out, identities,
user_count — and, for the unlink half, unlink and said.

The module name carries the underscore rather than the names inside it, the
standard library's convention (`_collections_abc`): nothing outside this package
imports it.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import auth, db
from app.auth import routes as auth_routes
from app.config import save_settings
from tests.support import ORIGIN


async def fill_attempts(key_type: str, key_value: str, count: int) -> None:
    """Spend a rate limiter's whole budget, as `count` failed attempts."""
    for _ in range(count):
        await auth.record_attempt(key_type, key_value, False)


class _RedirectProvider:
    """The few readings both halves below take of the provider's values."""

    def path(self, leaf: str) -> str:
        return f"/auth/{self.PROVIDER}/{leaf}"

    def access(self, raw: str) -> str:
        return self.ACCESS_PREFIX + raw

    def insert_identity(self, user_id: int, **extra) -> None:
        asyncio.run(db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider=self.PROVIDER,
            provider_user_id=self.ACCOUNT_ID, **extra)))

    def user_by_identity(self):
        return asyncio.run(db.fetch_one(
            "SELECT u.* FROM users u JOIN linked_identities li ON li.user_id = u.id "
            "WHERE li.provider_user_id = ?", (str(self.ACCOUNT_ID),)))


class SharedRedirectSignInTests(_RedirectProvider):
    """The policy half: starting, the handshake, and what a callback does."""

    # -- starting ------------------------------------------------------------

    def test_start_is_unavailable_until_the_instance_is_configured(self):
        for missing in self.CONFIG_FIELDS:
            # Cleared explicitly: emptying a field on a Settings object does not
            # remove the stored credential on its own — see config.save_settings.
            save_settings(self.settings_with(**{missing: ""}), clear_unset_secrets=True)
            resp = self.client.get(self.path("start"), follow_redirects=False)
            self.assertEqual(resp.status_code, 503, missing)
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM auth_handshakes"))), 0)

    def test_the_redirect_uri_ignores_a_spoofed_host_header(self):
        """Built from the configured origin and nothing else: the service compares
        it byte for byte, and a Host header is whatever the caller says."""
        resp = self.client.get(self.path("start"), follow_redirects=False,
                               headers={"Host": "evil.example.com"})
        query = parse_qs(urlsplit(resp.headers["location"]).query)
        self.assertEqual(query["redirect_uri"], [f"{ORIGIN}{self.path('callback')}"])

    def test_the_invite_travels_in_the_handshake_row_not_the_redirect(self):
        token = self.mint_invite()
        state = self.start(invite=token)
        row = asyncio.run(db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,)))
        self.assertEqual(row["invite_token"], token)
        self.assertEqual(row["provider"], self.PROVIDER)
        self.assertIsNone(row["session_id"])

    def test_link_requires_a_session(self):
        self.sign_out()
        resp = self.client.get(self.path("link"), follow_redirects=False,
                               headers={"Accept": "application/json"})
        self.assertEqual(resp.status_code, 401)

    def test_the_registration_page_carries_the_invite_into_the_provider_flow(self):
        self.sign_out()
        token = self.mint_invite()
        body = self.client.get("/register", params={"invite": token}).text
        self.assertIn(f"{self.path('start')}?invite={token}", body)

    # -- the handshake -------------------------------------------------------

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

    def test_a_callback_with_no_state_at_all_is_refused(self):
        """There is deliberately no "no state, so assume a sign-in" path — that
        would restore the exact hole the handshake table exists to close."""
        self.start()
        with patch.object(self.auth_module, "exchange_code", return_value=self.Token()):
            resp = self.client.get(self.path("callback"), params={"code": "auth-code"},
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

    # -- signing in ----------------------------------------------------------

    def test_a_known_identity_signs_its_owner_in(self):
        user = self.make_user("known", calendar_approved=True)
        self.insert_identity(user, display_name="Old Name")
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
        self.assertEqual(rows[0]["access_token"], self.access("access-1"))

    def test_an_unknown_identity_with_no_invite_creates_no_account(self):
        """Registration through the provider path is invite-gated exactly as
        registration with a password is — a sign-in proves only that somebody
        controls some account at the service."""
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

        user = self.user_by_identity()
        self.assertIsNotNone(user)
        self.assertIsNone(user["username"])
        self.assertIsNone(user["password_hash"])
        self.assertTrue(user["calendar_approved"])
        # An invite never grants the private-history feature: that one is always
        # a separate manual grant.
        self.assertFalse(user["distrakt_approved"])
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT used_count FROM invites WHERE token = ?", (token,)))), 1)
        self.assertEqual(int(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM invite_redemptions"))), 1)

    def test_open_registration_needs_no_invite_but_grants_nothing(self):
        save_settings(self.settings_with(allow_open_registration=True))
        self.sign_out()
        resp = self.callback(self.start())
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")
        self.assertFalse(self.user_by_identity()["calendar_approved"])

    def test_registration_through_the_provider_path_is_rate_limited(self):
        self.sign_out()
        asyncio.run(fill_attempts("register_ip", "testclient", auth.REGISTER_MAX_ATTEMPTS))
        resp = self.callback(self.start(invite=self.mint_invite()))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self.user_count(), 1)
        self.assertEqual(self.identities(), [])

    # -- linking -------------------------------------------------------------

    def test_linking_attaches_the_identity_to_the_signed_in_account(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        resp = self.callback(self.start(self.path("link")))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/me")
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["user_id"]), user)
        self.assertEqual(rows[0]["provider"], self.PROVIDER)
        self.assertEqual(rows[0]["provider_user_id"], str(self.ACCOUNT_ID))

    def test_relinking_the_same_account_refreshes_the_stored_token(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start(self.path("link")))
        self.callback(self.start(self.path("link")),
                      token=self.Token(access=self.access("access-2"),
                                       refresh=self.REFRESH_PREFIX + "refresh-2"))
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["access_token"], self.access("access-2"))
        self.assertEqual(rows[0]["refresh_token"], self.REFRESH_PREFIX + "refresh-2")

    def test_an_identity_linked_elsewhere_is_refused_never_moved(self):
        """Whoever authorizes last must not be able to take an identity away
        from the account already holding it."""
        owner = self.make_user("owner", calendar_approved=True)
        self.insert_identity(owner)
        interloper = self.make_user("interloper", calendar_approved=True)
        self.sign_in_as(interloper)

        resp = self.callback(self.start(self.path("link")))
        self.assertEqual(resp.status_code, 409)
        self.assertIn(self.routes_module.ALREADY_LINKED, resp.text)
        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["user_id"]), owner)

    def test_the_account_page_offers_connect_then_unlink(self):
        user = self.make_user("linker", calendar_approved=True)
        self.sign_in_as(user)
        self.assertIn(f'href="{self.path("link")}"', self.client.get("/me").text)
        self.callback(self.start(self.path("link")))
        body = self.client.get("/me").text
        self.assertIn(f'id="confirm-unlink-{self.PROVIDER}"', body)
        self.assertNotIn(f'href="{self.path("link")}"', body)

    # -- which id an identity is ---------------------------------------------

    def test_the_identity_is_keyed_on_the_numeric_id_not_the_display_name(self):
        """A username can be changed by its owner and re-registered by somebody
        else, so keying on one would let a released name inherit the linked
        account. Renaming must resolve to the SAME identity row."""
        user = self.make_user("renamer", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start(self.path("link")), name="Before")
        self.callback(self.start(self.path("link")), name="After")

        rows = self.identities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "After")
        self.assertEqual(rows[0]["provider_user_id"], str(self.ACCOUNT_ID))

    def test_a_different_numeric_id_is_a_different_identity(self):
        user = self.make_user("collector", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start(self.path("link")), account_id=self.ACCOUNT_ID,
                      name="Same Name")
        self.callback(self.start(self.path("link")), account_id=self.OTHER_ACCOUNT_ID,
                      name="Same Name")
        self.assertEqual(len(self.identities()), 2)

    def test_an_account_response_without_a_numeric_id_is_refused(self):
        state = self.start()
        with patch.object(self.auth_module, "exchange_code", return_value=self.Token()), \
             patch.object(self.auth_module, "fetch_account",
                          side_effect=self.auth_module.AccountLookupError("no account id")):
            resp = self.client.get(self.path("callback"),
                                   params={"state": state, "code": "c"},
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.identities(), [])

    # -- a stored token ------------------------------------------------------

    def test_a_token_that_is_still_valid_is_not_refreshed(self):
        user = self.make_user("watcher", calendar_approved=True)
        self.insert_identity(user, access_token=self.access("fine"),
                             token_expires_at=db.now() + 3600)

        def _explode(*a, **kw):  # pragma: no cover — asserting it isn't called
            raise AssertionError("refreshed a token that had not expired")

        with patch.object(self.auth_module, "refresh_access_token", side_effect=_explode):
            self.assertEqual(asyncio.run(self.routes_module.access_token_for_user(user)),
                             self.access("fine"))


class SharedRedirectUnlinkTests(_RedirectProvider):
    """The unlink half, for a provider's UnlinkTests — whose setUp patches its
    revoke call into `self.revoke_mock` and whose `unlink`/`said` post the
    unlink and read back the sentence under the linked services."""

    def test_the_last_login_method_cannot_be_unlinked(self):
        """An account with no password and no identities cannot be signed in to
        by anybody, including its owner, and there is no self-service way back."""
        self.sign_out()
        self.callback(self.start(invite=self.mint_invite()))
        self.assertIn("only way you can sign in", self.said(self.unlink()))
        self.assertEqual(len(self.identities()), 1)

    def test_unlinking_something_that_is_not_linked_is_refused(self):
        user = self.make_user("plain", calendar_approved=True)
        self.sign_in_as(user)
        self.assertEqual(self.said(self.unlink()), "That account isn&#39;t linked.")

    def test_a_failed_revocation_still_unlinks_and_says_so(self):
        """The service being unreachable is not a reason to refuse someone the
        removal of their own link — but they are told, because finishing the job
        at the service is something only they can do."""
        user = self.make_user("warned", calendar_approved=True)
        self.sign_in_as(user)
        self.callback(self.start(self.path("link")))
        self.revoke_mock.side_effect = self.revoke_failure()
        resp = self.unlink()
        self.assertEqual(self.identities(), [])
        self.assertIn(self.SERVICE_HOST, self.said(resp))
