"""Route gating, settings redaction, and the cross-site request rules.

THE FIRST TEST IN THIS FILE IS THE IMPORTANT ONE. Every other test here pins one
route or one rule; that one pins the whole effort, by failing the moment anybody
adds a route without saying who may call it. Its message names the offenders, so
a failure tells you what to fix rather than that something is wrong.

The rest covers: what an unauthenticated caller gets at each access level, that
the settings endpoint hands out no credentials, that a form-encoded body is
refused, and that a request from another origin is refused.

No network, and no login screen to drive — sessions are constructed directly
through the auth primitives. TRAKT_DATA_DIR points at a temp dir (set BEFORE
importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_route_gating -v
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-gating-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, authz, config, db, distrakt  # noqa: E402
from app.auth import AuthLevel  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"

# A distinctive value per credential, so a leak is unmistakable in a response
# body rather than something that might plausibly be a coincidence.
SECRET_VALUES = {name: f"SEKRIT-{name}-VALUE" for name in config.SECRET_FIELDS}

# Field names that read like a credential. Any STRING setting matching one of
# these has to be declared secret; the test below is what makes adding a new
# token/key field without redacting it a failure rather than an oversight.
CREDENTIAL_NAME_RE = re.compile(r"secret|token|password|_key")


def _request(*, host: str, origin: str | None = None, client: str = "127.0.0.1",
             forwarded_proto: str | None = None) -> Request:
    """A bare POST request, for exercising the origin rules without a client."""
    headers = [(b"host", host.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    if forwarded_proto:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
    return Request({
        "type": "http", "method": "POST", "path": "/api/settings", "query_string": b"",
        "scheme": "http", "server": (host, 80), "client": (client, 51234),
        "headers": headers,
    })


class GatingTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        GatingTestCase._counter += 1
        db.set_db_path(TMP / f"gating-{GatingTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        # Secure cookies are the default, so the client has to speak https or it
        # will not send the session back. Origin is what a browser attaches to a
        # fetch() that changes something.
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        # Something has to exist, or the first-run gate answers every request
        # before the access levels get a look in.
        self.admin_id = self._make_user("admin_user", is_admin=True,
                                        calendar_approved=True, distrakt_approved=True)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_user(self, username: str, **flags) -> int:
        return asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=Settings(), **flags))

    def sign_in_as(self, user_id: int) -> None:
        """Attach a session for `user_id` to the client.

        Built straight from the session primitives rather than by posting to the
        sign-in form: this file is about what the gate does once somebody is
        signed in, not about how they got there.
        """
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def sign_out(self) -> None:
        self.client.cookies.clear()


class DeclarationTests(GatingTestCase):
    def test_every_registered_route_declares_an_auth_level(self):
        """THE REGRESSION GUARD FOR THE WHOLE GATING EFFORT.

        A route with no declaration is refused to everyone at runtime, which is
        the safe outcome but a broken feature. This is where you find out, and it
        names the routes so you know which ones.
        """
        missing = authz.undeclared_routes(app)
        self.assertEqual(missing, [], (
            "These routes have no declared access level. Register them with "
            "authz.Guard and an AuthLevel instead of a bare @app.get/@app.post: "
            + ", ".join(missing)
        ))

    def test_undeclared_route_is_denied(self):
        """Deny-by-default, not log-and-allow. A route registered the old way
        never reaches its handler."""
        reached = []

        @app.get("/_undeclared_probe")
        async def probe():  # pragma: no cover — the point is that it isn't reached
            reached.append(True)
            return {"ok": True}

        try:
            self.assertEqual(authz.undeclared_routes(app), ["GET /_undeclared_probe"])
            resp = self.client.get("/_undeclared_probe")
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["reason"], "undeclared_route")
            self.assertEqual(reached, [])
        finally:
            app.router.routes = [r for r in app.router.routes
                                 if getattr(r, "path", None) != "/_undeclared_probe"]

    def test_declaring_one_handler_at_two_levels_is_refused(self):
        """Levels live on the handler, so one handler cannot mean two things —
        the audit would have no way to say which applied."""
        async def handler():  # pragma: no cover — never called
            return None

        authz.declare(handler, AuthLevel.SESSION)
        with self.assertRaises(RuntimeError):
            authz.declare(handler, AuthLevel.PUBLIC)

    def test_a_route_cannot_be_registered_without_a_level(self):
        guard = authz.Guard(app)
        with self.assertRaises(TypeError):
            guard.get("/_no_level")


class UnauthenticatedTests(GatingTestCase):
    """What a caller with no session gets at each of the five levels."""

    def setUp(self):
        super().setUp()
        self.sign_out()

    def test_public_routes_are_served(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_session_level_is_refused(self):
        resp = self.client.get("/me")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["reason"], "login_required")

    def test_calendar_level_is_refused(self):
        for path in ("/", "/pick", "/api/state", "/api/tile", "/api/details",
                     "/api/network-logo?name=HBO"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_distrakt_level_is_refused(self):
        """Tested route by route rather than as a group: a gap here exposes one
        specific user's private watch history."""
        for path in ("/distrakt", "/api/distrakt/list", "/api/distrakt/month",
                     "/api/distrakt/months", "/api/distrakt/search",
                     "/api/distrakt/seasons"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        for path in ("/api/distrakt/refresh", "/api/distrakt/import",
                     "/api/distrakt/backfill-networks", "/api/distrakt/remove",
                     "/api/distrakt/add", "/api/distrakt/abandon"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 401)

    def test_admin_level_is_refused(self):
        for path in ("/api/settings", "/api/integrations/status",
                     "/api/integrations/library"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        for path in ("/api/settings", "/api/auth/device/start",
                     "/api/auth/device/poll", "/api/auth/refresh",
                     "/api/integrations/options", "/api/integrations/add",
                     "/api/network-logo/regenerate"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 401)

    def test_a_browser_navigation_is_sent_to_sign_in(self):
        """A refusal is a status for a script and a destination for a person."""
        resp = self.client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")


class ApprovalTests(GatingTestCase):
    """A session is not enough on its own — each level is checked separately."""

    def test_unapproved_user_cannot_reach_the_calendar(self):
        self.sign_in_as(self._make_user("newcomer"))
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["reason"], "awaiting_approval")
        self.assertEqual(self.client.get("/me").status_code, 200)

    def test_calendar_user_is_not_an_admin_and_not_a_distrakt_user(self):
        self.sign_in_as(self._make_user("viewer", calendar_approved=True))
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 403)
        self.assertEqual(self.client.get("/api/integrations/status").status_code, 403)
        self.assertEqual(self.client.get("/distrakt").status_code, 403)

    def test_distrakt_needs_a_linked_trakt_identity_as_well_as_approval(self):
        """distrakt reads the requesting user's own watch history through their
        own Trakt token, so approval without a link has nothing to read."""
        user_id = self._make_user("tracker", calendar_approved=True, distrakt_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.get("/api/distrakt/months")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["reason"], "trakt_link_required")

        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=99)))
        self.assertEqual(self.client.get("/api/distrakt/months").status_code, 200)

    def test_a_plex_link_does_not_satisfy_the_trakt_requirement(self):
        user_id = self._make_user("plexonly", distrakt_approved=True)
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="plex", provider_user_id=7)))
        self.sign_in_as(user_id)
        self.assertEqual(self.client.get("/api/distrakt/months").status_code, 403)

    def test_admin_reaches_the_admin_routes(self):
        self.sign_in_as(self.admin_id)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(self.client.get("/api/integrations/status").status_code, 200)


