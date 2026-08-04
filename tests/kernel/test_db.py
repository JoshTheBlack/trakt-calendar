"""Unit tests for the SQLite foundation (app/db).

Covers the migration runner applying cleanly from empty and being idempotent,
the connection pragmas (foreign key enforcement in particular is ASSERTED rather
than assumed — it is per-connection and defaults off, so every cascade in the
schema is inert without it), and the async helpers' transaction semantics.

No network.
"""
from __future__ import annotations

import re
import unittest

from app import db
from tests.support import APP_DIR, TMP, new_db_path

EXPECTED_TABLES = {
    "users", "user_prefs", "linked_identities", "sessions", "login_attempts",
    "auth_handshakes", "invites", "invite_redemptions", "retired_identifiers",
    "app_meta", "schema_version",
    # Migration 2 — the calendar data model. (calendar_not_watching was folded
    # into not_watching_shows by migration 10 and dropped.)
    "api_cache", "calendar_view_state",
    # Migration 3 — public share links.
    "share_links",
    # Migration 4 — the per-user distrakt tracker data model. (distrakt_shows was
    # split into the two record tables below by migration 19 and dropped.)
    "distrakt_months", "distrakt_watch_state",
    "distrakt_show_progress", "distrakt_movie_watches",
    # Migration 9 / 10 — per-user emoji map, and show-level not-watching.
    "distrakt_prefs", "not_watching_shows",
    # Migration 11 — configuration consolidated out of settings.json.
    "app_secrets", "app_settings",
    # Migration 19 — month facts and viewer facts, stored apart.
    "distrakt_month_records", "distrakt_user_seasons", "distrakt_prompt_dismissals",
}


class DbTestCase(unittest.IsolatedAsyncioTestCase):
    """Each test gets its own database file so nothing leaks between them."""
    async def asyncSetUp(self):
        new_db_path("test")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()


