"""The per-account source preference store (app/sources/prefs).

Two things are worth pinning here. `auto` and `both` are DIFFERENT answers and
the difference only shows when a link lapses, which is exactly the case nobody
would notice going wrong. And an account with no stored row reads as the
defaults rather than as an error, because that is what every account looks like
until somebody opens the screen.

No network.
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from app import db
from app.providers.base import Source
from app.sources import prefs
from tests.support import migrated_db


class SelectionTests(unittest.TestCase):
    """Pure, so they need no database.

    THERE IS ONLY THE CALENDAR'S SELECTION NOW. A second one asked which
    services the TRACKER read, alongside what the account had linked; reading
    somebody's history needs their token, so the links already said it, and the
    two could only agree or contradict each other. What is linked decides who is
    asked (providers.for_tracker_ports), and nothing here has an opinion.
    """

    def test_the_calendar_admits_every_source_under_auto_whoever_is_linked(self):
        """The two halves read `auto` differently, and this is the difference.
        A calendar is fetched with the instance's own credentials or with none,
        so a viewer's links buy nothing and withhold nothing — an account whose
        only link is to one service still reads the other's calendar, and so
        does one that has linked nothing at all. `admits_calendar` takes no
        linked set, which is what makes those three the same call."""
        auto = prefs.SourcePrefs(user_id=1, calendar_source=prefs.AUTO)
        self.assertTrue(auto.admits_calendar(Source.TRAKT))
        self.assertTrue(auto.admits_calendar(Source.SIMKL))

    def test_a_stated_calendar_selection_still_means_what_it_says(self):
        """Widening the default must not widen a decision."""
        named = prefs.SourcePrefs(user_id=1, calendar_source="simkl")
        self.assertTrue(named.admits_calendar(Source.SIMKL))
        self.assertFalse(named.admits_calendar(Source.TRAKT))
        both = prefs.SourcePrefs(user_id=1, calendar_source=prefs.BOTH)
        self.assertTrue(both.admits_calendar(Source.TRAKT))
        self.assertTrue(both.admits_calendar(Source.SIMKL))

    def test_the_calendar_preference_says_nothing_about_the_tracker(self):
        """THE TRACKER HAS NO SELECTION OF ITS OWN ANY MORE, and this is here so
        that widening the calendar cannot quietly widen it. It had one, meaning
        "every service this account has LINKED" — which is what the links
        already say, so the two could only agree or contradict each other.
        Reading one person's viewing history means asking with THEIR token, so
        the links are the whole of it (providers.for_tracker_ports)."""
        self.assertFalse(hasattr(prefs.SourcePrefs(user_id=1), "tracker_source"))
        self.assertFalse(hasattr(prefs.SourcePrefs(user_id=1), "admits_tracker"))

    def test_every_known_service_is_a_valid_selection(self):
        """Spelled from Source rather than restated, so a service the app has
        cannot be one this refuses."""
        for source in Source:
            self.assertIn(str(source), prefs.SELECTIONS)


class NamingTheServicesTests(unittest.TestCase):
    """The selection vocabulary, and what it does when a THIRD service exists.

    `both` says "two" and means "do not narrow", which are the same thing only
    while there are exactly two services. The replacement is two shapes rather
    than a third word: `auto`, which grows because nobody stated it, and a set of
    services named explicitly, which does not because somebody did.
    """

    def test_a_set_of_services_is_spelled_by_naming_them(self):
        self.assertEqual(prefs.named_sources("trakt+simkl"), {"trakt", "simkl"})
        self.assertEqual(prefs.named_sources("simkl"), {"simkl"})

    def test_a_single_name_is_a_one_element_set(self):
        """Which is why the values this column already held needed no rewriting:
        they were always in the new spelling."""
        self.assertTrue(prefs.is_selection("trakt"))
        self.assertEqual(prefs.canonical_selection("trakt"), "trakt")

    def test_the_order_a_set_is_written_in_does_not_make_a_second_value(self):
        self.assertEqual(prefs.canonical_selection("simkl+trakt"), "trakt+simkl")
        self.assertEqual(prefs.named_sources("simkl+trakt"),
                         prefs.named_sources("trakt+simkl"))

    def test_auto_is_the_one_selection_that_grows(self):
        """A service registered tomorrow answers for an account that stated
        nothing, and does not answer for one that named the services it wanted.
        A name this app has never heard of stands in for that service here."""
        auto = prefs.SourcePrefs(user_id=1, calendar_source=prefs.AUTO)
        named = prefs.SourcePrefs(user_id=1, calendar_source="trakt+simkl")
        self.assertTrue(auto.admits_calendar("letterboxd"))
        self.assertFalse(named.admits_calendar("letterboxd"))

    def test_a_stored_both_means_the_two_services_it_could_have_meant(self):
        """THE MIGRATION OF MEANING, and it is a decision rather than an
        accident: rows in the field carry `both`, and it was chosen from a menu
        of two. Reading it as "all" would hand somebody a third service they were
        never offered, so it reads as exactly the pair — frozen, and not derived
        from whatever Source happens to hold today."""
        self.assertEqual(prefs.named_sources(prefs.BOTH), {"trakt", "simkl"})
        stored = prefs.SourcePrefs(user_id=1, calendar_source=prefs.BOTH)
        self.assertTrue(stored.admits_calendar(Source.TRAKT))
        self.assertTrue(stored.admits_calendar(Source.SIMKL))
        self.assertFalse(stored.admits_calendar("letterboxd"))

    def test_a_stored_both_and_the_named_pair_are_the_same_answer(self):
        """Which is what makes `both` a legacy SPELLING rather than a legacy
        BEHAVIOUR, and what lets nothing else in the app know about it."""
        self.assertEqual(prefs.named_sources(prefs.BOTH),
                         prefs.named_sources("trakt+simkl"))

    def test_an_unreadable_selection_reads_as_the_widest_answer(self):
        for value in ("", "letterboxd", "trakt+letterboxd", "+", None, "auto+trakt"):
            with self.subTest(value=value):
                self.assertIsNone(prefs.named_sources(value))
                self.assertFalse(prefs.is_selection(value))

    def test_a_named_set_is_read_the_same_way_wherever_it_is_asked(self):
        """One vocabulary, whichever value is holding it."""
        named = prefs.SourcePrefs(user_id=1, calendar_source="trakt+simkl")
        self.assertTrue(named.admits_calendar(Source.SIMKL))
        one = prefs.SourcePrefs(user_id=1, calendar_source="trakt")
        self.assertFalse(one.admits_calendar(Source.SIMKL))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        migrated_db("source-prefs")
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('viewer', ?, ?)",
            (now, now))
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_an_account_with_no_row_reads_as_the_defaults(self):
        stored = await prefs.load(self.user_id)
        self.assertEqual(stored.calendar_source, prefs.AUTO)
        self.assertEqual(stored.metadata_order, [])
        # ...and nothing was created by asking.
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)

    async def test_a_saved_preference_reads_back(self):
        saved = await prefs.save(replace(
            await prefs.load(self.user_id),
            calendar_source=prefs.BOTH, metadata_order=["simkl", "trakt"]))
        self.assertEqual(saved.calendar_source, prefs.BOTH)
        again = await prefs.load(self.user_id)
        self.assertEqual(again.calendar_source, prefs.BOTH)
        self.assertEqual(again.metadata_order, ["simkl", "trakt"])

    async def test_saving_twice_updates_the_one_row(self):
        first = await prefs.load(self.user_id)
        await prefs.save(replace(first, calendar_source="trakt"))
        await prefs.save(replace(first, calendar_source="simkl"))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 1)
        self.assertEqual((await prefs.load(self.user_id)).calendar_source, "simkl")

    async def test_one_account_s_preference_is_not_another_s(self):
        now = db.now()
        other = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('other', ?, ?)",
            (now, now))).lastrowid
        await prefs.save(replace(await prefs.load(self.user_id), calendar_source="trakt"))
        self.assertEqual((await prefs.load(other)).calendar_source, prefs.AUTO)

    async def test_an_unknown_selection_is_refused_rather_than_coerced(self):
        """A preference nobody can satisfy is a bug in the caller, and rewriting
        it to the default on the way in would hide it."""
        with self.assertRaises(ValueError):
            await prefs.save(replace(await prefs.load(self.user_id),
                                     calendar_source="letterboxd"))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)

    async def test_a_stored_value_this_version_does_not_know_reads_as_the_default(self):
        """The other direction, and deliberately not symmetrical: a row written by
        a newer version of the app must not stop an older one rendering a page."""
        await db.execute(
            "INSERT INTO source_prefs (user_id, calendar_source, metadata_order_json) "
            "VALUES (?, 'letterboxd', 'not json')",
            (self.user_id,))
        stored = await prefs.load(self.user_id)
        self.assertEqual(stored.calendar_source, prefs.AUTO)
        self.assertEqual(stored.metadata_order, [])

    async def test_a_named_set_round_trips_in_declared_order(self):
        await prefs.save(replace(
            await prefs.load(self.user_id), calendar_source="simkl+trakt"))
        # Stored in declared order, so one choice is one stored value.
        self.assertEqual((await prefs.load(self.user_id)).calendar_source,
                         "trakt+simkl")

    async def test_a_metadata_order_naming_an_unknown_service_is_refused(self):
        with self.assertRaises(ValueError):
            await prefs.save(replace(await prefs.load(self.user_id),
                                     metadata_order=["letterboxd"]))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)

    async def test_the_preference_goes_when_the_account_does(self):
        await prefs.save(replace(await prefs.load(self.user_id), calendar_source="simkl"))
        await db.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)

    async def test_a_stated_tracker_order_survives_a_round_trip(self):
        await prefs.save(replace(await prefs.load(self.user_id),
                                 tracker_priority=["simkl", "trakt"]))
        self.assertEqual((await prefs.load(self.user_id)).tracker_priority,
                         ["simkl", "trakt"])

    async def test_a_row_predating_the_column_reads_as_no_opinion(self):
        """The column has a default, so the migration gave every existing row an
        empty list rather than a NULL nothing can read — and an empty list is
        exactly what every account had before it could state one."""
        await db.execute(
            "INSERT INTO source_prefs (user_id, calendar_source) VALUES (?, 'auto')",
            (self.user_id,))
        self.assertEqual((await prefs.load(self.user_id)).tracker_priority, [])

    async def test_an_order_naming_an_unknown_service_is_refused(self):
        """Refused rather than coerced, the same way a bad selection is: an order
        naming a service this app has never heard of is a bug in the screen, and
        dropping it quietly would hide the screen sending the wrong name."""
        with self.assertRaises(ValueError):
            await prefs.save(replace(await prefs.load(self.user_id),
                                     tracker_priority=["trakt", "letterboxd"]))

    async def test_an_order_naming_a_service_twice_is_refused(self):
        with self.assertRaises(ValueError):
            await prefs.save(replace(await prefs.load(self.user_id),
                                     tracker_priority=["trakt", "trakt"]))

    async def test_a_retired_service_survives_a_round_trip(self):
        await prefs.save(replace(await prefs.load(self.user_id),
                                 tracker_retired=["trakt"]))
        stored = await prefs.load(self.user_id)
        self.assertEqual(stored.tracker_retired, ["trakt"])
        self.assertFalse(stored.counts_tracker("trakt"))
        self.assertTrue(stored.counts_tracker("simkl"))

    async def test_a_row_predating_the_retired_column_counts_everything(self):
        """The column has a default, so every existing row got an empty list —
        and empty is exactly the state every account was in before it could
        retire anything. Failing OPEN matters more here than for the order: a row
        that read as "retire everything" would silently stop counting services
        nobody had retired."""
        await db.execute(
            "INSERT INTO source_prefs (user_id, calendar_source) VALUES (?, 'auto')",
            (self.user_id,))
        stored = await prefs.load(self.user_id)
        self.assertEqual(stored.tracker_retired, [])
        self.assertTrue(stored.counts_tracker("trakt"))

    async def test_retiring_an_unknown_service_is_refused(self):
        with self.assertRaises(ValueError):
            await prefs.save(replace(await prefs.load(self.user_id),
                                     tracker_retired=["trakt", "letterboxd"]))

    async def test_naming_a_service_twice_is_tolerated_here(self):
        """Unlike the ORDER beside it, which refuses a duplicate because naming
        one service twice has no single meaning. This is a SET of exclusions:
        naming one twice says exactly what naming it once says, so it is stored
        the once and reads back the same whatever order the screen sent."""
        saved = await prefs.save(replace(await prefs.load(self.user_id),
                                         tracker_retired=["trakt", "trakt"]))
        self.assertEqual(saved.tracker_retired, ["trakt"])

    async def test_the_two_preferences_do_not_overwrite_each_other(self):
        """They are stored in one row and written by one verb, so a save that
        forgot either column would silently discard whichever the screen was not
        editing at the time."""
        await prefs.save(replace(await prefs.load(self.user_id),
                                 tracker_priority=["simkl", "trakt"]))
        await prefs.save(replace(await prefs.load(self.user_id),
                                 tracker_retired=["trakt"]))
        stored = await prefs.load(self.user_id)
        self.assertEqual(stored.tracker_priority, ["simkl", "trakt"])
        self.assertEqual(stored.tracker_retired, ["trakt"])


class TrackerOrderTests(unittest.TestCase):
    """Which linked tracker decides. Pure, like the selection rules above — the
    reordering takes the services that answer rather than looking them up."""

    def _prefs(self, priority):
        return prefs.SourcePrefs(user_id=1, tracker_priority=priority)

    def test_an_account_with_no_opinion_gets_the_declared_order_back(self):
        """Which is what every account had before this could be stated, and what
        keeps a single-service account behaving exactly as it did."""
        self.assertEqual(self._prefs([]).tracker_order(["trakt", "simkl"]),
                         ["trakt", "simkl"])

    def test_the_named_service_leads(self):
        self.assertEqual(self._prefs(["simkl"]).tracker_order(["trakt", "simkl"]),
                         ["simkl", "trakt"])

    def test_it_reorders_and_never_filters(self):
        """THE RULE THAT MAKES ONE PREFERENCE WORK ACROSS MIXED ROWS. A season
        only the un-preferred service knows about still has that service's
        number, because it is still in the list — just not first."""
        self.assertEqual(sorted(self._prefs(["simkl"]).tracker_order(["trakt", "simkl"])),
                         ["simkl", "trakt"])

    def test_a_preferred_service_that_does_not_answer_decides_nothing(self):
        """`sources` is already the set that answers for this account, so a
        service named here but not linked is simply absent — which is the whole
        fix for a service deciding from the number it left behind when its link
        lapsed."""
        self.assertEqual(self._prefs(["trakt", "simkl"]).tracker_order(["simkl"]),
                         ["simkl"])

    def test_a_name_this_version_does_not_know_falls_out(self):
        """A row written by a newer version must not stop an older one rendering
        a page — the same degrade rule the selections take."""
        self.assertEqual(self._prefs(["letterboxd", "simkl"]).tracker_order(
            ["trakt", "simkl"]), ["simkl", "trakt"])

    def test_a_document_that_is_not_a_list_reads_as_no_opinion(self):
        self.assertEqual(self._prefs("simkl").tracker_order(["trakt", "simkl"]),
                         ["trakt", "simkl"])


if __name__ == "__main__":
    unittest.main()
