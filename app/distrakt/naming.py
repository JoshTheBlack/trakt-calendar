"""Teach stored tracker records the per-service NAMES the calendar already knows.

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

THE SOURCE IS ALREADY PAID FOR. Every stored calendar window holds each title's
ids hoisted onto the group, both services' slugs among them, and reading them
back costs one query and no network at all — Simkl's calendar is CDN-hosted and
outside the API quota entirely, and Trakt's window was fetched for the calendar's
own sake. So this is a pure re-read of what the instance already went and got.
`app/calendar/enrich.py`'s drain derives its work from the same place for the
same reason: what the stored calendar names is knowable without any viewer having
read anything.

ADD-ONLY, AND THAT IS `store.learn_ids`' RULE RATHER THAN THIS MODULE'S. A value
already stored came off the payload the record was built from; a value arriving
here has been matched across services on the shared identity, which is a join and
not a statement. Filling a blank cannot make a working record worse. Overwriting
could.
"""
from __future__ import annotations

import hashlib
import logging

from .. import cache
from ..calendar import cache as calendar_cache
from ..calendar import entries as calendar_entries
from ..providers.base import ItemKey
from . import store

logger = logging.getLogger(__name__)

# The names this fills in, and ONLY these. A calendar group's hoisted `ids` also
# carries the shared spaces a record is KEYED on (tmdb, tvdb, imdb, mal) — and
# improving one of those is a different question with different consequences,
# because the record is filed under it. A match arriving from a calendar window
# has no business anywhere near the identity waterfall; it may only fill in what
# a title is CALLED on a service, never what it IS.
LEARNABLE = ("trakt_slug", "simkl_slug")

# Where a viewer's "nothing further to find" marker is kept, and for how long.
# THE TTL IS A BACKSTOP, NOT THE MECHANISM: the signature is what makes a repeat
# pass unnecessary, and this only bounds how long a stale marker could suppress
# one if the signature ever stopped moving when it should. A day is short enough
# that a missed name is a day late rather than permanent, and long enough that
# the walk is not paid for again and again inside one session.
_SETTLED_KEY = "distrakt_naming_settled:{user_id}"
_SETTLED_TTL_SECONDS = 24 * 60 * 60


def _owed_signature(wanted: list[tuple[ItemKey, tuple[str, ...]]]) -> str:
    """A stable digest of exactly what is outstanding, order-independent.

    Hashed rather than stored whole because a large account owes a long list and
    this value is compared, never read: nothing downstream needs to know WHICH
    titles it stood for, only whether the set is the one seen last time.
    """
    owed = sorted(f"{key}={'|'.join(names)}" for key, names in wanted)
    return hashlib.sha256("\n".join(owed).encode()).hexdigest()[:16]



async def fill_from_calendar(user_id: int) -> int:
    """Fill in missing service slugs on this viewer's stored records. Returns how
    many names were written — one row gaining both services' slugs counts twice —
    which is 0 on every pass after the work is done.

    CHEAP WHEN THERE IS NOTHING TO FIND, which on a settled instance is every
    call, and the reason this can sit on an ordinary page load. Two questions are
    asked before any window is inflated — what this viewer is short of, and
    whether that plus the stored calendar has changed since the last pass — and
    both are counts. Measured on the author's database, the walk they guard is
    about 40ms and the guards about 1ms.

    NOT GATED ON THE ROSTER. Reaching only listed titles is what left settled
    verdicts out in the first place — the rows that most need this are exactly
    the ones no live pass visits.
    """
    wanted = await store.identities_missing_slugs(user_id)
    if not wanted:
        return 0
    # SOME DEBT NEVER SETTLES, and that is why the guard above is not enough on
    # its own. A title no stored window names — an older show, something off the
    # calendar's horizon — is owed a name for as long as it is held, so "is
    # anything outstanding" answers yes for ever on a real account and the walk
    # below would run on every single load to rediscover the same nothing.
    #
    # NEITHER SIDE MOVED MEANS THE ANSWER CANNOT HAVE. The work is a function of
    # exactly two things: what is owed, and what the stored calendar knows. When
    # both are as they were the last time this ran, there is nothing to find.
    signature = f"{_owed_signature(wanted)}/{await calendar_cache.stored_window_signature()}"
    settled_key = _SETTLED_KEY.format(user_id=int(user_id))
    if await cache.get(settled_key, _SETTLED_TTL_SECONDS) == signature:
        return 0
    # ONLY THE TITLES OWED SOMETHING. This used to build an index of the whole
    # stored calendar to answer for a handful of them — measured at 3.8 seconds
    # to learn two names, most of it blocking the event loop. `title_key` is the
    # same identity the tracker keys its rows by and it is indexed, so the
    # question is a keyed read.
    index = await calendar_entries.slugs_for(str(key) for key, _owed in wanted)
    if not index:
        await cache.set(settled_key, signature)
        return 0
    changed = 0
    for key, owed in wanted:
        known = index.get(str(key)) or {}
        # ONLY THE NAMES THAT TITLE IS ACTUALLY SHORT OF. The index holds whatever
        # the calendar knew, which for a title both services list is both names —
        # and writing the other one onto a row that holds no id for that service
        # would make it look linkable to a service nobody can ask about it.
        learned = {name: known[name] for name in owed if name in known}
        if learned:
            changed += await store.learn_ids(user_id, key, learned)
    if changed:
        logger.info("distrakt naming: filled %d service name(s) for user %s from "
                    "the stored calendar", changed, user_id)
    # RECORDED AFTER THE WRITES, against what was owed BEFORE them, so the next
    # pass compares like with like: whatever this one could pay is paid, and the
    # rest is the residue this exact pair of inputs leaves behind.
    await cache.set(settled_key, signature)
    return changed
