"""What a refused provider completion does with the token it was handed
(app/auth/provider_login.py's `end_unkept_grant`), beyond what each provider's
own routes show in tests/auth/_redirect_login.py: the table every linkable
service answers, and the tokens it must never revoke.
"""
from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, patch

from app import auth, db
from app.auth import provider_login, revocation
from tests.support import AppTestCase, load_settings, save_settings


class AnUnkeptGrantTests(AppTestCase):

    def setUp(self):
        super().setUp()
        self.user = self.make_user("someone", calendar_approved=True)
        patcher = patch.object(revocation, "revoke", AsyncMock(return_value=None))
        self.revoke = patcher.start()
        self.addCleanup(patcher.stop)

    def identity(self, provider="trakt", token="fresh", account="acct-1"):
        return auth.ProviderIdentity(provider=provider, provider_user_id=account,
                                     display_name=None, access_token=token)

    def hold(self, stored, provider="trakt", account="acct-1"):
        """A row as stored — `stored` is the column's value, sealed or not."""
        asyncio.run(db.execute(
            "INSERT INTO linked_identities (user_id, provider, provider_user_id, access_token, "
            "created_at) VALUES (?, ?, ?, ?, 0)",
            (self.user, provider, account, stored)))

    def test_every_linkable_service_says_whether_it_has_an_instance_token(self):
        self.assertEqual(set(provider_login.INSTANCE_TOKEN_FIELDS), set(revocation.LINKABLE))

    def test_an_unheld_token_is_revoked(self):
        asyncio.run(provider_login.end_unkept_grant(self.identity()))
        self.revoke.assert_awaited_once_with("trakt", "fresh")

    def test_the_instances_own_trakt_token_is_never_revoked(self):
        save_settings(dataclasses.replace(load_settings(), trakt_access_token="fresh"))
        asyncio.run(provider_login.end_unkept_grant(self.identity()))
        self.revoke.assert_not_awaited()

    def test_a_row_that_cannot_be_read_keeps_every_token(self):
        """Sealed, with no key that opens it: nobody can say the fresh token is
        not the one it holds."""
        self.hold("enc:v1:not-a-token-this-key-opens")
        asyncio.run(provider_login.end_unkept_grant(self.identity()))
        self.revoke.assert_not_awaited()

    def test_another_service_accounts_row_says_nothing_about_this_token(self):
        self.hold("fresh", account="acct-2")
        asyncio.run(provider_login.end_unkept_grant(self.identity()))
        self.revoke.assert_awaited_once_with("trakt", "fresh")

    def test_plex_publishes_no_revocation_and_is_asked_nothing(self):
        asyncio.run(provider_login.end_unkept_grant(self.identity(provider="plex")))
        self.revoke.assert_not_awaited()

    def test_a_failed_revocation_only_logs(self):
        self.revoke.return_value = "could not be told"
        with self.assertLogs("app.auth.provider_login", "WARNING"):
            asyncio.run(provider_login.end_unkept_grant(self.identity()))
