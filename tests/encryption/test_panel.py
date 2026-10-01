"""The at-rest encryption section of the Settings panel: its markup for every state
the flow can be in, and the four actions that each answer with it.

THE MARKUP PER STATE IS A TABLE, NOT A SECOND IMPLEMENTATION. The section used to
be a state machine in the browser restating app/auth/encryption_flow.py's rules;
it is a template now, and the risk has moved rather than gone — a branch in the
wrong order draws "encrypted" over an instance whose key is missing. So every
phase is rendered against every key health, and what is expected of each is
written out below by hand rather than derived, because deriving it would only
test the template against a copy of itself.

No network.
"""
from __future__ import annotations

import asyncio
import os
import re
import unittest

from cryptography.fernet import Fernet

from app import db, secrets_box
from app.auth import encryption_flow as flow
from app.config import Settings, load_settings, save_settings
from app.templating import templates
from tests.support import AppTestCase

PLAIN = ("enc-plain", "⚠️ Secrets are stored in plaintext")
ENCRYPTED = ("enc-encrypted", "🔒 Secrets encrypted at rest")
MISSING = ("enc-plain", "⚠️ Secrets encrypted, key missing")
MISMATCH = ("enc-plain", "⚠️ Secrets encrypted under a different key")
RESEAL = ("enc-plain", "⚠️ Some secrets are unencrypted")

# (phase, health, key_valid, needs_reseal) -> (heading, actions offered, recovery link)
EXPECTED = {
    # No key in the environment, nothing wrong with the one that is not there.
    (flow.PHASE_NONE, flow.HEALTHY, False, False): (PLAIN, ["enable", "enable", "opt-out"], False),
    (flow.PHASE_PENDING_KEY, flow.HEALTHY, False, False): (PLAIN, ["verify", "opt-out"], False),
    (flow.PHASE_PENDING_ENCRYPT, flow.HEALTHY, False, False): (PLAIN, ["verify", "opt-out"], False),
    (flow.PHASE_ENCRYPTED, flow.HEALTHY, False, False): (ENCRYPTED, [], False),
    (flow.PHASE_OPTED_OUT, flow.HEALTHY, False, False): (PLAIN, ["enable", "enable"], False),
    # A valid key is set: nothing to restart for.
    (flow.PHASE_NONE, flow.HEALTHY, True, False): (PLAIN, ["encrypt", "opt-out"], False),
    (flow.PHASE_PENDING_KEY, flow.HEALTHY, True, False): (PLAIN, ["encrypt", "opt-out"], False),
    (flow.PHASE_PENDING_ENCRYPT, flow.HEALTHY, True, False): (PLAIN, ["encrypt"], False),
    (flow.PHASE_ENCRYPTED, flow.HEALTHY, True, False): (ENCRYPTED, [], False),
    (flow.PHASE_OPTED_OUT, flow.HEALTHY, True, False): (PLAIN, ["encrypt"], False),
    # `phase` is a ratchet: sealed once, but something since is in the clear.
    (flow.PHASE_ENCRYPTED, flow.HEALTHY, True, True): (RESEAL, ["encrypt"], False),
}
# A missing or wrong key outranks every phase, and offers only the way out.
for _phase in (flow.PHASE_NONE, flow.PHASE_PENDING_KEY, flow.PHASE_PENDING_ENCRYPT,
               flow.PHASE_ENCRYPTED, flow.PHASE_OPTED_OUT):
    for _reseal in (False, True):
        EXPECTED[(_phase, flow.KEY_MISSING, False, _reseal)] = (MISSING, [], True)
        EXPECTED[(_phase, flow.KEY_MISMATCH, True, _reseal)] = (MISMATCH, [], True)


def render(phase: str, health: str, key_valid: bool, needs_reseal: bool, *,
           reveal: dict | None = None, error: str | None = None) -> str:
    state = {"phase": phase, "health": health, "key_present": key_valid, "key_valid": key_valid,
             "secrets_present": True, "needs_reseal": needs_reseal, "env_var": secrets_box.ENV_VAR}
    return templates.env.get_template("_encryption_panel.html").render(
        encryption=state, encryption_reveal=reveal, encryption_error=error)


def heading(html: str) -> tuple[str, str]:
    return re.search(r'<div class="settings-section (enc-\w+)">([^<]+)</div>', html).groups()


def actions(html: str) -> list[str]:
    return re.findall(r'hx-post="/admin/encryption/([a-z-]+)"', html)


