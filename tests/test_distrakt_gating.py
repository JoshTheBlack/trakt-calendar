"""Who may reach the hidden tracker, whose Trakt token it reads with, and what
the calendar page tells the browser about it.

THE POINT OF THIS FILE: the tracker's whole premise is "read one person's
private watch history and show it back to them". A route that lets the wrong
person in does not merely misbehave, it hands somebody else's viewing over, and
a route that authenticates with the operator's token shows every user the
OPERATOR's viewing instead of their own. Both are tested route by route rather
than as a group, so a gap names the exact endpoint.

Four actors run against every route:
  - signed out
  - distrakt-approved but with NO linked Trakt identity (nothing to read)
  - Trakt-linked and calendar-approved but NOT distrakt-approved
  - distrakt-approved AND Trakt-linked — the only one admitted

No network: the app-wide Trakt credentials are left unset for the gating pass,
so admitted requests take their existing "not configured" paths, and the token
tests patch the shared HTTP client and record what it was asked to send.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_distrakt_gating -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-distrakt-gate-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, main, share_links  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"

# The tracker's read-only endpoints and its page. Enumerated one by one rather
# than discovered from the router: a route that silently loses its declaration
# should fail this file, not quietly drop out of it.
DISTRAKT_GETS = (
    "/distrakt",
    "/api/distrakt/list",
    "/api/distrakt/month",
    "/api/distrakt/months",
    "/api/distrakt/search",
    "/api/distrakt/seasons",
    "/api/distrakt/export",
    "/api/distrakt/share-link",
)

DISTRAKT_POSTS = (
    "/api/distrakt/refresh",
    "/api/distrakt/import",
    "/api/distrakt/backfill-networks",
    "/api/distrakt/remove",
    "/api/distrakt/add",
    "/api/distrakt/abandon",
    "/api/distrakt/restore",
    "/api/distrakt/share-link",
)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class RecordingClient:
    """Stands in for the pooled Trakt client and remembers every Authorization
    header it was handed, which is the only place a token becomes observable."""

    def __init__(self):
        self.authorizations: list[str] = []
        self.urls: list[str] = []

    async def get(self, url, headers=None):
        self.authorizations.append((headers or {}).get("Authorization", ""))
        self.urls.append(url)
        # A shape every caller here tolerates: an empty list reads as "no
        # episodes / no history / no results" rather than an error.
        return FakeResponse([])


class DistraktTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        DistraktTestCase._counter += 1
        db.set_db_path(TMP / f"distrakt-gate-{DistraktTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        # An account has to exist or the first-run gate answers everything before
        # the access levels are consulted.
        self.admin_id = self._make_user("admin_user", is_admin=True, calendar_approved=True)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_user(self, username: str, **flags) -> int:
        return asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=Settings(), **flags))

    def _link_trakt(self, user_id: int, provider_user_id: int, access_token: str | None = None) -> None:
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=provider_user_id,
            access_token=access_token)))

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def sign_out(self) -> None:
        self.client.cookies.clear()

    def tracker_user(self, username: str = "tracker", token: str = "USER-OWN-TOKEN") -> int:
        """The one actor who is allowed in: approved for the tracker and holding
        a Trakt identity for it to read through."""
        user_id = self._make_user(username, calendar_approved=True, distrakt_approved=True)
        self._link_trakt(user_id, provider_user_id=1000 + user_id, access_token=token)
        return user_id


class RefusedActorTests(DistraktTestCase):
    """Every route, every actor who must be refused. A pass here is the whole
    reason this file exists."""

    def _assert_all_refused(self, expected_status: int, expected_reason: str | None = None):
        for path in DISTRAKT_GETS:
            with self.subTest(method="GET", path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, expected_status)
                if expected_reason:
                    self.assertEqual(resp.json()["reason"], expected_reason)
        for path in DISTRAKT_POSTS:
            with self.subTest(method="POST", path=path):
                resp = self.client.post(path, json={})
                self.assertEqual(resp.status_code, expected_status)
                if expected_reason:
                    self.assertEqual(resp.json()["reason"], expected_reason)

    def test_signed_out_reaches_nothing(self):
        self.sign_out()
        self._assert_all_refused(401, "login_required")

    def test_approval_without_a_linked_trakt_account_reaches_nothing(self):
        """Approval alone is not enough: the tracker reads this person's own Trakt
        history, and an account with no link has nothing for it to read."""
        user_id = self._make_user("unlinked", calendar_approved=True, distrakt_approved=True)
        self.sign_in_as(user_id)
        self._assert_all_refused(403, "trakt_link_required")

    def test_a_linked_unapproved_account_reaches_nothing(self):
        """The mirror case: a fully linked, calendar-approved account that was
        never granted tracker access."""
        user_id = self._make_user("linked_only", calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=555, access_token="SOMEONE-ELSES")
        self.sign_in_as(user_id)
        self._assert_all_refused(403, "distrakt_not_approved")

    def test_a_plex_link_does_not_stand_in_for_a_trakt_one(self):
        """Plex proves control of a Plex account and says nothing about Trakt, so
        it cannot satisfy the half of the gate that exists to find a token."""
        user_id = self._make_user("plexonly", calendar_approved=True, distrakt_approved=True)
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="plex", provider_user_id=77, access_token="plex-tok")))
        self.sign_in_as(user_id)
        self._assert_all_refused(403, "trakt_link_required")

    def test_an_admin_without_the_grant_reaches_nothing(self):
        """Being an administrator is not the tracker grant. The two are separate
        on purpose — the tracker exposes a person's viewing, so it is only ever
        granted deliberately, per account."""
        self._link_trakt(self.admin_id, provider_user_id=1, access_token="admin-token")
        self.sign_in_as(self.admin_id)
        self._assert_all_refused(403, "distrakt_not_approved")


class AdmittedActorTests(DistraktTestCase):
    """The one actor who gets in, checked route by route so an over-tight gate is
    as visible as a missing one."""

    def test_an_approved_and_linked_user_is_admitted_everywhere(self):
        self.sign_in_as(self.tracker_user())
        for path in DISTRAKT_GETS:
            with self.subTest(method="GET", path=path):
                resp = self.client.get(path)
                self.assertNotIn(resp.status_code, (401, 403), f"{path} refused an admitted user")
        for path in DISTRAKT_POSTS:
            with self.subTest(method="POST", path=path):
                resp = self.client.post(path, json={})
                self.assertNotIn(resp.status_code, (401, 403), f"{path} refused an admitted user")

    def test_the_page_renders_for_them(self):
        self.sign_in_as(self.tracker_user())
        resp = self.client.get("/distrakt")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Copy blocks", resp.text)

    def test_one_user_cannot_read_another_users_tracker(self):
        """There is no cross-account addressing at all: the tracker a request
        reads is the session's, not anything the caller names."""
        owner = self.tracker_user("owner", token="owner-token")
        other = self.tracker_user("other", token="other-token")
        asyncio.run(main.distrakt_store.add_show(owner, "2026-07", {
            "trakt_id": 42, "season": 1, "slug": "secret-show", "title": "Secret Show",
            "network": "HBO", "media": "show", "tmdb": None,
        }))

        self.sign_in_as(other)
        listed = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual(listed["shows"], [])
        exported = self.client.get("/api/distrakt/export").json()
        self.assertEqual(exported["distrakt_shows"], [])

        self.sign_in_as(owner)
        listed = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual([s["title"] for s in listed["shows"]], ["Secret Show"])


