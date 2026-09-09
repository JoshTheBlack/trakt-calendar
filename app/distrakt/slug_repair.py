"""Recover a per-service NAME a stored record should have been written with, and
log every recovery so that the log growing is itself the alarm.

A tracker record carries each service's own name for a title — `trakt_slug`,
`simkl_slug` — beside that service's id. The id places a call; the name is what a
readable link to that service is built from, and both services ask that it be
sent when the caller has it. Every path that writes a record is handed those
names and is supposed to store them.

WHY A REPAIR EXISTS AT ALL, rather than just fixing the writers. A record missing
a name still WORKS: the link builder falls back to the numeric id, which both
services resolve, so nothing is broken and nothing complains. That makes a writer
which drops the name completely silent — the one found by the fix that created
this module had been storing records without a Trakt name for weeks, on the one
id-reading in either provider package that skipped the correction, and it was
noticed by counting rows rather than by anything going wrong.

SO THE POINT OF THE LOG IS ITS DATES, NOT ITS CONTENTS. `distrakt_slug_repairs`
records what was recovered, when, and from where. Rows dated to the first run are
the historical backlog. A row dated after that means a writer is STILL dropping a
name it was handed, and `evidence` narrows down which one. Without the log a
future regression would be as invisible as the first one was.

WHAT THIS MODULE DOES NOT DO, deliberately: the ordinary way a settled record
learns a name is `naming.fill_from_calendar`, which reads it out of a stored
calendar window for free. That is normal operation on a record written before the
two services' names were told apart, so it is NOT logged here — logging it would
fill the table with expected entries and drown the signal. This module handles
the case that path cannot: a record whose title no stored window names any more.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping

from .. import cache, db
from ..config import load_settings
from ..providers.base import ItemKey, Source
from . import store

logger = logging.getLogger(__name__)

# The names a repair may write, and what each one is owed BY. A name is owed only
# where the row holds that service's id: writing a Trakt name onto a title Trakt
# was never asked about would make the row look linkable to a service that cannot
# answer for it, which is the shape of the bug this whole area exists to prevent
# rather than a lesser version of it.
#
# TWO SPELLINGS OF ONE FACT, because the two callers address the same thing
# differently and neither may guess at the other's. A stored ROW is addressed by
# its columns (`trakt_id`); an id MAP off a payload is addressed by the id
# namespace (`trakt`). store.ID_COLUMNS is the translation between them and is
# where that pairing is defined — these two read it rather than restating it, so
# a service added there arrives here without an edit.
_OWED_BY_COLUMN = {column: f"{store.ID_COLUMNS[column]}_slug"
                   for column in ("trakt_id", "simkl_id")}
_OWED_BY_ID_KEY = {store.ID_COLUMNS[column]: slug
                   for column, slug in _OWED_BY_COLUMN.items()}
# Every name a repair is allowed to write, in either spelling's terms.
_WRITABLE = frozenset(_OWED_BY_COLUMN.values())

# Where a recovered name came from. Each value names a DIFFERENT bug when it
# shows up late, which is why the log stores it rather than a bare "repaired".
EVIDENCE_SHARED_SLUG = "shared_slug"      # a writer stored the old unattributed column
EVIDENCE_DETAIL_LOOKUP = "detail_lookup"  # a read had the name in hand and the row did not
EVIDENCE_SOURCE_LOOKUP = "source_lookup"  # nothing stored knew it; the service was asked


async def record(user_id: int, key: ItemKey, column: str, value: str,
                 evidence: str, rows_changed: int, title: str = "") -> None:
    """Log one recovered name. Never raises — see below.

    A REPAIR THAT FAILS TO LOG MUST NOT FAIL THE REPAIR. This is called from an
    ordinary page read and from background maintenance, and in both the useful
    work is the name landing on the record; the log is how somebody finds out
    later that it was needed. Losing a log line costs a diagnostic, and raising
    here would cost the viewer their modal.
    """
    try:
        await db.execute(
            "INSERT INTO distrakt_slug_repairs "
            "(user_id, media, match_source, match_id, column_name, value, "
            " evidence, rows_changed, title, repaired_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, key.media, key.match_source, key.match_id, column,
             str(value), evidence, int(rows_changed), str(title or ""), db.now()),
        )
    except Exception:
        logger.warning("Could not log a recovered service name for user %s", user_id,
                       exc_info=True)


async def learn_and_log(user_id: int, key: ItemKey, names: Mapping[str, str],
                        evidence: str, title: str = "") -> int:
    """Fill in `names` on every stored row of one title, logging each one.

    Returns how many stored rows changed. `store.learn_ids` is the writer rather
    than an UPDATE here, and that matters for two reasons it already documents:
    it never overwrites a value that is present, and it addresses the IDENTITY
    rather than a season, so one call teaches every month and every roster row of
    the same title at once.

    EACH NAME IS WRITTEN AND LOGGED SEPARATELY even though `learn_ids` would take
    both at once, because the log's unit is "one name that was missing" — a title
    short of Trakt's name and holding Simkl's is one bug, not two, and lumping
    them would make the count unreadable.
    """
    total = 0
    for column, value in (names or {}).items():
        if column not in _WRITABLE or not value:
            continue
        # ID_COLUMNS maps a column to the id-map key `learn_ids` reads it under,
        # and for these two the two spellings are the same — but going through
        # the same map the writer uses is what keeps this from being a second
        # place that decides what a column is called.
        changed = await store.learn_ids(user_id, key, {column: value})
        total += changed
        await record(user_id, key, column, value, evidence, changed, title)
    if total:
        logger.info("distrakt: recovered %d stored row(s) short of a service name "
                    "for %s (%s)", total, key, evidence)
    return total


def _text(row, name: str) -> str:
    """One field off a stored row, as trimmed text, empty when it is not set.

    A sqlite3.Row has no `.get`, and the two callers hand over a Row and a plain
    dict respectively, so the read goes through here rather than through a
    `try`/`except KeyError` at each site.
    """
    try:
        value = row[name]
    except (KeyError, IndexError):
        return ""
    return "" if value is None else str(value).strip()


def recoverable_names(stored_ids: Mapping, fresh_ids: Mapping) -> dict[str, str]:
    """The service names a fresh read is holding that a stored record is short of.

    THIS IS THE OTHER HALF OF THE SAME BUG and it is worth stating plainly: a
    detail read asks a service about a title and gets that service's own name
    back in the answer. A record short of that name is being looked at, from a
    payload containing it, and putting the two together is free. It was not
    happening — the read preferred what the record already held, so the name it
    had just been given was displayed as absent and then discarded.

    ONLY WHAT THE RECORD IS OWED AND LACKS, so an ordinary read of a complete
    record produces {} and writes nothing. That emptiness is what keeps the
    repair log meaningful: a row in it means something really was missing.
    """
    out: dict[str, str] = {}
    for id_key, slug_key in _OWED_BY_ID_KEY.items():
        if not _text(stored_ids, id_key):
            continue  # the record cannot be linked to this service at all
        if _text(stored_ids, slug_key):
            continue  # already knows what this service calls it
        if value := _text(fresh_ids, slug_key):
            out[slug_key] = value
    return out


def attribute_shared_slug(row: Mapping) -> dict[str, str]:
    """The namespaced name a row's OLD shared `slug` column can be proven to hold,
    or {} when nothing about the row proves it.

    THE COLUMN IS UNATTRIBUTED BY CONSTRUCTION, which is the whole reason it was
    replaced: both services call a title's readable name `slug`, they disagree
    about it ("the-rookie-2018" against "the-rookie"), and whichever service
    synced last won. So a value here cannot be handed to a service on the
    strength of being present.

    WHAT DOES PROVE IT is that the column only ever had two writers. If the row
    holds a Trakt id, and the shared value is NOT the Simkl name the row already
    carries, then Simkl did not write it and Trakt did. Measured against the live
    Trakt API over every affected title on the author's instance — 22 of 22
    titles, no mismatches — which is what makes this a derivation rather than the
    guess the original split refused to make.

    IT WILL NOT GUESS THE OTHER DIRECTION. A row with no Simkl name stored has
    nothing to be different FROM, so there is no evidence and this answers {} —
    even though the same instance's measurements happened to come out Trakt's
    there too. A wrong name is strictly worse than none: none falls back to the
    numeric id and works, while a wrong one builds a link to a title the service
    has never heard of, which is the live 404 the split was made to fix.
    """
    shared = _text(row, "slug")
    if not shared:
        return {}
    simkl_name = _text(row, "simkl_slug")
    trakt_name = _text(row, "trakt_slug")
    has_trakt = bool(_text(row, "trakt_id"))
    # Only the Trakt direction is derivable, and only against a Simkl name that
    # is present and different. The mirror case — proving a value is Simkl's from
    # a stored Trakt name — is deliberately absent: Simkl's own library read
    # hands over its name for every title at once, so a Simkl name missing from a
    # row is not the standing condition Trakt's is.
    if trakt_name or not has_trakt or not simkl_name or simkl_name == shared:
        return {}
    return {"trakt_slug": shared}


async def _rows_owing_a_name(user_id: int) -> list:
    """Stored rows that hold a service's id, no name for it, and an old shared
    value that might be recoverable.

    BOTH RECORD TABLES, because a settled verdict is exactly where this bites:
    its counts are never recomputed, so no live pass revisits it, and a record
    that settled before the names were told apart has no other way to learn one.
    """
    missing = " OR ".join(
        f"(COALESCE({id_column},'') <> '' AND COALESCE({slug_column},'') = '')"
        for id_column, slug_column in _OWED_BY_COLUMN.items())
    sql = " UNION ALL ".join(
        f"SELECT DISTINCT media, match_source, match_id, slug, trakt_slug, "
        f"simkl_slug, trakt_id, simkl_id, title FROM {table} "
        f"WHERE user_id = ? AND COALESCE(slug,'') <> '' AND ({missing})"
        for table in ("distrakt_month_records", "distrakt_user_seasons"))
    return await db.fetch_all(sql, (user_id, user_id))


async def repair_user(user_id: int) -> int:
    """Recover every name this viewer's records can be PROVEN to be short of,
    from evidence already stored. Returns how many stored rows changed.

    NO OUTBOUND CALL, and that is what lets this sit in background maintenance
    on every tick without a budget of its own: the evidence is a column already
    on the row. A title whose name cannot be derived is left alone rather than
    looked up, because the cost of asking scales with the roster while the number
    of genuinely stuck titles does not.
    """
    changed = 0
    seen: set[tuple[str, str, str]] = set()
    for row in await _rows_owing_a_name(user_id):
        address = (str(row["media"]), str(row["match_source"]), str(row["match_id"]))
        if address in seen:
            continue  # one identity, however many of its rows came back
        seen.add(address)
        names = attribute_shared_slug(row)
        if not names:
            continue
        key = ItemKey(*address)
        changed += await learn_and_log(user_id, key, names, EVIDENCE_SHARED_SLUG,
                                       str(row["title"] or ""))
    return changed


# A viewer's "nothing further to find by asking" marker, and how long it holds.
# THE SIGNATURE IS THE MECHANISM AND THE TTL IS THE BACKSTOP, which is
# `naming.fill_from_calendar`'s arrangement and is reused rather than reinvented:
# the work is a function of what is owed, so an unchanged owed set cannot have a
# new answer. The TTL only bounds how long a stale marker could suppress a pass —
# a day, so a name a service starts publishing is learned a day late rather than
# never.
_ASKED_KEY = "distrakt_slug_asked:{user_id}"
_ASKED_TTL_SECONDS = 24 * 60 * 60

# How many titles one pass may ASK about. The derivable half above is free and
# unbounded; this half spends a request per title, so it is drained a few at a
# time across ticks rather than in one burst on the first heartbeat after an
# upgrade. The backlog this exists for is a handful of titles on a real account,
# so the ceiling is about not stampeding a service after a restore or a big
# import rather than about the ordinary case.
_ASK_LIMIT_PER_PASS = 5


async def _ask_source_for_names(settings, user_id: int, row) -> dict[str, str]:
    """What the services themselves call a title, for the names this row lacks.

    NO NEW HTTP CODE, AND DELIBERATELY SO. `detail_source.fetch` is already "ask
    this source about this title, by that source's own id" — it routes through
    the source's own DetailPort, which uses that package's pooled, rate-limited,
    globally cached transport, and (for Simkl) follows the anime redirect that
    transport owns. A second way to make the same request would be a second set
    of those behaviours to keep correct.

    ASKED OF EACH SERVICE SEPARATELY, because the question is per service: only
    Trakt can say what Trakt calls a title. A row holding both ids and short of
    both names is two questions, and a row short of one is one.
    """
    from ..calendar import detail_source  # deferred: see this package's other
                                          # calendar reads, same import cycle

    found: dict[str, str] = {}
    for id_column, slug_column in _OWED_BY_COLUMN.items():
        if _text(row, slug_column) or not (source_id := _text(row, id_column)):
            continue
        source = store.ID_COLUMNS[id_column]
        try:
            answer = await detail_source.fetch(
                settings, Source(source), str(row["media"]), source_id, None)
        except Exception:
            # A service that cannot answer right now is not a reason to fail the
            # pass, or to lose the names another service did give. The marker
            # below still holds this viewer off for a day, so a service that is
            # down is asked again tomorrow rather than every minute.
            logger.info("distrakt: %s could not be asked for a name for %s",
                        source, row["match_id"], exc_info=True)
            continue
        if value := _text((answer or {}).get("ids") or {}, slug_column):
            found[slug_column] = value
    return found


async def repair_by_asking(settings, user_id: int) -> int:
    """Recover names no stored evidence can prove, by asking the service that
    owns them. Returns how many stored rows changed.

    THIS IS THE HALF `attribute_shared_slug` REFUSES TO GUESS AT. A row with no
    second name to differ from cannot be read locally without risking a link to a
    title the service has never heard of — but the service itself can simply be
    asked, and its answer is not a guess at all. That is the whole difference
    between this and the derivation above, and it is why one is free and this one
    is rationed.
    """
    owed = [row for row in await _rows_owing_a_name(user_id)
            if not attribute_shared_slug(row)]
    if not owed:
        return 0
    # Nothing owed has changed since the last pass, so nothing can have a new
    # answer — see the note on _ASKED_KEY.
    signature = _owed_signature(owed)
    marker = _ASKED_KEY.format(user_id=int(user_id))
    if await cache.get(marker, _ASKED_TTL_SECONDS) == signature:
        return 0
    changed = 0
    seen: set[tuple[str, str, str]] = set()
    for row in owed:
        if len(seen) >= _ASK_LIMIT_PER_PASS:
            # Deliberately WITHOUT writing the marker: there is known work left,
            # and the next tick should pick it up rather than wait a day.
            return changed
        address = (str(row["media"]), str(row["match_source"]), str(row["match_id"]))
        if address in seen:
            continue
        seen.add(address)
        if names := await _ask_source_for_names(settings, user_id, row):
            changed += await learn_and_log(
                user_id, ItemKey(*address), names, EVIDENCE_SOURCE_LOOKUP,
                str(row["title"] or ""))
    # Recorded against what was owed BEFORE the writes, so the next pass compares
    # like with like — `naming.fill_from_calendar` records its own the same way
    # and for the same reason.
    await cache.set(marker, signature)
    return changed


def _owed_signature(rows) -> str:
    """A stable digest of exactly which titles are still owed a name.

    Hashed rather than kept whole because the value is only ever compared:
    nothing needs to know WHICH titles it stood for, only whether the set is the
    one the last pass already asked about.
    """
    owed = sorted(f"{row['media']}:{row['match_source']}:{row['match_id']}"
                  for row in rows)
    return hashlib.sha256("\n".join(owed).encode()).hexdigest()[:16]


async def repair_all() -> int:
    """One maintenance pass over every account that has tracker rows.

    DRIVEN OFF THE RECORDS RATHER THAN THE USER LIST, so an instance with many
    accounts and one tracker user does the work of one. The query is two indexed
    reads and answers nothing on a settled instance, which is every pass after
    the first.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT user_id FROM ("
        "  SELECT user_id FROM distrakt_month_records WHERE COALESCE(slug,'') <> ''"
        "  UNION SELECT user_id FROM distrakt_user_seasons WHERE COALESCE(slug,'') <> ''"
        ")", ())
    settings = load_settings()
    total = 0
    for row in rows:
        user_id = int(row["user_id"])
        # THE FREE HALF FIRST, ALWAYS. Deriving a name from a column already on
        # the row costs nothing and shrinks what the second half has to ask
        # about, so a title both could answer for is never paid for.
        total += await repair_user(user_id)
        total += await repair_by_asking(settings, user_id)
    return total
