"""CHANGELOG.md rendered into the in-app "What's new" modal.

Two kinds of test. The parsing ones are ordinary. The rest guard promises that
are easy to break from a long way away: that the release-heading format the
parser depends on is still the one maintainers actually write, that the file is
copied into the Docker image at all, and that raw HTML in a changelog edit cannot
become markup on a signed-in page.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, changelog, db
from app.config import Settings, save_settings
from app.main import app
from tests.support import ORIGIN, ROOT, migrated_db


SAMPLE = """# Changelog

All notable changes are documented here. Format follows [Keep a Changelog](https://example.com).

## 🏷️ [1.2.0] - Unreleased

### Look and feel
- A thing that is not finished yet.

## 🏷️ [1.1.0] - 2026-07-28

### Sharing
- A thing that shipped, with `code` in it.

### 🥚
- Something hidden.

## 🏷️ [1.0.0] - 2026-07-20

- The first one.
"""


class ChangelogParsingTests(unittest.TestCase):
    def test_each_release_becomes_its_own_section(self):
        releases = changelog.parse(SAMPLE)
        self.assertEqual([r.version for r in releases], ["1.2.0", "1.1.0", "1.0.0"])
        self.assertEqual([r.date for r in releases],
                         ["Unreleased", "2026-07-28", "2026-07-20"])

    def test_a_release_in_progress_is_recognised_as_one(self):
        """The date field is free text rather than a date pattern precisely so
        this parses. A parser that insisted on ISO dates would silently drop the
        release currently being written — the one most worth reading."""
        releases = changelog.parse(SAMPLE)
        self.assertTrue(releases[0].is_unreleased)
        self.assertFalse(releases[1].is_unreleased)

    def test_the_preamble_is_left_out(self):
        """The modal has its own header; repeating the document title and the
        Keep a Changelog line inside it would be noise."""
        html = "".join(r.html for r in changelog.parse(SAMPLE))
        self.assertNotIn("Keep a Changelog", html)
        self.assertNotIn("<h1", html)

    def test_a_release_body_is_rendered_markdown_not_text(self):
        body = changelog.parse(SAMPLE)[1].html
        self.assertIn("<h3>Sharing</h3>", body)
        self.assertIn("<li>", body)
        self.assertIn("<code>code</code>", body)

    def test_a_release_keeps_its_own_body_and_no_one_elses(self):
        releases = changelog.parse(SAMPLE)
        self.assertIn("not finished yet", releases[0].html)
        self.assertNotIn("not finished yet", releases[1].html)
        self.assertIn("Something hidden", releases[1].html)

    def test_raw_html_in_the_source_does_not_survive(self):
        """The changelog is first-party today, but it is hand-edited and this
        renders onto a signed-in page. Dropping raw HTML costs nothing — the file
        contains none — and closes the door on a <script> in a changelog edit."""
        html = changelog.parse(
            "## 🏷️ [9.9.9] - 2026-01-01\n\n- <script>alert(1)</script> and "
            "<img src=x onerror=alert(1)>\n"
        )[0].html
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)

    def test_an_unreadable_file_gives_an_empty_list_not_an_exception(self):
        """The changelog is a nicety. Nothing else on the page should break
        because it is missing — which is what happens in a container built
        without the Dockerfile's COPY line."""
        changelog.reset_cache()
        self.addCleanup(changelog.reset_cache)
        with patch.object(changelog, "CHANGELOG_PATH", ROOT / "no-such-changelog.md"):
            self.assertEqual(changelog.releases(), [])

    def test_the_parse_happens_once(self):
        """The file cannot change under a running server — a new changelog only
        arrives with a new container — so it is read once per process."""
        changelog.reset_cache()
        self.addCleanup(changelog.reset_cache)
        with patch.object(changelog, "parse", wraps=changelog.parse) as spy:
            changelog.releases()
            changelog.releases()
            changelog.releases()
        self.assertEqual(spy.call_count, 1)


class CurrentVersionTests(unittest.TestCase):
    """current_version() is what app/chrome.py puts in every page's context —
    see tests/kernel/test_chrome.py for the merge itself."""

    def test_the_newest_release_at_the_top_wins(self):
        with patch.object(changelog, "releases", return_value=changelog.parse(SAMPLE)):
            self.assertEqual(changelog.current_version(), "1.2.0")

    def test_a_release_still_being_written_shows_its_real_version(self):
        """The heading's date field is what says "Unreleased" — the version in
        the brackets is real either way, so a reader always sees what the
        running code actually is rather than a blank space until release day."""
        releases = changelog.parse(SAMPLE)
        self.assertTrue(releases[0].is_unreleased)
        with patch.object(changelog, "releases", return_value=releases):
            self.assertEqual(changelog.current_version(), releases[0].version)

    def test_no_releases_gives_an_empty_string_not_an_exception(self):
        """Mirrors releases()'s own missing-file fallback: a template renders
        past a blank version the same way it renders past an empty changelog."""
        with patch.object(changelog, "releases", return_value=[]):
            self.assertEqual(changelog.current_version(), "")


class RealChangelogTests(unittest.TestCase):
    """Guards the format the parser depends on against drift in the actual file.

    These fail when somebody writes a release heading a new way, which is the
    only way this feature quietly stops working.
    """

    def test_the_projects_own_changelog_parses(self):
        releases = changelog.parse((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertGreater(len(releases), 1)
        self.assertTrue(all(r.version and r.date for r in releases))

    def test_every_h2_in_the_file_is_recognised_as_a_release(self):
        """A heading the parser does not match is a release that vanishes from
        the modal without any error anywhere."""
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        h2_count = sum(1 for line in text.splitlines() if line.startswith("## "))
        self.assertEqual(len(changelog.parse(text)), h2_count)

    def test_the_changelog_is_copied_into_the_docker_image(self):
        """It lives outside app/, and the Dockerfile only copies app/. Without
        its own COPY the modal is empty in production and nothing fails: no build
        error, no failing healthcheck, no log line at startup."""
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY CHANGELOG.md", dockerfile)


class ChangelogRouteTests(unittest.TestCase):
    def setUp(self):
        migrated_db("changelog")
        save_settings(Settings(public_base_url=ORIGIN))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(auth.create_user(
            username="reader", password="hunter2hunter2", settings=Settings()))

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def sign_in(self) -> None:
        session_id = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def test_a_signed_out_visitor_does_not_get_it(self):
        """Release notes are not sensitive, but they are a feature inventory and
        a version history — not something to hand a stranger probing the box."""
        resp = self.client.get("/api/changelog")
        self.assertEqual(resp.status_code, 401)

    def test_any_signed_in_account_gets_it(self):
        """Deliberately the lowest logged-in level: the menu entry is on every
        page for every account, and gating it further would make the header a
        different shape per account."""
        self.sign_in()
        resp = self.client.get("/api/changelog", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("text/html", resp.headers["content-type"])

    def test_the_newest_release_is_the_one_left_open(self):
        self.sign_in()
        body = self.client.get("/api/changelog", headers={"Accept": "text/html"}).text
        self.assertEqual(body.count("<details"), body.count("</details>"))
        # Exactly one section starts expanded, and it is the first one.
        self.assertEqual(body.count("<details class=\"release\" open>"), 1)
        self.assertLess(body.index("open>"), body.index("<details class=\"release\">"))


if __name__ == "__main__":
    unittest.main()
