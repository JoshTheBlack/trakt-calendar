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
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit


from app import auth, cache, db
from app.distrakt import routes as distrakt_routes
from app.auth import viewer as auth_viewer
from app.calendar import share_code, share_links
from app.config import Settings, save_settings
from app.main import app
from tests.support import AppTestCase, ORIGIN, tracker_month

# The tracker's read-only endpoints and its page. Enumerated one by one rather
# than discovered from the router: a route that silently loses its declaration
# should fail this file, not quietly drop out of it.
DISTRAKT_GETS = (
    "/distrakt",
    "/distrakt/fragments/month",
    "/distrakt/fragments/day-label",
    "/distrakt/fragments/details",
    "/distrakt/fragments/details/season",
    "/distrakt/fragments/search",
    "/distrakt/fragments/search-stored",
    "/distrakt/fragments/search-movie-stored",
    "/distrakt/fragments/seasons",
    "/api/distrakt/export",
    "/distrakt/fragments/search-movie",
    "/distrakt/fragments/import",
)

DISTRAKT_POSTS = (
    "/api/distrakt/refresh",
    "/api/distrakt/import",
    "/api/distrakt/emoji",
    "/api/distrakt/emoji-default",
    "/api/distrakt/remove",
    "/api/distrakt/add",
    "/api/distrakt/abandon",
    "/api/distrakt/acknowledge-return",
    "/api/distrakt/unknown-add",
    "/api/distrakt/unknown-resume",
    "/api/distrakt/unknown-dismiss",
    "/api/distrakt/unknown-snooze",
    "/api/distrakt/rewatch-start",
    "/api/distrakt/rewatch-complete",
    "/api/distrakt/restore",
    "/api/distrakt/share-link",
    "/api/distrakt/add-completed",
    "/api/distrakt/backfill/survey",
    "/api/distrakt/backfill/apply",
    "/api/distrakt/add-movie",
    "/api/distrakt/remove-movie",
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

    async def get(self, url, headers=None, timeout=None):
        self.authorizations.append((headers or {}).get("Authorization", ""))
        self.urls.append(url)
        # A shape every caller here tolerates: an empty list reads as "no
        # episodes / no history / no results" rather than an error.
        return FakeResponse([])


class DistraktTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        # An account has to exist or the first-run gate answers everything before
        # the access levels are consulted.
        self.admin_id = self._make_user("admin_user", is_admin=True, calendar_approved=True)

    def _make_user(self, username: str, **flags) -> int:
        return self.make_user(username, **flags)

    def _link_trakt(self, user_id: int, provider_user_id: int, access_token: str | None = None) -> None:
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=provider_user_id,
            access_token=access_token)))

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
        # A month has to be named: the bare address is the chooser, and the
        # month's own view is what this is about.
        today = date.today()
        resp = self.client.get(f"/distrakt?year={today.year}&month={today.month}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Copy blocks", resp.text)

    def test_one_user_cannot_read_another_users_tracker(self):
        """There is no cross-account addressing at all: the tracker a request
        reads is the session's, not anything the caller names."""
        owner = self.tracker_user("owner", token="owner-token")
        other = self.tracker_user("other", token="other-token")
        asyncio.run(distrakt_routes.distrakt_store.add_month_record(owner, "2026-07", {
            "ids": {"trakt": 42, "tmdb": 42, "slug": "secret-show"}, "season": 1,
            "title": "Secret Show", "network": "HBO", "media": "show",
            "kind": distrakt_routes.distrakt_store.RecordKind.SERIES_PREMIERE,
        }))

        # Read through the export, the one read left that answers with the
        # session's stored rows as they are — the JSON list of a month did too,
        # had no caller on the page, and is gone.
        def exported_titles() -> list[str]:
            doc = self.client.get("/api/distrakt/export").json()
            return [row["title"] for row in doc["distrakt_month_records"]]

        self.sign_in_as(other)
        self.assertEqual(exported_titles(), [])

        self.sign_in_as(owner)
        self.assertEqual(exported_titles(), ["Secret Show"])


