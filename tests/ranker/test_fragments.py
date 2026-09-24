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
"""
from __future__ import annotations

import asyncio
import re
import shutil
import unittest
from unittest import mock
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from app import auth, authz, db
from app.ranker import core as ranker, routes as ranker_routes
from app.config import Settings, save_settings
from app.media import user_images
from app.main import app
from tests.support import ORIGIN, STATIC_DIR, TEMPLATES_DIR, migrated_db


# Every template this feature owns. Named explicitly rather than globbed so a
# template added later has to be considered rather than silently skipped.
RANKER_TEMPLATES = (
    "ranker.html", "_ranker_board_shell.html", "_ranker_pool_page.html",
    "_ranker_pool_rows.html", "_ranker_pool_sentinel.html",
    "_ranker_category_rows.html", "_ranker_row.html", "_ranker_failed.html",
    "_ranker_removed.html", "_ranker_undo.html", "_ranker_search_results.html",
    "_ranker_tier_strip.html", "_ranker_tier_strips.html",
)

# Markup that belongs to a whole page and must never appear in a fragment: a
# fragment is swapped INTO a document that already has all of it.
PAGE_CHROME = ("<html", "<head", "<body", "<!DOCTYPE", "<link rel=\"stylesheet\"",
               "<script src=", "class=\"hero\"")


def title_ref(match_id: str, title: str) -> dict:
    return {"media": "show", "match_source": "tmdb", "match_id": match_id,
            "tmdb": int(match_id), "title": title, "year": 2026, "network": "AMC"}


class FragmentTestCase(unittest.TestCase):
    def setUp(self):
        migrated_db("frag")
        shutil.rmtree(user_images.USER_DATA_DIR, ignore_errors=True)
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

    def pool_html(self, board_uid: str, after: str = "") -> str:
        """A page of the pool: the titles after `after`, or from the start."""
        response = self.client.get(
            "/rankings/fragments/pool?" + urlencode({"board": board_uid, "after": after}))
        self.assertEqual(response.status_code, 200)
        return response.text


def nth_key(n: int) -> str:
    """The key board_with gives its n-th title."""
    return ranker.item_key("show", "tmdb", str(1000 + n))


def keys_drawn(html: str) -> list[str]:
    return re.findall(r'class="ranker-item" data-key="([^"]+)"', html)


class FragmentShapeTests(FragmentTestCase):
    """A fragment is ONE unit and no page chrome. Asserted by ABSENCE, because a
    fragment that also returns a stylesheet link still answers 200."""

    def test_a_pool_page_carries_no_page_chrome(self):
        board = self.board_with(pool=3)
        html = self.pool_html(board)
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
        """The ROWS are what has to match. Their wrapping differs by
        construction — inline they are already inside #rankerPool, while a page
        that arrives later has to say where to put them — so the lazy response
        must CONTAIN the shell's rows verbatim."""
        board = self.board_with(pool=ranker_routes.POOL_PAGE_SIZE + 5)
        shell = self.client.get("/rankings").text
        inline = re.search(r'<div class="ranker-pool" id="rankerPool"[^>]*>(.*?)\n            </div>',
                           shell, re.S)
        self.assertIsNotNone(inline, "the shell should render the pool inline")
        self.assertIn(inline.group(1).strip(), self.pool_html(board))

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

    def test_a_board_at_the_item_ceiling_can_still_draw_closed(self):
        """The largest boards are the ones the closed shape exists for: drawing
        every title open is what stops being quick to redraw. A limit at or above
        the ceiling would make the closed shape — its per-tier fetch included —
        code no board could reach."""
        self.assertLess(ranker_routes.EAGER_ROW_LIMIT, ranker.MAX_ITEMS_PER_BOARD)


