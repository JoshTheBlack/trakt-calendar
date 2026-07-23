"""The admin encryption lifecycle: the enable-flow phase machine, the key-health
canary, the lost-key recovery reset, and the unhealthy-write guard.

The crypto and the storage boundaries are proven elsewhere (test_secrets_box,
test_sealing). This file proves the LIFECYCLE on top of them: that the phase
advances the way the consent flow drives it, that the canary tells the three key
states apart, that the destructive reset blanks only what the current key cannot
open (keeping identity rows), and that a secret write is refused while the key is
unhealthy.

No network. TRAKT_DATA_DIR points at a temp dir, set BEFORE importing app modules.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_encryption_flow -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-encflow-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from app import config, db, encryption_flow, secrets_box  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


class _KeyEnv:
    """Set (or clear) ENCRYPTION_KEY and make secrets_box re-read it, restoring the
    previous value and cache on exit."""

    def __init__(self, key: str | None):
        self._key = key

    def __enter__(self):
        self._prev = os.environ.get(secrets_box.ENV_VAR)
        if self._key is None:
            os.environ.pop(secrets_box.ENV_VAR, None)
        else:
            os.environ[secrets_box.ENV_VAR] = self._key
        secrets_box.reset_cache()
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop(secrets_box.ENV_VAR, None)
        else:
            os.environ[secrets_box.ENV_VAR] = self._prev
        secrets_box.reset_cache()


def _sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(secrets_box.PREFIX)


class EncryptionFlowTests(unittest.IsolatedAsyncioTestCase):
    _counter = 0

    async def asyncSetUp(self):
        EncryptionFlowTests._counter += 1
        db.set_db_path(TMP / f"encflow-{EncryptionFlowTests._counter}.db")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()
        try:
            config.SETTINGS_FILE.unlink()
        except OSError:
            pass
        os.environ.pop(secrets_box.ENV_VAR, None)
        os.environ.pop(encryption_flow.ENV_ESCAPE_HATCH, None)
        secrets_box.reset_cache()
        # Module-level derived health is shared across the process; leave it at the
        # healthy default so an unrelated test is never treated as key-mismatched.
        encryption_flow._health = encryption_flow.HEALTHY

    async def _raw_secret(self, name: str):
        return await db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = ?", (name,))

    async def _canary(self):
        return await db.get_meta(encryption_flow.CANARY_KEY)

    # -- phase machine -------------------------------------------------------

    async def test_phase_progresses_none_pending_key_pending_encrypt_encrypted(self):
        self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_NONE)
        with _KeyEnv(None):
            # Chose to enable with no key yet: a key is generated to reveal and a
            # restart is required; nothing is encrypted.
            result = await encryption_flow.begin_enable(generate=True)
            self.assertTrue(result["restart_required"])
            self.assertTrue(secrets_box.key_is_valid(result["key"]))
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_PENDING_KEY)
        with _KeyEnv(KEY):
            self.assertTrue(await encryption_flow.verify_key())
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_PENDING_ENCRYPT)
            await encryption_flow.encrypt_now()
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_ENCRYPTED)
            self.assertTrue(_sealed(await self._canary()))

    async def test_verify_key_fails_when_no_key_arrived_and_phase_holds(self):
        with _KeyEnv(None):
            await encryption_flow.begin_enable(generate=False)
            self.assertFalse(await encryption_flow.verify_key())
            # Still waiting — nothing advanced, so the modal can re-offer the opt-out.
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_PENDING_KEY)

    async def test_opt_out_then_later_opt_in_with_key_present(self):
        await encryption_flow.opt_out()
        self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_OPTED_OUT)
        # Later opt-in from Settings while a valid key is already in the environment:
        # no restart, straight to the confirm step, then encrypt.
        with _KeyEnv(KEY):
            save_settings(Settings(sonarr_api_key="sonarr-secret"))
            result = await encryption_flow.begin_enable(generate=False)
            self.assertFalse(result["restart_required"])
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_PENDING_ENCRYPT)
            await encryption_flow.encrypt_now()
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_ENCRYPTED)
            self.assertTrue(_sealed(await self._raw_secret("sonarr_api_key")))

    async def test_encrypt_now_refuses_without_a_valid_key(self):
        with _KeyEnv(None):
            with self.assertRaises(RuntimeError):
                await encryption_flow.encrypt_now()

    async def test_env_escape_hatch_encrypts_at_startup(self):
        with _KeyEnv(KEY):
            save_settings(Settings(sonarr_api_key="sonarr-secret"))
            os.environ[encryption_flow.ENV_ESCAPE_HATCH] = "1"
            self.assertTrue(await encryption_flow.run_env_escape_hatch())
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_ENCRYPTED)
            self.assertTrue(_sealed(await self._raw_secret("sonarr_api_key")))
            # Idempotent: a second startup with the flag set is a no-op (already on).
            self.assertFalse(await encryption_flow.run_env_escape_hatch())

    async def test_env_escape_hatch_needs_a_key(self):
        with _KeyEnv(None):
            os.environ[encryption_flow.ENV_ESCAPE_HATCH] = "1"
            self.assertFalse(await encryption_flow.run_env_escape_hatch())

    # -- canary health -------------------------------------------------------

    async def _encrypt_a_secret_under(self, key: str):
        with _KeyEnv(key):
            save_settings(Settings(sonarr_api_key="sonarr-secret"))
            await encryption_flow.encrypt_now()

    async def test_canary_derives_healthy_missing_and_mismatch(self):
        await self._encrypt_a_secret_under(KEY)
        with _KeyEnv(KEY):
            self.assertEqual(await encryption_flow.refresh_health(), encryption_flow.HEALTHY)
        with _KeyEnv(None):
            # Sealed but no key: degraded/fail-open, NOT a mismatch.
            self.assertEqual(await encryption_flow.refresh_health(), encryption_flow.KEY_MISSING)
        with _KeyEnv(OTHER_KEY):
            self.assertEqual(await encryption_flow.refresh_health(), encryption_flow.KEY_MISMATCH)

    async def test_no_canary_is_healthy(self):
        # An instance that never enabled encryption has no canary and is healthy.
        with _KeyEnv(None):
            self.assertEqual(await encryption_flow.refresh_health(), encryption_flow.HEALTHY)

    # -- door 1: original key restored, nothing written ----------------------

    async def test_door_one_restore_key_writes_nothing(self):
        await self._encrypt_a_secret_under(KEY)
        sealed_secret = await self._raw_secret("sonarr_api_key")
        sealed_canary = await self._canary()
        with _KeyEnv(OTHER_KEY):
            self.assertEqual(await encryption_flow.refresh_health(), encryption_flow.KEY_MISMATCH)
        # Door 1 is simply putting the original key back and restarting — the health
        # re-derives to healthy and NOT ONE byte was written.
        with _KeyEnv(KEY):
            self.assertEqual(await encryption_flow.refresh_health(), encryption_flow.HEALTHY)
        self.assertEqual(await self._raw_secret("sonarr_api_key"), sealed_secret)
        self.assertEqual(await self._canary(), sealed_canary)
        self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_ENCRYPTED)

    # -- door 2: destructive reset -------------------------------------------

    async def _seed_identity_token_under(self, key: str) -> int:
        with _KeyEnv(key):
            user_id = await _make_user()

            def _work(conn):
                conn.execute(
                    "INSERT INTO linked_identities "
                    "(user_id, provider, provider_user_id, access_token, refresh_token, created_at) "
                    "VALUES (?, 'trakt', 'uuid-1', ?, ?, strftime('%s','now'))",
                    (user_id, secrets_box.seal("acc-token"), secrets_box.seal("ref-token")),
                )
                return conn.execute(
                    "SELECT id FROM linked_identities WHERE user_id = ?", (user_id,)
                ).fetchone()["id"]

            return await db.transaction(_work)

    async def test_door_two_blanks_unrecoverable_keeps_recoverable_and_rows(self):
        # Encrypted originally under KEY: a secret and an identity token sealed under
        # it are unrecoverable once OTHER_KEY is the current key.
        await self._encrypt_a_secret_under(KEY)
        identity_id = await self._seed_identity_token_under(KEY)
        # A value that genuinely belongs to the CURRENT key (OTHER_KEY): it must be
        # left intact by the reset.
        with _KeyEnv(OTHER_KEY):
            await db.execute(
                "INSERT INTO app_secrets (name, value) VALUES ('tmdb_api_key', ?)",
                (secrets_box.seal("belongs-to-new-key"),))
            self.assertEqual(await encryption_flow.refresh_health(), encryption_flow.KEY_MISMATCH)
            result = await encryption_flow.destructive_reset()

            self.assertEqual(result["app_secrets"], 1)      # only the KEY-sealed one
            self.assertEqual(result["identity_tokens"], 1)
            # Unrecoverable app secret is gone (unset == no row).
            self.assertIsNone(await self._raw_secret("sonarr_api_key"))
            # The value sealed under the current key survived untouched.
            self.assertEqual(secrets_box.open_(await self._raw_secret("tmdb_api_key")),
                             "belongs-to-new-key")
            # The identity ROW survives; its unreadable tokens are cleared to NULL so
            # it fails open to a clean re-link.
            row = await db.fetch_one(
                "SELECT access_token, refresh_token FROM linked_identities WHERE id = ?",
                (identity_id,))
            self.assertIsNotNone(row)
            self.assertIsNone(row["access_token"])
            self.assertIsNone(row["refresh_token"])
            # Re-keyed and healthy under the current key.
            self.assertEqual(await encryption_flow.get_phase(), encryption_flow.PHASE_ENCRYPTED)
            self.assertEqual(secrets_box.open_(await self._canary()),
                             encryption_flow.CANARY_PLAINTEXT)
            self.assertEqual(encryption_flow.health(), encryption_flow.HEALTHY)

    # -- the unhealthy-write guard -------------------------------------------

    async def test_secret_writes_blocked_while_unhealthy_but_not_when_healthy(self):
        await self._encrypt_a_secret_under(KEY)
        with _KeyEnv(KEY):
            await encryption_flow.refresh_health()
            self.assertFalse(encryption_flow.secret_writes_blocked())
        with _KeyEnv(None):
            await encryption_flow.refresh_health()
            self.assertTrue(encryption_flow.secret_writes_blocked())
        with _KeyEnv(OTHER_KEY):
            await encryption_flow.refresh_health()
            self.assertTrue(encryption_flow.secret_writes_blocked())

    async def test_save_settings_leaves_sealed_secret_intact_while_key_missing(self):
        await self._encrypt_a_secret_under(KEY)
        sealed_before = await self._raw_secret("sonarr_api_key")
        with _KeyEnv(None):
            await encryption_flow.refresh_health()
            # A save that carries a fresh credential must NOT overwrite the sealed row
            # while the key is missing — but its non-secret globals still persist.
            save_settings(Settings(sonarr_api_key="new-plaintext", trakt_client_id="public-id"))
            self.assertEqual(await self._raw_secret("sonarr_api_key"), sealed_before)
            globals_raw = await db.fetch_value(
                "SELECT value FROM app_settings WHERE name = 'app'")
            self.assertIn("public-id", globals_raw)


async def _make_user() -> int:
    from app import auth
    return await auth.create_user(username="alice", password=None, settings=Settings())


if __name__ == "__main__":
    unittest.main()
