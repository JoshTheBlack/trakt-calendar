"""Signing in with a passkey, and adding one (app/auth/passkeys.py, the passkey
routes in app/auth/routes.py and the admin removal).

A passkey is ONE MORE WAY INTO AN ACCOUNT THAT ALREADY EXISTS: added from the
Account page after the account's current password, used from the sign-in page
with no username, counted among an account's ways in. Every ceremony here is
driven by tests/auth/_authenticator.py's software authenticator against the
options the app's own buttons carry, so what is verified is what a browser would
send. No network.
"""
from __future__ import annotations

import asyncio
import json
import re
from html import unescape
from pathlib import Path
from unittest.mock import patch

from app import auth, db
from app.auth import encryption_flow, passkeys
from app.config import Settings
from tests.auth._authenticator import Authenticator, b64
from tests.support import ORIGIN, PASSWORD, AppTestCase, save_settings

RP_ID = "testserver"


def _attr(html: str, name: str) -> str | None:
    found = re.search(rf'{name}="([^"]*)"', html)
    return unescape(found.group(1)) if found else None


class PasskeyTestCase(AppTestCase):

    def make_settings(self):
        return Settings(simkl_public_calendar_enabled=False, public_base_url=ORIGIN)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("keyholder", calendar_approved=True)

    # -- adding ----------------------------------------------------------------

    def begin(self, password: str = PASSWORD, name: str = "Laptop"):
        return self.client.post("/me/passkeys/begin",
                                json={"current_password": password, "name": name})

    def add(self, device: Authenticator, *, name: str = "Laptop",
            password: str = PASSWORD) -> str:
        """Add `device`'s passkey to the signed-in account; the finish's answer."""
        options = _attr(self.begin(password, name=name).text, "data-passkey-create")
        self.assertIsNotNone(options, "the password step drew no Create passkey button")
        resp = self.client.post("/me/passkeys", json={
            "credential": device.create(options, origin=ORIGIN), "error": "", "name": name})
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def holding(self, device: Authenticator | None = None, user_id: int | None = None):
        """An account (this one by default) signed in, holding `device`'s
        passkey, and then signed out again so a sign-in can be tried."""
        device = device or Authenticator()
        self.sign_in_as(user_id or self.user_id)
        self.assertIn("added", self.add(device))
        self.client.cookies.clear()
        return device

    def stored(self, user_id: int | None = None) -> list:
        return asyncio.run(db.fetch_all("SELECT * FROM passkeys WHERE user_id = ?",
                                        (user_id or self.user_id,)))

    # -- signing in -------------------------------------------------------------

    def offer(self, client=None):
        resp = (client or self.client).post("/login/passkey/offer", json={})
        return resp, _attr(resp.text, "data-passkey-get")

    def sign_in(self, device: Authenticator, client=None, **wrong):
        client = client or self.client
        _resp, options = self.offer(client)
        origin = wrong.pop("origin", ORIGIN)
        return client.post("/login/passkey", json={
            "credential": device.assert_(options, origin=origin, **wrong), "error": ""})

    def failures(self) -> int:
        return asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = 'ip' AND succeeded = 0"))


