"""Migration 51: login_attempts admits 'passkey_ip', the passkey sign-in's own
handshake budget, and the rebuild carries every attempt already counted across —
a rebuild that forgot an in-progress lockout would be a way to clear one.

No network.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app import db
from tests.support import TMP


class Migration51Tests(unittest.TestCase):

    NAME = "migration-51.db"

    def setUp(self):
        path = TMP / self.NAME
        path.unlink(missing_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # the runner issues its own BEGIN IMMEDIATE
        with patch.object(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] <= 50]):
            db.migrate_sync(self.conn)
        self.conn.execute("INSERT INTO login_attempts (key_type, key_value, attempted_at, "
                          "succeeded) VALUES ('ip', '1.2.3.4', 5, 0), "
                          "('handshake_ip', '1.2.3.4', 6, 1)")
        self.version = db.migrate_sync(self.conn)

    def tearDown(self):
        self.conn.close()
        (TMP / self.NAME).unlink(missing_ok=True)

    def attempts(self) -> list[tuple]:
        return [tuple(row) for row in self.conn.execute(
            "SELECT id, key_type, key_value, attempted_at, succeeded FROM login_attempts "
            "ORDER BY id")]

    def test_the_attempts_already_counted_are_kept(self):
        self.assertEqual(self.version, 51)
        self.assertEqual(self.attempts(), [(1, "ip", "1.2.3.4", 5, 0),
                                           (2, "handshake_ip", "1.2.3.4", 6, 1)])

    def test_passkey_ip_is_admitted_and_an_unknown_type_is_not(self):
        self.conn.execute("INSERT INTO login_attempts (key_type, key_value, attempted_at, "
                          "succeeded) VALUES ('passkey_ip', '1.2.3.4', 7, 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO login_attempts (key_type, key_value, attempted_at, "
                              "succeeded) VALUES ('bogus_type', '1.2.3.4', 7, 1)")

    def test_the_lookup_index_is_rebuilt(self):
        indexes = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'login_attempts'")}
        self.assertIn("ix_login_attempts_lookup", indexes)

    def test_running_again_changes_nothing(self):
        before = self.attempts()
        self.assertEqual(db.migrate_sync(self.conn), 51)
        self.assertEqual(self.attempts(), before)
