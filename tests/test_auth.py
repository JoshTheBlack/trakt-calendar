"""Unit tests for the auth core (app/auth).

Covers the Argon2id hash/verify round trip and the transparent rehash when the
hashing library's defaults move; session create/validate/expire against BOTH
clocks (the sliding window that refreshes on use, and the absolute cap that
sliding must never extend); logging out everywhere on a password change; client
IP resolution with and without a trusted proxy; and the session cookie's flags,
including the rule that the `__Host-` name and the Secure flag travel together.

No network. TRAKT_DATA_DIR points at a temp dir (set BEFORE importing app
modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_auth -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-auth-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher  # noqa: E402
from fastapi import Response  # noqa: E402

from app import auth, db  # noqa: E402
from app.config import Settings  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

DAY = 24 * 3600


def fake_request(*, peer="203.0.113.9", headers=None, cookies=None, scheme="http"):
    """The slice of Request the auth helpers actually read.

    A real Request needs an ASGI scope to construct; these tests are about the
    resolution logic, not about Starlette.
    """
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    return SimpleNamespace(
        client=SimpleNamespace(host=peer) if peer else None,
        headers=lowered,
        cookies=cookies or {},
        url=SimpleNamespace(scheme=scheme),
        state=SimpleNamespace(),
    )


class AuthTestCase(unittest.IsolatedAsyncioTestCase):
    _counter = 0

    async def asyncSetUp(self):
        AuthTestCase._counter += 1
        db.set_db_path(TMP / f"auth-{AuthTestCase._counter}.db")
        await db.migrate()
        auth._warned_default_proxy = False

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def make_user(self, username="alice", password=None, **flags):
        return await auth.create_user(
            username=username, password=password, settings=Settings(), **flags)


class PasswordTests(AuthTestCase):
    async def test_hash_verify_round_trip(self):
        stored = await auth.hash_password("correct horse battery staple")
        self.assertTrue(stored.startswith("$argon2id$"), "must be argon2id, not argon2i/d")
        self.assertTrue((await auth.verify_password(stored, "correct horse battery staple")).ok)
        self.assertFalse((await auth.verify_password(stored, "wrong password")).ok)

    async def test_hashes_are_salted(self):
        a = await auth.hash_password("same")
        b = await auth.hash_password("same")
        self.assertNotEqual(a, b)

    async def test_verify_with_no_stored_hash_fails(self):
        """An account with no password must fail exactly like a wrong password,
        including spending the same CPU, so it isn't a timing oracle."""
        self.assertFalse((await auth.verify_password(None, "anything")).ok)
        self.assertFalse((await auth.verify_password("", "anything")).ok)

    async def test_verify_survives_a_corrupt_stored_hash(self):
        self.assertFalse((await auth.verify_password("not-a-hash", "anything")).ok)

    async def test_outdated_hash_is_upgraded_on_a_successful_verify(self):
        """Simulated by storing a hash made with weaker parameters, which is what
        a hash written by an older version of the hashing library looks like."""
        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
        stored = weak.hash("hunter2hunter2")

        result = await auth.verify_password(stored, "hunter2hunter2")
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.new_hash, "an outdated hash must be upgraded")
        self.assertNotEqual(result.new_hash, stored)
        # The upgraded hash verifies, and needs no further upgrade.
        follow_up = await auth.verify_password(result.new_hash, "hunter2hunter2")
        self.assertTrue(follow_up.ok)
        self.assertIsNone(follow_up.new_hash)

    async def test_current_defaults_do_not_trigger_a_rehash(self):
        stored = await auth.hash_password("hunter2hunter2")
        self.assertIsNone((await auth.verify_password(stored, "hunter2hunter2")).new_hash)

    async def test_upgrading_a_hash_does_not_revoke_sessions(self):
        """A transparent rehash is not a password change: the secret is the same,
        so the user's other sessions stay valid."""
        user_id = await self.make_user(password="hunter2hunter2")
        session_id = await auth.create_session(user_id)
        await auth.update_password_hash(user_id, await auth.hash_password("hunter2hunter2"))
        self.assertIsNotNone(await auth.validate_session(session_id))


