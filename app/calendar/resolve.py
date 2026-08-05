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

WITH ONE SOURCE BEHIND A GROUP THERE IS NOTHING TO RESOLVE, and this module says
so in one line rather than not existing: the seam is what the fill path and the
read path are written against, and a second source arriving should change what
happens INSIDE this function rather than add a call to it somewhere.
"""
from __future__ import annotations

from ..providers.base import Record, Source


def source_order(group) -> list[str]:
    """The sources in `group`, in DECLARED order.

    Declared rather than stored order because the payload is JSON and a dict
    that came back from it holds whatever order it was written in — which would
    make "the first source" depend on which service answered first during some
    fill weeks ago.
    """
    by_source = group.get("by_source") or {}
    return [str(s) for s in Source if str(s) in by_source]


def resolve(group, prefs=None) -> Record | None:
    """The one record this viewer sees for `group`, or None when the group holds
    nothing readable.

    `prefs` is the account's stored source preference and is accepted here from
    the start so that the read path is already written in terms of "resolve this
    for this viewer"; with a single source in a group there is exactly one answer
    and no preference can change it.

    THE IDS ARE THE GROUP'S, NOT THE RECORD'S. `ids` is hoisted onto the group at
    fill because it is the MATCH RESULT — the union of every id space any source
    named this title in — and the arr/Seerr buttons, the tracker and the ranker
    all read it. Handing back the one source's own ids would quietly narrow that
    union to whichever service happened to win.
    """
    order = source_order(group)
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
