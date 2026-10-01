"""The Share panel as the server draws it: a fragment every mutation answers.

WHAT THIS FILE HOLDS IT TO:
  - the panel is fetched, never shipped with the calendar page, and the calendar
    page no longer carries the per-calendar source map the panel's script read;
  - each control posts itself as JSON and is answered with the part it changed —
    the link box for the kind, a name and Rotate; the link box plus the Sources
    ticks for the display options, the ticks only when the link's calendar moved;
  - a custom view carries the services ticked and a pinned month into the link;
  - a taken name is refused in a sentence and a free one is saved;
  - ROTATING A LINK INVALIDATES THE OLD TOKEN, asserted against the public page;
  - every write refuses a form-encoded body, a viewer without calendar access, and
    a bad value, and a read inside a writing form opts out of the encoder.

No network: the owner's calendar is never read here, only the share row.
"""
from __future__ import annotations

import asyncio
import dataclasses
import re
import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from app import auth, config, db
from app.calendar import share_code, share_links, share_routes
from app.config import Settings, load_settings, save_settings
from app.endpoints import get_endpoint
from app.media import posters
from tests.support import AppTestCase, ORIGIN, window_fetch

PANEL = "/calendar/fragments/share"
WRITES = (
    ("/calendar/share/kind", {"kind": "username"}),
    ("/calendar/share/rotate", {}),
    ("/calendar/share/slug", {"slug": "my-shows"}),
    ("/calendar/share/view", {"mode": "custom", "card": "poster"}),
)