class CalendarTemplateTests(GatingTestCase):
    """Admin-only affordances are left out of the page rather than rendered and
    then refused — a button that always fails is worse than no button."""

    CALENDAR = "/?month=1&year=2026"

    def test_a_plain_user_gets_no_admin_controls(self):
        self.sign_in_as(self._make_user("viewer", calendar_approved=True))
        body = self.client.get(self.CALENDAR).text
        self.assertIn("window.IS_ADMIN = false", body)
        self.assertNotIn("openSettings()", body)
        self.assertNotIn("settingsModal", body)
        self.assertNotIn("arr-btn", body)

    def test_an_admin_gets_them(self):
        self.sign_in_as(self.admin_id)
        body = self.client.get(self.CALENDAR).text
        self.assertIn("window.IS_ADMIN = true", body)
        self.assertIn("openSettings()", body)
        self.assertIn("settingsModal", body)

    def test_the_credential_inputs_are_marked_for_the_write_only_handling(self):
        """The Settings screen builds its "leave blank to keep it" behaviour off
        these markers, so every credential input needs one.

        `trakt_refresh_token` has no input by design — it is only ever issued by
        Trakt during authorization, never typed.
        """
        self.sign_in_as(self.admin_id)
        body = self.client.get(self.CALENDAR).text
        typed = config.SECRET_FIELDS - {"trakt_refresh_token"}
        for name in typed:
            with self.subTest(field=name):
                self.assertRegex(body, rf'name="{name}"[^>]*data-secret|data-secret[^>]*name="{name}"')
        self.assertEqual(body.count('data-secret="1"'), len(typed))


