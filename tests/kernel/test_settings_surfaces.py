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
import re
from unittest.mock import patch

from app import auth, cache, db
from app.main import app
from app.config import Settings, load_settings, save_settings
from tests.support import AppTestCase, ORIGIN


class SettingsSurfaceTestCase(AppTestCase):
    def make_settings(self):
        # The configured origin has to match the one the client speaks, or the
        # cross-site rules refuse every save below for an unrelated reason.
        return Settings(public_base_url=ORIGIN)

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
    """Claiming a username and setting a password without an administrator."""

    def test_an_oauth_only_account_can_claim_a_username(self):
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4141)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/username", json={"username": "claimed"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(asyncio.run(auth.get_user(user_id))["username"], "claimed")

    def test_changing_an_existing_username_is_not_self_service(self):
        """A username is a public identifier — it is what /u/<name> links are
        built from — so handing it over would break links already shared and
        free the old name for someone else."""
        user_id = self._make_user("settled", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/username", json={"username": "different"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(asyncio.run(auth.get_user(user_id))["username"], "settled")

    def test_a_taken_or_reserved_username_is_refused(self):
        self._make_user("taken_name", calendar_approved=True)
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4242)
        self.sign_in_as(user_id)
        for name in ("taken_name", "admin", "!!"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.post("/api/me/username", json={"username": name}).status_code, 400)

    def test_a_username_may_not_collide_with_someone_elses_slug(self):
        """The cross-namespace rule holds on this path too, not just on
        registration."""
        owner = self._make_user("slugowner", calendar_approved=True)
        self.sign_in_as(owner)
        self.client.post("/api/me/share/slug", json={"slug": "wanted-name"})

        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4343)
        self.sign_in_as(user_id)
        self.assertEqual(
            self.client.post("/api/me/username", json={"username": "wanted-name"}).status_code, 400)

    def test_an_oauth_only_account_can_set_a_first_password(self):
        """No current password is asked for, because there is none — the live
        session is the only credential such an account has, and demanding one
        would make this unreachable for exactly the accounts that need it."""
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4444)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/password", json={
            "password": "brand-new-secret", "password_confirm": "brand-new-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "brand-new-secret")).ok)

    def test_changing_a_password_requires_the_current_one(self):
        user_id = self._make_user("haspw", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/password", json={
            "current_password": "wrong-one-entirely",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 403)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "hunter2hunter2")).ok)

    def test_the_right_current_password_changes_it(self):
        user_id = self._make_user("haspw2", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/password", json={
            "current_password": "hunter2hunter2",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "replacement-secret")).ok)

    def test_a_mismatch_or_a_short_password_is_refused(self):
        user_id = self._make_user("haspw3", calendar_approved=True)
        self.sign_in_as(user_id)
        for body in ({"password": "long-enough-here", "password_confirm": "something-else"},
                     {"password": "short", "password_confirm": "short"}):
            with self.subTest(body=body):
                resp = self.client.post("/api/me/password",
                                        json={"current_password": "hunter2hunter2", **body})
                self.assertEqual(resp.status_code, 400)

    def test_a_password_change_evicts_other_sessions_but_not_this_one(self):
        """A change after a compromise has to actually remove the other party,
        and signing the person out of the tab they just used would read as a
        failure."""
        user_id = self._make_user("evicter", calendar_approved=True)
        elsewhere = asyncio.run(auth.create_session(user_id))
        self.sign_in_as(user_id)

        resp = self.client.post("/api/me/password", json={
            "current_password": "hunter2hunter2",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertIsNone(asyncio.run(auth.validate_session(elsewhere)))
        # Still signed in here, on the cookie the response reissued.
        self.assertEqual(self.client.get("/me").status_code, 200)

    def test_both_routes_need_a_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/me/username", json={"username": "x"}).status_code, 401)
        self.assertEqual(self.client.post("/api/me/password", json={"password": "y"}).status_code, 401)

    def test_the_account_page_offers_the_forms_that_apply(self):
        oauth_only = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(oauth_only, provider_user_id=4545)
        self.sign_in_as(oauth_only)
        body = self.client.get("/me").text
        self.assertIn('id="usernameForm"', body)
        self.assertIn("Set a password", body)
        self.assertNotIn('id="currentPassword"', body)

        self.sign_in_as(self._make_user("named", calendar_approved=True))
        body = self.client.get("/me").text
        self.assertNotIn('id="usernameForm"', body)
        self.assertIn('id="currentPassword"', body)


class CacheSettingsWidgetTests(SettingsSurfaceTestCase):
    """The two cache settings that had no control on the admin screen."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_the_settings_screen_renders_both_inputs(self):
        body = self.client.get("/?month=1&year=2026").text
        self.assertIn('name="calendar_cache_ttl_minutes"', body)
        self.assertIn('name="api_cache_max_bytes"', body)

    def test_saving_them_persists(self):
        resp = self.client.post("/api/settings", json={
            "calendar_cache_ttl_minutes": 25,
            "api_cache_max_bytes": 512 * 1024 * 1024,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        settings = load_settings()
        self.assertEqual(settings.calendar_cache_ttl_minutes, 25)
        self.assertEqual(settings.api_cache_max_bytes, 512 * 1024 * 1024)

    def test_they_are_readable_back_through_the_settings_endpoint(self):
        """The screen loads its values from here, so a field missing from the
        response is a field that renders blank and then saves a zero."""
        payload = self.client.get("/api/settings").json()
        self.assertIn("calendar_cache_ttl_minutes", payload)
        self.assertIn("api_cache_max_bytes", payload)


class PrewarmSettingWidgetTests(SettingsSurfaceTestCase):
    """calendar_prewarm_enabled: the checkbox and its round trip through
    /api/settings, coerced to a real bool the way hide_not_watching is."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_the_settings_screen_renders_the_toggle(self):
        body = self.client.get("/?month=1&year=2026").text
        self.assertIn('name="calendar_prewarm_enabled"', body)

    def test_saving_it_persists_as_a_bool(self):
        resp = self.client.post("/api/settings", json={"calendar_prewarm_enabled": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        settings = load_settings()
        self.assertIs(settings.calendar_prewarm_enabled, True)

        resp = self.client.post("/api/settings", json={"calendar_prewarm_enabled": False})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(load_settings().calendar_prewarm_enabled, False)

    def test_a_checkbox_style_string_value_coerces_to_a_bool(self):
        """A form posts "true"/"false", not a JSON boolean; _as_bool must still
        turn that into a real bool rather than storing the truthy string."""
        resp = self.client.post("/api/settings", json={"calendar_prewarm_enabled": "true"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(load_settings().calendar_prewarm_enabled, True)

    def test_it_is_readable_back_through_the_settings_endpoint(self):
        payload = self.client.get("/api/settings").json()
        self.assertIn("calendar_prewarm_enabled", payload)


class SimklPublicCalendarSettingWidgetTests(SettingsSurfaceTestCase):
    """simkl_public_calendar_enabled: the checkbox on the Simkl tab and its
    round trip through /api/settings, defaulting to True the way an instance
    that never opens this tab needs it to."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_the_settings_screen_renders_the_toggle(self):
        body = self.client.get("/?month=1&year=2026").text
        self.assertIn('name="simkl_public_calendar_enabled"', body)

    def test_it_defaults_on(self):
        """An instance that never saves this field must read it back as True —
        the whole point of the default is that nobody has to go find it."""
        payload = self.client.get("/api/settings").json()
        self.assertIs(payload["simkl_public_calendar_enabled"], True)

    def test_saving_it_persists_as_a_bool(self):
        resp = self.client.post("/api/settings", json={"simkl_public_calendar_enabled": False})
        self.assertEqual(resp.status_code, 200, resp.text)
        settings = load_settings()
        self.assertIs(settings.simkl_public_calendar_enabled, False)

        resp = self.client.post("/api/settings", json={"simkl_public_calendar_enabled": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(load_settings().simkl_public_calendar_enabled, True)

    def test_it_is_readable_back_through_the_settings_endpoint(self):
        payload = self.client.get("/api/settings").json()
        self.assertIn("simkl_public_calendar_enabled", payload)


class SettingsTabsTests(SettingsSurfaceTestCase):
    """Settings is a handful of tabbed groups in one form."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def _body(self) -> str:
        return self.client.get("/?month=1&year=2026").text

    def test_every_tab_has_a_panel_and_only_the_first_is_showing(self):
        body = self._body()
        tabs = re.findall(r'data-tab="([\w-]+)"', body)
        panels = re.findall(r'data-tab-panel="([\w-]+)"', body)
        self.assertEqual(tabs, ["server", "trakt", "simkl", "calendar", "integrations"])
        self.assertEqual(panels, tabs)
        # Every tab but the first starts hidden; the CSS cannot be relied on to
        # hide them, so the attribute has to be in the markup.
        self.assertEqual(len(re.findall(r'data-tab-panel="\w+" role="tabpanel" hidden', body)),
                         len(tabs) - 1)

    def test_no_field_was_dropped_on_the_way_into_the_tabs(self):
        """The regrouping moved markup around every input the save path reads by
        id, and a field left behind would save as a blank or a zero."""
        body = self._body()
        for field_id in ("s_base_url", "s_trusted_proxies", "s_client_id", "s_client_secret",
                         "s_access_token", "s_timezone", "s_endpoint", "s_limit", "s_cache",
                         "s_calcache", "s_cachecap", "s_hide", "s_sonarr_url", "s_sonarr_key",
                         "s_radarr_url", "s_radarr_key", "s_seer_url", "s_seer_key",
                         "s_tmdb_key", "s_simkl_client_id", "s_simkl_client_secret",
                         "s_simkl_access_token", "s_simkl_public_calendar"):
            with self.subTest(field=field_id):
                self.assertIn(f'id="{field_id}"', body)

    def test_the_reconnect_notice_sits_outside_the_tabs(self):
        """It is an alert about the instance, and an alert that only appears on
        the tab you happen to be standing on is one you can miss."""
        body = self._body()
        notice = body.index('id="s_reconnect_box"')
        first_panel = body.index('data-tab-panel="server"')
        self.assertLess(notice, first_panel)

    def test_one_save_still_writes_fields_from_several_tabs(self):
        """Tabs are presentation only — the panels share a single form, so a
        value from the Server tab and one from Integrations go together."""
        resp = self.client.post("/api/settings", json={
            "public_base_url": ORIGIN, "sonarr_url": "http://localhost:8989",
            "calendar_cache_ttl_minutes": 30,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
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
        self.assertIn("/static/images/favicon.ico", resp.text)

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
            f"/api/admin/users/{victim}/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, ["victim-token"])

    def test_an_admin_unlink_that_asks_for_confirmation_revokes_nothing_yet(self):
        """The first call comes back asking for `force` and the identity stays.
        Killing its token on the way past would leave the account linked to a
        credential that no longer works."""
        orphan = self._make_user(None, password=None)
        self._link_trakt(orphan, provider_user_id=5252, token="orphan-token")
        self.sign_in_as(self.admin_id)
        resp = self.client.post(
            f"/api/admin/users/{orphan}/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.revoked, [])
        self.assertEqual(len(asyncio.run(auth.list_identities(orphan))), 1)

        forced = self.client.post(
            f"/api/admin/users/{orphan}/identities/unlink",
            json={"provider": "trakt", "force": True})
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertEqual(self.revoked, ["orphan-token"])

    def test_unlinking_plex_asks_trakt_nothing(self):
        user_id = self._make_user("plexy", calendar_approved=True)
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="plex", provider_user_id=6161, access_token="p")))
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "plex"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, [])

    def test_without_configured_credentials_it_says_so_rather_than_calling(self):
        """Revocation is authenticated with the app's own credentials. Without
        them there is no call to make, and saying so beats silently doing
        nothing."""
        save_settings(Settings(public_base_url=ORIGIN))
        user_id = self._make_user("uncfg", calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=7171, token="stranded")
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, [])
        self.assertIn("trakt.tv", resp.json()["warning"])


if __name__ == "__main__":
    unittest.main()