class AddingOneTests(PasskeyTestCase):

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.user_id)

    def test_the_options_ask_for_a_discoverable_verified_passkey_for_this_instance(self):
        first = Authenticator()
        self.add(first)
        options = json.loads(_attr(self.begin().text, "data-passkey-create"))
        self.assertEqual(options["rp"]["id"], RP_ID)
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "required")
        self.assertEqual(options["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(options["attestation"], "none")
        self.assertEqual([c["id"] for c in options["excludeCredentials"]],
                         [b64(first.credential_id)])
        self.assertEqual(options["user"]["name"], "keyholder")

    def test_a_finish_stores_the_passkey_under_one_handle(self):
        self.add(Authenticator(synced=True), name="Phone")
        self.add(Authenticator(synced=False), name="Laptop")
        rows = self.stored()
        self.assertEqual([(r["name"], r["backed_up"]) for r in rows],
                         [("Phone", 1), ("Laptop", 0)])
        self.assertEqual(json.loads(rows[0]["transports"]), ["internal", "hybrid"])
        handles = asyncio.run(db.fetch_all("SELECT passkey_handle FROM users WHERE id = ?",
                                           (self.user_id,)))
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0][0])

    def test_the_handle_is_made_once_and_is_never_the_row_id(self):
        self.begin()
        handle = asyncio.run(db.fetch_value("SELECT passkey_handle FROM users WHERE id = ?",
                                            (self.user_id,)))
        self.begin()
        self.assertEqual(asyncio.run(db.fetch_value(
            "SELECT passkey_handle FROM users WHERE id = ?", (self.user_id,))), handle)
        options = json.loads(_attr(self.begin().text, "data-passkey-create"))
        self.assertEqual(options["user"]["id"], handle)
        self.assertNotEqual(options["user"]["id"], b64(str(self.user_id).encode()))

    def test_at_the_most_the_password_step_says_so(self):
        for n in range(passkeys.MAX_PER_ACCOUNT):
            asyncio.run(db.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, name, created_at) "
                "VALUES (?, ?, x'00', 'k', 1)", (self.user_id, f"cred{n}")))
        answer = self.begin().text
        self.assertIsNone(_attr(answer, "data-passkey-create"))
        self.assertIn(f"at most {passkeys.MAX_PER_ACCOUNT} passkeys", answer)

    def test_a_challenge_another_session_started_is_refused(self):
        options = _attr(self.begin().text, "data-passkey-create")
        self.sign_in_as(self.user_id)  # a second session of the same account
        resp = self.client.post("/me/passkeys", json={
            "credential": Authenticator().create(options, origin=ORIGIN), "name": "x"})
        self.assertIn("Start again", unescape(resp.text))
        self.assertEqual(self.stored(), [])

    def test_a_passkey_made_for_another_origin_is_refused(self):
        options = _attr(self.begin().text, "data-passkey-create")
        resp = self.client.post("/me/passkeys", json={
            "credential": Authenticator().create(options, origin="https://evil.example"),
            "name": "x"})
        self.assertIn("couldn't be added", unescape(resp.text))
        self.assertEqual(self.stored(), [])

    def test_a_prompt_closed_draws_a_fresh_create_button(self):
        self.begin()
        resp = self.client.post("/me/passkeys", json={"credential": "", "error": "NotAllowedError",
                                                      "name": "x"})
        self.assertIn("No passkey was used", unescape(resp.text))
        self.assertIsNotNone(_attr(resp.text, "data-passkey-create"))


