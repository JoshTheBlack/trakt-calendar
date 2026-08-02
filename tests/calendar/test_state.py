"""Unit tests for the per-user calendar state layer (app/calendar/state.py).

Covers: the not-watching delta (idempotent, per (user, endpoint, year, month)),
the whole-document save/load round trip in app/state.py's shape, the distrakt
roster union read, and the change-detection writer preserving history when it
isn't resent.

No network.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app import db
from app.calendar import state as calendar_state
from tests.support import new_db_path

async def _make_user(username="viewer") -> int:
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, created_at, updated_at) "
        "VALUES (?, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


class StateTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("calstate")
        await db.migrate()
        self.user_id = await _make_user()

    async def asyncTearDown(self):
        db.close_thread_connection()


class NotWatchingDeltaTests(StateTestCase):
    async def test_toggle_on_and_off_is_a_delta(self):
        await calendar_state.set_not_watching(self.user_id, "slug-a", True)
        await calendar_state.set_not_watching(self.user_id, "slug-b", True)
        self.assertEqual(
            set(await calendar_state.not_watching_list(self.user_id)),
            {"slug-a", "slug-b"},
        )
        await calendar_state.set_not_watching(self.user_id, "slug-a", False)
        self.assertEqual(await calendar_state.not_watching_list(self.user_id), ["slug-b"])

    async def test_marking_twice_is_idempotent(self):
        await calendar_state.set_not_watching(self.user_id, "slug-a", True)
        await calendar_state.set_not_watching(self.user_id, "slug-a", True)
        self.assertEqual(await calendar_state.not_watching_list(self.user_id), ["slug-a"])

    async def test_a_mark_applies_to_every_endpoint_and_month(self):
        """The point of the global store: one toggle, seen everywhere the show is."""
        await calendar_state.set_not_watching(self.user_id, "mine", True)
        for endpoint, year, month in (("shows/new", 2026, 7), ("shows", 2026, 7),
                                      ("movies", 2027, 1)):
            state = await calendar_state.load_state(self.user_id, endpoint, year, month)
            self.assertEqual(state["notWatching"], ["mine"])

    async def test_isolated_per_user(self):
        other = await _make_user("other")
        await calendar_state.set_not_watching(self.user_id, "mine", True)
        self.assertEqual(await calendar_state.not_watching_list(other), [])


class WholeDocumentTests(StateTestCase):
    async def test_save_then_load_round_trips_in_state_shape(self):
        payload = {
            "notWatching": ["slug-a", "slug-b"],
            "history": [{"k": 1}],
            "lastCount": 5,
            "lastShowIds": ["slug-a", "slug-c"],
        }
        await calendar_state.save_state(self.user_id, "shows/new", 2026, 7, payload)
        loaded = await calendar_state.load_state(self.user_id, "shows/new", 2026, 7)
        self.assertEqual(set(loaded["notWatching"]), {"slug-a", "slug-b"})
        self.assertEqual(loaded["history"], [{"k": 1}])
        self.assertEqual(loaded["lastCount"], 5)
        self.assertEqual(loaded["lastShowIds"], ["slug-a", "slug-c"])

    async def test_empty_load_matches_the_legacy_default_shape(self):
        loaded = await calendar_state.load_state(self.user_id, "shows/new", 2026, 7)
        self.assertEqual(
            loaded, {"notWatching": [], "history": [], "lastCount": None, "lastShowIds": None})

    async def test_save_adds_to_the_not_watching_set_rather_than_replacing_it(self):
        """A document describes ONE view, so an id missing from it is not evidence
        the user unmarked that show — it may have been marked from another month
        entirely. Unmarking is set_not_watching's job."""
        await calendar_state.save_state(self.user_id, "shows/new", 2026, 7, {"notWatching": ["a", "b"]})
        await calendar_state.save_state(self.user_id, "shows", 2026, 8, {"notWatching": ["b", "c"]})
        self.assertEqual(
            set((await calendar_state.load_state(self.user_id, "shows/new", 2026, 7))["notWatching"]),
            {"a", "b", "c"},
        )


class ViewStateTests(StateTestCase):
    async def test_set_view_state_preserves_history_when_not_resent(self):
        await calendar_state.save_state(
            self.user_id, "shows/new", 2026, 7, {"history": [{"seen": True}], "lastCount": 1})
        # A change-detection write that omits history must not wipe it.
        await calendar_state.set_view_state(
            self.user_id, "shows/new", 2026, 7, last_count=9, last_show_ids=["x"])
        loaded = await calendar_state.load_state(self.user_id, "shows/new", 2026, 7)
        self.assertEqual(loaded["lastCount"], 9)
        self.assertEqual(loaded["lastShowIds"], ["x"])
        self.assertEqual(loaded["history"], [{"seen": True}])


class RosterUnionTests(StateTestCase):
    async def test_not_watching_ids_is_every_mark_the_user_has_made(self):
        """The distrakt roster read used to union two endpoints for one month.
        There is one set now, and the tracker asks the same question the calendar
        does: is this a show they said they aren't watching?"""
        for item_id in ("new-1", "prem-1", "all-1"):
            await calendar_state.set_not_watching(self.user_id, item_id, True)
        self.assertEqual(
            await calendar_state.not_watching_ids(self.user_id),
            {"new-1", "prem-1", "all-1"},
        )


class WhenAMarkWasMadeTests(StateTestCase):
    """The marks are timestamped, and the tracker reads them by date: one made
    before a month opened means "I never intended to watch this", and one made
    once the month was under way means "I was following this and stopped". Two
    different statements that look identical once made."""

    async def _mark_at(self, item_id: str, when: date) -> None:
        await calendar_state.set_not_watching(self.user_id, item_id, True)
        await db.execute(
            "UPDATE not_watching_shows SET created_at = ? WHERE user_id = ? AND item_id = ?",
            (int(datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp()),
             self.user_id, item_id),
        )

    async def test_only_the_marks_made_before_the_day_come_back(self):
        await self._mark_at("early", date(2026, 7, 20))
        await self._mark_at("late", date(2026, 8, 9))
        self.assertEqual(
            await calendar_state.not_watching_marked_before(self.user_id, date(2026, 8, 1)),
            {"early"},
        )

    async def test_a_mark_made_on_the_day_itself_is_not_before_it(self):
        """The boundary is the month's own 1st: a mark made that day is a decision
        about a month that has started, not about one that had not."""
        await self._mark_at("on-the-first", date(2026, 8, 1))
        self.assertEqual(
            await calendar_state.not_watching_marked_before(self.user_id, date(2026, 8, 1)),
            set(),
        )

    async def test_a_freshly_written_mark_is_dated(self):
        """Nothing above works if the column is not populated, and the write is
        the only thing that populates it."""
        await calendar_state.set_not_watching(self.user_id, "now", True)
        row = await db.fetch_one(
            "SELECT created_at FROM not_watching_shows WHERE user_id = ? AND item_id = ?",
            (self.user_id, "now"))
        self.assertGreater(row["created_at"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