class RowFragmentTests(FragmentTestCase):
    """The row every title that arrives after first paint is drawn from — a
    search result clicked or dragged in, a removed title put back.

    THE SAME CLAIM AS THE PAGINATION TESTS ABOVE, ONE ROW AT A TIME: a row that
    lands later must be the row the shell would have drawn, byte for byte, or the
    page shows two kinds of row and the drag machinery holds two shapes of node.
    That used to be a JavaScript builder kept in step with the template by hand.
    """

    def row_html(self, board_uid: str, key: str) -> str:
        response = self.client.get(f"/rankings/fragments/row?board={board_uid}&key={key}")
        self.assertEqual(response.status_code, 200)
        return response.text

    def test_a_row_fragment_is_byte_identical_to_the_row_the_shell_drew(self):
        board = self.board_with(pool=3, tiered=2)
        shell = self.client.get("/rankings").text
        # One tiered and one pooled, because the shell draws the two through
        # different includes and both must agree with the fragment.
        for key in ("show:tmdb:1000", "show:tmdb:1003"):
            with self.subTest(key=key):
                row = self.row_html(board, key)
                self.assertIn('class="ranker-item"', row)
                self.assertIn(row, shell)

    def test_no_row_carries_a_copy_of_the_title(self):
        board = self.board_with(pool=2, tiered=2)
        self.assertNotIn("data-ref", self.client.get("/rankings").text)
        self.assertNotIn("data-ref", self.row_html(board, "show:tmdb:1000"))

    def test_a_title_the_board_does_not_hold_draws_as_nothing(self):
        board = self.board_with(pool=1)
        self.assertEqual(self.row_html(board, "show:tmdb:404"), "")

    def test_another_accounts_row_draws_as_nothing(self):
        board = self.board_with(pool=1)
        self.sign_in_as(self.make_user("other", ranker_approved=True))
        self.assertEqual(self.row_html(board, "show:tmdb:1000"), "")

    def test_no_script_on_the_page_builds_a_row(self):
        """Scanned rather than reviewed: the markup of a row's controls appearing
        in a script is the second builder coming back."""
        scripts = sorted((STATIC_DIR / "js" / "ranker").glob("*.js"))
        self.assertTrue(scripts, "the page's scripts moved; this scans nothing")
        for script in scripts:
            source = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertNotIn("ranker-actions", source)
                self.assertNotIn("buildRow", source)


