"""Every control on the rankings board that posts itself, held to the same three
rules and to one more.

THE THREE REFUSALS ARE THE POINT OF THE FILE, and they are asserted route by
route rather than in prose, because each new control is a new place to forget
one:

  - A FORM-ENCODED BODY IS REFUSED WITH 415, before routing, by the app-wide
    request-shape guard. These controls are forms now, and a form's native
    encoding is exactly what that rule exists to refuse — `hx-json-enc` is what
    makes them send `application/json` instead. A regression reads to a user as
    "pressing this does nothing", which is why it is scanned rather than trusted.
  - ANOTHER ACCOUNT'S BOARD CHANGES NOTHING. Asserted on the database after the
    hostile request, because a route that answers politely and writes anyway
    passes a status-only test.
  - A BAD VALUE IS REFUSED AND WRITES NOTHING.

AND THE FOURTH: A SERVER ACTION BUMPS THE BOARD'S VERSION, AND THE PAGE IT
ANSWERS WITH CARRIES THE NEW ONE. That is what stops the next drag saving against
a board the server has moved on from and earning the 409 that means "another tab
changed this" — so each action is followed by a real save at the version the
answer drew.
"""
from __future__ import annotations

import asyncio
import json
import re

from app import db
from app.ranker import core as ranker
from tests.support import STATIC_DIR, AppTestCase

BOARD = "b1"


def key(match_id: str) -> str:
    return f"show:tmdb:{match_id}"


def show_ref(match_id: str, title: str = "") -> dict:
    return {"media": "show", "match_source": "tmdb", "match_id": match_id,
            "tmdb": int(match_id), "title": title or f"Title {match_id}", "year": 2026}


class _ActionTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin_user", is_admin=True)
        self.user_id = self.make_user("ranker_user", ranker_approved=True)
        self.sign_in_as(self.user_id)
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD, name="Top"))
        asyncio.run(ranker.add_titles(
            self.user_id, BOARD, [show_ref("1"), show_ref("2"), show_ref("3")]))

    # -- helpers ------------------------------------------------------------
    def rows(self, sql: str, params=()) -> list:
        return asyncio.run(db.fetch_all(sql, params))

    def value(self, sql: str, params=()):
        return asyncio.run(db.fetch_value(sql, params))

    def version(self) -> int:
        return int(self.value("SELECT version FROM tier_boards WHERE uid = ?", (BOARD,)))

    def drawn_version(self, page) -> int:
        return int(re.search(r'"version": (\d+)', page.text).group(1))

    def tier_uid(self) -> str:
        board = asyncio.run(ranker.fetch_board(self.user_id, BOARD))
        return board["categories"][0]["uid"]

    def save_at(self, version: int, **layout):
        """A drag, at the version the page said. 409 is what a stale one earns."""
        board = asyncio.run(ranker.fetch_board(self.user_id, BOARD))
        payload = {
            "version": version,
            "categories": [{"uid": c["uid"], "label": c["label"],
                            "rank_priority": c["rank_priority"],
                            "is_isolated": c["is_isolated"], "colour": c["colour"],
                            "items": [i["key"] for i in c["items"]]}
                           for c in board["categories"]],
            "pool": [i["key"] for i in board["pool"]],
        }
        payload.update(layout)
        return self.client.post(f"/api/rankings/boards/{BOARD}/save", json=payload)

    def assertDragStillSaves(self, page):
        """The page an action answered with carries the version the next save has
        to echo, so a drag a moment later is accepted rather than 409-ing."""
        self.assertEqual(self.drawn_version(page), self.version())
        saved = self.save_at(self.drawn_version(page))
        self.assertEqual(saved.status_code, 200, saved.text)


