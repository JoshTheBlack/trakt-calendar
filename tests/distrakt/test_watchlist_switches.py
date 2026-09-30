"""The list switches in the tracker's header: what they offer, what pressing one
does, and what the panel says about the writing behind it
(templates/_distrakt_watchlists.html, routes.api_distrakt_watchlist).

The pass itself is not run here — `nudge` is recorded — because what is asserted
is the page, and the pass has tests/distrakt/test_watchlist_writes.py.
"""
from __future__ import annotations

import asyncio
import re
import time
from html import unescape
from unittest.mock import patch

from app import db
from app.distrakt import watchlist_writes as ww
from app.providers.base import Source
from tests.distrakt.test_gating import DistraktTestCase

SIMKL = str(Source.SIMKL)
SIMKL_ACCOUNT = 8814058


def text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


class SwitchTestCase(DistraktTestCase):

    def make_settings(self):
        from app.config import Settings
        return Settings(simkl_public_calendar_enabled=False, simkl_client_id="simkl-client")

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user("lister")
        self.link_identity(self.user_id, "simkl", SIMKL_ACCOUNT, access_token="simkl-token")
        self.sign_in_as(self.user_id)
        self.nudged: list[int] = []
        started = patch.object(ww, "nudge", self.nudged.append)
        started.start()
        self.addCleanup(started.stop)
        self.addCleanup(ww._trouble.clear)

    def page(self) -> str:
        return self.client.get("/distrakt?year=2026&month=9").text

    def press(self, on: bool, source: str = SIMKL):
        # What the form sends: the hidden companion, then the box when ticked —
        # the pair htmx's json encoding turns into a list.
        return self.client.post("/api/distrakt/watchlist",
                                json={"source": source, "on": ["", "on"] if on else ""})

    def add_listed(self, tmdb: int):
        from app import distrakt
        asyncio.run(distrakt.add_user_record(self.user_id, {
            "ids": {"tmdb": tmdb}, "season": 1, "kind": "keepup", "title": f"Show {tmdb}"}))


