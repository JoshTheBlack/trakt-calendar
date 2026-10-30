"""The tracker's settings dialog: what it draws and what each control saves.

THE DIALOG REPLACES TWO PLACES. The default emoji was a popover in the tracker's
header; which linked tracker decides a season and whose stored numbers count were
on the account page, where nobody looking at the tracker was looking. They are one
dialog now, opened from the header, and the season-totals order — which catalogue a
tracker row's episode total is asked of — is the fourth control in it.

Tested over HTTP because that is the only thing that proves a control is WIRED:
twice in this build a correct function shipped attached to nothing, each with its
own unit tests passing.

What the browser does with the dialog (opening it, Escape and the backdrop closing
it, each control saving on its own) is measured in the browser, not here.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.distrakt import live, tracker_settings
from app.sources import prefs as source_prefs

from tests.distrakt.test_month_fragment import MonthFragmentTestCase
from tests.support import ORIGIN


class _SettingsCase(MonthFragmentTestCase):

    def make_settings(self):
        # All three catalogues askable, so the season-totals list has an order to
        # state; a list with one service in it is not drawn (tested below).
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid", simkl_client_id="sid",
                        tmdb_api_key="eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJtZSJ9.c2ln",
                        simkl_public_calendar_enabled=False)

    def setUp(self):
        super().setUp()
        # Both trackers linked, so every list has an order to state.
        self.link_identity(self.user_id, "simkl", 901, "tok2")

    def stored(self):
        return asyncio.run(source_prefs.load(self.user_id))

    def move(self, kind, source, direction):
        return self.client.post(f"/api/distrakt/settings/order/{kind}",
                                json={"source": source, "direction": direction})

    def retire(self, **counted):
        """As the form posts: every service mentioned, a ticked box as the list
        its hidden companion and the box make."""
        body = {f"counted:{name}": ["0", "1"] if on else "0"
                for name, on in {"trakt": True, "simkl": True, **counted}.items()}
        return self.client.post("/api/distrakt/settings/retired", json=body)


class TheDialogIsOnTheTrackersPageTests(_SettingsCase):

    def page(self) -> str:
        year, month = self.current
        resp = self.client.get(f"/distrakt?year={year}&month={month}")
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_the_header_opens_a_modal_dialog_natively(self):
        page = self.page()
        self.assertRegex(page, r'<button type="button" class="pill-btn" command="show-modal" '
                               r'commandfor="trackerSettings"')
        self.assertRegex(page, r'<dialog id="trackerSettings" class="modal-overlay" closedby="any"')
        self.assertRegex(page, r'command="close" commandfor="trackerSettings"')

    def test_the_header_no_longer_has_the_default_emoji_popover(self):
        page = self.page()
        self.assertNotIn("emojiDefaultMenu", page)
        self.assertNotIn("emojiDefaultPill", page)

    def test_every_control_in_it_posts_itself_as_json(self):
        page = self.page()
        dialog = page[page.index('<dialog id="trackerSettings"'):]
        dialog = dialog[:dialog.index("</dialog>")]
        self.assertIn('hx-post="/api/distrakt/emoji-default', dialog)
        self.assertIn('hx-post="/api/distrakt/settings/order/tracker_order"', dialog)
        self.assertIn('hx-post="/api/distrakt/settings/order/season_totals"', dialog)
        self.assertIn('hx-post="/api/distrakt/settings/retired"', dialog)
        self.assertEqual(dialog.count("hx-post="), dialog.count("hx-json-enc"))
        self.assertNotRegex(dialog, r"\son[a-z]+=")

    def test_a_switch_is_offered_per_tracker_even_for_one_that_is_not_linked(self):
        asyncio.run(__import__("app").db.execute(
            "DELETE FROM linked_identities WHERE user_id = ? AND provider = 'simkl'",
            (self.user_id,)))
        page = self.page()
        self.assertIn('name="counted:trakt"', page)
        self.assertIn('name="counted:simkl"', page)
        self.assertIn("(not linked)", page)

    def test_the_account_page_no_longer_carries_the_trackers_controls(self):
        body = self.client.get("/me").text
        for gone in ('id="trackerRetired"', 'name="counted:trakt"', "tracker_order-",
                     "/me/tracker-retired"):
            self.assertNotIn(gone, body)


class RetiringATrackersHistoryTests(_SettingsCase):
    """"I have moved off that service."

    THE EXIT FROM A STATE THAT HAD NONE. Unlinking stops a service being ASKED and
    always did; it could not stop the numbers it had already contributed from
    counting, so every row it ever touched stayed amber for ever and a migrated
    account read as permanently degraded. This is how somebody says so."""

    def test_a_service_can_be_retired_and_reads_back(self):
        resp = self.retire(trakt=False)
        self.assertEqual(resp.status_code, 200)
        self.assertNotRegex(resp.text, r'id="counted-trakt"[^>]*checked')
        self.assertRegex(resp.text, r'id="counted-simkl"[^>]*checked')
        self.assertFalse(self.stored().counts_tracker("trakt"))

    def test_a_save_asks_the_month_to_fetch_itself_again(self):
        resp = self.retire(trakt=False)
        self.assertIn("month-changed", resp.headers.get("HX-Trigger", ""))

    def test_it_can_be_undone(self):
        """The whole difference between this and deleting the numbers: switching
        one back on has to put it straight back, which is only possible because
        nothing was thrown away."""
        self.retire(trakt=False)
        self.retire()
        self.assertTrue(self.stored().counts_tracker("trakt"))

    def test_the_whole_set_is_what_is_stored(self):
        self.retire(trakt=False, simkl=False)
        self.retire(simkl=False)
        stored = self.stored()
        self.assertTrue(stored.counts_tracker("trakt"))
        self.assertFalse(stored.counts_tracker("simkl"))

    def test_an_unknown_service_is_refused_rather_than_dropped(self):
        resp = self.retire(letterboxd=False)
        self.assertIn("Unknown service: letterboxd.", resp.text)
        self.assertNotIn("month-changed", resp.headers.get("HX-Trigger", ""))
        self.assertEqual(self.stored().tracker_retired, [])

    def test_a_service_the_body_does_not_mention_is_not_read_as_retired(self):
        resp = self.client.post("/api/distrakt/settings/retired", json={"counted:trakt": "0"})
        self.assertIn("Every service has to be stated", resp.text)
        self.assertEqual(self.stored().tracker_retired, [])

    def test_it_needs_a_session(self):
        self.client.cookies.clear()
        self.assertIn(self.retire(trakt=False).status_code, (401, 403))


class StatingWhichTrackerDecidesTests(_SettingsCase):

    def test_a_move_is_stored_and_answered_with_the_list(self):
        resp = self.move("tracker_order", "simkl", "up")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.stored().tracker_priority, ["simkl", "trakt"])
        self.assertEqual(re.findall(r'data-source="(\w+)"', resp.text), ["simkl", "trakt"])
        self.assertIn("month-changed", resp.headers.get("HX-Trigger", ""))

    def test_the_first_row_has_no_up_arrow_and_the_last_no_down(self):
        resp = self.move("tracker_order", "simkl", "up")
        self.assertNotIn('id="tracker_order-simkl-up"', resp.text)
        self.assertNotIn('id="tracker_order-trakt-down"', resp.text)
        self.assertIn('id="tracker_order-simkl-down"', resp.text)

    def test_the_moved_row_keeps_focus_on_the_arrow_it_still_has(self):
        resp = self.move("tracker_order", "simkl", "up")
        self.assertRegex(resp.text, r'id="tracker_order-simkl-down"[^>]*autofocus')

    def test_it_does_not_disturb_a_retired_service_or_the_totals_order(self):
        self.retire(trakt=False)
        self.move("season_totals", "simkl", "up")
        self.move("tracker_order", "simkl", "up")
        stored = self.stored()
        self.assertEqual(stored.tracker_priority, ["simkl", "trakt"])
        self.assertFalse(stored.counts_tracker("trakt"))
        self.assertEqual(stored.season_totals_order, ["tmdb", "simkl", "trakt"])

    def test_a_move_that_cannot_happen_is_drawn_stores_nothing_and_leaves_the_month(self):
        for source, direction in (("trakt", "up"), ("letterboxd", "down"), ("simkl", "sideways")):
            with self.subTest(source=source, direction=direction):
                resp = self.move("tracker_order", source, direction)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("cannot move that way", resp.text)
                self.assertNotIn("month-changed", resp.headers.get("HX-Trigger", ""))
        self.assertEqual(self.stored().tracker_priority, [])

    def test_a_list_this_version_does_not_have_is_said_not_obeyed(self):
        resp = self.move("metadata_order", "simkl", "up")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("There is no such list.", resp.text)
        self.assertEqual(self.stored().metadata_order, [])

    def test_tmdb_is_never_a_tracker(self):
        """It keeps no watch history: it is a catalogue, so it is offered in the
        totals order and never in the list of trackers that decide."""
        names = {entry["source"] for entry in tracker_settings.build(
            source_prefs.SourcePrefs(user_id=1), {"trakt", "simkl", "tmdb"},
            SimpleNamespace(tmdb_configured=True, trakt_catalogue_configured=True,
                            simkl_catalogue_configured=True)).orders["tracker_order"]}
        self.assertEqual(names, {"trakt", "simkl"})


class StatingWhereEpisodeTotalsComeFromTests(_SettingsCase):

    def test_the_default_is_the_declared_order_and_a_press_stores_it_whole(self):
        resp = self.move("season_totals", "trakt", "up")
        self.assertEqual(resp.status_code, 200)
        stored = self.stored().season_totals_order
        self.assertEqual(stored[0], "trakt")
        self.assertEqual(sorted(stored), sorted(re.findall(r'data-source="(\w+)"', resp.text)))
        self.assertIn("month-changed", resp.headers.get("HX-Trigger", ""))

    def test_it_is_the_tracker_rows_own_and_not_the_panels(self):
        """A viewer who wants Trakt's season lengths in their tracker still reads
        the declared order's description in the panel: the order changes who is
        ASKED for a total and nothing else."""
        self.move("season_totals", "trakt", "up")
        stored = self.stored()
        self.assertEqual(stored.metadata_order, [])
        self.assertEqual(stored.source_order(["tmdb", "trakt", "simkl"]),
                         ["tmdb", "trakt", "simkl"])

    def test_a_list_with_one_catalogue_in_it_is_not_drawn(self):
        only_one = SimpleNamespace(tmdb_configured=False, trakt_catalogue_configured=True,
                                   simkl_catalogue_configured=False)
        panel = tracker_settings.build(source_prefs.SourcePrefs(user_id=1), set(), only_one)
        self.assertEqual(panel.orders["season_totals"], [])

    def test_a_catalogue_this_instance_cannot_ask_is_not_offered(self):
        no_simkl = SimpleNamespace(tmdb_configured=True, trakt_catalogue_configured=True,
                                   simkl_catalogue_configured=False)
        panel = tracker_settings.build(source_prefs.SourcePrefs(user_id=1), set(), no_simkl)
        self.assertEqual([entry["source"] for entry in panel.orders["season_totals"]],
                         ["tmdb", "trakt"])

    def test_every_registered_describing_source_is_offered_when_it_can_be_asked(self):
        """No per-service table to forget: the list is the registry's."""
        from app import providers
        everything = SimpleNamespace(tmdb_configured=True, trakt_catalogue_configured=True,
                                     simkl_catalogue_configured=True)
        panel = tracker_settings.build(source_prefs.SourcePrefs(user_id=1), set(), everything)
        self.assertEqual([entry["source"] for entry in panel.orders["season_totals"]],
                         [str(source) for source, provider in providers.registered().items()
                          if provider.detail_port is not None])