class RequestingUsersTokenTests(DistraktTestCase):
    """The tracker authenticates as whoever asked, never as the instance.

    settings.json's Trakt token is deliberately given a value here, so "the
    user's token was used" and "no token at all was used" cannot be confused
    with each other.
    """

    APP_WIDE = "APP-WIDE-OPERATOR-TOKEN"

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            trakt_client_id="client-id",
            trakt_access_token=self.APP_WIDE,
            trakt_refresh_token="APP-WIDE-REFRESH",
        ))

    def test_the_settings_object_carries_the_users_token_not_the_instances(self):
        user_id = self.tracker_user(token="THE-USERS-TOKEN")
        settings = asyncio.run(main._distrakt_settings(user_id))
        self.assertEqual(settings.trakt_access_token, "THE-USERS-TOKEN")
        self.assertNotEqual(settings.trakt_access_token, self.APP_WIDE)
        # The operator's refresh token has no business travelling next to
        # somebody else's access token.
        self.assertEqual(settings.trakt_refresh_token, "")
        # Everything genuinely app-wide still comes through.
        self.assertEqual(settings.trakt_client_id, "client-id")

    def test_it_comes_from_the_per_user_token_source(self):
        """Pinned to the function that knows how to renew a user's token, rather
        than to a direct read of the identity row, so an expired token is
        refreshed on this path exactly as it is everywhere else."""
        user_id = self.tracker_user(token="STORED")
        with patch("app.trakt_routes.access_token_for_user", return_value="FROM-THE-SOURCE") as spy:
            async def _fake(uid, settings=None):
                return "FROM-THE-SOURCE"
            spy.side_effect = _fake
            settings = asyncio.run(main._distrakt_settings(user_id))
        spy.assert_awaited_once()
        self.assertEqual(spy.await_args.args[0], user_id)
        self.assertEqual(settings.trakt_access_token, "FROM-THE-SOURCE")

    def test_every_trakt_call_a_month_read_makes_carries_the_users_token(self):
        user_id = self.tracker_user(token="USER-A-TOKEN")
        asyncio.run(main.distrakt_store.add_show(user_id, "2026-07", {
            "trakt_id": 7, "season": 2, "slug": "show", "title": "Show",
            "network": "HBO", "media": "show", "tmdb": 1,
        }))
        recorder = RecordingClient()
        self.sign_in_as(user_id)
        with patch("app.trakt.shared_client", return_value=recorder):
            resp = self.client.get("/api/distrakt/month?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(recorder.authorizations, "the month read made no Trakt call to inspect")
        self.assertEqual(set(recorder.authorizations), {"Bearer USER-A-TOKEN"})
        self.assertNotIn(f"Bearer {self.APP_WIDE}", recorder.authorizations)

    def test_two_users_reading_the_same_month_each_use_their_own(self):
        first = self.tracker_user("first", token="FIRST-TOKEN")
        second = self.tracker_user("second", token="SECOND-TOKEN")
        for user_id in (first, second):
            asyncio.run(main.distrakt_store.add_show(user_id, "2026-07", {
                "trakt_id": 7, "season": 2, "slug": "show", "title": "Show",
                "network": "HBO", "media": "show", "tmdb": 1,
            }))

        seen = {}
        for name, user_id in (("first", first), ("second", second)):
            recorder = RecordingClient()
            self.sign_in_as(user_id)
            with patch("app.trakt.shared_client", return_value=recorder):
                self.client.get("/api/distrakt/month?year=2026&month=7")
            seen[name] = set(recorder.authorizations)

        self.assertEqual(seen["first"], {"Bearer FIRST-TOKEN"})
        self.assertEqual(seen["second"], {"Bearer SECOND-TOKEN"})

    def test_search_and_seasons_use_it_too(self):
        """The two lookup endpoints are the easiest to overlook — they take no
        user_id and read no stored rows — and a Trakt search is scored against
        the calling account."""
        user_id = self.tracker_user(token="LOOKUP-TOKEN")
        self.sign_in_as(user_id)
        for path in ("/api/distrakt/search?q=test", "/api/distrakt/seasons?id=7"):
            with self.subTest(path=path):
                recorder = RecordingClient()
                with patch("app.trakt.shared_client", return_value=recorder):
                    self.client.get(path)
                self.assertEqual(set(recorder.authorizations), {"Bearer LOOKUP-TOKEN"})

    def test_a_link_with_no_token_does_not_fall_back_to_the_instances(self):
        """An identity row whose token was cleared reads as "this user has no
        Trakt access", not as "use the operator's". Falling back would show them
        the operator's watch history."""
        user_id = self._make_user("tokenless", calendar_approved=True, distrakt_approved=True)
        self._link_trakt(user_id, provider_user_id=4242, access_token=None)
        settings = asyncio.run(main._distrakt_settings(user_id))
        self.assertEqual(settings.trakt_access_token, "")
        self.assertFalse(settings.configured)

        recorder = RecordingClient()
        self.sign_in_as(user_id)
        with patch("app.trakt.shared_client", return_value=recorder):
            resp = self.client.get("/api/distrakt/search?q=test")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(recorder.authorizations, [])

    def test_a_private_trakt_response_is_never_written_to_the_shared_cache(self):
        """The blob cache is keyed by URL and shared by the whole instance, so two
        people asking about the same show send the identical key. Watch progress
        must therefore never be stored there at all."""
        from app import cache, trakt

        user_id = self.tracker_user(token="PRIVATE-TOKEN")
        recorder = RecordingClient()
        settings = asyncio.run(main._distrakt_settings(user_id))
        with patch("app.trakt.shared_client", return_value=recorder):
            asyncio.run(trakt.fetch_show_progress_detail(settings, 7))
        self.assertTrue(recorder.urls)
        for url in recorder.urls:
            with self.subTest(url=url):
                self.assertIsNone(asyncio.run(cache.get(url, 3600)))


class EasterEggFlagTests(DistraktTestCase):
    """What the calendar page tells the browser, and what it buys."""

    CALENDAR = "/?month=1&year=2026"

    def test_an_unapproved_user_is_told_false(self):
        self.sign_in_as(self._make_user("plain", calendar_approved=True))
        self.assertIn("window.DISTRAKT_AVAILABLE = false", self.client.get(self.CALENDAR).text)

    def test_approval_without_a_trakt_link_is_still_false(self):
        """The flag mirrors the real gate, both halves of it — otherwise the
        easter egg would send someone to a page that refuses them."""
        self.sign_in_as(self._make_user("unlinked", calendar_approved=True, distrakt_approved=True))
        self.assertIn("window.DISTRAKT_AVAILABLE = false", self.client.get(self.CALENDAR).text)

    def test_a_link_without_approval_is_still_false(self):
        user_id = self._make_user("linked", calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=31, access_token="t")
        self.sign_in_as(user_id)
        self.assertIn("window.DISTRAKT_AVAILABLE = false", self.client.get(self.CALENDAR).text)

    def test_an_approved_and_linked_user_is_told_true(self):
        self.sign_in_as(self.tracker_user())
        self.assertIn("window.DISTRAKT_AVAILABLE = true", self.client.get(self.CALENDAR).text)

    def test_the_flag_leaks_no_link_to_the_page_it_gates(self):
        """A refused account's calendar must not advertise the tracker anywhere
        else either — the flag being false is the whole disclosure budget."""
        self.sign_in_as(self._make_user("plain2", calendar_approved=True))
        body = self.client.get(self.CALENDAR).text
        self.assertNotIn("window.DISTRAKT_AVAILABLE = true", body)

    def test_forging_the_flag_changes_nothing(self):
        """The flag is a rendering hint. The server decides, on every route, from
        the session — so a client that sets it by hand and navigates is refused
        exactly as it would have been without it."""
        self.sign_in_as(self._make_user("forger", calendar_approved=True))
        resp = self.client.get("/distrakt")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["reason"], "distrakt_not_approved")
        for path in DISTRAKT_GETS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)


