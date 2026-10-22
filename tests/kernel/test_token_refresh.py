"""The heartbeat's renewal of the instance's own Trakt token.

It runs on every tick and does nothing almost every time, which is exactly why
it needs saying out loud what it does when it is due: a renewal that never fired
reads as the calendar going quiet weeks later, when the token finally lapses,
with nothing in the log pointing back here.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import httpx

from app import db, settings_routes
from app.auth import trakt as trakt_auth
from app.config import Settings, load_settings, save_settings
from tests.support import migrated_db


class _RefreshCase(unittest.TestCase):
    """The database, the saved credentials and one heartbeat tick, shared by the
    classes below without either running the other's tests."""

    def setUp(self):
        migrated_db("token-refresh")
        self.calls: list[tuple] = []

    def tearDown(self):
        db.close_thread_connection()

    def _save(self, **overrides):
        values = dict(trakt_client_id="cid", trakt_client_secret="secret",
                      trakt_access_token="old-access", trakt_refresh_token="old-refresh",
                      trakt_token_expires_at=int(time.time()) - 60)
        values.update(overrides)
        save_settings(Settings(**values))

    def _tick(self, answer=None, error=None):
        async def refresh(client_id, client_secret, refresh_token):
            self.calls.append((client_id, client_secret, refresh_token))
            if error is not None:
                raise error
            return answer
        with mock.patch.object(trakt_auth, "refresh_access_token", refresh):
            asyncio.run(settings_routes.maybe_refresh_trakt_token())


class HeartbeatRefreshTests(_RefreshCase):

    def test_an_expired_token_is_renewed_and_both_halves_are_kept(self):
        """Trakt issues a NEW refresh token every time one is spent and retires
        the old one, so saving only the access token would strand the next renewal."""
        self._save()
        now = int(time.time())
        self._tick({"access_token": "new-access", "refresh_token": "new-refresh",
                    "created_at": now, "expires_in": 7776000})
        self.assertEqual(self.calls, [("cid", "secret", "old-refresh")])
        saved = load_settings()
        self.assertEqual((saved.trakt_access_token, saved.trakt_refresh_token),
                         ("new-access", "new-refresh"))
        self.assertEqual(saved.trakt_token_expires_at, now + 7776000)

    def test_a_token_still_in_date_is_left_alone(self):
        self._save(trakt_token_expires_at=int(time.time()) + 3600)
        self._tick({"access_token": "x"})
        self.assertEqual(self.calls, [])

    def test_nothing_is_asked_without_everything_a_refresh_needs(self):
        """No expiry recorded (a token pasted by hand), or any credential blank."""
        for overrides in ({"trakt_token_expires_at": 0}, {"trakt_refresh_token": ""},
                          {"trakt_client_secret": ""}):
            with self.subTest(overrides=overrides):
                # A fresh database each time: saving a blank secret KEEPS the
                # stored one, so a case after the first would inherit it.
                db.close_thread_connection()
                migrated_db("token-refresh")
                self._save(**overrides)
                self._tick({"access_token": "x"})
        self.assertEqual(self.calls, [])

    def test_a_failed_renewal_keeps_the_old_token_and_does_not_escape_the_tick(self):
        for error in (httpx.ConnectError("down"), trakt_auth.TraktRateLimitError("429")):
            with self.subTest(error=type(error).__name__):
                self._save()
                self._tick(error=error)
                self.assertEqual(load_settings().trakt_access_token, "old-access")


class OneGrantHeldTwiceTests(_RefreshCase):
    """The app-wide token and an administrator's linked identity can be ONE Trakt
    grant (trakt_routes.adopt_app_token links the pair Settings holds). Trakt
    retires a refresh token the moment it is spent, so a renewal stored in only
    one place left the other with a dead token: the heartbeat then logged a 400
    every minute until "Authorize with Trakt" was run again."""

    def _identity(self, user_id: int, refresh: str) -> int:
        from app import auth, secrets_box

        def _link(conn):
            auth.insert_linked_identity(conn, user_id=user_id, provider="trakt",
                                        provider_user_id=str(user_id))
        asyncio.run(db.transaction(_link))
        row_id = asyncio.run(db.fetch_value(
            "SELECT id FROM linked_identities WHERE user_id = ?", (user_id,)))
        asyncio.run(db.execute(
            "UPDATE linked_identities SET access_token = ?, refresh_token = ?, "
            "token_expires_at = ? WHERE id = ?",
            (secrets_box.seal("old-access"), secrets_box.seal(refresh),
             int(time.time()) - 60, row_id)))
        return row_id

    def _user(self) -> int:
        from app import auth
        return asyncio.run(auth.create_user(username="admin1", password="a-long-password-1"))

    def _identity_refresh(self, row_id):
        from app import secrets_box
        stored = asyncio.run(db.fetch_value(
            "SELECT refresh_token FROM linked_identities WHERE id = ?", (row_id,)))
        return secrets_box.open_(stored)

    NEW = {"access_token": "new-access", "refresh_token": "new-refresh",
           "created_at": 1, "expires_in": 7776000}

    def test_the_heartbeat_renewal_reaches_the_identity_holding_the_same_grant(self):
        self._save()
        row_id = self._identity(self._user(), "old-refresh")
        self._tick(dict(self.NEW, created_at=int(time.time())))
        self.assertEqual(self._identity_refresh(row_id), "new-refresh")

    def test_a_users_own_renewal_reaches_the_app_wide_copy(self):
        from app.auth import trakt_routes
        self._save()
        user = self._user()
        self._identity(user, "old-refresh")

        async def refresh(client_id, client_secret, refresh_token):
            return dict(self.NEW, created_at=int(time.time()))
        with mock.patch.object(trakt_auth, "refresh_access_token", refresh):
            token = asyncio.run(trakt_routes.access_token_for_user(user))
        self.assertEqual(token, "new-access")
        self.assertEqual(load_settings().trakt_refresh_token, "new-refresh")

    def test_a_different_grant_is_left_alone(self):
        self._save()
        row_id = self._identity(self._user(), "someone-elses-refresh")
        self._tick(dict(self.NEW, created_at=int(time.time())))
        self.assertEqual(self._identity_refresh(row_id), "someone-elses-refresh")

    def test_a_refusal_says_whose_token_and_trakts_reason(self):
        self._save()
        user = self._user()
        self._identity(user, "old-refresh")
        refused = httpx.HTTPStatusError(
            "400", request=httpx.Request("POST", "https://api.trakt.tv/oauth/token"),
            response=httpx.Response(400, json={"error": "invalid_grant"}))
        with self.assertLogs("app.settings_routes", "WARNING") as logs:
            self._tick(error=refused)
        line = "\n".join(logs.output)
        self.assertIn("the app-wide token in Settings > Trakt", line)
        self.assertIn(f"user {user}'s linked Trakt account", line)
        self.assertIn("HTTP 400 invalid_grant", line)
        self.assertNotIn("old-refresh", line)
