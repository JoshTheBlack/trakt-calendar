"""A network's Discord emoji code, edited on the tracker's own rows.

WHAT IS HELD HERE: the map keeps only what somebody chose (an empty code or the
default removes the entry, and nothing is registered for them); a save changes
ONE key, so two tabs editing two networks keep both; the answer redraws every
row's cell for that network and both posts, out of band, and nothing else; and
each account's map stays its own.

The browser half — Enter saving once, a click away saving once, nothing sent for
a click in and out — is htmx's `change` trigger and is measured in the browser,
not here.
"""
from __future__ import annotations

import asyncio
import re
from html import unescape
from unittest.mock import AsyncMock, patch

from app import distrakt as distrakt_store
from app.distrakt import post_memo, prefs
from app.providers.base import Media

from tests.distrakt.test_month_fragment import KEY, MonthFragmentTestCase


def _ref(network: str) -> str:
    return prefs.network_ref(network)


def _oob_cell(html: str, network: str) -> str:
    """The out-of-band cell an answer carries for `network`, or ""."""
    found = re.search(
        rf'<span class="distrakt-emoji[^"]*" data-net="{_ref(network)}"\s*'
        rf'hx-swap-oob="outerHTML:\.distrakt-emoji\[data-net=\'{_ref(network)}\'\]">.*?</span>\s*</span>',
        html, re.S)
    return found.group(0) if found else ""


def _post(html: str, which: int) -> str | None:
    found = re.search(rf'<textarea id="post{which}Text"[^>]*>(.*?)</textarea>', html, re.S)
    return unescape(found.group(1)) if found else None


class TheMapKeepsOnlyWhatWasChosenTests(MonthFragmentTestCase):

    def prefs_now(self):
        return asyncio.run(distrakt_store.get_emoji_prefs(self.user_id))

    def test_a_code_sets_one_key(self):
        asyncio.run(prefs.set_network_emoji(self.user_id, "HBO", " :hbo: "))
        self.assertEqual(self.prefs_now(), ({"HBO": ":hbo:"}, ":tv:"))

    def test_an_empty_code_puts_the_network_back_on_the_default(self):
        asyncio.run(prefs.set_network_emoji(self.user_id, "HBO", ":hbo:"))
        asyncio.run(prefs.set_network_emoji(self.user_id, "HBO", "   "))
        self.assertEqual(self.prefs_now(), ({}, ":tv:"))

    def test_a_code_equal_to_the_default_is_no_entry_and_so_is_every_other(self):
        """An entry equal to the default says nothing an absent one does not, and
        stops following the default when it changes — so a save sweeps them,
        including the ones a roster once filed for every network it named."""
        asyncio.run(distrakt_store.set_emoji_prefs(
            self.user_id, {"AMC": ":tv:", "Netflix": ":tv:", "FX": ":fx:"}, ":tv:"))
        asyncio.run(prefs.set_network_emoji(self.user_id, "HBO", ":tv:"))
        self.assertEqual(self.prefs_now(), ({"FX": ":fx:"}, ":tv:"))

    def test_two_tabs_editing_two_networks_keep_both(self):
        """Each tab holds the map as it was drawn; a save that sent its whole copy
        back would undo the other tab's. One key per save cannot."""
        asyncio.run(prefs.set_network_emoji(self.user_id, "HBO", ":hbo:"))
        asyncio.run(prefs.set_network_emoji(self.user_id, "AMC", ":amc:"))
        self.assertEqual(self.prefs_now()[0], {"HBO": ":hbo:", "AMC": ":amc:"})

    def test_a_code_is_capped_not_checked(self):
        """A plain unicode emoji is as valid a code as `:name:` is."""
        asyncio.run(prefs.set_network_emoji(self.user_id, "HBO", "📺"))
        asyncio.run(prefs.set_network_emoji(self.user_id, "AMC", "x" * 500))
        emojis = self.prefs_now()[0]
        self.assertEqual(emojis["HBO"], "📺")
        self.assertEqual(len(emojis["AMC"]), prefs.MAX_CODE_LENGTH)

    def test_a_new_default_takes_the_entries_that_restated_the_old_one_with_it(self):
        """Drawn as "no code of its own", so they move with the default rather
        than keeping the old one the moment it changes."""
        asyncio.run(distrakt_store.set_emoji_prefs(
            self.user_id, {"AMC": ":tv:", "FX": ":fx:", "HBO": ":film:"}, ":tv:"))
        asyncio.run(prefs.set_default_emoji(self.user_id, ":film:"))
        self.assertEqual(self.prefs_now(), ({"FX": ":fx:"}, ":film:"))

    def test_an_empty_default_is_the_apps_own(self):
        asyncio.run(prefs.set_default_emoji(self.user_id, "  "))
        self.assertEqual(self.prefs_now()[1], distrakt_store.DEFAULT_EMOJI)

    def test_drawing_and_adding_register_no_network(self):
        """Nothing is filed for the viewer: the rows name every network the month
        has, which is what the map's own editor used to need the filing for."""
        self.list_one()
        self.month(*self.current)
        self.assertEqual(self.prefs_now(), ({}, ":tv:"))


