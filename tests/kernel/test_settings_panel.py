"""The Settings panel as the server draws it, and the one writer behind its Save.

WHAT THIS FILE HOLDS THE PANEL TO, and each is a rule a regression would break
silently rather than loudly:

  - EVERY SETTING'S TYPE IS DECIDED IN ONE PLACE, config.apply_form. A form sends
    strings; a wrong coercion writes a wrong setting and the failure shows up
    later, somewhere else. Strings, integers (including the two typed in MB and
    GB), booleans (from a select, from a checkbox and its companion, and as JSON)
    and credentials both set and cleared are each driven through that writer and
    through the route, and read back.
  - NO RESPONSE CONTAINS A STORED SECRET — not in plaintext, and not as the
    ciphertext encryption stores. The literal strings are asserted absent.
  - EVERY WRITE REFUSES A FORM-ENCODED BODY (415), A NON-ADMINISTRATOR, AND A BAD
    VALUE — the last drawn as a sentence at 200 with nothing written, because a
    non-200 is never swapped.
  - EVERY CONTROL THAT WRITES CARRIES THE ENCODER, and a read inside a writing form
    opts out of it (the encoder throws on a GET's empty body and the request is
    silently dropped).

The device-code flow has its own file beside this one; the encryption section's
markup per state is tests/encryption/test_panel.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app import assets, config, db, secrets_box
from app.auth import encryption_flow
from app.config import Settings, load_settings, save_settings
from tests.support import AppTestCase, ORIGIN

PANEL = "/settings/fragments/panel"

# A distinctive value per credential, so a leak is unmistakable in a response
# body rather than something that might plausibly be a coincidence.
SECRET_VALUES = {name: f"SEKRIT-{name}-VALUE" for name in config.SECRET_FIELDS}

# Every route the panel posts to. Each one must refuse the three things below.
WRITES = (
    ("/settings", {"endpoint": "shows/new"}),
    ("/settings/arr-options", {"kind": "sonarr"}),
    ("/settings/trakt/device/start", {}),
    ("/settings/trakt/device/poll", {}),
    ("/settings/trakt/device/cancel", {}),
    ("/settings/trakt/refresh", {}),
    ("/settings/trakt/adopt", {}),
    ("/admin/encryption/enable", {"generate": "true"}),
    ("/admin/encryption/verify", {}),
    ("/admin/encryption/encrypt", {}),
    ("/admin/encryption/opt-out", {}),
)


def _set_key(key: str | None) -> None:
    if key is None:
        os.environ.pop(secrets_box.ENV_VAR, None)
    else:
        os.environ[secrets_box.ENV_VAR] = key
    secrets_box.reset_cache()


class PanelTestCase(AppTestCase):
    def make_settings(self):
        return dataclasses.replace(super().make_settings(), public_base_url=ORIGIN)

    def setUp(self):
        super().setUp()
        self.admin_id = self.make_user("admin_user", is_admin=True, calendar_approved=True)
        self.sign_in_as(self.admin_id)

    def tearDown(self):
        _set_key(None)
        encryption_flow._health = encryption_flow.HEALTHY
        super().tearDown()

    def panel(self, **headers) -> str:
        resp = self.client.get(PANEL, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text

    def save(self, **fields):
        return self.client.post("/settings", json=fields)


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------

class TheWriterDecidesEveryTypeTests(unittest.TestCase):
    """config.apply_form, asked directly — no route, no database."""

    def write(self, form: dict, current: Settings | None = None) -> Settings:
        return config.apply_form(current or Settings(), form)

    def test_a_string_arrives_as_typed_less_the_whitespace_around_it(self):
        settings = self.write({"sonarr_url": "  http://sonarr:8989 \n", "timezone": "Europe/London"})
        self.assertEqual(settings.sonarr_url, "http://sonarr:8989")
        self.assertEqual(settings.timezone, "Europe/London")

    def test_an_integer_arrives_from_the_string_a_form_sends(self):
        settings = self.write({"pagination_limit": "250", "calendar_cache_ttl_minutes": 25})
        self.assertEqual(settings.pagination_limit, 250)
        self.assertEqual(settings.calendar_cache_ttl_minutes, 25)

    def test_the_two_caps_are_typed_in_their_unit_and_stored_in_bytes(self):
        settings = self.write({"api_cache_max_bytes": "512", "poster_cache_max_bytes": "3"})
        self.assertEqual(settings.api_cache_max_bytes, 512 * 1024 * 1024)
        self.assertEqual(settings.poster_cache_max_bytes, 3 * 1024 * 1024 * 1024)

    def test_the_panel_shows_a_cap_in_the_unit_it_is_typed_in(self):
        shown = config.number_inputs(Settings(api_cache_max_bytes=2 * 1024 ** 3,
                                              poster_cache_max_bytes=7 * 1024 ** 3))
        self.assertEqual(shown["api_cache_max_bytes"].value, 2048)
        self.assertEqual(shown["poster_cache_max_bytes"].value, 7)

    def test_a_number_that_is_not_one_is_refused_rather_than_defaulted(self):
        """from_dict quietly drops an unreadable number for a hand-edited file;
        a form is somebody pressing Save, and they are told."""
        with self.assertRaisesRegex(config.SettingsFormError, "Pagination limit must be a whole number"):
            self.write({"pagination_limit": "three hundred"})
        with self.assertRaisesRegex(config.SettingsFormError, "whole number"):
            self.write({"cache_ttl_minutes": "2.5"})

    def test_a_number_outside_its_bounds_is_refused(self):
        with self.assertRaisesRegex(config.SettingsFormError, "between 16 and 65536"):
            self.write({"api_cache_max_bytes": "4"})
        with self.assertRaisesRegex(config.SettingsFormError, "between 1 and 1000"):
            self.write({"pagination_limit": "0"})

    def test_a_boolean_arrives_from_a_select_a_checkbox_and_json_alike(self):
        for sent, expected in (("true", True), ("false", False), (True, True), (False, False),
                               (["0", "1"], True), ("0", False), ("on", True)):
            with self.subTest(sent=sent):
                self.assertIs(self.write({"hide_not_watching": sent}).hide_not_watching, expected)

    def test_a_boolean_is_never_read_as_a_truthy_string(self):
        """The bug a per-field coercion in the browser used to guard against by
        hand: "false" and "0" are both truthy strings."""
        current = Settings(allow_open_registration=True)
        self.assertIs(self.write({"allow_open_registration": "false"}, current).allow_open_registration, False)

    def test_a_typed_credential_replaces_the_stored_one(self):
        current = Settings(tmdb_api_key="old-key")
        self.assertEqual(self.write({"tmdb_api_key": "new-key"}, current).tmdb_api_key, "new-key")

    def test_an_empty_credential_field_leaves_the_stored_one_alone(self):
        current = Settings(tmdb_api_key="old-key")
        for sent in ("", "   ", None):
            with self.subTest(sent=sent):
                self.assertEqual(self.write({"tmdb_api_key": sent}, current).tmdb_api_key, "old-key")

    def test_a_ticked_clear_box_removes_the_stored_credential(self):
        current = Settings(tmdb_api_key="old-key", sonarr_api_key="keep-me")
        settings = self.write({"tmdb_api_key": "", config.clear_field("tmdb_api_key"): "1"}, current)
        self.assertEqual(settings.tmdb_api_key, "")
        self.assertEqual(settings.sonarr_api_key, "keep-me")

    def test_a_value_typed_beside_a_ticked_clear_box_is_what_is_stored(self):
        """What somebody typed is the more specific instruction."""
        current = Settings(tmdb_api_key="old-key")
        settings = self.write({"tmdb_api_key": "typed", config.clear_field("tmdb_api_key"): "1"}, current)
        self.assertEqual(settings.tmdb_api_key, "typed")

    def test_a_setting_the_form_does_not_mention_is_left_alone(self):
        current = Settings(pagination_limit=123, sonarr_url="http://kept")
        settings = self.write({"endpoint": "shows/premieres"}, current)
        self.assertEqual((settings.pagination_limit, settings.sonarr_url), (123, "http://kept"))

    def test_the_network_floor_is_typed_as_one_line(self):
        self.assertEqual(self.write({"network_filter": "HBO, Netflix ,-BBC One"}).network_filter,
                         ["HBO", "Netflix", "-BBC One"])

    def test_a_base_url_with_a_path_is_refused(self):
        with self.assertRaisesRegex(config.SettingsFormError, "origin only"):
            self.write({"public_base_url": "https://shows.example.com/app"})

    def test_every_number_the_form_offers_is_a_declared_integer_setting(self):
        """A guard on the table: a name that is not an int field would be drawn
        and then silently ignored by the writer."""
        declared = config._scalar_field_types()
        for name in config.NUMBER_FIELDS:
            with self.subTest(field=name):
                self.assertIs(declared.get(name), int)


class RoundTripThroughTheRouteTests(PanelTestCase):
    """The same types, posted the way the panel posts them and read back out of
    both the store and the panel."""

    def test_a_string_round_trips(self):
        self.assertEqual(self.save(sonarr_url="http://sonarr:8989").status_code, 204)
        self.assertEqual(load_settings().sonarr_url, "http://sonarr:8989")
        self.assertIn('value="http://sonarr:8989"', self.panel())

    def test_an_integer_round_trips_in_its_typed_unit(self):
        self.assertEqual(self.save(api_cache_max_bytes="512", pagination_limit="250").status_code, 204)
        self.assertEqual(load_settings().api_cache_max_bytes, 512 * 1024 * 1024)
        page = self.panel()
        self.assertRegex(page, r'name="api_cache_max_bytes"[^>]*value="512"')
        self.assertRegex(page, r'name="pagination_limit"[^>]*value="250"')

    def test_a_boolean_round_trips_from_a_select(self):
        self.assertEqual(self.save(allow_open_registration="true").status_code, 204)
        self.assertIs(load_settings().allow_open_registration, True)
        self.assertRegex(self.panel(), r'<option value="true" selected>Open')

    def test_a_boolean_round_trips_from_a_checkbox_and_its_companion(self):
        self.assertEqual(self.save(hide_not_watching=["0", "1"]).status_code, 204)
        self.assertIs(load_settings().hide_not_watching, True)
        self.assertRegex(self.panel(), r'id="s_hide_not_watching"[^>]*checked')
        self.assertEqual(self.save(hide_not_watching="0").status_code, 204)
        self.assertIs(load_settings().hide_not_watching, False)

    def test_a_credential_is_set_and_then_cleared(self):
        self.assertEqual(self.save(tmdb_api_key="fresh-key").status_code, 204)
        self.assertEqual(load_settings().tmdb_api_key, "fresh-key")
        self.assertIn('name="clear:tmdb_api_key"', self.panel())
        self.assertEqual(self.save(**{"clear:tmdb_api_key": "1"}).status_code, 204)
        self.assertEqual(load_settings().tmdb_api_key, "")
        self.assertNotIn('name="clear:tmdb_api_key"', self.panel())

    def test_a_cleared_credential_leaves_no_row(self):
        self.save(tmdb_api_key="fresh-key")
        self.save(**{"clear:tmdb_api_key": "1"})
        self.assertIsNone(asyncio.run(db.fetch_value(
            "SELECT value FROM app_secrets WHERE name = 'tmdb_api_key'")))

    def test_a_certification_floor_is_written_in_the_filters_panels_own_spelling(self):
        resp = self.save(**{"chip:show_certifications:TV-MA": "exclude",
                            "chip:show_certifications:TV-14": "include",
                            "chip:show_certifications:TV-Y": ""})
        self.assertEqual(resp.status_code, 204, resp.text)
        from app.calendar import filter as calendar_filter
        self.assertEqual(calendar_filter.parse_spec(load_settings().show_certifications),
                         calendar_filter.parse_spec("TV-14, -TV-MA"))

    def test_a_successful_save_reloads_the_page_it_was_opened_from(self):
        resp = self.save(endpoint="shows/premieres")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers["HX-Refresh"], "true")


# ---------------------------------------------------------------------------
# no secret, ever
# ---------------------------------------------------------------------------

class NoStoredSecretIsDrawnTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        save_settings(dataclasses.replace(load_settings(), **SECRET_VALUES))

    def assert_no_secret(self, body: str, extra: tuple[str, ...] = ()) -> None:
        for value in (*SECRET_VALUES.values(), *extra):
            with self.subTest(value=value[:24]):
                self.assertNotIn(value, body)

    def test_the_panel_carries_no_credential_in_plaintext(self):
        self.assert_no_secret(self.panel())

    def test_a_stored_credential_says_it_is_set_and_offers_to_clear_it(self):
        page = self.panel()
        for name in ("trakt_client_secret", "sonarr_api_key", "tmdb_api_key"):
            with self.subTest(field=name):
                block = re.search(rf'<div class="field secret-field" id="secret-{name}".*?</div>', page, re.S)
                self.assertIsNotNone(block, name)
                self.assertIn("Saved — leave blank to keep it", block.group(0))
                self.assertIn(f'name="clear:{name}"', block.group(0))
                self.assertIn('value=""', block.group(0))

    def test_an_unset_credential_offers_nothing_to_clear(self):
        save_settings(Settings(**{**dataclasses.asdict(load_settings()), "seer_api_key": ""}),
                      clear_unset_secrets=True)
        block = re.search(r'id="secret-seer_api_key".*?</div>', self.panel(), re.S).group(0)
        self.assertIn('placeholder="Not set"', block)
        self.assertNotIn("clear:seer_api_key", block)

    def test_no_credential_appears_as_ciphertext_either(self):
        """With encryption on, what is stored is `enc:v1:` ciphertext — and that
        string must not be drawn any more than the plaintext is."""
        _set_key(Fernet.generate_key().decode())
        asyncio.run(encryption_flow.encrypt_now())
        stored = asyncio.run(db.fetch_all("SELECT value FROM app_secrets"))
        ciphertexts = tuple(row["value"] for row in stored)
        self.assertTrue(ciphertexts and all(v.startswith(secrets_box.PREFIX) for v in ciphertexts))
        self.assert_no_secret(self.panel(), ciphertexts)
        self.assert_no_secret(self.save(pagination_limit="nope").text, ciphertexts)

    def test_no_answer_a_panel_action_gives_carries_one(self):
        with patch("app.auth.trakt.refresh_access_token", side_effect=AssertionError("not reached")):
            for path, body in WRITES:
                if path == "/settings":
                    body = {"pagination_limit": "nope"}   # a refusal, so there is a body to read
                if path.startswith("/settings/trakt/device/start") or path.endswith("/refresh"):
                    body = {}
                with self.subTest(path=path):
                    with patch("app.auth.trakt.request_device_code",
                               side_effect=AssertionError("not reached")), \
                         patch("app.auth.trakt.poll_device_token",
                               side_effect=AssertionError("not reached")), \
                         patch("app.auth.trakt.fetch_account",
                               side_effect=AssertionError("not reached")), \
                         patch("app.integrations.arr.fetch_options",
                               return_value={"profiles": [], "folders": []}):
                        try:
                            text = self.client.post(path, json=body).text
                        except AssertionError:
                            continue
                    self.assert_no_secret(text)


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

class EveryWriteRefusesTests(PanelTestCase):
    def test_a_form_encoded_body_is_refused_before_it_reaches_a_route(self):
        for path, body in WRITES:
            with self.subTest(path=path):
                resp = self.client.post(path, data={k: str(v) for k, v in body.items()})
                self.assertEqual(resp.status_code, 415, resp.text)
        self.assertEqual(load_settings().endpoint, Settings().endpoint)

    def test_a_non_administrator_is_refused_everything_including_the_panel(self):
        self.client.cookies.clear()
        self.sign_in_as(self.make_user("viewer", calendar_approved=True))
        self.assertEqual(self.client.get(PANEL).status_code, 403)
        for path, body in WRITES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=body).status_code, 403)
        self.assertEqual(load_settings().endpoint, Settings().endpoint)

    def test_a_bad_value_is_drawn_as_a_sentence_and_nothing_is_written(self):
        before = dataclasses.asdict(load_settings())
        for fields, sentence in (
            ({"pagination_limit": "lots"}, "Pagination limit must be a whole number."),
            ({"public_base_url": "https://shows.example.com/app"}, "origin only"),
            ({"cookie_secure": "sometimes"}, "Session cookie security must be one of"),
            ({"poster_cache_max_bytes": "9999"}, "between 1 and 1024"),
        ):
            with self.subTest(fields=fields):
                resp = self.save(**fields, endpoint="shows/premieres")
                self.assertEqual(resp.status_code, 200)
                self.assertIn(sentence, resp.text)
                self.assertNotIn("HX-Refresh", resp.headers)
        self.assertEqual(dataclasses.asdict(load_settings()), before)

    def test_a_credential_save_is_refused_while_the_key_is_unhealthy_and_says_where_to_go(self):
        _set_key(Fernet.generate_key().decode())
        save_settings(dataclasses.replace(load_settings(), sonarr_api_key="sealed"))
        asyncio.run(encryption_flow.encrypt_now())
        _set_key(None)
        self.assertEqual(asyncio.run(encryption_flow.refresh_health()), encryption_flow.KEY_MISSING)
        resp = self.save(sonarr_api_key="new-key")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Encryption is unhealthy", resp.text)
        self.assertIn('href="/admin/encryption/recovery"', resp.text)
        # An ordinary setting still saves: those are never sealed.
        self.assertEqual(self.save(pagination_limit="250").status_code, 204)
        self.assertEqual(load_settings().pagination_limit, 250)


# ---------------------------------------------------------------------------
# the markup
# ---------------------------------------------------------------------------

class TheMarkupWritesThroughTheEncoderTests(PanelTestCase):
    PAGES = (PANEL, "/me", "/?month=1&year=2026")

    def test_every_control_that_writes_carries_the_encoder(self):
        """A control posting without hx-json-enc sends its native encoding and is
        refused by the shape guard, which reads to the person pressing it as
        nothing happening."""
        for element in re.findall(r"<(?:form|button|input|select|span|div)\b[^>]*hx-post[^>]*>", self.panel()):
            with self.subTest(element=element[:80]):
                self.assertIn("hx-json-enc", element)

    def test_a_read_inside_a_writing_form_opts_out_of_the_encoder(self):
        """MEASURED IN THE BROWSER on the rankings board: the encoder is switched on
        through inheritance, a GET has no body to encode, and the request is dropped
        with one console line."""
        for page in self.PAGES:
            html = self.client.get(page).text
            for form in re.findall(r"<form\b[^>]*hx-json-enc[^>]*>(.*?)</form>", html, re.S):
                for element in re.findall(r"<[a-z]+\b[^>]*hx-get[^>]*>", form):
                    with self.subTest(page=page, element=element[:80]):
                        self.assertIn('hx-json-enc="false"', element)

    def test_every_setting_names_the_form_and_nothing_else_does(self):
        """The form is empty and the fields point at it, so the controls that are
        not settings — the encryption section, the pairing, the loaders — cannot
        sweep every typed credential into their own request bodies."""
        page = self.panel()
        self.assertRegex(page, r'<form id="settingsForm"[^>]*></form>')
        for element in re.findall(r"<(?:input|select|textarea)\b[^>]*\bname=\"[^\"]+\"[^>]*>", page):
            name = re.search(r'\bname="([^"]+)"', element).group(1)
            with self.subTest(name=name):
                if name == "settings_tab":
                    self.assertNotIn("form=", element)
                else:
                    self.assertIn('form="settingsForm"', element)
        for element in re.findall(r"<[a-z]+\b[^>]*hx-(?:post|get)[^>]*>", page):
            with self.subTest(element=element[:80]):
                self.assertNotIn('form="settingsForm"', element)

    def test_the_form_does_not_ask_the_browser_to_validate_a_tab_it_cannot_show(self):
        self.assertRegex(self.panel(), r'<form id="settingsForm"[^>]*novalidate')


class TheTabsAreRadiosTests(PanelTestCase):
    def test_five_tabs_five_panels_and_the_first_is_chosen(self):
        page = self.panel()
        radios = re.findall(r'<input type="radio" name="settings_tab" id="stab_(\w+)"[^>]*>', page)
        self.assertEqual(radios, ["server", "trakt", "simkl", "calendar", "integrations"])
        self.assertEqual(re.findall(r'data-tab-panel="(\w+)"', page), radios)
        self.assertIn("checked", re.search(r'id="stab_server"[^>]*>', page).group(0))
        self.assertEqual(page.count(" checked>") and len(re.findall(
            r'name="settings_tab"[^>]*checked', page)), 1)

    def test_the_stylesheet_is_what_switches_them(self):
        css = (assets.BASE_DIR / assets.STYLESHEET).read_text(encoding="utf-8")
        for tab in ("server", "trakt", "simkl", "calendar", "integrations"):
            with self.subTest(tab=tab):
                self.assertIn(f'.settings-modal:has(#stab_{tab}:checked) [data-tab-panel="{tab}"]', css)

    def test_the_reconnect_notice_sits_above_the_tabs(self):
        """An alert that only appears on the tab you happen to be standing on is
        one you can miss."""
        page = self.panel()
        self.assertLess(page.index('id="traktReconnect"'), page.index('data-tab-panel="server"'))


class TheHintsAreTheServersSentencesTests(PanelTestCase):
    def test_the_proxy_hint_names_the_address_the_request_came_from(self):
        self.assertIn("This request came from testclient.", self.panel())

    def test_forwarded_headers_from_an_untrusted_peer_are_called_out(self):
        page = self.client.get(PANEL, headers={"X-Forwarded-For": "203.0.113.9"}).text
        self.assertIn("Forwarded headers ARE arriving but are being ignored", page)
        self.assertNotIn("ARE arriving", self.panel())

    def test_the_base_url_field_says_when_the_environment_overrides_it(self):
        self.assertNotIn("PUBLIC_BASE_URL environment variable is set", self.panel())
        with patch.dict(os.environ, {config.PUBLIC_BASE_URL_ENV: ORIGIN}):
            self.assertIn("PUBLIC_BASE_URL environment variable is set", self.panel())

    def test_the_cookie_warning_follows_the_scheme_the_browser_is_on(self):
        """Read off the browser's own Referer — the reading the save's refusal
        uses — so the warning and the refusal cannot disagree."""
        # A same-origin GET carries no Origin in a browser, only a Referer — which
        # is the header this has to work from, so the client's default Origin is
        # blanked here.
        https = self.panel(Origin="", Referer=f"{ORIGIN}/calendar")
        self.assertIn('data-cookie-hint="never"', https)
        self.assertNotIn('data-cookie-hint="always"', https)
        http = self.panel(Origin="", Referer="http://box.local:8000/calendar")
        self.assertIn('data-cookie-hint="always"', http)
        self.assertNotIn('data-cookie-hint="never"', http)

    def test_no_scheme_means_no_guessed_warning(self):
        page = self.client.get(PANEL, headers={"Origin": ""}).text
        self.assertNotIn('data-cookie-hint="never"', page)
        self.assertNotIn('data-cookie-hint="always"', page)

    def test_both_registration_warnings_are_drawn_for_the_stylesheet_to_choose(self):
        page = self.panel()
        for hint in ('data-registration-hint="closed"', 'data-registration-hint="open"',
                     'data-registration-hint="open-auto"', 'data-approval-hint="base"',
                     'data-approval-hint="open-auto"'):
            with self.subTest(hint=hint):
                self.assertIn(hint, page)

    def test_the_token_line_is_a_sentence_about_a_day(self):
        save_settings(dataclasses.replace(load_settings(), trakt_token_expires_at=4102444800))
        self.assertIn("Token valid until 1 January 2100", self.panel())


class TheMenuOffersSettingsToAdministratorsOnEveryPageTests(PanelTestCase):
    PAGES = ("/me", "/?month=1&year=2026", "/admin")

    def test_an_administrator_gets_the_entry_and_the_slot_everywhere(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                html = self.client.get(page).text
                menu = html.split('class="nav-menu-items"', 1)[1].split("</details>", 1)[0]
                self.assertIn('hx-get="/settings/fragments/panel"', menu)
                self.assertIn('<div id="settingsSlot"></div>', html)

    def test_anyone_else_gets_neither(self):
        self.client.cookies.clear()
        self.sign_in_as(self.make_user("viewer", calendar_approved=True))
        for page in ("/me", "/?month=1&year=2026"):
            with self.subTest(page=page):
                html = self.client.get(page).text
                self.assertNotIn("/settings/fragments/panel", html)
                self.assertNotIn("settingsSlot", html)

    def test_no_page_loads_a_settings_script(self):
        for page, scripts in assets.PAGE_SCRIPTS.items():
            for name in ("settings", "cert-picker", "share-links"):
                with self.subTest(page=page, name=name):
                    self.assertFalse(any(name in script for script in scripts))


class ArrOptionsTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        save_settings(dataclasses.replace(load_settings(), sonarr_url="http://saved:8989",
                                          sonarr_api_key="saved-key",
                                          sonarr_quality_profile_id=4,
                                          sonarr_root_folder="/tv"))

    def load(self, **fields):
        return self.client.post("/settings/arr-options", json={"kind": "sonarr", **fields})

    def test_the_saved_choice_is_drawn_before_anything_is_loaded(self):
        block = re.search(r'id="arrOptions-sonarr".*?</button>', self.panel(), re.S).group(0)
        self.assertIn('<option value="4" selected>Profile #4</option>', block)
        self.assertIn('<option value="/tv" selected>/tv</option>', block)

    def test_the_services_lists_are_drawn_as_options_keeping_the_choice(self):
        async def options(kind, url, key):
            return {"profiles": [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}],
                    "folders": [{"path": "/tv"}, {"path": "/anime"}]}
        with patch("app.integrations.arr.fetch_options", side_effect=options):
            resp = self.load(sonarr_quality_profile_id="4", sonarr_root_folder="/anime")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<option value="4" selected>HD-1080p</option>', resp.text)
        self.assertIn('<option value="/anime" selected>/anime</option>', resp.text)
        self.assertIn("Sonarr options loaded.", resp.text)

    def test_a_typed_url_is_used_beside_a_saved_key(self):
        """It used to fall back to BOTH saved values when either was blank, so a new
        URL typed beside a saved key asked the old server."""
        asked = []

        async def options(kind, url, key):
            asked.append((url, key))
            return {"profiles": [], "folders": []}
        with patch("app.integrations.arr.fetch_options", side_effect=options):
            self.load(sonarr_url="http://new:8989", sonarr_api_key="")
        self.assertEqual(asked, [("http://new:8989", "saved-key")])

    def test_a_service_that_cannot_be_reached_is_a_sentence(self):
        import httpx
        with patch("app.integrations.arr.fetch_options", side_effect=httpx.ConnectError("no")):
            resp = self.load()
        self.assertIn("reach Sonarr at http://saved:8989 — check the URL and API key.", resp.text)
        self.assertIn('<option value="4" selected>', resp.text)

    def test_nothing_to_ask_with_means_nothing_is_asked(self):
        save_settings(Settings(**{**dataclasses.asdict(load_settings()), "sonarr_url": "",
                                  "sonarr_api_key": ""}), clear_unset_secrets=True)
        with patch("app.integrations.arr.fetch_options", side_effect=AssertionError("reached")):
            resp = self.load()
        self.assertIn("Enter the Sonarr URL and API key first.", resp.text)


if __name__ == "__main__":
    unittest.main()
