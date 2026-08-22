"""A service that has stopped listing a title, recorded rather than acted on.

A DELTA READ CANNOT SEE A REMOVAL. Simkl says so outright: `date_from` returns
what changed, and a title that is gone does not change — it simply stops being
mentioned. The prescribed detection is to re-read the library with
`extended=simkl_ids_only`, a payload of nothing but ids, and diff it against what
is held locally.

WHAT IS PRESCRIBED NEXT IS DELETING THE LOCAL ROWS, AND THIS APP DOES NOT DO IT.
The two mistakes are not symmetrical. Watch history is not re-derivable from
anything here — no other record of it exists — so a wrong deletion is permanent,
silent, and discovered long after the read that caused it. A wrong MARK is
visible on the row, costs nothing, and is undone by the next read that names the
title. Given a diff that can be wrong for reasons outside this app's control (a
sync hiccup, a re-catalogued title, a bucket that failed in a way the partial-read
logic did not catch), recording the service's statement is the only version of
this that a viewer can recover from.

SO NOTHING HERE DELETES ANYTHING. The mark rides on the viewer's own list, rolls
forward month to month, clears itself when the service names the title again, and
is removed for good only by the viewer purging the row.
"""
from __future__ import annotations

import logging

from ..providers.base import ItemKey
from . import store

logger = logging.getLogger(__name__)


def _held_by_us(records: list[dict], source: str) -> dict[int, list[ItemKey]]:
    """{that service's id: the identities holding it} across the viewer's list.

    KEYED ON THE SERVICE'S OWN ID because that is the only thing the ids-only
    payload carries. It names no shared id space, so the identity waterfall has
    nothing to run on and a match has to be made on the one id both sides have.
    A row that does not carry this service's id was never listed there and cannot
    be missing from it.
    """
    out: dict[int, list[ItemKey]] = {}
    for record in records or []:
        raw = (record.get("ids") or {}).get(source)
        if raw in (None, ""):
            continue
        try:
            out.setdefault(int(raw), []).append(store.record_key(record))
        except (TypeError, ValueError):
            continue
    return out


def _may_believe(ours: dict, held: dict) -> bool:
    """Whether a library listing may be believed about what is ABSENT from it.

    THE SAME SHAPE AS watch_history._may_retire_rows, AND DELIBERATELY SO. What
    makes a listing obviously wrong is not how many titles it names but that it
    names NOT ONE of the titles this viewer is known to hold there — a read like
    that has demonstrated nothing about the library it claims to describe, and
    believing it would mark an entire tracker as gone from a single answer.

    The failure it guards against is milder here than there, because nothing is
    deleted: the cost is a page covered in marks that are all wrong, which is
    useless and alarming rather than destructive. It is still worth refusing.

    A viewer who genuinely holds nothing at the service reaches this too, and
    keeps their rows unmarked until they add one thing back. That is a wrong
    answer they caused and can see.
    """
    return bool(held) and bool(set(ours) & set(held))


async def check(settings, user_id: int, source, port) -> int:
    """Ask one service what it still holds and record what it no longer does.
    Returns how many rows changed.

    RUN ONLY WHEN THE REMOVAL BEACON HAS MOVED. `/sync/activities` carries a
    `removed_from_list` stamp per catalogue, and the caller gates on it — so this
    costs nothing on an ordinary pass and runs on the rare one where the service
    has said outright that something was taken away.

    A LISTING THAT COULD NOT BE READ WHOLE IS NOT DIFFED. `fetch_library_ids`
    answers None rather than a short list for exactly this: every title in an
    unread bucket is absent from the answer, and absence is the entire signal.
    """
    held = await port.fetch_library_ids(settings)
    if held is None:
        return 0
    name = str(source)
    records = await store.user_records(user_id)
    ours = _held_by_us(records, name)
    if not ours:
        return 0
    if not _may_believe(ours, held):
        logger.error(
            "distrakt removals: refusing to mark %s's titles missing — its "
            "library listing named none of the %d title(s) this viewer holds "
            "there. Nothing is marked and the next pass asks again.",
            name, len(ours))
        return 0

    # WHAT EACH IDENTITY CURRENTLY SAYS, so a service being added to or removed
    # from the list leaves the OTHER service's answer untouched. A row marked
    # missing at Simkl and still held at Trakt has to keep saying both.
    current: dict[str, set[str]] = {}
    for record in records:
        current.setdefault(str(store.record_key(record)),
                           set(record.get("missing_sources") or []))
    changed = 0
    for service_id, keys in ours.items():
        gone = service_id not in held
        for key in keys:
            was = current.get(str(key), set())
            now = (was | {name}) if gone else (was - {name})
            if now == was:
                continue
            changed += await store.set_missing_sources(user_id, key, now)
            current[str(key)] = now
    if changed:
        logger.info("distrakt removals: %s no longer lists %d of %d title(s) this "
                    "viewer holds there; %d row(s) updated",
                    name, sum(1 for i in ours if i not in held), len(ours), changed)
    return changed
