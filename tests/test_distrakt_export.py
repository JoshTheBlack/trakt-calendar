"""Tests for the per-user distrakt JSON export / restore.

Export is one user's complete tracker dataset; restore is the inverse — REPLACE,
not merge, in one transaction, scoped to the session user. The properties that
matter and are tested here:

  - export -> restore is a round-trip identity,
  - restore IGNORES any user_id present in the file and writes only the session
    user's rows (a document can never write into someone else's tracker),
  - restore refuses a schema version it doesn't understand rather than guessing,
  - restore replaces rather than merges, and leaves the previous data intact if
    the document fails part-way through.

Both the data-layer functions and the two HTTP routes are exercised. No network.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_distrakt_export -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-distrakt-export-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, distrakt  # noqa: E402
from app import watch_history as wh  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"


async def _seed_dataset(user_id: int, *, tag: str) -> None:
    """A dataset touching all five tables, so a round trip has something to
    prove in each of them."""
    await distrakt.add_show(user_id, "2026-07", {
        "trakt_id": 101, "tmdb": 555, "season": 1, "slug": f"slug-{tag}",
        "title": f"Show {tag}", "network": "Net", "media": "show",
    })
    await distrakt.add_show(user_id, "2026-08", {
        "trakt_id": 202, "season": 2, "slug": f"other-{tag}", "title": "Second",
    })
    # A frozen month, which is where the snapshot columns and movies_json matter.
    doc = await distrakt.load_month(user_id, "2026-07")
    doc["closed"] = True
    doc["totals_refreshed_at"] = db.now()
    doc["movies"] = [{"title": f"Film {tag}", "year": 2026, "watched_at": "2026-07-04T00:00:00Z"}]
    doc["shows"][0].update({
        "watched": 4, "total": 8, "cadence": "Tue", "premiere": "7/1",
        "finale": "7/29", "bucket": "keepup",
        "started_airing": True, "finished_airing": False,
    })
    await distrakt.save_month(user_id, doc)
    await wh._save(user_id, {
        "last_synced": "2026-07-20",
        "beacons": {"ep_watched": tag, "ep_removed": None,
                    "mv_watched": tag, "mv_removed": None},
        "shows": {"101": {"1": [1, 2, 3, 4]}},
        "movies": {"9": {"title": f"Film {tag}", "year": 2026,
                         "watched_at": "2026-07-04T00:00:00Z"}},
    })


class ExportTestCase(unittest.IsolatedAsyncioTestCase):
    _counter = 0

    async def asyncSetUp(self):
        ExportTestCase._counter += 1
        db.set_db_path(TMP / f"export-{ExportTestCase._counter}.db")
        await db.migrate()
        save_settings(Settings())
        self.user_id = await auth.create_user(
            username="tracker", password="hunter2hunter2", settings=Settings(),
            calendar_approved=True, distrakt_approved=True)
        self.other_id = await auth.create_user(
            username="other", password="hunter2hunter2", settings=Settings(),
            calendar_approved=True, distrakt_approved=True)

    async def asyncTearDown(self):
        db.close_thread_connection()


class RoundTripTests(ExportTestCase):
    async def test_export_restore_is_a_round_trip_identity(self):
        await _seed_dataset(self.user_id, tag="mine")
        original = await distrakt.export_user_data(self.user_id)

        await distrakt.restore_user_data(self.user_id, original)
        again = await distrakt.export_user_data(self.user_id)

        # `exported_at` is the moment of export, not part of the dataset.
        original.pop("exported_at"), again.pop("exported_at")
        self.assertEqual(again, original)

    async def test_round_trip_preserves_the_frozen_month_verbatim(self):
        """The whole point of the snapshot columns: a restored frozen month still
        renders offline, with its airing flags and movies intact."""
        await _seed_dataset(self.user_id, tag="mine")
        doc = await distrakt.export_user_data(self.user_id)

        # Wipe everything, then restore from the document alone.
        await auth.wipe_user_data(self.user_id)
        self.assertEqual(await distrakt.list_months(self.user_id), [])
        await distrakt.restore_user_data(self.user_id, doc)

        july = await distrakt.load_month(self.user_id, "2026-07")
        self.assertTrue(july["closed"])
        self.assertEqual(july["movies"], [{"title": "Film mine", "year": 2026,
                                           "watched_at": "2026-07-04T00:00:00Z"}])
        rec = distrakt.frozen_shows(july)[0]
        self.assertTrue(rec["started_airing"])
        self.assertFalse(rec["finished_airing"])
        self.assertEqual((rec["watched"], rec["total"], rec["bucket"]), (4, 8, "keepup"))
        # and the watch-history side came back too
        state = await wh._load(self.user_id)
        self.assertEqual(wh.watched_map(state), {(101, 1): 4})
        self.assertEqual(state["last_synced"], "2026-07-20")
        self.assertIn("9", state["movies"])

    async def test_export_contains_only_the_requesting_users_data(self):
        await _seed_dataset(self.user_id, tag="mine")
        await _seed_dataset(self.other_id, tag="theirs")
        doc = await distrakt.export_user_data(self.user_id)
        blob = repr(doc)
        self.assertIn("slug-mine", blob)
        self.assertNotIn("theirs", blob)
        self.assertEqual({r["month"] for r in doc["distrakt_months"]}, {"2026-07", "2026-08"})


class RestoreScopingTests(ExportTestCase):
    async def test_restore_ignores_a_user_id_present_in_the_file(self):
        """A hostile or hand-edited document naming another account must land on
        the SESSION user's rows and nowhere else."""
        await _seed_dataset(self.user_id, tag="mine")
        doc = await distrakt.export_user_data(self.user_id)
        # Plant the other user's id everywhere a naive restore might honour it.
        doc["user_id"] = self.other_id
        for table, _cols in distrakt._EXPORT_TABLES:
            for row in doc[table]:
                row["user_id"] = self.other_id

        await distrakt.restore_user_data(self.other_id, doc)

        # It went to the account that asked for it...
        self.assertEqual(await distrakt.list_months(self.other_id), ["2026-07", "2026-08"])
        # ...and the id in the file bought nothing: the original owner is untouched.
        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-07", "2026-08"])
        rows = await db.fetch_all(
            "SELECT user_id, COUNT(*) c FROM distrakt_shows GROUP BY user_id ORDER BY user_id")
        self.assertEqual([(r["user_id"], r["c"]) for r in rows],
                         [(self.user_id, 2), (self.other_id, 2)])

    async def test_restore_replaces_rather_than_merges(self):
        await _seed_dataset(self.user_id, tag="mine")
        doc = await distrakt.export_user_data(self.user_id)
        # A month that is NOT in the document must be gone after the restore.
        await distrakt.add_show(self.user_id, "2026-09", {"trakt_id": 777, "season": 1})
        self.assertIn("2026-09", await distrakt.list_months(self.user_id))

        await distrakt.restore_user_data(self.user_id, doc)

        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-07", "2026-08"])
        self.assertIsNone(await distrakt.load_month(self.user_id, "2026-09"))

    async def test_restore_refuses_an_unknown_schema_version(self):
        await _seed_dataset(self.user_id, tag="mine")
        doc = await distrakt.export_user_data(self.user_id)
        for bad in (distrakt.EXPORT_SCHEMA + 1, 0, None, "1"):
            with self.subTest(schema=bad):
                bad_doc = dict(doc, schema=bad)
                with self.assertRaises(distrakt.RestoreError):
                    await distrakt.restore_user_data(self.other_id, bad_doc)
        # Nothing was written by any of the refused attempts.
        self.assertEqual(await distrakt.list_months(self.other_id), [])

    async def test_a_failing_document_leaves_the_existing_data_intact(self):
        """One transaction: a row that violates the schema rolls the whole restore
        back rather than leaving a half-replaced tracker behind."""
        await _seed_dataset(self.user_id, tag="mine")
        doc = await distrakt.export_user_data(self.user_id)
        before = await distrakt.export_user_data(self.user_id)
        doc["distrakt_shows"].append({"month": "2026-07", "trakt_id": None, "season": None})

        with self.assertRaises(db.DatabaseError):
            await distrakt.restore_user_data(self.user_id, doc)

        after = await distrakt.export_user_data(self.user_id)
        before.pop("exported_at"), after.pop("exported_at")
        self.assertEqual(after, before)