class UserTests(AuthTestCase):
    def _seeding_settings(self) -> Settings:
        return Settings(endpoint="shows/premieres", card_style="poster",
                        day_packing="packed", hide_not_watching=True,
                        network_filter=["HBO", "Netflix"], genres="-anime",
                        countries="us,gb", timezone="America/New_York")

    async def test_create_user_seeds_layout_prefs_from_settings(self):
        """settings.json's per-user fields are the seed for a new account's
        preferences; after creation the two diverge."""
        user_id = await auth.create_user(username="seeded", password="hunter2hunter2",
                                         settings=self._seeding_settings())
        prefs = await db.fetch_one("SELECT * FROM user_prefs WHERE user_id = ?", (user_id,))
        self.assertEqual(prefs["endpoint"], "shows/premieres")
        self.assertEqual(prefs["card_style"], "poster")
        self.assertEqual(prefs["day_packing"], "packed")
        self.assertEqual(prefs["hide_not_watching"], 1)
        user = await auth.get_user(user_id)
        self.assertEqual(user["timezone"], "America/New_York")

    async def test_the_filters_are_deliberately_not_seeded(self):
        """Layout is cosmetic and inheriting it is a kindness; a filter REMOVES
        shows, and doing that to an account that never asked reads as the
        calendar not carrying them at all."""
        user_id = await auth.create_user(username="unfiltered", password="hunter2hunter2",
                                         settings=self._seeding_settings())
        prefs = await db.fetch_one("SELECT * FROM user_prefs WHERE user_id = ?", (user_id,))
        self.assertEqual(prefs["genres"], "")
        self.assertEqual(prefs["countries"], "")
        self.assertEqual(prefs["network_filter_json"], "[]")

    async def test_onboarding_alone_may_inherit_the_operators_filters(self):
        """The upgrade path: those settings ARE the bootstrap admin's own, from
        before the instance had accounts, so their calendar keeps rendering as
        it did."""
        settings = self._seeding_settings()

        def _work(conn):
            user_id = auth.insert_user(conn, username="operator", password_hash=None,
                                       is_admin=True, is_bootstrap=True)
            auth.insert_user_prefs(conn, user_id, settings, seed_filters=True)
            return user_id

        user_id = await db.transaction(_work)
        prefs = await db.fetch_one("SELECT * FROM user_prefs WHERE user_id = ?", (user_id,))
        self.assertEqual(prefs["genres"], "-anime")
        self.assertEqual(prefs["countries"], "us,gb")
        self.assertEqual(prefs["network_filter_json"], '["HBO", "Netflix"]')

    async def test_lookup_is_case_insensitive(self):
        await self.make_user(username="alice", password="hunter2hunter2")
        self.assertIsNotNone(await auth.find_user_by_username("ALICE"))

    async def test_identifier_rules(self):
        self.assertIsNone(auth.identifier_error("josh"))
        self.assertIsNone(auth.identifier_error("a1"))
        self.assertIsNotNone(auth.identifier_error("a"))            # too short
        self.assertIsNotNone(auth.identifier_error("_leading"))     # bad first character
        self.assertIsNotNone(auth.identifier_error("has space"))
        self.assertIsNotNone(auth.identifier_error("x" * 33))       # too long
        self.assertIsNotNone(auth.identifier_error("admin"))        # reserved
        self.assertIsNotNone(auth.identifier_error("ADMIN"))        # reserved, any case

    async def test_retired_identifiers_block_reuse(self):
        await db.execute(
            "INSERT INTO retired_identifiers (kind, value, retired_at) VALUES (?, ?, ?)",
            ("username", "ghost", db.now()))
        self.assertTrue(await auth.identifier_is_retired("username", "ghost"))
        self.assertTrue(await auth.identifier_is_retired("username", "GHOST"))
        self.assertFalse(await auth.identifier_is_retired("username", "alive"))
        self.assertFalse(await auth.identifier_is_retired("slug", "ghost"))


