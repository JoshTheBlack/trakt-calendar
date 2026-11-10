"""Teach stored tracker records what the calendar already knows about their
titles: each service's id, each service's name for the title, the shared ids,
and the network — by the same rules a newly arriving record is described by
(app/distrakt/arrival.py), so a repaired row and a new one carry the same words.

A record is keyed on a shared identity and carries, beside that, each service's
own id and each service's own slug. The id is what places a call; the slug is
what a link to that service is built from, and both services ask that the slug
be sent when the caller has it — resolving a bare numeric id costs them a title
lookup and a redirect they need not have done.

TWO KINDS OF RECORD NEVER LEARNED ONE, and neither gap is reachable from the
sync path that fills the rest in:

  A SETTLED VERDICT IS OUTSIDE THE LIVE PASS. Its counts are decided once and
  never recomputed, which is the whole point of settling — so the pass that
  teaches a roster record an id it was written without never visits it. A season
  finished last March could carry an id for years and still have no slug, and
  the only way it ever picked one up was another season of the same title
  happening to still be listed.

  TRAKT'S SLUG RIDES HISTORY EVENTS. It arrives when a play does, so a title
  nobody has watched recently goes without one indefinitely — where Simkl's
  arrives for every title at once off a library read.

AND A ROW FILED OFF ONE SERVICE NEVER LEARNED THE OTHER'S ID. The two services
catalogue a title at different times and a row keeps whatever ids existed on the
day it was written — a calendar import carries the ids of whichever service
listed that airing, and the history backfill those of the one service it
surveys. Nothing revisited a settled row to learn more, while the stored calendar
went on holding the other service's OWN record of the same title. Likewise a
history-built row has no network, and rows written before the whole id map was
kept have no tvdb or imdb; the calendar usually knows both. A SHARED id is only
ever ADDED beside the one the row is filed under, never in place of it: the
filing id is what addresses the row, so the row stays exactly where it was.

THE SOURCE IS ALREADY PAID FOR. The title store (app/metadata/rows.py) holds one
row per service per title, keyed by the same identity the tracker files rows
under, and reading it
costs one indexed query and no network at all. So this is a pure re-read of what
the instance already went and got. `app/calendar/enrich.py`'s drain derives its
work from the same place for the same reason.

WHY A LEARNED ID CAN BE TRUSTED. The calendar row it comes from is that service's
own record, and it sits under this identity because THAT SERVICE states the same
shared id the tracker row is filed under — an exact match on a shared id, the
same standard a catalogue lookup would have to meet, and the same match the
calendar itself merges the two services' cards on. Never a title search.

ADD-ONLY, AND THAT IS `store.learn_ids`' RULE RATHER THAN THIS MODULE'S. A value
already stored came off the payload the record was built from; a value arriving
here is a join. Filling a blank cannot make a working record worse. Overwriting
could — and a wrong id for an earlier service shadows a correct one for a later
service, because the details layer asks the first declared service the record
has an id for.
"""
from __future__ import annotations

import hashlib
import logging

from .. import cache
from ..calendar import cache as calendar_cache
from ..metadata import rows as title_rows
from . import arrival, store

logger = logging.getLogger(__name__)

# Where a viewer's "nothing further to find" marker is kept, and for how long.
# THE TTL IS A BACKSTOP, NOT THE MECHANISM: the signature is what makes a repeat
# pass unnecessary, and this only bounds how long a stale marker could suppress
# one if the signature ever stopped moving when it should. A day is short enough
# that a missed name is a day late rather than permanent, and long enough that
# the walk is not paid for again and again inside one session.
_SETTLED_KEY = "distrakt_naming_settled:{user_id}"
_SETTLED_TTL_SECONDS = 24 * 60 * 60


def _owed_signature(wanted: list[store.Owed]) -> str:
    """A stable digest of exactly what is outstanding, order-independent.

    Hashed rather than stored whole because a large account owes a long list and
    this value is compared, never read: nothing downstream needs to know WHICH
    titles it stood for, only whether the set is the one seen last time.
    """
    owed = sorted(f"{debt.key}={'|'.join(debt.names)}" for debt in wanted)
    return hashlib.sha256("\n".join(owed).encode()).hexdigest()[:16]


