"""Coverage for the app-level response compression and the /static cache
lifetime: GZipMiddleware sits in front of every route (see app/main.py, added
before authz.install so it nests inside the authz stack), and /static gets a
short Cache-Control instead of the bare ETag-only default StaticFiles ships.

No network: the Trakt window fetch is patched the same way
tests/calendar/test_routes.py does it.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import auth, db
from app.config import Settings, save_settings
from app.main import app
from tests.support import ORIGIN, migrated_db


def _configured_settings() -> Settings:
    return Settings(trakt_client_id="test-client-id", trakt_access_token="test-access-token")


def _entry(slug: str, title: str, first_aired: str) -> dict:
    return {
        "first_aired": first_aired,
        "episode": {"season": 1, "number": 1, "title": f"{title} pilot"},
        "show": {
            "title": title, "country": "us", "genres": [],
            "ids": {"slug": slug, "trakt": abs(hash(slug)) % 100000},
        },
    }


class GzipResponseTests(unittest.TestCase):
    def setUp(self):
        migrated_db("gzip")
        save_settings(_configured_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(auth.create_user(
            username="gzip_viewer", password="hunter2hunter2",
            settings=_configured_settings(), calendar_approved=True,
        ))
        session_id = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

        # Enough cards that the rendered page is well past GZipMiddleware's
        # (default, unchanged) 500-byte floor, so this exercises real
        # compression rather than the middleware's too-small passthrough.
        entries = [
            _entry(f"show-{i}", f"Show {i}", f"2026-07-{(i % 27) + 1:02d}T20:00:00Z")
            for i in range(120)
        ]
        fetch = AsyncMock(return_value=entries)
        patcher = patch("app.calendar_cache.fetch_window_raw", fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def test_large_calendar_response_is_gzip_compressed(self):
        resp = self.client.get("/?year=2026&month=7&endpoint=shows")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("content-encoding"), "gzip")

    def test_small_response_is_not_forced_through_gzip(self):
        """A tiny JSON response stays under the default minimum_size, so it
        should ship uncompressed — GZip's own floor, left at its default per
        the decision not to tune it (see app/main.py's GZipMiddleware line)."""
        resp = self.client.get("/api/state?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.headers.get("content-encoding"), "gzip")


class StaticCacheHeaderTests(unittest.TestCase):
    def setUp(self):
        migrated_db("static")
        save_settings(_configured_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def test_static_asset_carries_a_short_max_age(self):
        resp = self.client.get("/static/css/style.css")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "max-age=600")

    def test_fonts_are_cached_for_a_year_instead(self):
        """A vendored woff2 cannot change without its filename changing — the name
        carries the version — so the "we will forget to bump asset_v" risk that
        keeps everything else at 600s does not apply. At 600s the text visibly
        re-flows from the fallback face on every visit more than 10 minutes apart."""
        resp = self.client.get("/static/fonts/inter-v20-latin-400.woff2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"),
                         "public, max-age=31536000, immutable")

    def test_the_long_cache_is_scoped_to_fonts(self):
        """Images live one directory over and have no version in their names."""
        resp = self.client.get("/static/images/trakttop.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "max-age=600")

    def test_htmx_is_self_hosted(self):
        """htmx ships from /static, not a CDN, so boosted navigation has no
        third-party dependency (consistent with the self-hosted fonts). It also
        rides the same short /static cache lifetime as every other asset."""
        resp = self.client.get("/static/js/htmx.min.js")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "max-age=600")
        self.assertIn("htmx", resp.text[:400])


class BoostedNavigationMarkupTests(unittest.TestCase):
    """The calendar page wires hx-boost for snappy, no-reflash navigation. These
    pin the structural pieces that make the boosted swap safe: the vendored htmx
    <script>, the per-page context moved into the swapped region (#pageData),
    hx-boost declared on <body> so every link and form under it inherits it, the
    two JS-handled forms opting back out, and the app scripts loaded (deferred)
    in <head> so a body swap can't re-execute them.
    """
    def setUp(self):
        migrated_db("boost-markup")
        save_settings(_configured_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(auth.create_user(
            username="boost_viewer", password="hunter2hunter2",
            settings=_configured_settings(), calendar_approved=True,
        ))
        session_id = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

        entries = [_entry("show-a", "Show A", "2026-07-10T20:00:00Z")]
        fetch = AsyncMock(return_value=entries)
        patcher = patch("app.calendar_cache.fetch_window_raw", fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _page(self) -> str:
        resp = self.client.get("/?year=2026&month=7&endpoint=shows")
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_vendored_htmx_script_is_included(self):
        self.assertIn("/static/js/htmx.min.js", self._page())

    def test_app_scripts_load_deferred_in_head(self):
        """A boosted nav swaps the <body>, so the app scripts must sit in <head>
        with defer — inside the swapped body they'd be re-fetched and re-executed
        on every nav, re-declaring every top-level const."""
        html = self._page()
        head = html[: html.index("</head>")]
        for src in ("htmx.min.js", "nav.js", "calendar/boot.js"):
            with self.subTest(src=src):
                self.assertIn(src, head)
        # ...and NOT left at the end of <body> where the old page carried them.
        body = html[html.index("</head>"):]
        self.assertNotIn("/static/js/calendar/", body)

    def test_page_context_lives_in_the_swapped_region(self):
        """month/year/endpoint/total moved off <body> (which an innerHTML swap
        leaves in place, going stale) onto #pageData inside the swapped body."""
        html = self._page()
        self.assertIn('id="pageData"', html)
        self.assertIn('data-total="1"', html)
        self.assertIn('data-month="7"', html)
        self.assertIn('data-endpoint="shows"', html)

    def test_the_whole_page_is_boosted_from_the_body(self):
        """Boosting is declared once, on <body>, and inherited by every link and
        form under it — including the month arrows and the endpoint switcher,
        which used to carry the attribute themselves."""
        html = self._page()
        body_tag = html[html.index("<body"): html.index(">", html.index("<body")) + 1]
        self.assertIn('hx-boost="true"', body_tag)
        prev_i = html.index("month-nav-btn prev")
        self.assertNotIn("hx-boost", html[prev_i - 80: prev_i + 80])

    def test_the_js_handled_form_opts_out_of_boosting(self):
        """Filters is submitted by JS and writes over the API. htmx boosts every
        form under a boosted <body>, so it has to say no — otherwise a save turns
        into an attempted navigation. (The settings modal's form does the same;
        it is admin-only, so this viewer's page does not carry it — both are
        covered at the template level by tests/kernel/test_page_head.py.)"""
        html = self._page()
        i = html.index('id="filtersForm"')
        self.assertIn('hx-boost="false"', html[i - 40: i + 140])

    def test_endpoint_switcher_is_a_boosted_get_form(self):
        html = self._page()
        form_i = html.index('id="endpointSelect"')
        # The select carries name=endpoint and submits its enclosing boosted form.
        self.assertIn('name="endpoint"', html[form_i - 40: form_i + 80])
        self.assertIn("this.form.requestSubmit()", html[form_i - 40: form_i + 160])


if __name__ == "__main__":
    unittest.main()