class ChangingTheOrderChangesWhichServiceAnswersARowsTotalTests(_SettingsCase):
    """End to end through the live pass: the same row, the same ids, two viewers'
    orders, two different services asked — and a frozen month's record unchanged."""

    ALL = SimpleNamespace(trakt_configured=True, simkl_configured=True, tmdb_configured=True,
                          trakt_catalogue_configured=True, simkl_catalogue_configured=True)
    TOTALS = {"tmdb": 10, "trakt": 8, "simkl": 9}

    def _live(self, rec):
        async def season_facts(settings, answerer, media, season, **_kwargs):
            return {"season": season, "total": self.TOTALS[str(answerer.source)],
                    "cadence": "Mon", "premiere": None, "finale": None,
                    "started_airing": True, "finished_airing": False, "air_dates": []}

        async def run():
            with patch("app.distrakt.live.store_panel.season_facts",
                       AsyncMock(side_effect=season_facts)):
                return await live.compute_live_shows(
                    self.user_id, [rec], self.ALL, watched_lookup={},
                    sources_read=("trakt",), allow_degrade=False)

        return asyncio.run(run())[0]

    def rec(self):
        return {"media": "show", "match_source": "tmdb", "match_id": "700", "season": 1,
                "title": "Ordered Show", "network": "HBO",
                "ids": {"tmdb": 700, "trakt": 700, "simkl": 700}, "total": 3, "watched": 0}

    def test_the_row_is_measured_against_the_first_catalogue_the_viewer_names(self):
        default = self._live(self.rec())
        self.assertEqual((default["total"], default["total_by_source"]), (10, {"tmdb": 10}))
        self.move("season_totals", "trakt", "up")
        self.move("season_totals", "trakt", "up")
        after = self._live(self.rec())
        self.assertEqual((after["total"], after["total_by_source"]), (8, {"trakt": 8}))

    def test_a_viewer_who_stated_nothing_is_asked_in_the_declared_order(self):
        neighbour = self.make_user("neighbour", distrakt_approved=True, calendar_approved=True)
        self.move("season_totals", "simkl", "up")
        self.move("season_totals", "simkl", "up")
        self.assertEqual(asyncio.run(live.totals_order(neighbour)), ("tmdb", "trakt", "simkl"))
        self.assertEqual(asyncio.run(live.totals_order(self.user_id))[0], "simkl")
