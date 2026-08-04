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
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import auth, clock, db, distrakt
from app.providers.base import ItemKey
from app.distrakt import watch_history as wh
from app.config import Settings, save_settings
from app.main import app
from tests.distrakt.test_store import month_back
from tests.support import ORIGIN, migrated_db, new_db_path

async def _seed_dataset(user_id: int, *, tag: str) -> None:
    """A dataset touching every exported table, so a round trip has something to
    prove in each of them."""
    await distrakt.add_month_record(user_id, "2026-07", {
        "ids": {"trakt": 101, "tmdb": 555, "slug": f"slug-{tag}"}, "season": 1,
        "title": f"Show {tag}", "network": "Net", "media": "show",
        "kind": distrakt.RecordKind.SERIES_PREMIERE,
    })
    await distrakt.add_month_record(user_id, "2026-08", {
        "ids": {"trakt": 202, "tmdb": 606, "slug": f"other-{tag}"}, "season": 2,
        "title": "Second", "kind": distrakt.RecordKind.SEASON_PREMIERE,
    })
    # The viewer's own list, which belongs to no month at all.
    await distrakt.add_user_record(user_id, {
        "ids": {"trakt": 303, "tmdb": 707}, "season": 1, "title": f"Listed {tag}",
        "kind": distrakt.RecordKind.KEEPUP, "watched": 2, "total": 6,
    })
    await distrakt.dismiss_prompt(user_id, ItemKey("show", "tmdb", "808"), 3)
    # A frozen month, which is where the snapshot fields and movies_json matter.
    doc = await distrakt.load_month(user_id, "2026-07")
    doc["closed"] = True
    doc["totals_refreshed_at"] = db.now()
    doc["movies"] = [{"title": f"Film {tag}", "year": 2026, "watched_at": "2026-07-04T00:00:00Z"}]
    doc["shows"][0].update({
        "watched": 4, "total": 8, "cadence": "Tue", "premiere": "7/1",
        "finale": "7/29", "started_airing": True, "finished_airing": False,
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
        self.assertEqual((rec["watched"], rec["total"], rec["bucket"]), (4, 8, "new"))
        # the viewer's own list and their dismissals are not a month's, and come
        # back on their own
        listed, = await distrakt.user_records(self.user_id)
        self.assertEqual((listed["kind"], listed["watched"]), ("keepup", 2))
        self.assertEqual(await distrakt.dismissed_prompts(self.user_id),
                         {("show:tmdb:808", 3)})
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
            "SELECT user_id, COUNT(*) c FROM distrakt_month_records "
            "GROUP BY user_id ORDER BY user_id")
        self.assertEqual([(r["user_id"], r["c"]) for r in rows],
                         [(self.user_id, 2), (self.other_id, 2)])

    async def test_restore_replaces_rather_than_merges(self):
        await _seed_dataset(self.user_id, tag="mine")
        doc = await distrakt.export_user_data(self.user_id)
        # A month that is NOT in the document must be gone after the restore.
        await distrakt.add_month_record(self.user_id, "2026-09", {
            "ids": {"tmdb": 777}, "season": 1,
            "kind": distrakt.RecordKind.SERIES_PREMIERE})
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
        doc["distrakt_month_records"].append(
            {"month": "2026-07", "kind": "completed", "match_id": None, "season": None})

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

    async def _march(self, kind: str) -> dict:
        records = await distrakt.month_records(self.user_id, "2026-03", [kind])
        self.assertEqual(len(records), 1, f"expected exactly one {kind} record")
        return records[0]

    async def test_a_pre_rekey_backup_still_restores_its_roster(self):
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())

        march = await distrakt.load_month(self.user_id, "2026-03")
        self.assertTrue(march["closed"])
        row = await self._march("abandoned")
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

    async def test_a_row_that_premiered_and_was_settled_in_one_month_becomes_two(self):
        """Its 3/1 premiere falls in the month it sits on AND the month recorded a
        verdict on it. Two statements about March, not a duplicate — and season 2
        makes the premiere a returning one rather than a series premiere."""
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())
        self.assertEqual(
            {r["kind"] for r in await distrakt.month_records(self.user_id, "2026-03")},
            {"season_premiere", "abandoned"})
        # A premiere record carries no viewer progress, whatever the row said.
        self.assertEqual((await self._march("season_premiere"))["watched"], 0)

    async def test_a_row_still_in_progress_becomes_the_viewer_s_own_record(self):
        """It belongs to no month: the old row's month said only where the copy
        happened to sit."""
        doc = self._legacy_doc()
        doc["distrakt_shows"][0].update({"abandoned": 0, "bucket": "keepup",
                                         "premiere": "1/5", "finished_airing": 0})
        await distrakt.restore_user_data(self.user_id, doc)
        self.assertEqual(await distrakt.month_records(self.user_id, "2026-03"), [])
        listed, = await distrakt.user_records(self.user_id)
        self.assertEqual((listed["kind"], listed["watched"], listed["total"]),
                         ("keepup", 3, 8))

    async def test_the_provenance_column_is_read_under_its_old_name(self):
        """`source` became `added_by`. Losing it would make every restored record
        look hand-added, and removing one would stop marking the calendar."""
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())
        self.assertEqual((await self._march("abandoned"))["added_by"],
                         distrakt.ADDED_BY_CALENDAR)

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

    def _schema_4_doc(self) -> dict:
        """An export taken after the roster split but before the caches gained a
        source: rows already keyed on the shared identity, one flat sync cursor,
        and a flat beacon blob."""
        return {
            "schema": 4,
            "exported_at": 1750000000,
            "distrakt_watch_state": [
                {"last_synced": "2026-03-20",
                 "beacons_json": '{"ep_watched": "2026-03-20T00:00:00Z"}'},
            ],
            "distrakt_show_progress": [
                {"media": "show", "match_source": "tmdb", "match_id": "9001",
                 "season": 2, "watched_episodes_json": '{"1": "2026-03-04"}',
                 "trakt_id": 601, "simkl_id": None},
            ],
            "distrakt_movie_watches": [
                {"media": "movie", "match_source": "tmdb", "match_id": "55",
                 "watched_at": "2026-03-04T00:00:00Z", "title": "A Film",
                 "year": 1999, "trakt_id": 55, "simkl_id": None},
            ],
        }

    async def test_a_schema_4_backup_keeps_its_caches_and_names_their_source(self):
        """Unlike the pre-3 documents above, these rows ARE keyed on the shared
        identity and there is nothing to guess: every one of them came from the
        only service that has ever written them. So they restore intact, with that
        service named, rather than being dropped to re-fetch."""
        await distrakt.restore_user_data(self.user_id, self._schema_4_doc())
        state = await wh._load(self.user_id)
        self.assertEqual(wh.watched_map(state), {("show:tmdb:9001", 2): 1})
        self.assertIn("movie:tmdb:55", state["movies"])
        self.assertEqual(state["last_synced"], "2026-03-20")
        self.assertEqual(state["beacons"], {"ep_watched": "2026-03-20T00:00:00Z"})
        for table in ("distrakt_show_progress", "distrakt_movie_watches"):
            row = await db.fetch_one(
                f"SELECT source FROM {table} WHERE user_id = ?", (self.user_id,))
            self.assertEqual(row["source"], "trakt", table)

    async def test_a_schema_4_backup_keeps_its_month_records(self):
        """The roster split is not re-run on a document that already has its two
        record tables — doing so would overwrite them with the empty result of
        splitting a roster that is not there."""
        doc = self._schema_4_doc()
        doc["distrakt_months"] = [
            {"month": "2026-03", "closed": 1, "totals_refreshed_at": 1750000000,
             "movies_json": "[]", "created_at": 1740000000},
        ]
        doc["distrakt_month_records"] = [
            {"month": "2026-03", "kind": "abandoned", "media": "show",
             "match_source": "tmdb", "match_id": "9001", "season": 2,
             "trakt_id": 601, "tmdb": 9001, "title": "Old Show", "network": "HBO",
             "watched": 3, "total": 8, "started_airing": 1, "finished_airing": 0,
             "added_by": "calendar", "created_at": 1740000000},
        ]
        await distrakt.restore_user_data(self.user_id, doc)
        record = await self._march("abandoned")
        self.assertEqual((record["watched"], record["total"]), (3, 8))
        # The per-source breakdown is empty on a record written before it existed,
        # which reads as "nobody wrote one down" rather than as a claim.
        row = await db.fetch_one(
            "SELECT watched_by_source, total_by_source FROM distrakt_month_records "
            "WHERE user_id = ?", (self.user_id,))
        self.assertEqual((row["watched_by_source"], row["total_by_source"]), (None, None))

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
        """The point of the re-key: after the restore the records answer to the
        identity the page would name them by, so they can be removed."""
        await distrakt.restore_user_data(self.user_id, self._legacy_doc())
        key = ItemKey("show", "tmdb", "9001")
        self.assertEqual(
            await distrakt.remove_season_everywhere(self.user_id, key, 2), ["2026-03"])
        self.assertEqual((await distrakt.load_month(self.user_id, "2026-03"))["shows"], [])

