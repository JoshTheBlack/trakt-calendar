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
from tests.support import new_db_path


class SelectionTests(unittest.TestCase):
    """Pure, so they need no database — which is the point of `admits` taking the
    linked set rather than looking it up."""

    def test_auto_asks_every_linked_service_and_nothing_else(self):
        linked = {Source.TRAKT}
        self.assertTrue(prefs.admits(prefs.AUTO, Source.TRAKT, linked))
        self.assertFalse(prefs.admits(prefs.AUTO, Source.SIMKL, linked))

    def test_auto_follows_a_link_that_lapses(self):
        self.assertFalse(prefs.admits(prefs.AUTO, Source.SIMKL, set()))

    def test_both_keeps_asking_when_a_link_lapses(self):
        """The whole reason `both` is not a synonym for `auto`: it is a stated
        preference, and a stated preference must not quietly become single-source
        because a token expired. The missing service shows as missing."""
        self.assertTrue(prefs.admits(prefs.BOTH, Source.SIMKL, set()))
        self.assertTrue(prefs.admits(prefs.BOTH, Source.TRAKT, set()))

    def test_naming_one_service_excludes_the_other_however_it_is_linked(self):
        linked = {Source.TRAKT, Source.SIMKL}
        self.assertTrue(prefs.admits("simkl", Source.SIMKL, linked))
        self.assertFalse(prefs.admits("simkl", Source.TRAKT, linked))

    def test_a_string_and_a_source_member_are_the_same_answer(self):
        self.assertEqual(prefs.admits(prefs.AUTO, "trakt", {"trakt"}),
                         prefs.admits(prefs.AUTO, Source.TRAKT, {Source.TRAKT}))

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

    def test_the_tracker_still_needs_the_link_the_calendar_does_not(self):
        """Stated explicitly so that widening the calendar cannot quietly widen
        this too. Reading one person's viewing history means asking with THEIR
        token, so a service they have not linked has nothing to answer with."""
        auto = prefs.SourcePrefs(user_id=1, tracker_source=prefs.AUTO)
        self.assertTrue(auto.admits_tracker(Source.TRAKT, {"trakt"}))
        self.assertFalse(auto.admits_tracker(Source.SIMKL, {"trakt"}))
        self.assertFalse(auto.admits_tracker(Source.TRAKT, set()))

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

    def test_the_tracker_reads_a_named_set_the_same_way(self):
        """The vocabulary is one vocabulary; only what `auto` comes out to
        differs between the two halves."""
        self.assertTrue(prefs.admits("trakt+simkl", Source.SIMKL, set()))
        self.assertFalse(prefs.admits("trakt", Source.SIMKL, {"simkl"}))


class PerEndpointSelectionTests(unittest.TestCase):
    """One account wanting a service's shows and not its movies."""

    def prefs(self, **kwargs):
        return prefs.SourcePrefs(user_id=1, **kwargs)

    def test_an_override_answers_for_its_own_calendar_only(self):
        stated = self.prefs(calendar_source=prefs.AUTO,
                            endpoint_sources={"movies": "trakt"})
        self.assertEqual(stated.calendar_selection("movies"), "trakt")
        self.assertEqual(stated.calendar_selection("shows"), prefs.AUTO)
        self.assertFalse(stated.admits_calendar(Source.SIMKL, "movies"))
        self.assertTrue(stated.admits_calendar(Source.SIMKL, "shows"))

    def test_asking_without_an_endpoint_gets_the_account_wide_answer(self):
        """Every caller that has no endpoint in hand keeps the behaviour it had
        before there was one to have."""
        stated = self.prefs(endpoint_sources={"movies": "trakt"})
        self.assertEqual(stated.calendar_selection(), prefs.AUTO)
        self.assertTrue(stated.admits_calendar(Source.SIMKL))

    def test_an_unreadable_override_falls_back_rather_than_erroring(self):
        for value in ("letterboxd", "", 7, None, ["trakt"]):
            with self.subTest(value=value):
                stated = self.prefs(calendar_source="simkl",
                                    endpoint_sources={"movies": value})
                self.assertEqual(stated.calendar_selection("movies"), "simkl")

    def test_an_endpoint_this_version_does_not_have_is_simply_never_asked_about(self):
        stated = self.prefs(endpoint_sources={"retired-calendar": "trakt"})
        self.assertEqual(stated.calendar_selection("shows"), prefs.AUTO)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("source-prefs")
        await db.migrate()
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
        self.assertEqual(stored.tracker_source, prefs.AUTO)
        self.assertEqual(stored.precedence, {})
        # ...and nothing was created by asking.
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)

    async def test_a_saved_preference_reads_back(self):
        saved = await prefs.save(replace(
            await prefs.load(self.user_id),
            calendar_source=prefs.BOTH, tracker_source="simkl",
            precedence={"default": "simkl", "fields": {"poster": "trakt"}}))
        self.assertEqual(saved.calendar_source, prefs.BOTH)
        again = await prefs.load(self.user_id)
        self.assertEqual(again.calendar_source, prefs.BOTH)
        self.assertEqual(again.tracker_source, "simkl")
        self.assertEqual(again.precedence, {"default": "simkl",
                                            "fields": {"poster": "trakt"}})

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
        await prefs.save(replace(await prefs.load(self.user_id), tracker_source="trakt"))
        self.assertEqual((await prefs.load(other)).tracker_source, prefs.AUTO)

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
            "INSERT INTO source_prefs (user_id, calendar_source, tracker_source, "
            "precedence_json) VALUES (?, 'letterboxd', 'auto', 'not json')",
            (self.user_id,))
        stored = await prefs.load(self.user_id)
        self.assertEqual(stored.calendar_source, prefs.AUTO)
        self.assertEqual(stored.precedence, {})

    async def test_a_named_set_and_a_per_endpoint_override_round_trip(self):
        await prefs.save(replace(
            await prefs.load(self.user_id),
            calendar_source="simkl+trakt",
            endpoint_sources={"movies": "trakt", "shows": "trakt+simkl"}))
        again = await prefs.load(self.user_id)
        # Stored in declared order, so one choice is one stored value.
        self.assertEqual(again.calendar_source, "trakt+simkl")
        self.assertEqual(again.endpoint_sources,
                         {"movies": "trakt", "shows": "trakt+simkl"})

    async def test_an_override_naming_an_unknown_service_is_refused(self):
        with self.assertRaises(ValueError):
            await prefs.save(replace(await prefs.load(self.user_id),
                                     endpoint_sources={"movies": "letterboxd"}))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)

    async def test_a_row_predating_the_per_endpoint_column_reads_as_no_override(self):
        """The column has a default, so the migration gave every existing row an
        empty document rather than a NULL nothing can read."""
        await db.execute(
            "INSERT INTO source_prefs (user_id, calendar_source, tracker_source, "
            "precedence_json) VALUES (?, 'both', 'auto', '{}')",
            (self.user_id,))
        stored = await prefs.load(self.user_id)
        self.assertEqual(stored.endpoint_sources, {})
        self.assertEqual(stored.calendar_source, prefs.BOTH)

    async def test_the_preference_goes_when_the_account_does(self):
        await prefs.save(replace(await prefs.load(self.user_id), tracker_source="simkl"))
        await db.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)


if __name__ == "__main__":
    unittest.main()