class ThePasswordComesFirstTests(PasskeyTestCase):

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.user_id)

    def test_a_wrong_password_mints_nothing_and_is_counted(self):
        before = asyncio.run(db.fetch_value("SELECT COUNT(*) FROM auth_handshakes"))
        answer = self.begin(password="not it").text
        self.assertIn("That isn't your current password.", unescape(answer))
        self.assertIsNone(_attr(answer, "data-passkey-create"))
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM auth_handshakes")),
                         before)
        self.assertEqual(self.failures(), 1)

    def test_a_locked_out_address_is_refused_without_being_counted(self):
        for _ in range(auth.LOGIN_IP_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("ip", "testclient", False))
        answer = self.begin().text
        self.assertIn("Too many attempts", answer)
        self.assertIsNone(_attr(answer, "data-passkey-create"))
        self.assertEqual(self.failures(), auth.LOGIN_IP_MAX_ATTEMPTS)

    def test_the_right_password_mints_the_challenge_and_clears_the_counter(self):
        self.begin(password="not it")
        self.assertIsNotNone(_attr(self.begin().text, "data-passkey-create"))
        self.assertEqual(self.failures(), 0)

    def test_an_account_with_no_password_is_drawn_the_control_off_and_refused(self):
        asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?",
                               (self.user_id,)))
        self.link_identity(self.user_id, "trakt", 77, access_token="t")
        page = self.client.get("/me").text
        section = page[page.index('id="accountPasskeys"'):]
        self.assertIn('type="button" disabled>Add a passkey', section)
        self.assertIn("Set a password below first", section)
        self.assertNotIn("/me/passkeys/begin", section.split('id="accountCredentials"')[0])
        self.assertIn("Set a password first", unescape(self.begin(password="").text))

    def test_a_first_password_turns_the_control_on_without_a_reload(self):
        asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?",
                               (self.user_id,)))
        self.link_identity(self.user_id, "trakt", 78, access_token="t")
        answer = self.client.post("/me/password", json={
            "password": "a-new-password", "password_confirm": "a-new-password"}).text
        self.assertIn('id="accountPasskeys"', answer)
        self.assertIn('hx-swap-oob="outerHTML"', answer[answer.index('id="accountPasskeys"') - 200:])
        self.assertIn('hx-post="/me/passkeys/begin"', answer)

    def test_password_saved_names_the_passkeys_only_when_there_are_some(self):
        body = {"current_password": PASSWORD, "password": "another-password",
                "password_confirm": "another-password"}
        self.assertNotIn("passkeys still sign you in",
                         self.client.post("/me/password", json=body).text)
        self.add(Authenticator(), password="another-password")
        body["current_password"] = "another-password"
        self.assertIn("passkeys still sign you in",
                      self.client.post("/me/password", json=body).text)


