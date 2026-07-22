"""Unit tests for the per-user calendar state layer and the legacy import
(app/calendar_state).

Covers: the not-watching delta (idempotent, per (user, endpoint, year, month)),
the whole-document save/load round trip in app/state.py's shape, the distrakt
roster union read, the change-detection writer preserving history when it isn't
resent, and the legacy state_*.json import — which backs the files up BEFORE
reading them, lands the rows on the given user, and is idempotent on a re-run.
The import is exercised both directly and through onboarding (the only path that
runs it in production), the same way tests/test_auth_routes.py drives onboarding.

No network. TRAKT_DATA_DIR points at a temp dir (set BEFORE importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_calendar_state -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-calstate-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, calendar_state, db  # noqa: E402
from app.config import DATA_DIR, Settings, save_settings  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])


async def _make_user(username="viewer") -> int:
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, created_at, updated_at) "
        "VALUES (?, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


def _clear_legacy_files():
    for f in DATA_DIR.glob("state_*.json"):
        f.unlink()
    backup = calendar_state.LEGACY_BACKUP_DIR
    if backup.exists():
        for f in backup.glob("*"):
            f.unlink()


class StateTestCase(unittest.IsolatedAsyncioTestCase):
    _counter = 0

    async def asyncSetUp(self):
        StateTestCase._counter += 1
        db.set_db_path(TMP / f"calstate-{StateTestCase._counter}.db")
        await db.migrate()
        _clear_legacy_files()
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


class LegacyImportTests(StateTestCase):
    def _write_legacy(self, name, doc):
        (DATA_DIR / name).write_text(json.dumps(doc), encoding="utf-8")

    async def test_backs_up_before_importing_and_lands_rows_on_the_user(self):
        self._write_legacy("state_shows_new_2026_7.json",
                            {"notWatching": ["slug-a", "slug-b"], "lastCount": 3,
                             "lastShowIds": ["slug-a"], "history": [{"h": 1}]})
        self._write_legacy("state_shows_premieres_2026_8.json", {"notWatching": ["slug-c"]})

        count = await calendar_state.import_legacy_state(self.user_id)
        self.assertEqual(count, 2)

        # The backup copies exist and match the originals verbatim.
        backup = calendar_state.LEGACY_BACKUP_DIR
        self.assertTrue((backup / "state_shows_new_2026_7.json").exists())
        self.assertEqual(
            (backup / "state_shows_new_2026_7.json").read_text(encoding="utf-8"),
            (DATA_DIR / "state_shows_new_2026_7.json").read_text(encoding="utf-8"),
        )
        # The originals are NOT deleted.
        self.assertTrue((DATA_DIR / "state_shows_new_2026_7.json").exists())

        # Change detection landed on this user, mapped back to the right
        # endpoint/year/month; every file's marks merged into the one global set.
        july = await calendar_state.load_state(self.user_id, "shows/new", 2026, 7)
        self.assertEqual(july["lastCount"], 3)
        self.assertEqual(july["lastShowIds"], ["slug-a"])
        self.assertEqual(july["history"], [{"h": 1}])
        self.assertEqual(set(july["notWatching"]), {"slug-a", "slug-b", "slug-c"})
        aug = await calendar_state.load_state(self.user_id, "shows/premieres", 2026, 8)
        self.assertEqual(set(aug["notWatching"]), {"slug-a", "slug-b", "slug-c"})

    async def test_import_is_idempotent_on_a_rerun(self):
        self._write_legacy("state_shows_2026_7.json", {"notWatching": ["slug-a"]})
        await calendar_state.import_legacy_state(self.user_id)
        await calendar_state.import_legacy_state(self.user_id)  # re-run
        rows = await db.fetch_all(
            "SELECT * FROM not_watching_shows WHERE user_id = ?", (self.user_id,))
        self.assertEqual(len(rows), 1)

    async def test_unrecognized_filenames_are_skipped(self):
        self._write_legacy("state_nonsense_endpoint_2026_7.json", {"notWatching": ["x"]})
        self._write_legacy("state_shows_2026_7.json", {"notWatching": ["ok"]})
        count = await calendar_state.import_legacy_state(self.user_id)
        self.assertEqual(count, 1)  # only the recognized one
        self.assertEqual(
            (await calendar_state.load_state(self.user_id, "shows", 2026, 7))["notWatching"], ["ok"])

    async def test_no_files_is_a_noop(self):
        self.assertEqual(await calendar_state.import_legacy_state(self.user_id), 0)


class OnboardingImportTests(unittest.TestCase):
    """The production path: onboarding creates the bootstrap admin and calls the
    importer via app/auth_routes._import_legacy_calendar_state."""
    _counter = 0

    def setUp(self):
        OnboardingImportTests._counter += 1
        db.set_db_path(TMP / f"calstate-onboard-{OnboardingImportTests._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        _clear_legacy_files()
        (DATA_DIR / "state_shows_new_2026_7.json").write_text(
            json.dumps({"notWatching": ["onboarded-slug"], "lastCount": 2}), encoding="utf-8")
        from app.main import app
        self.client = TestClient(app, base_url="https://testserver",
                                 headers={"Origin": "https://testserver"})

    def tearDown(self):
        self.client.close()
        _clear_legacy_files()
        db.close_thread_connection()

    def test_onboarding_imports_legacy_state_onto_the_new_admin(self):
        resp = self.client.post("/onboarding", json={
            "username": "operator", "password": "hunter2hunter2", "password_confirm": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 200, resp.text)
        admin = asyncio.run(auth.find_user_by_username("operator"))
        loaded = asyncio.run(
            calendar_state.load_state(admin["id"], "shows/new", 2026, 7))
        self.assertEqual(loaded["notWatching"], ["onboarded-slug"])
        self.assertEqual(loaded["lastCount"], 2)
        # And the backup was made.
        self.assertTrue((calendar_state.LEGACY_BACKUP_DIR / "state_shows_new_2026_7.json").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