# Every converted control, as (path, a body it would ordinarily send). One table,
# so a route added without its refusals is a route missing from a list somebody
# has to look at.
ACTIONS = (
    ("/rankings/boards/new", {"name": "Another"}),
    (f"/rankings/boards/{BOARD}/rename", {"name": "Renamed"}),
    (f"/rankings/boards/{BOARD}/clone", {}),
    (f"/rankings/boards/{BOARD}/delete", {}),
    (f"/rankings/boards/{BOARD}/tiers/new", {"label": "S"}),
    (f"/rankings/boards/{BOARD}/tiers/template", {}),
    (f"/rankings/boards/{BOARD}/tiers/tier-s/settings",
     {"label": "S", "rank_priority": "10", "colour": "#FF0000"}),
    (f"/rankings/boards/{BOARD}/items/remove", {"key": key("1")}),
    (f"/rankings/boards/{BOARD}/items/move", {f"pick:{key('1')}": "1", "tier": "tier-s"}),
    (f"/rankings/boards/{BOARD}/import", {"media": "show"}),
    (f"/rankings/boards/{BOARD}/seed", {}),
    (f"/rankings/boards/{BOARD}/header-image", {"image_b64": "", "name": "x"}),
    (f"/rankings/boards/{BOARD}/removals/restore", {"key": key("1")}),
    ("/api/rankings/restore", {"payload": "{}", "ack": "REPLACE MY BOARDS"}),
)


class EveryControlSendsJsonTests(_ActionTestCase):
    def test_a_form_encoded_body_is_refused_before_it_reaches_a_route(self):
        """415 from the request-shape guard, which runs before routing. It is
        CSRF defence-in-depth behind the session cookie's SameSite=Lax: a
        cross-origin HTML form can only send form-encoded or text/plain, so this
        makes that POST unreachable."""
        for path, body in ACTIONS:
            with self.subTest(path=path):
                resp = self.client.post(path, data={k: str(v) for k, v in body.items()})
                self.assertEqual(resp.status_code, 415, resp.text)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 3)
        self.assertEqual(self.version(), 1)

    def test_every_control_that_writes_carries_the_encoder_that_makes_it_json(self):
        """The markup half of the same rule: a form posting without hx-json-enc
        would send its native encoding and be refused by the guard above, which
        the person pressing it experiences as nothing happening."""
        html = self.client.get(f"/rankings?board={BOARD}").text
        for element in re.findall(r"<(?:form|button|input|select)\b[^>]*hx-post[^>]*>", html):
            with self.subTest(element=element[:70]):
                self.assertIn("hx-json-enc", element)

    def test_a_read_inside_a_writing_form_opts_out_of_the_encoder(self):
        """MEASURED IN THE BROWSER, and it fails silently. The encoder is turned
        on by an attribute htmx looks up THROUGH INHERITANCE, so a GET inside a
        json-enc form inherits it — and a GET has no body to encode, because htmx
        has already moved its fields onto the address and set the body to null.
        The encoder walks that null, throws, and the request is dropped with
        nothing but a console line to say so."""
        html = self.client.get(f"/rankings?board={BOARD}").text
        for form in re.findall(r"<form\b[^>]*hx-json-enc[^>]*>(.*?)</form>", html, re.S):
            for element in re.findall(r"<[a-z]+\b[^>]*hx-get[^>]*>", form):
                with self.subTest(element=element[:70]):
                    self.assertIn('hx-json-enc="false"', element)


class CrossTenantTests(_ActionTestCase):
    """A board that is not the caller's is not writable through any of them, and
    that is asserted on the victim's rows rather than on a status code."""

    def setUp(self):
        super().setUp()
        self.victim = self.make_user("victim", ranker_approved=True)
        asyncio.run(ranker.create_board(self.victim, uid="theirs", name="Theirs"))
        asyncio.run(ranker.add_titles(self.victim, "theirs", [show_ref("9")]))
        asyncio.run(ranker.save_layout(self.victim, "theirs", {
            "version": 1,
            "categories": [{"uid": "tier-s", "label": "S", "items": [key("9")]}],
            "pool": []}))
        self.before = self._snapshot()

    def _snapshot(self) -> list[tuple]:
        return [tuple(row) for row in self.rows(
            "SELECT b.uid, b.name, b.version, c.uid, c.label, i.match_id, i.category_id "
            "  FROM tier_boards b "
            "  LEFT JOIN tier_categories c ON c.board_id = b.id "
            "  LEFT JOIN tier_items i ON i.board_id = b.id "
            " WHERE b.user_id = ? ORDER BY c.uid, i.match_id", (self.victim,))]

    def test_no_control_reaches_another_accounts_board(self):
        for path, body in ACTIONS:
            hostile = path.replace(f"/{BOARD}/", "/theirs/")
            if hostile == path:
                continue        # not addressed at a board at all
            with self.subTest(path=hostile):
                self.client.post(hostile, json=body)
                self.assertEqual(self._snapshot(), self.before)


