"""A hand-run maintenance pass that takes off a month the rows that were never
that month's to hold.

*** THIS IS THE ONE THING IN THE PACKAGE THAT EDITS A SETTLED MONTH, AND IT ONLY
RUNS WHEN A PERSON RUNS IT. *** Nothing imports this module: it is reached as
`python -m app.distrakt.prune` and by no other route. It is not wired into a
page, a route, a startup hook or a rollover, and it must not become so. A month
the calendar has left behind is a record of what happened during it; the app
rewriting one on its own — on a page load, on a schedule, as a side effect of
something else — would mean that record could change without anybody asking, and
there is no second copy of it to compare against. So the deletions here are
deliberate, reported before they happen, and separately authorised.

WHY THERE IS ANYTHING TO REMOVE. A new month used to be built by copying the
previous month's roster into it, so a title stayed on every month from the one it
premiered in until the one it was finished in. That copy is gone — a month is
built from its own premieres, and what the viewer is behind on is read across
every month at once (store.user_roster) — but the rows the old build wrote are
still stored. A title that premiered in June therefore still sits in July's and
August's rosters as though it were theirs.

THE RULE, AND IT IS DELIBERATELY NARROW: a row goes only if it neither PREMIERED
in its month, nor COMPLETED in it, nor WAS ABANDONED in it. "Completed in July"
is a fact about July even for a title that premiered in May, and it is precisely
the kind of thing a closed month exists to record — the verdict, not the
progress. Everything the stored row cannot answer is KEPT and reported instead:
no bucket recorded, no readable premiere date, nothing that names the row. An
untidy row costs a little tidiness; a deleted one cannot be got back.

POINTING IT AT A DATABASE is the app's own TRAKT_DATA_DIR and nothing new, so
there is no second way to say where the data is and no chance of the two
disagreeing. Set it to a COPY of the directory and run the report there first:

    TRAKT_DATA_DIR=/path/to/copy python -m app.distrakt.prune

The path it is about to read is the first thing it prints, so a mistake about
which database is in front of you is visible before anything is examined.

I/O IS AT THE EDGES. `plan_removals` takes months of rows and returns what to
drop with no database anywhere near it, which is what makes the rule — the risky
part — testable on hand-written rows.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from .. import db
from . import discord_fmt, live, store
from .store import Bucket

# The buckets that record a VERDICT a month reached about a title, as opposed to
# where the title had got to while the month was running. These are the facts a
# closed month is kept for, so a row carrying one belongs to its month however it
# arrived there and whenever it premiered.
_VERDICT_BUCKETS = frozenset({Bucket.COMPLETED, Bucket.ABANDONED})

# The values the `bucket` column is allowed to hold. Anything else — NULL on a
# row written before the column was populated, or a value from some later
# vocabulary — means the row cannot tell us what its month made of it.
_KNOWN_BUCKETS = frozenset(Bucket)


class Verdict(NamedTuple):
    """One row's fate on one month, and the sentence that explains it.

    Carries the row itself rather than a copy of the few fields the report
    prints, because the caller that acts on a drop needs the identity to address
    it by and re-deriving that from a summary would be a second way of naming a
    row.

    `ambiguous` marks a KEEP made because the row could not answer the question,
    rather than because it answered it in the row's favour. Those are worth
    reporting separately: they are where a rule this blunt is least sure of
    itself, and they are the list to read if the pass looks like it removed too
    little.
    """
    month: str
    row: Mapping[str, Any]
    drop: bool
    reason: str
    ambiguous: bool = False


class MonthPlan(NamedTuple):
    """What one month would lose, and what it kept only for want of an answer."""
    month: str
    examined: int
    drops: list[Verdict]
    unsure_keeps: list[Verdict]


def _title_of(verdict: Verdict) -> str:
    """`Title S03` — how a row is named in the report, so a reviewer can find it
    on the page it came from."""
    row = verdict.row
    return f"{row.get('title') or '(untitled)'} S{int(row.get('season') or 0):02d}"


def judge_row(month: str, row: Mapping[str, Any]) -> Verdict:
    """Whether `row` belongs to `month`, and why.

    Ordered so the STRONGEST reason to keep is the one reported: a reviewer
    scanning the kept rows wants to see "it premiered here" before "the row could
    not say". Every check that keeps a row comes before the one that drops it,
    which is the whole design — the drop is what is left when nothing spoke up
    for the row.

    Premiered-in-month is asked of discord_fmt, which already owns that test for
    the month's first notice. Asking it a second way here is how the notice and
    this pass would come to disagree about which month a title belongs to, and
    only one of them would be deleting rows.
    """
    month_number = store.parse_month_key(month)[1]
    bucket = str(row.get("bucket") or "")

    if discord_fmt.premiered_in_month(row, month_number):
        return Verdict(month, row, False, "premiered in this month")
    if bucket == Bucket.COMPLETED:
        return Verdict(month, row, False, "completed in this month")
    # The flag and the bucket are checked together because either one on its own
    # is enough: `abandoned` is what the user pressed, and the bucket is what the
    # month wrote down when it froze. A row carrying one without the other is
    # still a row about giving something up.
    if row.get("abandoned") or bucket == Bucket.ABANDONED:
        return Verdict(month, row, False, "abandoned in this month")
    if bucket not in _KNOWN_BUCKETS:
        return Verdict(month, row, False,
                       "no bucket recorded, so the row cannot say whether this "
                       "month settled it", ambiguous=True)
    premiere = discord_fmt.premiere_month(row)
    if premiere is None:
        return Verdict(month, row, False,
                       "no readable premiere date, so it cannot be shown to have "
                       "premiered elsewhere", ambiguous=True)
    return Verdict(month, row, True,
                   f"premiered in month {premiere}, and this month neither "
                   f"finished nor abandoned it (bucket {bucket})")


def plan_removals(months: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[MonthPlan]:
    """What each of `months` would lose, in month order. PURE — no database, no
    clock, no network. `months` maps "YYYY-MM" to that month's stored rows.

    The whole store goes in at once rather than a month at a time because of the
    rescue below, which is a fact about a season across every month and cannot be
    seen from inside one of them.

    THE LAST COPY OF A SEASON IS NEVER DROPPED. The user's own lists — what they
    are behind on, what they are keeping up with — are derived from the union of
    every month's rows (store.user_roster), so a season with no row left anywhere
    disappears from the tracker altogether rather than merely moving off a month.
    Most seasons have a row on the month they premiered in and that row is kept by
    the rule above, so the case only arises for a title the tracker met part-way
    through — one picked up from recent viewing on a month that was already
    running. Those are exactly the rows the narrow rule cannot tell from carried
    ones, and losing the title entirely is not a tidier month, it is a hole. The
    EARLIEST month keeps its copy: that is the month the tracker first knew the
    title, and it is invisible there either way, since a month that is over only
    ever presents what it settled.
    """
    verdicts: dict[str, list[Verdict]] = {}
    for month in sorted(months):
        verdicts[month] = [judge_row(month, row) for row in months[month]]

    # Where every copy of a season is marked for removal, put the earliest one
    # back. Indexed by (item key, season) — the same pair the watch-history
    # lookups use, so "the same season" means here what it means everywhere else.
    seasons: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)
    for month, month_verdicts in verdicts.items():
        for position, verdict in enumerate(month_verdicts):
            try:
                seasons[live.live_key(verdict.row)].append((month, position))
            except store.UnkeyableRecord:
                # Nothing shared names this row, so it can be matched with no copy
                # on another month — and, for the same reason, addressed by no
                # delete. A row nothing can name is not a row to remove on a
                # guess, so a drop becomes a keep that says why. A keep is already
                # right and is left with the reason it earned.
                if verdict.drop:
                    verdicts[month][position] = verdict._replace(
                        drop=False, ambiguous=True,
                        reason="nothing on the row names it, so no copy of it "
                               "elsewhere could be found")
    for places in seasons.values():
        if all(verdicts[month][position].drop for month, position in places):
            month, position = min(places)  # earliest month, then insertion order
            verdicts[month][position] = verdicts[month][position]._replace(
                drop=False, ambiguous=True,
                reason="the only record of this season anywhere, so removing it "
                       "would drop the title off the roster entirely")

    return [
        MonthPlan(
            month=month,
            examined=len(month_verdicts),
            drops=[v for v in month_verdicts if v.drop],
            unsure_keeps=[v for v in month_verdicts if not v.drop and v.ambiguous],
        )
        for month, month_verdicts in verdicts.items()
    ]


def format_report(plans: Iterable[MonthPlan]) -> str:
    """The plan as text for a person to read before authorising anything. PURE.

    Every removal is named individually — title, season and the reason it did not
    qualify — because the report IS the safeguard: it is the only chance anybody
    gets to notice that a row the rule calls carried is actually something the
    month should keep. A count would not give them that chance.
    """
    lines: list[str] = []
    total_examined = total_drops = 0
    for plan in plans:
        total_examined += plan.examined
        total_drops += len(plan.drops)
        lines.append(f"  {plan.month}  {plan.examined} rows examined, "
                     f"{len(plan.drops)} to remove")
        for verdict in plan.drops:
            lines.append(f"      - {_title_of(verdict)} — {verdict.reason}")
        for verdict in plan.unsure_keeps:
            lines.append(f"      ? {_title_of(verdict)} — KEPT: {verdict.reason}")
    lines.append(f"  {total_drops} of {total_examined} rows would be removed.")
    return "\n".join(lines)


async def read_months(user_id: int) -> dict[str, list[dict]]:
    """Every month this user holds, and the rows on it.

    Reads the month list rather than the distinct months of the rows themselves:
    a month row always exists by the time a show row is filed under it (both
    store.add_show and store.save_month write it), so this is the wider of the
    two and cannot miss anything. The reverse is not true — a month emptied of
    its rows keeps its row — and an empty month here simply examines nothing.
    """
    months: dict[str, list[dict]] = {}
    for month in await store.list_months(user_id):
        doc = await store.load_month(user_id, month)
        months[month] = list((doc or {}).get("shows") or [])
    return months


async def apply_removals(user_id: int, plans: Iterable[MonthPlan]) -> int:
    """Delete every row the plans marked. Returns how many rows actually went.

    Goes through store.remove_show, whose docstring notes that callers guard
    closed months. This one deliberately does not: removing a row from a month
    the calendar has closed is the entire purpose of this pass, and it is why the
    pass is a separate program a person has to run, point at a data directory and
    then authorise a second time. Nothing that serves a request may do this.
    """
    removed = 0
    for plan in plans:
        for verdict in plan.drops:
            if await store.remove_show(user_id, plan.month,
                                       store.record_key(verdict.row),
                                       int(verdict.row["season"])):
                removed += 1
    return removed


def build_parser() -> argparse.ArgumentParser:
    """The command line. Reporting is the DEFAULT and writing is the flag, so a
    run that forgets to say which it wanted does the harmless one."""
    parser = argparse.ArgumentParser(
        prog="python -m app.distrakt.prune",
        description=(
            "Report — and, with --apply, remove — tracker rows sitting on a month "
            "that neither premiered them, finished them, nor gave up on them. "
            "Point TRAKT_DATA_DIR at a COPY of the data directory and read the "
            "report before running it anywhere that matters."),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete the rows listed. Without it nothing is written.")
    parser.add_argument(
        "--user", type=int, default=None, metavar="ID",
        help="restrict the pass to one account. Default: every account holding rows.")
    return parser


async def run(apply: bool, user_id: int | None, out) -> int:
    """The pass itself. Returns a process exit status.

    The database path is printed before anything is read, because the single
    worst outcome here is being right about the rows and wrong about which
    database they are in.
    """
    print(f"Database: {db.db_path()}", file=out)
    users = [user_id] if user_id is not None else await store.users_with_shows()
    if not users:
        print("No tracker rows in this database; nothing to examine.", file=out)
        return 0

    plans_by_user: list[tuple[int, list[MonthPlan]]] = []
    for uid in users:
        plans = plan_removals(await read_months(uid))
        plans_by_user.append((uid, plans))
        print(f"\nAccount {uid}:", file=out)
        print(format_report(plans), file=out)

    if not apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply to remove the "
              "rows listed above.", file=out)
        return 0
    for uid, plans in plans_by_user:
        removed = await apply_removals(uid, plans)
        print(f"\nAccount {uid}: {removed} rows removed.", file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = sys.stdout
    # A show title can carry characters the console's code page cannot spell —
    # Windows still defaults to a legacy one — and a report that dies part-way
    # through printing is a far worse outcome than a substituted character in a
    # title. Nothing here is parsed by anything, so the substitution costs
    # nothing but the shape of one glyph.
    if hasattr(out, "reconfigure"):
        out.reconfigure(errors="replace")
    return asyncio.run(run(apply=args.apply, user_id=args.user, out=out))


if __name__ == "__main__":  # pragma: no cover — the entry point itself
    raise SystemExit(main())
