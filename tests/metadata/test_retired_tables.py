"""app/metadata/tables.py — the tables the one store retired are named by nothing.

Migration 49 copied the per-service tables into one row per title, per season and
per episode and left them whole, so that a later release can check a production
copy against them before it drops them. A reader or writer still naming one would
read rows nothing writes any more, or write rows nothing reads — and look right
on any database converted a moment ago. So no module in app/ may name one: the
migrations that made and converted them (app/db.py) and the list that says why
each is kept (app/metadata/tables.py) are the only places they are spelled.
"""
from __future__ import annotations

import re
import sqlite3
import unittest
from pathlib import Path

from app import db
from app.metadata import tables
from tests.support import migrated_db

APP = Path(__file__).resolve().parents[2] / "app"

# The migrations made and converted them; tables.py names them to say why each
# is kept.
_ALLOWED = {APP / "db.py", APP / "metadata" / "tables.py"}


class NothingNamesARetiredTableTests(unittest.TestCase):

    def test_no_module_in_app_names_a_retired_table(self):
        named = re.compile(r"\b(" + "|".join(map(re.escape, tables.RETIRED)) + r")\b")
        found = []
        for path in sorted(APP.rglob("*.py")):
            if path in _ALLOWED:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if named.search(line):
                    found.append(f"{path.relative_to(APP.parent)}:{number}: {line.strip()}")
        self.assertEqual(found, [], (
            "these lines name a table the title store retired — read and write "
            "app/metadata/tables.py's TITLES, SEASONS, EPISODES and SERVICE_IDS instead"))

    def test_the_list_is_what_the_schema_kept(self):
        path = migrated_db("retired-tables")
        db.close_thread_connection()
        conn = sqlite3.connect(path)
        try:
            present = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        self.assertEqual(sorted(set(tables.RETIRED) - present), [])
        self.assertEqual(sorted({tables.TITLES, tables.SEASONS, tables.EPISODES,
                                 tables.SERVICE_IDS, tables.ALIASES} - present), [])


if __name__ == "__main__":
    unittest.main()