class BadValueTests(_ActionTestCase):
    def test_a_refusal_is_drawn_on_the_page_and_writes_nothing(self):
        """A non-200 is never swapped, so an answer that replaces the page has to
        BE a page — the reason is a sentence on it rather than a status code."""
        cases = [
            (f"/rankings/boards/{BOARD}/rename", {"name": "x" * 61}, "at most 60"),
            (f"/rankings/boards/{BOARD}/tiers/new", {"label": "x" * 41}, "at most 40"),
            (f"/rankings/boards/{BOARD}/items/remove", {"key": "not-a-key"}, "key"),
            (f"/rankings/boards/{BOARD}/items/move",
             {"tier": "tier-s"}, "Nothing was selected"),
            (f"/rankings/boards/{BOARD}/items/move",
             {f"pick:{key('1')}": "1", "tier": "nope"}, "No tier"),
            (f"/rankings/boards/{BOARD}/import", {"media": "show", "year": "soon"},
             "whole number"),
        ]
        for path, body, says in cases:
            with self.subTest(path=path, body=body):
                resp = self.client.post(path, json=body)
                self.assertEqual(resp.status_code, 200)
                self.assertIn(says, resp.text)
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_items"), 3)

    def test_a_tier_setting_outside_its_range_is_refused(self):
        asyncio.run(ranker.add_categories(
            self.user_id, BOARD, [{"uid": "tier-s", "label": "S"}]))
        for body in ({"label": "S", "rank_priority": "99999", "colour": "#FF0000"},
                     {"label": "S", "rank_priority": "1", "colour": "not a colour"}):
            with self.subTest(body=body):
                resp = self.client.post(
                    f"/rankings/boards/{BOARD}/tiers/tier-s/settings", json=body)
                self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.value(
            "SELECT rank_priority FROM tier_categories WHERE uid = 'tier-s'"), 0)