class SessionTests(AuthTestCase):
    async def test_create_and_validate(self):
        user_id = await self.make_user(calendar_approved=True, is_admin=True)
        session_id = await auth.create_session(user_id, user_agent="tests", ip_address="10.0.0.1")
        current = await auth.validate_session(session_id)
        self.assertIsNotNone(current)
        self.assertEqual(current.user_id, user_id)
        self.assertEqual(current.username, "alice")
        self.assertTrue(current.is_admin)
        self.assertTrue(current.calendar_approved)
        self.assertFalse(current.distrakt_approved)
        self.assertFalse(current.has_trakt_identity)

    async def test_unknown_and_empty_sessions_are_rejected(self):
        self.assertIsNone(await auth.validate_session(None))
        self.assertIsNone(await auth.validate_session(""))
        self.assertIsNone(await auth.validate_session("not-a-real-session-id"))

    async def test_initial_lifetimes(self):
        user_id = await self.make_user()
        start = db.now()
        session_id = await auth.create_session(user_id, now=start)
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        self.assertEqual(row["expires_at"], start + 14 * DAY)
        self.assertEqual(row["absolute_expires_at"], start + 60 * DAY)

    async def test_expired_session_is_rejected(self):
        user_id = await self.make_user()
        start = db.now()
        session_id = await auth.create_session(user_id, now=start)
        self.assertIsNone(await auth.validate_session(session_id, now=start + 14 * DAY))
        self.assertIsNone(await auth.validate_session(session_id, now=start + 15 * DAY))

    async def test_sliding_refresh_extends_expiry(self):
        user_id = await self.make_user()
        start = db.now()
        session_id = await auth.create_session(user_id, now=start)

        later = start + 10 * DAY
        current = await auth.validate_session(session_id, now=later)
        self.assertEqual(current.expires_at, later + 14 * DAY)
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        self.assertEqual(row["expires_at"], later + 14 * DAY)
        self.assertEqual(row["last_seen_at"], later)

        # ...and it is still alive well past the ORIGINAL expiry.
        self.assertIsNotNone(await auth.validate_session(session_id, now=start + 20 * DAY))

    async def test_sliding_refresh_writes_at_most_once_per_hour(self):
        """An active session must not cause a database write on every request."""
        user_id = await self.make_user()
        start = db.now()
        session_id = await auth.create_session(user_id, now=start)

        soon = start + 59 * 60
        current = await auth.validate_session(session_id, now=soon)
        self.assertEqual(current.expires_at, start + 14 * DAY, "should not have slid yet")
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        self.assertEqual(row["last_seen_at"], start)

        past_the_hour = start + 3600
        current = await auth.validate_session(session_id, now=past_the_hour)
        self.assertEqual(current.expires_at, past_the_hour + 14 * DAY)

    async def test_touch_false_never_writes(self):
        user_id = await self.make_user()
        start = db.now()
        session_id = await auth.create_session(user_id, now=start)
        await auth.validate_session(session_id, now=start + 5 * DAY, touch=False)
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        self.assertEqual(row["expires_at"], start + 14 * DAY)
        self.assertEqual(row["last_seen_at"], start)

    async def test_absolute_cap_is_never_extended_by_sliding(self):
        """Walk a session forward in ten-day steps, each of which slides it, and
        confirm it still dies at the absolute cap measured from creation."""
        user_id = await self.make_user()
        start = db.now()
        session_id = await auth.create_session(user_id, now=start)

        for step in range(1, 6):  # +10d through +50d: all valid, all sliding
            now = start + step * 10 * DAY
            current = await auth.validate_session(session_id, now=now)
            self.assertIsNotNone(current, f"should still be valid at +{step * 10}d")
            self.assertEqual(current.absolute_expires_at, start + 60 * DAY,
                             "the absolute cap must never move")
            self.assertLessEqual(current.expires_at, start + 60 * DAY,
                                 "sliding must clamp to the absolute cap")

        self.assertIsNone(await auth.validate_session(session_id, now=start + 60 * DAY))
        self.assertIsNone(await auth.validate_session(session_id, now=start + 61 * DAY))

    async def test_disabled_account_invalidates_the_session(self):
        user_id = await self.make_user()
        session_id = await auth.create_session(user_id)
        await db.execute("UPDATE users SET is_disabled = 1 WHERE id = ?", (user_id,))
        self.assertIsNone(await auth.validate_session(session_id))

    async def test_revoke_session_hard_deletes(self):
        user_id = await self.make_user()
        session_id = await auth.create_session(user_id)
        await auth.revoke_session(session_id)
        self.assertIsNone(await auth.validate_session(session_id))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM sessions"), 0)

    async def test_revoke_user_sessions_spares_other_users(self):
        alice = await self.make_user(username="alice")
        bob = await self.make_user(username="bob")
        await auth.create_session(alice)
        await auth.create_session(alice)
        bob_session = await auth.create_session(bob)
        self.assertEqual(await auth.revoke_user_sessions(alice), 2)
        self.assertIsNotNone(await auth.validate_session(bob_session))

    async def test_password_change_revokes_every_session(self):
        """Enforced by set_password itself rather than left to each caller to
        remember, so it holds for admin-driven resets too."""
        user_id = await self.make_user(password="hunter2hunter2")
        sessions = [await auth.create_session(user_id) for _ in range(3)]
        other = await self.make_user(username="bob")
        other_session = await auth.create_session(other)

        await auth.set_password(user_id, "a-brand-new-password")

        for session_id in sessions:
            self.assertIsNone(await auth.validate_session(session_id))
        self.assertIsNotNone(await auth.validate_session(other_session))

        user = await auth.get_user(user_id)
        self.assertIsNotNone(user["password_changed_at"])
        self.assertTrue((await auth.verify_password(user["password_hash"],
                                                    "a-brand-new-password")).ok)
        self.assertFalse((await auth.verify_password(user["password_hash"],
                                                     "hunter2hunter2")).ok)

    async def test_sweep_deletes_only_dead_sessions(self):
        user_id = await self.make_user()
        start = db.now()
        dead = await auth.create_session(user_id, now=start - 20 * DAY)
        alive = await auth.create_session(user_id, now=start)
        self.assertEqual(await auth.sweep_expired_sessions(start), 1)
        rows = {r["id"] for r in await db.fetch_all("SELECT id FROM sessions")}
        self.assertEqual(rows, {alive})
        self.assertNotIn(dead, rows)

    async def test_deleting_a_user_cascades_to_sessions(self):
        user_id = await self.make_user()
        session_id = await auth.create_session(user_id)
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.assertIsNone(await auth.validate_session(session_id))

    async def test_has_trakt_identity_reflects_linked_identities(self):
        user_id = await self.make_user()
        session_id = await auth.create_session(user_id)
        self.assertFalse((await auth.validate_session(session_id)).has_trakt_identity)

        def _link(conn):
            auth.insert_linked_identity(conn, user_id=user_id, provider="trakt",
                                        provider_user_id=12345, display_name="Josh")
        await db.transaction(_link)
        self.assertTrue((await auth.validate_session(session_id)).has_trakt_identity)

        # A Plex link is not a Trakt link, and distrakt needs specifically Trakt.
        other = await self.make_user(username="bob")
        other_session = await auth.create_session(other)
        await db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=other, provider="plex", provider_user_id=999))
        self.assertFalse((await auth.validate_session(other_session)).has_trakt_identity)


