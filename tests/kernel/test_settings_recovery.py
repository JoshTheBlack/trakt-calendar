"""Recovering an instance nobody can sign in to, without losing its credentials.

Two halves of one failure. `public_base_url` is required before "Continue with
Trakt" is offered, and the only screen that can set it is behind that sign-in —
so an instance without one has to be rescued from outside. The rescue that
existed was hand-editing data/settings.json, and that path silently deleted every
stored credential: almost every request loads settings WITHOUT opening the sealed
secrets (the request-shape guard, sign-in, the session lookup all do), and the
save that folds a hand-edited key into the database was handed that
secret-less Settings and treated each blank field as an instruction to delete.

So: an absent secret must never remove a stored one, an explicit clear must still
clear, and there must be a way in that does not involve editing the file at all.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import config
from app.config import PUBLIC_BASE_URL_ENV, Settings, load_settings, save_settings
from tests.support import ORIGIN, PASSWORD, AppTestCase

STORED_SECRETS = {
    "trakt_client_secret": "client-secret-value",
    "trakt_access_token": "access-token-value",
    "sonarr_api_key": "sonarr-key-value",
}


class SettingsFileTestCase(AppTestCase):
    """A per-test settings.json, so a hand-edit here is invisible to the rest of
    the suite — every test in the process otherwise shares the one data
    directory config.DATA_DIR was bound to at import."""

    def make_settings(self):
        return Settings(public_base_url=ORIGIN)

    def setUp(self):
        super().setUp()
        settings_file = self.db_path.with_name(f"{self.db_path.stem}-settings.json")
        patcher = patch.object(config, "SETTINGS_FILE", settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings_file = settings_file

    def store_credentials(self) -> None:
        save_settings(Settings(public_base_url=ORIGIN, **STORED_SECRETS))

    def hand_edit(self, **values) -> None:
        """Write settings.json the way an operator locked out of the UI would."""
        self.settings_file.write_text(json.dumps(values, indent=2), encoding="utf-8")


class HandEditedSettingsFileTests(SettingsFileTestCase):
    def setUp(self):
        super().setUp()
        self.store_credentials()

    def assert_credentials_intact(self) -> None:
        stored = load_settings()
        for name, value in STORED_SECRETS.items():
            self.assertEqual(getattr(stored, name), value, name)

    def test_a_key_absent_from_the_file_does_not_delete_a_stored_secret(self):
        """The file has never held the credentials — they live in app_secrets —
        so their absence from it says nothing about what the operator wants."""
        self.hand_edit(public_base_url="https://recovered.example.com")
        self.assertEqual(load_settings().public_base_url, "https://recovered.example.com")
        self.assert_credentials_intact()

    def test_a_load_that_never_opens_the_secrets_does_not_delete_them(self):
        """The path that actually destroyed them. open_secrets=False leaves every
        credential field blank on purpose, and this load still folds the
        hand-edited key into the database — so it is holding a Settings whose
        blanks mean "not read", not "cleared"."""
        self.hand_edit(public_base_url="https://recovered.example.com")
        load_settings(open_secrets=False)
        self.assert_credentials_intact()

    def test_signing_in_after_a_hand_edit_leaves_the_credentials_alone(self):
        """End to end, in the order it was hit: the operator added the missing
        setting by hand, restarted, and signed in — and it was the sign-in that
        wiped them, because sign-in deliberately reads settings without opening
        the sealed secrets."""
        self.hand_edit(public_base_url=ORIGIN)
        self.make_user("someone", calendar_approved=True)
        resp = self.client.post("/login", json={"username": "someone",
                                                "password": PASSWORD})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assert_credentials_intact()

    def test_the_hand_edited_value_still_reaches_the_database_and_leaves_the_file(self):
        """The recovery path itself has to keep working: a database-owned key
        added by hand is adopted and then dropped from the file, so the file
        never re-accumulates configuration."""
        self.hand_edit(public_base_url="https://recovered.example.com",
                       allow_open_registration=True)
        load_settings()
        remaining = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertNotIn("public_base_url", remaining)
        self.assertIs(remaining["allow_open_registration"], True)
        self.assertEqual(load_settings().public_base_url, "https://recovered.example.com")


class SecretClearingTests(SettingsFileTestCase):
    """The behaviour that must survive the fix: an operator emptying a credential
    on the Settings screen still unsets it."""

    def setUp(self):
        super().setUp()
        self.store_credentials()
        self.sign_in_as(self.make_user("admin_user", is_admin=True, calendar_approved=True))

    def test_an_explicit_null_from_the_settings_screen_clears_one_secret(self):
        resp = self.client.post("/api/settings", json={"trakt_access_token": None})
        self.assertEqual(resp.status_code, 200, resp.text)
        stored = load_settings()
        self.assertEqual(stored.trakt_access_token, "")
        # Only the one that was asked for: the others were not in the payload.
        self.assertEqual(stored.trakt_client_secret, STORED_SECRETS["trakt_client_secret"])
        self.assertEqual(stored.sonarr_api_key, STORED_SECRETS["sonarr_api_key"])

    def test_a_cleared_secret_leaves_no_row_behind(self):
        """`secrets_set` is derived from the value, so a row holding "" would
        report the credential as still set."""
        self.client.post("/api/settings", json={"trakt_access_token": None})
        payload = self.client.get("/api/settings").json()
        self.assertIs(payload["secrets_set"]["trakt_access_token"], False)
        self.assertIs(payload["secrets_set"]["trakt_client_secret"], True)

    def test_a_blank_field_still_means_leave_it_alone(self):
        resp = self.client.post("/api/settings", json={"trakt_access_token": ""})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(load_settings().trakt_access_token,
                         STORED_SECRETS["trakt_access_token"])

    def test_the_settings_endpoint_still_returns_no_credential_values(self):
        """Credentials are write-only over this API — only a flag saying whether
        each one has a value."""
        body = self.client.get("/api/settings").text
        for value in STORED_SECRETS.values():
            self.assertNotIn(value, body)

    def test_save_settings_leaves_a_blank_secret_alone_unless_asked(self):
        """The unit-level rule, stated where a future caller will meet it: a
        Settings object cannot tell "never loaded" from "emptied on purpose", so
        the destructive reading is the one that has to be requested."""
        save_settings(Settings(public_base_url=ORIGIN))
        self.assertEqual(load_settings().trakt_access_token,
                         STORED_SECRETS["trakt_access_token"])

        save_settings(Settings(public_base_url=ORIGIN), clear_unset_secrets=True)
        self.assertEqual(load_settings().trakt_access_token, "")


class PublicBaseUrlBootstrapTests(SettingsFileTestCase):
    """The way back into an instance that cannot be signed into, which is what
    the hand-editing was for in the first place."""

    def make_settings(self):
        return Settings(trakt_client_id="client-id", trakt_client_secret="client-secret")

    def with_env(self, value: str):
        return patch.dict("os.environ", {PUBLIC_BASE_URL_ENV: value})

    def test_the_environment_variable_supplies_a_missing_base_url(self):
        with self.with_env("https://shows.example.com"):
            self.assertEqual(load_settings().public_base_url, "https://shows.example.com")

    def test_it_turns_provider_sign_in_back_on(self):
        """The whole point: the login page can only offer Trakt once a base URL
        exists, and that page is what the locked-out operator is looking at."""
        self.make_user("someone", calendar_approved=True)
        self.assertNotIn("/auth/trakt/start", self.client.get("/login").text)
        with self.with_env("https://shows.example.com"):
            self.assertIn("/auth/trakt/start", self.client.get("/login").text)

    def test_a_stored_value_wins(self):
        save_settings(Settings(public_base_url="https://stored.example.com"))
        with self.with_env("https://shows.example.com"):
            self.assertEqual(load_settings().public_base_url, "https://stored.example.com")

    def test_it_is_not_written_to_storage(self):
        """A fallback, not a migration: removing the variable removes the
        override, rather than leaving a value nobody can account for."""
        with self.with_env("https://shows.example.com"):
            load_settings()
        self.assertEqual(load_settings().public_base_url, "")

    def test_a_trailing_slash_is_tolerated(self):
        with self.with_env("https://shows.example.com/"):
            self.assertEqual(load_settings().public_base_url, "https://shows.example.com")

    def test_an_unusable_value_is_ignored_rather_than_fatal(self):
        """This exists to rescue an instance, so it must never be the reason one
        will not start — and a value with a path in it would build a redirect URI
        Trakt refuses byte for byte."""
        for bad in ("shows.example.com", "https://shows.example.com/app", "not a url"):
            with self.subTest(value=bad), self.with_env(bad):
                self.assertEqual(load_settings().public_base_url, "")

    def test_it_survives_a_first_run_with_nothing_persisted_anywhere(self):
        """The genuinely empty instance takes an earlier branch out of
        load_settings than every other test here."""
        from app import db

        db.connection().execute("DELETE FROM app_settings")
        db.connection().commit()
        with self.with_env("https://shows.example.com"):
            self.assertEqual(load_settings().public_base_url, "https://shows.example.com")


if __name__ == "__main__":
    unittest.main()
