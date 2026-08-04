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

    def test_every_known_service_is_a_valid_selection(self):
        """Spelled from Source rather than restated, so a service the app has
        cannot be one this refuses."""
        for source in Source:
            self.assertIn(str(source), prefs.SELECTIONS)


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

    async def test_the_preference_goes_when_the_account_does(self):
        await prefs.save(replace(await prefs.load(self.user_id), tracker_source="simkl"))
        await db.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM source_prefs"), 0)


if __name__ == "__main__":
    unittest.main()