class SearchIsMarkupTests(FragmentTestCase):
    """The search box asks for its results and the server draws them; no script
    requests, builds or carries a result's title any more."""

    def test_no_script_draws_or_requests_search_results(self):
        source = (STATIC_DIR / "js" / "ranker" / "search.js").read_text(encoding="utf-8")
        for fragment in ("createElement", "innerHTML", "/api/rankings/search", "dataset.ref"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    def test_the_box_and_the_media_choice_ask_for_the_results_fragment(self):
        self.board_with(pool=1)
        html = self.client.get("/rankings").text
        for control in (r'<input type="search" id="searchBox"[^>]*>',
                        r'<select id="searchMedia"[^>]*>'):
            element = re.search(control, html, re.S).group(0)
            with self.subTest(control=control):
                # The BOARD is in the address, because a result has to be drawn
                # knowing whether this board already holds it.
                self.assertIn('hx-get="/rankings/fragments/search?board=b1"', element)
                self.assertIn('hx-target="#searchResults"', element)
                self.assertIn('hx-sync="#searchResults:replace"', element)


def inside(html: str, opening: str) -> str:
    """What sits between `opening` and the tag that closes it, by DEPTH.

    A container used to be found by the indentation of its closing tag, which
    worked until a row started carrying a <div> of its own: a non-greedy match
    then stopped at the first inner close and reported a container as empty that
    was not. Counting the nesting says the same thing and does not care how
    anything is indented.
    """
    at = html.index(opening) + len(opening)
    depth, i = 1, at
    while depth:
        opens = html.find("<div", i)
        closes = html.index("</div>", i)
        if 0 <= opens < closes:
            depth, i = depth + 1, opens + 4
        else:
            depth, i = depth - 1, closes + 6
    return html[at:i - 6]


def move_menu(client, board: str, key: str) -> tuple[str, str]:
    """A row's move menu as its ⇄ asks for it: the opening <form> tag, and what
    sits inside it."""
    html = client.get(f"/rankings/fragments/move-menu?board={board}&key={key}").text
    found = re.search(r"(<form\b[^>]*>)(.*?)</form>", html, re.S)
    return found.group(1), found.group(2)


class TierChoicesAreMarkupTests(FragmentTestCase):
    """Where a title can be moved to is the server's to say, and saying it is the
    whole of the feature now: each row's ⇄ asks for a form with one button per
    destination, posting to the route the bulk move already used."""

    def test_a_rows_menu_offers_the_pool_and_every_tier_in_board_order(self):
        self.board_with(tiered=1)
        _, menu = move_menu(self.client, "b1", "show:tmdb:1000")
        # The POOL FIRST, as the empty tier, which is what move_items already
        # reads as "back to the pool" — so leaving a tier and changing tier are
        # one request rather than two.
        self.assertEqual(re.findall(r'<button name="tier" value="([^"]*)"', menu), ["", "tier-s"])
        self.assertIn(">Pool (unranked)</button>", menu)

    def test_a_row_carries_an_empty_menu_and_the_address_that_fills_it(self):
        """Drawn with every row, a menu was a third of the row's bytes; it is
        asked for when opened. The row holds the popover and its opener only."""
        self.board_with(tiered=1)
        html = self.client.get("/rankings").text
        key = "show:tmdb:1000"
        self.assertIn(f'<div class="ranker-menu" id="move-{key}" popover></div>', html)
        opener = re.search(rf'<button[^>]*popovertarget="move-{re.escape(key)}"[^>]*>', html).group(0)
        self.assertIn('hx-get="/rankings/fragments/move-menu?board=b1&amp;key=show%3Atmdb%3A1000"',
                      opener)
        self.assertIn('hx-trigger="click once"', opener)
        # Not the popover's id: a key's colons would read as pseudo-classes.
        self.assertIn('hx-target="next .ranker-menu"', opener)
        self.assertNotIn('name="tier"', html.split('id="selectionBar"')[0])

    def test_a_title_the_board_does_not_hold_draws_no_menu(self):
        self.board_with(tiered=1)
        self.assertEqual(self.client.get(
            "/rankings/fragments/move-menu?board=b1&key=show:tmdb:999999").text, "")

    def test_another_accounts_board_draws_no_menu(self):
        self.board_with(tiered=1)
        stranger = self.make_user("stranger", ranker_approved=True)
        self.sign_in_as(stranger)
        self.assertEqual(self.client.get(
            "/rankings/fragments/move-menu?board=b1&key=show:tmdb:1000").text, "")

    def test_the_menu_is_one_form_posting_to_the_route_the_bulk_move_uses(self):
        """One key instead of a selection, and otherwise the same request: the
        move menu needed no route of its own. The request is said ONCE, on the
        form, and each choice is only its own value — which the browser adds to
        the form's fields when that button is the one pressed."""
        self.board_with(tiered=1)
        form, menu = move_menu(self.client, "b1", "show:tmdb:1000")
        self.assertIn('hx-post="/rankings/boards/b1/items/move"', form)
        self.assertIn("hx-json-enc", form)
        self.assertIn('hx-target="body"', form)
        self.assertIn('<input type="hidden" name="pick:show:tmdb:1000" value="1">', menu)
        # The key's field belongs to this form alone: naming the selection form
        # would put it in every bulk move.
        self.assertNotIn("selectionForm", menu)
        for choice in re.findall(r"<button[^>]*>", menu):
            with self.subTest(choice=choice):
                self.assertNotIn("hx-", choice)
                self.assertNotIn('type="button"', choice)   # a submit, or it sends nothing

    def test_a_row_carries_its_menu_however_that_row_was_drawn(self):
        """The shell, a page of the pool and a title added after first paint all
        render the same row, which is row_fragment's own claim — and a row
        without its menu would be a title that cannot be moved from wherever it
        happened to arrive."""
        board = self.board_with(pool=ranker_routes.POOL_PAGE_SIZE + 5)
        key = "show:tmdb:1000"
        inline = self.client.get("/rankings").text
        paged = self.pool_html(board)
        one = self.client.get(
            f"/rankings/fragments/row?board={board}&key={key}").text
        for what, html in (("the shell", inline), ("a pool page", paged),
                           ("row_fragment", one)):
            with self.subTest(drawn_by=what):
                self.assertIn(f'id="move-{key}"', html)
                self.assertIn('hx-get="/rankings/fragments/move-menu?board=b1&amp;'
                              'key=show%3Atmdb%3A1000"', html)

    def test_the_bulk_move_list_offers_what_a_rows_menu_offers(self):
        """The pool first and every tier after it, because a selection can now
        hold tiered titles as well as pooled ones — but the first TIER is the one
        chosen, since sending a pool selection back to the pool changes nothing
        anybody could see."""
        self.board_with(tiered=1)
        html = self.client.get("/rankings").text
        select = re.search(r'<select id="bulkTarget"[^>]*>(.*?)</select>', html, re.S).group(1)
        self.assertEqual(re.findall(r'<option value="([^"]*)"', select), ["", "tier-s"])
        self.assertEqual(re.findall(r'<option value="([^"]*)" selected', select), ["tier-s"])
        _, menu = move_menu(self.client, "b1", "show:tmdb:1000")
        self.assertEqual(re.findall(r">([^<]+)</option>", select),
                         re.findall(r">([^<]+)</button>", menu))

    def test_a_deleted_tier_takes_every_choice_naming_it_with_it(self):
        """The bulk-move list and the export dialog's scope, drawn with the page,
        and every row's menu, asked for when it is opened. A delete answers with
        the page redrawn, so none of them has to be found and taken out by hand —
        they are simply not drawn, and a menu opened afterwards is drawn from the
        tiers the board now has."""
        board = self.board_with(pool=3, tiered=1)
        before = self.client.get("/rankings").text
        # The bulk list's option and the export scope carry the tier as a value;
        # the restore the delete's own answer offers for this tier names it in its
        # request instead, so it is not counted here.
        choices = re.compile(r'value="(?:cat:)?tier-s"')
        self.assertEqual(len(choices.findall(before)), 2)
        self.assertEqual(len(choices.findall(move_menu(self.client, board, "show:tmdb:1000")[1])), 1)
        after = self.client.post(f"/rankings/boards/{board}/tiers/tier-s/delete", json={})
        self.assertEqual(choices.findall(move_menu(self.client, board, "show:tmdb:1000")[1]), [])
        self.assertEqual(choices.findall(after.text), [])

    def test_no_script_moves_a_title_between_tiers(self):
        """Nothing opens the menu, places it, remembers which row it belongs to,
        or walks that row into a container. The menu is the row's own, so being
        the row IS the answer; the browser is left with neither the question nor
        a copy of where anything went."""
        rows = (STATIC_DIR / "js" / "ranker" / "rows.js").read_text(encoding="utf-8")
        # By IDENTIFIER, not by the words around them: this file is comment-dense
        # and its own comments name what it used to do.
        for gone in ("movingRow", "moveRowTo", "openMoveMenu", "showPopover",
                     "hidePopover", "menu.style"):
            with self.subTest(identifier=gone):
                self.assertNotIn(gone, rows)

        # Nor does any other file on this page build a destination list of its
        # own: the bulk-move <select> is the server's markup too.
        for name in ("selection.js", "export.js"):
            source = (STATIC_DIR / "js" / "ranker" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertNotIn("createElement('option')", source)
                self.assertNotIn("new Option(", source)


class MoveShowsWhereItWentTests(FragmentTestCase):
    """A move answers with the whole page, so the destination has to be drawn
    open — otherwise a title leaves the pool and is seen landing nowhere.

    ONLY THE DESTINATION, and only when the board would otherwise draw its tiers
    closed: this is about the one tier the request touched, not about reopening
    a board somebody deliberately collapsed.
    """

    def board_with_two_tiers(self) -> str:
        asyncio.run(ranker.create_board(self.user_id, uid="b1", name="Top", year=2026))
        refs = [title_ref(str(1000 + n), f"Title {n}") for n in range(3)]
        asyncio.run(ranker.add_titles(self.user_id, "b1", refs))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": 1,
            "categories": [
                {"uid": "tier-a", "label": "A", "rank_priority": 60, "items": []},
                {"uid": "tier-b", "label": "B", "rank_priority": 50, "items": []},
            ],
            "pool": [ranker.item_key("show", "tmdb", str(1000 + n)) for n in range(3)],
        }))
        return "b1"

    def move_into(self, board: str, key: str, tier: str) -> str:
        with mock.patch.object(ranker_routes, "EAGER_ROW_LIMIT", 0):
            response = self.client.post(
                f"/rankings/boards/{board}/items/move",
                json={f"pick:{key}": "1", "tier": tier})
        self.assertEqual(response.status_code, 200)
        return response.text

    def test_the_tier_a_title_moved_into_comes_back_open_with_its_rows(self):
        board = self.board_with_two_tiers()
        key = ranker.item_key("show", "tmdb", "1000")
        html = self.move_into(board, key, "tier-a")
        opened = re.search(r'<details class="ranker-tier"\s+data-uid="tier-a"( open)?', html)
        self.assertIsNotNone(opened, "the destination tier should be in the answer")
        self.assertEqual(opened.group(1), " open")
        body = inside(html, '<div class="ranker-rows" id="tierBody-tier-a" role="list"')
        self.assertIn(f'data-key="{key}"', body,
                      "an open tier has to arrive with its rows, or it opens on nothing")

    def test_a_tier_nobody_moved_into_stays_closed(self):
        """The complement, without which the test above would pass against a
        board that simply drew everything open."""
        board = self.board_with_two_tiers()
        key = ranker.item_key("show", "tmdb", "1000")
        html = self.move_into(board, key, "tier-a")
        other = re.search(r'<details class="ranker-tier"\s+data-uid="tier-b"( open)?', html)
        self.assertIsNotNone(other)
        self.assertIsNone(other.group(1))
        self.assertIn("tierBody-tier-b", html)
        self.assertNotIn(f'data-key="{key}"',
                         inside(html, '<div class="ranker-rows" id="tierBody-tier-b" role="list"'))

    def test_moving_back_to_the_pool_opens_nothing(self):
        """The pool is always on screen, so there is no tier to name — and
        naming one would reopen a tier the move emptied of that title.

        A SECOND TITLE STAYS IN A TIER, which is the whole reason this asserts
        anything: `rows_open` is about how much is TIERED, so a board with every
        title back in the pool draws its tiers open quite correctly and the test
        would pass without the code under it doing a thing.
        """
        board = self.board_with_two_tiers()
        key = ranker.item_key("show", "tmdb", "1000")
        self.move_into(board, key, "tier-a")
        self.move_into(board, ranker.item_key("show", "tmdb", "1001"), "tier-b")
        html = self.move_into(board, key, "")
        for uid in ("tier-a", "tier-b"):
            with self.subTest(tier=uid):
                found = re.search(r'<details class="ranker-tier"\s+data-uid="%s"( open)?' % uid, html)
                self.assertIsNone(found.group(1))
        self.assertIn(f'data-key="{key}"', inside(html, '<div class="ranker-pool" id="rankerPool"'))


