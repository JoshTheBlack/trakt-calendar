"""A big day is drawn in pages: its first `DAY_PAGE_CARDS` cards with the day,
and a control that asks for the rest of it when pressed.

WHY. An unfiltered All Episodes day holds ~600 cards, and every one of them is
markup the browser parses and holds whether or not anybody scrolls past it:
MEASURED, an unfiltered October was 258 MB of HTML and ~1.9 million elements,
which is the multi-gigabyte tab. PAGING, NEVER CAPPING: the day's count, its chip
and its is-new marks stay the whole day's (the shell filed the whole month), and
nothing is hidden that a press does not bring back.

THE CONTROL CARRIES ONE SIGNED TOKEN (app/signed_tokens.py), and it is checked
for the account: whose page drew it, which calendar and day, how many cards the
page drew, and the references a late day carries — the source override, the
is-new token, the page token. A token minted for one account reads as nothing
for another, and an edited one as nothing at all. It is good for `TOKEN_SECONDS`
— longer than any page's other references, because a page left open is still a
page, and a press on one whose references have lapsed draws the rest without is-new
marks rather than refusing.
"""
from __future__ import annotations

from datetime import date
from typing import NamedTuple

from .. import signed_tokens

# How many of a day's cards are drawn before the control. A day this size or
# smaller is drawn whole.
DAY_PAGE_CARDS = 100
# How long a control is honoured.
TOKEN_SECONDS = 24 * 60 * 60

_PURPOSE = "calendar day page"
_VERSION = 1
# The longest text any field carries: an endpoint key, a source override and the
# page's two tokens are all far shorter.
_MAX_TEXT = 64


class Rest(NamedTuple):
    """What a "show the rest" control names: the account whose page drew it, the
    calendar and the day, how many of the day's cards were drawn, the page's
    source override ("" for none), is-new token and page token, and when."""
    user_id: int
    endpoint: str
    day: date
    drawn: int
    source: str
    new: str
    page: str
    issued_at: int


def first_page(count: int, anchor_at: int | None = None) -> int:
    """How many of a day's `count` cards are drawn with it: every one up to
    `DAY_PAGE_CARDS`, and past that the first page — or as far as the card a
    jump is aimed at (`anchor_at`, its place in the day), which has to be in the
    page for the browser to land on it."""
    if count <= DAY_PAGE_CARDS:
        return count
    return max(DAY_PAGE_CARDS, (anchor_at or 0) + 1)


def token(rest: Rest) -> str:
    """`rest` sealed for a control's address."""
    return signed_tokens.seal(_PURPOSE, b"".join((
        bytes((_VERSION,)), signed_tokens.varint(rest.user_id),
        signed_tokens.varint(rest.issued_at), signed_tokens.text(rest.endpoint),
        signed_tokens.varint(rest.day.toordinal()), signed_tokens.varint(rest.drawn),
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
        drawn = reader.varint()
        source, new, page = (reader.text(_MAX_TEXT), reader.text(_MAX_TEXT),
                             reader.text(_MAX_TEXT))
        reader.end()
        rest = Rest(owner, endpoint, day, drawn, source, new, page, issued)
    except (ValueError, OverflowError, UnicodeError):
        return None
    if rest.user_id != int(user_id) or not 0 <= now - rest.issued_at <= TOKEN_SECONDS:
        return None
    return rest
