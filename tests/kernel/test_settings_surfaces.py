"""The Settings screen's own surfaces, and the error page.

Each of these is a control over a setting whose behaviour is proven elsewhere:
what a non-admin may set for themselves, the cache and prewarm widgets reading
and writing the values app/config.py declares, the tab strip, and unlinking a
provider revoking the token as it goes. The error page is here because it is
the one page every route can reach and nothing else owns it.

No network: token revocation is patched wherever an unlink runs.
"""
from __future__ import annotations

import unittest
import asyncio
import dataclasses
import re
from unittest.mock import patch

from app import auth, cache, chrome, db
from app.main import app
from app.config import Settings
from tests.support import load_settings, save_settings
from tests.support import AppTestCase, ORIGIN


class SettingsSurfaceTestCase(AppTestCase):
    def make_settings(self):
        # The configured origin has to match the one the client speaks, or the
        # cross-site rules refuse every save below for an unrelated reason.
        # Built ON the shared fixture rather than beside it, so whatever "an
        # instance with nothing set up" has to mean stays stated once (see
        # tests/support.py — a source needing no credential is one of them).
        return dataclasses.replace(super().make_settings(), public_base_url=ORIGIN)

    def setUp(self):
        super().setUp()
        self.admin_id = self._make_user("admin_user", is_admin=True, calendar_approved=True)

    def _make_user(self, username, password="hunter2hunter2", **flags) -> int:
        return self.make_user(username, password, **flags)

    def sign_in_as(self, user_id: int) -> None:
        """As the shared one, but starting from an empty jar — these tests move
        between accounts and a stale cookie of another kind would travel."""
        self.client.cookies.clear()
        super().sign_in_as(user_id)
    def _link_trakt(self, user_id: int, provider_user_id: int, token: str | None = "tok") -> None:
        self.link_identity(user_id, "trakt", provider_user_id, token)