class SigningInTests(PasskeyTestCase):

    def test_a_passkey_signs_its_account_in_as_a_password_does(self):
        device = self.holding()
        resp = self.sign_in(device)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers["HX-Redirect"], "/")
        self.assertTrue(self.client.cookies.get(auth.COOKIE_NAME_SECURE))
        self.assertEqual(self.client.get("/me").status_code, 200)
        self.assertIsNotNone(self.stored()[0]["last_used_at"])

    def test_every_refusal_is_one_sentence(self):
        device = self.holding()
        other = self.make_user("other", calendar_approved=True)
        other_device = self.holding(user_id=other)
        stranger = Authenticator()
        stranger.user_handle = device.user_handle
        cases = {
            "unknown": lambda: self.sign_in(stranger),
            "wrong user handle": lambda: self.sign_in(device, user_handle=other_device.user_handle),
            "wrong origin": lambda: self.sign_in(device, origin="https://evil.example"),
            "wrong rp id": lambda: self.sign_in(device, rp_id="evil.example"),
            "not verified": lambda: self.sign_in(device, verified=False),
        }
        for case, attempt in cases.items():
            with self.subTest(case):
                resp = attempt()
                self.assertEqual(resp.status_code, 200)
                self.assertIn(passkeys.REFUSED, unescape(resp.text))
                self.assertIsNotNone(_attr(resp.text, "data-passkey-get"),
                                     "a refusal draws a fresh button")
        asyncio.run(db.execute("UPDATE users SET is_disabled = 1 WHERE id = ?", (self.user_id,)))
        self.assertIn(passkeys.REFUSED, unescape(self.sign_in(device).text))
        asyncio.run(db.execute("UPDATE users SET is_disabled = 0 WHERE id = ?", (self.user_id,)))
        asyncio.run(db.execute("DELETE FROM passkeys WHERE user_id = ?", (self.user_id,)))
        self.assertIn(passkeys.REFUSED, unescape(self.sign_in(device).text))

    def test_a_bad_signature_is_refused(self):
        device = self.holding()
        _resp, options = self.offer()
        answer = json.loads(device.assert_(options, origin=ORIGIN))
        answer["response"]["signature"] = b64(b"\x30\x06\x02\x01\x01\x02\x01\x01")
        resp = self.client.post("/login/passkey", json={"credential": json.dumps(answer)})
        self.assertIn(passkeys.REFUSED, unescape(resp.text))

    def test_a_challenge_works_once(self):
        device = self.holding()
        _resp, options = self.offer()
        state = self.client.cookies.get(passkeys.ATTEMPT_COOKIE_SECURE)
        answer = device.assert_(options, origin=ORIGIN)
        self.assertEqual(
            self.client.post("/login/passkey", json={"credential": answer}).status_code, 204)
        self.client.cookies.set(passkeys.ATTEMPT_COOKIE_SECURE, state)
        self.assertIn(passkeys.REFUSED,
                      unescape(self.client.post("/login/passkey", json={"credential": answer}).text))

    def test_a_challenge_another_browser_was_given_is_refused(self):
        device = self.holding()
        _resp, options = self.offer()
        self.client.cookies.clear()
        resp = self.client.post("/login/passkey",
                                json={"credential": device.assert_(options, origin=ORIGIN)})
        self.assertIn(passkeys.REFUSED, unescape(resp.text))

    def test_an_expired_challenge_is_refused(self):
        device = self.holding()
        _resp, options = self.offer()
        asyncio.run(db.execute("UPDATE auth_handshakes SET expires_at = 1"))
        resp = self.client.post("/login/passkey",
                                json={"credential": device.assert_(options, origin=ORIGIN)})
        self.assertIn(passkeys.REFUSED, unescape(resp.text))

    def test_a_sign_count_that_goes_backwards_is_refused(self):
        device = self.holding(Authenticator(synced=False, counter=5))
        self.assertEqual(self.sign_in(device, counter=9).status_code, 204)
        self.client.cookies.clear()
        self.assertIn(passkeys.REFUSED, unescape(self.sign_in(device, counter=7).text))

    def test_a_synced_passkey_reporting_no_count_is_accepted_every_time(self):
        device = self.holding(Authenticator(synced=True, counter=0))
        for _ in range(2):
            self.client.cookies.clear()
            self.assertEqual(self.sign_in(device, counter=0).status_code, 204)

    def test_failures_count_on_the_address_and_a_success_clears_them(self):
        device = self.holding()
        self.sign_in(device, origin="https://evil.example")
        self.assertEqual(self.failures(), 1)
        self.assertEqual(self.sign_in(device).status_code, 204)
        self.assertEqual(self.failures(), 0)

    def test_a_locked_out_address_is_refused_and_not_counted(self):
        device = self.holding()
        for _ in range(auth.LOGIN_IP_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("ip", "testclient", False))
        resp = self.sign_in(device)
        self.assertIn(passkeys.REFUSED, unescape(resp.text))
        self.assertEqual(self.failures(), auth.LOGIN_IP_MAX_ATTEMPTS)

    def test_a_prompt_the_browser_closed_is_not_a_failure(self):
        self.holding()
        self.offer()
        resp = self.client.post("/login/passkey", json={"credential": "", "error": "NotAllowedError"})
        self.assertIn("No passkey was used", unescape(resp.text))
        self.assertEqual(self.failures(), 0)

    def test_password_and_passkey_end_in_the_one_sign_in_tail(self):
        from app.auth import routes
        device = self.holding()
        real = routes._signed_in
        calls = []

        async def counted(*args, **kwargs):
            calls.append(args[1])
            return await real(*args, **kwargs)

        with patch.object(routes, "_signed_in", counted):
            self.client.post("/login", json={"username": "keyholder", "password": PASSWORD})
            self.client.cookies.clear()
            self.sign_in(device)
        self.assertEqual(calls, [self.user_id, self.user_id])