class ExportRouteTests(unittest.TestCase):
    """The two HTTP endpoints, end to end. JSON posts carry an Origin header
    because every mutating endpoint is same-origin checked."""
    _counter = 0

    def setUp(self):
        ExportRouteTests._counter += 1
        db.set_db_path(TMP / f"export-route-{ExportRouteTests._counter}.db")
        asyncio.run(db.migrate())
        save_settings(Settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = self._make_distrakt_user("tracker")
        self.other_id = self._make_distrakt_user("other")

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_distrakt_user(self, username: str) -> int:
        user_id = asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=Settings(),
            calendar_approved=True, distrakt_approved=True))
        # distrakt additionally requires a linked Trakt identity.
        asyncio.run(db.execute(
            "INSERT INTO linked_identities (user_id, provider, provider_user_id, created_at) "
            "VALUES (?, 'trakt', ?, ?)", (user_id, f"trakt-{user_id}", db.now())))
        return user_id

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def test_export_then_restore_through_the_routes(self):
        asyncio.run(_seed_dataset(self.user_id, tag="mine"))
        self.sign_in_as(self.user_id)

        exported = self.client.get("/api/distrakt/export")
        self.assertEqual(exported.status_code, 200)
        doc = exported.json()
        self.assertEqual(doc["schema"], distrakt.EXPORT_SCHEMA)
        self.assertIn("attachment", exported.headers.get("content-disposition", ""))

        # Restoring the same document onto a DIFFERENT account moves the data to
        # whoever is signed in, which is the "move my dev data over" case.
        self.sign_in_as(self.other_id)
        resp = self.client.post("/api/distrakt/restore", json=doc)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["months"], ["2026-07", "2026-08"])
        self.assertEqual(asyncio.run(distrakt.list_months(self.other_id)),
                         ["2026-07", "2026-08"])

    def test_restore_route_refuses_an_unknown_schema(self):
        self.sign_in_as(self.user_id)
        resp = self.client.post("/api/distrakt/restore", json={"schema": 99})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(asyncio.run(distrakt.list_months(self.user_id)), [])

    def test_export_is_scoped_to_the_caller(self):
        asyncio.run(_seed_dataset(self.other_id, tag="theirs"))
        self.sign_in_as(self.user_id)
        doc = self.client.get("/api/distrakt/export").json()
        self.assertEqual(doc["distrakt_months"], [])
        self.assertNotIn("theirs", repr(doc))

    def test_both_routes_need_a_distrakt_approved_session(self):
        plain = asyncio.run(auth.create_user(
            username="plain", password="hunter2hunter2", settings=Settings(),
            calendar_approved=True))
        for user in (None, plain):
            with self.subTest(user=user):
                self.client.cookies.clear()
                if user is not None:
                    self.sign_in_as(user)
                self.assertIn(self.client.get("/api/distrakt/export").status_code, (401, 403))
                self.assertIn(
                    self.client.post("/api/distrakt/restore", json={"schema": 1}).status_code,
                    (401, 403))


if __name__ == "__main__":
    unittest.main()