class VersionTests(_ActionTestCase):
    """EVERY ACTION THAT CHANGES THE BOARD BUMPS ITS VERSION AND HANDS THE NEW ONE
    BACK. The page these answer with is what the next drag saves from, so an
    action that bumped without redrawing — or redrew without bumping — is a board
    that refuses its own next drag."""

    def test_adding_a_tier_bumps_it_and_the_next_drag_still_saves(self):
        before = self.version()
        page = self.client.post(f"/rankings/boards/{BOARD}/tiers/new", json={"label": "S"})
        self.assertEqual(self.version(), before + 1)
        self.assertDragStillSaves(page)

    def test_the_template_bumps_it_once_and_a_second_press_changes_nothing(self):
        before = self.version()
        page = self.client.post(f"/rankings/boards/{BOARD}/tiers/template", json={})
        self.assertEqual(self.version(), before + 1)
        self.assertEqual(self.value("SELECT COUNT(*) FROM tier_categories"),
                         len(ranker.TIER_TEMPLATE))
        again = self.client.post(f"/rankings/boards/{BOARD}/tiers/template", json={})
        self.assertIn("already on this board", again.text)
        self.assertEqual(self.version(), before + 1)
        self.assertDragStillSaves(again)

    def test_writing_a_tiers_settings_bumps_it(self):
        self.client.post(f"/rankings/boards/{BOARD}/tiers/new", json={"label": "S"})
        uid = self.tier_uid()
        before = self.version()
        page = self.client.post(f"/rankings/boards/{BOARD}/tiers/{uid}/settings", json={
            "label": "Top", "rank_priority": "60", "colour": "#FF7F7F",
            "is_isolated": ["0", "1"]})
        self.assertEqual(self.version(), before + 1)
        self.assertEqual(self.value(
            "SELECT is_isolated FROM tier_categories WHERE uid = ?", (uid,)), 1)
        self.assertDragStillSaves(page)

    def test_the_hidden_companion_is_what_lets_isolation_be_turned_off(self):
        """An unchecked box sends nothing at all, and this form writes every one
        of a tier's settings — so absent has to mean off rather than leave alone.
        The hidden input before the box is what makes "off" sayable."""
        self.client.post(f"/rankings/boards/{BOARD}/tiers/new", json={"label": "S"})
        uid = self.tier_uid()
        settings = {"label": "S", "rank_priority": "0", "colour": "#FF7F7F"}
        self.client.post(f"/rankings/boards/{BOARD}/tiers/{uid}/settings",
                         json={**settings, "is_isolated": ["0", "1"]})
        self.assertEqual(self.value(
            "SELECT is_isolated FROM tier_categories WHERE uid = ?", (uid,)), 1)
        self.client.post(f"/rankings/boards/{BOARD}/tiers/{uid}/settings",
                         json={**settings, "is_isolated": "0"})
        self.assertEqual(self.value(
            "SELECT is_isolated FROM tier_categories WHERE uid = ?", (uid,)), 0)
        html = self.client.get(
            f"/rankings/fragments/tier-settings?board={BOARD}&tier={uid}").text
        self.assertIn('<input type="hidden" name="is_isolated" value="0">', html)

    def test_removing_a_title_bumps_it_and_the_next_drag_still_saves(self):
        before = self.version()
        page = self.client.post(f"/rankings/boards/{BOARD}/items/remove",
                                json={"key": key("1")})
        self.assertEqual(self.version(), before + 1)
        self.assertDragStillSaves(page)

    def test_a_bulk_move_bumps_it_and_the_next_drag_still_saves(self):
        self.client.post(f"/rankings/boards/{BOARD}/tiers/new", json={"label": "S"})
        uid = self.tier_uid()
        before = self.version()
        page = self.client.post(f"/rankings/boards/{BOARD}/items/move", json={
            f"pick:{key('2')}": "1", f"pick:{key('1')}": "1", "tier": uid})
        self.assertEqual(self.version(), before + 1)
        self.assertIn("Moved 2 title(s).", page.text)
        board = asyncio.run(ranker.fetch_board(self.user_id, BOARD))
        # In the order the fields arrived, appended after whatever the tier held.
        self.assertEqual([i["key"] for i in board["categories"][0]["items"]],
                         [key("2"), key("1")])
        self.assertDragStillSaves(page)

    def test_a_bulk_move_appends_rather_than_reshuffling(self):
        self.client.post(f"/rankings/boards/{BOARD}/tiers/new", json={"label": "S"})
        uid = self.tier_uid()
        self.client.post(f"/rankings/boards/{BOARD}/items/move",
                         json={f"pick:{key('3')}": "1", "tier": uid})
        self.client.post(f"/rankings/boards/{BOARD}/items/move",
                         json={f"pick:{key('1')}": "1", "tier": uid})
        board = asyncio.run(ranker.fetch_board(self.user_id, BOARD))
        self.assertEqual([i["key"] for i in board["categories"][0]["items"]],
                         [key("3"), key("1")])

    def test_moving_back_to_the_pool_is_the_empty_tier(self):
        self.client.post(f"/rankings/boards/{BOARD}/tiers/new", json={"label": "S"})
        uid = self.tier_uid()
        self.client.post(f"/rankings/boards/{BOARD}/items/move",
                         json={f"pick:{key('1')}": "1", "tier": uid})
        self.client.post(f"/rankings/boards/{BOARD}/items/move",
                         json={f"pick:{key('1')}": "1", "tier": ""})
        board = asyncio.run(ranker.fetch_board(self.user_id, BOARD))
        self.assertEqual(board["categories"][0]["items"], [])
        self.assertIn(key("1"), [i["key"] for i in board["pool"]])

    def test_an_import_bumps_nothing_it_did_not_change(self):
        """Adding titles to the pool leaves the arrangement alone, so a drag from
        before it is still a save the server accepts."""
        before = self.version()
        page = self.client.post(f"/rankings/boards/{BOARD}/import", json={"media": "show"})
        self.assertEqual(self.drawn_version(page), self.version())
        self.assertEqual(self.version(), before)
        self.assertDragStillSaves(page)


