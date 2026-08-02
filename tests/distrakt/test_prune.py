"""The hand-run pass that takes carried rows off the months they were copied to.

WHAT IS WORTH TESTING HERE IS THE RULE, and it is worth testing hard: this is the
only code in the app that deletes a row from a month the calendar has closed, and
a row it drops wrongly is gone. So most of what follows is the pure decision —
which rows it keeps, which it drops, and every shape of row that cannot answer the
question — with one round trip against a real database to pin that a dry run
writes nothing and that an authorised run removes exactly what it listed.

No network and no clock: the rule compares a row's stored premiere month against
the month it is filed under, and neither is today.
"""
from __future__ import annotations

import io
import re
import unittest

from app import db
from app.distrakt import prune, store
from app.providers.base import ItemKey
from tests.support import APP_DIR, new_db_path


def row(title: str, season: int = 1, *, premiere: str | None = "6/12",
        bucket: str | None = "keepup", abandoned: bool = False,
        trakt_id: int | None = None, **extra) -> dict:
    """A stored roster row, in the shape store.row_to_show hands out.

    The identity triple is spelled out because that is what a row read back from
    the database carries, and the rule has to work on the real shape rather than
    on a convenient one.
    """
    ident = trakt_id if trakt_id is not None else abs(hash((title, season))) % 100000
    return {
        "media": "show", "match_source": "trakt", "match_id": str(ident),
        "key": f"show:trakt:{ident}",
        "season": season, "ids": {"trakt": ident},
        "title": title, "network": "HBO",
        "abandoned": abandoned, "abandoned_form": None,
        "watched": 0, "total": 8, "cadence": "Sun",
        "premiere": premiere, "finale": None, "bucket": bucket,
        "started_airing": True, "finished_airing": False,
        "added_by": "calendar", **extra,
    }


class JudgeRowTests(unittest.TestCase):
    """One row against one month. Every branch of the rule, stated as the
    question a reviewer would ask of the report."""

    def test_a_title_that_premiered_in_the_month_stays(self):
        verdict = prune.judge_row("2026-06", row("Premiered Here", premiere="6/12"))
        self.assertFalse(verdict.drop)
        self.assertIn("premiered", verdict.reason)

    def test_a_title_carried_in_from_an_earlier_month_goes(self):
        """The case the pass exists for: June's premiere sitting in July's roster
        with nothing about July to show for it."""
        verdict = prune.judge_row("2026-07", row("Carried", premiere="6/12"))
        self.assertTrue(verdict.drop)
        self.assertIn("bucket keepup", verdict.reason)

    def test_completed_here_is_a_fact_about_here_whenever_it_premiered(self):
        """A May premiere finished off in July is the thing a closed July exists
        to record. Deleting it would destroy the record, not tidy it."""
        verdict = prune.judge_row("2026-07", row("Finished", premiere="5/01",
                                                 bucket="completed"))
        self.assertFalse(verdict.drop)
        self.assertIn("completed", verdict.reason)

    def test_abandoned_here_is_the_same(self):
        verdict = prune.judge_row("2026-07", row("Given Up", premiere="5/01",
                                                 bucket="abandoned"))
        self.assertFalse(verdict.drop)
        self.assertIn("abandoned", verdict.reason)

    def test_the_abandoned_flag_alone_is_enough(self):
        """The flag is what the user pressed and the bucket is what the month
        wrote down at freeze; either one on its own still means they gave it up
        here. A row carrying one without the other must not be dropped on the
        strength of the other being absent."""
        verdict = prune.judge_row("2026-07", row("Given Up", premiere="5/01",
                                                 bucket="keepup", abandoned=True))
        self.assertFalse(verdict.drop)
        self.assertIn("abandoned", verdict.reason)

    def test_both_facts_at_once_reports_the_premiere(self):
        """Premiered here AND completed here: kept either way, and the report
        names the stronger reason so the list reads as what it is."""
        verdict = prune.judge_row("2026-06", row("Both", premiere="6/02",
                                                 bucket="completed"))
        self.assertFalse(verdict.drop)
        self.assertIn("premiered", verdict.reason)

    def test_a_row_with_no_bucket_is_kept_and_flagged(self):
        """NULL bucket — a row an open month never had a verdict written onto.
        The row cannot say whether its month settled it, and a row that cannot
        answer is kept."""
        verdict = prune.judge_row("2026-07", row("No Bucket", premiere="5/01",
                                                 bucket=None))
        self.assertFalse(verdict.drop)
        self.assertTrue(verdict.ambiguous)

    def test_a_bucket_from_no_known_vocabulary_is_kept_and_flagged(self):
        verdict = prune.judge_row("2026-07", row("Odd", premiere="5/01",
                                                 bucket="something-else"))
        self.assertFalse(verdict.drop)
        self.assertTrue(verdict.ambiguous)

    def test_a_missing_premiere_date_is_kept_and_flagged(self):
        """No date means the row cannot be shown to have premiered somewhere
        else, which is the only thing that would justify removing it."""
        verdict = prune.judge_row("2026-07", row("Undated", premiere=None))
        self.assertFalse(verdict.drop)
        self.assertTrue(verdict.ambiguous)

    def test_an_unreadable_premiere_date_is_kept_and_flagged(self):
        for bad in ("", "soon", "7", "7/", "??/??", "2026-06-12"):
            with self.subTest(premiere=bad):
                verdict = prune.judge_row("2026-07", row("Unreadable", premiere=bad))
                self.assertFalse(verdict.drop)
                self.assertTrue(verdict.ambiguous)

    def test_a_zero_padded_premiere_reads_as_its_month(self):
        """"07/04" and "7/4" are the same July."""
        self.assertFalse(prune.judge_row("2026-07", row("Padded", premiere="07/04")).drop)
        self.assertTrue(prune.judge_row("2026-08", row("Padded", premiere="07/04")).drop)

    def test_the_month_is_read_from_its_key_not_guessed(self):
        with self.assertRaises(ValueError):
            prune.judge_row("2026-7", row("Anything"))