class PoolSentinelPlacementTests(FragmentTestCase):
    """WHERE the sentinel sits, and where the rows it fetches land.

    The pool is a Sortable container. A drag moves its children about, which
    scrolls the sentinel into view, so `intersect` fires MID-DRAG — and while the
    sentinel was a child of the pool, the swap that followed rewrote the list
    Sortable was holding. The displaced row juddered as the placeholder was
    recomputed against DOM that had changed underneath it, and the dragged title
    could be dropped into a region the swap had already replaced, which detached
    it from the page. Keeping the sentinel out of that container is what makes a
    mid-drag page arrival harmless; appending the rows is the one mutation that
    moves nothing already in the list.
    """

    def test_the_sentinel_is_not_inside_the_sortable_pool(self):
        self.board_with(pool=ranker_routes.POOL_PAGE_SIZE + 5)
        shell = self.client.get("/rankings").text
        opening = re.search(r'<div class="ranker-pool" id="rankerPool"[^>]*>', shell)
        self.assertIsNotNone(opening, "the shell should render the pool inline")
        self.assertNotIn("ranker-pool-sentinel", inside(shell, opening.group(0)))
        # It is still inside what SCROLLS, or `intersect` would fire immediately
        # and every page would arrive at once.
        self.assertIn("ranker-pool-sentinel",
                      inside(shell, '<div class="ranker-pool-scroll">'))

    def test_a_late_page_appends_its_rows_rather_than_replacing_anything(self):
        board = self.board_with(pool=ranker_routes.POOL_PAGE_SIZE + 5)
        html = self.pool_html(board, nth_key(ranker_routes.POOL_PAGE_SIZE - 1))
        self.assertIn('<div id="rankerPool" hx-swap-oob="beforeend">', html)
        self.assertIn('class="ranker-item"', html)