class WhileTheKeyIsWrongTests(PasskeyTestCase):

    def test_a_passkey_still_signs_in(self):
        import os
        from cryptography.fernet import Fernet
        from app import secrets_box
        device = self.holding()

        def set_key(key):
            if key is None:
                os.environ.pop(secrets_box.ENV_VAR, None)
            else:
                os.environ[secrets_box.ENV_VAR] = key
            secrets_box.reset_cache()

        def restore():
            set_key(None)
            encryption_flow._health = encryption_flow.HEALTHY

        self.addCleanup(restore)
        set_key(Fernet.generate_key().decode())
        save_settings(Settings(simkl_public_calendar_enabled=False, public_base_url=ORIGIN,
                               trakt_access_token="sealed-token"))
        asyncio.run(encryption_flow.encrypt_now())
        set_key(Fernet.generate_key().decode())
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()),
                         encryption_flow.KEY_MISMATCH)
        resp = self.sign_in(device)
        self.assertEqual(resp.status_code, 204, resp.text[:300])


class WhereTheyAreOfferedTests(PasskeyTestCase):

    def page(self) -> str:
        return self.client.get("/login").text

    def test_none_without_a_public_base_url_or_with_one_a_browser_will_not_use(self):
        for base in ("", "http://tns.example", "https://192.168.1.2", "http://127.0.0.1:8000"):
            with self.subTest(base=base):
                self.assertIsNone(passkeys.relying_party(Settings(public_base_url=base)))
        for base, rp in (("https://tns.example", ("tns.example", "https://tns.example")),
                         ("http://localhost:8000", ("localhost", "http://localhost:8000"))):
            with self.subTest(base=base):
                self.assertEqual(tuple(passkeys.relying_party(Settings(public_base_url=base))), rp)

    def test_the_sign_in_page_asks_for_the_button_and_the_answer_is_hidden_with_its_origin(self):
        page = self.page()
        self.assertIn('hx-post="/login/passkey/offer"', page)
        self.assertIn('autocomplete="username webauthn"', page)
        self.assertNotIn("data-passkey-get", page, "the page's own GET mints nothing")
        resp, options = self.offer()
        self.assertIsNotNone(options)
        self.assertIn(f'data-passkey-origin="{ORIGIN}" hidden', unescape(resp.text))
        self.assertEqual(json.loads(options)["rpId"], RP_ID)
        self.assertEqual(json.loads(options)["userVerification"], "required")

    def test_with_no_public_base_url_nothing_is_offered(self):
        save_settings(Settings(simkl_public_calendar_enabled=False))
        page = self.page()
        self.assertNotIn("/login/passkey/offer", page)
        self.assertNotIn("webauthn", page)
        self.assertEqual(self.client.post("/login/passkey/offer", json={}).status_code, 204)

    def test_throttled_the_button_is_not_drawn(self):
        with patch.object(auth, "passkey_start_limited", return_value=True):
            resp, options = self.offer()
        self.assertIsNone(options)
        self.assertNotIn("passwordFold", unescape(resp.text))

    def test_the_account_page_says_why_where_passkeys_cannot_be_offered(self):
        save_settings(Settings(simkl_public_calendar_enabled=False))
        self.sign_in_as(self.user_id)
        page = self.client.get("/me").text
        self.assertIn("public base URL to be https", page)
        self.assertNotIn("/me/passkeys/begin", page)