class MarkupPerStateTests(unittest.TestCase):
    def test_every_phase_against_every_key_health(self):
        self.assertEqual(len(EXPECTED), 31)
        for (phase, health, key_valid, reseal), (title, offered, recovery) in EXPECTED.items():
            with self.subTest(phase=phase, health=health, key_valid=key_valid, needs_reseal=reseal):
                html = render(phase, health, key_valid, reseal)
                self.assertEqual(heading(html), title)
                self.assertEqual(actions(html), offered)
                self.assertEqual('href="/admin/encryption/recovery"' in html, recovery)

    def test_every_action_answers_the_section_and_sends_json(self):
        html = render(flow.PHASE_NONE, flow.HEALTHY, False, False)
        for button in re.findall(r"<button\b[^>]*hx-post[^>]*>", html):
            with self.subTest(button=button[:60]):
                self.assertIn("hx-json-enc", button)
                self.assertIn('hx-target="#encryptionPanel"', button)
                self.assertIn('hx-swap="outerHTML"', button)

    def test_the_two_first_offers_differ_only_in_whether_a_key_is_generated(self):
        html = render(flow.PHASE_NONE, flow.HEALTHY, False, False)
        self.assertIn("hx-vals='{\"generate\": true}'", html)
        self.assertIn("hx-vals='{\"generate\": false}'", html)

    def test_encrypting_asks_first(self):
        html = render(flow.PHASE_PENDING_ENCRYPT, flow.HEALTHY, True, False)
        self.assertIn('id="confirm-encrypt-secrets"', html)
        self.assertIn("Make sure you have saved the key first.", html)

    def test_a_generated_key_is_drawn_to_be_copied(self):
        html = render(flow.PHASE_PENDING_KEY, flow.HEALTHY, False, False,
                      reveal={"restart_required": True, "key": "THE-GENERATED-KEY"})
        self.assertIn('value="THE-GENERATED-KEY"', html)
        self.assertIn('data-copy="#encryptionKey"', html)
        self.assertIn("shown once and never again", html)
        self.assertEqual(actions(html), ["verify", "opt-out"])

    def test_bringing_your_own_key_shows_how_to_make_one_instead(self):
        html = render(flow.PHASE_PENDING_KEY, flow.HEALTHY, False, False,
                      reveal={"restart_required": True, "key": None})
        self.assertIn("Fernet.generate_key()", html)
        self.assertNotIn('id="encryptionKey"', html)

    def test_an_error_is_drawn_in_the_section(self):
        html = render(flow.PHASE_PENDING_KEY, flow.HEALTHY, False, False, error="No valid key found.")
        self.assertIn('<p class="hint err" role="alert">No valid key found.</p>', html)


class ActionRouteTests(AppTestCase):
    """The four actions, driven through the routes."""

    def setUp(self):
        super().setUp()
        self._set_key(None)
        self.sign_in_as(self.make_user("admin_user", is_admin=True, calendar_approved=True))
        save_settings(Settings(sonarr_api_key="sonarr-secret", simkl_public_calendar_enabled=False))

    def tearDown(self):
        self._set_key(None)
        flow._health = flow.HEALTHY
        super().tearDown()

    @staticmethod
    def _set_key(key: str | None) -> None:
        if key is None:
            os.environ.pop(secrets_box.ENV_VAR, None)
        else:
            os.environ[secrets_box.ENV_VAR] = key
        secrets_box.reset_cache()

    def act(self, action: str, **body):
        resp = self.client.post(f"/admin/encryption/{action}", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text

    def phase(self) -> str:
        return asyncio.run(flow.get_phase())

    def test_generate_verify_encrypt_end_to_end(self):
        answer = self.act("enable", generate=True)
        key = re.search(r'id="encryptionKey" readonly value="([^"]+)"', answer).group(1)
        Fernet(key)  # raises unless it is a usable key
        self.assertEqual(self.phase(), flow.PHASE_PENDING_KEY)

        # Shown once: nothing drawn afterwards carries it.
        self.assertNotIn(key, self.client.get("/settings/fragments/panel").text)

        self._set_key(key)  # the operator's restart with the key in place
        answer = self.act("verify")
        self.assertEqual(self.phase(), flow.PHASE_PENDING_ENCRYPT)
        self.assertEqual(actions(answer), ["encrypt"])

        answer = self.act("encrypt")
        self.assertEqual(heading(answer), ENCRYPTED)
        raw = asyncio.run(db.fetch_value("SELECT value FROM app_secrets WHERE name = 'sonarr_api_key'"))
        self.assertTrue(raw.startswith(secrets_box.PREFIX))
        self.assertNotIn(raw, answer)
        self.assertEqual(load_settings().sonarr_api_key, "sonarr-secret")

    def test_bringing_your_own_key_reveals_nothing(self):
        answer = self.act("enable", generate=False)
        self.assertNotIn('id="encryptionKey"', answer)
        self.assertIn("Fernet.generate_key()", answer)

    def test_a_key_already_in_place_goes_straight_to_the_confirmation(self):
        self._set_key(Fernet.generate_key().decode())
        answer = self.act("enable", generate=True)
        self.assertNotIn('id="encryptionKey"', answer)
        self.assertEqual(self.phase(), flow.PHASE_PENDING_ENCRYPT)
        self.assertEqual(actions(answer), ["encrypt"])

    def test_checking_for_a_key_that_has_not_arrived_says_so_and_changes_nothing(self):
        self.act("enable", generate=False)
        answer = self.act("verify")
        self.assertIn("No valid key found in the environment yet.", answer)
        self.assertEqual(self.phase(), flow.PHASE_PENDING_KEY)

    def test_encrypting_without_a_key_is_refused_and_seals_nothing(self):
        answer = self.act("encrypt")
        self.assertIn("No valid encryption key is set in the environment yet.", answer)
        raw = asyncio.run(db.fetch_value("SELECT value FROM app_secrets WHERE name = 'sonarr_api_key'"))
        self.assertEqual(raw, "sonarr-secret")

    def test_not_now_records_the_choice(self):
        answer = self.act("opt-out")
        self.assertEqual(self.phase(), flow.PHASE_OPTED_OUT)
        self.assertEqual(actions(answer), ["enable", "enable"])


if __name__ == "__main__":
    unittest.main()
