"""Trakt's device-code flow, held on the server and polled by a block that stops
itself.

THE PAGE HOLDS NOTHING. It used to keep the device code and a typed client secret
in script memory and re-send both on every poll; the pairing now lives in the
shared cache's key/value store, per administrator, expiring with the code. These
tests hold the four things that design promises:

  - the pairing is found again by the route, not handed back by the page;
  - its secrets are sealed like every other stored credential;
  - it is gone the moment the flow ends, however it ends — and a poll after that
    never reaches Trakt;
  - the answer draws its own poller only while there is something to wait for,
    which is the whole of how polling stops.

EVERY TRAKT CALL IS A STAND-IN, and the ones a test expects NOT to happen are
stand-ins that fail the test if reached.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import time
import unittest
import zlib
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet

from app import cache, db, secrets_box, settings_routes
from app.auth import encryption_flow
from app.auth import trakt as trakt_auth
from app.config import load_settings, save_settings
from tests.support import AppTestCase, ORIGIN

CODE = {"device_code": "DEVICE-CODE-VALUE", "user_code": "ABCD-1234",
        "verification_url": "https://trakt.tv/activate", "expires_in": 600, "interval": 5}
TOKEN = {"access_token": "fresh-token", "refresh_token": "fresh-refresh",
         "expires_in": 7776000, "created_at": 1_700_000_000}
ACCOUNT = {"id": 424242, "name": "Josh"}
POLLER = re.compile(r'hx-post="/settings/trakt/device/poll"[^>]*hx-trigger="every (\d+)s"')


def must_not_reach(name: str) -> AsyncMock:
    return AsyncMock(side_effect=AssertionError(f"{name} was reached"))


class DeviceFlowTestCase(AppTestCase):
    def make_settings(self):
        return dataclasses.replace(super().make_settings(), public_base_url=ORIGIN,
                                   trakt_client_id="saved-id", trakt_client_secret="saved-secret")

    def setUp(self):
        super().setUp()
        self.admin_id = self.make_user("boss", is_admin=True, calendar_approved=True)
        self.sign_in_as(self.admin_id)

    def tearDown(self):
        os.environ.pop(secrets_box.ENV_VAR, None)
        secrets_box.reset_cache()
        encryption_flow._health = encryption_flow.HEALTHY
        super().tearDown()

    def post(self, action: str, **body):
        return self.client.post(f"/settings/trakt/device/{action}", json=body)

    def start(self, code: dict = CODE, **body):
        with patch("app.auth.trakt.request_device_code", AsyncMock(return_value=code)) as asked:
            resp = self.post("start", **body)
        self.asked = asked
        return resp

    def poll(self, stand_in: AsyncMock):
        with patch("app.auth.trakt.poll_device_token", stand_in), \
             patch("app.auth.trakt.fetch_account", AsyncMock(return_value=ACCOUNT)):
            return self.post("poll")

    def pairing(self):
        return asyncio.run(settings_routes._pairing(self.admin_id))

    def raw_rows(self) -> list[str]:
        """Every stored row for this administrator's pairing, decompressed, so an
        assertion can read what is actually at rest rather than what the reader
        returns."""
        key = settings_routes._pairing_key(self.admin_id)
        rows = asyncio.run(db.fetch_all("SELECT payload FROM api_cache WHERE cache_key = ?", (key,)))
        return [zlib.decompress(row["payload"]).decode("utf-8") for row in rows]


class StartingTests(DeviceFlowTestCase):
    def test_a_started_pairing_is_held_server_side_and_polls_itself(self):
        resp = self.start()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('value="ABCD-1234"', resp.text)
        self.assertEqual(POLLER.search(resp.text).group(1), "5")
        # The code a token is exchanged for never reaches the page.
        self.assertNotIn("DEVICE-CODE-VALUE", resp.text)
        self.assertEqual(self.pairing().device_code, "DEVICE-CODE-VALUE")

    def test_the_poller_waits_as_long_as_trakt_asks_and_never_less_than_five_seconds(self):
        for asked, drawn in ((9, "9"), (2, "5")):
            with self.subTest(interval=asked):
                resp = self.start(code={**CODE, "interval": asked})
                self.assertEqual(POLLER.search(resp.text).group(1), drawn)

    def test_the_pairing_expires_with_the_code(self):
        self.start(code={**CODE, "expires_in": 300})
        self.assertAlmostEqual(self.pairing().expires_at, time.time() + 300, delta=5)

    def test_what_is_typed_is_what_is_used(self):
        self.start(trakt_client_id="typed-id", trakt_client_secret="typed-secret")
        self.asked.assert_awaited_once_with("typed-id")
        self.assertEqual((self.pairing().client_id, self.pairing().client_secret),
                         ("typed-id", "typed-secret"))

    def test_nothing_typed_uses_the_saved_values_and_stores_no_secret(self):
        self.start()
        self.asked.assert_awaited_once_with("saved-id")
        self.assertEqual(self.pairing().client_secret, "")
        self.assertNotIn("saved-secret", "".join(self.raw_rows()))

    def test_no_client_id_is_refused_before_trakt_is_asked(self):
        save_settings(dataclasses.replace(load_settings(), trakt_client_id=""))
        with patch("app.auth.trakt.request_device_code", must_not_reach("request_device_code")):
            resp = self.post("start")
        self.assertIn("Enter your Trakt Client ID first.", resp.text)
        self.assertIsNone(self.pairing())

    def test_no_client_secret_anywhere_is_refused_before_trakt_is_asked(self):
        save_settings(dataclasses.replace(load_settings(), trakt_client_secret=""),
                      clear_unset_secrets=True)
        with patch("app.auth.trakt.request_device_code", must_not_reach("request_device_code")):
            resp = self.post("start")
        self.assertIn("Enter your Trakt Client Secret first.", resp.text)

    def test_it_is_refused_while_the_at_rest_key_is_unhealthy(self):
        """The token this flow ends by storing is a credential, and save_settings
        would silently leave it unwritten."""
        encryption_flow._health = encryption_flow.KEY_MISSING
        with patch("app.auth.trakt.request_device_code", must_not_reach("request_device_code")):
            resp = self.post("start")
        self.assertIn("Encryption is unhealthy", resp.text)
        self.assertIsNone(self.pairing())

    def test_a_trakt_failure_is_a_sentence_and_holds_nothing(self):
        with patch("app.auth.trakt.request_device_code",
                   AsyncMock(side_effect=httpx.ConnectError("unreachable"))):
            resp = self.post("start")
        self.assertIn("Could not start device authorization", resp.text)
        self.assertIsNone(POLLER.search(resp.text))
        self.assertEqual(self.raw_rows(), [])

    def test_a_verification_page_that_is_not_https_is_not_drawn_as_a_link(self):
        resp = self.start(code={**CODE, "verification_url": "javascript:alert(1)"})
        self.assertNotIn("javascript:", resp.text)
        self.assertIn('href="https://trakt.tv/activate"', resp.text)

    def test_its_secrets_are_sealed_while_encryption_is_on(self):
        os.environ[secrets_box.ENV_VAR] = Fernet.generate_key().decode()
        secrets_box.reset_cache()
        self.start(trakt_client_secret="TYPED-SECRET-VALUE")
        at_rest = "".join(self.raw_rows())
        self.assertIn(secrets_box.PREFIX, at_rest)
        self.assertNotIn("DEVICE-CODE-VALUE", at_rest)
        self.assertNotIn("TYPED-SECRET-VALUE", at_rest)
        # ...and the route still reads them back.
        self.assertEqual(self.pairing().client_secret, "TYPED-SECRET-VALUE")


class PollingTests(DeviceFlowTestCase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_still_waiting_draws_the_poller_again(self):
        for outcome in (trakt_auth.DevicePending("wait"), trakt_auth.DeviceSlowDown("slow")):
            with self.subTest(outcome=type(outcome).__name__):
                resp = self.poll(AsyncMock(side_effect=outcome))
                self.assertIsNotNone(POLLER.search(resp.text))
                self.assertIsNotNone(self.pairing())

    def test_a_poll_carries_nothing_and_the_route_reads_the_pairing_back(self):
        stand_in = AsyncMock(side_effect=trakt_auth.DevicePending("wait"))
        self.poll(stand_in)
        stand_in.assert_awaited_once_with("saved-id", "saved-secret", "DEVICE-CODE-VALUE")

    def test_approval_stores_the_token_forgets_the_pairing_and_stops_polling(self):
        resp = self.poll(AsyncMock(return_value=TOKEN))
        self.assertIn("Authorized", resp.text)
        self.assertIsNone(POLLER.search(resp.text))
        self.assertEqual(load_settings().trakt_access_token, "fresh-token")
        self.assertEqual(self.raw_rows(), [])
        # The three parts of the tab an authorization changes ride out of band.
        for part in ('id="traktToken" class="trakt-token" hx-swap-oob="true"',
                     'id="traktReconnect" class="field" hx-swap-oob="true"',
                     'id="secret-trakt_access_token" hx-swap-oob="true"'):
            with self.subTest(part=part):
                self.assertIn(part, resp.text)
        self.assertNotIn("fresh-token", resp.text)
        self.assertNotIn("fresh-refresh", resp.text)

    def test_a_refusal_from_trakt_ends_the_flow(self):
        for outcome in (trakt_auth.DeviceExpired("The device code expired — start over."),
                        trakt_auth.DeviceDenied("Authorization was denied.")):
            with self.subTest(outcome=type(outcome).__name__):
                self.start()
                resp = self.poll(AsyncMock(side_effect=outcome))
                self.assertIn(str(outcome), resp.text)
                self.assertIsNone(POLLER.search(resp.text))
                self.assertEqual(self.raw_rows(), [])

    def test_a_transport_hiccup_keeps_waiting(self):
        resp = self.poll(AsyncMock(side_effect=httpx.ConnectError("blip")))
        self.assertIsNotNone(POLLER.search(resp.text))
        self.assertIn("still waiting", resp.text)
        self.assertIsNotNone(self.pairing())

    def test_an_error_trakt_answered_with_ends_the_flow(self):
        request = httpx.Request("POST", trakt_auth.DEVICE_TOKEN_URL)
        failure = httpx.HTTPStatusError("500", request=request,
                                        response=httpx.Response(500, request=request))
        resp = self.poll(AsyncMock(side_effect=failure))
        self.assertIn("Trakt error", resp.text)
        self.assertIsNone(POLLER.search(resp.text))
        self.assertIsNone(self.pairing())


class EndingTests(DeviceFlowTestCase):
    def test_cancelling_forgets_the_pairing_and_a_later_poll_never_reaches_trakt(self):
        self.start()
        resp = self.post("cancel")
        self.assertIn("Authorization cancelled.", resp.text)
        self.assertIsNone(POLLER.search(resp.text))
        self.assertEqual(self.raw_rows(), [])
        resp = self.poll(must_not_reach("poll_device_token"))
        self.assertIn("no longer waiting", resp.text)
        self.assertIsNone(POLLER.search(resp.text))

    def test_an_expired_pairing_is_forgotten_without_asking_trakt(self):
        self.start()
        stored = asyncio.run(cache.get_stale(settings_routes._pairing_key(self.admin_id)))
        asyncio.run(cache.set(settings_routes._pairing_key(self.admin_id),
                              {**stored, "expires_at": int(time.time()) - 1}))
        resp = self.poll(must_not_reach("poll_device_token"))
        self.assertIn("no longer waiting", resp.text)
        self.assertEqual(self.raw_rows(), [])

    def test_a_pairing_sealed_under_a_key_that_has_gone_is_forgotten(self):
        os.environ[secrets_box.ENV_VAR] = Fernet.generate_key().decode()
        secrets_box.reset_cache()
        self.start()
        os.environ[secrets_box.ENV_VAR] = Fernet.generate_key().decode()
        secrets_box.reset_cache()
        resp = self.poll(must_not_reach("poll_device_token"))
        self.assertIn("no longer waiting", resp.text)
        self.assertEqual(self.raw_rows(), [])


class ThePanelFindsItAgainTests(DeviceFlowTestCase):
    def test_reopening_the_panel_mid_flow_shows_the_code_and_resumes_polling(self):
        self.start()
        page = self.client.get("/settings/fragments/panel").text
        self.assertIn('value="ABCD-1234"', page)
        self.assertIsNotNone(POLLER.search(page))
        self.assertNotIn("DEVICE-CODE-VALUE", page)

    def test_with_nothing_waiting_the_panel_offers_to_start(self):
        page = self.client.get("/settings/fragments/panel").text
        self.assertIn('hx-post="/settings/trakt/device/start"', page)
        self.assertIsNone(POLLER.search(page))

    def test_one_administrators_pairing_is_not_anothers(self):
        self.start()
        other = self.make_user("second_admin", is_admin=True, calendar_approved=True)
        self.client.cookies.clear()
        self.sign_in_as(other)
        resp = self.poll(must_not_reach("poll_device_token"))
        self.assertIn("no longer waiting", resp.text)
        self.assertIsNotNone(asyncio.run(settings_routes._pairing(self.admin_id)))


if __name__ == "__main__":
    unittest.main()