class ABudgetOfItsOwnTests(PasskeyTestCase):
    """Every public passkey handshake — the sign-in page's offer and each
    refusal's fresh button — is minted under one budget per address, apart from
    the one the services' sign-in buttons share."""

    def make_settings(self):
        return Settings(simkl_public_calendar_enabled=False, public_base_url=ORIGIN,
                        trakt_client_id="client-id", trakt_client_secret="client-secret")

    def minted(self) -> int:
        return asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM auth_handshakes WHERE provider = 'passkey'"))

    def test_loading_the_sign_in_page_spends_none_of_the_services_budget(self):
        for _ in range(auth.HANDSHAKE_MAX_ATTEMPTS):
            _resp, options = self.offer()
            self.assertIsNotNone(options)
        resp = self.client.get("/auth/trakt/start", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def test_refusals_past_the_budget_mint_nothing_more(self):
        answers =[self.client.post("/login/passkey",
                                    json={"credential": "", "error": "NotAllowedError"})
                   for _ in range(auth.PASSKEY_MAX_ATTEMPTS + 10)]
        # bad credentials, straight through the per-address lockout
        answers += [self.client.post("/login/passkey", json={"credential": "{}", "error": ""})
                    for _ in range(auth.LOGIN_IP_MAX_ATTEMPTS + 10)]
        self.assertEqual(self.minted(), auth.PASSKEY_MAX_ATTEMPTS)
        self.assertIsNotNone(_attr(answers[auth.PASSKEY_MAX_ATTEMPTS - 1].text,
                                   "data-passkey-get"))
        last = unescape(answers[-1].text)
        self.assertIsNone(_attr(last, "data-passkey-get"))
        self.assertIn(passkeys.REFUSED, last)
        self.assertIn("Passkey sign-in is paused", last)

    def test_the_offer_stops_at_its_budget_and_the_services_start_goes_on(self):
        for _ in range(auth.PASSKEY_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("passkey_ip", "testclient", True))
        _resp, options = self.offer()
        self.assertIsNone(options)
        self.assertEqual(self.minted(), 0)
        self.assertEqual(
            self.client.get("/auth/trakt/start", follow_redirects=False).status_code, 303)

    def test_a_refusal_over_budget_still_says_what_happened(self):
        for _ in range(auth.PASSKEY_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("passkey_ip", "testclient", True))
        resp = self.client.post("/login/passkey",
                                json={"credential": "", "error": "NotAllowedError"})
        text = unescape(resp.text)
        self.assertIn("No passkey was used", text)
        self.assertIn(f'data-passkey-origin="{ORIGIN}" hidden', text)
        self.assertIsNone(_attr(text, "data-passkey-get"))

    def test_a_refused_request_is_not_counted(self):
        for _ in range(auth.PASSKEY_MAX_ATTEMPTS + 5):
            self.offer()
        self.assertEqual(asyncio.run(db.fetch_value(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = 'passkey_ip'")),
            auth.PASSKEY_MAX_ATTEMPTS)


class TheSignInHintTests(PasskeyTestCase):
    """Whether this browser's sign-in page leads with a passkey: written by every
    sign-in to whether the account holds one (app/auth/cookies.py)."""

    def hinted(self) -> bool:
        return self.client.cookies.get(auth.SIGN_IN_HINT_SECURE) == "passkey"

    def test_every_session_cookie_is_set_with_the_answer(self):
        """The keyword is required, so a sign-in path that forgot it would fail
        when it ran; this says so for every caller before any of them runs."""
        app = Path(__file__).resolve().parents[2] / "app"
        calls = []
        for path in app.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for found in re.finditer(r"(?<!def )set_session_cookie\((.*?)\)\n", text, re.S):
                calls.append((path.name, "leads_with_passkey=" in found.group(1)))
        self.assertTrue(calls)
        self.assertEqual([name for name, passes in calls if not passes], [])

    def test_adding_sets_it_signing_out_keeps_it_and_the_last_removal_clears_it(self):
        self.sign_in_as(self.user_id)
        self.add(Authenticator())
        self.assertTrue(self.hinted())
        self.client.post("/logout", json={})
        self.assertTrue(self.hinted())
        self.sign_in_as(self.user_id)
        (row,) = self.stored()
        self.client.post("/me/passkeys/remove", json={"passkey": row["id"]})
        self.assertFalse(self.hinted())

    def test_each_sign_in_writes_whether_the_account_holds_one(self):
        device = self.holding()
        self.client.cookies.clear()
        self.client.post("/login", json={"username": "keyholder", "password": PASSWORD})
        self.assertTrue(self.hinted(), "a password sign-in of an account with a passkey")
        self.client.cookies.clear()
        self.sign_in(device)
        self.assertTrue(self.hinted(), "a passkey sign-in")
        self.make_user("plain", calendar_approved=True)
        self.client.post("/login", json={"username": "plain", "password": PASSWORD})
        self.assertFalse(self.hinted(), "an account with none clears it")

    def test_a_service_sign_in_writes_it_too(self):
        from app.auth import provider_login
        self.holding()
        for holds in (True, False):
            with self.subTest(holds=holds):
                if not holds:
                    asyncio.run(db.execute("DELETE FROM passkeys"))
                identity = auth.ProviderIdentity(provider="trakt", provider_user_id="svc-1",
                                                 display_name="k", access_token="t")
                asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
                    conn, user_id=self.user_id, provider="trakt", provider_user_id="svc-1",
                    access_token="t"))) if holds else None
                from fastapi import Response
                outcome = asyncio.run(provider_login.complete_provider_login(
                    identity=identity, handshake={"invite_token": None},
                    request=_FakeRequest(), settings=self.make_settings(), already_linked="x"))
                self.assertEqual(outcome.leads_with_passkey, holds)
                response = Response()
                provider_login.attach_session(response, outcome, self.make_settings(),
                                              _FakeRequest(hinted=True))
                written = response.headers.getlist("set-cookie")
                self.assertEqual(any("tns_signin=passkey" in c for c in written), holds)

    def test_the_page_with_it_leads_with_the_passkey_and_folds_the_password(self):
        plain = self.client.get("/login").text
        self.assertNotIn("passwordFold", plain)
        self.holding()
        self.client.cookies.set(auth.SIGN_IN_HINT_SECURE, "passkey")
        lead = self.client.get("/login").text
        self.assertLess(lead.index('hx-post="/login/passkey/offer"'), lead.index("auth-providers"))
        self.assertLess(lead.index("auth-providers"), lead.index('id="passwordFold"'))
        self.assertIn("<summary>Use your password instead</summary>", lead)

        def providers(page):
            start = page.index('<div class="auth-providers">')
            return page[start:page.index("</div>", start)]
        self.assertEqual(providers(plain), providers(lead))

    def test_it_is_ignored_where_passkeys_are_not_offered(self):
        save_settings(Settings(simkl_public_calendar_enabled=False))
        self.client.cookies.set(auth.SIGN_IN_HINT_SECURE, "passkey")
        page = self.client.get("/login").text
        self.assertNotIn("passwordFold", page)
        self.assertNotIn("/login/passkey/offer", page)

    def test_a_throttled_offer_opens_the_fold(self):
        self.client.cookies.set(auth.SIGN_IN_HINT_SECURE, "passkey")
        with patch.object(auth, "passkey_start_limited", return_value=True):
            resp, options = self.offer()
        self.assertIsNone(options)
        self.assertIn('id="passwordFold" open', unescape(resp.text))
        self.assertIn('hx-swap-oob="outerHTML"', unescape(resp.text))

    def test_a_refusal_on_the_leading_page_points_at_the_password(self):
        device = self.holding()
        self.client.cookies.set(auth.SIGN_IN_HINT_SECURE, "passkey")
        self.assertIn("use your password below",
                      self.sign_in(device, origin="https://evil.example").text)


