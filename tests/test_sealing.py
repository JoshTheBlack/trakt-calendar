"""Sealing at the storage boundaries, and the seal-in-place backfill.

The crypto primitive is proven in isolation elsewhere (test_secrets_box). This
file proves the WIRING: that with a key configured every secret and per-user token
is ciphertext in the database and plaintext again at the point it is used, that the
app behaves identically with no key, and that the fail-open promise holds — a key
that is merely MISSING degrades to "unset" and never lets an automatic write blank
the intact ciphertext, while a WRONG key fails loud.

Three boundaries are covered:
  - the linked_identities token writers (app/auth) and the two read sites that feed
    a Trakt call (app/trakt_routes);
  - the app-level SECRET_FIELDS on the save/load path through app/config;
  - the reusable backfill that converts existing plaintext rows to sealed in place.

No network. TRAKT_DATA_DIR points at a temp dir, set BEFORE importing app modules.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_sealing -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-sealing-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from app import auth, config, db, secrets_backfill, secrets_box, trakt_routes  # noqa: E402
from app.config import Settings, load_settings, save_settings  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


class _KeyEnv:
    """Set (or clear) ENCRYPTION_KEY and make secrets_box re-read it, restoring the
    previous value and cache on exit. secrets_box caches the key, so a test that
    changes it must reset the cache for the change to be seen."""

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


class IdentityTokenSealingTests(unittest.IsolatedAsyncioTestCase):
    """A per-user Trakt token: ciphertext on the row, plaintext at the read sites."""

    _counter = 0

    async def asyncSetUp(self):
        IdentityTokenSealingTests._counter += 1
        db.set_db_path(TMP / f"sealing-identity-{IdentityTokenSealingTests._counter}.db")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()
        os.environ.pop(secrets_box.ENV_VAR, None)
        secrets_box.reset_cache()

    async def _make_identity(self, *, access="acc-token", refresh="ref-token",
                             expires=None) -> tuple[int, int]:
        user_id = await auth.create_user(
            username="alice", password=None, settings=Settings())

        def _work(conn):
            return auth.insert_linked_identity(
                conn, user_id=user_id, provider="trakt", provider_user_id="uuid-1",
                display_name="Alice", access_token=access, refresh_token=refresh,
                token_expires_at=expires,
            )

        identity_id = await db.transaction(_work)
        return user_id, identity_id

    async def _raw(self, identity_id: int, column: str):
        return await db.fetch_value(
            f"SELECT {column} FROM linked_identities WHERE id = ?", (identity_id,))

    async def test_tokens_are_ciphertext_in_the_db_and_plaintext_at_use(self):
        with _KeyEnv(KEY):
            user_id, identity_id = await self._make_identity()
            # The row holds sealed values — neither plaintext token appears verbatim.
            raw_access = await self._raw(identity_id, "access_token")
            raw_refresh = await self._raw(identity_id, "refresh_token")
            self.assertTrue(_sealed(raw_access))
            self.assertTrue(_sealed(raw_refresh))
            self.assertNotIn("acc-token", raw_access)
            self.assertNotIn("ref-token", raw_refresh)
            # Both read sites hand back the real token.
            self.assertEqual(await trakt_routes.access_token_for_user(user_id), "acc-token")
            self.assertEqual(await trakt_routes.stored_access_token(user_id), "acc-token")

    async def test_no_key_stores_and_serves_plaintext(self):
        with _KeyEnv(None):
            user_id, identity_id = await self._make_identity()
            self.assertEqual(await self._raw(identity_id, "access_token"), "acc-token")
            self.assertEqual(await trakt_routes.access_token_for_user(user_id), "acc-token")
            self.assertEqual(await trakt_routes.stored_access_token(user_id), "acc-token")

    async def test_a_null_token_stays_null_through_sealing(self):
        """A Plex-style link carries no Trakt token; sealing None must leave NULL."""
        with _KeyEnv(KEY):
            _, identity_id = await self._make_identity(access=None, refresh=None)
            self.assertIsNone(await self._raw(identity_id, "access_token"))
            self.assertIsNone(await self._raw(identity_id, "refresh_token"))

    async def test_sealed_token_with_no_key_reads_as_unset_and_is_not_overwritten(self):
        """Fail open: with the key gone the token reads as unset (never ciphertext),
        and the automatic read path must NOT blank the still-intact row."""
        with _KeyEnv(KEY):
            user_id, identity_id = await self._make_identity()
            sealed_before = await self._raw(identity_id, "access_token")
        with _KeyEnv(None):
            self.assertIsNone(await trakt_routes.access_token_for_user(user_id))
            self.assertIsNone(await trakt_routes.stored_access_token(user_id))
            # The ciphertext is untouched — it returns the moment the key is back.
            self.assertEqual(await self._raw(identity_id, "access_token"), sealed_before)
        with _KeyEnv(KEY):
            self.assertEqual(await trakt_routes.access_token_for_user(user_id), "acc-token")

    async def test_sealed_token_with_wrong_key_fails_loud(self):
        with _KeyEnv(KEY):
            user_id, _ = await self._make_identity()
        with _KeyEnv(OTHER_KEY):
            with self.assertRaises(secrets_box.SealedButWrongKey):
                await trakt_routes.access_token_for_user(user_id)
            with self.assertRaises(secrets_box.SealedButWrongKey):
                await trakt_routes.stored_access_token(user_id)


class AppSecretSealingTests(unittest.TestCase):
    """The SECRET_FIELDS values on the config save/load path."""

    _counter = 0

    def setUp(self):
        AppSecretSealingTests._counter += 1
        db.set_db_path(TMP / f"sealing-appsecret-{AppSecretSealingTests._counter}.db")
        asyncio.run(db.migrate())

    def tearDown(self):
        db.close_thread_connection()
        for name in ("settings.json",):
            try:
                (TMP / name).unlink()
            except OSError:
                pass
        try:
            config.SETTINGS_FILE.unlink()
        except OSError:
            pass
        os.environ.pop(secrets_box.ENV_VAR, None)
        secrets_box.reset_cache()

    def _raw_secret(self, name: str):
        return asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = ?", (name,)))

    def _file(self) -> dict:
        return json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))

    def test_secret_is_sealed_in_the_db_and_opened_on_load(self):
        with _KeyEnv(KEY):
            save_settings(Settings(sonarr_api_key="sonarr-secret",
                                   trakt_client_id="public-id"))
            raw = self._raw_secret("sonarr_api_key")
            self.assertTrue(_sealed(raw))
            self.assertNotIn("sonarr-secret", raw)
            # The non-secret global is NEVER sealed.
            globals_raw = asyncio.run(db.fetch_value(
                "SELECT value FROM app_settings WHERE name = 'app'"))
            self.assertIn("public-id", globals_raw)
            self.assertNotIn(secrets_box.PREFIX, globals_raw)
            # Round-trips back to plaintext transparently.
            self.assertEqual(load_settings().sonarr_api_key, "sonarr-secret")

    def test_no_key_keeps_the_prior_plaintext_behavior(self):
        with _KeyEnv(None):
            save_settings(Settings(sonarr_api_key="sonarr-secret"))
            self.assertEqual(self._raw_secret("sonarr_api_key"), "sonarr-secret")
            self.assertEqual(load_settings().sonarr_api_key, "sonarr-secret")

    def test_sealed_secret_with_no_key_loads_as_unset(self):
        """Fail open: a sealed app secret with the key gone reads as blank, so a
        provider is handed nothing rather than ciphertext."""
        with _KeyEnv(KEY):
            save_settings(Settings(sonarr_api_key="sonarr-secret"))
        with _KeyEnv(None):
            self.assertEqual(load_settings().sonarr_api_key, "")

    def test_sealed_secret_with_wrong_key_fails_loud_on_load(self):
        with _KeyEnv(KEY):
            save_settings(Settings(sonarr_api_key="sonarr-secret"))
        with _KeyEnv(OTHER_KEY):
            with self.assertRaises(secrets_box.SealedButWrongKey):
                load_settings()

    def test_automatic_file_reduction_never_blanks_a_sealed_secret(self):
        """The read-only-degradation guard: with the key missing, a hand-edit that
        would normally trigger the file-folding save must NOT run, or it would seal
        the blanked-out secret over the recoverable ciphertext."""
        with _KeyEnv(KEY):
            save_settings(Settings(sonarr_api_key="sonarr-secret",
                                   trakt_client_id="public-id"))
            load_settings()  # reduce the file to the recovery fields
            sealed_before = self._raw_secret("sonarr_api_key")
        with _KeyEnv(None):
            # Operator hand-adds a global back into the reduced file. Normally this
            # folds into the DB and re-reduces the file; while degraded it must not.
            reduced = self._file()
            reduced["trakt_client_id"] = "edited-id"
            config.SETTINGS_FILE.write_text(json.dumps(reduced), encoding="utf-8")
            load_settings()
            # The sealed secret row is byte-for-byte intact — nothing overwrote it.
            self.assertEqual(self._raw_secret("sonarr_api_key"), sealed_before)
            # And the hand-edit was not folded away, so it still applies for now.
            self.assertIn("trakt_client_id", self._file())
        # With the key back, everything reads correctly again and reduction resumes.
        with _KeyEnv(KEY):
            self.assertEqual(load_settings().sonarr_api_key, "sonarr-secret")


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    """The seal-in-place conversion the encryption opt-in runs."""

    _counter = 0

    async def asyncSetUp(self):
        BackfillTests._counter += 1
        db.set_db_path(TMP / f"sealing-backfill-{BackfillTests._counter}.db")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()
        try:
            config.SETTINGS_FILE.unlink()
        except OSError:
            pass
        os.environ.pop(secrets_box.ENV_VAR, None)
        secrets_box.reset_cache()

    async def _seed_plaintext(self) -> int:
        """A plaintext app secret and a plaintext identity token, as an instance
        that ran before encryption would hold them. Returns the identity id."""
        user_id = await auth.create_user(
            username="alice", password=None, settings=Settings())

        def _work(conn):
            conn.execute(
                "INSERT INTO app_secrets (name, value) VALUES ('sonarr_api_key', ?)",
                ("plain-sonarr",))
            return auth.insert_linked_identity(
                conn, user_id=user_id, provider="trakt", provider_user_id="uuid-1",
                access_token="plain-access", refresh_token="plain-refresh",
            )

        with _KeyEnv(None):  # seed unsealed regardless of ambient env
            return await db.transaction(_work)

    async def _raw_secret(self, name: str):
        return await db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = ?", (name,))

    async def _raw_token(self, identity_id: int, column: str):
        return await db.fetch_value(
            f"SELECT {column} FROM linked_identities WHERE id = ?", (identity_id,))

    async def test_backfill_seals_plaintext_rows_across_both_stores(self):
        identity_id = await self._seed_plaintext()
        with _KeyEnv(KEY):
            counts = await secrets_backfill.seal_plaintext_in_place()
            self.assertEqual(counts, {"app_secrets": 1, "identity_tokens": 1})
            self.assertTrue(_sealed(await self._raw_secret("sonarr_api_key")))
            self.assertTrue(_sealed(await self._raw_token(identity_id, "access_token")))
            self.assertTrue(_sealed(await self._raw_token(identity_id, "refresh_token")))
            # Values are preserved — read back through the ordinary paths.
            self.assertEqual(secrets_box.open_(await self._raw_secret("sonarr_api_key")),
                             "plain-sonarr")

    async def test_backfill_is_a_no_op_the_second_time(self):
        identity_id = await self._seed_plaintext()
        with _KeyEnv(KEY):
            await secrets_backfill.seal_plaintext_in_place()
            sealed_secret = await self._raw_secret("sonarr_api_key")
            sealed_access = await self._raw_token(identity_id, "access_token")
            counts = await secrets_backfill.seal_plaintext_in_place()
            self.assertEqual(counts, {"app_secrets": 0, "identity_tokens": 0})
            # Already-sealed rows are left byte-for-byte alone.
            self.assertEqual(await self._raw_secret("sonarr_api_key"), sealed_secret)
            self.assertEqual(await self._raw_token(identity_id, "access_token"), sealed_access)

    async def test_backfill_with_no_key_does_nothing(self):
        identity_id = await self._seed_plaintext()
        with _KeyEnv(None):
            counts = await secrets_backfill.seal_plaintext_in_place()
            self.assertEqual(counts, {"app_secrets": 0, "identity_tokens": 0})
            self.assertEqual(await self._raw_secret("sonarr_api_key"), "plain-sonarr")
            self.assertEqual(await self._raw_token(identity_id, "access_token"), "plain-access")


if __name__ == "__main__":
    unittest.main()
