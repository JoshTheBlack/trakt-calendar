"""Recovering a per-service name a stored record was written without, and the log
that makes a future regression visible.

THE LOG IS THE FEATURE. A record missing `trakt_slug` still works — the link
builder falls back to the numeric id — so a writer that drops the name breaks
nothing and says nothing. These tests hold the two halves that change that: the
recovery only fires where the name can be PROVEN, and every recovery is written
down with where it came from.
"""
from __future__ import annotations

import unittest
from unittest import mock

from app import db
from app.calendar import detail_source
from app.distrakt import slug_repair
from app.providers.base import ItemKey
from tests.support import DatabaseTestCase


class AttributionTests(unittest.TestCase):
    """The rule for reading the OLD shared `slug` column, which is unattributed
    by construction: both services call the field `slug` and disagree about it,
    so whichever synced last won."""

    def test_a_value_that_is_not_the_stored_simkl_name_is_trakts(self):
        """The column had exactly two writers. If the row already knows what
        Simkl calls the title and the shared value is something else, Simkl did
        not write it — measured against the live Trakt API over every affected
        title on a real instance, 22 of 22, no mismatches."""
        self.assertEqual(
            slug_repair.attribute_shared_slug({
                "slug": "the-rookie-2018", "trakt_slug": None,
                "simkl_slug": "the-rookie", "trakt_id": 134421, "simkl_id": 1,
            }),
            {"trakt_slug": "the-rookie-2018"},
        )

    def test_it_refuses_to_guess_with_no_simkl_name_to_differ_from(self):
        """THE IMPORTANT ONE. A row holding no Simkl name has nothing to prove
        the shared value is not Simkl's, so there is no evidence — and a WRONG
        name is strictly worse than none, because none falls back to an id that
        works while a wrong one builds a link to a title the service never had."""
        self.assertEqual(
            slug_repair.attribute_shared_slug({
                "slug": "house-of-the-dragon", "trakt_slug": None,
                "simkl_slug": None, "trakt_id": 154574, "simkl_id": None,
            }),
            {},
        )

    def test_an_identical_value_proves_nothing(self):
        """Equal to the Simkl name means it may simply BE the Simkl name."""
        self.assertEqual(
            slug_repair.attribute_shared_slug({
                "slug": "the-bear", "trakt_slug": None, "simkl_slug": "the-bear",
                "trakt_id": 1, "simkl_id": 2,
            }),
            {},
        )

    def test_a_row_that_already_knows_is_left_alone(self):
        self.assertEqual(
            slug_repair.attribute_shared_slug({
                "slug": "x-2018", "trakt_slug": "x-2018", "simkl_slug": "x",
                "trakt_id": 1, "simkl_id": 2,
            }),
            {},
        )

    def test_a_row_with_no_trakt_id_is_owed_no_trakt_name(self):
        """A name is owed only where the row holds that service's id. Writing one
        anyway would make the record look linkable to a service that cannot be
        asked about it."""
        self.assertEqual(
            slug_repair.attribute_shared_slug({
                "slug": "something", "trakt_slug": None, "simkl_slug": "other",
                "trakt_id": None, "simkl_id": 2,
            }),
            {},
        )


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
        self.assertEqual(
            slug_repair.recoverable_names({"simkl": 2}, {"trakt_slug": "b"}), {})

    def test_an_empty_answer_recovers_nothing(self):
        self.assertEqual(
            slug_repair.recoverable_names({"trakt": 1}, {"trakt_slug": ""}), {})


