"""Retiring a service's stored numbers — the exit from a state that had none.

Unlinking a service stops it being ASKED, and that already worked. What it could
not do is stop the numbers it had already contributed from counting: they live in
the watch state, per source, and every row that ever had one went on rendering it.
The row said so honestly — `counts_freshness` reads "partial", meaning "a number
here belongs to a service nobody asked and no refresh will move it" — but there
was no way out of that state, so an account that had genuinely migrated read as
permanently degraded rather than as a healthy single-service account.

THE THREE THINGS THAT MAKE IT AN EXIT RATHER THAN A HIDE, each pinned here:
  - the number stops COUNTING,
  - the number is still SHOWN, marked, because deleting it would be unexplainable
    and un-retiring has to be able to put it straight back,
  - the row stops reading as degraded, which is the entire complaint.

No network: these exercise the pure merge and label functions directly.
"""
from __future__ import annotations

import unittest

from app.config import Settings
from app.distrakt import counts, live
from app.sources.prefs import SourcePrefs

# Both catalogues configured, so `detail_source` can name one for the total.
SETTINGS = Settings(trakt_client_id="id", simkl_client_id="id")
LABELS = {"trakt": "Trakt", "simkl": "Simkl"}
DETAIL = {"total": 12, "cadence": "Tue", "premiere": "7/1", "finale": "7/29",
          "started_airing": True, "finished_airing": False}
REC = {"media": "show", "match_source": "tmdb", "match_id": "900", "season": 1,
       "title": "A Show", "ids": {"tmdb": 900, "trakt": 111, "simkl": 222},
       "kind": "keepup", "watched": 4, "total": 12}


class ThePreferenceTests(unittest.TestCase):
    def test_an_account_that_has_said_nothing_counts_everything(self):
        """The honest default, and what every account had before this existed."""
        prefs = SourcePrefs(user_id=1)
        self.assertTrue(prefs.counts_tracker("trakt"))
        self.assertEqual(prefs.retired_trackers(), frozenset())

    def test_a_retired_service_stops_counting(self):
        prefs = SourcePrefs(user_id=1, tracker_retired=["trakt"])
        self.assertFalse(prefs.counts_tracker("trakt"))
        self.assertTrue(prefs.counts_tracker("simkl"))

    def test_an_unreadable_value_counts_everything_rather_than_nothing(self):
        """Same degrade rule as every other preference here: a row written by a
        newer version must not stop an older one rendering a page — and failing
        OPEN is the safe direction, because failing closed would silently stop
        counting services the viewer never retired."""
        prefs = SourcePrefs(user_id=1, tracker_retired="not-a-list")
        self.assertTrue(prefs.counts_tracker("trakt"))
        self.assertEqual(prefs.retired_trackers(), frozenset())

    def test_it_can_be_narrowed_to_the_services_being_asked_about(self):
        prefs = SourcePrefs(user_id=1, tracker_retired=["trakt", "simkl"])
        self.assertEqual(prefs.retired_trackers(["simkl"]), {"simkl"})


class ARetiredNumberStopsCountingTests(unittest.TestCase):
    def _show(self, retired=frozenset(), asked=("trakt", "simkl")):
        return live._merge_available(
            REC, dict(DETAIL), {"trakt": 4, "simkl": 9}, SETTINGS, asked,
            retired=retired)

    def test_the_primary_count_ignores_a_retired_service(self):
        """Trakt leads the order and holds 4; retiring it means Simkl's 9 is the
        number the row and the bucket rule see."""
        self.assertEqual(self._show()["watched"], 4)
        self.assertEqual(self._show(retired={"trakt"})["watched"], 9)

    def test_the_number_is_still_stored_and_still_shown(self):
        """Retiring stops it counting; it does not throw it away. A number that
        vanished with no explanation is the same invisibility the freshness
        states were built to remove, and un-retiring has to be able to put it
        straight back."""
        show = self._show(retired={"trakt"})
        self.assertEqual(show["watched_by_source"], {"trakt": 4, "simkl": 9})
        self.assertEqual(show["retired_sources"], ["trakt"])

    def test_the_row_stops_reading_as_degraded(self):
        """THE WHOLE COMPLAINT. A service nobody asked makes a row "partial" —
        true, and permanent, so a migrated account was amber for ever. Once the
        account has said it no longer counts that service, its number is not an
        unanswered question but a decision, and the row goes green."""
        asked = ("simkl",)  # trakt unlinked: asked no longer names it
        self.assertEqual(self._show(asked=asked)["counts_freshness"], "partial")
        self.assertEqual(
            self._show(retired={"trakt"}, asked=asked)["counts_freshness"], "current")

    def test_the_note_stops_calling_it_the_last_one_read(self):
        """The sentence exists to say whether the numbers ON the row are current,
        and a retired service's number is not on the row any more."""
        note = self._show(retired={"trakt"}, asked=("simkl",))["counts_note"]
        self.assertNotIn("Trakt", note)
        self.assertIn("up to date", note)

    def test_retiring_every_service_leaves_nothing_counted(self):
        """Deliberately NOT exempted the way `tracker_order` exempts a sole
        answer. That one can only decide which of several answers leads; this is a
        statement that a service's numbers are not to be used — and a title only
        the retired service knew is the row carrying the stalest number of all, so
        exempting it would leave the migration half-done. It reads as nothing
        counted, which the tooltip explains and a switch undoes."""
        self.assertEqual(self._show(retired={"trakt", "simkl"})["watched"], 0)


class TheTooltipNamesTheDecisionTests(unittest.TestCase):
    def _detail(self, retired=()):
        return counts.counts_detail(
            {"trakt": 4, "simkl": 9}, 12, LABELS, ("trakt", "simkl"),
            asked=("simkl",), linked=("simkl",), retired=retired)

    def test_a_retired_service_says_so_beside_its_number(self):
        """The number is still there, and the line says why it is not counted."""
        self.assertIn("Trakt: 4 of 12 — retired, not counted", self._detail(("trakt",)))

    def test_retired_replaces_not_asked_rather_than_joining_it(self):
        """Both are true of a retired service — it is usually an unlinked one —
        but only one is the reason. A line reading "not asked, retired, not
        counted" reports the mechanism and the decision as separate findings when
        the decision is the whole answer and the only actionable half."""
        line = [l for l in self._detail(("trakt",)).splitlines()
                if l.startswith("Trakt")][0]
        self.assertNotIn("not asked", line)

    def test_without_the_decision_it_still_reads_as_not_asked(self):
        """The state this replaces has not gone away — it is what a row says
        before anybody retires anything, and it must still be reachable."""
        self.assertIn("Trakt: 4 of 12 — not asked", self._detail())


class AFrozenMonthIsNeverReAnsweredTests(unittest.TestCase):
    """THE BOUNDARY THIS MUST NOT CROSS. A settled month's breakdown is not a
    cache and not a live claim: it is what that month RECORDED, and it will never
    be recomputed. A preference stated today must not silently re-answer it —
    exactly the rule the tracker-order preference already follows.
    """

    def test_a_frozen_months_tooltip_is_rendered_without_the_preference(self):
        """A frozen month re-renders from its stored breakdown through the same
        function with no `retired` argument, so the numbers it recorded read back
        exactly as recorded. Nothing about the account's current preference can
        reach them."""
        frozen = counts.counts_detail({"trakt": 4, "simkl": 9}, 12, LABELS,
                                      ("trakt", "simkl"))
        self.assertIn("Trakt: 4 of 12", frozen)
        self.assertNotIn("retired", frozen)
