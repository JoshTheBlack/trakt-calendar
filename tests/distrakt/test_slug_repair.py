"""Recovering a per-service name a stored record was written without, and the log
that makes a future regression visible.

THE LOG IS THE FEATURE. A record missing `trakt_slug` still works — the link
builder falls back to the numeric id — so a writer that drops the name breaks
nothing and says nothing. These tests hold the two halves that change that: the
recovery only fires where a fresh read actually holds a name the record lacks,
and every recovery is written down with where it came from.
"""
from __future__ import annotations

import unittest

from app import db
from app.distrakt import slug_repair
from app.providers.base import ItemKey
from tests.support import DatabaseTestCase


class RecoverableNamesTests(unittest.TestCase):
    """What a fresh read is holding that a stored record is short of."""

    def test_a_name_the_read_has_and_the_record_lacks(self):
        self.assertEqual(
            slug_repair.recoverable_names(
                {"trakt": 1, "simkl": 2, "simkl_slug": "a"},
                {"trakt_slug": "b", "simkl_slug": "a"}),
            {"trakt_slug": "b"},
        )

    def test_a_complete_record_recovers_nothing(self):
        """So an ordinary read of a healthy record writes nothing and logs
        nothing — which is what keeps a row in the log meaningful."""
        self.assertEqual(
            slug_repair.recoverable_names({"trakt": 1, "trakt_slug": "b"},
                                          {"trakt_slug": "b"}),
            {},
        )

    def test_a_service_the_record_has_no_id_for_is_skipped(self):
        """A name is owed only where the row holds that service's id. Writing one
        anyway would make the record look linkable to a service that cannot be
        asked about it."""
        self.assertEqual(
            slug_repair.recoverable_names({"simkl": 2}, {"trakt_slug": "b"}), {})

    def test_an_empty_answer_recovers_nothing(self):
        self.assertEqual(
            slug_repair.recoverable_names({"trakt": 1}, {"trakt_slug": ""}), {})


class LearnAndLogTests(DatabaseTestCase):
    """End to end against the database: the name lands on every row of the title
    and the recovery is written down."""

    KEY = ItemKey("show", "tmdb", "134421")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = db.now()
        await db.execute(
            "INSERT INTO users (id, username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) "
            "VALUES (1, 'someone', 0, 1, 1, ?, ?)", (now, now))
        # A month record holding both services' ids and Simkl's name, and no
        # Trakt name — on two months, because a name is a fact about the title.
        for month in ("2026-07", "2026-08"):
            await db.execute(
                "INSERT INTO distrakt_month_records "
                "(user_id, month, kind, media, match_source, match_id, season, "
                " simkl_slug, trakt_id, simkl_id, title, created_at) "
                "VALUES (1, ?, 'premiere', 'show', 'tmdb', '134421', 1, "
                "'the-rookie', 134421, 555, 'The Rookie', ?)", (month, now))

    async def _learn(self, names):
        return await slug_repair.learn_and_log(
            1, self.KEY, names, slug_repair.EVIDENCE_DETAIL_LOOKUP, "The Rookie")

    async def test_the_name_lands_on_every_row_of_the_title(self):
        """One recovery, both months: `learn_ids` addresses the identity rather
        than a season."""
        self.assertEqual(await self._learn({"trakt_slug": "the-rookie-2018"}), 2)
        rows = await db.fetch_all(
            "SELECT trakt_slug FROM distrakt_month_records ORDER BY month")
        self.assertEqual([r["trakt_slug"] for r in rows],
                         ["the-rookie-2018", "the-rookie-2018"])

    async def test_the_recovery_is_written_down_with_its_evidence(self):
        await self._learn({"trakt_slug": "the-rookie-2018"})
        row, = await db.fetch_all("SELECT * FROM distrakt_slug_repairs")
        self.assertEqual(row["column_name"], "trakt_slug")
        self.assertEqual(row["value"], "the-rookie-2018")
        self.assertEqual(row["evidence"], slug_repair.EVIDENCE_DETAIL_LOOKUP)
        self.assertEqual(row["match_id"], "134421")
        self.assertEqual(row["rows_changed"], 2)
        self.assertEqual(row["title"], "The Rookie")
        self.assertTrue(row["repaired_at"])

    async def test_nothing_is_overwritten(self):
        """`learn_ids` fills blanks only, so a stored name a service actually
        stated always beats a later one."""
        self.assertEqual(await self._learn({"simkl_slug": "something-else"}), 0)
        rows = await db.fetch_all("SELECT DISTINCT simkl_slug FROM distrakt_month_records")
        self.assertEqual([r["simkl_slug"] for r in rows], ["the-rookie"])

    async def test_only_a_services_name_may_be_written(self):
        """A shared id or a bare `slug` is not a name this repair owns."""
        self.assertEqual(await self._learn({"slug": "x", "tmdb": "1"}), 0)
        rows = await db.fetch_all("SELECT COUNT(*) c FROM distrakt_slug_repairs")
        self.assertEqual(rows[0]["c"], 0)


class LogWriteTests(DatabaseTestCase):
    async def test_a_log_failure_never_reaches_the_caller(self):
        """The useful work is the name landing on the record; the log is how
        somebody finds out later that it was needed. Raising here would cost a
        viewer their modal to save a diagnostic."""
        await db.execute("DROP TABLE distrakt_slug_repairs")
        # Must not raise.
        await slug_repair.record(1, ItemKey("show", "tmdb", "1"),
                                 "trakt_slug", "x", slug_repair.EVIDENCE_DETAIL_LOOKUP, 1)


if __name__ == "__main__":
    unittest.main()