class TheRowDrawsItsCodeTests(MonthFragmentTestCase):

    def setUp(self):
        super().setUp()
        self.list_one()

    def cell(self, html: str, network: str = "HBO") -> str:
        found = re.search(rf'<span class="distrakt-emoji[^"]*" data-net="{_ref(network)}">.*?</span>\s*</span>',
                          html, re.S)
        self.assertIsNotNone(found, html[:2000])
        return found.group(0)

    def test_the_field_saves_itself_on_change_and_swaps_nothing_of_its_own(self):
        year, month = self.current
        cell = unescape(self.cell(self.month(year, month).html))
        self.assertIn(f'hx-post="/api/distrakt/emoji?year={year}&month={month}"', cell)
        self.assertIn('hx-trigger="change"', cell)
        self.assertIn('hx-swap="none"', cell)
        self.assertIn("hx-json-enc", cell)
        self.assertIn('name="code"', cell)
        self.assertIn("""hx-vals='{"network": "HBO"}'""", cell)

    def test_a_network_with_no_code_shows_the_default_dimmed(self):
        cell = self.cell(self.month(*self.current).html)
        self.assertIn('class="distrakt-emoji is-default"', cell)
        self.assertIn('value=":tv:"', cell)

    def test_a_network_with_its_own_code_shows_it(self):
        asyncio.run(prefs.set_network_emoji(self.user_id, "HBO", ":hbo:"))
        cell = self.cell(self.month(*self.current).html)
        self.assertIn('class="distrakt-emoji"', cell)
        self.assertIn('value=":hbo:"', cell)

    def test_a_row_with_no_network_and_a_film_row_draw_no_code(self):
        html = self._drawn({
            "shows": [{"key": str(KEY), "season": 1, "title": "Nowhere", "bucket": "cleanup",
                       "network": "", "count_label": "0/1", "count_split": False}],
            "movies": [{"key": "movie:tmdb:7", "title": "A Film", "watched_at": "2026-01-02"}]})
        self.assertNotIn("distrakt-emoji-code", html)

    def test_the_badge_is_the_logo_alone(self):
        """No logo, no fallback: the code has a cell of its own."""
        html = self.month(*self.current).html
        badge = re.search(r'<span class="distrakt-badge"[^>]*>(.*?)</span>', html, re.S)
        self.assertEqual(badge.group(1).strip(), "")