class SelectionIsMarkupTests(_ActionTestCase):
    """The selection is one checkbox per row, and three readings of it — the
    highlight, the count and the bar — are the stylesheet's."""

    def test_every_pool_row_carries_the_box_the_move_submits(self):
        html = self.client.get(f"/rankings?board={BOARD}").text
        self.assertIn(f'name="pick:{key("1")}"', html)
        box = re.search(r'<input type="checkbox" class="ranker-pick"[^>]*>', html).group(0)
        # Hidden because it is not a control anybody operates, and
        # autocomplete="off" so a reload cannot come back with rows still ticked.
        self.assertIn("hidden", box)
        self.assertIn('autocomplete="off"', box)

    def test_the_bar_and_the_count_are_read_off_the_boxes(self):
        from tests.support import STATIC_DIR
        css = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn(
            ".ranker-pool-col:not(:has(#rankerPool .ranker-pick:checked)) .ranker-bulk "
            "{ display: none; }", css)
        self.assertIn('#poolSelectedCount::after { content: counter(picked) " selected"; }', css)
        self.assertIn("counter-reset: picked;", css)
        self.assertIn("counter-increment: picked;", css)

    def test_the_bar_is_drawn_after_the_rows_it_counts(self):
        """A CSS counter reads what has been counted BEFORE it in the document, so
        a readout above the rows would always say nought."""
        html = self.client.get(f"/rankings?board={BOARD}").text
        self.assertLess(html.index('id="rankerPool"'), html.index('id="poolBulk"'))

    def test_the_selection_travels_as_one_field_per_key(self):
        """json-enc turns a repeated name into a list only when there is more than
        one of it, so a selection of one would arrive as a string where a
        selection of two arrives as a list — and every reader would have to know
        that. One field per key is the calendar's chip shape."""
        self.client.post(f"/rankings/boards/{BOARD}/tiers/new", json={"label": "S"})
        uid = self.tier_uid()
        one = self.client.post(f"/rankings/boards/{BOARD}/items/move",
                               json={f"pick:{key('1')}": "1", "tier": uid})
        self.assertIn("Moved 1 title(s).", one.text)


