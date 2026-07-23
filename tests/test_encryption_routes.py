"""The encryption lifecycle through the HTTP surface: the admin consent endpoints,
the request gate that steers a wrong-key instance to the recovery screen, and the
recovery reset door.

The service layer is proven in test_encryption_flow; this file proves the WIRING —
that the consent endpoints drive the phase, that a derived key mismatch redirects a
browser to recovery and refuses the ordinary admin API, that the reset door needs
its typed phrase, and that the admin credential save is refused while the key is
unhealthy but a non-secret save is not.

No network. TRAKT_DATA_DIR points at a temp dir, set BEFORE importing app modules.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_encryption_routes -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-encroutes-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, encryption_flow, secrets_box  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.encryption_routes import RECOVERY_PATH, RESET_CONFIRM_PHRASE  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()
HTML = {"Accept": "text/html"}


def _set_key(key: str | None) -> None:
    if key is None:
        os.environ.pop(secrets_box.ENV_VAR, None)
    else:
        os.environ[secrets_box.ENV_VAR] = key
    secrets_box.reset_cache()


class EncryptionRouteTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        EncryptionRouteTestCase._counter += 1
        db.set_db_path(TMP / f"encroutes-{EncryptionRouteTestCase._counter}.db")
        asyncio.run(db.migrate())
        _set_key(None)
        save_settings(Settings())
        self.client = TestClient(app, base_url="https://testserver",
                                 headers={"Origin": "https://testserver"})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()
        _set_key(None)
        try:
            config.SETTINGS_FILE.unlink()
        except OSError:
            pass
        # Shared module state — leave the process healthy for unrelated tests.
        encryption_flow._health = encryption_flow.HEALTHY

    def _onboard(self):
        resp = self.client.post("/onboarding", json={
            "username": "josh", "password": "hunter2hunter2",
            "password_confirm": "hunter2hunter2",
        })
        self.assertEqual(resp.status_code, 200, resp.text)

    def _encrypt_under(self, key: str, *, sonarr="sonarr-secret"):
        """Seed a plaintext secret and turn encryption on under `key`, as an instance
        that enabled it would end up. Leaves the environment holding `key`."""
        _set_key(None)
        save_settings(Settings(sonarr_api_key=sonarr))
        _set_key(key)
        asyncio.run(encryption_flow.encrypt_now())

    # -- consent flow --------------------------------------------------------

    def test_consent_flow_generate_verify_encrypt(self):
        self._onboard()
        # A plaintext secret exists to be sealed by the flow.
        _set_key(None)
        save_settings(Settings(sonarr_api_key="sonarr-secret", trakt_client_id="pub"))

        status = self.client.get("/api/admin/encryption").json()
        self.assertEqual(status["phase"], encryption_flow.PHASE_NONE)

        enable = self.client.post("/api/admin/encryption/enable", json={"generate": True}).json()
        self.assertTrue(enable["restart_required"])
        self.assertTrue(secrets_box.key_is_valid(enable["key"]))
        self.assertEqual(enable["phase"], encryption_flow.PHASE_PENDING_KEY)

        # Operator saves the revealed key to the environment and restarts.
        _set_key(enable["key"])
        verify = self.client.post("/api/admin/encryption/verify", json={}).json()
        self.assertTrue(verify["detected"])
        self.assertEqual(verify["phase"], encryption_flow.PHASE_PENDING_ENCRYPT)

        encrypt = self.client.post("/api/admin/encryption/encrypt", json={}).json()
        self.assertTrue(encrypt["ok"])
        self.assertEqual(encrypt["phase"], encryption_flow.PHASE_ENCRYPTED)

        raw = asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'sonarr_api_key'"))
        self.assertTrue(raw.startswith(secrets_box.PREFIX))

    def test_opt_out_records_the_phase(self):
        self._onboard()
        resp = self.client.post("/api/admin/encryption/opt-out", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["phase"], encryption_flow.PHASE_OPTED_OUT)

    # -- key-mismatch gate + recovery ---------------------------------------

    def test_wrong_key_gates_to_recovery_and_refuses_ordinary_admin_api(self):
        self._onboard()
        self._encrypt_under(KEY)
        # Operator restarts with the WRONG key.
        _set_key(OTHER_KEY)
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()),
                         encryption_flow.KEY_MISMATCH)

        # A browser navigation is redirected to the recovery screen.
        home = self.client.get("/", headers=HTML, follow_redirects=False)
        self.assertEqual(home.status_code, 303)
        self.assertTrue(home.headers["location"].endswith(RECOVERY_PATH))

        # The recovery screen itself renders, with its reset control.
        page = self.client.get(RECOVERY_PATH, headers=HTML)
        self.assertEqual(page.status_code, 200)
        self.assertIn(RESET_CONFIRM_PHRASE, page.text)

        # The ordinary admin API is refused while unhealthy.
        gated = self.client.get("/api/settings")
        self.assertEqual(gated.status_code, 503)
        self.assertEqual(gated.json()["reason"], "key_mismatch")

    def test_missing_key_reaches_recovery_page_directly_with_generate_key_door(self):
        """KEY_MISSING does not gate the whole app (it fails open, by design), so
        nothing forces an admin here the way a wrong key does — but the page must
        still work when they find it themselves, offering a way to generate a key
        they've never had before rather than only "put the original back"."""
        self._onboard()
        self._encrypt_under(KEY)
        _set_key(None)
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()),
                         encryption_flow.KEY_MISSING)

        # Ordinary navigation is NOT redirected — the app stays usable.
        home = self.client.get("/", headers=HTML, follow_redirects=False)
        self.assertNotEqual(home.status_code, 303)

        # But the recovery page itself renders when visited directly.
        page = self.client.get(RECOVERY_PATH, headers=HTML)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Generate a new key", page.text)
        # The mismatch door's typed-confirm reset control is not offered here —
        # there is nothing to reset yet until a (new) key is actually in the
        # environment. (The JS constant for that phrase is still embedded either
        # way — it's inert unless the reset button it belongs to also exists.)
        self.assertNotIn("Discard unrecoverable secrets and reset", page.text)

    def test_generate_key_for_a_missing_key_is_stateless(self):
        self._onboard()
        self._encrypt_under(KEY)
        _set_key(None)
        asyncio.run(encryption_flow.refresh_health())

        resp = self.client.post("/api/admin/encryption/recovery/generate-key", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        key1 = resp.json()["key"]
        self.assertTrue(secrets_box.key_is_valid(key1))
        # Calling it again is harmless and gives a fresh key — nothing is stored
        # server-side until the admin saves one to their own environment.
        key2 = self.client.post("/api/admin/encryption/recovery/generate-key", json={}).json()["key"]
        self.assertNotEqual(key1, key2)
        self.assertEqual(asyncio.run(encryption_flow.get_phase()), encryption_flow.PHASE_ENCRYPTED)

    def test_setting_the_generated_key_and_restarting_lands_on_the_ordinary_mismatch_door(self):
        """The point of the generated key: once it's saved and the app restarts,
        KEY_MISSING becomes an ordinary KEY_MISMATCH, and the door-2 reset that
        already exists for that state is what actually cleans up from there."""
        self._onboard()
        self._encrypt_under(KEY)
        _set_key(None)
        new_key = self.client.post("/api/admin/encryption/recovery/generate-key", json={}).json()["key"]

        _set_key(new_key)  # "restart" with the freshly generated key saved
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()),
                         encryption_flow.KEY_MISMATCH)
        page = self.client.get(RECOVERY_PATH, headers=HTML)
        self.assertIn(RESET_CONFIRM_PHRASE, page.text)

        reset = self.client.post("/api/admin/encryption/recovery/reset",
                                 json={"confirm": RESET_CONFIRM_PHRASE})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(encryption_flow.health(), encryption_flow.HEALTHY)

    def test_recovery_reset_requires_the_typed_phrase(self):
        self._onboard()
        self._encrypt_under(KEY)
        _set_key(OTHER_KEY)
        asyncio.run(encryption_flow.refresh_health())

        wrong = self.client.post("/api/admin/encryption/recovery/reset",
                                 json={"confirm": "nope"})
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(asyncio.run(encryption_flow.get_phase()),
                         encryption_flow.PHASE_ENCRYPTED)  # unchanged
        self.assertEqual(encryption_flow.health(), encryption_flow.KEY_MISMATCH)

    def test_recovery_reset_door_clears_unrecoverable_and_heals(self):
        self._onboard()
        self._encrypt_under(KEY)
        _set_key(OTHER_KEY)
        asyncio.run(encryption_flow.refresh_health())

        reset = self.client.post("/api/admin/encryption/recovery/reset",
                                 json={"confirm": RESET_CONFIRM_PHRASE})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertTrue(reset.json()["ok"])
        # Healed under the current key; the unrecoverable secret is gone.
        self.assertEqual(encryption_flow.health(), encryption_flow.HEALTHY)
        self.assertIsNone(asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'sonarr_api_key'")))
        # And the app is reachable again.
        self.assertEqual(self.client.get("/api/settings").status_code, 200)

    def test_reset_refused_when_key_is_healthy(self):
        self._onboard()
        self._encrypt_under(KEY)
        asyncio.run(encryption_flow.refresh_health())  # healthy under KEY
        resp = self.client.post("/api/admin/encryption/recovery/reset",
                                json={"confirm": RESET_CONFIRM_PHRASE})
        self.assertEqual(resp.status_code, 409)

    # -- the unhealthy-write guard on the admin save -------------------------

    def test_settings_secret_save_blocked_while_key_missing_global_save_allowed(self):
        self._onboard()
        self._encrypt_under(KEY)
        # Key removed: degraded/fail-open, app still runs, but secret writes must be
        # refused so a re-save can't overwrite the recoverable ciphertext.
        _set_key(None)
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()),
                         encryption_flow.KEY_MISSING)

        blocked = self.client.post("/api/settings", json={"sonarr_api_key": "new-key"})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["reason"], "key_unhealthy")

        allowed = self.client.post("/api/settings", json={"pagination_limit": 250})
        self.assertEqual(allowed.status_code, 200, allowed.text)


if __name__ == "__main__":
    unittest.main()