class DistraktTemplateTests(GatingTestCase):
    _identity = 0

    def _tracker(self, *, is_admin: bool) -> int:
        DistraktTemplateTests._identity += 1
        provider_user_id = DistraktTemplateTests._identity
        user_id = self._make_user("trackadmin" if is_admin else "tracker",
                                  is_admin=is_admin, distrakt_approved=True)
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=provider_user_id)))
        return user_id

    def test_the_emoji_map_is_rendered_in_rather_than_fetched(self):
        """The roster falls back to these emoji for networks with no logo, so the
        page needs them without a second round trip."""
        user_id = self._tracker(is_admin=False)
        asyncio.run(distrakt.set_emoji_prefs(user_id, {"HBO": ":hbo:"}, ":tv:"))
        self.sign_in_as(user_id)
        body = self.client.get("/distrakt").text
        self.assertIn('window.NETWORK_EMOJIS = {"HBO": ":hbo:"}', body)

    def test_the_emoji_map_is_this_users_own(self):
        """It renders into ONE account's Discord posts, so it is that account's.
        It used to be app-wide, which meant any user's roster import registered
        networks into the operator's map."""
        mine = self._tracker(is_admin=False)
        theirs = self._tracker(is_admin=True)
        asyncio.run(distrakt.set_emoji_prefs(mine, {"HBO": ":mine:"}, ":tv:"))
        asyncio.run(distrakt.set_emoji_prefs(theirs, {"HBO": ":theirs:"}, ":tv:"))
        self.sign_in_as(mine)
        self.assertIn(":mine:", self.client.get("/distrakt").text)
        self.sign_out()
        self.sign_in_as(theirs)
        body = self.client.get("/distrakt").text
        self.assertIn(":theirs:", body)
        self.assertNotIn(":mine:", body)

    def test_a_new_account_starts_with_an_empty_map(self):
        """Nothing seeds it — not settings.json, not another account. It fills in
        from this user's own roster and travels with their Backup export."""
        self.sign_in_as(self._tracker(is_admin=False))
        self.assertIn("window.NETWORK_EMOJIS = {}", self.client.get("/distrakt").text)

    def test_every_tracker_user_can_edit_their_own_map(self):
        """No longer admin-only: the map decides how THIS account's posts render."""
        self.sign_in_as(self._tracker(is_admin=False))
        self.assertIn("saveEmojiMap()", self.client.get("/distrakt").text)