class SavingACodeTests(MonthFragmentTestCase):

    def setUp(self):
        super().setUp()
        post_memo.forget_all()
        self.list_one()
        year, month = self.current
        self.address = f"/api/distrakt/emoji?year={year}&month={month}"

    def save(self, network: str, code: str):
        return self.client.post(self.address, json={"network": network, "code": code})

    def test_the_answer_redraws_every_copy_of_that_networks_cell(self):
        self.month(*self.current)
        resp = self.save("HBO", ":hbo:")
        self.assertEqual(resp.status_code, 200)
        cell = _oob_cell(resp.text, "HBO")
        self.assertIn('value=":hbo:"', cell)
        self.assertNotIn("is-default", cell.split(">")[0])
        # A selector, not an id: every row on HBO is one of its targets.
        self.assertEqual(resp.text.count("hx-swap-oob="), 3)  # the cell and two posts

    def test_the_posts_carry_the_new_code_and_nothing_else_changes(self):
        drawn = self.month(*self.current)
        before = _post(drawn.html, 2)
        self.assertIn(":tv:`Listed Show S01", before)
        resp = self.save("HBO", ":hbo:")
        after = _post(resp.text, 2)
        self.assertEqual(after, before.replace(":tv:`Listed Show S01", ":hbo:`Listed Show S01"))
        self.assertEqual(_post(resp.text, 1), _post(drawn.html, 1))
        self.assertNotIn("HX-Trigger", resp.headers)

    def test_an_empty_code_falls_back_to_the_default(self):
        self.month(*self.current)
        self.save("HBO", ":hbo:")
        resp = self.save("HBO", "")
        cell = _oob_cell(resp.text, "HBO")
        self.assertIn('class="distrakt-emoji is-default"', cell)
        self.assertIn('value=":tv:"', cell)
        self.assertIn(":tv:`Listed Show S01", _post(resp.text, 2))

    def test_with_nothing_kept_the_page_is_asked_to_fetch_its_month(self):
        """A restart since the month was drawn: the cell is still redrawn, and the
        posts come with the month the page then fetches."""
        resp = self.save("HBO", ":hbo:")
        self.assertIn("month-changed", resp.headers.get("HX-Trigger", ""))
        self.assertIsNone(_post(resp.text, 2))
        self.assertIn('value=":hbo:"', _oob_cell(resp.text, "HBO"))

    def test_two_tabs_saving_two_networks_keep_both(self):
        """Both tabs were drawn before either saved; each sends one network."""
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "media": Media.SHOW, "ids": {"trakt": 501, "tmdb": 501},
            "season": 1, "title": "Other Show", "network": "AMC",
            "kind": distrakt_store.RecordKind.KEEPUP}))
        self.month(*self.current)
        self.save("HBO", ":hbo:")
        resp = self.save("AMC", ":amc:")
        self.assertEqual(asyncio.run(distrakt_store.get_emoji_prefs(self.user_id))[0],
                         {"HBO": ":hbo:", "AMC": ":amc:"})
        post2 = _post(resp.text, 2)
        self.assertIn(":hbo:`Listed Show", post2)
        self.assertIn(":amc:`Other Show", post2)

    def test_a_network_that_is_not_one_is_refused(self):
        for network in ("", "   ", "x" * (prefs.MAX_NETWORK_LENGTH + 1)):
            with self.subTest(network=network[:10]):
                resp = self.save(network, ":x:")
                self.assertIn("toast err", resp.text)
        self.assertEqual(asyncio.run(distrakt_store.get_emoji_prefs(self.user_id))[0], {})

    def test_the_default_is_saved_by_its_own_field_and_the_month_follows(self):
        year, month = self.current
        resp = self.client.post(f"/api/distrakt/emoji-default?year={year}&month={month}",
                                json={"code": ":film:"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("month-changed", resp.headers.get("HX-Trigger", ""))
        self.assertRegex(resp.text, r'id="emojiDefaultPillLabel" hx-swap-oob="outerHTML"')
        self.assertNotIn('id="emojiDefaultPill"', resp.text)
        self.assertIn("🙂 :film:", resp.text)
        self.assertRegex(resp.text, r'<input type="text" id="emojiDefault" name="code" value=":film:"')
        self.assertEqual(asyncio.run(distrakt_store.get_emoji_prefs(self.user_id)), ({}, ":film:"))
        # Empty means the app's own default.
        self.client.post(f"/api/distrakt/emoji-default?year={year}&month={month}", json={"code": ""})
        self.assertEqual(asyncio.run(distrakt_store.get_emoji_prefs(self.user_id))[1],
                         distrakt_store.DEFAULT_EMOJI)

    def test_one_accounts_save_is_not_anothers(self):
        """The map renders into ONE account's posts, and what the answer re-renders
        from is kept per account: a neighbour saving the same network changes
        neither this map nor these posts."""
        self.month(*self.current)
        neighbour = self.make_user("neighbour", distrakt_approved=True, calendar_approved=True)
        self.link_identity(neighbour, "trakt", 901, "tok2")
        self.sign_in_as(neighbour)  # the session cookie replaces itself by name
        resp = self.save("HBO", ":theirs:")
        # Nothing was drawn for them, so nothing of this account's is re-rendered.
        self.assertIsNone(_post(resp.text, 2))
        self.assertEqual(asyncio.run(distrakt_store.get_emoji_prefs(self.user_id))[0], {})
        self.assertEqual(asyncio.run(distrakt_store.get_emoji_prefs(neighbour))[0],
                         {"HBO": ":theirs:"})


class TheLogoToolsTests(MonthFragmentTestCase):
    """Under a code while it is edited: the network's name, its tile to download
    where there is one, and — for an administrator — making the tile again."""

    def setUp(self):
        super().setUp()
        self.list_one()
        year, month = self.current
        self.address = f"/api/distrakt/network-logo?year={year}&month={month}"

    def tools(self, html: str) -> str:
        found = re.search(r'<span class="distrakt-emoji-tools">(.*?)</span>\s*</span>', html, re.S)
        return found.group(1) if found else ""

    def test_download_is_offered_only_where_there_is_a_tile(self):
        with patch("app.media.logos.versions", return_value={}):
            self.assertNotIn("download", self.tools(self.month(*self.current).html))
        with patch("app.media.logos.versions", return_value={"HBO": 7}):
            tools = unescape(self.tools(self.month(*self.current).html))
        self.assertIn('href="/api/network-logo?name=HBO&v=7&download=1"', tools)
        self.assertIn('hx-boost="false"', tools)

    def test_only_an_administrator_is_offered_the_regenerate(self):
        self.assertNotIn("/api/distrakt/network-logo", self.month(*self.current).html)
        self.assertEqual(self.client.post(self.address, json={"network": "HBO"}).status_code, 403)

    def as_admin(self):
        admin = self.make_user("admin", is_admin=True, distrakt_approved=True,
                               calendar_approved=True)
        self.link_identity(admin, "trakt", 902, "tok3")
        asyncio.run(distrakt_store.add_user_record(admin, {
            "media": Media.SHOW, "ids": {"trakt": 500, "tmdb": 500}, "season": 1,
            "title": "Listed Show", "network": "HBO",
            "kind": distrakt_store.RecordKind.KEEPUP}))
        self.sign_in_as(admin)

    def test_an_administrator_regenerates_by_the_months_own_tmdb_id(self):
        self.as_admin()
        self.assertIn("/api/distrakt/network-logo", self.month(*self.current).html)
        made = AsyncMock(return_value="tile")
        with patch("app.media.logos.regenerate", made), \
                patch("app.media.logos.versions", return_value={"HBO": 9}):
            resp = self.client.post(self.address, json={"network": "HBO"})
        self.assertEqual(made.await_args.args[1:], ("HBO", 500))
        # Every badge and every code cell for HBO, with the new tile's version.
        self.assertIn(f"hx-swap-oob=\"outerHTML:.distrakt-badge[data-net='{_ref('HBO')}']\"", resp.text)
        self.assertIn(f"hx-swap-oob=\"outerHTML:.distrakt-emoji[data-net='{_ref('HBO')}']\"", resp.text)
        self.assertIn("name=HBO&amp;v=9", resp.text)
        self.assertIn("toast ok", resp.text)

    def test_a_regenerate_that_made_nothing_says_the_old_tile_is_kept(self):
        self.as_admin()
        self.month(*self.current)
        with patch("app.media.logos.regenerate", AsyncMock(return_value=None)), \
                patch("app.media.logos.versions", return_value={"HBO": 9}):
            resp = self.client.post(self.address, json={"network": "HBO"})
        self.assertIn("the old one is kept", resp.text)
        self.assertIn("toast err", resp.text)

    def test_a_network_the_month_names_no_tmdb_id_for_is_refused(self):
        self.as_admin()
        with patch("app.media.logos.regenerate", AsyncMock()) as made:
            resp = self.client.post(self.address, json={"network": "Nowhere TV"})
        made.assert_not_awaited()
        self.assertIn("toast err", resp.text)
