"""What each viewer's two posts were last rendered from, per month, in process.

FOR ONE QUESTION: what do the posts say once a network's emoji code has changed?
The code is edited on a row, and the posts are what somebody edits codes in order
to copy — so the answer to a save has to carry both posts redrawn. Everything
else in them (which seasons, what counts, which link) is what the month was last
worked out to hold, and working it out again costs a watch-history read and a
season lookup per listed title, to change nothing but the emoji. So the month's
render inputs are kept here as they were last built, and a save renders the posts
from them with the new map (discord_fmt.render_posts).

THE LAST BUILD OF THAT MONTH FOR THAT VIEWER, whichever tab asked for it. Every
answer that changes what a month holds rebuilds it and so replaces what is kept
here, which makes the kept inputs at least as new as the page the save came from.

IN PROCESS AND BOUNDED, and allowed to be empty. A restart, or a month nobody has
drawn since, simply finds nothing — and the caller then asks the page to fetch
its month again, the answer that was used before this existed. It is never
handed to anybody but the viewer it was built for: the key is theirs.
"""
from __future__ import annotations

from collections import OrderedDict

from .. import db
from .discord_fmt import PostInputs

# How many (viewer, month) builds are kept. A viewer looks at a handful of months
# at a time and an instance has a handful of viewers; the oldest is dropped first.
LIMIT = 64

_KEPT: OrderedDict[tuple[int, int, str], PostInputs] = OrderedDict()


def _key(user_id: int, month_key: str) -> tuple[int, int, str]:
    # WHICH DATABASE, as well as whose month: a user id is only a viewer within
    # one database (db.generation says why a memo has to ask).
    return db.generation(), int(user_id), str(month_key)


def remember(user_id: int, month_key: str, inputs: PostInputs) -> None:
    key = _key(user_id, month_key)
    _KEPT[key] = inputs
    _KEPT.move_to_end(key)
    while len(_KEPT) > LIMIT:
        _KEPT.popitem(last=False)


def recall(user_id: int, month_key: str) -> PostInputs | None:
    return _KEPT.get(_key(user_id, month_key))


def forget_all() -> None:
    """Drop everything kept — for a test that needs a cold process."""
    _KEPT.clear()