class TheSwitchTests(SwitchTestCase):

    def test_the_header_offers_a_linked_service_switched_off(self):
        html = self.page()
        self.assertIn('id="watchlistPill"', html)
        self.assertIn('popovertarget="watchlistMenu"', html)
        panel = text(html.split('id="watchlistPanel"', 1)[1].split("</form>", 1)[0])
        self.assertIn("Add these shows to Simkl", panel)
        self.assertIn("Off.", panel)

    def test_it_links_out_to_the_list_it_writes(self):
        self.assertIn(f'href="https://simkl.com/{SIMKL_ACCOUNT}/tv/plantowatch/"', self.page())

    def test_a_viewer_with_no_list_to_write_is_told_so(self):
        asyncio.run(db.execute("DELETE FROM linked_identities WHERE user_id = ? AND provider = ?",
                               (self.user_id, "simkl")))
        self.assertIn("No linked service has a list to add to.", text(self.page()))

    def test_switching_on_says_the_writing_has_begun_and_asks_for_a_pass(self):
        self.add_listed(1)
        self.add_listed(2)
        answer = self.press(True)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(asyncio.run(ww.switched_on(self.user_id)), [SIMKL])
        self.assertIn("Adding 2…", text(answer.text))
        # Both halves out of band, and the panel asking for itself until done.
        # The pill's LABEL, never the button the open panel is anchored to.
        self.assertRegex(answer.text, r'id="watchlistPillLabel" hx-swap-oob="outerHTML"')
        self.assertNotIn('id="watchlistPill"', answer.text)
        self.assertIn('id="watchlistPanel" class="watchlist-panel-body" hx-swap-oob="outerHTML"',
                      answer.text)
        self.assertIn('hx-get="/distrakt/fragments/watchlists" hx-trigger="load delay:2s"',
                      answer.text)
        self.assertEqual(self.nudged, [self.user_id])

    def test_once_written_it_says_how_many_and_stops_asking(self):
        self.add_listed(1)
        self.press(True)
        asyncio.run(ww._record(self.user_id, SIMKL, {
            "show:tmdb:1": ww.WriteRow("1", "none", ww.WriteState.WRITTEN)}))
        answer = self.client.get("/distrakt/fragments/watchlists")
        self.assertIn("On · 1 show added.", text(answer.text))
        self.assertNotIn("hx-trigger=\"load delay", answer.text)

    def test_switching_off_stops_and_says_nothing_changes_there(self):
        self.press(True)
        answer = self.press(False)
        self.assertEqual(asyncio.run(ww.switched_on(self.user_id)), [])
        self.assertIn("Off.", text(answer.text))

    def test_a_failure_is_drawn_with_its_count_and_wording(self):
        self.add_listed(1)
        self.press(True)
        asyncio.run(ww._record(self.user_id, SIMKL, {
            "show:tmdb:1": ww.WriteRow(None, "none", ww.WriteState.FAILED)}))
        ww._trouble[(self.user_id, SIMKL)] = ww.Trouble("Simkl could not be reached.",
                                                        time.monotonic())
        html = self.page()
        self.assertIn("1 failed. Simkl could not be reached.", text(html))
        self.assertIn("⚠ 1", text(html.split('id="watchlistPill"', 1)[1].split("</button>", 1)[0]))
        # A failure is waited out, not watched: the panel does not poll.
        self.assertNotIn('hx-get="/distrakt/fragments/watchlists"', html)

    def test_a_service_nobody_offers_is_refused(self):
        self.press(True, source="plex")
        self.assertEqual(asyncio.run(ww.switched_on(self.user_id)), [])

    def test_a_refused_show_is_said_beside_its_switch(self):
        """A list at its limit refuses and is never retried, so without a line of
        its own the panel would read as if every show had been added."""
        self.add_listed(1)
        self.add_listed(2)
        self.press(True)
        asyncio.run(ww._record(self.user_id, SIMKL, {
            "show:tmdb:1": ww.WriteRow("1", "none", ww.WriteState.WRITTEN),
            "show:tmdb:2": ww.WriteRow(None, "none", ww.WriteState.REFUSED)}))
        panel = text(self.client.get("/distrakt/fragments/watchlists").text)
        self.assertIn("On · 1 show added.", panel)
        self.assertIn("1 refused: the list on Simkl is full.", panel)

    def test_a_switch_whose_link_went_can_be_turned_off_but_not_on(self):
        self.press(True)
        asyncio.run(db.execute("DELETE FROM linked_identities WHERE user_id = ? AND provider = ?",
                               (self.user_id, "simkl")))
        self.assertIn("Simkl isn't linked.", text(self.page()))
        self.press(True)
        self.assertEqual(asyncio.run(ww.switched_on(self.user_id)), [SIMKL])
        self.press(False)
        self.assertEqual(asyncio.run(ww.switched_on(self.user_id)), [])
        self.press(True)
        self.assertEqual(asyncio.run(ww.switched_on(self.user_id)), [])


class BothServicesTests(SwitchTestCase):
    """A viewer linked to Simkl and Trakt gets a switch for each, each linking
    to its own list, with no change to the page for the second."""

    def make_settings(self):
        from app.config import Settings
        return Settings(simkl_public_calendar_enabled=False, simkl_client_id="simkl-client",
                        trakt_client_id="trakt-client")

    def setUp(self):
        super().setUp()
        self.link_identity(self.user_id, "trakt", "30ee86b0-0000-4000-8000-000000000001",
                           access_token="trakt-token")

    def test_each_linked_service_has_its_switch_and_its_link(self):
        html = self.page()
        panel = text(html.split('id="watchlistPanel"', 1)[1].split("</div>", 1)[0])
        self.assertIn("Add these shows to Simkl", panel)
        self.assertIn("Add these shows to Trakt", panel)
        self.assertIn(f'href="https://simkl.com/{SIMKL_ACCOUNT}/tv/plantowatch/"', html)
        self.assertIn('href="https://app.trakt.tv/users/me/watchlist"', html)

    def test_trakt_switches_on_by_itself(self):
        self.add_listed(1)
        answer = self.press(True, source=str(Source.TRAKT))
        self.assertEqual(asyncio.run(ww.switched_on(self.user_id)), [str(Source.TRAKT)])
        self.assertIn("Adding 1…", text(answer.text))