class SettingsRedactionTests(GatingTestCase):
    """The single highest-value fix here: this endpoint used to hand the Trakt
    access token, the Trakt client secret, the TMDB key, and every Sonarr /
    Radarr / Seerr API key to any unauthenticated caller."""

    def setUp(self):
        super().setUp()
        save_settings(Settings(**SECRET_VALUES, trakt_client_id="public-client-id"))
        self.sign_in_as(self.admin_id)

    def test_no_credential_value_appears_in_the_response(self):
        resp = self.client.get("/api/settings")
        payload = resp.json()
        for name, value in SECRET_VALUES.items():
            with self.subTest(field=name):
                # Not anywhere in the bytes, and the field itself isn't a key —
                # only its name inside the `secrets_set` flags.
                self.assertNotIn(value, resp.text)
                self.assertNotIn(name, payload)

    def test_no_credential_is_written_to_settings_json_on_disk(self):
        """Secrets persist to the DB now, not the file. settings.json is reduced to
        the two file-only recovery fields, so no credential value — and no global —
        is left sitting in it in the clear."""
        on_disk = config.SETTINGS_FILE.read_text(encoding="utf-8")
        for value in SECRET_VALUES.values():
            self.assertNotIn(value, on_disk)
        self.assertNotIn("public-client-id", on_disk)
        self.assertEqual(set(json.loads(on_disk)), {"cookie_secure", "allow_open_registration"})

    def test_the_response_says_which_credentials_are_stored(self):
        payload = self.client.get("/api/settings").json()
        self.assertEqual(set(payload["secrets_set"]), set(config.SECRET_FIELDS))
        self.assertTrue(all(payload["secrets_set"].values()))
        save_settings(Settings())
        payload = self.client.get("/api/settings").json()
        self.assertFalse(any(payload["secrets_set"].values()))

    def test_non_secret_settings_still_come_back(self):
        payload = self.client.get("/api/settings").json()
        # The OAuth client id is public — it goes to Trakt in the browser during
        # authorization, and the Settings screen has to show it.
        self.assertEqual(payload["trakt_client_id"], "public-client-id")
        self.assertEqual(payload["endpoint"], Settings().endpoint)

    def test_every_credential_shaped_setting_is_declared_secret(self):
        """What makes a NEWLY ADDED credential field fail this suite instead of
        quietly shipping in the clear."""
        undeclared = [
            f.name for f in dataclasses.fields(Settings)
            if f.type in ("str", str)
            and CREDENTIAL_NAME_RE.search(f.name)
            and f.name not in config.SECRET_FIELDS
        ]
        self.assertEqual(undeclared, [], (
            "These settings are named like credentials but are returned in the "
            "clear. Add them to config.SECRET_FIELDS: " + ", ".join(undeclared)
        ))

    def test_saving_without_a_credential_keeps_the_stored_one(self):
        """The Settings screen renders its credential inputs empty because it
        cannot read them back, so a save that omits them must not wipe them."""
        resp = self.client.post("/api/settings", json={"endpoint": "shows/premieres"})
        self.assertEqual(resp.status_code, 200, resp.text)
        settings = config.load_settings()
        self.assertEqual(settings.endpoint, "shows/premieres")
        self.assertEqual(settings.tmdb_api_key, SECRET_VALUES["tmdb_api_key"])

    def test_a_blank_credential_keeps_the_stored_one(self):
        self.client.post("/api/settings", json={"tmdb_api_key": "   "})
        self.assertEqual(config.load_settings().tmdb_api_key, SECRET_VALUES["tmdb_api_key"])

    def test_a_new_credential_replaces_the_stored_one(self):
        self.client.post("/api/settings", json={"tmdb_api_key": "fresh-key"})
        self.assertEqual(config.load_settings().tmdb_api_key, "fresh-key")

    def test_an_explicit_null_clears_a_credential(self):
        """Clearing has to be possible and has to be deliberate — otherwise the
        only way to disable an integration would be to hand-edit the file."""
        self.client.post("/api/settings", json={"sonarr_api_key": None})
        self.assertEqual(config.load_settings().sonarr_api_key, "")
        self.assertEqual(config.load_settings().tmdb_api_key, SECRET_VALUES["tmdb_api_key"])

    def test_the_save_response_is_redacted_too(self):
        body = self.client.post("/api/settings", json={"endpoint": "shows/new"}).text
        for value in SECRET_VALUES.values():
            self.assertNotIn(value, body)


