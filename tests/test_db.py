"""Unit tests for the SQLite foundation (app/db).

Covers the migration runner applying cleanly from empty and being idempotent,
the connection pragmas (foreign key enforcement in particular is ASSERTED rather
than assumed — it is per-connection and defaults off, so every cascade in the
schema is inert without it), and the async helpers' transaction semantics.

No network. TRAKT_DATA_DIR points at a temp dir (set BEFORE importing app
modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_db -v
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-db-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])

EXPECTED_TABLES = {
    "users", "user_prefs", "linked_identities", "sessions", "login_attempts",
    "auth_handshakes", "invites", "invite_redemptions", "retired_identifiers",
    "app_meta", "schema_version",
    # Migration 2 — the calendar data model.
    "api_cache", "calendar_not_watching", "calendar_view_state",
    # Migration 3 — public share links.
    "share_links",
}


class DbTestCase(unittest.IsolatedAsyncioTestCase):
    """Each test gets its own database file so nothing leaks between them."""

    _counter = 0

    async def asyncSetUp(self):
        DbTestCase._counter += 1
        db.set_db_path(TMP / f"test-{DbTestCase._counter}.db")
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
        app_dir = Path(__file__).resolve().parent.parent / "app"
        offenders = sorted(
            path.name for path in app_dir.glob("*.py")
            if path.name != "db.py" and pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(offenders, [], f"modules importing sqlite3 directly: {offenders}")


if __name__ == "__main__":
    unittest.main()