class MigrationEighteenTests(unittest.IsolatedAsyncioTestCase):
    """The migration that re-keyed the tracker onto shared title ids.

    Written against a database built at schema 17 and then migrated to 18 exactly,
    because the thing under test is the SQL that carries rows across — a database
    created at 18 has nothing to carry, and one taken further has had those rows
    restructured again by 19.
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

    async def _to_18(self) -> int:
        return await db.run(lambda conn: db_migrate_to(conn, 18))

    async def _to_current(self) -> int:
        """Migration 18 and everything after it.

        The two tests below read the result back through watch_history's loader,
        which speaks the CURRENT schema — so stopping at 18 would test the loader
        against a database no running instance ever has. What is being asserted is
        still 18's work: nothing after it touches these rows' contents."""
        await self._to_18()
        return await db.migrate()

    async def test_a_roster_row_is_carried_across_and_re_keyed(self):
        await self._seed_v17()
        self.assertEqual(await self._to_18(), 18)

        row = await db.fetch_one(
            "SELECT * FROM distrakt_shows WHERE user_id = ?", (self.user_id,))
        self.assertEqual((row["match_source"], row["match_id"]), ("tmdb", "4242"))
        self.assertEqual((row["trakt_id"], row["tmdb"], row["slug"]), (601, 4242, "old"))
        self.assertEqual((row["title"], row["season"], row["watched"], row["total"]),
                         ("Old", 2, 3, 8))
        # `source` is now `added_by`, with its value unchanged.
        self.assertEqual(row["added_by"], distrakt.ADDED_BY_CALENDAR)

    async def test_cached_progress_is_re_keyed_through_the_roster(self):
        """The progress table never recorded a shared id, so the only place to get
        one is the roster row for the same title."""
        await self._seed_v17()
        await self._to_current()
        state = await wh._load(self.user_id)
        self.assertEqual(wh.watched_map(state), {("show:tmdb:4242", 2): 1})

    async def test_cached_film_watches_are_dropped_and_re_seeded(self):
        """Nothing in the database has ever recorded a shared id for a film, so
        there is nothing to re-key from. Clearing `last_synced` with them is what
        makes the next sync fetch them again rather than start after them."""
        await self._seed_v17()
        await self._to_current()
        state = await wh._load(self.user_id)
        self.assertEqual(state["movies"], {})
        self.assertIsNone(state["last_synced"])

    async def test_a_row_with_no_shared_id_refuses_to_migrate(self):
        """Every read path keys on the triple afterwards, so a row that resolves
        to nothing would be unreachable. Refusing is the only option that neither
        destroys it nor invents an identity for it."""
        await self._seed_v17(tmdb=None)
        with self.assertRaises(RuntimeError) as caught:
            await self._to_18()
        self.assertIn("shared ids", str(caught.exception))
        # Nothing was changed: the roster is still there, in its old shape.
        self.assertEqual(await db.schema_version(), 17)
        self.assertEqual(
            await db.fetch_value("SELECT COUNT(*) FROM distrakt_shows"), 1)


class MigrationNineteenTests(unittest.IsolatedAsyncioTestCase):
    """The split of the one roster table into month records and user records.

    Written against a database built at schema 18 and then migrated, because the
    thing under test is the SQL that classifies existing rows — a database created
    at 19 has nothing to classify.

    WHAT A MONTH IS DECIDES WHAT ITS ROWS BECOME, and the migration asks
    `distrakt_months.closed` rather than the calendar. Only a month that FROZE ever
    had premiere dates, buckets and counts written onto its rows, so only a frozen
    month can be classified at all; the rest are held for app/distrakt/unsettled.py
    to settle from a season lookup. `_row` therefore marks its month frozen unless
    the test says otherwise, because a row carrying a bucket and a premiere date
    IS a row from a month that froze — an unfrozen one carrying either is a state
    the old schema could not produce.

    Months are still spelled relative to today so a test cannot assert about a
    month that has since become the current one for some other reason. Nothing
    here depends on WHICH month that is, and one test below proves it.
    """
    async def asyncSetUp(self):
        new_db_path("m19")
        await db.run(lambda conn: db_migrate_to(conn, 18))
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
            "created_at, updated_at) VALUES ('tracker', 1, 1, 1, ?, ?)", (now, now))
        self.user_id = result.lastrowid
        self.older, self.newer = month_back(2), month_back(1)
        self.under_way, self.ahead = month_back(0), month_back(-1)

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _row(self, month: str, *, tmdb: int = 4242, season: int = 2,
                   title: str = "Old", premiere: str | None = None, bucket: str | None = None,
                   abandoned: int = 0, watched: int = 3, total: int = 8,
                   cadence: str | None = "Mon", finished_airing: int = 0) -> None:
        # The month is recorded as FROZEN unless the test already said otherwise —
        # DO NOTHING, so an explicit _month() before this one wins. A row carrying
        # counts, a cadence and a bucket is a row a freeze wrote, and a fixture
        # that made one on an open month would be asserting about a database the
        # old schema could not produce.
        await db.execute(
            "INSERT INTO distrakt_months (user_id, month, closed, created_at) "
            "VALUES (?, ?, 1, 0) ON CONFLICT(user_id, month) DO NOTHING",
            (self.user_id, month))
        await db.execute(
            "INSERT INTO distrakt_shows (user_id, month, media, match_source, match_id, "
            "trakt_id, tmdb, slug, title, season, network, abandoned, watched, total, "
            "cadence, premiere, bucket, started_airing, finished_airing, added_by) "
            "VALUES (?, ?, 'show', 'tmdb', ?, 601, ?, 'old', ?, ?, 'HBO', ?, ?, ?, ?, ?, ?, "
            "1, ?, 'calendar')",
            (self.user_id, month, str(tmdb), tmdb, title, season, abandoned, watched,
             total, cadence, premiere, bucket, finished_airing))

    async def _month(self, month: str, *, closed: bool) -> None:
        await db.execute(
            "INSERT INTO distrakt_months (user_id, month, closed, created_at) "
            "VALUES (?, ?, ?, 0) ON CONFLICT(user_id, month) DO UPDATE SET "
            "closed = excluded.closed",
            (self.user_id, month, int(closed)))

    async def _to_19(self) -> int:
        return await db.migrate()

    async def _kinds(self, month: str) -> set[str]:
        return {r["kind"] for r in await distrakt.month_records(self.user_id, month)}

    async def test_a_verdict_stays_on_the_month_that_reached_it(self):
        await self._row(self.newer, bucket="completed", premiere=None)
        self.assertGreaterEqual(await self._to_19(), 19)
        self.assertEqual(await self._kinds(self.newer), {"completed"})
        self.assertEqual(await distrakt.user_records(self.user_id), [])

    async def test_the_abandoned_flag_counts_as_a_verdict_on_its_own(self):
        """The flag is what the viewer pressed and the bucket is what the month
        wrote down when it froze; a row carrying one without the other is still a
        row about giving something up."""
        await self._row(self.newer, abandoned=1, bucket=None)
        await self._to_19()
        self.assertEqual(await self._kinds(self.newer), {"abandoned"})

    async def test_a_row_that_premiered_in_its_month_becomes_a_premiere_record(self):
        month_number = distrakt.parse_month_key(self.newer)[1]
        await self._row(self.newer, season=1, premiere=f"{month_number}/12")
        await self._to_19()
        self.assertEqual(await self._kinds(self.newer), {"series_premiere"})
        # A premiere record carries no viewer progress.
        record, = await distrakt.month_records(self.user_id, self.newer)
        self.assertEqual(record["watched"], 0)
        self.assertEqual(record["total"], 8)
        # ...and the season is still in progress, so it is on the viewer's list too
        listed, = await distrakt.user_records(self.user_id)
        self.assertEqual((listed["kind"], listed["watched"]), ("keepup", 3))

    async def test_a_later_season_premiere_is_a_returning_one(self):
        month_number = distrakt.parse_month_key(self.newer)[1]
        await self._row(self.newer, season=3, premiere=f"{month_number}/12")
        await self._to_19()
        self.assertEqual(await self._kinds(self.newer), {"season_premiere"})

    async def test_a_row_that_premiered_elsewhere_produces_no_premiere_record(self):
        """Its premiere date names a different month, so this month announced
        nothing about it."""
        other = (distrakt.parse_month_key(self.newer)[1] % 12) + 1
        await self._row(self.newer, premiere=f"{other}/12")
        await self._to_19()
        self.assertEqual(await self._kinds(self.newer), set())

    async def test_premiered_and_settled_in_one_month_becomes_two_records(self):
        month_number = distrakt.parse_month_key(self.newer)[1]
        await self._row(self.newer, season=1, premiere=f"{month_number}/2",
                        bucket="completed", watched=8)
        await self._to_19()
        self.assertEqual(await self._kinds(self.newer), {"series_premiere", "completed"})
        self.assertEqual((await distrakt.month_records(
            self.user_id, self.newer, ["completed"]))[0]["watched"], 8)

    async def test_the_copies_of_a_carried_season_collapse_to_one_user_record(self):
        """The whole point of the split: a season carried onto three months had
        three rows saying the same thing about the viewer, and the most recent one
        carries the most recent counts."""
        await self._row(month_back(3), watched=1)
        await self._row(self.older, watched=4)
        await self._row(self.newer, watched=7)
        await self._to_19()
        listed, = await distrakt.user_records(self.user_id)
        self.assertEqual(listed["watched"], 7)

    async def test_a_season_settled_anywhere_is_off_the_viewer_s_list(self):
        """Giving up on a season in one month is a statement about the season, so
        an older in-progress copy must not resurrect it."""
        await self._row(self.older, watched=1)
        await self._row(self.newer, abandoned=1)
        await self._to_19()
        self.assertEqual(await distrakt.user_records(self.user_id), [])
        self.assertEqual(await self._kinds(self.newer), {"abandoned"})

    async def test_an_unfrozen_months_rows_are_held_rather_than_guessed_at(self):
        """The bug this guards, and it cost a month. A month that never froze wrote
        no premiere date onto any of its rows, so nothing stored says which of them
        it ANNOUNCED and which were seasons the viewer had in hand. Read as the
        latter, a pre-filled month ahead is exactly where a title turned away on
        the calendar sits — and the very same mark that means "never put this in
        that month" then reads as "I was following this and stopped" and lands as a
        verdict on the month under way. So neither is guessed: the rows are held,
        with their month, until a season lookup can say."""
        await self._month(self.ahead, closed=False)
        await self._row(self.ahead, season=1, premiere=None)
        await self._row(self.ahead, tmdb=7373, season=4, premiere=None)
        await self._to_19()
        self.assertEqual(await self._kinds(self.ahead), set(),
                         "a month that never froze announced something anyway")
        self.assertEqual(await distrakt.user_records(self.user_id), [],
                         "a held row became work in hand and can be abandoned")
        held = await db.fetch_all(
            "SELECT month, season FROM distrakt_unsettled_rows WHERE user_id = ? "
            "ORDER BY season", (self.user_id,))
        self.assertEqual([(r["month"], r["season"]) for r in held],
                         [(self.ahead, 1), (self.ahead, 4)])

    async def test_an_unfrozen_month_never_wins_the_latest_month_race(self):
        """A season the viewer is part-way through, also sitting on a month that
        never froze, must take its counts from the month that actually recorded
        some — an unfrozen month's row reads 0 of 0, because a freeze is the only
        thing that ever wrote those columns."""
        await self._month(self.ahead, closed=False)
        await self._row(self.newer, watched=5)
        await self._row(self.ahead, watched=0, total=0, premiere=None)
        await self._to_19()
        listed, = await distrakt.user_records(self.user_id)
        self.assertEqual(listed["watched"], 5)

    async def test_the_same_database_migrates_the_same_way_on_any_day(self):
        """The property whose absence made this migration destroy a month. It used
        to split rows on whether their month was still ahead of TODAY, so the same
        database migrated differently depending on when it was run — which is why
        it passed against a development store whose months had all frozen and lost
        a month in production, where nobody had opened the app since before it
        ended. Nothing here reads the clock, so there is no such day."""
        await self._month(self.ahead, closed=False)
        await self._row(self.older, bucket="completed")
        await self._row(self.newer, tmdb=7373, watched=5)
        await self._row(self.ahead, tmdb=8484, premiere=None)

        def snapshot(conn):
            return [tuple(r) for table in
                    ("SELECT month, kind, match_id, season FROM distrakt_month_records "
                     "ORDER BY month, kind, match_id",
                     "SELECT match_id, season, kind, watched FROM distrakt_user_seasons "
                     "ORDER BY match_id",
                     "SELECT month, match_id, season FROM distrakt_unsettled_rows "
                     "ORDER BY month, match_id")
                    for r in conn.execute(table)]

        with clock_fixed_at("2026-03-15"):
            await self._to_19()
            in_march = await db.run(snapshot)
        # The same rows, migrated again from scratch nine months later.
        new_db_path("m19-again")
        await db.run(lambda conn: db_migrate_to(conn, 18))
        await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES ('tracker', 1, 1, 1, 0, 0)")
        await self._month(self.ahead, closed=False)
        await self._row(self.older, bucket="completed")
        await self._row(self.newer, tmdb=7373, watched=5)
        await self._row(self.ahead, tmdb=8484, premiere=None)
        with clock_fixed_at("2026-12-15"):
            await self._to_19()
            in_december = await db.run(snapshot)
        self.assertEqual(in_march, in_december)

    async def test_a_season_that_has_finished_airing_is_one_to_catch_up_on(self):
        await self._row(self.newer, finished_airing=1)
        await self._to_19()
        self.assertEqual((await distrakt.user_records(self.user_id))[0]["kind"], "catchup")

    async def test_a_season_that_drops_all_at_once_is_too(self):
        await self._row(self.newer, cadence="b")
        await self._to_19()
        self.assertEqual((await distrakt.user_records(self.user_id))[0]["kind"], "catchup")

    async def test_a_month_that_never_froze_keeps_its_rows_and_says_they_are_held(self):
        """A month still open has no premiere date on any of its rows, so nothing
        can prove which of them it announced. The rows are kept exactly as they
        are — WITH THEIR MONTH, which is the one thing nothing else records and the
        one thing a later pass cannot re-derive — and the months are named in the
        log, because an operator looking at a month short of its titles in the
        seconds before the first load drains it should not have to work it out."""
        await self._month(self.newer, closed=False)
        await self._row(self.newer, premiere=None, cadence=None, watched=0, total=0)
        with self.assertLogs("app.db", level="INFO") as caught:
            await self._to_19()
        self.assertEqual(await self._kinds(self.newer), set())
        self.assertEqual(await distrakt.user_records(self.user_id), [])
        held, = await db.fetch_all(
            "SELECT month, title, season FROM distrakt_unsettled_rows WHERE user_id = ?",
            (self.user_id,))
        self.assertEqual((held["month"], held["title"], held["season"]),
                         (self.newer, "Old", 2))
        said = "\n".join(caught.output)
        self.assertIn(self.newer, said)
        self.assertIn("had not frozen", said)

    async def test_a_frozen_month_holds_nothing_back(self):
        """It stored its premiere dates when it froze, so it had everything the
        classification needed and nothing is left for a later pass."""
        month_number = distrakt.parse_month_key(self.older)[1]
        await self._month(self.older, closed=True)
        await self._row(self.older, season=1, premiere=f"{month_number}/12")
        with self.assertLogs("app.db", level="INFO") as caught:
            await self._to_19()
        self.assertEqual(await self._kinds(self.older), {"series_premiere"})
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM distrakt_unsettled_rows"), 0)
        self.assertNotIn("had not frozen", "\n".join(caught.output))

    async def test_a_verdict_on_an_unfrozen_month_is_still_a_verdict(self):
        """The `abandoned` flag was set the moment the viewer pressed the button,
        on whatever month was open at the time — not at a freeze, like `bucket`.
        Giving up is the one thing an unfrozen month can prove it decided, so it is
        honoured rather than held."""
        await self._month(self.newer, closed=False)
        await self._row(self.newer, abandoned=1, bucket=None, premiere=None)
        await self._to_19()
        self.assertEqual(await self._kinds(self.newer), {"abandoned"})
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM distrakt_unsettled_rows"), 0,
            "a season already settled was also held for settling")

    async def test_an_instance_already_past_the_split_gets_the_held_rows_table(self):
        """The first version of the split dropped the roster table without keeping
        anything, so an instance that applied it has no held rows and never will.
        It still needs the TABLE: the drain pass reads it on every tracker load,
        and a missing table is an error rather than an empty answer."""
        await self._row(self.older, bucket="completed")
        await db.run(lambda conn: db_migrate_to(conn, 19))
        await db.run(lambda conn: conn.execute("DROP TABLE distrakt_unsettled_rows"))
        await db.migrate()
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM distrakt_unsettled_rows"), 0)
        # ...and it is not created a second time for an instance that migrated
        # after the correction and already has it, held rows and all.
        await db.migrate()
        self.assertEqual(await self._kinds(self.older), {"completed"})

    async def test_a_row_that_cannot_be_addressed_refuses_to_migrate(self):
        """A record is filed under its identity and, on a month, under its month
        key. A row missing either would land where nothing could reach it."""
        await db.execute(
            "UPDATE distrakt_shows SET month = 'nonsense' WHERE user_id = ?", (self.user_id,))
        await self._row(self.newer)
        await db.execute(
            "UPDATE distrakt_shows SET match_id = '' WHERE user_id = ?", (self.user_id,))
        with self.assertRaises(RuntimeError) as caught:
            await self._to_19()
        self.assertIn("shared id", str(caught.exception))
        # Nothing was changed: the rows are still there, in their old shape.
        self.assertEqual(await db.schema_version(), 18)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM distrakt_shows"), 1)


def clock_fixed_at(iso: str):
    """Run the block believing today is `iso`.

    Only there to prove the migration does NOT care what day it is, so it moves
    the one seam that could make it care and nothing else."""
    return mock.patch.dict(os.environ, {clock.FAKE_TODAY_ENV: iso})


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