class RequestingUsersTokenTests(DistraktTestCase):
    """The tracker authenticates as whoever asked, never as the instance.

    settings.json's Trakt token is deliberately given a value here, so "the
    user's token was used" and "no token at all was used" cannot be confused
    with each other. What the settings object itself carries is
    tests/auth/test_viewer.py's; this is about the tracker's routes using it.
    """

    APP_WIDE = "APP-WIDE-OPERATOR-TOKEN"

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            trakt_client_id="client-id",
            trakt_access_token=self.APP_WIDE,
            trakt_refresh_token="APP-WIDE-REFRESH",
        ))

    def test_every_trakt_call_a_month_read_makes_carries_the_users_token(self):
        user_id = self.tracker_user(token="USER-A-TOKEN")
        asyncio.run(distrakt_routes.distrakt_store.add_month_record(user_id, "2026-07", {
            "ids": {"trakt": 7, "tmdb": 1, "slug": "show"}, "season": 2, "title": "Show",
            "network": "HBO", "media": "show",
            "kind": distrakt_routes.distrakt_store.RecordKind.SEASON_PREMIERE,
        }))
        recorder = RecordingClient()
        self.sign_in_as(user_id)
        with patch("app.providers.trakt.transport.shared_client", return_value=recorder):
            resp = self.client.get("/distrakt/fragments/month?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(recorder.authorizations, "the month read made no Trakt call to inspect")
        self.assertEqual(set(recorder.authorizations), {"Bearer USER-A-TOKEN"})
        self.assertNotIn(f"Bearer {self.APP_WIDE}", recorder.authorizations)

    def test_two_users_reading_the_same_month_each_use_their_own(self):
        first = self.tracker_user("first", token="FIRST-TOKEN")
        second = self.tracker_user("second", token="SECOND-TOKEN")
        for user_id in (first, second):
            asyncio.run(distrakt_routes.distrakt_store.add_month_record(user_id, "2026-07", {
                "ids": {"trakt": 7, "tmdb": 1, "slug": "show"}, "season": 2, "title": "Show",
                "network": "HBO", "media": "show",
                "kind": distrakt_routes.distrakt_store.RecordKind.SEASON_PREMIERE,
            }))

        seen = {}
        for name, user_id in (("first", first), ("second", second)):
            recorder = RecordingClient()
            self.sign_in_as(user_id)
            with patch("app.providers.trakt.transport.shared_client", return_value=recorder):
                self.client.get("/distrakt/fragments/month?year=2026&month=7")
            seen[name] = set(recorder.authorizations)

        self.assertEqual(seen["first"], {"Bearer FIRST-TOKEN"})
        self.assertEqual(seen["second"], {"Bearer SECOND-TOKEN"})

    def test_search_and_seasons_use_it_too(self):
        """The two lookup endpoints are the easiest to overlook — they take no
        user_id and read no stored rows — and a Trakt search is scored against
        the calling account."""
        user_id = self.tracker_user(token="LOOKUP-TOKEN")
        self.sign_in_as(user_id)
        # THE SEASON PICKER GETS HERE THROUGH THE SEARCH, so the search answer
        # it names has to be in the shared cache for the lookup to be reached at
        # all — the pick re-reads it rather than taking ids off the page.
        asyncio.run(cache.set(
            "https://api.trakt.tv/search/show?query=seeded&extended=full%2Cimages",
            [{"show": {"title": "Test", "year": 2026,
                       "ids": {"trakt": 7, "tmdb": 7, "slug": "test"}}}]))
        for path in ("/distrakt/fragments/search?q=test",
                     "/distrakt/fragments/seasons?q=seeded&i=0"):
            with self.subTest(path=path):
                recorder = RecordingClient()
                with patch("app.providers.trakt.transport.shared_client", return_value=recorder):
                    self.client.get(path)
                self.assertEqual(set(recorder.authorizations), {"Bearer LOOKUP-TOKEN"})

    def test_a_link_with_no_token_does_not_fall_back_to_the_instances(self):
        """An identity row whose token was cleared reads as "this user has no
        Trakt access", not as "use the operator's". Falling back would show them
        the operator's watch history.

        THE SEARCH STILL RUNS, and that is not a weakening of this rule. Trakt's
        /search is a public catalogue read that authenticates with the instance's
        client id; what must never happen is somebody else's BEARER riding on it,
        which is what this asserts. The request goes out with no Authorization
        header at all — see transport.api_headers.
        """
        user_id = self._make_user("tokenless", calendar_approved=True, distrakt_approved=True)
        self._link_trakt(user_id, provider_user_id=4242, access_token=None)
        settings = asyncio.run(auth_viewer.settings_for_user(user_id))
        self.assertEqual(settings.trakt_access_token, "")
        self.assertFalse(settings.trakt_configured)

        recorder = RecordingClient()
        self.sign_in_as(user_id)
        with patch("app.providers.trakt.transport.shared_client", return_value=recorder):
            resp = self.client.get("/distrakt/fragments/search?q=test")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(recorder.authorizations), {""})

    def test_a_private_trakt_response_is_never_written_to_the_shared_cache(self):
        """The blob cache is keyed by URL and shared by the whole instance, so two
        people asking about the same show send the identical key. Watch progress
        must therefore never be stored there at all."""
        from app import cache
        from app.providers.trakt import sync as trakt_sync

        user_id = self.tracker_user(token="PRIVATE-TOKEN")
        recorder = RecordingClient()
        settings = asyncio.run(auth_viewer.settings_for_user(user_id))
        with patch("app.providers.trakt.transport.shared_client", return_value=recorder):
            asyncio.run(trakt_sync.fetch_show_progress_detail(settings, 7))
        self.assertTrue(recorder.urls)
        for url in recorder.urls:
            with self.subTest(url=url):
                self.assertIsNone(asyncio.run(cache.get(url, 3600)))


