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


class HeartbeatRefreshTests(unittest.TestCase):
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