class _FakeRequest:
    """Just enough of a request for the provider sign-in's tail."""

    def __init__(self, hinted: bool = False):
        self.headers = {"user-agent": "test"}
        self.cookies = {auth.SIGN_IN_HINT_SECURE: "passkey"} if hinted else {}
        self.client = type("Peer", (), {"host": "203.0.113.9"})()
        self.url = type("URL", (), {"scheme": "https"})()


class AWayInTests(PasskeyTestCase):

    def test_with_a_passkey_the_last_service_may_be_unlinked(self):
        asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?",
                               (self.user_id,)))
        self.link_identity(self.user_id, "trakt", 91, access_token="t")
        with self.assertRaises(auth.LastLoginMethod):
            asyncio.run(auth.unlink_identity(self.user_id, "trakt"))
        asyncio.run(db.execute(
            "INSERT INTO passkeys (user_id, credential_id, public_key, name, created_at) "
            "VALUES (?, 'c1', x'00', 'k', 1)", (self.user_id,)))
        self.sign_in_as(self.user_id)
        self.assertIn("unlink-trakt", unescape(self.client.get("/me").text))
        self.assertTrue(asyncio.run(auth.unlink_identity(self.user_id, "trakt")))

    def test_the_last_way_in_cannot_be_removed_and_is_not_offered(self):
        asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?",
                               (self.user_id,)))
        for n in (1, 2):
            asyncio.run(db.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, name, created_at) "
                "VALUES (?, ?, x'00', ?, 1)", (self.user_id, f"c{n}", f"Key {n}")))
        first, second = (r["id"] for r in self.stored())
        self.sign_in_as(self.user_id)
        self.assertEqual(self.client.get("/me").text.count('hx-post="/me/passkeys/remove"'), 2)
        self.client.post("/me/passkeys/remove", json={"passkey": first})
        page = self.client.get("/me").text
        self.assertNotIn('hx-post="/me/passkeys/remove"', page)
        resp = self.client.post("/me/passkeys/remove", json={"passkey": second})
        self.assertIn("only way you can sign in", unescape(resp.text))
        self.assertEqual(len(self.stored()), 1)

    def test_an_administrator_removes_the_last_one_only_when_forced(self):
        admin = self.make_user("boss", is_admin=True, calendar_approved=True)
        asyncio.run(db.execute("UPDATE users SET password_hash = NULL WHERE id = ?",
                               (self.user_id,)))
        asyncio.run(db.execute(
            "INSERT INTO passkeys (user_id, credential_id, public_key, name, created_at) "
            "VALUES (?, 'c1', x'00', 'Only', 1)", (self.user_id,)))
        (row,) = self.stored()
        self.sign_in_as(admin)
        page = self.client.get("/admin").text
        self.assertIn("passkey: Only", page)
        path = f"/admin/users/{self.user_id}/passkeys/remove"
        first = self.client.post(path, json={"passkey": str(row["id"])}).text
        self.assertIn("only way to sign in", first)
        self.assertIn('"force": "1"', unescape(first))
        self.assertEqual(len(self.stored()), 1)
        self.client.post(path, json={"passkey": str(row["id"]), "force": "1"})
        self.assertEqual(self.stored(), [])

    def test_a_deleted_account_takes_its_passkeys_with_it(self):
        admin = self.make_user("boss2", is_admin=True, calendar_approved=True)
        self.holding()
        asyncio.run(auth.delete_user(self.user_id, actor_user_id=admin))
        self.assertEqual(asyncio.run(db.fetch_value("SELECT COUNT(*) FROM passkeys")), 0)


class ANameForANewOneTests(PasskeyTestCase):

    def test_the_browser_and_system_it_was_added_from(self):
        cases = {
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101 Firefox/155.0":
                "Firefox on Windows",
            "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0 Mobile Safari/537.36": "Chrome on Android",
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0 Safari/537.36 Edg/153.0": "Edge on Windows",
            "": "Passkey",
        }
        for agent, name in cases.items():
            with self.subTest(agent=agent):
                self.assertEqual(passkeys.suggested_name(agent), name)

    def test_a_name_is_trimmed_and_never_empty(self):
        self.assertEqual(passkeys.clean_name("  My   phone \n"), "My phone")
        self.assertEqual(passkeys.clean_name(""), "Passkey")
        self.assertEqual(len(passkeys.clean_name("x" * 500)), passkeys.NAME_MAX)

    def test_a_passkey_can_be_renamed(self):
        self.sign_in_as(self.user_id)
        self.add(Authenticator(), name="Old")
        (row,) = self.stored()
        self.client.post("/me/passkeys/rename", json={"passkey": row["id"], "name": "New"})
        self.assertEqual(self.stored()[0]["name"], "New")