class PlanRemovalsTests(unittest.TestCase):
    """The whole store at once — where the rule needs to see more than one month."""

    def test_months_are_reported_in_order_with_their_counts(self):
        plans = prune.plan_removals({
            "2026-07": [row("A", premiere="7/01"), row("B", premiere="6/01")],
            "2026-06": [row("B", premiere="6/01")],
        })
        self.assertEqual([p.month for p in plans], ["2026-06", "2026-07"])
        self.assertEqual([p.examined for p in plans], [1, 2])
        self.assertEqual([len(p.drops) for p in plans], [0, 1])

    def test_a_carried_copy_goes_while_the_month_that_premiered_it_keeps_its_own(self):
        """The shape the pass was built for: one title, three months, one home."""
        june = row("Carried", premiere="6/12")
        plans = {p.month: p for p in prune.plan_removals({
            "2026-06": [june],
            "2026-07": [dict(june)],
            "2026-08": [dict(june)],
        })}
        self.assertEqual(plans["2026-06"].drops, [])
        self.assertEqual(len(plans["2026-07"].drops), 1)
        self.assertEqual(len(plans["2026-08"].drops), 1)

    def test_the_last_copy_of_a_season_is_never_dropped(self):
        """A title the tracker met part-way through — picked up from recent
        viewing on a month already running — has no row on the month it premiered
        in, because nobody was tracking then. Dropping every copy would take it
        off the user's own lists, which are read from all the months at once."""
        carried = row("Met Part Way", premiere="3/01", added_by="history")
        plans = {p.month: p for p in prune.plan_removals({
            "2026-07": [carried],
            "2026-08": [dict(carried)],
        })}
        self.assertEqual(plans["2026-07"].drops, [])
        self.assertEqual(len(plans["2026-08"].drops), 1)
        kept = plans["2026-07"].unsure_keeps
        self.assertEqual(len(kept), 1)
        self.assertIn("only record", kept[0].reason)

    def test_the_rescue_keeps_the_earliest_month_not_the_latest(self):
        plans = {p.month: p for p in prune.plan_removals({
            "2026-08": [row("Met Part Way", premiere="3/01")],
            "2026-05": [row("Met Part Way", premiere="3/01")],
            "2026-07": [row("Met Part Way", premiere="3/01")],
        })}
        self.assertEqual(plans["2026-05"].drops, [])
        self.assertEqual(len(plans["2026-07"].drops), 1)
        self.assertEqual(len(plans["2026-08"].drops), 1)

    def test_no_rescue_when_some_other_month_keeps_the_season_anyway(self):
        """A season the rule already keeps somewhere needs no rescue, so every
        carried copy of it goes."""
        plans = {p.month: p for p in prune.plan_removals({
            "2026-05": [row("Finished In May", premiere="3/01", bucket="completed")],
            "2026-07": [row("Finished In May", premiere="3/01")],
            "2026-08": [row("Finished In May", premiere="3/01")],
        })}
        self.assertEqual(plans["2026-05"].drops, [])
        self.assertEqual(len(plans["2026-07"].drops), 1)
        self.assertEqual(len(plans["2026-08"].drops), 1)

    def test_two_seasons_of_one_show_are_rescued_independently(self):
        """The rescue is per SEASON, not per title: season 1 having a home says
        nothing about season 2."""
        plans = {p.month: p for p in prune.plan_removals({
            "2026-06": [row("Show", season=1, premiere="6/01")],
            "2026-07": [row("Show", season=1, premiere="6/01"),
                        row("Show", season=2, premiere="3/01")],
        })}
        dropped = {v.row["season"] for v in plans["2026-07"].drops}
        self.assertEqual(dropped, {1})

    def test_a_row_nothing_can_name_is_kept_and_flagged(self):
        """Without a shared id there is no way to find its copies on other months
        and no way to address it in a delete. Not a row to remove on a guess."""
        nameless = row("Nameless", premiere="3/01")
        for field in ("media", "match_source", "match_id", "key", "ids"):
            nameless.pop(field, None)
        plan = prune.plan_removals({"2026-07": [nameless]})[0]
        self.assertEqual(plan.drops, [])
        self.assertEqual(len(plan.unsure_keeps), 1)
        self.assertIn("names it", plan.unsure_keeps[0].reason)

    def test_an_empty_month_examines_nothing(self):
        plan = prune.plan_removals({"2026-07": []})[0]
        self.assertEqual((plan.examined, plan.drops, plan.unsure_keeps), (0, [], []))