class RepairAndLogTests(DatabaseTestCase):
    """End to end against the database: the name lands on every row of the title
    and the recovery is written down."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = db.now()
        await db.execute(
            "INSERT INTO users (id, username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) "
            "VALUES (1, 'someone', 0, 1, 1, ?, ?)", (now, now))
        # The real shape from production: a month record holding both services'
        # ids, the old shared name, Simkl's name, and no Trakt name.
        for month in ("2026-07", "2026-08"):
            await db.execute(
                "INSERT INTO distrakt_month_records "
                "(user_id, month, kind, media, match_source, match_id, season, "
                " slug, simkl_slug, trakt_id, simkl_id, title, created_at) "
                "VALUES (1, ?, 'premiere', 'show', 'tmdb', '134421', 1, "
                "'the-rookie-2018', 'the-rookie', 134421, 555, 'The Rookie', ?)",
                (month, db.now()))

    async def test_the_name_lands_on_every_row_of_the_title(self):
        """One recovery, both months: a name is a fact about the TITLE, so
        `learn_ids` addresses the identity rather than a season."""
        changed = await slug_repair.repair_user(1)
        self.assertEqual(changed, 2)
        rows = await db.fetch_all(
            "SELECT trakt_slug FROM distrakt_month_records ORDER BY month")
        self.assertEqual([r["trakt_slug"] for r in rows],
                         ["the-rookie-2018", "the-rookie-2018"])

    async def test_the_recovery_is_written_down_with_its_evidence(self):
        await slug_repair.repair_user(1)
        rows = await db.fetch_all("SELECT * FROM distrakt_slug_repairs")
        self.assertEqual(len(rows), 1, "one identity, one log row")
        row = rows[0]
        self.assertEqual(row["column_name"], "trakt_slug")
        self.assertEqual(row["value"], "the-rookie-2018")
        self.assertEqual(row["evidence"], slug_repair.EVIDENCE_SHARED_SLUG)
        self.assertEqual(row["match_id"], "134421")
        self.assertEqual(row["rows_changed"], 2)
        self.assertEqual(row["title"], "The Rookie")
        self.assertTrue(row["repaired_at"])

    async def test_a_second_pass_finds_nothing_and_logs_nothing(self):
        """THE PROPERTY THE WHOLE DESIGN RESTS ON. The log is read by DATE — a
        row dated after the backlog means a writer is still dropping names — so a
        pass that re-logged settled work every minute would make it unreadable."""
        await slug_repair.repair_user(1)
        before = await db.fetch_all("SELECT COUNT(*) c FROM distrakt_slug_repairs")
        self.assertEqual(await slug_repair.repair_user(1), 0)
        after = await db.fetch_all("SELECT COUNT(*) c FROM distrakt_slug_repairs")
        self.assertEqual(before[0]["c"], after[0]["c"])

    async def test_nothing_is_overwritten(self):
        """`learn_ids` fills blanks only, so a stored name a service actually
        stated always beats one this module derived."""
        await db.execute(
            "UPDATE distrakt_month_records SET trakt_slug = 'already-known'")
        self.assertEqual(await slug_repair.repair_user(1), 0)
        rows = await db.fetch_all("SELECT DISTINCT trakt_slug FROM distrakt_month_records")
        self.assertEqual([r["trakt_slug"] for r in rows], ["already-known"])

    async def test_an_unprovable_row_is_left_alone_rather_than_guessed_at(self):
        await db.execute("UPDATE distrakt_month_records SET simkl_slug = ''")
        self.assertEqual(await slug_repair.repair_user(1), 0)
        rows = await db.fetch_all("SELECT COUNT(*) c FROM distrakt_slug_repairs")
        self.assertEqual(rows[0]["c"], 0)


class AskingTheServiceTests(DatabaseTestCase):
    """The half `attribute_shared_slug` refuses to guess at: a row with nothing
    to compare against, where the service that owns the name is simply asked.

    No network. `detail_source.fetch` is the seam and is stood in for — the tests
    that matter assert what it was ASKED, because asking the wrong service by the
    wrong id is the failure that produces a link to a title nobody has.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = db.now()
        await db.execute(
            "INSERT INTO users (id, username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) "
            "VALUES (1, 'someone', 0, 1, 1, ?, ?)", (now, now))
        # No simkl_slug, so nothing local can prove whose the shared name is.
        # This is the production shape the derivation deliberately leaves alone.
        await db.execute(
            "INSERT INTO distrakt_month_records "
            "(user_id, month, kind, media, match_source, match_id, season, "
            " slug, trakt_id, simkl_id, title, created_at) "
            "VALUES (1, '2026-07', 'premiere', 'show', 'tmdb', '154574', 1, "
            "'house-of-the-dragon', 154574, 777, 'House of the Dragon', ?)", (now,))

    @staticmethod
    def _answer(**by_source):
        """A stand-in `detail_source.fetch` that records what it was asked."""
        calls = []

        async def fetch(settings, source, media, source_id, season, **kw):
            calls.append((str(source), str(media), str(source_id)))
            return {"ids": by_source.get(str(source), {})}

        return fetch, calls

    async def test_it_asks_each_service_by_that_services_own_id(self):
        """A service cannot look a title up by an id it does not issue, so the
        Trakt question carries the Trakt id and the Simkl question the Simkl one."""
        fetch, calls = self._answer(
            trakt={"trakt_slug": "house-of-the-dragon"},
            simkl={"simkl_slug": "house-of-the-dragon-hbo"})
        with mock.patch.object(detail_source, "fetch", fetch):
            changed = await slug_repair.repair_by_asking(object(), 1)
        self.assertEqual(sorted(calls),
                         [("simkl", "show", "777"), ("trakt", "show", "154574")])
        self.assertEqual(changed, 2, "one row, two names")

    async def test_the_answer_is_stored_and_logged_as_a_lookup(self):
        fetch, _calls = self._answer(trakt={"trakt_slug": "house-of-the-dragon"})
        with mock.patch.object(detail_source, "fetch", fetch):
            await slug_repair.repair_by_asking(object(), 1)
        row = (await db.fetch_all("SELECT * FROM distrakt_month_records"))[0]
        self.assertEqual(row["trakt_slug"], "house-of-the-dragon")
        log = await db.fetch_all(
            "SELECT * FROM distrakt_slug_repairs WHERE column_name = 'trakt_slug'")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["evidence"], slug_repair.EVIDENCE_SOURCE_LOOKUP,
                         "a name nothing stored knew must be distinguishable in "
                         "the log from one that was derivable")

    async def test_a_service_that_cannot_answer_costs_the_others_nothing(self):
        """A source being down is not a reason to lose the name another gave."""
        async def fetch(settings, source, media, source_id, season, **kw):
            if str(source) == "trakt":
                raise RuntimeError("Trakt is unreachable")
            return {"ids": {"simkl_slug": "hotd"}}

        with mock.patch.object(detail_source, "fetch", fetch):
            changed = await slug_repair.repair_by_asking(object(), 1)
        self.assertEqual(changed, 1)
        row = (await db.fetch_all("SELECT * FROM distrakt_month_records"))[0]
        self.assertEqual(row["simkl_slug"], "hotd")
        self.assertIn(row["trakt_slug"], (None, ""))

    async def test_a_second_pass_does_not_ask_again(self):
        """THE BOUND THAT MAKES THIS SAFE ON A ONE-MINUTE HEARTBEAT. The work is
        a function of what is owed, so an unchanged owed set cannot have a new
        answer — the same guard `naming.fill_from_calendar` uses, and without it
        a title no service can name would be asked about every single tick."""
        async def fetch(settings, source, media, source_id, season, **kw):
            calls.append(str(source))
            return {"ids": {}}  # neither service names it

        calls: list[str] = []
        with mock.patch.object(detail_source, "fetch", fetch):
            await slug_repair.repair_by_asking(object(), 1)
            first = len(calls)
            await slug_repair.repair_by_asking(object(), 1)
        self.assertEqual(first, 2, "both services asked once")
        self.assertEqual(len(calls), first, "and not asked a second time")

    async def test_a_row_the_derivation_can_prove_is_never_asked_about(self):
        """The free half runs first and this half only sees what is left, so a
        title both could answer for costs no request at all."""
        await db.execute(
            "UPDATE distrakt_month_records SET simkl_slug = 'house-of-dragon'")
        fetch, calls = self._answer(trakt={"trakt_slug": "x"})
        with mock.patch.object(detail_source, "fetch", fetch):
            await slug_repair.repair_by_asking(object(), 1)
        self.assertEqual(calls, [], "derivable rows must not reach the network")


class LogWriteTests(DatabaseTestCase):
    async def test_a_log_failure_never_reaches_the_caller(self):
        """The useful work is the name landing on the record; the log is how
        somebody finds out later that it was needed. Raising here would cost a
        viewer their modal to save a diagnostic."""
        await db.execute("DROP TABLE distrakt_slug_repairs")
        # Must not raise.
        await slug_repair.record(1, ItemKey("show", "tmdb", "1"),
                                 "trakt_slug", "x", "shared_slug", 1)


if __name__ == "__main__":
    unittest.main()
