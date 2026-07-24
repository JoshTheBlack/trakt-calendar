"""Coverage for the app-level response compression and the /static cache
lifetime: GZipMiddleware sits in front of every route (see app/main.py, added
before authz.install so it nests inside the authz stack), and /static gets a
short Cache-Control instead of the bare ETag-only default StaticFiles ships.

No network: the Trakt window fetch is patched the same way
tests/test_calendar_route.py does it.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_calendar_perf -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-calperf-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"


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
    _counter = 0

    def setUp(self):
        GzipResponseTests._counter += 1
        db.set_db_path(TMP / f"gzip-{GzipResponseTests._counter}.db")
        asyncio.run(db.migrate())
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
        db.set_db_path(TMP / "static.db")
        asyncio.run(db.migrate())
        save_settings(_configured_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def test_static_asset_carries_a_short_max_age(self):
        resp = self.client.get("/static/css/style.css")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "max-age=600")


if __name__ == "__main__":
    unittest.main()