class MigrationTests(DbTestCase):
    async def test_applies_cleanly_from_empty(self):
        names = {r["name"] for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue(EXPECTED_TABLES <= names, f"missing: {EXPECTED_TABLES - names}")
        self.assertEqual(await db.schema_version(), max(v for v, _ in db.MIGRATIONS))

    async def test_is_idempotent(self):
        before = await db.schema_version()
        self.assertEqual(await db.migrate(), before)
        self.assertEqual(await db.migrate(), before)
        # Re-running must not have duplicated the single version row.
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM schema_version"), 1)

    async def test_migration_12_adds_certification_columns_to_user_prefs(self):
        """A fresh install gets the columns via CREATE TABLE; an upgrading one
        gets them via the ALTER TABLE migration. Both must default to '' so an
        existing row with no opinion on certification keeps behaving as
        unfiltered, the same way genres/countries already do."""
        now = db.now()
        await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('cert-user', ?, ?)",
            (now, now))
        user_id = await db.fetch_value("SELECT id FROM users WHERE username = 'cert-user'")
        await db.execute(
            "INSERT INTO user_prefs (user_id, endpoint, card_style, day_packing) "
            "VALUES (?, 'shows/new', 'vertical', 'stacked')", (user_id,))
        row = await db.fetch_one(
            "SELECT show_certifications, movie_certifications FROM user_prefs WHERE user_id = ?",
            (user_id,))
        self.assertEqual(row["show_certifications"], "")
        self.assertEqual(row["movie_certifications"], "")

    async def test_only_one_bootstrap_account_can_exist(self):
        """The database half of the first-run race guard."""
        now = db.now()
        await db.execute(
            "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
            "VALUES ('one', 1, ?, ?)", (now, now))
        with self.assertRaises(db.IntegrityError):
            await db.execute(
                "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
                "VALUES ('two', 1, ?, ?)", (now, now))
        # The index is partial: ordinary accounts are unconstrained.
        await db.execute(
            "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
            "VALUES ('three', 0, ?, ?)", (now, now))
        await db.execute(
            "INSERT INTO users (username, is_bootstrap, created_at, updated_at) "
            "VALUES ('four', 0, ?, ?)", (now, now))

    async def test_migration_10_folds_per_view_marks_into_one_per_show(self):
        """An upgrading instance keeps every not-watching mark it had, and the
        same show marked in three different views becomes one row carrying the
        EARLIEST of the three timestamps — the moment the user first said it."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "fold-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # the runner issues its own BEGIN IMMEDIATE
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 9]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'operator', ?, ?)", (now, now))
            for endpoint, month, item, ts in (
                ("shows/new", 7, "the-show", now + 50),
                ("shows", 7, "the-show", now + 10),
                ("shows/premieres", 8, "the-show", now + 30),
                ("shows/new", 7, "other-show", now),
            ):
                conn.execute(
                    "INSERT INTO calendar_not_watching "
                    "(user_id, endpoint, year, month, item_id, created_at) "
                    "VALUES (1, ?, 2026, ?, ?, ?)", (endpoint, month, item, ts))
            conn.commit()

            db.migrate_sync(conn)

            rows = {r["item_id"]: r["created_at"] for r in
                    conn.execute("SELECT item_id, created_at FROM not_watching_shows")}
            self.assertEqual(rows, {"the-show": now + 10, "other-show": now})
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                             "AND name = 'calendar_not_watching'").fetchone()[0], 0)
        finally:
            conn.close()

    async def test_username_is_case_insensitive(self):
        """Without NOCASE, `Admin` and `admin` would be two separate accounts."""
        now = db.now()
        await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('admin', ?, ?)",
            (now, now))
        with self.assertRaises(db.IntegrityError):
            await db.execute(
                "INSERT INTO users (username, created_at, updated_at) VALUES ('ADMIN', ?, ?)",
                (now, now))

    async def test_migration_14_widens_key_type_and_keeps_existing_rows(self):
        """The rebuild that adds 'ranker_search'/'ranker_export' must carry an
        in-progress lockout across, the same guarantee migration 7 made for
        'handshake_ip' — a rebuild that silently forgot one would be a way to
        clear it."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-14-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 13]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                "VALUES ('ip', '1.2.3.4', ?, 0)", (now,))
            conn.commit()

            db.migrate_sync(conn)

            rows = conn.execute(
                "SELECT key_type, key_value, attempted_at FROM login_attempts").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["key_type"], "ip")
            self.assertEqual(rows[0]["key_value"], "1.2.3.4")
            self.assertEqual(rows[0]["attempted_at"], now)

            conn.execute(
                "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                "VALUES ('ranker_search', '7', ?, 1)", (now,))
            conn.execute(
                "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                "VALUES ('ranker_export', '7', ?, 1)", (now,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) "
                    "VALUES ('bogus_type', '7', ?, 1)", (now,))
        finally:
            conn.close()

    async def test_migration_21_opens_the_provider_column_and_keeps_every_row(self):
        """Both provider tables carried `CHECK (provider IN ('plex','trakt'))`,
        which made admitting a third service a table rebuild.

        Two things have to hold at once. The name set must genuinely be open
        afterwards — that is the whole point of the rebuild — and every existing
        row must come across, because a linked identity is how somebody signs in
        and an account created through a provider has no password to fall back
        on. `purpose` stays constrained: that set is this app's own and really
        is closed.
        """
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-21-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 20]):
                db.migrate_sync(conn)
            now = db.now()
            conn.execute(
                "INSERT INTO users (id, username, created_at, updated_at) "
                "VALUES (1, 'viewer', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                "display_name, access_token, created_at, last_login_at) "
                "VALUES (1, 'trakt', 'uuid-1', 'Viewer', 'tok', ?, ?)", (now, now))
            conn.execute(
                "INSERT INTO auth_handshakes (state, provider, purpose, created_at, expires_at) "
                "VALUES ('st-1', 'plex', 'login', ?, ?)", (now, now + 600))
            # The constraint this migration exists to remove, still in force.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                    "created_at) VALUES (1, 'simkl', 'acct-9', ?)", (now,))
            conn.commit()

            db.migrate_sync(conn)

            identity = conn.execute("SELECT * FROM linked_identities").fetchall()
            self.assertEqual(len(identity), 1)
            self.assertEqual(identity[0]["provider"], "trakt")
            self.assertEqual(identity[0]["provider_user_id"], "uuid-1")
            self.assertEqual(identity[0]["display_name"], "Viewer")
            self.assertEqual(identity[0]["access_token"], "tok")
            self.assertEqual(identity[0]["created_at"], now)
            handshake = conn.execute("SELECT * FROM auth_handshakes").fetchall()
            self.assertEqual(len(handshake), 1)
            self.assertEqual(handshake[0]["state"], "st-1")
            self.assertEqual(handshake[0]["provider"], "plex")

            # A third service now writes to both tables with no schema change.
            conn.execute(
                "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                "created_at) VALUES (1, 'simkl', 'acct-9', ?)", (now,))
            conn.execute(
                "INSERT INTO auth_handshakes (state, provider, purpose, created_at, expires_at) "
                "VALUES ('st-2', 'simkl', 'link', ?, ?)", (now, now + 600))
            # ...but the identity is still unique per (provider, account).
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO linked_identities (user_id, provider, provider_user_id, "
                    "created_at) VALUES (1, 'simkl', 'acct-9', ?)", (now,))
            # ...and `purpose` is still a closed set, because that one is ours.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO auth_handshakes (state, provider, purpose, created_at, "
                    "expires_at) VALUES ('st-3', 'simkl', 'sideways', ?, ?)", (now, now + 600))

            # The rebuild drops the old table's indexes with it. A missing one
            # here is invisible until every account page full-scans.
            indexes = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
            self.assertIn("ix_linked_identities_user", indexes)
            self.assertIn("ix_auth_handshakes_expires", indexes)
        finally:
            conn.close()

    async def test_migration_21_refuses_rather_than_dropping_an_orphaned_identity(self):
        """An identity row whose account is gone cannot be carried across, and
        the migration says which rows and why instead of failing on a bare
        foreign key error — or, far worse, quietly copying fewer rows than it
        found. Nothing is changed when it refuses."""
        import sqlite3

        from unittest.mock import patch

        path = TMP / "migration-21-orphan-test.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 20]):
                db.migrate_sync(conn)
            now = db.now()
            # Written with the enforcement off, which is how a hand-repaired or
            # partially restored database acquires one in the first place.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO linked_identities (user_id, provider, provider_user_id, created_at) "
                "VALUES (404, 'trakt', 'uuid-orphan', ?)", (now,))
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            with self.assertRaises(RuntimeError) as caught:
                db.migrate_sync(conn)
            self.assertIn("no longer exists", str(caught.exception))

            # Rolled back whole: the row is still there and so is the old version.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM linked_identities").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], 20)
        finally:
            conn.close()