class TierStripTests(_ActionTestCase):
    """The little row of posters in a tier's header is a VIEW OF WHAT THAT TIER
    HOLDS, so the server is the only thing that can draw it.

    THE DEFECT THIS CLOSES, reported from real use: a drag, a nudge and the move
    menu change which titles a tier holds without the server hearing about it
    until the save lands, so the strip the page was drawn with went on showing a
    title that had left — until something redrew the page. Every other action
    answers with the board and fixes it for free; those three ask for the strips
    once their save is accepted.
    """

    def setUp(self):
        super().setUp()
        asyncio.run(ranker.add_categories(self.user_id, BOARD, [
            {"uid": "tier-s", "label": "S"}, {"uid": "tier-a", "label": "A"}]))
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("1"), key("2")], "tier-s"))

    def strips(self) -> str:
        return self.client.get(f"/rankings/fragments/tier-strips?board={BOARD}").text

    def test_the_fragment_redraws_every_tiers_strip_out_of_band(self):
        """One answer for the whole board, because a drag moves a title OUT of one
        tier and INTO another — two of them change at once."""
        html = self.strips()
        self.assertEqual(
            re.findall(r'data-tier="([^"]*)"', html), ["tier-s", "tier-a"])
        for uid in ("tier-s", "tier-a"):
            with self.subTest(tier=uid):
                self.assertIn(
                    f"""hx-swap-oob="outerHTML:.ranker-strip[data-tier='{uid}']\"""", html)
        # The selector htmx splits on the FIRST colon must contain no space, or it
        # is read as a swap spec with modifiers instead.
        for value in re.findall(r'hx-swap-oob="([^"]*)"', html):
            with self.subTest(value=value):
                self.assertNotIn(" ", value)

    def test_it_follows_what_the_tier_actually_holds(self):
        self.assertEqual(self.strips().count("<img"), 2)
        asyncio.run(ranker.move_items(self.user_id, BOARD, [key("1")], "tier-a"))
        html = self.strips()
        strip = re.search(r"data-tier=\"tier-s\".*?</span>", html, re.S).group(0)
        self.assertEqual(strip.count("<img"), 1)

    def test_the_inline_strip_and_the_fragment_are_one_renderer(self):
        """A second copy of this markup is how the header and the tier would come
        to disagree about what the tier holds."""
        page = self.client.get(f"/rankings?board={BOARD}").text
        inline = re.search(r'<span class="ranker-strip"[^>]*data-tier="tier-s".*?</span>',
                           page, re.S).group(0)
        fragment = re.search(r'<span class="ranker-strip"[^>]*data-tier="tier-s".*?</span>',
                             self.strips(), re.S).group(0)
        self.assertEqual(inline, fragment.replace(
            """ hx-swap-oob="outerHTML:.ranker-strip[data-tier='tier-s']\"""", ""))

    def test_the_page_names_the_event_and_the_markup_owns_the_address(self):
        """The script says only that the tiers moved. A rename in one alone would
        leave every header silently stale."""
        page = self.client.get(f"/rankings?board={BOARD}").text
        refresher = re.search(r'<span id="tierStripsRefresh"[^>]*>', page, re.S).group(0)
        event = re.search(r'hx-trigger="([^"]+)"', refresher).group(1)
        self.assertIn(f"/rankings/fragments/tier-strips?board={BOARD}", refresher)
        # Nothing of its own is swapped in: the whole answer is out of band.
        self.assertIn('hx-swap="none"', refresher)
        save = (STATIC_DIR / "js" / "ranker" / "save.js").read_text(encoding="utf-8")
        self.assertIn(f"htmx.trigger('#tierStripsRefresh', '{event}')", save)
        self.assertNotIn("ranker-strip", save)

    def test_the_refresher_sits_outside_the_tiers_it_redraws(self):
        """This app turns attribute inheritance on and htmx looks a request's verb
        up through it, so an hx-get on an ancestor of the tiers would become the
        verb of every control inside them."""
        page = self.client.get(f"/rankings?board={BOARD}").text
        tiers = re.search(r'<div id="rankerTiers">(.*?)\n        </div>', page, re.S).group(1)
        self.assertNotIn("tierStripsRefresh", tiers)
        self.assertNotIn("hx-get", re.search(r'<div id="rankerTiers"[^>]*>', page).group(0))

    def test_a_board_that_went_away_redraws_nothing_rather_than_failing(self):
        self.assertEqual(
            self.client.get("/rankings/fragments/tier-strips?board=gone").text, "")