class SharePanelTestCase(AppTestCase):
    def make_settings(self):
        # Both calendar services on, so the Sources control has something to offer.
        return dataclasses.replace(super().make_settings(), public_base_url=ORIGIN,
                                   trakt_client_id="test-client-id",
                                   simkl_public_calendar_enabled=True)

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("linkowner", calendar_approved=True)
        self.sign_in_as(self.owner)

    def row(self):
        return asyncio.run(share_links.get_or_create(self.owner))

    def link(self) -> str:
        row = self.row()
        return share_links.generated_urls(row, "linkowner", ORIGIN)[row["preferred_kind"]] or ""

    @staticmethod
    def view_of(url: str) -> dict:
        query = dict(parse_qsl(urlsplit(url).query))
        return share_code.decode(query["p"]) if "p" in query else query

    def panel(self) -> str:
        resp = self.client.get(PANEL)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text

    def post(self, path: str, **body):
        resp = self.client.post(path, json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text


class ThePanelIsFetchedTests(SharePanelTestCase):
    def test_it_arrives_whole_and_open(self):
        page = self.panel()
        self.assertIn('<dialog id="shareModal" class="modal-overlay" closedby="any"'
                      ' data-modal-discard data-open-modal>', page)
        self.assertIn('id="share_url"', page)
        self.assertIn(f'value="{self.link()}"', page)

    def test_the_calendar_page_carries_no_share_panel_and_no_source_map(self):
        with patch("app.calendar.cache.fetch_window_records", window_fetch([])):
            page = self.client.get("/?month=1&year=2026").text
        self.assertNotIn('id="shareModal"', page)
        self.assertNotIn("SHARE_SOURCE_CHOICES", page)
        self.assertIn(f'hx-get="{PANEL}"', page)
        self.assertIn('<div id="shareSlot"></div>', page)

    def test_the_kinds_are_named_once_for_both_surfaces_that_offer_them(self):
        page = self.panel()
        for kind, label in share_links.KIND_LABELS.items():
            with self.subTest(kind=kind):
                self.assertRegex(page, rf'<option value="{kind}"[^>]*>{label}</option>')

    def test_the_base_url_warning_is_for_administrators_only(self):
        save_settings(dataclasses.replace(load_settings(), public_base_url=""))
        self.assertNotIn("Set a public base URL in Settings", self.panel())
        admin = self.make_user("boss", is_admin=True, calendar_approved=True)
        self.client.cookies.clear()
        self.sign_in_as(admin)
        self.assertIn("Set a public base URL in Settings", self.panel())

    def test_it_says_that_links_already_shared_keep_working(self):
        """It used to say switching turned the others off, which set_active_kind
        has deliberately never done."""
        page = self.panel()
        self.assertIn("Links you have already shared keep working.", page)
        self.assertNotIn("Switching turns the others off", page)


class TheKindTests(SharePanelTestCase):
    def test_choosing_a_kind_is_answered_with_that_link(self):
        answer = self.post("/calendar/share/kind", kind="username")
        self.assertEqual(self.row()["preferred_kind"], "username")
        self.assertIn('value="https://testserver/u/linkowner"', answer)
        self.assertIn('<div id="shareLink" class="field share-link">', answer)
        # Rotate belongs to the token link alone.
        self.assertNotIn('/calendar/share/rotate', answer)
        self.assertIn('/calendar/share/rotate', self.post("/calendar/share/kind", kind="token"))

    def test_an_unknown_kind_is_drawn_and_changes_nothing(self):
        answer = self.post("/calendar/share/kind", kind="carrier-pigeon")
        self.assertIn("That is not a kind of link.", answer)
        self.assertEqual(self.row()["preferred_kind"], "token")


class RotateTests(SharePanelTestCase):
    def test_rotating_invalidates_the_old_token(self):
        old = self.row()["token"]
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/s/{old}").status_code, 200)
        self.sign_in_as(self.owner)

        answer = self.post("/calendar/share/rotate")
        new = self.row()["token"]
        self.assertNotEqual(new, old)
        self.assertIn(f"/s/{new}", answer)
        self.assertNotIn(old, answer)

        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/s/{old}").status_code, 404)
        self.assertEqual(self.client.get(f"/s/{new}").status_code, 200)

    def test_rotate_asks_first(self):
        page = self.panel()
        self.assertIn('id="confirm-share-rotate"', page)
        self.assertIn("The old link will stop working immediately.", page)


class TheCustomNameTests(SharePanelTestCase):
    def test_a_free_name_is_saved_and_the_link_rides_along(self):
        answer = self.post("/calendar/share/slug", slug="my-shows")
        self.assertIn("Saved.", answer)
        self.assertEqual(self.row()["custom_slug"], "my-shows")
        self.assertIn('id="shareLink" class="field share-link" hx-swap-oob="true"', answer)

    def test_a_taken_name_is_refused_in_a_sentence_and_nothing_is_saved(self):
        other = self.make_user("someone_else", calendar_approved=True)
        asyncio.run(share_links.set_custom_slug(other, "taken-name"))
        answer = self.post("/calendar/share/slug", slug="taken-name")
        self.assertIn("That slug is taken.", answer)
        self.assertNotIn("hx-swap-oob", answer)
        self.assertIsNone(self.row()["custom_slug"])

    def test_a_name_that_is_somebody_s_username_is_refused(self):
        self.make_user("claimed", calendar_approved=True)
        self.assertIn("already someone", self.post("/calendar/share/slug", slug="claimed"))

    def test_an_empty_box_writes_nothing(self):
        asyncio.run(share_links.set_custom_slug(self.owner, "kept"))
        answer = self.post("/calendar/share/slug", slug="  ")
        self.assertEqual(answer.strip(), "")
        self.assertEqual(self.row()["custom_slug"], "kept")

    def test_the_box_checks_as_the_typing_pauses_and_drops_an_overtaken_check(self):
        box = re.search(r'<input type="text" id="share_slug"[^>]*>', self.panel()).group(0)
        self.assertIn('hx-trigger="input changed delay:500ms"', box)
        self.assertIn('hx-sync="this:replace"', box)


class TheDisplayOptionsTests(SharePanelTestCase):
    def view(self, **fields) -> str:
        return self.post("/calendar/share/view", **fields)

    def test_a_custom_view_with_services_and_a_pinned_month_is_written_into_the_link(self):
        answer = self.view(mode="custom", endpoint="shows", tz="America/New_York", card="poster",
                           packing="packed", hidenw=["0", "1"], month="8", year="2026",
                           **{"source:trakt": "1", "source:simkl": "1"})
        self.assertEqual(self.view_of(self.link()), {
            "endpoint": "shows", "tz": "America/New_York", "card": "poster", "packing": "packed",
            "hidenw": "1", "source": "trakt+simkl", "month": "8", "year": "2026"})
        self.assertIn('hx-swap-oob="true"', answer)

    def test_the_link_opens_on_the_pinned_month(self):
        self.view(mode="custom", card="poster", hidenw="0", month="8", year="2026")
        url = self.link()
        self.client.cookies.clear()
        with patch.object(posters, "ensure_posters", return_value=None):
            resp = self.client.get(url.replace(ORIGIN, ""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("August 2026", resp.text)

    def test_no_month_pins_no_year_whatever_the_year_says(self):
        self.view(mode="custom", card="poster", hidenw="0", month="", year="2031")
        self.assertNotIn("year", self.view_of(self.link()))

    def test_nothing_ticked_says_nothing_about_sources(self):
        self.view(mode="custom", endpoint="shows", hidenw="0")
        self.assertNotIn("source", self.view_of(self.link()))

    def test_a_tick_for_a_service_the_calendar_cannot_use_is_dropped(self):
        """Season Finales is published by one service, so there is nothing to
        choose — and a tick carried invisibly would leave the link saying
        something the panel had stopped showing."""
        self.view(mode="custom", endpoint="shows/finales", hidenw="0", **{"source:simkl": "1"})
        self.assertNotIn("source", self.view_of(self.link()))

    def test_the_ticks_are_redrawn_only_when_the_calendar_moves(self):
        moved = self.view(mode="custom", endpoint="shows/finales", hidenw="0")
        self.assertIn('id="shareSources"', moved)
        self.assertRegex(moved, r'id="shareSources" hx-swap-oob="true" hidden')
        stayed = self.view(mode="custom", endpoint="shows/finales", hidenw="1")
        self.assertNotIn('id="shareSources"', stayed)
        back = self.view(mode="custom", endpoint="shows", hidenw="1")
        self.assertEqual(re.findall(r'name="source:(\w+)"', back), ["trakt", "simkl"])

    def test_the_panel_ticks_what_the_link_names(self):
        self.view(mode="custom", endpoint="shows", hidenw="0", **{"source:simkl": "1"})
        block = re.search(r'id="shareSources".*?</div>\s*<span', self.panel(), re.S).group(0)
        self.assertRegex(block, r'name="source:simkl" value="1" autocomplete="off" checked')
        self.assertNotRegex(block, r'name="source:trakt"[^>]*checked')

    def test_using_my_current_display_goes_back_to_a_bare_link(self):
        self.view(mode="custom", card="poster", hidenw="0")
        self.view(mode="current", card="poster", hidenw="0")
        self.assertIsNone(share_links.link_view(self.row()))
        self.assertNotIn("?", self.link())

    def test_the_options_open_on_the_share_pages_own_defaults(self):
        page = self.panel()
        row = self.row()
        card = row["card_style"] or load_settings().card_style
        self.assertRegex(page, rf'<option value="{card}" selected>')
        self.assertRegex(page, r'<input type="radio" name="mode" value="current" autocomplete="off" checked>')

    def test_a_bad_value_is_drawn_and_nothing_is_written(self):
        for fields, sentence in (({"tz": "Mars/Olympus_Mons"}, "Unknown timezone."),
                                 ({"card": "hologram"}, "Unknown card style."),
                                 ({"month": "13", "year": "2026"}, "Month must be between 1 and 12.")):
            with self.subTest(fields=fields):
                answer = self.view(mode="custom", hidenw="0", **fields)
                self.assertIn(sentence, answer)
        self.assertIsNone(share_links.link_view(self.row()))

    def test_the_link_options_do_not_touch_the_owners_own_view(self):
        before = asyncio.run(auth.get_user_prefs(self.owner))
        self.view(mode="custom", endpoint="shows/finales", card="poster", packing="packed",
                  hidenw="1", tz="Pacific/Auckland")
        self.assertEqual(asyncio.run(auth.get_user_prefs(self.owner)), before)


class EveryWriteRefusesTests(SharePanelTestCase):

    def test_a_viewer_without_calendar_access_is_refused(self):
        self.client.cookies.clear()
        self.sign_in_as(self.make_user("waiting"))
        self.assertEqual(self.client.get(PANEL).status_code, 403)
        for path, body in WRITES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=body).status_code, 403)

    def test_every_control_that_writes_carries_the_encoder(self):
        for element in re.findall(r"<(?:form|button|input|select)\b[^>]*hx-post[^>]*>", self.panel()):
            with self.subTest(element=element[:80]):
                self.assertIn("hx-json-enc", element)

    def test_a_read_inside_a_writing_form_opts_out_of_the_encoder(self):
        for form in re.findall(r"<form\b[^>]*hx-json-enc[^>]*>(.*?)</form>", self.panel(), re.S):
            for element in re.findall(r"<[a-z]+\b[^>]*hx-get[^>]*>", form):
                with self.subTest(element=element[:80]):
                    self.assertIn('hx-json-enc="false"', element)


class TheSourceChoicesTests(SharePanelTestCase):
    def test_one_tick_per_service_that_could_fill_the_calendar(self):
        choices = share_routes.share_source_choices(get_endpoint("shows"), load_settings())
        self.assertEqual([(c["value"], c["name"]) for c in choices],
                         [("trakt", "source:trakt"), ("simkl", "source:simkl")])

    def test_a_calendar_one_service_publishes_gets_no_control(self):
        self.assertEqual(share_routes.share_source_choices(get_endpoint("shows/finales"), load_settings()), [])

    def test_a_service_switched_off_for_the_instance_leaves_nothing_to_choose(self):
        settings = dataclasses.replace(load_settings(), simkl_public_calendar_enabled=False)
        self.assertEqual(share_routes.share_source_choices(get_endpoint("shows"), settings), [])

    def test_the_ticks_are_the_services_and_never_a_list_of_names(self):
        """The rule has to keep working for a service nobody has written yet, so
        this asks the function with a registry that has one."""
        from app import providers
        from tests.calendar.test_routes import _third_source

        with patch("app.providers.registered", return_value=dict(
                providers.registered(), **{"mercury": _third_source()})):
            widened = share_routes.share_source_choices(get_endpoint("shows"), load_settings())
        self.assertEqual([choice["value"] for choice in widened], ["trakt", "simkl", "mercury"])


if __name__ == "__main__":
    unittest.main()