class PragmaTests(DbTestCase):
    async def test_foreign_keys_are_actually_on(self):
        """Asserted, not assumed: the setting is per-connection and defaults off,
        and every ON DELETE CASCADE in the schema depends on it."""
        self.assertEqual(await db.fetch_value("PRAGMA foreign_keys"), 1)

    async def test_foreign_keys_are_enforced_and_cascade(self):
        now = db.now()
        with self.assertRaises(db.IntegrityError):
            await db.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, "
                "absolute_expires_at, last_seen_at) VALUES ('x', 9999, ?, ?, ?, ?)",
                (now, now + 60, now + 60, now))

        user_id = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('u', ?, ?)",
            (now, now))).lastrowid
        await db.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at, "
            "absolute_expires_at, last_seen_at) VALUES ('s1', ?, ?, ?, ?, ?)",
            (user_id, now, now + 60, now + 60, now))
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM sessions"), 0)

    async def test_connection_policy(self):
        self.assertEqual(str(await db.fetch_value("PRAGMA journal_mode")).lower(), "wal")
        self.assertEqual(await db.fetch_value("PRAGMA busy_timeout"), db.BUSY_TIMEOUT_MS)
        self.assertEqual(await db.fetch_value("PRAGMA synchronous"), 1)  # NORMAL

    async def test_every_thread_gets_the_pragmas(self):
        """The helpers run on a worker thread pool and each thread opens its own
        connection, so a fresh thread must come up with foreign keys on too.
        That is why the pragmas live in the connection factory rather than in a
        migration."""
        results = []
        for _ in range(8):
            results.append(await db.run(
                lambda conn: (conn.execute("PRAGMA foreign_keys").fetchone()[0],
                              conn.execute("PRAGMA journal_mode").fetchone()[0])))
        self.assertTrue(all(fk == 1 and str(jm).lower() == "wal" for fk, jm in results))