class PoolSentinelTests(FragmentTestCase):
    """The sentinel is what ends the chain. A last page that still carries one
    asks for a page that does not exist, forever."""

    def test_a_page_carries_the_sentinel_for_the_next_one(self):
        """Named by the title the page ENDS on, not by a page number."""
        size = ranker_routes.POOL_PAGE_SIZE
        board = self.board_with(pool=size * 2 + 1)
        first = self.pool_html(board)
        self.assertEqual(first.count('class="ranker-item"'), size)
        after = urlencode({"after": nth_key(size - 1)})
        self.assertIn(f'hx-get="/rankings/fragments/pool?board={board}&amp;{after}"', first)
        self.assertIn('hx-trigger="intersect once"', first)
        self.assertIn('hx-swap="outerHTML"', first)

        second = self.pool_html(board, nth_key(size - 1))
        self.assertEqual(keys_drawn(second)[0], nth_key(size))
        self.assertIn(urlencode({"after": nth_key(size * 2 - 1)}), second)

    def test_the_last_page_carries_no_sentinel(self):
        size = ranker_routes.POOL_PAGE_SIZE
        board = self.board_with(pool=size + 2)
        last = self.pool_html(board, nth_key(size - 1))
        self.assertEqual(last.count('class="ranker-item"'), 2)
        self.assertNotIn("ranker-pool-sentinel", last)

    def test_a_pool_that_fits_on_one_page_has_no_sentinel_at_all(self):
        board = self.board_with(pool=3)
        self.assertNotIn("ranker-pool-sentinel", self.pool_html(board))

    def test_a_page_after_the_last_title_is_empty_rather_than_an_error(self):
        """A stale sentinel — from a board that shrank in another tab — asks for
        what follows a title nothing follows any more. It gets nothing, which
        ends the chain, rather than a 500 that leaves a broken element behind."""
        board = self.board_with(pool=2)
        html = self.pool_html(board, nth_key(1))
        self.assertNotIn("ranker-item", html)
        self.assertNotIn("ranker-pool-sentinel", html)


