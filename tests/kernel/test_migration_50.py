"""Migration 50: the passkeys table and handle, and the not-watching marks
spelled in a Simkl namespace the title store no longer keeps.

On a database brought to 49 by the migrations themselves, with titles whose
Simkl group still holds `traktslug` / `tvdbslug` / `mdlslug` as 49 copied them:
a mark only one of those names matches is rewritten to its title's key; one
naming two titles, one a kept spelling still matches, and one nothing names are
left alone; a mark whose title's key is already marked becomes that one mark.
Then the schema the passkeys feature reads, and a re-run that does nothing.

No network.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from app import db
from tests.support import TMP


def _connect(name: str) -> sqlite3.Connection:
    path = TMP / name
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # the runner issues its own BEGIN IMMEDIATE
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _title(conn, key: str, source: str, own: str, ids: dict, *, media: str = "show") -> None:
    conn.execute(f"INSERT INTO titles (title_key, media, ids_json, created_at, {source}_id, "
                 f"{source}_ids_json) VALUES (?, ?, '{{}}', 1, ?, ?)",
                 (key, media, own, json.dumps(ids)))


def _mark(conn, user_id: int, item_id: str, created_at: int = 7) -> None:
    conn.execute("INSERT INTO not_watching_shows (user_id, item_id, created_at) VALUES (?, ?, ?)",
                 (user_id, item_id, created_at))


class _Migrated(unittest.TestCase):

    NAME = "migration-50.db"
    UP_TO_50 = [m for m in db.MIGRATIONS if m[0] <= 50]

    @classmethod
    def setUpClass(cls):
        cls.conn = conn = _connect(cls.NAME)
        with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 49]):
            db.migrate_sync(conn)
        for user in (1, 2):
            conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                         "VALUES (?, ?, 1, 1)", (user, f"u{user}"))
        # Nocturne: Simkl names it `lazarus` in tvdb's namespace alone.
        _title(conn, "show:tmdb:292741", "simkl", "11",
               {"simkl": 11, "simkl_slug": "nocturne", "tmdb": 292741, "tvdbslug": "lazarus"})
        # Two titles Simkl gives one Trakt name.
        _title(conn, "show:tmdb:1", "simkl", "21", {"simkl": 21, "traktslug": "twins"})
        _title(conn, "show:tmdb:2", "simkl", "22", {"simkl": 22, "traktslug": "twins"})
        # A retired name that is also a name the store keeps for another title.
        _title(conn, "show:tmdb:3", "simkl", "31", {"simkl": 31, "mdlslug": "kept"})
        _title(conn, "show:tmdb:4", "trakt", "41", {"trakt": 41, "trakt_slug": "kept"})
        # One whose key the viewer has already marked as well.
        _title(conn, "show:tvdb:5", "simkl", "51", {"simkl": 51, "traktslug": "both"})
        _mark(conn, 1, "lazarus", 3)
        _mark(conn, 2, "lazarus", 4)
        _mark(conn, 1, "twins")
        _mark(conn, 1, "kept")
        _mark(conn, 1, "nobody-names-this")
        _mark(conn, 1, "both")
        _mark(conn, 1, "show:tvdb:5", 1)
        _mark(conn, 1, "show:tmdb:999")
        cls.before = cls.marks()
        with patch.object(db, "MIGRATIONS", cls.UP_TO_50):
            cls.version = db.migrate_sync(conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        (TMP / cls.NAME).unlink(missing_ok=True)

    @classmethod
    def marks(cls) -> set[tuple[int, str, int]]:
        return {tuple(row) for row in cls.conn.execute(
            "SELECT user_id, item_id, created_at FROM not_watching_shows")}


class TheMarksTests(_Migrated):

    def test_a_mark_one_retired_name_matches_takes_its_titles_key(self):
        marks = self.marks()
        self.assertIn((1, "show:tmdb:292741", 3), marks)
        self.assertIn((2, "show:tmdb:292741", 4), marks)
        self.assertNotIn("lazarus", {item for _user, item, _at in marks})

    def test_a_name_on_two_titles_a_kept_spelling_and_an_unknown_name_are_left_alone(self):
        marks = {(user, item) for user, item, _at in self.marks()}
        for item in ("twins", "kept", "nobody-names-this", "show:tmdb:999"):
            self.assertIn((1, item), marks)

    def test_a_title_already_marked_by_its_key_is_marked_once(self):
        marks = self.marks()
        self.assertIn((1, "show:tvdb:5", 1), marks)
        self.assertNotIn("both", {item for _user, item, _at in marks})

    def test_nothing_else_changed(self):
        rewritten = {(1, "lazarus", 3), (2, "lazarus", 4), (1, "both", 7)}
        self.assertEqual(self.before - self.marks(), rewritten)
        self.assertEqual(self.marks() - self.before,
                         {(1, "show:tmdb:292741", 3), (2, "show:tmdb:292741", 4)})


class ThePasskeySchemaTests(_Migrated):

    NAME = "migration-50-schema.db"

    def test_the_schema_is_at_50(self):
        self.assertEqual(self.version, 50)

    def test_the_passkeys_table_and_its_indexes(self):
        columns = [row["name"] for row in self.conn.execute("PRAGMA table_info(passkeys)")]
        self.assertEqual(columns, ["id", "user_id", "credential_id", "public_key", "sign_count",
                                   "transports", "name", "backed_up", "created_at",
                                   "last_used_at"])
        indexes = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"ix_passkeys_user", "ux_users_passkey_handle"} <= indexes)

    def test_every_account_starts_with_no_handle_and_a_handle_is_unique(self):
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE passkey_handle IS NOT NULL").fetchone()[0], 0)
        self.conn.execute("UPDATE users SET passkey_handle = 'h' WHERE id = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE users SET passkey_handle = 'h' WHERE id = 2")
        self.conn.execute("UPDATE users SET passkey_handle = NULL WHERE id = 1")

    def test_an_accounts_passkeys_go_with_it(self):
        self.conn.execute("INSERT INTO users (id, username, created_at, updated_at) "
                          "VALUES (9, 'gone', 1, 1)")
        self.conn.execute("INSERT INTO passkeys (user_id, credential_id, public_key, name, "
                          "created_at) VALUES (9, 'cred', x'00', 'Laptop', 1)")
        self.conn.execute("DELETE FROM users WHERE id = 9")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0], 0)

    def test_running_again_changes_nothing(self):
        before = self.marks()
        with patch.object(db, "MIGRATIONS", self.UP_TO_50):
            self.assertEqual(db.migrate_sync(self.conn), 50)
        self.assertEqual(self.marks(), before)