class SelfServiceCredentialsTests(SettingsSurfaceTestCase):
    """Claiming a username and setting a password without an administrator.

    Both forms answer with the credentials section, a refusal drawn in its line
    at 200 — a statused refusal is never swapped."""

    def _refusal(self, resp) -> str:
        self.assertEqual(resp.status_code, 200, resp.text)
        return re.search(r'<p class="auth-error[^"]*" role="alert">(.*?)</p>', resp.text, re.S).group(1)

    def test_an_oauth_only_account_can_claim_a_username(self):
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4141)
        self.sign_in_as(user_id)
        resp = self.client.post("/me/username", json={"username": "claimed"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Username saved.", resp.text)
        self.assertEqual(asyncio.run(auth.get_user(user_id))["username"], "claimed")
        # The form is gone, and the names and the heading beside it follow out of band.
        self.assertNotIn('id="newUsername"', resp.text)
        self.assertRegex(resp.text, r'id="accountNames" hx-swap-oob="outerHTML"')
        self.assertRegex(resp.text, r'<h1 id="accountHeading" hx-swap-oob="outerHTML">claimed</h1>')

    def test_changing_an_existing_username_is_not_self_service(self):
        """A username is a public identifier — it is what /u/<name> links are
        built from — so handing it over would break links already shared and
        free the old name for someone else."""
        user_id = self._make_user("settled", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/me/username", json={"username": "different"})
        self.assertIn("You already have a username", self._refusal(resp))
        self.assertEqual(asyncio.run(auth.get_user(user_id))["username"], "settled")

    def test_a_taken_or_reserved_username_is_refused(self):
        self._make_user("taken_name", calendar_approved=True)
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4242)
        self.sign_in_as(user_id)
        for name in ("taken_name", "admin", "!!"):
            with self.subTest(name=name):
                resp = self.client.post("/me/username", json={"username": name})
                self.assertTrue(self._refusal(resp))
        self.assertIsNone(asyncio.run(auth.get_user(user_id))["username"])

    def test_a_username_may_not_collide_with_someone_elses_slug(self):
        """The cross-namespace rule holds on this path too, not just on
        registration."""
        owner = self._make_user("slugowner", calendar_approved=True)
        self.sign_in_as(owner)
        self.client.post("/calendar/share/slug", json={"slug": "wanted-name"})

        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4343)
        self.sign_in_as(user_id)
        self.assertTrue(self._refusal(
            self.client.post("/me/username", json={"username": "wanted-name"})))
        self.assertIsNone(asyncio.run(auth.get_user(user_id))["username"])

    def test_an_oauth_only_account_can_set_a_first_password(self):
        """No current password is asked for, because there is none — the live
        session is the only credential such an account has, and demanding one
        would make this unreachable for exactly the accounts that need it."""
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4444)
        self.sign_in_as(user_id)
        resp = self.client.post("/me/password", json={
            "password": "brand-new-secret", "password_confirm": "brand-new-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "brand-new-secret")).ok)
        # The section is drawn for an account that now HAS a password.
        self.assertIn('id="currentPassword"', resp.text)

    def test_changing_a_password_requires_the_current_one(self):
        user_id = self._make_user("haspw", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/me/password", json={
            "current_password": "wrong-one-entirely",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertIn("isn", self._refusal(resp))
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "hunter2hunter2")).ok)

    def test_the_right_current_password_changes_it(self):
        user_id = self._make_user("haspw2", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/me/password", json={
            "current_password": "hunter2hunter2",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Password saved.", resp.text)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "replacement-secret")).ok)

    def test_a_mismatch_or_a_short_password_is_refused(self):
        user_id = self._make_user("haspw3", calendar_approved=True)
        self.sign_in_as(user_id)
        for body, sentence in (
                ({"password": "long-enough-here", "password_confirm": "something-else"},
                 "The two passwords don&#39;t match."),
                ({"password": "short", "password_confirm": "short"},
                 f"Use at least {auth.MIN_PASSWORD_LENGTH} characters.")):
            with self.subTest(body=body):
                resp = self.client.post("/me/password",
                                        json={"current_password": "hunter2hunter2", **body})
                self.assertEqual(self._refusal(resp), sentence)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "hunter2hunter2")).ok)

    def test_the_current_password_check_stays_rate_limited(self):
        """A guess-checking oracle, throttled on the shared per-address counter —
        and still throttled now that its refusal is a sentence."""
        user_id = self._make_user("guessed", calendar_approved=True)
        self.sign_in_as(user_id)
        for _ in range(auth.LOGIN_IP_MAX_ATTEMPTS):
            asyncio.run(auth.record_attempt("ip", "testclient", False))
        resp = self.client.post("/me/password", json={
            "current_password": "hunter2hunter2",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(self._refusal(resp), "Too many attempts. Try again later.")
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "hunter2hunter2")).ok)

    def test_a_password_change_evicts_other_sessions_but_not_this_one(self):
        """A change after a compromise has to actually remove the other party,
        and signing the person out of the tab they just used would read as a
        failure."""
        user_id = self._make_user("evicter", calendar_approved=True)
        elsewhere = asyncio.run(auth.create_session(user_id))
        self.sign_in_as(user_id)

        resp = self.client.post("/me/password", json={
            "current_password": "hunter2hunter2",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertIsNone(asyncio.run(auth.validate_session(elsewhere)))
        # Still signed in here, on the cookie the response reissued.
        self.assertEqual(self.client.get("/me").status_code, 200)

    def test_both_routes_need_a_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/me/username", json={"username": "x"}).status_code, 401)
        self.assertEqual(self.client.post("/me/password", json={"password": "y"}).status_code, 401)

    def test_the_account_page_offers_the_forms_that_apply(self):
        oauth_only = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(oauth_only, provider_user_id=4545)
        self.sign_in_as(oauth_only)
        body = self.client.get("/me").text
        self.assertIn('hx-post="/me/username"', body)
        self.assertIn("Set a password", body)
        self.assertNotIn('id="currentPassword"', body)

        self.sign_in_as(self._make_user("named", calendar_approved=True))
        body = self.client.get("/me").text
        self.assertNotIn('hx-post="/me/username"', body)
        self.assertIn('id="currentPassword"', body)


class CacheSettingsWidgetTests(SettingsSurfaceTestCase):
    """The two cache settings that had no control on the admin screen."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_the_settings_panel_renders_both_inputs(self):
        body = self.client.get("/settings/fragments/panel").text
        self.assertIn('name="calendar_cache_ttl_minutes"', body)
        self.assertIn('name="api_cache_max_bytes"', body)

    def test_saving_them_persists(self):
        """The cap is TYPED in megabytes and stored in bytes — the writer's
        conversion, not the browser's."""
        resp = self.client.post("/settings", json={
            "calendar_cache_ttl_minutes": "25",
            "api_cache_max_bytes": "512",
        })
        self.assertEqual(resp.status_code, 204, resp.text)
        settings = load_settings()
        self.assertEqual(settings.calendar_cache_ttl_minutes, 25)
        self.assertEqual(settings.api_cache_max_bytes, 512 * 1024 * 1024)

    def test_they_are_drawn_with_their_stored_values(self):
        """A field drawn without its value renders blank and then saves a
        refusal, so the panel carries what is stored."""
        save_settings(dataclasses.replace(load_settings(), calendar_cache_ttl_minutes=33))
        body = self.client.get("/settings/fragments/panel").text
        self.assertRegex(body, r'name="calendar_cache_ttl_minutes"[^>]*value="33"')
        self.assertRegex(body, r'name="api_cache_max_bytes"[^>]*value="1024"')


class CheckboxSettingCoercionTests(SettingsSurfaceTestCase):
    """A checkbox setting's round trip through the Settings form, coerced to a
    real bool by the one writer (config.apply_form).

    IT USED TO BE `calendar_prewarm_enabled` THAT CARRIED THIS, and that setting
    is gone — the scheduled month refresh replaced the pre-warm it gated, and a
    flag whose only job was to enable something that no longer exists would be a
    switch with nothing behind it. The coercion is what mattered here and it is
    not specific to that setting, so it moves to one that remains rather than
    being deleted with it.
    """

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_the_settings_panel_renders_the_toggle(self):
        body = self.client.get("/settings/fragments/panel").text
        self.assertIn('name="hide_not_watching"', body)

    def test_saving_it_persists_as_a_bool(self):
        resp = self.client.post("/settings", json={"hide_not_watching": True})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertIs(load_settings().hide_not_watching, True)

        resp = self.client.post("/settings", json={"hide_not_watching": False})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertIs(load_settings().hide_not_watching, False)

    def test_a_checkbox_and_its_companion_coerce_to_a_bool(self):
        """A form posts the hidden companion's "0" and, when ticked, the box's
        "1" under the same name; the writer must still store a real bool."""
        resp = self.client.post("/settings", json={"hide_not_watching": ["0", "1"]})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertIs(load_settings().hide_not_watching, True)

    def test_it_is_drawn_ticked_once_it_is_on(self):
        self.client.post("/settings", json={"hide_not_watching": "1"})
        self.assertRegex(self.client.get("/settings/fragments/panel").text,
                         r'id="s_hide_not_watching"[^>]*checked')


class SimklPublicCalendarSettingWidgetTests(SettingsSurfaceTestCase):
    """simkl_public_calendar_enabled: the checkbox on the Simkl tab and its
    round trip through the Settings form, defaulting to True the way an instance
    that never opens this tab needs it to.

    THE STORED KEY KEEPS ITS ORIGINAL SPELLING while the label says something
    different, and that is deliberate: the key is what the value is saved under
    in app_settings, so renaming it would orphan every operator's existing
    choice and quietly reset it to the default."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def panel(self) -> str:
        return self.client.get("/settings/fragments/panel").text

    def test_the_settings_panel_renders_the_toggle(self):
        self.assertIn('name="simkl_public_calendar_enabled"', self.panel())

    def test_the_label_says_what_the_setting_does(self):
        """It governs whether Simkl contributes to the calendar, full stop —
        not only whether an unconfigured Simkl may be reached, which was a
        qualifier that almost never failed to apply and so said nothing."""
        self.assertIn("Include Simkl results on the calendar", self.panel())

    def test_it_defaults_on(self):
        """An instance that never saves this field must read it back as True —
        the whole point of the default is that nobody has to go find it."""
        # ASKED OF THE DECLARATION, NOT OF THE FIXTURE. This suite's shared
        # settings switch this very field OFF (see tests/support.py — otherwise
        # every page render in it would reach a real CDN), so round-tripping it
        # through the saved instance would measure that choice instead of the
        # default it is about.
        self.assertIs(Settings().simkl_public_calendar_enabled, True)
        drawn_ticked = bool(re.search(r'id="s_simkl_public_calendar_enabled"[^>]*checked', self.panel()))
        self.assertIs(drawn_ticked, load_settings().simkl_public_calendar_enabled)

    def test_saving_it_persists_as_a_bool(self):
        resp = self.client.post("/settings", json={"simkl_public_calendar_enabled": ["0"]})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertIs(load_settings().simkl_public_calendar_enabled, False)

        resp = self.client.post("/settings", json={"simkl_public_calendar_enabled": ["0", "1"]})
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertIs(load_settings().simkl_public_calendar_enabled, True)


class SettingsTabsTests(SettingsSurfaceTestCase):
    """Settings is a handful of tabbed groups over one form."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def _body(self) -> str:
        return self.client.get("/settings/fragments/panel").text

    def test_every_tab_has_a_panel_and_only_the_first_is_chosen(self):
        body = self._body()
        tabs = re.findall(r'data-tab="([\w-]+)"', body)
        panels = re.findall(r'data-tab-panel="([\w-]+)"', body)
        self.assertEqual(tabs, ["server", "trakt", "simkl", "calendar", "integrations"])
        self.assertEqual(panels, tabs)
        # The tab showing is the radio that is checked, and exactly one is.
        self.assertEqual(re.findall(r'name="settings_tab" id="stab_(\w+)"[^>]*checked', body),
                         ["server"])

    def test_no_field_was_dropped_on_the_way_into_the_tabs(self):
        """A field left out of the panel would never be saved again, and one
        drawn without its form would never be sent."""
        body = self._body()
        for name in ("public_base_url", "trusted_proxy_ips", "trakt_client_id",
                     "trakt_client_secret", "trakt_access_token", "timezone", "endpoint",
                     "pagination_limit", "cache_ttl_minutes", "calendar_cache_ttl_minutes",
                     "api_cache_max_bytes", "hide_not_watching", "sonarr_url", "sonarr_api_key",
                     "radarr_url", "radarr_api_key", "seer_url", "seer_api_key", "tmdb_api_key",
                     "simkl_client_id", "simkl_client_secret",
                     "simkl_public_calendar_enabled"):
            with self.subTest(field=name):
                self.assertRegex(body, rf'id="s_{name}" name="{name}"[^>]*form="settingsForm"')

    def test_the_reconnect_notice_sits_outside_the_tabs(self):
        """It is an alert about the instance, and an alert that only appears on
        the tab you happen to be standing on is one you can miss."""
        body = self._body()
        notice = body.index('id="traktReconnect"')
        first_panel = body.index('data-tab-panel="server"')
        self.assertLess(notice, first_panel)

    def test_one_save_still_writes_fields_from_several_tabs(self):
        """Tabs are presentation only — every field names the one form, so a
        value from the Server tab and one from Integrations go together."""
        resp = self.client.post("/settings", json={
            "public_base_url": ORIGIN, "sonarr_url": "http://localhost:8989",
            "calendar_cache_ttl_minutes": "30",
        })
        self.assertEqual(resp.status_code, 204, resp.text)
        settings = load_settings()
        self.assertEqual(settings.sonarr_url, "http://localhost:8989")
        self.assertEqual(settings.calendar_cache_ttl_minutes, 30)


class ErrorPageTests(SettingsSurfaceTestCase):
    """A mistyped address gets a page, not Starlette's raw JSON."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_a_browser_gets_the_themed_page(self):
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("Let's all go to the lobby", resp.text)
        self.assertIn("Back to the calendar", resp.text)

    def test_the_lobby_page_carries_our_own_favicon(self):
        """The page came from somewhere else. Its own icon did not come with it."""
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertIn("/static/images/distrakkl-favicon.ico", resp.text)
        self.assertIn("/static/images/favicon-32.png", resp.text)

    def test_the_lobby_page_carries_the_wordmark_drained_of_colour(self):
        """A dead end still says whose app it is. The wordmark is the app's own
        gold-to-crimson artwork and every other asset on this page is greyscale,
        so it is desaturated to match rather than left as the one saturated thing
        on a monochrome stage — asserted because a filter silently dropped in a
        later edit would not fail anything else."""
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertIn("/static/images/distrakkl-wordmark.svg", resp.text)
        self.assertIn("grayscale(1)", resp.text)

    def test_the_lobby_page_names_the_product(self):
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertIn(chrome.PRODUCT_NAME, resp.text)

    def test_the_lobby_page_stands_up_without_the_stylesheet(self):
        """Styles are inline on purpose: the page that renders when something is
        already wrong cannot depend on a second request succeeding."""
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertNotIn("css/style.css", resp.text)

    def test_a_refused_page_keeps_the_plain_card(self):
        """403 is not a place to be charming. It also keeps error.html wired to
        something, so the fallback cannot rot unnoticed while the lobby page is
        the one everybody looks at.

        Driven through the handler rather than a URL because the routes that
        refuse a signed-in reader answer by rendering their own page, not by
        raising — there is no request that reaches this branch with a 403.
        """
        from fastapi import Request
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from app.main import handle_http_exception

        request = Request({"type": "http", "method": "GET", "path": "/nope",
                           "headers": [(b"accept", b"text/html")],
                           "query_string": b"", "app": app})
        resp = asyncio.run(handle_http_exception(
            request, StarletteHTTPException(status_code=403)))
        self.assertEqual(resp.status_code, 403)
        body = resp.body.decode()
        self.assertIn("error-card", body)
        self.assertNotIn("Let's all go to the lobby", body)

    def test_a_script_still_gets_json(self):
        """fetch() sends Accept: */*, and a caller that parses the body has to
        keep getting something parseable."""
        resp = self.client.get("/no-such-page")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("application/json", resp.headers["content-type"])
        self.assertFalse(resp.json()["ok"])

    def test_a_wrong_method_reads_as_not_found(self):
        """Answering "that exists but not like that" tells a stranger which
        addresses are real."""
        resp = self.client.post("/no-such-page", json={}, headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("405", resp.text)

    def test_the_page_names_no_route_and_offers_no_inventory(self):
        """It says the same thing for a never-existed path and for one that is
        simply not this account's to open."""
        secret = self.client.get("/api/admin/hidden-thing", headers={"Accept": "text/html"}).text
        typo = self.client.get("/calender", headers={"Accept": "text/html"}).text
        self.assertNotIn("hidden-thing", secret)
        self.assertNotIn("calender", typo)

    def test_the_share_pages_keep_their_own_wording(self):
        """A dead share link says so specifically — it is a different question
        from a mistyped address, and the answer is more useful."""
        resp = self.client.get("/s/not-a-real-token", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("shared calendar", resp.text)


class TokenRevocationOnUnlinkTests(SettingsSurfaceTestCase):
    """Unlinking asks Trakt to forget the authorization rather than leaving it
    standing in the user's connected-apps list."""

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            public_base_url=ORIGIN, trakt_client_id="cid", trakt_client_secret="secret"))
        self.revoked: list[str] = []

        async def _revoke(client_id, client_secret, access_token):
            self.revoked.append(access_token)

        patcher = patch("app.auth.trakt.revoke_token", side_effect=_revoke)
        self.revoke_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_admin_unlink_revokes_too(self):
        victim = self._make_user("victim", calendar_approved=True)
        self._link_trakt(victim, provider_user_id=5151, token="victim-token")
        self.sign_in_as(self.admin_id)
        resp = self.client.post(
            f"/admin/users/{victim}/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Unlinked Trakt.", resp.text)
        self.assertEqual(self.revoked, ["victim-token"])

    def test_an_admin_unlink_that_asks_for_confirmation_revokes_nothing_yet(self):
        """The first call comes back asking for `force` and the identity stays.
        Killing its token on the way past would leave the account linked to a
        credential that no longer works."""
        orphan = self._make_user(None, password=None)
        self._link_trakt(orphan, provider_user_id=5252, token="orphan-token")
        self.sign_in_as(self.admin_id)
        resp = self.client.post(
            f"/admin/users/{orphan}/identities/unlink", json={"provider": "trakt"})
        self.assertIn("Unlink anyway", resp.text)
        self.assertEqual(self.revoked, [])
        self.assertEqual(len(asyncio.run(auth.list_identities(orphan))), 1)

        forced = self.client.post(
            f"/admin/users/{orphan}/identities/unlink",
            json={"provider": "trakt", "force": "1"})
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertEqual(self.revoked, ["orphan-token"])

    def test_unlinking_plex_asks_trakt_nothing(self):
        user_id = self._make_user("plexy", calendar_approved=True)
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="plex", provider_user_id=6161, access_token="p")))
        self.sign_in_as(user_id)
        resp = self.client.post("/me/identities/unlink", json={"provider": "plex"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(asyncio.run(auth.list_identities(user_id)), [])
        self.assertEqual(self.revoked, [])

    def test_without_configured_credentials_it_says_so_rather_than_calling(self):
        """Revocation is authenticated with the app's own credentials. Without
        them there is no call to make, and saying so beats silently doing
        nothing."""
        save_settings(Settings(public_base_url=ORIGIN))
        user_id = self._make_user("uncfg", calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=7171, token="stranded")
        self.sign_in_as(user_id)
        resp = self.client.post("/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, [])
        # Said beside the rows, since only the account holder can finish it.
        identities = resp.text.split('id="accountIdentities"', 1)[1]
        line = re.search(r'<p class="auth-error[^"]*" role="alert">(.*?)</p>', identities, re.S).group(1)
        self.assertIn("trakt.tv", line)


if __name__ == "__main__":
    unittest.main()
