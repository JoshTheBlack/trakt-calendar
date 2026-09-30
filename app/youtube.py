"""Which YouTube video a string names, if any — the one reading of it.

EVERY PROVIDER'S VIDEO LIST IS READ THROUGH THIS, and the details panel embeds
only the id it yields. That used to be two answers — the panel's own filter read
a URL carefully while Simkl's provider wrapped whatever its field held in a watch
URL — and the gap between them is where a trailer button that goes nowhere came
from. The provider now reads each value once, here, into a `Video` whose id the
panel plays as it stands. In the kernel because every provider package asks it
and none of them owns it.

WHAT A SOURCE ACTUALLY SENDS, measured on a live instance's cached Simkl
payloads: a bare eleven-character id nearly always, and otherwise an id with
query junk glued on and cut at twenty characters (`CEhHdPcz0u0?si=DTY4z`), a
fragment of a URL (`v=…`, `watch?v=…`, `=…`, `shorts/…`, `/…?feature`), a whole
URL already truncated past saving (`https://youtu.be/EhV`), another service's
link, a channel handle, a number, or free text. Every recoverable shape yields
its id; everything else yields None, so a caller offers no trailer rather than a
link to an error page.

EXACTLY ELEVEN, WITH NOTHING ID-LIKE AFTER IT. Taking the first eleven id
characters of anything longer embedded a video that is not this title's:
`sosaundercover` and `a7GH44eZKq5shskR` both read as ids that way.
"""
from __future__ import annotations

import re

_ID = r"[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])"

# A full address on one of YouTube's own hosts, wherever it sits in the string.
_ON_YOUTUBE = re.compile(
    rf"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|v/|shorts/)"
    rf"|youtu\.be/)({_ID})")

# The whole string is an id, perhaps behind a URL fragment's leading part and
# perhaps followed by query junk. Anchored at both ends so another service's URL
# can never be mined for an eleven-character path segment.
_BARE = re.compile(rf"^(?:/?(?:watch\?)?v=|=|/|shorts/)?({_ID})(?:[?&#].*)?$")


def video_id(value: object) -> str | None:
    """The eleven-character video id `value` names, or None."""
    text = str(value or "").strip()
    match = _ON_YOUTUBE.search(text) or _BARE.match(text)
    return match.group(1) if match else None


def watch_url(value: object) -> str:
    """YouTube's own page for the video `value` names, or "" when it names none."""
    found = video_id(value)
    return f"https://www.youtube.com/watch?v={found}" if found else ""