class Post1ShareLinkTests(DistraktTestCase):
    """The share link the announcement post embeds."""

    # The same origin the client speaks, because every absolute URL this app
    # builds comes from this setting AND it is what the cross-site rules compare
    # an incoming Origin against — a mismatch here would refuse the saves below
    # for a reason that has nothing to do with what is being tested.
    BASE = ORIGIN

    def setUp(self):
        super().setUp()
        save_settings(Settings(public_base_url=self.BASE))
        self.user_id = self.tracker_user("poster")
        self.sign_in_as(self.user_id)

    def _post1(self) -> str:
        resp = self.client.get("/api/distrakt/month?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        return resp.json()["post1"]

    def test_the_default_link_is_the_form_the_share_panel_prefers(self):
        row = asyncio.run(share_links.get_or_create(self.user_id))
        self.assertIn(f"<{self.BASE}/s/{row['token']}>", self._post1())

    def test_the_selector_switches_which_form_is_embedded(self):
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        resp = self.client.post("/api/distrakt/share-link", json={"kind": "username"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"<{self.BASE}/u/poster>", self._post1())

    def test_the_choice_is_remembered_per_user(self):
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self.client.post("/api/distrakt/share-link", json={"kind": "username"})
        self.assertEqual(self.client.get("/api/distrakt/share-link").json()["kind"], "username")

        other = self.tracker_user("other_poster")
        self.sign_in_as(other)
        self.assertIsNone(self.client.get("/api/distrakt/share-link").json()["kind"])

    def test_clearing_the_choice_goes_back_to_following_the_share_panel(self):
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self.client.post("/api/distrakt/share-link", json={"kind": "username"})
        self.client.post("/api/distrakt/share-link", json={"kind": ""})
        row = asyncio.run(share_links.get(self.user_id))
        self.assertIsNone(row["post_link_kind"])
        self.assertIn(f"<{self.BASE}/s/{row['token']}>", self._post1())

    def test_the_view_option_rides_along_in_the_query_string(self):
        resp = self.client.post("/api/distrakt/share-link", json={"endpoint": "shows/premieres"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("?endpoint=shows%2Fpremieres>", self._post1())

    def test_the_two_controls_save_independently(self):
        """Each field is only written when it is present, so changing the view
        does not silently reset which link form is embedded."""
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self.client.post("/api/distrakt/share-link", json={"kind": "username"})
        self.client.post("/api/distrakt/share-link", json={"endpoint": "shows/finales"})
        payload = self.client.get("/api/distrakt/share-link").json()
        self.assertEqual(payload["kind"], "username")
        self.assertEqual(payload["endpoint"], "shows/finales")

    def test_an_unknown_kind_or_endpoint_is_refused(self):
        for body in ({"kind": "email"}, {"endpoint": "shows/imaginary"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/distrakt/share-link", json=body).status_code, 400)

    def test_a_form_that_would_not_resolve_falls_back_rather_than_vanishing(self):
        """A stored choice can stop working after the fact — the user disables
        that form, or clears the slug it named. Publishing the link that still
        works beats dropping it out of every future post without saying so."""
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self.client.post("/api/distrakt/share-link", json={"kind": "username"})
        asyncio.run(share_links.set_enabled(self.user_id, "username", False))
        row = asyncio.run(share_links.get(self.user_id))
        self.assertIn(f"<{self.BASE}/s/{row['token']}>", self._post1())

    def test_no_public_base_url_omits_the_link_cleanly(self):
        """With nowhere to point, the post is simply the two lists it always
        was — not a line with a broken or half-built URL in it."""
        save_settings(Settings())
        post1 = self._post1()
        self.assertNotIn("Full calendar", post1)
        self.assertNotIn("://", post1)
        payload = self.client.get("/api/distrakt/share-link").json()
        self.assertTrue(payload["base_url_missing"])
        self.assertIsNone(payload["url"])

    def test_every_form_disabled_omits_the_link(self):
        for kind in ("token", "username", "slug"):
            asyncio.run(share_links.set_enabled(self.user_id, kind, False))
        self.assertNotIn("Full calendar", self._post1())

    def test_the_link_is_built_from_the_configured_origin_not_the_request(self):
        """A spoofed Host header must not make this instance publish somebody
        else's address as the place to find its calendar."""
        post1 = self.client.get("/api/distrakt/month?year=2026&month=7",
                                headers={"Host": "evil.example.net"}).json()["post1"]
        self.assertIn(self.BASE, post1)
        self.assertNotIn("evil.example.net", post1)

    def test_another_users_link_is_never_embedded(self):
        other = self.tracker_user("neighbour")
        other_row = asyncio.run(share_links.get_or_create(other))
        mine = asyncio.run(share_links.get_or_create(self.user_id))
        post1 = self._post1()
        self.assertIn(mine["token"], post1)
        self.assertNotIn(other_row["token"], post1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