class RequestShapeTests(GatingTestCase):
    """The two rules every mutating request has to satisfy before anything looks
    at its cookies."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_a_form_encoded_body_is_refused(self):
        """A form-encoded cross-origin POST is a CORS "simple request": the
        browser sends it with cookies and asks nobody's permission first. Not
        accepting the shape at all is what removes the whole class."""
        for path in ("/api/settings", "/api/state", "/api/distrakt/refresh",
                     "/api/integrations/add", "/onboarding", "/login", "/logout"):
            with self.subTest(path=path):
                resp = self.client.post(path, data={"trakt_access_token": "stolen"})
                self.assertEqual(resp.status_code, 415)
        self.assertEqual(config.load_settings().trakt_access_token, "")

    def test_a_body_with_no_content_type_is_refused(self):
        resp = self.client.post("/api/settings", content=b"{}")
        self.assertEqual(resp.status_code, 415)

    def test_a_request_from_another_origin_is_refused(self):
        resp = self.client.post("/api/settings", json={"endpoint": "shows/premieres"},
                                headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["reason"], "cross_origin")
        self.assertEqual(config.load_settings().endpoint, Settings().endpoint)

    def test_the_configured_base_url_wins_over_the_host_header(self):
        """Once an instance knows its own address, the check stops depending on a
        request header at all.

        Exercised directly rather than over the client, because the setting that
        carries that address is added by the provider-login work; this is what
        proves the check will use it the moment it exists.
        """
        settings = Settings()
        settings.public_base_url = "https://shows.example.com"
        request = _request(host="testserver", origin=ORIGIN)
        self.assertEqual(authz.expected_origin(request, settings), "https://shows.example.com")
        self.assertIsNotNone(authz.cross_origin_reason(request, settings))
        matching = _request(host="testserver", origin="https://shows.example.com")
        self.assertIsNone(authz.cross_origin_reason(matching, settings))

    def test_a_forwarded_https_request_matches_a_secure_origin(self):
        """Behind a TLS-terminating proxy the request itself arrives over plain
        HTTP. Reconstructing the origin as `http://…` there would reject every
        real request on a real deployment."""
        settings = Settings(trusted_proxy_ips="10.0.0.0/8")
        request = _request(host="shows.example.com", origin="https://shows.example.com",
                           client="10.1.2.3", forwarded_proto="https")
        self.assertIsNone(authz.cross_origin_reason(request, settings))

    def test_a_same_origin_fetch_without_an_origin_header_is_allowed(self):
        del self.client.headers["Origin"]
        resp = self.client.post("/api/settings", json={},
                                headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_cross_site_fetch_without_an_origin_header_is_refused(self):
        del self.client.headers["Origin"]
        for site in ("cross-site", "same-site", ""):
            with self.subTest(sec_fetch_site=site):
                resp = self.client.post("/api/settings", json={},
                                        headers={"Sec-Fetch-Site": site})
                self.assertEqual(resp.status_code, 403)

    def test_reads_are_not_subject_to_either_rule(self):
        resp = self.client.get("/api/settings", headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 200)


class FirstRunTests(unittest.TestCase):
    """Before any account exists there is nobody who could be authorized, and the
    only useful destination is the setup form."""

    def setUp(self):
        GatingTestCase._counter += 1
        db.set_db_path(TMP / f"gating-firstrun-{GatingTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def test_setup_and_health_stay_reachable(self):
        self.assertEqual(self.client.get("/onboarding").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_a_browser_is_sent_to_setup(self):
        resp = self.client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/onboarding")

    def test_everything_else_is_refused(self):
        resp = self.client.get("/api/settings")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["reason"], "setup_required")


if __name__ == "__main__":
    unittest.main()