async def fill_from_calendar(user_id: int, settings) -> int:
    """Fill in what this viewer's stored records are short of from the stored
    calendar: missing service ids with their names, missing names, a blank
    network. Returns how many row values were written, which is 0 on every pass
    after the work is done.

    CHEAP WHEN THERE IS NOTHING TO FIND, which on a settled instance is every
    call, and the reason this can sit on an ordinary page load. Two questions are
    asked before anything is read — what this viewer is short of, and whether
    that plus the stored calendar has changed since the last pass.

    NOT GATED ON THE ROSTER. Reaching only listed titles is what left settled
    verdicts out in the first place — the rows that most need this are exactly
    the ones no live pass visits.
    """
    wanted = await store.identities_owing(user_id)
    if not wanted:
        return 0
    # SOME DEBT NEVER SETTLES, and that is why the guard above is not enough on
    # its own. A title the stored calendar holds no row for from a service is
    # owed that service's id for as long as it is held, so "is anything
    # outstanding" answers yes for ever on a real account.
    #
    # NEITHER SIDE MOVED MEANS THE ANSWER CANNOT HAVE — for THIS pass, which reads
    # nothing but the stored calendar. Its work is a function of exactly two
    # things, what is owed and what the stored calendar knows, so when both are as
    # they were there is nothing to find.
    # DO NOT CARRY THIS GUARD INTO A REPAIR THAT ASKS A SERVICE. There the answer
    # changes when the UPSTREAM catalogue does, with nothing local changing at all
    # — a title not listed today is listed next week — and a marker keyed on what
    # is owed would ask once, find nothing and freeze the backlog for ever. That
    # repair wants a re-ask clock per row and per service instead.
    signature = f"{_owed_signature(wanted)}/{await calendar_cache.stored_window_signature()}"
    settled_key = _SETTLED_KEY.format(user_id=int(user_id))
    if await cache.get(settled_key, _SETTLED_TTL_SECONDS) == signature:
        return 0
    # ONLY THE TITLES OWED SOMETHING, as a keyed read on `title_key` — the same
    # identity the tracker keys its rows by, and indexed. This once built an index
    # of the whole stored calendar to answer a handful of titles: 3.8 seconds to
    # learn two names, most of it blocking the event loop.
    index = await title_rows.known_about(str(debt.key) for debt in wanted)
    changed = 0
    for debt in wanted:
        by_source = index.get(str(debt.key)) or {}
        # THE SAME RULE A NEWLY ARRIVING RECORD IS DESCRIBED BY
        # (arrival.calendar_teaches), so a repaired row and a new one carry the
        # same ids: each service's own id with its name, and shared ids, only
        # ever added.
        taught = arrival.calendar_teaches(debt.ids, by_source)
        # AND ONE TITLE'S ROWS AGREE: a value any row of it already holds is
        # every row's, so a settled row filed before a sibling learned an id is
        # given it too. Only what is actually owed is written.
        known = {**debt.ids, **taught}
        fill = {name: value for name, value in known.items() if name in debt.names}
        if fill:
            wrote = await store.learn_ids(user_id, debt.key, fill)
            if wrote and taught:
                # EVERY LEARNED ID IS LOGGED, BY NAME. An id arrived here by a
                # join, and a wrong one would shadow a right one for good; a line
                # per title is what makes one auditable after the fact.
                logger.info("distrakt naming: user %s learned %s for %s from the "
                            "stored calendar (%d row value(s))", user_id,
                            taught, debt.key, wrote)
            changed += wrote
        if store.NETWORK in debt.names:
            stored = arrival.stored_facts(
                settings, {"media": debt.key.media, "ids": known}, by_source)
            network = debt.network or (stored.network if stored else "")
            changed += await store.learn_network(user_id, debt.key, network)
    # RECORDED AFTER THE WRITES, against what was owed BEFORE them, so the next
    # pass compares like with like: whatever this one could pay is paid, and the
    # rest is the residue this exact pair of inputs leaves behind.
    await cache.set(settled_key, signature)
    return changed