class PoolPagesFollowTitlesNotPositionsTests(FragmentTestCase):
    """The pool is shown a page at a time while the page showing it keeps saving,
    so the next page is "the titles after the one the last page ended on" rather
    than "the sixty from the 120th".

    BOTH WAYS A POSITION WENT WRONG, MEASURED in the browser before this: when a
    save had taken titles out ahead of the position, the next page slid past
    titles that were never shown (so never moved, and back in the pool after a
    reload); when the pool had grown, the next page repeated titles already on
    screen, and the next save was refused whole for naming one title twice."""

    def test_titles_leaving_the_pool_make_no_page_skip_any(self):
        size = ranker_routes.POOL_PAGE_SIZE
        board = self.board_with(pool=size * 2 + 5)
        asyncio.run(ranker.add_categories(self.user_id, board, [{"uid": "tier-a", "label": "A"}]))
        # The first page, all of it, dragged into a tier and saved.
        asyncio.run(ranker.move_items(self.user_id, board, [nth_key(n) for n in range(size)],
                                      "tier-a"))
        page = keys_drawn(self.pool_html(board, nth_key(size - 1)))
        self.assertEqual(page[0], nth_key(size))
        self.assertEqual(len(page), size)

    def test_titles_joining_the_pool_make_no_page_repeat_any(self):
        """Titles that come back to the pool from a tier land ahead of the place
        the page reached — a position would now point sixty titles too early."""
        size = ranker_routes.POOL_PAGE_SIZE
        board = self.board_with(pool=size * 2, tiered=10)     # the tiered hold the lowest ids
        first = keys_drawn(self.pool_html(board))
        asyncio.run(ranker.move_items(self.user_id, board, [nth_key(n) for n in range(10)], ""))
        second = keys_drawn(self.pool_html(board, first[-1]))
        self.assertEqual(set(first) & set(second), set())
        self.assertEqual(second[0], nth_key(10 + size))

    def test_the_title_a_page_ended_on_still_marks_the_place_from_a_tier(self):
        """It has since been dragged somewhere else; its place in the pool's order
        has not moved."""
        size = ranker_routes.POOL_PAGE_SIZE
        board = self.board_with(pool=size + 5)
        asyncio.run(ranker.add_categories(self.user_id, board, [{"uid": "tier-a", "label": "A"}]))
        asyncio.run(ranker.move_items(self.user_id, board, [nth_key(size - 1)], "tier-a"))
        self.assertEqual(keys_drawn(self.pool_html(board, nth_key(size - 1)))[0], nth_key(size))

    def test_a_title_the_board_no_longer_holds_starts_from_the_beginning(self):
        """A repeat is something the page drops (ranker/rows.js); a gap nobody
        would ever see."""
        board = self.board_with(pool=5)
        asyncio.run(ranker.remove_items(self.user_id, board, [nth_key(2)]))
        self.assertEqual(keys_drawn(self.pool_html(board, nth_key(2)))[0], nth_key(0))

    def test_the_pool_is_read_in_one_order_even_after_a_tier_is_deleted(self):
        """A deleted tier hands its titles back to the pool with the ranks they
        held there. Read by rank, they would sit where no page could find them
        again; the pool is read by row id everywhere."""
        board = self.board_with(pool=3, tiered=3)             # S holds 0, 1, 2
        asyncio.run(ranker.save_layout(self.user_id, board, {
            "version": asyncio.run(ranker.fetch_board(self.user_id, board))["version"],
            "categories": [{"uid": "tier-s", "label": "S", "rank_priority": 60,
                            "items": [nth_key(2), nth_key(1), nth_key(0)]}],
            "pool": [nth_key(n) for n in range(3, 6)]}))
        self.client.post(f"/rankings/boards/{board}/tiers/tier-s/delete", json={})
        pool = [i["key"] for i in asyncio.run(ranker.fetch_board(self.user_id, board))["pool"]]
        self.assertEqual(pool, [nth_key(n) for n in range(6)])
        self.assertEqual(keys_drawn(self.pool_html(board, nth_key(1))),
                         [nth_key(n) for n in range(2, 6)])


class FragmentFailureTests(FragmentTestCase):
    """A fragment that cannot be built is a GAP WITH A WAY OUT, not an exception
    and not a broken board."""

    def test_an_unknown_board_renders_retry_markup(self):
        response = self.client.get("/rankings/fragments/pool?board=nope&after=")
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
        response = self.client.get("/rankings/fragments/pool?board=nope&after=")
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
            source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
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


class TierOrderTests(FragmentTestCase):
    """Tiers are drawn highest priority first, which is the order the ranking
    they describe is already built in."""

    def board_with_priorities(self, *priorities: tuple[str, int]) -> str:
        asyncio.run(ranker.create_board(self.user_id, uid="b1", name="Top 2026", year=2026))
        fresh = asyncio.run(ranker.fetch_board(self.user_id, "b1"))
        asyncio.run(ranker.save_layout(self.user_id, "b1", {
            "version": fresh["version"],
            "categories": [{"uid": f"tier-{label.lower()}", "label": label,
                            "rank_priority": priority, "items": []}
                           for label, priority in priorities],
            "pool": [],
        }))
        return "b1"

    def labels_in_order(self) -> list[str]:
        html = self.client.get("/rankings").text
        return re.findall(r'<span class="ranker-tier-label">([^<]*)</span>', html)

    def test_the_board_draws_its_tiers_highest_priority_first(self):
        """Created in the wrong order on purpose: nothing reorders tiers by hand,
        so the order they were made in is not something a viewer can correct."""
        self.board_with_priorities(("F", 10), ("S", 60), ("B", 40))
        self.assertEqual(self.labels_in_order(), ["S", "B", "F"])

    def test_tiers_sharing_a_priority_keep_the_order_they_had(self):
        """A stable sort, so two tiers at the same priority do not swap places
        between one render and the next."""
        self.board_with_priorities(("First", 30), ("Second", 30), ("Top", 60))
        self.assertEqual(self.labels_in_order(), ["Top", "First", "Second"])


