"""A big day is drawn in pages: `DAY_PAGE_CARDS` of its cards with the day, and
a control that asks for the rest of it when pressed.

WHY. An unfiltered All Episodes day holds ~600 cards, and every one of them is
markup the browser parses and holds whether or not anybody scrolls past it:
MEASURED, an unfiltered October was 258 MB of HTML and ~1.9 million elements,
which is the multi-gigabyte tab. PAGING, NEVER CAPPING: the day's count, its chip
and its is-new marks stay the whole day's (the shell filed the whole month), and
nothing is hidden that a press does not bring back.

WHICH CARDS THE FIRST PAGE DRAWS IS A RANKING, not the day's first hundred by
time (`first_page`): first the cards every one of the viewer's services lists,
then those all but one lists, and so on down to the cards one service alone
lists — and among cards listed alike, the one whose card shows more of its title
first (query.View.complete, counted for a paged day alone by query.ranked), then
the day's own order. A day's first hundred by time were whatever aired at midnight;
the cards several catalogues agree on are the ones a reader is most likely
looking for. Both pages are drawn in the day's own order.

THE CONTROL CARRIES ONE SIGNED TOKEN (app/signed_tokens.py), and it is checked
for the account: whose page drew it, which calendar and day, WHICH OF THE DAY'S
CARDS the page drew (their places in the day), the day those places are places
IN (`cards_digest`), and the references a late day carries — the source
override, the is-new token, the page token. The places are carried rather than
worked out again because the ranking reads what services have said, and the
page's own view queues descriptions behind it: ranked again at the press, a card
described meanwhile could move between the pages and be drawn twice or not at
all. A token minted for one account reads as nothing for another, and an edited
one as nothing at all. It is good for `TOKEN_SECONDS` — longer than any page's
other references, because a page left open is still a page, and a press on one
whose references have lapsed draws the rest without is-new marks rather than
refusing.

PLACES MEAN SOMETHING ONLY IN THE DAY THEY WERE COUNTED IN. A refill can add a
card to the day, take one off or move one, and every place after it then names
a different card: the rest drawn from the old places draws a card the page
already holds, or leaves one out, with nothing to show for it. So the token
carries a digest of the day's cards in the order the page counted them, and a
press on a day that no longer matches it says the month changed, as a late day
does — whatever else remembers the page, because what the page filed (the
shell's month, kept a few hours) can be gone long before the token is.
"""
from __future__ import annotations

import hashlib
import heapq
from collections.abc import Iterable, Sequence
from datetime import date
from typing import NamedTuple

from .. import signed_tokens

# How many of a day's cards are drawn before the control. A day this size or
# smaller is drawn whole.
DAY_PAGE_CARDS = 100
# How long a control is honoured.
TOKEN_SECONDS = 24 * 60 * 60

_PURPOSE = "calendar day page"
# 2: the token carries the digest of the day its places were counted in.
_VERSION = 2
# The longest text any field carries: an endpoint key, a source override and the
# page's two tokens are all far shorter.
_MAX_TEXT = 64
# How long the day's digest is. It tells one ordering of a day's cards from
# another, not a forger from this instance — the seal does that — so 64 bits is
# far past what any day's changes could collide in.
DIGEST_BYTES = 8


class Rest(NamedTuple):
    """What a "show the rest" control names: the account whose page drew it, the
    calendar and the day, the places in the day of the cards the page drew
    (ascending), the digest of the day they are places in (`cards_digest`), the
    page's source override ("" for none), is-new token and page token, and
    when."""
    user_id: int
    endpoint: str
    day: date
    drawn: tuple[int, ...]
    counted: bytes
    source: str
    new: str
    page: str
    issued_at: int


def cards_digest(views: Iterable) -> bytes:
    """A day's cards (`views`, in the day's order), each named by its row in the
    title store — its title's key and its episode row, which is the card's
    identity whoever lists it — digested in that order: what a press checks its
    places are still places in."""
    digest = hashlib.blake2b(digest_size=DIGEST_BYTES, person=b"day cards")
    for view in views:
        digest.update(signed_tokens.text(view.card.title_key))
        digest.update(signed_tokens.text(view.card.unit))
    return digest.digest()


def _rank(view) -> tuple[int, int]:
    """How a card ranks for the first page, smallest first: how many of the
    viewer's own services list it, most first, then how much of its title it
    shows."""
    return (-len(view.listed), -view.complete)


def first_page(views: Sequence, anchor_at: int | None = None) -> tuple[int, ...]:
    """The places in the day (`views`, its cards in the day's order) of the
    cards drawn with it, ascending: every one up to `DAY_PAGE_CARDS`, and past
    that the best ranked (`_rank`, the day's order breaking ties) — with the
    card a jump is aimed at (`anchor_at`, its place) among them wherever it
    ranks, because it has to be in the page for the browser to land on it."""
    count = len(views)
    if count <= DAY_PAGE_CARDS:
        return tuple(range(count))
    chosen = set(heapq.nsmallest(DAY_PAGE_CARDS, range(count),
                                 key=lambda at: (*_rank(views[at]), at)))
    if anchor_at is not None and 0 <= anchor_at < count:
        chosen.add(anchor_at)
    return tuple(sorted(chosen))


def token(rest: Rest) -> str:
    """`rest` sealed for a control's address. The drawn places go as their
    count and then each one's gap after the one before, so a page of a hundred
    costs about a hundred bytes however big the day."""
    gaps, before = [], -1
    for at in rest.drawn:
        gaps.append(signed_tokens.varint(at - before - 1))
        before = at
    if len(rest.counted) != DIGEST_BYTES:
        raise ValueError("a day's digest is DIGEST_BYTES long (cards_digest)")
    return signed_tokens.seal(_PURPOSE, b"".join((
        bytes((_VERSION,)), signed_tokens.varint(rest.user_id),
        signed_tokens.varint(rest.issued_at), signed_tokens.text(rest.endpoint),
        signed_tokens.varint(rest.day.toordinal()),
        signed_tokens.varint(len(rest.drawn)), *gaps, rest.counted,
        signed_tokens.text(rest.source), signed_tokens.text(rest.new),
        signed_tokens.text(rest.page))))


def read(text: str, *, user_id: int, now: int) -> Rest | None:
    """What a control's token names, when this instance sealed it for THIS
    account within `TOKEN_SECONDS` — None for anything else."""
    payload = signed_tokens.opened(_PURPOSE, text)
    if payload is None:
        return None
    try:
        reader = signed_tokens.Reader(payload)
        if reader.byte() != _VERSION:
            return None
        owner, issued = reader.varint(), reader.varint()
        endpoint = reader.text(_MAX_TEXT)
        day = date.fromordinal(reader.varint())
        count = reader.varint()
        if count > DAY_PAGE_CARDS + 1:
            return None
        drawn, at = [], -1
        for _ in range(count):
            at += reader.varint() + 1
            drawn.append(at)
        counted = reader.take(DIGEST_BYTES)
        source, new, page = (reader.text(_MAX_TEXT), reader.text(_MAX_TEXT),
                             reader.text(_MAX_TEXT))
        reader.end()
        rest = Rest(owner, endpoint, day, tuple(drawn), counted, source, new, page, issued)
    except (ValueError, OverflowError, UnicodeError):
        return None
    if rest.user_id != int(user_id) or not 0 <= now - rest.issued_at <= TOKEN_SECONDS:
        return None
    return rest