class ExportOptionsAreMarkupTests(_ActionTestCase):
    """The dialog's choices, its size readout and its Markdown all come from the
    server, which is what took a second copy of the renderer's layout arithmetic
    out of the browser."""

    def setUp(self):
        super().setUp()
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": 1,
            "categories": [{"uid": "tier-s", "label": "S", "rank_priority": 60,
                            "items": [key("1"), key("2"), key("3")]}],
            "pool": []}))

    def readout(self, **options):
        from urllib.parse import urlencode
        return self.client.get("/rankings/fragments/export?" + urlencode(
            {"board": BOARD, "top_x": 3, "columns": 3, "fmt": "webp", "scale": 1,
             "scope": "global", **options})).text

    def test_the_size_is_the_renderers_own_and_the_page_ships_no_constants(self):
        from app.ranker import grid_builder
        layout = grid_builder.compute_layout(3, columns=3, scale=1.0,
                                             show_titles=False, podium=False)
        self.assertIn(f"{layout.width} × {layout.height}px", self.readout())
        page = self.client.get(f"/rankings?board={BOARD}").text
        for constant in ("tile_w", "caption_h", "podium_ranks", "max_dimension"):
            with self.subTest(constant=constant):
                self.assertNotIn(constant, page)

    def test_a_canvas_too_tall_for_its_format_disables_download_and_says_why(self):
        """A hundred titles in three columns is a canvas WebP's encoder throws
        on — after thirty seconds of rendering. The readout refuses it in words
        before anything is allocated, which is the same check the download makes."""
        asyncio.run(ranker.add_titles(
            self.user_id, BOARD, [show_ref(str(n)) for n in range(100, 200)]))
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": "tier-s", "label": "S", "rank_priority": 60,
                            "items": [key(str(n)) for n in range(100, 200)]}],
            "pool": [key("1"), key("2"), key("3")]}))
        html = self.readout(top_x=100, columns=3, fmt="webp")
        self.assertIn("too tall for WEBP", html)
        self.assertNotIn("<img alt=", html)            # and no preview of it
        button = re.search(r'<button[^>]*id="exportGo"[^>]*>', html).group(0)
        self.assertIn("disabled", button)
        self.assertIn('hx-swap-oob="outerHTML"', button)
        # The same options in a format that can hold them draw one.
        self.assertIn("<img alt=", self.readout(top_x=100, columns=3, fmt="jpeg"))

    def test_nothing_tiered_says_so_rather_than_drawing_an_empty_grid(self):
        asyncio.run(ranker.save_layout(self.user_id, BOARD, {
            "version": self.version(),
            "categories": [{"uid": "tier-s", "label": "S", "items": []}],
            "pool": [key("1"), key("2"), key("3")]}))
        html = self.readout()
        self.assertIn("nothing tiered to export", html)
        self.assertIn("disabled", re.search(r'<button[^>]*id="exportGo"[^>]*>', html).group(0))

    def test_the_preview_address_carries_the_options_it_was_drawn_from(self):
        html = self.readout(title="Top Shows")
        src = re.search(r'<img alt="Preview of the poster grid" src="([^"]+)"', html).group(1)
        self.assertTrue(src.startswith("/rankings/preview.webp?"))
        self.assertIn("title=Top+Shows", src)

    def test_the_two_compound_choices_are_read_where_the_request_is(self):
        """One <select> can carry one value, so "which tier" and "which picture"
        each arrive as one — and are split apart on the server rather than in the
        browser."""
        self.assertIn("1. **Title 1**", self.readout(scope="cat:tier-s"))
        self.assertIn("Export scope must be", self.readout(scope="nonsense"))


class BackupRestoreIsAFormTests(_ActionTestCase):
    def test_the_document_travels_as_a_field_and_the_phrase_is_checked(self):
        doc = asyncio.run(ranker.export_user_data(self.user_id))
        refused = self.client.post("/api/rankings/restore", json={
            "payload": json.dumps(doc), "ack": "nope"})
        self.assertIn("REPLACE MY BOARDS", refused.text)
        self.assertNotIn("<html", refused.text)
        accepted = self.client.post("/api/rankings/restore", json={
            "payload": json.dumps(doc), "ack": "REPLACE MY BOARDS"})
        self.assertIn("Restored 1 board(s).", accepted.text)

    def test_the_form_states_the_phrase_the_route_checks(self):
        from app.ranker import routes as ranker_routes
        html = self.client.get("/rankings").text
        field = re.search(r'<input type="text" id="backupAck"[^>]*>', html, re.S).group(0)
        self.assertIn(f'pattern="{ranker_routes.RESTORE_ACK}"', field)
        self.assertIn("required", field)
