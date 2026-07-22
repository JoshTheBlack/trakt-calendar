"""The last round of surfaces: the tracker's backup panel, the share link's
display options, the share page's own view controls, and self-service
credentials.

Each of these closes a gap where the machinery already existed and only the way
in was missing, so these tests are mostly about the SEAM — that the control
reaches the function behind it, and that the destructive one cannot be reached
by accident.

No network anywhere: the Trakt window fetch is patched where a read would
otherwise reach for it, and token revocation is patched wherever an unlink runs.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_finishing_touches -v
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-finishing-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, distrakt as distrakt_store, share_links  # noqa: E402
from app.config import Settings, load_settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"


class FinishingTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        FinishingTestCase._counter += 1
        db.set_db_path(TMP / f"finishing-{FinishingTestCase._counter}.db")
        asyncio.run(db.migrate())
        # The configured origin has to match the one the client speaks, or the
        # cross-site rules refuse every save below for an unrelated reason.
        save_settings(Settings(public_base_url=ORIGIN))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.admin_id = self._make_user("admin_user", is_admin=True, calendar_approved=True)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_user(self, username, password="hunter2hunter2", **flags) -> int:
        return asyncio.run(auth.create_user(
            username=username, password=password, settings=Settings(), **flags))

    def _link_trakt(self, user_id: int, provider_user_id: int, token: str | None = "tok") -> None:
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=provider_user_id,
            access_token=token)))

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def tracker_user(self, username="tracker") -> int:
        user_id = self._make_user(username, calendar_approved=True, distrakt_approved=True)
        self._link_trakt(user_id, provider_user_id=900 + user_id)
        return user_id


class BackupPanelTests(FinishingTestCase):
    """Download and restore, and the acknowledgement in front of the destructive
    half."""

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)

    def _add_show(self, user_id: int, title: str) -> None:
        asyncio.run(distrakt_store.add_show(user_id, "2026-07", {
            "trakt_id": 11, "season": 1, "slug": "a-show", "title": title,
            "network": "HBO", "media": "show", "tmdb": None,
        }))

    def test_the_page_offers_a_download_and_a_restore(self):
        body = self.client.get("/distrakt").text
        self.assertIn('href="/api/distrakt/export"', body)
        self.assertIn('id="restoreFile"', body)

    def test_the_restore_control_demands_a_typed_acknowledgement(self):
        """Restore replaces rather than merges, so the page asks for a phrase
        that has to be read and copied — a confirm dialog can be dismissed by
        reflex, and this cannot."""
        body = self.client.get("/distrakt").text
        self.assertIn("REPLACE MY DATA", body)
        self.assertIn('id="restoreAck"', body)
        # The button starts unusable, so the phrase is the only way to arm it.
        self.assertRegex(body, r'id="restoreBtn"[^>]*disabled')

    def test_the_export_downloads_as_a_file(self):
        resp = self.client.get("/api/distrakt/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertEqual(resp.json()["schema"], distrakt_store.EXPORT_SCHEMA)

    def test_download_then_restore_round_trips_through_the_routes(self):
        self._add_show(self.user_id, "Kept Show")
        exported = self.client.get("/api/distrakt/export").json()

        asyncio.run(distrakt_store.remove_show(self.user_id, "2026-07", 11, 1))
        self.assertEqual(self.client.get("/api/distrakt/list?year=2026&month=7").json()["shows"], [])

        resp = self.client.post("/api/distrakt/restore", json=exported)
        self.assertEqual(resp.status_code, 200, resp.text)
        listed = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual([s["title"] for s in listed["shows"]], ["Kept Show"])

    def test_a_restore_lands_on_whoever_asked_not_whoever_exported(self):
        """The file names no owner the server will honour: it is restored to the
        session that uploaded it, and the account it came from is untouched."""
        self._add_show(self.user_id, "Mine")
        exported = self.client.get("/api/distrakt/export").json()

        other = self.tracker_user("other_tracker")
        self.sign_in_as(other)
        resp = self.client.post("/api/distrakt/restore", json=exported)
        self.assertEqual(resp.status_code, 200, resp.text)
        landed = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual([s["title"] for s in landed["shows"]], ["Mine"])

        self.sign_in_as(self.user_id)
        still_there = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual([s["title"] for s in still_there["shows"]], ["Mine"])


class ShareLinkViewOptionsTests(FinishingTestCase):
    """The display options written into the generated link — and the promise
    that they are written into the LINK and nowhere else."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("linkowner", calendar_approved=True)
        self.sign_in_as(self.user_id)

    def _share(self) -> dict:
        return self.client.get("/api/me/share").json()

    def test_the_default_link_carries_no_view_params(self):
        """"Use my current display" is the absence of params: the page then
        resolves the owner's own defaults, which is exactly what the owner is
        currently looking at."""
        payload = self._share()
        self.assertIsNone(payload["link_view"])
        self.assertNotIn("?", payload["urls"]["token"])

    def test_chosen_options_are_written_into_the_link(self):
        resp = self.client.post("/api/me/share/view", json={"view": {
            "endpoint": "shows/premieres", "card": "poster", "packing": "packed",
            "hidenw": "1", "tz": "America/New_York",
        }})
        self.assertEqual(resp.status_code, 200, resp.text)
        url = resp.json()["urls"]["token"]
        for fragment in ("endpoint=shows%2Fpremieres", "card=poster", "packing=packed",
                         "hidenw=1", "tz=America%2FNew_York"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, url)

    def test_the_link_options_do_not_touch_the_owners_own_view(self):
        """THE POINT OF THE WHOLE DESIGN. Customizing a link somebody else will
        open must not change how the owner's private calendar renders."""
        before = asyncio.run(auth.get_user_prefs(self.user_id))
        before_tz = asyncio.run(auth.get_user(self.user_id))["timezone"]

        self.client.post("/api/me/share/view", json={"view": {
            "endpoint": "shows/finales", "card": "poster", "packing": "packed",
            "hidenw": "1", "tz": "Pacific/Auckland",
        }})

        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id)), before)
        self.assertEqual(asyncio.run(auth.get_user(self.user_id))["timezone"], before_tz)

    def test_the_link_options_do_not_touch_the_share_pages_own_defaults(self):
        """The owner-default columns are the fallback for a link that carries no
        params. Writing the chosen options into them would make "use my current
        display" mean the customized view instead."""
        row = asyncio.run(share_links.get_or_create(self.user_id))
        before = {key: row[key] for key in ("endpoint", "card_style", "day_packing",
                                            "hide_not_watching", "timezone")}
        self.client.post("/api/me/share/view", json={"view": {"endpoint": "shows/finales"}})
        after = asyncio.run(share_links.get(self.user_id))
        self.assertEqual({key: after[key] for key in before}, before)

    def test_clearing_goes_back_to_a_bare_link(self):
        self.client.post("/api/me/share/view", json={"view": {"endpoint": "shows/finales"}})
        resp = self.client.post("/api/me/share/view", json={"view": None})
        self.assertIsNone(resp.json()["link_view"])
        self.assertNotIn("?", resp.json()["urls"]["token"])

    def test_an_unusable_option_is_refused_rather_than_silently_dropped(self):
        """These end up in a URL handed to someone else. A value the page would
        ignore is a link that quietly does not do what its author set."""
        for view in ({"endpoint": "shows/imaginary"}, {"card": "hologram"},
                     {"packing": "sideways"}, {"hidenw": "yes"},
                     {"tz": "Mars/Olympus_Mons"}, {"nonsense": "1"}):
            with self.subTest(view=view):
                resp = self.client.post("/api/me/share/view", json={"view": view})
                self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._share()["link_view"])

    def test_the_options_apply_to_whichever_link_form_is_generated(self):
        self.client.post("/api/me/share/enabled", json={"kind": "username", "enabled": True})
        self.client.post("/api/me/share/view", json={"view": {"endpoint": "shows/premieres"}})
        urls = self._share()["urls"]
        self.assertIn("endpoint=shows%2Fpremieres", urls["token"])
        self.assertIn("endpoint=shows%2Fpremieres", urls["username"])

    def test_the_generated_link_actually_opens_on_the_chosen_view(self):
        """End to end: the params the panel writes are the params the public
        page honours."""
        self.client.post("/api/me/share/view", json={"view": {"card": "poster"}})
        url = self._share()["urls"]["token"]
        self.client.cookies.clear()
        resp = self.client.get(url.replace(ORIGIN, "") + "&year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("card-poster", resp.text)


class SharePageViewControlsTests(FinishingTestCase):
    """The visitor's own controls on a public page: GET-only, no session."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("pageowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = self.client.get("/api/me/share").json()["token"]
        self.client.cookies.clear()

    def test_the_page_renders_view_controls(self):
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('name="endpoint"', body)
        self.assertIn('name="card"', body)
        self.assertIn('name="packing"', body)
        self.assertIn('name="tz"', body)
        self.assertIn('name="hidenw"', body)

    def test_the_controls_are_a_get_form_and_add_no_write_surface(self):
        """A public page has no session to write with. The controls are the same
        whitelisted query params a hand-edited URL already carries."""
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('method="get"', body)
        self.assertNotIn('method="post"', body.lower())

    def test_they_reflect_what_the_url_asked_for(self):
        body = self.client.get(f"/s/{self.token}?year=2026&month=7&card=poster").text
        self.assertRegex(body, r'<option value="poster" selected>')

    def test_the_month_stays_put_when_a_view_option_changes(self):
        """The form carries year/month as hidden fields, so switching the card
        style does not bounce the visitor back to today."""
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('<input type="hidden" name="year" value="2026">', body)
        self.assertIn('<input type="hidden" name="month" value="7">', body)

    def test_hide_not_watching_always_sends_a_value(self):
        """A select rather than a checkbox: an unchecked box is omitted from the
        query entirely, which reads as "unspecified" and falls back to the
        owner's default instead of to "show everything"."""
        body = self.client.get(f"/s/{self.token}?year=2026&month=7&hidenw=1").text
        self.assertIn('<option value="0"', body)
        self.assertIn('<option value="1" selected>', body)


class SelfServiceCredentialsTests(FinishingTestCase):
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


class CacheSettingsWidgetTests(FinishingTestCase):
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


class SettingsTabsTests(FinishingTestCase):
    """Settings is four tabbed groups in one form."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def _body(self) -> str:
        return self.client.get("/?month=1&year=2026").text

    def test_every_tab_has_a_panel_and_only_the_first_is_showing(self):
        body = self._body()
        tabs = re.findall(r'data-tab="([\w-]+)"', body)
        panels = re.findall(r'data-tab-panel="([\w-]+)"', body)
        self.assertEqual(tabs, ["server", "trakt", "calendar", "integrations"])
        self.assertEqual(panels, tabs)
        # Three of the four start hidden; the CSS cannot be relied on to hide
        # them, so the attribute has to be in the markup.
        self.assertEqual(len(re.findall(r'data-tab-panel="\w+" role="tabpanel" hidden', body)), 3)

    def test_no_field_was_dropped_on_the_way_into_the_tabs(self):
        """The regrouping moved markup around every input the save path reads by
        id, and a field left behind would save as a blank or a zero."""
        body = self._body()
        for field_id in ("s_base_url", "s_trusted_proxies", "s_client_id", "s_client_secret",
                         "s_access_token", "s_timezone", "s_endpoint", "s_limit", "s_cache",
                         "s_calcache", "s_cachecap", "s_hide", "s_sonarr_url", "s_sonarr_key",
                         "s_radarr_url", "s_radarr_key", "s_seer_url", "s_seer_key",
                         "s_tmdb_key"):
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


class ErrorPageTests(FinishingTestCase):
    """A mistyped address gets a page, not Starlette's raw JSON."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_a_browser_gets_the_themed_page(self):
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("error-card", resp.text)
        self.assertIn("Back to the calendar", resp.text)

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


class TokenRevocationOnUnlinkTests(FinishingTestCase):
    """Unlinking asks Trakt to forget the authorization rather than leaving it
    standing in the user's connected-apps list."""

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            public_base_url=ORIGIN, trakt_client_id="cid", trakt_client_secret="secret"))
        self.revoked: list[str] = []

        async def _revoke(client_id, client_secret, access_token):
            self.revoked.append(access_token)

        patcher = patch("app.trakt_auth.revoke_token", side_effect=_revoke)
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
