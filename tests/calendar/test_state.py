"""Unit tests for the per-user calendar state layer (app/calendar/state.py).

Covers: the not-watching delta (idempotent, per (user, endpoint, year, month)),
the whole-document save/load round trip in app/state.py's shape, the distrakt
roster union read, and the change-detection writer preserving history when it
isn't resent.

No network.
"""
from __future__ import annotations

import json
import unittest

from app import db
from app.calendar import state as calendar_state
from tests.support import migrated_db

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
        migrated_db("calstate")
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


class UnmarkingClearsLegacySpellingsTests(StateTestCase):
    """A mark the READ accepts must be one the WRITE can clear.

    `marked_by_ids` honours a mark stored under any of a title's slug
    namespaces, because marks predate mark keys. Unmarking deleted the mark key
    alone, so those marks could be read but never cleared: the card drew as
    marked, the toggle deleted a row that was not there, and the redraw showed
    it marked again. On one real account that was 909 of 1,038 marks.
    """

    async def _store_title(self, title_key: str, ids: dict) -> None:
        """One calendar_titles row, which is where the spellings are read from."""
        await db.execute(
            "INSERT INTO calendar_titles (source, media, source_id, title_key, "
            "title, title_fold, ids_json, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            ("simkl", "show", ids.get("simkl") or "1", title_key, "T", "t",
             json.dumps(ids), db.now()),
        )

    async def test_a_mark_under_a_slug_is_cleared_by_the_mark_key(self):
        key = "show:tmdb:321122"
        await self._store_title(key, {"slug": "lovesick", "simkl": 3101847,
                                      "tmdb": "321122"})
        # The shape a mark made before mark keys existed is stored in.
        await calendar_state.set_not_watching(self.user_id, "lovesick", True)
        marks = await calendar_state.not_watching_ids(self.user_id)
        self.assertTrue(calendar_state.marked_by_ids(
            marks, mark_key=key, ids={"slug": "lovesick"}))

        # The card can only send its mark key, and that has to be enough.
        await calendar_state.set_not_watching(self.user_id, key, False)
        marks = await calendar_state.not_watching_ids(self.user_id)
        self.assertEqual(marks, set())
        self.assertFalse(calendar_state.marked_by_ids(
            marks, mark_key=key, ids={"slug": "lovesick"}))

    async def test_a_spelling_two_titles_share_clears_both(self):
        """Deliberate, not collateral: the stored mark was hiding both already.

        `lovesick` is the slug of two different 2026 shows on one real account,
        so the single row answered for both and the read cannot tell them apart.
        Clearing it is what lets the next mark be written under a key that can.
        """
        a, b = "show:tmdb:321122", "show:tmdb:290234"
        await self._store_title(a, {"slug": "lovesick", "simkl": 3101847})
        await self._store_title(b, {"slug": "lovesick", "simkl": 3211409})
        await calendar_state.set_not_watching(self.user_id, "lovesick", True)
        await calendar_state.set_not_watching(self.user_id, a, False)
        self.assertEqual(await calendar_state.not_watching_ids(self.user_id), set())

    async def test_marking_writes_the_mark_key_alone(self):
        """One new fact, one row — the asymmetry `set_not_watching` documents."""
        key = "show:tmdb:321122"
        await self._store_title(key, {"slug": "lovesick", "tvdbslug": "lovesick-477390"})
        await calendar_state.set_not_watching(self.user_id, key, True)
        self.assertEqual(await calendar_state.not_watching_ids(self.user_id), {key})

    async def test_a_title_nothing_stores_still_unmarks_by_its_own_key(self):
        """The 85 marks naming titles no calendar row can identify: there is
        nothing to expand, and the mark key alone must still come out."""
        key = "show:tmdb:999999"
        await calendar_state.set_not_watching(self.user_id, key, True)
        await calendar_state.set_not_watching(self.user_id, key, False)
        self.assertEqual(await calendar_state.not_watching_ids(self.user_id), set())


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
    """The marks are timestamped, and the timestamp is what orders them.

    Nothing reads it to decide anything any more. It used to separate a mark made
    before a month opened ("I never intended to watch this") from one made
    part-way through it ("I was following this and stopped"), and the tracker now
    tells those apart by the MONTH'S standing instead — a month nobody has reached
    has no verdicts to record, whenever the mark was made.
    """

    async def test_a_freshly_written_mark_is_dated(self):
        """not_watching_list promises oldest mark first, and the write is the only
        thing that populates the column it orders by."""
        await calendar_state.set_not_watching(self.user_id, "now", True)
        row = await db.fetch_one(
            "SELECT created_at FROM not_watching_shows WHERE user_id = ? AND item_id = ?",
            (self.user_id, "now"))
        self.assertGreater(row["created_at"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
