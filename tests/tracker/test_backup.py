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
"""
from __future__ import annotations

import asyncio
import unittest

from fastapi.testclient import TestClient

from app import auth, db, distrakt
from app.providers.base import ItemKey
from app.distrakt import watch_history as wh
from app.config import Settings, save_settings
from app.main import app
from tests.support import ORIGIN, migrated_db, new_db_path

async def _seed_dataset(user_id: int, *, tag: str) -> None:
    """A dataset touching all five tables, so a round trip has something to
    prove in each of them."""
    await distrakt.add_show(user_id, "2026-07", {
        "ids": {"trakt": 101, "tmdb": 555, "slug": f"slug-{tag}"}, "season": 1,
        "title": f"Show {tag}", "network": "Net", "media": "show",
    })
    await distrakt.add_show(user_id, "2026-08", {
        "ids": {"trakt": 202, "tmdb": 606, "slug": f"other-{tag}"}, "season": 2,
        "title": "Second",
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
        "shows": {"show:tmdb:555": {"ids": {"trakt": 101},
                                   "seasons": {"1": [1, 2, 3, 4]}}},
        "movies": {"movie:tmdb:9": {"ids": {"trakt": 9},
                                   "title": f"Film {tag}", "year": 2026,
                                   "watched_at": "2026-07-04T00:00:00Z"}},
    })

class ExportTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("export")
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
        self.assertEqual(wh.watched_map(state), {("show:tmdb:555", 1): 4})
        self.assertEqual(state["last_synced"], "2026-07-20")
        self.assertIn("movie:tmdb:9", state["movies"])

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
        for table, _cols in distrakt.backup._EXPORT_TABLES:
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
        await distrakt.add_show(self.user_id, "2026-09", {"ids": {"tmdb": 777}, "season": 1})
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
        doc["distrakt_shows"].append({"month": "2026-07", "match_id": None, "season": None})

        with self.assertRaises(db.DatabaseError):
            await distrakt.restore_user_data(self.user_id, doc)

        after = await distrakt.export_user_data(self.user_id)
        before.pop("exported_at"), after.pop("exported_at")
        self.assertEqual(after, before)

class ExportRouteTests(unittest.TestCase):
    """The two HTTP endpoints, end to end. JSON posts carry an Origin header
    because every mutating endpoint is same-origin checked."""
    def setUp(self):
        migrated_db("export-route")
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

class LegacyBackupTests(ExportTestCase):
    """A backup taken BEFORE the tracker was re-keyed, restored onto the current
    schema.

    This is the one thing this change could destroy that nothing else could give
    back, so it is tested against a document written by hand in the old shape
    rather than one this code produced: a fixture the new writer generates would
    only ever prove the new writer agrees with itself.
    """

    def _legacy_doc(self, schema: int = 2) -> dict:
        """A schema-2 export: rows named by Trakt's id, provenance called
        `source`, and the two caches keyed the same way."""
        return {
            "schema": schema,
            "exported_at": 1750000000,
            "distrakt_months": [
                {"month": "2026-03", "closed": 1, "totals_refreshed_at": 1750000000,
                 "movies_json": '[{"title": "Frozen Film", "year": 1999, '
                                '"watched_at": "2026-03-04T00:00:00Z"}]',
                 "created_at": 1740000000},
            ],
            "distrakt_shows": [
                {"month": "2026-03", "trakt_id": 601, "tmdb": 9001, "slug": "old-show",
                 "media": "show", "title": "Old Show", "season": 2, "network": "HBO",
                 "abandoned": 1, "abandoned_form": "`Old Show S02 (3/8)`",
                 "watched": 3, "total": 8, "cadence": "Mon", "premiere": "3/1",
                 "finale": "4/19", "bucket": "abandoned",
                 "started_airing": 1, "finished_airing": 0, "source": "calendar"},
            ],
            "distrakt_watch_state": [{"last_synced": "2026-03-20", "beacons_json": None}],
            "distrakt_show_progress": [
                {"trakt_id": 601, "season": 2, "watched_episodes_json": '{"1": ""}'},
            ],
            "distrakt_movie_watches": [
                {"trakt_id": 55, "watched_at": "2026-03-04T00:00:00Z",
                 "title": "Frozen Film", "year": 1999},
            ],
            "distrakt_prefs": [{"network_emojis_json": '{"HBO": ":film:"}',
                                "default_network_emoji": ":tv:", "updated_at": 1750000000}],
        }

    async def test_a_pre_rekey_backup_still_restores_its_roster(self):
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())

        march = await distrakt.load_month(self.user_id, "2026-03")
        self.assertTrue(march["closed"])
        row, = march["shows"]
        # Re-keyed by running the waterfall over the ids the row already held —
        # the same resolution the migration does to a live database.
        self.assertEqual((row["match_source"], row["match_id"]), ("tmdb", "9001"))
        self.assertEqual(row["ids"], {"trakt": 601, "tmdb": 9001, "slug": "old-show"})
        # and everything a frozen month renders from came back untouched
        self.assertEqual((row["watched"], row["total"], row["bucket"]),
                         (3, 8, "abandoned"))
        self.assertEqual(row["abandoned_form"], "`Old Show S02 (3/8)`")
        self.assertTrue(row["started_airing"])
        self.assertEqual([m["title"] for m in march["movies"]], ["Frozen Film"])

    async def test_the_provenance_column_is_read_under_its_old_name(self):
        """`source` became `added_by`. Losing it would make every restored row
        look hand-added, and removing one would stop marking the calendar."""
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())
        row, = (await distrakt.load_month(self.user_id, "2026-03"))["shows"]
        self.assertEqual(row["added_by"], distrakt.ADDED_BY_CALENDAR)

    async def test_the_emoji_map_survives_because_nothing_else_holds_it(self):
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())
        self.assertEqual(await distrakt.get_emoji_prefs(self.user_id),
                         ({"HBO": ":film:"}, ":tv:"))

    async def test_the_caches_are_left_to_refetch_rather_than_guessed_at(self):
        """Their rows named a title by Trakt's id and carry no shared id to re-key
        from. They are a cache of the provider's own answers, so they are dropped
        and `last_synced` cleared — the next sync re-seeds them, exactly as
        MIGRATION_18 does to a live database."""
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())
        state = await wh._load(self.user_id)
        self.assertEqual(state["shows"], {})
        self.assertEqual(state["movies"], {})
        self.assertIsNone(state["last_synced"])

    async def test_a_schema_1_document_restores_the_same_way(self):
        """Version 1 predates the emoji map, and saying nothing about it must not
        read as "delete it" — the rule that lets an old export land at all."""
        await distrakt.set_emoji_prefs(self.user_id, {"AMC": ":zombie:"}, ":tv:")
        doc = self._legacy_doc(schema=1)
        doc.pop("distrakt_prefs")
        await distrakt.restore_user_data(self.user_id, doc)
        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-03"])
        self.assertEqual(await distrakt.get_emoji_prefs(self.user_id),
                         ({"AMC": ":zombie:"}, ":tv:"))

    async def test_a_row_with_no_shared_id_refuses_the_whole_restore(self):
        """Nothing can tell it apart from another title of the same name, and a
        partial restore that silently dropped somebody's roster row would be
        worse than one that says what it cannot do."""
        doc = self._legacy_doc()
        doc["distrakt_shows"][0]["tmdb"] = None
        with self.assertRaises(distrakt.RestoreError) as caught:
            await distrakt.restore_user_data(self.user_id, doc)
        self.assertIn("Old Show", str(caught.exception))
        self.assertEqual(await distrakt.list_months(self.user_id), [])

    async def test_a_restored_legacy_row_can_then_be_addressed(self):
        """The point of the re-key: after the restore the row answers to the
        identity the page would name it by, so it can be abandoned or removed."""
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())
        key = ItemKey("show", "tmdb", "9001")
        self.assertTrue(await distrakt.remove_show(self.user_id, "2026-03", key, 2))
        self.assertEqual((await distrakt.load_month(self.user_id, "2026-03"))["shows"], [])

class MigrationEighteenTests(unittest.IsolatedAsyncioTestCase):
    """The one migration in this change that moves user data.

    Written against a database built at schema 17 and then migrated, because the
    thing under test is the SQL that carries rows across — a database created at
    18 has nothing to carry.
    """
    async def asyncSetUp(self):
        self.path = new_db_path("m18")
        await db.run(lambda conn: db_migrate_to(conn, 17))
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
            "created_at, updated_at) VALUES ('tracker', 1, 1, 1, ?, ?)", (now, now))
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _seed_v17(self, *, tmdb: int | None = 4242) -> None:
        await db.execute(
            "INSERT INTO distrakt_months (user_id, month, closed, created_at) "
            "VALUES (?, '2026-03', 1, ?)", (self.user_id, db.now()))
        await db.execute(
            "INSERT INTO distrakt_shows (user_id, month, trakt_id, tmdb, slug, media, title, "
            "season, network, watched, total, bucket, started_airing, finished_airing, source) "
            "VALUES (?, '2026-03', 601, ?, 'old', 'show', 'Old', 2, 'HBO', 3, 8, "
            "'keepup', 1, 0, 'calendar')",
            (self.user_id, tmdb))
        await db.execute(
            "INSERT INTO distrakt_show_progress (user_id, trakt_id, season, "
            "watched_episodes_json) VALUES (?, 601, 2, '{\"1\": \"\"}')", (self.user_id,))
        await db.execute(
            "INSERT INTO distrakt_movie_watches (user_id, trakt_id, watched_at, title, year) "
            "VALUES (?, 55, '2026-03-04T00:00:00Z', 'A Film', 1999)", (self.user_id,))
        await db.execute(
            "INSERT INTO distrakt_watch_state (user_id, last_synced) VALUES (?, '2026-03-20')",
            (self.user_id,))

    async def test_a_roster_row_is_carried_across_and_re_keyed(self):
        await self._seed_v17()
        self.assertEqual(await db.migrate(), 18)

        row, = (await distrakt.load_month(self.user_id, "2026-03"))["shows"]
        self.assertEqual((row["match_source"], row["match_id"]), ("tmdb", "4242"))
        self.assertEqual(row["ids"], {"trakt": 601, "tmdb": 4242, "slug": "old"})
        self.assertEqual((row["title"], row["season"], row["watched"], row["total"]),
                         ("Old", 2, 3, 8))
        # `source` is now `added_by`, with its value unchanged.
        self.assertEqual(row["added_by"], distrakt.ADDED_BY_CALENDAR)

    async def test_cached_progress_is_re_keyed_through_the_roster(self):
        """The progress table never recorded a shared id, so the only place to get
        one is the roster row for the same title."""
        await self._seed_v17()
        await db.migrate()
        state = await wh._load(self.user_id)
        self.assertEqual(wh.watched_map(state), {("show:tmdb:4242", 2): 1})

    async def test_cached_film_watches_are_dropped_and_re_seeded(self):
        """Nothing in the database has ever recorded a shared id for a film, so
        there is nothing to re-key from. Clearing `last_synced` with them is what
        makes the next sync fetch them again rather than start after them."""
        await self._seed_v17()
        await db.migrate()
        state = await wh._load(self.user_id)
        self.assertEqual(state["movies"], {})
        self.assertIsNone(state["last_synced"])

    async def test_a_row_with_no_shared_id_refuses_to_migrate(self):
        """Every read path keys on the triple afterwards, so a row that resolves
        to nothing would be unreachable. Refusing is the only option that neither
        destroys it nor invents an identity for it."""
        await self._seed_v17(tmdb=None)
        with self.assertRaises(RuntimeError) as caught:
            await db.migrate()
        self.assertIn("shared ids", str(caught.exception))
        # Nothing was changed: the roster is still there, in its old shape.
        self.assertEqual(await db.schema_version(), 17)
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM distrakt_shows"), 1)


def db_migrate_to(conn, version: int) -> int:
    """Apply migrations up to and including `version`, and no further.

    A test that needs a database as it stood at 17 cannot use db.migrate(), which
    always brings it fully up to date — which is exactly the state the migration
    under test has to start from.
    """
    kept = [step for step in db.MIGRATIONS if step[0] <= version]
    original, db.MIGRATIONS = db.MIGRATIONS, kept
    try:
        return db.migrate_sync(conn)
    finally:
        db.MIGRATIONS = original


if __name__ == "__main__":
    unittest.main()
