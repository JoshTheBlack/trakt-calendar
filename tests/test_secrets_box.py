"""The at-rest crypto primitive and the configuration consolidation.

Two things are pinned here, matching the two pieces of the foundation:

  - app/secrets_box.py — the seal/open crypto with no DB and no wiring. The
    coexistence and fail-open/fail-loud rules are the correctness core: a sealed
    value with no key must read as unset (never as ciphertext), while a sealed
    value under the WRONG key must raise rather than be mistaken for unset.
  - The consolidation of settings.json into the DB — the numbered migration that
    copies the credentials into app_secrets and the non-secret globals into
    app_settings, and load_settings() reducing the file to only the two recovery
    fields. Each field class must survive a load/save round-trip through its new
    home, and the file must never re-accumulate a secret.

No key is wired into config here, so stored secrets are plaintext and behavior is
identical to before encryption existed. Sealing at the boundaries is proven
separately.

No network. TRAKT_DATA_DIR points at a temp dir, set BEFORE importing app modules.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_secrets_box -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-secrets-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from app import config, db, secrets_box  # noqa: E402
from app.config import RECOVERY_FIELDS, SECRET_FIELDS, Settings, load_settings, save_settings  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


class _KeyEnv:
    """Set (or clear) ENCRYPTION_KEY and make secrets_box re-read it, restoring the
    previous state and cache on exit. The key is read once and cached, so a test
    that changes it has to reset the cache to be seen."""

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


class SecretsBoxTests(unittest.TestCase):
    """The crypto in isolation — no DB, no app wiring."""

    def tearDown(self):
        # Leave no key set for the next test regardless of what this one did.
        os.environ.pop(secrets_box.ENV_VAR, None)
        secrets_box.reset_cache()

    def test_seal_then_open_round_trips_under_a_key(self):
        with _KeyEnv(KEY):
            self.assertTrue(secrets_box.is_enabled())
            sealed = secrets_box.seal("s3cr3t-token")
            self.assertTrue(sealed.startswith(secrets_box.PREFIX))
            self.assertNotIn("s3cr3t-token", sealed)
            self.assertEqual(secrets_box.open_(sealed), "s3cr3t-token")

    def test_open_of_legacy_plaintext_is_unchanged(self):
        """A value with no prefix predates encryption; it must come back verbatim
        whether or not a key is set, so sealed and plaintext rows coexist."""
        with _KeyEnv(KEY):
            self.assertEqual(secrets_box.open_("legacy-plaintext"), "legacy-plaintext")
        with _KeyEnv(None):
            self.assertEqual(secrets_box.open_("legacy-plaintext"), "legacy-plaintext")

    def test_no_key_makes_seal_and_open_pass_throughs(self):
        with _KeyEnv(None):
            self.assertFalse(secrets_box.is_enabled())
            self.assertEqual(secrets_box.seal("plain"), "plain")
            self.assertEqual(secrets_box.open_("plain"), "plain")

    def test_sealed_value_with_no_key_reads_as_unset_never_ciphertext(self):
        """The fail-open case: the key was removed. The value must read as unset so
        the app degrades to re-link/re-enter, and the ciphertext must never leak
        out to be shipped to a provider."""
        with _KeyEnv(KEY):
            sealed = secrets_box.seal("do-not-leak")
        with _KeyEnv(None):
            self.assertIsNone(secrets_box.open_(sealed))
            self.assertNotEqual(secrets_box.open_(sealed), sealed)

    def test_sealed_value_with_wrong_key_fails_loud(self):
        """The fail-loud case: a rotated or mistyped key must raise, never be taken
        for 'unset' (which would invite an overwrite of a still-recoverable value)
        and never return garbage."""
        with _KeyEnv(KEY):
            sealed = secrets_box.seal("real-value")
        with _KeyEnv(OTHER_KEY):
            with self.assertRaises(secrets_box.SealedButWrongKey):
                secrets_box.open_(sealed)

    def test_none_stays_none_through_seal_and_open(self):
        with _KeyEnv(KEY):
            self.assertIsNone(secrets_box.seal(None))
            self.assertIsNone(secrets_box.open_(None))
        with _KeyEnv(None):
            self.assertIsNone(secrets_box.seal(None))
            self.assertIsNone(secrets_box.open_(None))

    def test_empty_string_is_kept_distinct_from_none(self):
        """"" is a real stored value; it must not collapse to None on the way in or
        out, sealed or not."""
        with _KeyEnv(KEY):
            sealed = secrets_box.seal("")
            self.assertIsNotNone(sealed)
            self.assertEqual(secrets_box.open_(sealed), "")
        with _KeyEnv(None):
            self.assertEqual(secrets_box.seal(""), "")
            self.assertEqual(secrets_box.open_(""), "")

    def test_key_is_valid_accepts_a_real_key_and_rejects_a_bad_one(self):
        self.assertTrue(secrets_box.key_is_valid(KEY))
        self.assertTrue(secrets_box.key_is_valid(Fernet.generate_key()))  # bytes form
        self.assertFalse(secrets_box.key_is_valid("not-a-real-key"))
        self.assertFalse(secrets_box.key_is_valid(""))
        self.assertFalse(secrets_box.key_is_valid("short"))

    def test_a_malformed_env_key_fails_fast_rather_than_reading_as_no_key(self):
        """A truncated or mistyped key is an error, not the opt-out: it must raise
        so the operator fixes it instead of silently storing everything plaintext
        under a key they believe is protecting them."""
        with _KeyEnv("clearly-not-a-fernet-key"):
            with self.assertRaises(secrets_box.InvalidKeyError):
                secrets_box.is_enabled()
            with self.assertRaises(secrets_box.InvalidKeyError):
                secrets_box.seal("x")

    def test_a_blank_env_key_is_the_opt_out_not_an_error(self):
        with _KeyEnv("   "):
            self.assertFalse(secrets_box.is_enabled())
            self.assertEqual(secrets_box.seal("x"), "x")

    def test_plaintext_storage_warning_reflects_what_the_caller_found_unsealed(self):
        """Independent of is_enabled(): the caller inspects the real rows for the
        `enc:v1:` prefix (see app.main._warn_on_key_state), including the window
        where a key is configured but the backfill hasn't sealed old rows yet —
        so a key being present must NOT silence this on its own."""
        self.assertIsNotNone(secrets_box.plaintext_storage_warning(True))
        self.assertIsNone(secrets_box.plaintext_storage_warning(False))
        with _KeyEnv(KEY):
            self.assertIsNotNone(secrets_box.plaintext_storage_warning(True))
            self.assertIsNone(secrets_box.plaintext_storage_warning(False))


# A distinctive value per secret so a leak into the wrong store is unmistakable.
SECRET_VALUES = {name: f"SEKRIT-{name}" for name in SECRET_FIELDS}


class ConsolidationTests(unittest.TestCase):
    """settings.json splits into three homes, with no key wired in (plaintext)."""

    _counter = 0

    def setUp(self):
        ConsolidationTests._counter += 1
        db.set_db_path(TMP / f"consolidation-{ConsolidationTests._counter}.db")
        # A pre-consolidation settings.json holding one of everything, so the split
        # can be checked per class. Written BEFORE migrate so the migration sees it.
        self._full = Settings(
            **SECRET_VALUES,
            trakt_client_id="public-client-id",     # a non-secret global
            genres="-anime",                         # a content-floor field
            network_filter=["HBO", "AMC"],           # a non-secret global (list)
            cookie_secure="never",                   # a recovery field
            allow_open_registration=True,            # a recovery field
        )
        config.SETTINGS_FILE.write_text(
            json.dumps(self._full.to_dict(), indent=2), encoding="utf-8")
        asyncio.run(db.migrate())

    def tearDown(self):
        db.close_thread_connection()
        try:
            config.SETTINGS_FILE.unlink()
        except OSError:
            pass

    def _file_now(self) -> dict:
        return json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))

    def _secret_row(self, name: str):
        return asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = ?", (name,)))

    def _globals_row(self) -> dict:
        raw = asyncio.run(db.fetch_value("SELECT value FROM app_settings WHERE name = 'app'"))
        return json.loads(raw)

    def test_migration_moves_secrets_and_globals_into_their_stores(self):
        # Secrets land in app_secrets, plaintext (no key wired in this build).
        for name, value in SECRET_VALUES.items():
            with self.subTest(secret=name):
                self.assertEqual(self._secret_row(name), value)
        # Globals and the content-floor fields land in app_settings; secrets do not.
        globals_doc = self._globals_row()
        self.assertEqual(globals_doc["trakt_client_id"], "public-client-id")
        self.assertEqual(globals_doc["genres"], "-anime")
        self.assertEqual(globals_doc["network_filter"], ["HBO", "AMC"])
        for name in SECRET_FIELDS:
            self.assertNotIn(name, globals_doc)
        # The recovery fields belong to the file, not the globals store.
        for name in RECOVERY_FIELDS:
            self.assertNotIn(name, globals_doc)

    def test_migration_is_idempotent(self):
        before_secret = self._secret_row("tmdb_api_key")
        before_globals = self._globals_row()
        asyncio.run(db.migrate())
        asyncio.run(db.migrate())
        self.assertEqual(self._secret_row("tmdb_api_key"), before_secret)
        self.assertEqual(self._globals_row(), before_globals)

    def test_load_reduces_the_file_to_exactly_the_recovery_fields(self):
        # The migration copies but does not shrink the file; the first load does.
        loaded = load_settings()
        self.assertEqual(set(self._file_now()), set(RECOVERY_FIELDS))
        # Nothing else lingers in the file — no secret and no global.
        for name in SECRET_VALUES.values():
            self.assertNotIn(name, json.dumps(self._file_now()))
        self.assertNotIn("public-client-id", json.dumps(self._file_now()))
        # And the recovery values are the ones that were set.
        self.assertEqual(self._file_now()["cookie_secure"], "never")
        self.assertIs(self._file_now()["allow_open_registration"], True)
        # The assembled object is unchanged from what was stored.
        self.assertEqual(loaded.trakt_client_id, "public-client-id")
        self.assertEqual(loaded.tmdb_api_key, SECRET_VALUES["tmdb_api_key"])

    def test_reducing_the_file_is_a_no_op_the_second_time(self):
        load_settings()
        first = self._file_now()
        load_settings()
        self.assertEqual(self._file_now(), first)

    def test_each_field_class_survives_a_save_then_load_round_trip(self):
        load_settings()  # reduce first
        updated = Settings(
            trakt_client_id="new-public-id",       # global -> app_settings
            sonarr_api_key="new-sonarr-key",       # secret -> app_secrets
            cookie_secure="auto",                  # recovery -> settings.json
        )
        save_settings(updated)
        again = load_settings()
        self.assertEqual(again.trakt_client_id, "new-public-id")
        self.assertEqual(again.sonarr_api_key, "new-sonarr-key")
        self.assertEqual(again.cookie_secure, "auto")
        # Confirm each value physically lives in its own home.
        self.assertEqual(self._secret_row("sonarr_api_key"), "new-sonarr-key")
        self.assertEqual(self._globals_row()["trakt_client_id"], "new-public-id")
        self.assertEqual(self._file_now()["cookie_secure"], "auto")

    def test_no_key_means_secrets_are_stored_plaintext(self):
        """With no ENCRYPTION_KEY, the store holds the value verbatim — the
        pre-encryption behavior, byte for byte."""
        self.assertFalse(secrets_box.is_enabled())
        self.assertEqual(self._secret_row("trakt_access_token"),
                         SECRET_VALUES["trakt_access_token"])
        self.assertFalse(str(self._secret_row("trakt_access_token"))
                         .startswith(secrets_box.PREFIX))

    def test_a_hand_added_secret_is_folded_into_the_db_and_blanked_from_the_file(self):
        """An operator who edits a secret back into the reduced file must have it
        migrate into app_secrets and disappear from the file on next load, so the
        file never re-accumulates a plaintext credential."""
        load_settings()  # reduce to recovery-only
        reduced = self._file_now()
        reduced["seer_api_key"] = "hand-added-key"
        config.SETTINGS_FILE.write_text(json.dumps(reduced), encoding="utf-8")

        loaded = load_settings()
        self.assertEqual(loaded.seer_api_key, "hand-added-key")
        self.assertEqual(self._secret_row("seer_api_key"), "hand-added-key")
        self.assertEqual(set(self._file_now()), set(RECOVERY_FIELDS))
        self.assertNotIn("hand-added-key", json.dumps(self._file_now()))


if __name__ == "__main__":
    unittest.main()
