"""The rankings page's server-rendered fragments, and the rules that keep them
interchangeable with the shell that would have rendered them inline.

THE POINT OF THIS FILE is the class of bug a browser shows and a route test does
not. A fragment that comes back wrapped in page chrome looks fine as a response
and destroys the page it swaps into; a pool sentinel that never stops asking for
the next page looks fine on page one and loops forever on the last; a form that
posts through hx-boost sends the wrong content type and is refused by the
anti-CSRF middleware, which the user experiences as "saving silently does
nothing" rather than as an error. Each of those is asserted here directly.

The lazily-arrived-equals-inline test is the load-bearing one: it is what makes
the pagination invisible, and it fails the moment the two rendering paths drift.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_ranker_fragments -v
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-ranker-frag-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, authz, db, ranker, ranker_routes, user_images  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"

# Every template this feature owns. Named explicitly rather than globbed so a
# template added later has to be considered rather than silently skipped.
RANKER_TEMPLATES = (
    "ranker.html", "_ranker_board_shell.html", "_ranker_pool_page.html",
    "_ranker_category_rows.html", "_ranker_row.html", "_ranker_failed.html",
)

# Markup that belongs to a whole page and must never appear in a fragment: a
# fragment is swapped INTO a document that already has all of it.
PAGE_CHROME = ("<html", "<head", "<body", "<!DOCTYPE", "<link rel=\"stylesheet\"",
               "<script src=", "class=\"hero\"")


def title_ref(match_id: str, title: str) -> dict:
    return {"media": "show", "match_source": "tmdb", "match_id": match_id,
            "tmdb": int(match_id), "title": title, "year": 2026, "network": "AMC"}


class FragmentTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        FragmentTestCase._counter += 1
        db.set_db_path(TMP / f"frag-{FragmentTestCase._counter}.db")
        shutil.rmtree(user_images.USER_DATA_DIR, ignore_errors=True)
        asyncio.run(db.migrate())
        save_settings(Settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        # Something has to exist or the first-run gate answers before any access
        # level is consulted.
        self.make_user("admin_user", is_admin=True)
        self.user_id = self.make_user("ranker_user", ranker_approved=True)
        self.sign_in_as(self.user_id)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def make_user(self, username: str, **flags) -> int:
        return asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=Settings(), **flags))

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def board_with(self, pool: int = 0, tiered: int = 0, uid: str = "b1") -> str:
        """A board holding `pool` unranked titles and `tiered` in one tier."""
        asyncio.run(ranker.create_board(self.user_id, uid=uid, name="Top 2026", year=2026))
        refs = [title_ref(str(1000 + n), f"Title {n}") for n in range(pool + tiered)]
        asyncio.run(ranker.add_titles(self.user_id, uid, refs))
        if tiered:
            keys = [ranker.item_key("show", "tmdb", str(1000 + n)) for n in range(tiered)]
            asyncio.run(ranker.save_layout(self.user_id, uid, {
                "version": 1,
                "categories": [{"uid": "tier-s", "label": "S", "rank_priority": 60,
                                "items": keys}],
                "pool": [],
            }))
        return uid

    def pool_html(self, board_uid: str, page: int) -> str:
        response = self.client.get(f"/rankings/fragments/pool?board={board_uid}&page={page}")
        self.assertEqual(response.status_code, 200)
        return response.text


class FragmentShapeTests(FragmentTestCase):
    """A fragment is ONE unit and no page chrome. Asserted by ABSENCE, because a
    fragment that also returns a stylesheet link still answers 200."""

    def test_a_pool_page_carries_no_page_chrome(self):
        board = self.board_with(pool=3)
        html = self.pool_html(board, 0)
        self.assertIn("ranker-item", html)
        for chrome in PAGE_CHROME:
            self.assertNotIn(chrome, html, f"a pool page must not contain {chrome!r}")

    def test_tier_rows_carry_no_page_chrome(self):
        board = self.board_with(tiered=3)
        response = self.client.get(f"/rankings/fragments/tier?board={board}&tier=tier-s")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("ranker-item"), 3)
        for chrome in PAGE_CHROME:
            self.assertNotIn(chrome, response.text)

    def test_the_page_itself_is_a_whole_document(self):
        """The complement of the tests above: whatever a fragment must not have,
        the page it swaps into does. Without this they would both pass against a
        route that returned nothing at all."""
        self.board_with(pool=2)
        html = self.client.get("/rankings").text
        for chrome in ("<html", "<head", "<body"):
            self.assertIn(chrome, html)


class InlineAndLazyAgreeTests(FragmentTestCase):
    """A page that arrives late must be indistinguishable from one the shell
    rendered inline — otherwise the pagination is visible, which is the whole
    thing it exists not to be."""

    def test_the_first_pool_page_is_byte_identical_inline_and_lazily(self):
        board = self.board_with(pool=ranker_routes.POOL_PAGE_SIZE + 5)
        shell = self.client.get("/rankings").text
        inline = re.search(r'<div class="ranker-pool" id="rankerPool"[^>]*>(.*?)\n        </div>',
                           shell, re.S)
        self.assertIsNotNone(inline, "the shell should render the pool inline")
        self.assertEqual(inline.group(1).strip(), self.pool_html(board, 0).strip())

    def test_a_tiers_rows_are_byte_identical_inline_and_lazily(self):
        board = self.board_with(tiered=4)
        shell = self.client.get("/rankings").text
        inline = re.search(r'<div class="ranker-rows" id="tierBody-tier-s"[^>]*>(.*?)\n                </div>',
                           shell, re.S)
        self.assertIsNotNone(inline, "a small board should render its rows inline")
        lazy = self.client.get(f"/rankings/fragments/tier?board={board}&tier=tier-s").text
        self.assertEqual(inline.group(1).strip(), lazy.strip())

    def test_a_large_board_renders_its_tiers_closed_and_defers_the_rows(self):
        """Above the row limit the tiers come down as headers that fetch their
        own rows, so a board with a lot on it does not pay for rows nobody has
        looked at."""
        board = self.board_with(tiered=4)
        with mock.patch.object(ranker_routes, "EAGER_ROW_LIMIT", 0):
            shell = self.client.get("/rankings").text
        self.assertIn(f'hx-get="/rankings/fragments/tier?board={board}&amp;tier=tier-s"', shell)
        self.assertIn('hx-trigger="click once"', shell)
        # The header, its count and the artwork strip are there; the rows are not.
        self.assertIn('id="tierBody-tier-s"', shell)
        body = re.search(r'<div class="ranker-rows" id="tierBody-tier-s".*?</div>', shell, re.S)
        self.assertNotIn("ranker-item", body.group(0))


class PoolSentinelTests(FragmentTestCase):
    """The sentinel is what ends the chain. A last page that still carries one
    asks for a page that does not exist, forever."""

    def test_a_page_carries_the_sentinel_for_the_next_one(self):
        size = ranker_routes.POOL_PAGE_SIZE
        board = self.board_with(pool=size * 2 + 1)
        first = self.pool_html(board, 0)
        self.assertEqual(first.count('class="ranker-item"'), size)
        self.assertIn(f'hx-get="/rankings/fragments/pool?board={board}&amp;page=1"', first)
        self.assertIn('hx-trigger="intersect once"', first)
        self.assertIn('hx-swap="outerHTML"', first)

        second = self.pool_html(board, 1)
        self.assertIn(f'page=2"', second)

    def test_the_last_page_carries_no_sentinel(self):
        size = ranker_routes.POOL_PAGE_SIZE
        board = self.board_with(pool=size + 2)
        last = self.pool_html(board, 1)
        self.assertEqual(last.count('class="ranker-item"'), 2)
        self.assertNotIn("ranker-pool-sentinel", last)

    def test_a_pool_that_fits_on_one_page_has_no_sentinel_at_all(self):
        board = self.board_with(pool=3)
        self.assertNotIn("ranker-pool-sentinel", self.pool_html(board, 0))

    def test_a_page_past_the_end_is_empty_rather_than_an_error(self):
        """A stale sentinel — from a board that shrank in another tab — asks for
        a page that is now past the end. It gets nothing, which ends the chain,
        rather than a 500 that leaves a broken element on the page."""
        board = self.board_with(pool=2)
        html = self.pool_html(board, 9)
        self.assertNotIn("ranker-item", html)
        self.assertNotIn("ranker-pool-sentinel", html)


class FragmentFailureTests(FragmentTestCase):
    """A fragment that cannot be built is a GAP WITH A WAY OUT, not an exception
    and not a broken board."""

    def test_an_unknown_board_renders_retry_markup(self):
        response = self.client.get("/rankings/fragments/pool?board=nope&page=0")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ranker-failed", response.text)
        self.assertIn("Retry", response.text)
        self.assertIn('hx-target="closest .ranker-failed"', response.text)
        self.assertIn('hx-swap="outerHTML"', response.text)

    def test_the_retry_button_re_requests_exactly_itself(self):
        board = self.board_with(pool=1)
        response = self.client.get(f"/rankings/fragments/tier?board={board}&tier=ghost")
        self.assertIn(f"/rankings/fragments/tier?board={board}&amp;tier=ghost", response.text)

    def test_a_failed_fragment_is_still_a_bare_fragment(self):
        response = self.client.get("/rankings/fragments/pool?board=nope&page=0")
        for chrome in PAGE_CHROME:
            self.assertNotIn(chrome, response.text)


class BoostedFormTests(FragmentTestCase):
    """S17, and it is the sharpest edge on this page.

    hx-boost submits a form with its NATIVE encoding, so a boosted POST form
    sends application/x-www-form-urlencoded — which the request-shape middleware
    refuses with 415, deliberately, as CSRF defence. Boost is for GET navigation
    only; every mutation goes through fetch with a JSON body. A regression here
    reaches the user as "saving does nothing", so it is asserted on the rendered
    templates rather than trusted to review.
    """

    def rendered(self) -> str:
        self.board_with(pool=2, tiered=2)
        return self.client.get("/rankings").text

    def test_no_ranker_template_declares_a_posting_form(self):
        for name in RANKER_TEMPLATES:
            source = (TEMPLATES / name).read_text(encoding="utf-8")
            for form in re.findall(r"<form\b[^>]*>", source, re.I):
                self.assertNotRegex(
                    form, r'method\s*=\s*["\']?post',
                    f"{name} declares a posting form; hx-boost would send it "
                    "urlencoded and the request-shape middleware would refuse it",
                )

    def test_the_rendered_page_boosts_nothing_that_posts(self):
        html = self.rendered()
        for element in re.findall(r"<(?:form|a)\b[^>]*hx-boost[^>]*>", html, re.I):
            self.assertNotRegex(element, r'method\s*=\s*["\']?post')

    def test_the_board_switcher_is_boosted_get_links(self):
        """The one place boost IS used, and what it is used for: real links to a
        real URL, so the switcher works with no script at all."""
        html = self.rendered()
        self.assertIn('hx-boost="true"', html)
        self.assertRegex(html, r'<a class="ranker-board-link[^"]*"\s+href="/rankings\?board=')


class NoBrowserDialogTests(FragmentTestCase):
    """This page asks for a name and asks to confirm through its own dialog, never
    through the browser's.

    prompt()/confirm()/alert() block the page, cannot be styled to match anything
    around them, and a browser set to suppress them turns "name your board" into
    a button that does nothing at all. Scanned rather than reviewed, because the
    failure is silent on the machine of whoever adds one.
    """

    SCRIPT = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "ranker.js"

    def test_the_page_script_calls_no_browser_dialog(self):
        source = re.sub(r"//.*", "", self.SCRIPT.read_text(encoding="utf-8"))
        for name in ("prompt", "confirm", "alert"):
            self.assertNotRegex(
                source, r"(?<![.\w])(window\.)?" + name + r"\s*\(",
                f"ranker.js calls {name}(); ask() is the in-page replacement",
            )

    def test_the_page_carries_the_dialog_that_replaces_them(self):
        self.board_with(pool=1)
        html = self.client.get("/rankings").text
        for element in ('id="askModal"', 'id="askInput"', 'id="askOk"', 'id="askMessage"'):
            self.assertIn(element, html)


class FragmentGatingTests(FragmentTestCase):
    """Fragment routes are views on private data and are gated exactly as the
    page is. A view that forgot its level is a board readable by anyone with a
    session."""

    def test_declared_at_ranker_approved(self):
        fragment_routes = [route for route in authz.iter_routes(app)
                           if str(getattr(route, "path", "")).startswith("/rankings/fragments")]
        self.assertTrue(fragment_routes, "the fragment routes should be registered")
        for route in fragment_routes:
            self.assertEqual(authz.route_level(route), auth.AuthLevel.RANKER_APPROVED,
                             getattr(route, "path", route))

    def test_an_account_without_the_grant_is_refused(self):
        board = self.board_with(pool=2)
        self.sign_in_as(self.make_user("no_grant"))
        for url in (f"/rankings/fragments/pool?board={board}&page=0",
                    f"/rankings/fragments/tier?board={board}&tier=tier-s",
                    "/rankings"):
            with self.subTest(url=url):
                self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_another_account_cannot_read_this_ones_board_through_a_fragment(self):
        """The cross-tenant control, at the one surface that renders HTML rather
        than JSON. It answers as it would for a board that does not exist."""
        board = self.board_with(pool=3, tiered=2)
        self.sign_in_as(self.make_user("other", ranker_approved=True))
        html = self.client.get(f"/rankings/fragments/pool?board={board}&page=0").text
        self.assertIn("ranker-failed", html)
        self.assertNotIn("Title 0", html)


class CategoryDeleteRouteTests(FragmentTestCase):
    """The endpoint the UI's undo-a-tier-deletion is built on. Its titles fall
    back to the pool, which is what makes the undo a re-save rather than a
    restore from somewhere."""

    def test_deleting_a_tier_returns_its_titles_to_the_pool(self):
        board = self.board_with(tiered=3)
        response = self.client.request(
            "DELETE", f"/api/rankings/boards/{board}/categories/tier-s", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["returned"], 3)
        after = self.client.get(f"/api/rankings/boards/{board}").json()["board"]
        self.assertEqual(after["categories"], [])
        self.assertEqual(len(after["pool"]), 3)

    def test_an_unknown_tier_is_refused_without_touching_the_board(self):
        board = self.board_with(tiered=2)
        response = self.client.request(
            "DELETE", f"/api/rankings/boards/{board}/categories/ghost", json={})
        self.assertEqual(response.status_code, 400)
        after = self.client.get(f"/api/rankings/boards/{board}").json()["board"]
        self.assertEqual(len(after["categories"][0]["items"]), 2)

    def test_another_account_cannot_delete_a_tier_and_the_rows_prove_it(self):
        board = self.board_with(tiered=2)
        self.sign_in_as(self.make_user("intruder", ranker_approved=True))
        response = self.client.request(
            "DELETE", f"/api/rankings/boards/{board}/categories/tier-s", json={})
        self.assertEqual(response.status_code, 404)
        # Asserted on the database, not on the status: a route that refuses and
        # writes anyway is exactly what a status-only check misses.
        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM tier_categories")), 1)


class SavedImageRouteTests(FragmentTestCase):
    """The two GETs the export modal's header-image picker needs. Session 4 left
    them to whoever built that modal."""

    def test_listing_starts_empty_and_reports_the_cap(self):
        data = self.client.get("/api/me/images").json()
        self.assertEqual(data["images"], [])
        self.assertEqual(data["max"], user_images.MAX_IMAGES_PER_USER)

    def test_an_uploaded_image_lists_and_serves(self):
        import base64
        import io

        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        created = self.client.post("/api/me/images", json={"image_b64": encoded})
        self.assertEqual(created.status_code, 200)
        uid = created.json()["uid"]

        self.assertEqual(self.client.get("/api/me/images").json()["images"],
                         [{"uid": uid, "name": "Image 1"}])
        served = self.client.get(f"/api/me/images/{uid}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-type"], "image/webp")

    def test_an_image_belonging_to_somebody_else_is_not_served(self):
        import base64
        import io

        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64)).save(buffer, format="PNG")
        uid = self.client.post("/api/me/images", json={
            "image_b64": base64.b64encode(buffer.getvalue()).decode()}).json()["uid"]
        self.sign_in_as(self.make_user("stranger"))
        self.assertEqual(self.client.get(f"/api/me/images/{uid}").status_code, 404)

    def test_a_traversal_uid_is_refused_before_it_becomes_a_path(self):
        self.assertEqual(
            self.client.get("/api/me/images/..%2f..%2fsettings").status_code, 404)


class PageShellTests(FragmentTestCase):
    """What the first response has to carry for the page to work at all."""

    def test_the_board_renders_server_side_with_its_titles(self):
        """Nothing essential waits on JavaScript: the titles are in the HTML."""
        self.board_with(pool=2, tiered=2)
        html = self.client.get("/rankings").text
        self.assertIn("Title 0", html)
        self.assertIn('id="rankerBoard"', html)

    def test_the_page_carries_the_version_a_save_must_echo(self):
        board = self.board_with(tiered=1)
        html = self.client.get("/rankings").text
        stored = asyncio.run(ranker.fetch_board(self.user_id, board))
        self.assertIn(f'"version": {stored["version"]}', html)

    def test_closed_tiers_still_ship_their_item_keys(self):
        """A closed tier draws no rows, so the client would have nothing to name
        in a save — and save_layout refuses a payload that omits a tier. The keys
        travel in the page data instead."""
        self.board_with(tiered=3)
        with mock.patch.object(ranker_routes, "EAGER_ROW_LIMIT", 0):
            html = self.client.get("/rankings").text
        self.assertIn('"show:tmdb:1002"', html)

    def test_an_unusable_source_is_absent_rather_than_disabled(self):
        """The account under test has no linked identity and no tracker data, so
        neither optional action exists on the page at all."""
        self.board_with(pool=1)
        html = self.client.get("/rankings").text
        self.assertNotIn("Import finished titles", html)
        self.assertNotIn("Seed from ratings", html)

    def test_a_stale_board_link_lands_on_the_switcher_rather_than_a_404(self):
        self.board_with(pool=1)
        response = self.client.get("/rankings?board=gone")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ranker-boards", response.text)
        self.assertIn("No board open", response.text)

    def test_the_new_assets_are_registered_for_cache_busting(self):
        from app import assets
        for name in ("static/css/ranker.css", "static/js/ranker.js",
                     "static/js/sortable.min.js"):
            self.assertIn(name, assets._CACHED_ASSETS)
            self.assertTrue((assets.BASE_DIR / name).exists(), f"{name} should exist")


if __name__ == "__main__":
    unittest.main()
