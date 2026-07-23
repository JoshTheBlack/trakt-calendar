"""Cross-cutting integration sweep for at-rest encryption.

The unit and boundary tests elsewhere (test_secrets_box, test_sealing,
test_encryption_flow, test_encryption_routes) each prove one layer in isolation.
This file proves the layers actually compose: that a per-user identity created
today reads back through the real Trakt-call path with the plaintext token on the
wire; that the app-wide Trakt credential app/trakt.py uses on every calendar fetch
does the same; and that a full enable -> encrypt -> lose-the-key -> recover cycle
leaves every one of those call paths working at the far end.

No network — the Trakt HTTP client is patched and its Authorization header
inspected, which is the only place a token becomes observable.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_encryption_integration -v
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-encintegration-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from app import auth, db, encryption_flow, main, secrets_backfill, secrets_box, trakt, trakt_routes  # noqa: E402
from app.config import Settings, load_settings, save_settings  # noqa: E402
from app.endpoints import get_endpoint  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
SHOWS = get_endpoint("shows")

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


def _set_key(key: str | None) -> None:
    if key is None:
        os.environ.pop(secrets_box.ENV_VAR, None)
    else:
        os.environ[secrets_box.ENV_VAR] = key
    secrets_box.reset_cache()


class _RecordingClient:
    """Stands in for the pooled Trakt client and remembers every Authorization
    header it was handed — the only place a token becomes observable on the wire."""

    def __init__(self, body=None):
        self.body = body if body is not None else []
        self.authorizations: list[str] = []

    async def get(self, url, headers=None):
        self.authorizations.append((headers or {}).get("Authorization", ""))

        class _Resp:
            status_code = 200
            headers = {}

            def json(_self):
                return self.body

        return _Resp()


class EncryptionIntegrationTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        EncryptionIntegrationTestCase._counter += 1
        db.set_db_path(TMP / f"encintegration-{EncryptionIntegrationTestCase._counter}.db")
        asyncio.run(db.migrate())
        _set_key(None)
        save_settings(Settings())

    def tearDown(self):
        db.close_thread_connection()
        _set_key(None)
        try:
            from app import config
            config.SETTINGS_FILE.unlink()
        except OSError:
            pass
        # Shared module state — leave the process healthy for unrelated tests.
        encryption_flow._health = encryption_flow.HEALTHY


class IdentityToProviderCallTests(EncryptionIntegrationTestCase):
    """create identity -> ciphertext in the DB -> the provider-call path yields
    the correct plaintext, all the way to the Authorization header Trakt sees."""

    def test_a_linked_users_token_is_ciphertext_at_rest_and_plaintext_on_the_wire(self):
        _set_key(KEY)
        user_id = asyncio.run(auth.create_user(
            username="alice", password=None, settings=Settings()))

        def _link(conn):
            return auth.insert_linked_identity(
                conn, user_id=user_id, provider="trakt", provider_user_id="uuid-1",
                display_name="Alice", access_token="alice-token", refresh_token="alice-refresh",
            )

        identity_id = asyncio.run(db.transaction(_link))

        raw = asyncio.run(db.fetch_value(
            "SELECT access_token FROM linked_identities WHERE id = ?", (identity_id,)))
        self.assertTrue(raw.startswith(secrets_box.PREFIX))
        self.assertNotIn("alice-token", raw)

        # The read sites that feed a real Trakt call both resolve the ciphertext
        # back to the plaintext token.
        token = asyncio.run(trakt_routes.access_token_for_user(user_id))
        self.assertEqual(token, "alice-token")
        self.assertEqual(asyncio.run(trakt_routes.stored_access_token(user_id)), "alice-token")

        # And a settings object built from it (the shape main._distrakt_settings
        # hands to app/trakt.py) puts that plaintext token on the wire.
        settings = dataclasses.replace(load_settings(), trakt_access_token=token)
        recorder = _RecordingClient()
        with patch("app.trakt.shared_client", return_value=recorder):
            asyncio.run(trakt.fetch_calendar(SHOWS, settings, 2026, 7))
        self.assertEqual(recorder.authorizations, ["Bearer alice-token"])


class AppLevelTraktTokenTests(EncryptionIntegrationTestCase):
    """The operator's own Trakt token, sealed in app_secrets and read through
    load_settings() on the highest-traffic path: every calendar fetch."""

    def test_the_app_wide_token_is_sealed_at_rest_and_plaintext_on_a_calendar_fetch(self):
        _set_key(KEY)
        save_settings(Settings(
            trakt_client_id="client-id",
            trakt_access_token="operator-token",
            trakt_refresh_token="operator-refresh",
        ))
        raw = asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'trakt_access_token'"))
        self.assertTrue(raw.startswith(secrets_box.PREFIX))
        self.assertNotIn("operator-token", raw)

        settings = load_settings()
        self.assertEqual(settings.trakt_access_token, "operator-token")

        recorder = _RecordingClient()
        with patch("app.trakt.shared_client", return_value=recorder):
            asyncio.run(trakt.fetch_calendar(SHOWS, settings, 2026, 7))
        self.assertEqual(recorder.authorizations, ["Bearer operator-token"])

    def test_with_no_key_the_same_fetch_still_carries_the_plaintext_token(self):
        """No-key behavior must stay byte-for-byte identical to before encryption
        existed — the app-wide token round-trips as plaintext either way."""
        _set_key(None)
        save_settings(Settings(trakt_client_id="client-id", trakt_access_token="plain-token"))
        settings = load_settings()
        recorder = _RecordingClient()
        with patch("app.trakt.shared_client", return_value=recorder):
            asyncio.run(trakt.fetch_calendar(SHOWS, settings, 2026, 7))
        self.assertEqual(recorder.authorizations, ["Bearer plain-token"])


class EndToEndEnableAndRecoverTests(EncryptionIntegrationTestCase):
    """From a plaintext instance, through the two-stage enable flow, to a lost-key
    recovery — checking at each stage that the real call paths still work."""

    def _calendar_call_uses(self, expected_token: str) -> None:
        settings = load_settings()
        recorder = _RecordingClient()
        with patch("app.trakt.shared_client", return_value=recorder):
            asyncio.run(trakt.fetch_calendar(SHOWS, settings, 2026, 7))
        self.assertEqual(recorder.authorizations, [f"Bearer {expected_token}"])

    def test_enable_encrypt_lose_key_restore_key_door_one(self):
        _set_key(None)
        save_settings(Settings(trakt_client_id="client-id", trakt_access_token="op-token"))
        self._calendar_call_uses("op-token")

        # Stage 1: choose to enable, get a generated key, restart-required.
        result = asyncio.run(encryption_flow.begin_enable(generate=True))
        self.assertTrue(result["restart_required"])
        key = result["key"]
        self.assertEqual(asyncio.run(encryption_flow.get_phase()), encryption_flow.PHASE_PENDING_KEY)

        # Restart: the key survives into the environment.
        _set_key(key)
        self.assertTrue(asyncio.run(encryption_flow.verify_key()))
        self.assertEqual(asyncio.run(encryption_flow.get_phase()), encryption_flow.PHASE_PENDING_ENCRYPT)

        # Stage 2: encrypt in place.
        asyncio.run(encryption_flow.encrypt_now())
        self.assertEqual(asyncio.run(encryption_flow.get_phase()), encryption_flow.PHASE_ENCRYPTED)
        raw = asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'trakt_access_token'"))
        self.assertTrue(raw.startswith(secrets_box.PREFIX))

        # The calendar fetch still works, transparently, after encryption.
        self._calendar_call_uses("op-token")
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()), encryption_flow.HEALTHY)

        # The key is lost and a fresh one lands in the environment instead.
        _set_key(OTHER_KEY)
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()), encryption_flow.KEY_MISMATCH)
        self.assertTrue(encryption_flow.secret_writes_blocked())

        # Door 1: the original key comes back. Nothing was written; healthy again.
        raw_before = raw
        _set_key(key)
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()), encryption_flow.HEALTHY)
        raw_after = asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'trakt_access_token'"))
        self.assertEqual(raw_before, raw_after)
        self._calendar_call_uses("op-token")

    def test_door_two_reset_after_a_lost_key_recovers_a_working_instance(self):
        _set_key(None)
        save_settings(Settings(trakt_client_id="client-id", trakt_access_token="op-token"))
        user_id = asyncio.run(auth.create_user(
            username="bob", password=None, settings=Settings()))

        def _link(conn):
            return auth.insert_linked_identity(
                conn, user_id=user_id, provider="trakt", provider_user_id="uuid-2",
                access_token="bobs-token", refresh_token="bobs-refresh",
            )

        identity_id = asyncio.run(db.transaction(_link))

        _set_key(KEY)
        asyncio.run(encryption_flow.encrypt_now())
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()), encryption_flow.HEALTHY)

        # The key is permanently lost; a new one is set instead.
        _set_key(OTHER_KEY)
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()), encryption_flow.KEY_MISMATCH)

        # Door 2: the destructive reset. The app secret and the identity's tokens
        # are unrecoverable under the new key and get blanked; the identity ROW
        # survives so the user can simply re-link.
        asyncio.run(encryption_flow.destructive_reset())
        self.assertEqual(encryption_flow.health(), encryption_flow.HEALTHY)
        self.assertIsNone(asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'trakt_access_token'")))
        row = asyncio.run(db.fetch_one(
            "SELECT access_token, refresh_token FROM linked_identities WHERE id = ?",
            (identity_id,)))
        self.assertIsNone(row["access_token"])
        self.assertIsNone(row["refresh_token"])
        self.assertIsNone(asyncio.run(trakt_routes.access_token_for_user(user_id)))

        # The operator re-enters the app-wide token; it seals under the current
        # (new) key and the calendar fetch path works again end to end.
        save_settings(Settings(trakt_client_id="client-id", trakt_access_token="fresh-op-token"))
        raw = asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'trakt_access_token'"))
        self.assertTrue(raw.startswith(secrets_box.PREFIX))
        self._calendar_call_uses("fresh-op-token")

        # The user re-links; their fresh token seals under the new key too.
        asyncio.run(auth.store_identity_tokens(
            identity_id, access_token="bobs-new-token",
            refresh_token="bobs-new-refresh", token_expires_at=None,
        ))
        self.assertEqual(asyncio.run(trakt_routes.access_token_for_user(user_id)), "bobs-new-token")


class StartupPlaintextWarningTests(EncryptionIntegrationTestCase):
    """The one-line startup warning (app.main._warn_on_key_state) has to catch
    the window a bare is_enabled() check misses: a key configured in the
    environment does not mean the rows already in the database are sealed —
    only running the backfill does that. A key present with an unsealed row
    still on disk must still warn."""

    def _warn(self) -> list[str]:
        with self.assertLogs("app.main", level="WARNING") as logged:
            asyncio.run(main._warn_on_key_state(encryption_flow.HEALTHY))
        return logged.output

    def test_no_key_and_a_plaintext_secret_warns(self):
        _set_key(None)
        save_settings(Settings(sonarr_api_key="plain-secret"))
        self.assertTrue(any("UNENCRYPTED" in m for m in self._warn()))

    def test_a_key_present_but_the_backfill_not_yet_run_still_warns(self):
        """The exact regression this covers: a valid key in the environment used
        to silence the warning outright, even though the secret written before
        the key existed is still plaintext on disk until the backfill runs."""
        _set_key(None)
        save_settings(Settings(sonarr_api_key="plain-secret"))
        _set_key(KEY)  # key now present; nothing has been backfilled yet
        self.assertTrue(any("UNENCRYPTED" in m for m in self._warn()))

    def test_once_actually_sealed_no_warning(self):
        _set_key(None)
        save_settings(Settings(sonarr_api_key="plain-secret"))
        _set_key(KEY)
        asyncio.run(encryption_flow.encrypt_now())
        with self.assertRaises(AssertionError):  # assertLogs raises if nothing logged
            self._warn()

    def test_no_secrets_at_all_no_warning(self):
        _set_key(None)
        with self.assertRaises(AssertionError):
            self._warn()


class NeedsResealTests(EncryptionIntegrationTestCase):
    """`phase` is a one-way ratchet (it flips to `encrypted` once and stays
    there), but a row can go back to plaintext afterward — most plausibly a
    relink or credential re-save landing while the key was briefly missing,
    now blocked by the guard in app.auth.link_provider_identity, but the
    signal has to exist independent of that guard too: it is what lets the
    Settings panel and the startup warning both tell a stale `encrypted` flag
    apart from an instance that is actually fully sealed."""

    def test_fully_sealed_instance_reports_no_reseal_needed(self):
        _set_key(KEY)
        save_settings(Settings(sonarr_api_key="secret"))
        asyncio.run(encryption_flow.encrypt_now())
        self.assertFalse(asyncio.run(secrets_backfill.unsealed_present()))

    def test_a_row_written_after_encrypting_while_keyless_is_detected(self):
        """The exact shape of the bug report this covers: an instance already
        fully encrypted, then a per-user token written while the key was
        absent (bypassing the guard directly, as a stand-in for "the guard
        didn't exist yet" / any other path that writes a token) leaves one row
        plaintext even though `phase` still reads `encrypted`."""
        _set_key(KEY)
        save_settings(Settings(sonarr_api_key="secret"))
        user_id = asyncio.run(auth.create_user(
            username="alice", password=None, settings=Settings()))

        def _link(conn):
            return auth.insert_linked_identity(
                conn, user_id=user_id, provider="trakt", provider_user_id="uuid-1",
                access_token="alice-token", refresh_token="alice-refresh",
            )

        identity_id = asyncio.run(db.transaction(_link))
        asyncio.run(encryption_flow.encrypt_now())
        self.assertEqual(asyncio.run(encryption_flow.get_phase()), encryption_flow.PHASE_ENCRYPTED)
        self.assertFalse(asyncio.run(secrets_backfill.unsealed_present()))

        # A token lands in the clear — the key was unavailable at write time,
        # bypassing seal() the same way a pre-guard relink did.
        _set_key(None)
        asyncio.run(auth.store_identity_tokens(
            identity_id, access_token="fresh-plaintext-token",
            refresh_token="fresh-plaintext-refresh", token_expires_at=None))
        _set_key(KEY)

        self.assertTrue(asyncio.run(secrets_backfill.unsealed_present()))
        # `encrypt_now` is idempotent and safe to run again — the same "Encrypt
        # secrets now" button the Settings panel re-offers when needs_reseal is
        # true — and it seals only what is actually still plaintext.
        asyncio.run(encryption_flow.encrypt_now())
        self.assertFalse(asyncio.run(secrets_backfill.unsealed_present()))
        raw = asyncio.run(db.fetch_value(
            "SELECT access_token FROM linked_identities WHERE id = ?", (identity_id,)))
        self.assertTrue(raw.startswith(secrets_box.PREFIX))
        self.assertEqual(asyncio.run(trakt_routes.access_token_for_user(user_id)),
                         "fresh-plaintext-token")

    def test_status_endpoint_reports_needs_reseal(self):
        from fastapi.testclient import TestClient
        from app.main import app

        _set_key(KEY)
        save_settings(Settings(sonarr_api_key="secret"))
        asyncio.run(encryption_flow.encrypt_now())

        client = TestClient(app, base_url="https://testserver",
                            headers={"Origin": "https://testserver"})
        try:
            resp = client.post("/onboarding", json={
                "username": "josh", "password": "hunter2hunter2",
                "password_confirm": "hunter2hunter2",
            })
            self.assertEqual(resp.status_code, 200, resp.text)
            status = client.get("/api/admin/encryption").json()
            self.assertFalse(status["needs_reseal"])

            _set_key(None)
            save_settings(Settings(radarr_api_key="a-fresh-radarr-key"))
            _set_key(KEY)
            status = client.get("/api/admin/encryption").json()
            self.assertTrue(status["needs_reseal"])
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
