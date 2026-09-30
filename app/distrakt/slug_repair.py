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
records what was recovered, when, and from where. A row dated recently means a
writer is STILL dropping a name it was handed, and `evidence` narrows down which
one. Without the log a future regression would be as invisible as the first one
was. Older rows also carry `shared_slug` and `source_lookup`: those were a
one-time backlog drain for records written before the two services' names were
told apart, which ran until nothing was owed and went with the shared column.

WHAT THIS MODULE DOES NOT DO, deliberately: the ordinary way a settled record
learns a name is `naming.fill_from_calendar`, which reads it out of a stored
calendar window for free. That is normal operation, so it is NOT logged here —
logging it would fill the table with expected entries and drown the signal.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from .. import db
from ..providers.base import ItemKey
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

# Where a recovered name came from, which is why the log stores it rather than a
# bare "repaired": a read had the name in hand and the row did not.
EVIDENCE_DETAIL_LOOKUP = "detail_lookup"


async def record(user_id: int, key: ItemKey, column: str, value: str,
                 evidence: str, rows_changed: int, title: str = "") -> None:
    """Log one recovered name. Never raises — see below.

    A REPAIR THAT FAILS TO LOG MUST NOT FAIL THE REPAIR. This is called from an
    ordinary page read, and there the useful work is the name landing on the
    record; the log is how somebody finds out later that it was needed. Losing a
    log line costs a diagnostic, and raising here would cost the viewer their
    modal.
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
    """One field off an id map, as trimmed text, empty when it is not set."""
    value = row.get(name)
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