class DropTargetTests(FragmentTestCase):
    """A container is a drop target only once it holds what it claims to.

    Sortable treats a CHILDLESS drop target as somewhere to insert, and scans
    every one of them against the pointer on every move. A tier that has not
    been opened yet is childless — its rows are still on the server — so binding
    it let an off-screen container take the drag away from the tier below it and
    give it back, over and over, which is what the shudder was.
    """

    def dnd_source(self) -> str:
        return (STATIC_DIR / "js" / "ranker" / "dnd.js").read_text(encoding="utf-8")

    def test_a_tier_body_is_a_drop_target_only_once_its_rows_are_there(self):
        self.assertIn(".ranker-rows[data-loaded]", self.dnd_source())
        self.assertNotIn("'.ranker-pool, .ranker-rows'", self.dnd_source())

    def test_the_swap_marks_a_body_loaded_before_rebinding_the_containers(self):
        """Order matters: the binding pass reads data-loaded, so a tier whose
        rows have just landed has to be marked first or it waits for the next
        swap to become droppable."""
        boot = (STATIC_DIR / "js" / "ranker" / "boot.js").read_text(encoding="utf-8")
        self.assertLess(boot.index("dataset.loaded = '1'"),
                        boot.index("initSortable(document)", boot.index("after:swap")))

    def test_an_inline_tier_body_says_it_is_loaded_and_a_deferred_one_does_not(self):
        self.board_with(tiered=4)
        inline = self.client.get("/rankings").text
        self.assertRegex(inline, r'<div class="ranker-rows" id="tierBody-tier-s"[^>]*data-loaded="1"')
        with mock.patch.object(ranker_routes, "EAGER_ROW_LIMIT", 0):
            deferred = self.client.get("/rankings").text
        body = re.search(r'<div class="ranker-rows" id="tierBody-tier-s"[^>]*>', deferred)
        self.assertIsNotNone(body)
        self.assertNotIn("data-loaded", body.group(0))


class NoBrowserDialogTests(FragmentTestCase):
    """This page asks for a name and asks to confirm in markup, never through the
    browser's own dialogs.

    prompt()/confirm()/alert() block the page, cannot be styled to match anything
    around them, and a browser set to suppress them turns "name your board" into
    a button that does nothing at all. Scanned rather than reviewed, because the
    failure is silent on the machine of whoever adds one.
    """

    SCRIPTS = sorted(
        (STATIC_DIR / "js" / "ranker")
        .glob("*.js"))

    def test_the_page_script_calls_no_browser_dialog(self):
        self.assertTrue(self.SCRIPTS, "the page's scripts moved; this scans nothing")
        for script in self.SCRIPTS:
            source = re.sub(r"//.*", "", script.read_text(encoding="utf-8"))
            for name in ("prompt", "confirm", "alert"):
                with self.subTest(script=script.name, dialog=name):
                    self.assertNotRegex(
                        source, r"(?<![.\w])(window\.)?" + name + r"\s*\(",
                        f"{script.name} calls {name}(); ask() is the in-page replacement",
                    )

    def test_the_page_carries_the_markup_that_replaces_them(self):
        """The one generic dialog that asked for a name or for a yes is gone: each
        question is its own form now, and "are you sure" is templates/_confirm.html
        — a control that turns into its own confirmation with no request behind
        it."""
        self.board_with(pool=1)
        html = self.client.get("/rankings").text
        for element in ('id="newBoardModal"', 'id="renameBoardModal"',
                        'id="newTierModal"', 'id="confirm-board-delete"'):
            self.assertIn(element, html)
        self.assertNotIn('id="askModal"', html)


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
        for url in (f"/rankings/fragments/pool?board={board}&after=",
                    f"/rankings/fragments/tier?board={board}&tier=tier-s",
                    "/rankings"):
            with self.subTest(url=url):
                self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_another_account_cannot_read_this_ones_board_through_a_fragment(self):
        """The cross-tenant control, at the one surface that renders HTML rather
        than JSON. It answers as it would for a board that does not exist."""
        board = self.board_with(pool=3, tiered=2)
        self.sign_in_as(self.make_user("other", ranker_approved=True))
        html = self.client.get(f"/rankings/fragments/pool?board={board}&after=").text
        self.assertIn("ranker-failed", html)
        self.assertNotIn("Title 0", html)


