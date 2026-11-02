"""app/metadata/groups.py — a service's column group, spelled once.

`EMPTY` is what a reader is handed for a column a service's group does not have,
and it has to be exactly what that column would read in a group with nothing to
say — the schema's own default — or a service with no such column reads as
having said something. Checked here against the schema migration 49 built, for
every column every service declares.
"""
from __future__ import annotations

import sqlite3
import unittest

from app import db, providers
from app.metadata import groups, tables
from tests.support import migrated_db

_TABLES = (tables.TITLES, tables.SEASONS, tables.EPISODES)


class EmptyIsTheSchemasDefaultTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        path = migrated_db("groups-schema")
        db.close_thread_connection()
        cls.conn = sqlite3.connect(path)
        cls.defaults = {
            table: {row[1]: row[4] for row in cls.conn.execute(f"PRAGMA table_info({table})")}
            for table in _TABLES}

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def default(self, table: str, column: str):
        """What a fresh row reads in `column`: its declared default, evaluated."""
        spelled = self.defaults[table][column]
        return None if spelled is None else self.conn.execute(f"SELECT {spelled}").fetchone()[0]

    def test_every_declared_column_has_an_empty_value(self):
        missing = sorted({name for table in _TABLES for source in groups.sources()
                          for name in groups.names(table, source)} - set(groups.EMPTY))
        self.assertEqual(missing, [])

    def test_each_empty_value_is_what_the_column_reads_with_nothing_said(self):
        for table in _TABLES:
            for source in groups.sources():
                for name in groups.names(table, source):
                    with self.subTest(table=table, column=f"{source}_{name}"):
                        self.assertEqual(self.default(table, f"{source}_{name}"),
                                         groups.EMPTY[name])

    def test_every_registered_service_has_a_group(self):
        self.assertEqual(set(groups.sources()), {str(s) for s in providers.registered()})


class SpelledTests(unittest.TestCase):

    def test_held_and_given_become_the_services_own_columns(self):
        self.assertEqual(
            groups.spelled("CASE WHEN held.id IS given.id THEN given.title END", "titles", "tmdb"),
            "CASE WHEN titles.tmdb_id IS excluded.tmdb_id THEN excluded.tmdb_title END")

    def test_a_column_the_service_lacks_reads_as_its_empty_value(self):
        lacking = [(table, source, name) for table in _TABLES for source in groups.sources()
                   for name in groups.EMPTY if not groups.has(table, source, name)]
        self.assertTrue(lacking, "every service declares every column — nothing to check")
        table, source, name = lacking[0]
        self.assertEqual(groups.column(table, source, name, "t"), groups.literal(groups.EMPTY[name]))


if __name__ == "__main__":
    unittest.main()
