"""Which source's values one viewer sees for a title two sources may describe.

THE SECOND HALF OF A SPLIT THE CACHE FORCED. Two operations sit between "what a
source said" and "what a person sees", and they have different scopes, which is
why they happen in different places:

  MATCHING    is this record and that record the same airing? User-independent,
              so it runs at FILL and is stored (app/calendar/cache.py groups the
              records it stores).
  RESOLUTION  which source's value does THIS VIEWER see for this field? Per
              account by definition, so it runs at READ, over already-cached
              data, and changing a preference therefore invalidates nothing.

`resolve` answers the second and returns a RECORD, not a rendered Item, and that
is deliberate: the per-viewer genre/country/certification filter matches on the
raw genre slugs, so it has to run between resolution and rendering. Rendering is
`app/providers/base.py`'s `render`, the one place a stored instant becomes a
viewer's local day.

RESOLUTION IS ALSO EXCLUSION, and both halves are here for the same reason. The
window a group came out of was filled by asking every source the instance admits
and is served to every viewer from one row, so "I only want this service" cannot
be honoured while filling it without deciding for everybody else too. It is
honoured here instead: a group naming no source this viewer admits resolves to
nothing and never reaches their page, while the same row still gives the next
viewer everything they asked for.
"""
from __future__ import annotations

from ..providers.base import Record, Source
from ..sources import prefs as source_prefs


def source_order(group) -> list[str]:
    """The sources in `group`, in DECLARED order.

    Declared rather than stored order because the payload is JSON and a dict
    that came back from it holds whatever order it was written in — which would
    make "the first source" depend on which service answered first during some
    fill weeks ago.
    """
    by_source = group.get("by_source") or {}
    return [str(s) for s in Source if str(s) in by_source]


def admitted_order(group, prefs=None, linked=()) -> list[str]:
    """The sources in `group` this viewer will read, in declared order.

    THE PER-VIEWER EXCLUSION, AND IT HAS TO HAPPEN HERE. The window this group
    came out of is stored once per (endpoint, week) and served to every viewer,
    so a selection applied while filling it would write one person's exclusion
    into what everybody else reads. Applied at READ, over the union every source
    contributed, one viewer asking for one service and another asking for both
    get different answers out of the identical row — and changing the preference
    invalidates nothing, because nothing was ever stored per viewer.

    `prefs=None` means no account is asking — a public share page — and admits
    every source the group holds.

    AN ACCOUNT THAT HAS LINKED NOTHING STILL HAS A CALENDAR. 'auto' means "follow
    the links", and with no links there is nothing to follow — but a calendar
    needs no link to be worth reading: this instance's own client credentials
    fetch it, and one source's calendar files need no credential at all. Reading
    the absence of a link as a decision to see nothing would blank the calendar
    for every account on an instance whose operator configured the services
    centrally and whose users only ever sign in, which is the ordinary shape of a
    self-hosted install. Zero links is the absence of a statement, not a
    statement of "none" — a stated selection ('both', or one service by name) is
    honoured whatever is linked.
    """
    order = source_order(group)
    if prefs is None:
        return order
    if prefs.calendar_source == source_prefs.AUTO and not linked:
        return order
    return [name for name in order if prefs.admits_calendar(name, linked)]


def resolve(group, prefs=None, linked=()) -> Record | None:
    """The one record this viewer sees for `group`, or None when the group holds
    nothing this viewer reads.

    NONE IS A REAL ANSWER HERE, NOT ONLY A MALFORMED ONE. A group every source
    behind it is one this viewer excluded has nothing to show them, and dropping
    it is how "show me only this service" narrows a shared window down to the
    titles that service actually listed.

    WHICH OF SEVERAL ADMITTED SOURCES WINS IS THE DECLARED ORDER, and that is
    deliberately the whole of it for now: a per-field precedence map is a
    separate decision with its own screen behind it, and guessing at it here
    would put a second, quieter answer in the way of that one. `prefs` carries
    the map already; nothing reads it yet.

    THE IDS ARE THE GROUP'S, NOT THE RECORD'S. `ids` is hoisted onto the group at
    fill because it is the MATCH RESULT — the union of every id space any source
    named this title in — and the arr/Seerr buttons, the tracker and the ranker
    all read it. Handing back the one source's own ids would quietly narrow that
    union to whichever service happened to win.
    """
    order = admitted_order(group, prefs, linked)
    if not order:
        return None
    payload = (group.get("by_source") or {})[order[0]]
    try:
        record = Record.from_dict(payload)
    except (ValueError, TypeError, KeyError):
        return None
    ids = group.get("ids")
    if isinstance(ids, dict) and ids:
        record.ids = dict(ids)
    return record