class HelperTests(DbTestCase):
    async def test_execute_reports_lastrowid_and_rowcount(self):
        now = db.now()
        inserted = await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('a', ?, ?)",
            (now, now))
        self.assertIsNotNone(inserted.lastrowid)
        updated = await db.execute("UPDATE users SET timezone = 'UTC' WHERE id = ?",
                                   (inserted.lastrowid,))
        self.assertEqual(updated.rowcount, 1)

    async def test_fetch_one_and_all(self):
        now = db.now()
        await db.executemany(
            "INSERT INTO users (username, created_at, updated_at) VALUES (?, ?, ?)",
            [("a", now, now), ("b", now, now)])
        self.assertEqual(len(await db.fetch_all("SELECT * FROM users")), 2)
        row = await db.fetch_one("SELECT * FROM users WHERE username = 'b'")
        self.assertEqual(row["username"], "b")
        self.assertIsNone(await db.fetch_one("SELECT * FROM users WHERE username = 'zz'"))

    async def test_transaction_rolls_back_on_error(self):
        now = db.now()

        def _boom(conn):
            conn.execute("INSERT INTO users (username, created_at, updated_at) "
                         "VALUES ('kept?', ?, ?)", (now, now))
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            await db.transaction(_boom)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM users"), 0)

    async def test_transaction_commits(self):
        now = db.now()

        def _work(conn):
            conn.execute("INSERT INTO users (username, created_at, updated_at) "
                         "VALUES ('x', ?, ?)", (now, now))
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        user_id = await db.transaction(_work)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM users"), 1)
        self.assertEqual(
            await db.fetch_value("SELECT id FROM users WHERE username = 'x'"), user_id)

    async def test_app_meta_roundtrip_and_upsert(self):
        self.assertIsNone(await db.get_meta("plex_client_id"))
        self.assertEqual(await db.get_meta("plex_client_id", "fallback"), "fallback")
        await db.set_meta("plex_client_id", "abc")
        await db.set_meta("plex_client_id", "def")
        self.assertEqual(await db.get_meta("plex_client_id"), "def")


class DriverIsolationTests(unittest.TestCase):
    def test_only_the_db_module_imports_sqlite3(self):
        """app/db.py is the only module allowed to touch the driver directly.

        Everything else goes through its async helpers, which push the blocking
        call onto a worker thread — an `import sqlite3` anywhere else is how a
        blocking query ends up stalling the event loop from inside a route.
        """
        pattern = re.compile(r"^\s*(import sqlite3|from sqlite3 import)", re.MULTILINE)
        # Walked, not globbed: app/ holds packages now (auth, distrakt,
        # providers), and a top-level glob stopped seeing most of the app the
        # moment the first one was created.
        offenders = sorted(
            str(path.relative_to(APP_DIR)) for path in APP_DIR.rglob("*.py")
            if path.name != "db.py" and pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(offenders, [], f"modules importing sqlite3 directly: {offenders}")


if __name__ == "__main__":
    unittest.main()