class ReportTests(unittest.TestCase):
    """The report is the safeguard — it is the only chance anybody has to notice
    that a row the rule calls carried is one the month should keep. So every
    removal is named, not counted."""

    def test_every_removal_names_the_title_the_season_and_the_reason(self):
        text = prune.format_report(prune.plan_removals({
            "2026-06": [row("Kept Here", season=3, premiere="6/01")],
            "2026-07": [row("Kept Here", season=3, premiere="6/01")],
        }))
        self.assertIn("Kept Here S03", text)
        self.assertIn("premiered in month 6", text)
        self.assertIn("1 of 2 rows would be removed", text)

    def test_an_unsure_keep_is_reported_too(self):
        text = prune.format_report(prune.plan_removals({
            "2026-07": [row("No Bucket", premiere="5/01", bucket=None)],
        }))
        self.assertIn("KEPT:", text)
        self.assertIn("0 of 1 rows would be removed", text)


class PruneAgainstADatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The I/O edges: reading a store, writing nothing on a dry run, and removing
    exactly what was listed when asked."""

    async def asyncSetUp(self):
        new_db_path("prune")
        await db.migrate()
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("tracker", now, now),
        )
        self.user_id = result.lastrowid
        # June premieres it, July carries it, and July also finished something
        # that started in May. Closed months, because that is where these rows are.
        for month, shows in (
            ("2026-06", [row("Carried", premiere="6/12")]),
            ("2026-07", [row("Carried", premiere="6/12"),
                         row("Finished", premiere="5/01", bucket="completed")]),
        ):
            await store.save_month(self.user_id, {
                "month": month, "closed": True, "totals_refreshed_at": now,
                "shows": shows,
            })

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _months(self) -> dict[str, list[str]]:
        return {month: [s["title"] for s in rows]
                for month, rows in (await prune.read_months(self.user_id)).items()}

    async def test_it_reads_every_month_the_user_holds(self):
        self.assertEqual(await self._months(),
                         {"2026-06": ["Carried"], "2026-07": ["Carried", "Finished"]})

    async def test_a_dry_run_writes_nothing(self):
        out = io.StringIO()
        self.assertEqual(await prune.run(apply=False, user_id=None, out=out), 0)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("Carried S01", out.getvalue())
        self.assertEqual(await self._months(),
                         {"2026-06": ["Carried"], "2026-07": ["Carried", "Finished"]})

    async def test_an_authorised_run_removes_exactly_what_it_listed(self):
        out = io.StringIO()
        self.assertEqual(await prune.run(apply=True, user_id=None, out=out), 0)
        self.assertIn("1 rows removed", out.getvalue())
        self.assertEqual(await self._months(),
                         {"2026-06": ["Carried"], "2026-07": ["Finished"]})

    async def test_it_is_idempotent(self):
        """Everything left after a run qualifies, so a second run finds nothing."""
        await prune.run(apply=True, user_id=None, out=io.StringIO())
        out = io.StringIO()
        await prune.run(apply=True, user_id=None, out=out)
        self.assertIn("0 rows removed", out.getvalue())

    async def test_a_closed_month_is_not_a_reason_to_skip_a_row(self):
        """Both months here are frozen. Editing a settled month is exactly what
        this pass is for and why it is a separate program somebody has to run."""
        doc = await store.load_month(self.user_id, "2026-07")
        self.assertTrue(doc["closed"])
        await prune.run(apply=True, user_id=self.user_id, out=io.StringIO())
        self.assertEqual([s["title"] for s in
                          (await store.load_month(self.user_id, "2026-07"))["shows"]],
                         ["Finished"])

    async def test_it_finds_the_accounts_holding_rows_by_itself(self):
        self.assertEqual(await store.users_with_shows(), [self.user_id])

    async def test_an_account_with_nothing_stored_is_reported_and_left_alone(self):
        empty = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 0, 1, 1, ?, ?)",
            ("nobody", db.now(), db.now()),
        )
        out = io.StringIO()
        await prune.run(apply=True, user_id=empty.lastrowid, out=out)
        self.assertIn("0 of 0 rows", out.getvalue())

    async def test_the_database_it_is_about_to_read_is_the_first_thing_it_says(self):
        """The worst outcome available here is being right about the rows and
        wrong about which database they are in."""
        out = io.StringIO()
        await prune.run(apply=False, user_id=None, out=out)
        self.assertTrue(out.getvalue().startswith(f"Database: {db.db_path()}"))


class NothingRunsItButAPersonTests(unittest.TestCase):
    """BY INSPECTION: no module in app/ imports the pass.

    The behavioural tests above say what it does when it is run. This says it is
    never run by the app at all, and that is the guarantee that actually matters:
    a page load, a startup hook or a rollover that reached this module would be
    editing a settled month with nobody asking, and no test of what it deletes
    would notice. An import that merely happens not to run today would satisfy a
    behavioural test and not this one — the same reasoning as
    tests/ranker/test_standalone.py's inspection half.
    """
    # Its own module and the package docstring that files it are the two places
    # the name is allowed to appear.
    ALLOWED = {"distrakt/prune.py", "distrakt/__init__.py"}
    IMPORTS_IT = re.compile(r"^\s*(from\s+\S*\s+import\s+[^\n]*\bprune\b|"
                            r"import\s+[^\n]*\bprune\b)", re.MULTILINE)

    def _modules(self):
        return [p for p in APP_DIR.rglob("*.py")
                if p.relative_to(APP_DIR).as_posix() not in self.ALLOWED]

    def test_no_module_in_the_app_imports_it(self):
        offenders = sorted(
            path.relative_to(APP_DIR).as_posix() for path in self._modules()
            if self.IMPORTS_IT.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(offenders, [], (
            "these modules import the hand-run pass. Nothing the app does on its "
            f"own may delete a row from a month that has settled: {offenders}"))

    def test_the_pattern_would_catch_one_if_there_were(self):
        """Guards the test above against passing because it matches nothing."""
        for wired_in in ("from . import prune", "from .distrakt import prune",
                         "import app.distrakt.prune"):
            with self.subTest(line=wired_in):
                self.assertRegex(wired_in, self.IMPORTS_IT)

    def test_it_checked_a_real_list_of_modules(self):
        self.assertGreater(len(self._modules()), 20)


class CommandLineTests(unittest.TestCase):
    """Reporting is the default and writing is the flag, so a run that forgets to
    say which it wanted does the harmless one."""

    def test_writing_is_off_unless_asked_for(self):
        self.assertFalse(prune.build_parser().parse_args([]).apply)
        self.assertTrue(prune.build_parser().parse_args(["--apply"]).apply)

    def test_every_account_unless_one_is_named(self):
        self.assertIsNone(prune.build_parser().parse_args([]).user)
        self.assertEqual(prune.build_parser().parse_args(["--user", "3"]).user, 3)


class RemoveShowIsTheOneDeleteTests(unittest.IsolatedAsyncioTestCase):
    """The pass owns no SQL of its own: it addresses a row through the identity
    the store files it under, so a row it lists is a row it can actually remove."""

    async def asyncSetUp(self):
        new_db_path("prune-delete")
        await db.migrate()
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("tracker", now, now),
        )
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_the_verdict_carries_enough_to_address_the_row(self):
        rec = row("Carried", premiere="6/12", trakt_id=4242)
        await store.add_show(self.user_id, "2026-07", rec)
        # One copy and nowhere else to keep it: rescued rather than dropped.
        plans = prune.plan_removals(await prune.read_months(self.user_id))
        self.assertEqual([len(p.drops) for p in plans], [0])

        await store.add_show(self.user_id, "2026-06", rec)
        plans = prune.plan_removals(await prune.read_months(self.user_id))
        self.assertEqual(await prune.apply_removals(self.user_id, plans), 1)
        self.assertEqual((await store.load_month(self.user_id, "2026-07"))["shows"], [])
        # June — where it premiered — still has it, addressed by the same identity.
        self.assertIsNotNone(await store.find_user_row(
            self.user_id, ItemKey("show", "trakt", "4242"), 1))


if __name__ == "__main__":
    unittest.main()
