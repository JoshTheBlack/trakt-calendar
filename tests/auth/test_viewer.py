"""The settings object that authenticates as one signed-in person
(app/auth/viewer.py's settings_for_user).

Every feature that reads somebody's own viewing, or asks a service that answers
only with that person's own token, builds its settings here — so what this
returns decides whose viewing a request can see. The instance-wide Trakt token
is deliberately given a value in these tests, so "the user's token was used" and
"no token at all was used" cannot be confused with each other.

Which routes USE it is each feature's own question and is tested there
(tests/distrakt/test_gating.py for the tracker).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.auth import viewer
from app.config import Settings, save_settings
from tests.support import AppTestCase


class SettingsForUserTests(AppTestCase):
    APP_WIDE = "APP-WIDE-OPERATOR-TOKEN"

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            trakt_client_id="client-id",
            trakt_access_token=self.APP_WIDE,
            trakt_refresh_token="APP-WIDE-REFRESH",
        ))
        self.user_id = self.make_user("viewer", calendar_approved=True)

    def _settings(self):
        return asyncio.run(viewer.settings_for_user(self.user_id))

    def test_the_settings_object_carries_the_users_token_not_the_instances(self):
        self.link_identity(self.user_id, "trakt", 1001, "THE-USERS-TOKEN")
        settings = self._settings()
        self.assertEqual(settings.trakt_access_token, "THE-USERS-TOKEN")
        self.assertNotEqual(settings.trakt_access_token, self.APP_WIDE)
        # The operator's refresh token has no business travelling next to
        # somebody else's access token.
        self.assertEqual(settings.trakt_refresh_token, "")
        # Everything genuinely app-wide still comes through.
        self.assertEqual(settings.trakt_client_id, "client-id")

    def test_both_services_tokens_ride_on_the_one_settings_object(self):
        """Each source reads its own field off `settings` — that is what
        `is_configured` asks about — so a per-source credential object would be a
        second mechanism for something this one already expresses, and the port
        protocol would have to grow an argument for it."""
        self.link_identity(self.user_id, "trakt", 1001, "THE-USERS-TOKEN")
        # V2-shaped (see app/auth/simkl.py.V2_ACCESS_TOKEN_PREFIX): a token
        # missing this prefix reads as a legacy v1 grant and is never sent
        # (simkl_routes._is_v2_token), which is not what this test is about.
        self.link_identity(self.user_id, "simkl", 90210, "simkl_at_THEIR-SIMKL-TOKEN")
        settings = self._settings()
        self.assertEqual(settings.trakt_access_token, "THE-USERS-TOKEN")
        self.assertEqual(settings.simkl_access_token, "simkl_at_THEIR-SIMKL-TOKEN")

    def test_an_account_with_no_simkl_link_carries_no_simkl_token(self):
        """`simkl_configured` then goes false and that port is simply not asked,
        which is exactly what has always been true of Trakt."""
        self.link_identity(self.user_id, "trakt", 1001, "THE-USERS-TOKEN")
        settings = self._settings()
        self.assertEqual(settings.simkl_access_token, "")
        self.assertFalse(settings.simkl_configured)

    def test_an_account_with_nothing_linked_carries_no_token_at_all(self):
        """Not the instance's as a fallback: that would show them the operator's
        watch history."""
        settings = self._settings()
        self.assertEqual(settings.trakt_access_token, "")
        self.assertFalse(settings.trakt_configured)

    def test_it_comes_from_the_per_user_token_source(self):
        """Pinned to the function that knows how to renew a user's token, rather
        than to a direct read of the identity row, so an expired token is
        refreshed on this path exactly as it is everywhere else."""
        self.link_identity(self.user_id, "trakt", 1001, "STORED")

        async def _fake(uid, settings=None):
            return "FROM-THE-SOURCE"

        with patch("app.auth.trakt_routes.access_token_for_user",
                   side_effect=_fake) as spy:
            settings = self._settings()
        spy.assert_awaited_once()
        self.assertEqual(spy.await_args.args[0], self.user_id)
        self.assertEqual(settings.trakt_access_token, "FROM-THE-SOURCE")
