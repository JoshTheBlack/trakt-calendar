"""Short signed tokens: what a page hands a browser to carry back, sealed so the
server reads back only what it wrote.

A TOKEN IS A REFERENCE THE SERVER CAN TRUST: a picture's report address
(app/metadata/pictures.py) and a big day's "show the rest" control
(app/calendar/day_pages.py) each carry a few facts the server decided when it
drew the page, and each must be refused if anybody but this instance spelled
them. ONE IMPLEMENTATION OF THE SEALING, because a second copy of an HMAC check
is where a security rule drifts silently: each caller owns its LAYOUT — which
facts, in what order, behind a version byte of its own — and this module owns
the key, the signature and the spelling.

THE KEY is derived from the instance's ENCRYPTION_KEY for one PURPOSE
(secrets_box.derived_key — never the key that seals the secrets, and a different
key for every purpose, so a token minted for one use is refused by every other).
An instance with no ENCRYPTION_KEY signs with keys derived, purpose by purpose,
from one made when the process starts and never stored: a page drawn before a
restart then carries tokens nothing will read, which costs what each caller says
it costs.

THE SIGNATURE is the first 16 bytes of HMAC-SHA256 over the payload — 128 bits,
compared in constant time.

THE SPELLING is base64url with no padding, and ONE spelling: the default
decoder skips characters outside its alphabet and accepts padding past the end,
so one token would otherwise read the same under many. Text outside the alphabet
or past `MAX_TEXT` is refused before anything is decoded.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from . import secrets_box

SIGNATURE_BYTES = 16
MAX_TEXT = 1024
_TEXT = re.compile(r"[A-Za-z0-9_-]{2,%d}" % MAX_TEXT)

# The key an instance with no ENCRYPTION_KEY signs with — see the module docstring.
_PROCESS_KEY = secrets.token_bytes(32)


def _key(purpose: str) -> bytes:
    """`purpose`'s own key — derived for it from the instance's ENCRYPTION_KEY,
    or, without one, from the process's key: one fallback key shared by every
    purpose would open a token minted for one use under any other."""
    return (secrets_box.derived_key(purpose)
            or hmac.new(_PROCESS_KEY, purpose.encode("utf-8"), hashlib.sha256).digest())


def sign(purpose: str, payload: bytes) -> bytes:
    """`payload`'s signature under `purpose`'s key."""
    return hmac.new(_key(purpose), payload, hashlib.sha256).digest()[:SIGNATURE_BYTES]


def seal(purpose: str, payload: bytes) -> str:
    """`payload` and its signature, spelled for an address."""
    return base64.urlsafe_b64encode(payload + sign(purpose, payload)).rstrip(b"=").decode("ascii")


def opened(purpose: str, text: str) -> bytes | None:
    """The payload `text` carries, when this instance sealed it for `purpose` —
    None for anything else, malformed or forged alike."""
    raw = str(text or "")
    if not _TEXT.fullmatch(raw):
        return None
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    except ValueError:
        return None
    payload, signature = data[:-SIGNATURE_BYTES], data[-SIGNATURE_BYTES:]
    if not payload or not hmac.compare_digest(signature, sign(purpose, payload)):
        return None
    return payload


# ---------------------------------------------------------------------------
# the pieces a layout is spelled in
# ---------------------------------------------------------------------------

def varint(value: int) -> bytes:
    """`value` (>= 0) as unsigned LEB128."""
    value = int(value)
    if value < 0:
        raise ValueError("a varint is never negative")
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def optional_int(value: int | None) -> bytes:
    """Any integer or None: 0 for none, otherwise the number zigzagged and plus
    one, so a negative survives as well as a positive."""
    if value is None:
        return varint(0)
    value = int(value)
    return varint(((value << 1) ^ (value >> 63)) + 1)


def text(value: str) -> bytes:
    """A string, its UTF-8 length first."""
    data = str(value).encode("utf-8")
    return varint(len(data)) + data


class Reader:
    """A payload read front to back; every read past its end, and one that
    leaves bytes over (`end`), raises ValueError."""

    def __init__(self, data: bytes):
        self.data, self.at = data, 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.at + count > len(self.data):
            raise ValueError("a token shorter than it says")
        piece = self.data[self.at:self.at + count]
        self.at += count
        return piece

    def byte(self) -> int:
        return self.take(1)[0]

    def varint(self) -> int:
        value = shift = 0
        while True:
            byte = self.byte()
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
            if shift > 63:
                raise ValueError("a varint longer than any this module writes")

    def optional_int(self) -> int | None:
        code = self.varint()
        if not code:
            return None
        code -= 1
        return (code >> 1) ^ -(code & 1)

    def text(self, limit: int) -> str:
        length = self.varint()
        if length > limit:
            raise ValueError("a string longer than its layout allows")
        return self.take(length).decode("utf-8")

    def end(self) -> None:
        if self.at != len(self.data):
            raise ValueError("a token longer than it says")