class DependencyTests(AuthTestCase):
    async def _request_for(self, **flags):
        user_id = await self.make_user(**flags)
        session_id = await auth.create_session(user_id)
        return fake_request(cookies={auth.COOKIE_NAME_SECURE: session_id})

    async def test_signed_out_is_401_at_every_level(self):
        self.assertIsNone(await auth.current_user(fake_request()))
        for dep in (auth.require_session, auth.require_calendar,
                    auth.require_distrakt, auth.require_admin):
            with self.assertRaises(auth.AuthError) as ctx:
                await dep(fake_request())
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertEqual(ctx.exception.reason, "login_required")

    async def test_session_level_accepts_an_unapproved_user(self):
        request = await self._request_for()
        self.assertIsNotNone(await auth.require_session(request))
        with self.assertRaises(auth.AuthError) as ctx:
            await auth.require_calendar(request)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.reason, "awaiting_approval")

    async def test_calendar_level(self):
        request = await self._request_for(calendar_approved=True)
        self.assertIsNotNone(await auth.require_calendar(request))
        with self.assertRaises(auth.AuthError):
            await auth.require_admin(request)

    async def test_distrakt_level_requires_approval_and_a_trakt_link(self):
        user_id = await self.make_user(calendar_approved=True)
        session_id = await auth.create_session(user_id)
        request = fake_request(cookies={auth.COOKIE_NAME_SECURE: session_id})
        with self.assertRaises(auth.AuthError) as ctx:
            await auth.require_distrakt(request)
        self.assertEqual(ctx.exception.reason, "distrakt_not_approved")

        await db.execute("UPDATE users SET distrakt_approved = 1 WHERE id = ?", (user_id,))
        with self.assertRaises(auth.AuthError) as ctx:
            await auth.require_distrakt(fake_request(
                cookies={auth.COOKIE_NAME_SECURE: session_id}))
        self.assertEqual(ctx.exception.reason, "trakt_link_required")

        await db.transaction(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=1))
        self.assertIsNotNone(await auth.require_distrakt(fake_request(
            cookies={auth.COOKIE_NAME_SECURE: session_id})))

    async def test_admin_level(self):
        request = await self._request_for(is_admin=True)
        self.assertIsNotNone(await auth.require_admin(request))

    async def test_current_user_is_cached_per_request(self):
        request = await self._request_for()
        first = await auth.current_user(request)
        await auth.revoke_session(first.session_id)
        self.assertIs(await auth.current_user(request), first)
        self.assertIsNone(await auth.current_user(fake_request(
            cookies={auth.COOKIE_NAME_SECURE: first.session_id})))

    def test_every_level_has_a_dependency(self):
        self.assertEqual(set(auth.DEPENDENCY_FOR_LEVEL), set(auth.AuthLevel))
        self.assertIsNone(auth.DEPENDENCY_FOR_LEVEL[auth.AuthLevel.PUBLIC])