class EasterEggFlagTests(DistraktTestCase):
    """What the calendar page tells the browser, and what it buys.

    IT IS A FACT ABOUT THE PAGE, so it rides on #pageMeta rather than on `window`:
    a swap brings the new page's copy and leaves nothing of the old one behind.
    That is what stops the reveal firing on a page that never offered one — the
    tracker, most visibly, where the calendar's leftover flag used to make the
    Konami code navigate away from the page it opens.
    """

    CALENDAR = "/?month=1&year=2026"

    def _reveal_offered(self) -> bool:
        meta = re.search(r'<div id="pageMeta"[^>]*>', self.client.get(self.CALENDAR).text)
        return "data-distrakt" in meta.group(0)

    def test_an_unapproved_user_is_told_false(self):
        self.sign_in_as(self._make_user("plain", calendar_approved=True))
        self.assertFalse(self._reveal_offered())

    def test_approval_without_a_trakt_link_is_still_false(self):
        """The flag mirrors the real gate, both halves of it — otherwise the
        easter egg would send someone to a page that refuses them."""
        self.sign_in_as(self._make_user("unlinked", calendar_approved=True, distrakt_approved=True))
        self.assertFalse(self._reveal_offered())

    def test_a_link_without_approval_is_still_false(self):
        user_id = self._make_user("linked", calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=31, access_token="t")
        self.sign_in_as(user_id)
        self.assertFalse(self._reveal_offered())

    def test_an_approved_and_linked_user_is_told_true(self):
        self.sign_in_as(self.tracker_user())
        self.assertTrue(self._reveal_offered())

    def test_the_tracker_never_offers_the_reveal_to_itself(self):
        """There is nowhere for it to go, and the second listener that thought
        otherwise is what made the Konami code navigate away from it. The page that IS the tracker
        carries no reveal however entitled the account is."""
        self.sign_in_as(self.tracker_user())
        meta = re.search(r'<div id="pageMeta"[^>]*>', self.client.get("/distrakt").text)
        self.assertNotIn("data-distrakt", meta.group(0))

    def test_the_flag_leaks_no_link_to_the_page_it_gates(self):
        """A refused account's calendar must not advertise the tracker anywhere
        else either — the flag being false is the whole disclosure budget."""
        self.sign_in_as(self._make_user("plain2", calendar_approved=True))
        self.assertNotIn("data-distrakt", self.client.get(self.CALENDAR).text)

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
        # NO CALENDAR SOURCE, WHICH IS WHAT THIS FIXTURE HAS ALWAYS MEANT. These
        # tests are about the link a post embeds; a month with a calendar to
        # import premieres from would go and read one, and the one source whose
        # calendar needs no credential is on by default.
        save_settings(Settings(public_base_url=self.BASE,
                               simkl_public_calendar_enabled=False))
        self.user_id = self.tracker_user("poster")
        self.sign_in_as(self.user_id)
        self.page = "/distrakt?year=2026&month=7"

    def _save(self, body: dict):
        """Choosing a link form or a view. The month rides in the ADDRESS, as it
        does on every tracker mutation, and the answer is the month redrawn —
        which is where the copy block that embeds the link lives."""
        return self.client.post("/api/distrakt/share-link?year=2026&month=7", json=body)

    def _post1(self) -> str:
        """The announcement the month computes. Read off the payload rather than
        out of the fragment's <textarea>: the link is what is being asserted and
        markup would have it HTML-escaped."""
        return tracker_month(self.user_id, 2026, 7)["post1"]

    def _stored(self) -> dict:
        """What this account has actually chosen, as the page reads it back."""
        return asyncio.run(distrakt_routes._post_link_choices(self.user_id))

    def _embedded_link(self) -> tuple[str, dict]:
        """The URL the post embeds, split into (origin + path, what it asks for).

        The post link is always pinned to the month it announces and handed out
        as a `?p=` code, so the assertions below are about where it points and
        what it says rather than about the exact characters."""
        match = re.search(r"\*\*Full calendar:\*\* (\S+)", self._post1())
        self.assertIsNotNone(match, "no link embedded in post 1")
        parts = urlsplit(match.group(1))
        query = dict(parse_qsl(parts.query))
        view = share_code.decode(query["p"]) if "p" in query else query
        return f"{parts.scheme}://{parts.netloc}{parts.path}", view

    def _assert_points_at(self, path: str):
        url, view = self._embedded_link()
        self.assertEqual(url, f"{self.BASE}{path}")
        return view

    def test_the_default_link_is_the_form_the_share_panel_prefers(self):
        row = asyncio.run(share_links.get_or_create(self.user_id))
        self._assert_points_at(f"/s/{row['token']}")

    def test_the_selector_switches_which_form_is_embedded(self):
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        resp = self._save({"kind": "username"})
        self.assertEqual(resp.status_code, 200)
        self._assert_points_at("/u/poster")

    def test_the_choice_is_remembered_per_user(self):
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self._save({"kind": "username"})
        self.assertEqual(self._stored()["kind"], "username")

        other = self.tracker_user("other_poster")
        self.user_id = other
        self.sign_in_as(other)
        self.assertEqual(self._stored()["kind"], "")

    def test_clearing_the_choice_goes_back_to_following_the_share_panel(self):
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self._save({"kind": "username"})
        self._save({"kind": ""})
        row = asyncio.run(share_links.get(self.user_id))
        self.assertIsNone(row["post_link_kind"])
        self._assert_points_at(f"/s/{row['token']}")

    def test_the_view_option_rides_along_in_the_link(self):
        resp = self._save({"endpoint": "shows/premieres"})
        self.assertEqual(resp.status_code, 200)
        _, view = self._embedded_link()
        self.assertEqual(view["endpoint"], "shows/premieres")

    def test_the_link_is_pinned_to_the_month_the_post_announces(self):
        """A post is read for weeks after it goes up. "Whatever month it is
        opened in" is the wrong month by then — even for a post written during
        the month it announces."""
        _, view = self._embedded_link()
        self.assertEqual((view.get("year"), view.get("month")), ("2026", "7"))

    def test_the_two_controls_save_independently(self):
        """Each field is only written when it is present, so changing the view
        does not silently reset which link form is embedded."""
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self._save({"kind": "username"})
        self._save({"endpoint": "shows/finales"})
        stored = self._stored()
        self.assertEqual(stored["kind"], "username")
        self.assertEqual(stored["endpoint"], "shows/finales")

    def test_an_unknown_kind_or_endpoint_is_refused(self):
        """Drawn at 200 rather than answered with a status, like every refusal
        this page can make: a non-200 is never swapped, so a status code would
        be a control that silently did nothing. Nothing is stored either way."""
        for body in ({"kind": "email"}, {"endpoint": "shows/imaginary"}):
            with self.subTest(body=body):
                resp = self._save(body)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("toast err", resp.text)
        self.assertEqual((self._stored()["kind"], self._stored()["endpoint"]), ("", ""))

    def test_a_form_that_would_not_resolve_falls_back_rather_than_vanishing(self):
        """A stored choice can stop working after the fact — the user disables
        that form, or clears the slug it named. Publishing the link that still
        works beats dropping it out of every future post without saying so."""
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self._save({"kind": "username"})
        asyncio.run(share_links.set_enabled(self.user_id, "username", False))
        row = asyncio.run(share_links.get(self.user_id))
        self._assert_points_at(f"/s/{row['token']}")

    def test_no_public_base_url_omits_the_link_cleanly(self):
        """With nowhere to point, the post is simply the two lists it always
        was — not a line with a broken or half-built URL in it."""
        # Still no calendar source, for setUp's reason: this is about a post with
        # nowhere to point, not about a month reading a calendar.
        save_settings(Settings(simkl_public_calendar_enabled=False))
        post1 = self._post1()
        self.assertNotIn("Full calendar", post1)
        self.assertNotIn("://", post1)
        # With nothing publishable the page draws no selector at all, because
        # the post has no link for one to choose between.
        self.assertFalse(self._stored()["usable"])
        self.assertNotIn("postLinkKind", self.client.get(self.page).text)

    def test_every_form_disabled_omits_the_link(self):
        for kind in ("token", "username", "slug"):
            asyncio.run(share_links.set_enabled(self.user_id, kind, False))
        self.assertNotIn("Full calendar", self._post1())

    def test_the_link_is_bare_so_the_preview_card_draws(self):
        """Angle brackets around a URL are Discord's own way of suppressing the
        preview card, and that card is the reason to publish the link: it
        carries the calendar's picture. This is the whole difference between a
        post that shows the month and one with a blue line under it."""
        post1 = self._post1()
        self.assertIn(f"**Full calendar:** {self.BASE}", post1)
        self.assertNotIn("<http", post1)

    def test_the_link_is_built_from_the_configured_origin_not_the_request(self):
        """A spoofed Host header must not make this instance publish somebody
        else's address as the place to find its calendar."""
        post1 = self._post1()
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
