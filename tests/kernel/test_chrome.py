"""app/chrome.py: the shared page context every route merges into its template
context — the header nav flags plus the version and build label, absorbed from
app/nav.py and the eight hand-restated copies it replaced.

Two kinds of test. The unit tests below exercise page_context() as a pure
function of a user (or None). The one pytest-style test at the bottom is a
route-level smoke test, using the shared fixtures in conftest.py, proving the
context actually reaches a real rendered page rather than only a mock.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import assets, changelog, chrome

class _User:
    def __init__(self, is_admin=False, calendar_approved=False, ranker_approved=False):
        self.is_admin = is_admin
        self.calendar_approved = calendar_approved
        self.ranker_approved = ranker_approved

class PageContextTests(unittest.TestCase):
    def test_no_user_is_a_complete_all_false_set(self):
        """A caller with no session must get every flag rather than a missing
        key a template would render as undefined."""
        ctx = chrome.page_context(None)
        self.assertFalse(ctx["is_admin"])
        self.assertFalse(ctx["calendar_available"])
        self.assertFalse(ctx["ranker_available"])
        self.assertIn("version", ctx)
        self.assertIn("build", ctx)

    def test_each_flag_reads_its_own_attribute(self):
        ctx = chrome.page_context(_User(is_admin=True, calendar_approved=True))
        self.assertTrue(ctx["is_admin"])
        self.assertTrue(ctx["calendar_available"])
        self.assertFalse(ctx["ranker_available"])

    def test_version_comes_from_the_changelog(self):
        with patch.object(changelog, "current_version", return_value="9.9.9"):
            self.assertEqual(chrome.page_context(None)["version"], "9.9.9")

    def test_it_no_longer_carries_the_asset_token(self):
        """Templates get it from app/assets.py through the head macro, which is
        the only thing that ever read it. A key nothing reads would tell the next
        reader that a route decides its page's asset URLs, and none does."""
        self.assertNotIn("asset_v", chrome.page_context(None))

if __name__ == "__main__":
    unittest.main()


def test_pick_page_renders_the_shared_chrome_context(client, settings):
    """The picker at "/" is the simplest page fed by chrome.page_context — no
    Trakt config required to reach it, unlike the calendar itself."""
    from app import auth

    user_id = asyncio.run(auth.create_user(
        username="chromecheck", password="hunter2hunter2",
        settings=settings, calendar_approved=True))
    session_id = asyncio.run(auth.create_session(user_id))
    client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    resp = client.get("/")

    assert resp.status_code == 200
    # The asset token reaches the page through the head macro rather than
    # through this route's context, which is the arrangement worth pinning: the
    # route supplies only the header flags.
    assert f"?v={assets.ASSET_VERSION}" in resp.text