class ClientIpTests(unittest.TestCase):
    TRUSTED = Settings(trusted_proxy_ips="172.18.0.0/16, 127.0.0.1/32")
    UNTRUSTED = Settings(trusted_proxy_ips="")

    def setUp(self):
        auth._warned_default_proxy = False

    def test_no_proxy_configured_ignores_forwarded_headers(self):
        """Trusting a header from an untrusted peer would let anyone claim any
        source address and slip a per-IP rate limit, so they are dropped
        entirely rather than merged."""
        request = fake_request(peer="198.51.100.7",
                               headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(auth.client_ip(request, self.UNTRUSTED), "198.51.100.7")

    def test_untrusted_peer_claiming_to_be_a_proxy_is_ignored(self):
        request = fake_request(peer="198.51.100.7",
                               headers={"X-Forwarded-For": "1.2.3.4, 172.18.0.5"})
        self.assertEqual(auth.client_ip(request, self.TRUSTED), "198.51.100.7")

    def test_trusted_proxy_yields_the_forwarded_client(self):
        request = fake_request(peer="172.18.0.5", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(auth.client_ip(request, self.TRUSTED), "203.0.113.9")

    def test_trusted_chain_walks_right_to_left_past_trusted_hops(self):
        request = fake_request(peer="172.18.0.5",
                               headers={"X-Forwarded-For": "203.0.113.9, 172.18.0.9"})
        self.assertEqual(auth.client_ip(request, self.TRUSTED), "203.0.113.9")

    def test_a_client_supplied_prefix_does_not_win(self):
        """A client that sends its own X-Forwarded-For gets appended to, not
        substituted, so the rightmost untrusted entry is the real peer."""
        request = fake_request(peer="172.18.0.5",
                               headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.9"})
        self.assertEqual(auth.client_ip(request, self.TRUSTED), "203.0.113.9")

    def test_trusted_peer_with_no_forwarded_header_falls_back_to_the_peer(self):
        request = fake_request(peer="172.18.0.5")
        self.assertEqual(auth.client_ip(request, self.TRUSTED), "172.18.0.5")

    def test_x_real_ip_is_honored_only_from_a_trusted_peer(self):
        trusted = fake_request(peer="172.18.0.5", headers={"X-Real-IP": "203.0.113.9"})
        self.assertEqual(auth.client_ip(trusted, self.TRUSTED), "203.0.113.9")
        untrusted = fake_request(peer="198.51.100.7", headers={"X-Real-IP": "203.0.113.9"})
        self.assertEqual(auth.client_ip(untrusted, self.TRUSTED), "198.51.100.7")

    def test_all_hops_trusted_falls_back_to_the_leftmost(self):
        request = fake_request(peer="172.18.0.5",
                               headers={"X-Forwarded-For": "172.18.0.2, 172.18.0.9"})
        self.assertEqual(auth.client_ip(request, self.TRUSTED), "172.18.0.2")

    def test_garbage_cidrs_are_dropped_not_fatal(self):
        settings = Settings(trusted_proxy_ips="not-an-ip, 172.18.0.0/16")
        request = fake_request(peer="172.18.0.5", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(auth.client_ip(request, settings), "203.0.113.9")

    def test_missing_client_is_not_a_crash(self):
        self.assertEqual(auth.client_ip(fake_request(peer=None), self.TRUSTED), "unknown")

    def test_default_proxy_config_with_forwarded_headers_warns_once(self):
        """That combination silently collapses every user onto the proxy's
        address, which turns per-IP rate limiting into a global one."""
        default = Settings(trusted_proxy_ips="127.0.0.1/32")
        request = fake_request(peer="172.18.0.5", headers={"X-Forwarded-For": "203.0.113.9"})
        with self.assertLogs("app.auth", level="WARNING") as captured:
            auth.client_ip(request, default)
        self.assertIn("trusted_proxy_ips", captured.output[0])
        # Once, not on every request.
        self.assertTrue(auth._warned_default_proxy)
        with self.assertRaises(AssertionError):
            with self.assertLogs("app.auth", level="WARNING"):
                auth.client_ip(request, default)

    def test_no_warning_without_forwarded_headers(self):
        default = Settings(trusted_proxy_ips="127.0.0.1/32")
        with self.assertRaises(AssertionError):
            with self.assertLogs("app.auth", level="WARNING"):
                auth.client_ip(fake_request(peer="172.18.0.5"), default)

    def test_no_warning_once_configured(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs("app.auth", level="WARNING"):
                auth.client_ip(
                    fake_request(peer="172.18.0.5", headers={"X-Forwarded-For": "203.0.113.9"}),
                    self.TRUSTED)


class CookieTests(unittest.TestCase):
    def _set(self, settings, request=None):
        response = Response()
        auth.set_session_cookie(response, "SESSIONVALUE", settings, request)
        return response.headers["set-cookie"]

    def test_default_is_secure_and_host_prefixed(self):
        """The default must not depend on the request's scheme, because behind a
        TLS-terminating proxy that scheme is "http". The `__Host-` prefix is
        browser-enforced and requires Secure, Path=/, and no Domain together."""
        header = self._set(Settings(), fake_request(scheme="http"))
        self.assertIn("__Host-tns_session=SESSIONVALUE", header)
        self.assertIn("Secure", header)
        self.assertIn("HttpOnly", header)
        self.assertIn("Path=/", header)
        self.assertIn("samesite=lax", header.lower())
        self.assertNotIn("Domain=", header)

    def test_never_drops_secure_and_the_host_prefix_together(self):
        """The prefix is only legal with Secure — sending `__Host-` without it
        would make the browser reject the cookie outright."""
        header = self._set(Settings(cookie_secure="never"), fake_request(scheme="http"))
        self.assertIn("tns_session=SESSIONVALUE", header)
        self.assertNotIn("__Host-", header)
        self.assertNotIn("Secure", header)
        self.assertIn("HttpOnly", header)

    def test_auto_follows_the_scheme(self):
        settings = Settings(cookie_secure="auto")
        self.assertNotIn("Secure", self._set(settings, fake_request(scheme="http")))
        self.assertIn("Secure", self._set(settings, fake_request(scheme="https")))

    def test_auto_honors_x_forwarded_proto_only_from_a_trusted_proxy(self):
        settings = Settings(cookie_secure="auto", trusted_proxy_ips="172.18.0.0/16")
        trusted = fake_request(peer="172.18.0.5", scheme="http",
                               headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", self._set(settings, trusted))
        spoofed = fake_request(peer="198.51.100.7", scheme="http",
                               headers={"X-Forwarded-Proto": "https"})
        self.assertNotIn("Secure", self._set(settings, spoofed))

    def test_max_age_is_the_absolute_cap(self):
        header = self._set(Settings(), fake_request())
        self.assertIn(f"Max-Age={auth.SESSION_ABSOLUTE_SECONDS}", header)

    def test_cookie_name_matches_the_secure_flag(self):
        self.assertEqual(auth.session_cookie_name(Settings()), auth.COOKIE_NAME_SECURE)
        self.assertEqual(auth.session_cookie_name(Settings(cookie_secure="never")),
                         auth.COOKIE_NAME)

    def test_read_accepts_either_name(self):
        """So that changing the Secure policy doesn't sign the whole instance
        out."""
        secure = Settings()
        plain = Settings(cookie_secure="never")
        self.assertEqual(auth.read_session_cookie(
            fake_request(cookies={auth.COOKIE_NAME_SECURE: "A"}), secure), "A")
        self.assertEqual(auth.read_session_cookie(
            fake_request(cookies={auth.COOKIE_NAME: "B"}), secure), "B")
        self.assertEqual(auth.read_session_cookie(
            fake_request(cookies={auth.COOKIE_NAME_SECURE: "C"}), plain), "C")
        self.assertIsNone(auth.read_session_cookie(fake_request(), secure))

    def test_clear_removes_both_names(self):
        response = Response()
        auth.clear_session_cookie(response, Settings(), fake_request())
        headers = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
        self.assertEqual(len(headers), 2, headers)
        self.assertTrue(any(h.startswith("__Host-tns_session=") for h in headers), headers)
        self.assertTrue(any(h.startswith("tns_session=") for h in headers), headers)


if __name__ == "__main__":
    unittest.main()