class CategoryDeleteRouteTests(FragmentTestCase):
    """The tier delete: a button posting itself, answered with the page. Its
    titles fall back to the pool, and what the tier was goes into the board's
    removal history, which is what the toast's Undo restores from."""

    def delete(self, board: str, tier: str):
        return self.client.post(f"/rankings/boards/{board}/tiers/{tier}/delete", json={})

    def test_deleting_a_tier_returns_its_titles_to_the_pool(self):
        board = self.board_with(tiered=3)
        response = self.delete(board, "tier-s")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Tier deleted — its 3 titles went back to the pool.", response.text)
        after = self.client.get(f"/api/rankings/boards/{board}").json()["board"]
        self.assertEqual(after["categories"], [])
        self.assertEqual(len(after["pool"]), 3)

    def test_an_unknown_tier_is_refused_without_touching_the_board(self):
        board = self.board_with(tiered=2)
        response = self.delete(board, "ghost")
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="toast err is-flash"', response.text)
        self.assertIn("No tier", response.text)
        after = self.client.get(f"/api/rankings/boards/{board}").json()["board"]
        self.assertEqual(len(after["categories"][0]["items"]), 2)

    def test_another_account_cannot_delete_a_tier_and_the_rows_prove_it(self):
        board = self.board_with(tiered=2)
        self.sign_in_as(self.make_user("intruder", ranker_approved=True))
        self.delete(board, "tier-s")
        # Asserted on the database, not on the status: a route that refuses and
        # writes anyway is exactly what a status-only check misses.
        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM tier_categories")), 1)
        self.assertEqual(
            asyncio.run(db.fetch_value("SELECT COUNT(*) FROM tier_category_removals")), 0)

    def test_the_retired_json_delete_is_gone(self):
        """The delete used to be a JSON route the page called and then patched
        itself up after. Nothing calls it now, and an authenticated route nobody
        calls is a surface with no caller exercising its refusals."""
        self.assertNotIn(
            "/api/rankings/boards/{board_uid}/categories/{category_uid}",
            {str(getattr(r, "path", "")) for r in authz.iter_routes(app)})


def png_bytes(colour=(200, 30, 30)) -> str:
    """A small PNG, base64'd, as an upload body carries one."""
    import base64
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class SavedImageRouteTests(FragmentTestCase):
    """The account's saved pictures, which the export dialog chooses between.

    THE LIBRARY IS MARKUP NOW, so these ask the fragment what it drew rather than
    a JSON list what it holds. The one route that still answers bytes is the
    picture itself, which is what an <img> asks for."""

    def test_the_library_starts_empty_and_reports_the_cap(self):
        html = self.client.get("/me/fragments/images").text
        self.assertIn("Nothing saved yet.", html)
        self.assertIn(f"0 of {user_images.MAX_IMAGES_PER_USER} saved.", html)

    def test_an_uploaded_image_is_drawn_in_the_library_and_serves(self):
        added = self.client.post("/me/images", json={"image_b64": png_bytes()})
        self.assertEqual(added.status_code, 200)
        uid = re.search(r'/api/me/images/([0-9a-f]{32})', added.text).group(1)
        self.assertIn('value="Image 1"', added.text)
        self.assertIn(f"1 of {user_images.MAX_IMAGES_PER_USER} saved.", added.text)

        served = self.client.get(f"/api/me/images/{uid}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-type"], "image/webp")

    def test_naming_and_removing_answer_with_the_library(self):
        added = self.client.post("/me/images", json={"image_b64": png_bytes()})
        uid = re.search(r'/api/me/images/([0-9a-f]{32})', added.text).group(1)
        named = self.client.post(f"/me/images/{uid}/name", json={"name": "Beach"})
        self.assertIn('value="Beach"', named.text)
        gone = self.client.post(f"/me/images/{uid}/delete", json={})
        self.assertIn("Nothing saved yet.", gone.text)

    def test_an_image_belonging_to_somebody_else_is_not_served_or_touched(self):
        added = self.client.post("/me/images", json={"image_b64": png_bytes((0, 0, 0))})
        uid = re.search(r'/api/me/images/([0-9a-f]{32})', added.text).group(1)
        self.sign_in_as(self.make_user("stranger"))
        self.assertEqual(self.client.get(f"/api/me/images/{uid}").status_code, 404)
        # A stranger's delete says "No such image" and leaves the owner's alone,
        # asserted on the store rather than on the answer.
        self.assertIn("No such image.",
                      self.client.post(f"/me/images/{uid}/delete", json={}).text)
        self.sign_in_as(self.user_id)
        self.assertIn(uid, self.client.get("/me/fragments/images").text)

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
        # The page's own stylesheet is now a section of the one bundled sheet,
        # so only its scripts are separate files to bust.
        from app import assets
        for name in ("static/js/ranker/core.js", "static/js/sortable.min.js"):
            self.assertIn(name, assets._CACHED_ASSETS)
            self.assertTrue((assets.BASE_DIR / name).exists(), f"{name} should exist")


if __name__ == "__main__":
    unittest.main()
