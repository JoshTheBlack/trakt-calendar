"""Public calendar sharing: the /s/, /u/, /c/ read-only pages and the
share_links data layer behind them (app/share_links.py, app/share_routes.py).

Covers: all three URL shapes resolving to the same account; a disabled,
deleted, or never-existent identifier all 404 IDENTICALLY; a public request
NEVER issues an outbound HTTP call even when nothing is cached; view-option
precedence (query param -> owner's share_links default -> app default);
slug/username collisions rejected in both directions; deleting an account
retires its slug and token and leaves zero orphan rows; and the share-page
rate limiter.

No network — the Trakt window fetch is patched at app.calendar_cache's own
module boundary, same as tests/test_calendar_route.py. TRAKT_DATA_DIR points at
a temp dir (set BEFORE importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_share_links -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-share-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, calendar_cache, db, share_links  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.endpoints import get_endpoint  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"
BASE_URL = ORIGIN


def _configured_settings(**extra) -> Settings:
    return Settings(
        trakt_client_id="test-client-id", trakt_access_token="test-access-token",
        public_base_url=BASE_URL, **extra,
    )


def _entry(slug: str, title: str, first_aired: str, network: str = "") -> dict:
    return {
        "first_aired": first_aired,
        "episode": {"season": 1, "number": 1, "title": f"{title} pilot"},
        "show": {
            "title": title, "country": "us", "genres": [], "network": network,
            "ids": {"slug": slug, "trakt": abs(hash(slug)) % 100000},
        },
    }


class ShareTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        ShareTestCase._counter += 1
        db.set_db_path(TMP / f"share-{ShareTestCase._counter}.db")
        asyncio.run(db.migrate())
        save_settings(_configured_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_user(self, username: str, **flags) -> int:
        flags.setdefault("calendar_approved", True)
        return asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=_configured_settings(), **flags))

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def _seed_window(self, endpoint_key: str, day: date, entries: list[dict], ttl: int = 600) -> None:
        start = calendar_cache.window_start(day)
        asyncio.run(calendar_cache.store_window(endpoint_key, start, entries, ttl, db.now()))

    def _enable_all_and_set_slug(self, user_id: int, slug: str) -> dict:
        """Drive the whole panel through the real routes, as the owner would."""
        self.sign_in_as(user_id)
        for kind in ("token", "username", "slug"):
            resp = self.client.post("/api/me/share/enabled", json={"kind": kind, "enabled": True})
            self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.post("/api/me/share/slug", json={"slug": slug})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()


# ---------------------------------------------------------------------------
# all three shapes, and identical 404s
# ---------------------------------------------------------------------------

class ThreeShapesResolveTheSameUserTests(ShareTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("share_owner")
        self.share = self._enable_all_and_set_slug(self.user_id, "owner-slug")
        self.client.cookies.clear()  # every request below is anonymous
        # Seeded directly into the cache rather than via a patched fetch: a
        # share read never fetches (allow_fetch=False), so a mocked
        # fetch_window_raw would simply never be called.
        self._seed_window("shows/new", date(2026, 7, 15), [_entry("show-a", "Show A", "2026-07-15T20:00:00Z")])

    def test_token_username_and_slug_all_render_the_same_calendar(self):
        token_resp = self.client.get(f"/s/{self.share['token']}?year=2026&month=7")
        username_resp = self.client.get("/u/share_owner?year=2026&month=7")
        slug_resp = self.client.get("/c/owner-slug?year=2026&month=7")
        for resp in (token_resp, username_resp, slug_resp):
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Show A", resp.text)
            self.assertIn("share_owner", resp.text)

    def test_username_lookup_is_case_insensitive(self):
        resp = self.client.get("/u/SHARE_OWNER?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Show A", resp.text)

    def test_slug_lookup_is_case_insensitive(self):
        resp = self.client.get("/c/Owner-Slug?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Show A", resp.text)

    def test_disabling_one_form_leaves_the_others_working(self):
        self.sign_in_as(self.user_id)
        resp = self.client.post("/api/me/share/enabled", json={"kind": "username", "enabled": False})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.client.cookies.clear()

        self.assertEqual(self.client.get("/u/share_owner").status_code, 404)
        self.assertEqual(self.client.get(f"/s/{self.share['token']}").status_code, 200)
        self.assertEqual(self.client.get("/c/owner-slug").status_code, 200)


class IdenticalMissTests(ShareTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self._make_user("miss_owner")
        self.share = self._enable_all_and_set_slug(self.owner_id, "miss-slug")
        self.admin_id = self._make_user("miss_admin", is_admin=True)
        self.client.cookies.clear()

    def test_unknown_token_username_and_slug_all_404(self):
        for resp in (
            self.client.get("/s/does-not-exist"),
            self.client.get("/u/does-not-exist"),
            self.client.get("/c/does-not-exist"),
        ):
            self.assertEqual(resp.status_code, 404)

    def test_disabled_account_404s_on_all_three_forms(self):
        asyncio.run(auth.set_disabled(self.owner_id, True))
        for resp in (
            self.client.get(f"/s/{self.share['token']}"),
            self.client.get("/u/miss_owner"),
            self.client.get("/c/miss-slug"),
        ):
            self.assertEqual(resp.status_code, 404)

    def test_all_miss_causes_render_the_byte_identical_page(self):
        unknown = self.client.get("/s/never-existed")
        asyncio.run(auth.set_disabled(self.owner_id, True))
        disabled = self.client.get(f"/s/{self.share['token']}")
        asyncio.run(auth.delete_user(self.owner_id, actor_user_id=self.admin_id))
        deleted = self.client.get(f"/s/{self.share['token']}")

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(deleted.status_code, 404)
        self.assertEqual(unknown.text, disabled.text)
        self.assertEqual(unknown.text, deleted.text)


# ---------------------------------------------------------------------------
# the hard invariant: a public request never calls out to Trakt
# ---------------------------------------------------------------------------

class NeverFetchTests(ShareTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("cache_owner")
        self.share = self._enable_all_and_set_slug(self.user_id, "cache-slug")
        self.client.cookies.clear()

        async def _raise(*args, **kwargs):
            raise AssertionError("a public share request must never fetch from Trakt")

        patcher = patch("app.calendar_cache.fetch_window_raw", side_effect=_raise)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_month_with_nothing_cached_renders_empty_not_500(self):
        resp = self.client.get(f"/s/{self.share['token']}?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Nothing here", resp.text)

    def test_a_stale_cached_window_is_served_as_is_never_refetched(self):
        endpoint = get_endpoint("shows/new")
        self._seed_window(endpoint.key, date(2026, 7, 15), [_entry("show-a", "Show A", "2026-07-15T20:00:00Z")],
                          ttl=1)  # already-expired TTL: a normal read would refetch
        resp = self.client.get(f"/s/{self.share['token']}?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Show A", resp.text)
        self.assertIn("Data as of", resp.text)


# ---------------------------------------------------------------------------
# view-option precedence: param -> owner's share_links default -> app default
# ---------------------------------------------------------------------------

class PrecedenceTests(ShareTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("pref_owner")
        self.share = self._enable_all_and_set_slug(self.user_id, "pref-slug")
        self._seed_window("shows/new", date(2026, 7, 15),
                          [_entry("show-a", "Show A", "2026-07-15T20:00:00Z", network="HBO")])
        self.url = f"/s/{self.share['token']}"

    def test_card_style_app_default_when_nothing_else_set(self):
        self.client.cookies.clear()
        resp = self.client.get(f"{self.url}?year=2026&month=7")
        self.assertIn("card-vertical", resp.text)  # Settings() default

    def test_owner_share_default_beats_app_default(self):
        self.sign_in_as(self.user_id)
        resp = self.client.post("/api/me/prefs", json={"card_style": "poster"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.client.cookies.clear()
        resp = self.client.get(f"{self.url}?year=2026&month=7")
        self.assertIn("card-poster", resp.text)

    def test_query_param_beats_owner_default(self):
        self.sign_in_as(self.user_id)
        self.client.post("/api/me/prefs", json={"card_style": "poster"})
        self.client.cookies.clear()
        resp = self.client.get(f"{self.url}?year=2026&month=7&card=horizontal")
        self.assertIn("card-horizontal", resp.text)
        self.assertNotIn("card-poster", resp.text)

    def test_invalid_query_param_falls_back_silently(self):
        self.client.cookies.clear()
        resp = self.client.get(f"{self.url}?year=2026&month=7&card=not-a-style")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("card-vertical", resp.text)

    def test_networks_param_overrides_owner_default(self):
        self.client.cookies.clear()
        resp = self.client.get(f"{self.url}?year=2026&month=7&networks=Netflix")
        self.assertNotIn("Show A", resp.text)  # HBO filtered out
        resp = self.client.get(f"{self.url}?year=2026&month=7&networks=HBO")
        self.assertIn("Show A", resp.text)

    def test_tz_param_invalid_falls_back_to_owner_default_never_errors(self):
        self.sign_in_as(self.user_id)
        self.client.post("/api/me/timezone", json={"timezone": "America/Los_Angeles"})
        self.client.cookies.clear()
        resp = self.client.get(f"{self.url}?year=2026&month=7&tz=Not/AZone")
        self.assertEqual(resp.status_code, 200)  # never an error


# ---------------------------------------------------------------------------
# cross-namespace collisions, rejected in both directions
# ---------------------------------------------------------------------------

class CrossNamespaceTests(ShareTestCase):
    def test_a_slug_may_not_equal_an_existing_username(self):
        self._make_user("taken_username")
        owner_id = self._make_user("slug_seeker")
        err = asyncio.run(share_links.set_custom_slug(owner_id, "taken_username"))
        self.assertIsNotNone(err)
        row = asyncio.run(share_links.get(owner_id))
        self.assertIsNone(row["custom_slug"])

    def test_a_username_may_not_equal_an_existing_slug(self):
        slug_owner = self._make_user("slug_owner")
        asyncio.run(share_links.set_custom_slug(slug_owner, "reserved-slug"))
        err = asyncio.run(auth.username_availability_error("reserved-slug"))
        self.assertIsNotNone(err)

    def test_slug_taken_by_another_user_is_rejected(self):
        first = self._make_user("first_owner")
        second = self._make_user("second_owner")
        self.assertIsNone(asyncio.run(share_links.set_custom_slug(first, "one-true-slug")))
        err = asyncio.run(share_links.set_custom_slug(second, "one-true-slug"))
        self.assertIsNotNone(err)

    def test_re_saving_your_own_slug_is_not_a_collision_with_yourself(self):
        owner_id = self._make_user("self_saver")
        self.assertIsNone(asyncio.run(share_links.set_custom_slug(owner_id, "my-slug")))
        self.assertIsNone(asyncio.run(share_links.set_custom_slug(owner_id, "my-slug")))

    def test_reserved_words_are_rejected_as_slugs(self):
        owner_id = self._make_user("reserved_tester")
        err = asyncio.run(share_links.set_custom_slug(owner_id, "admin"))
        self.assertIsNotNone(err)

    def test_clearing_the_slug_also_disables_the_slug_form(self):
        owner_id = self._make_user("clearer")
        asyncio.run(share_links.set_custom_slug(owner_id, "clear-me"))
        asyncio.run(share_links.set_enabled(owner_id, "slug", True))
        asyncio.run(share_links.set_custom_slug(owner_id, ""))
        row = asyncio.run(share_links.get(owner_id))
        self.assertIsNone(row["custom_slug"])
        self.assertFalse(bool(row["enabled_slug"]))


# ---------------------------------------------------------------------------
# delete-user retires the slug and token, and leaves zero orphans
# ---------------------------------------------------------------------------

class DeleteRetiresShareIdentifiersTests(ShareTestCase):
    def test_delete_retires_slug_and_token_and_leaves_no_orphan_row(self):
        owner_id = self._make_user("doomed_owner")
        admin_id = self._make_user("doomed_admin", is_admin=True)
        asyncio.run(share_links.set_custom_slug(owner_id, "doomed-slug"))
        row = asyncio.run(share_links.get(owner_id))
        token = row["token"]

        asyncio.run(auth.delete_user(owner_id, actor_user_id=admin_id))

        orphan = asyncio.run(db.fetch_one("SELECT 1 FROM share_links WHERE user_id = ?", (owner_id,)))
        self.assertIsNone(orphan)
        retired = {
            (r["kind"], r["value"])
            for r in asyncio.run(db.fetch_all("SELECT kind, value FROM retired_identifiers"))
        }
        self.assertIn(("slug", "doomed-slug"), retired)
        self.assertIn(("token", token), retired)

    def test_a_deleted_owners_links_404_and_the_retired_slug_blocks_reuse(self):
        owner_id = self._make_user("gone_owner")
        admin_id = self._make_user("gone_admin", is_admin=True)
        asyncio.run(share_links.set_custom_slug(owner_id, "gone-slug"))
        asyncio.run(auth.delete_user(owner_id, actor_user_id=admin_id))

        self.assertEqual(self.client.get("/c/gone-slug").status_code, 404)

        newcomer_id = self._make_user("newcomer")
        err = asyncio.run(share_links.set_custom_slug(newcomer_id, "gone-slug"))
        self.assertIsNotNone(err)

    def test_wipe_data_does_not_touch_share_links(self):
        """§6(i): WIPE DATA keeps the account's share links — only DELETE
        ACCOUNT retires them."""
        owner_id = self._make_user("wiped_owner")
        asyncio.run(share_links.set_custom_slug(owner_id, "wiped-slug"))
        asyncio.run(auth.wipe_user_data(owner_id))
        row = asyncio.run(share_links.get(owner_id))
        self.assertEqual(row["custom_slug"], "wiped-slug")


# ---------------------------------------------------------------------------
# share-page rate limiting
# ---------------------------------------------------------------------------

class ShareRateLimitTests(ShareTestCase):
    def test_share_ip_key_type_is_rate_limited_after_the_threshold(self):
        for _ in range(119):
            asyncio.run(auth.record_attempt("share_ip", "203.0.113.5", True))
        self.assertFalse(asyncio.run(auth.rate_limited(
            "share_ip", "203.0.113.5", max_attempts=120, window_seconds=60)))
        asyncio.run(auth.record_attempt("share_ip", "203.0.113.5", True))
        self.assertTrue(asyncio.run(auth.rate_limited(
            "share_ip", "203.0.113.5", max_attempts=120, window_seconds=60)))

    def test_share_page_returns_429_once_the_caller_is_rate_limited(self):
        owner_id = self._make_user("limited_owner")
        share = self._enable_all_and_set_slug(owner_id, "limited-slug")
        self.client.cookies.clear()

        # Learn the IP this TestClient is seen as, then push it over the limit
        # directly rather than firing 120 real requests.
        self.client.get(f"/s/{share['token']}")
        row = asyncio.run(db.fetch_one(
            "SELECT key_value FROM login_attempts WHERE key_type = 'share_ip' "
            "ORDER BY attempted_at DESC LIMIT 1"))
        ip = row["key_value"]
        for _ in range(130):
            asyncio.run(auth.record_attempt("share_ip", ip, True))

        resp = self.client.get(f"/s/{share['token']}")
        self.assertEqual(resp.status_code, 429)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
