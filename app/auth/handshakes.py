"""Provider handshakes — the row and the cookie that bind an OAuth round trip
to the browser and session that started it.

A provider sign-in leaves this app and comes back as a top-level GET
navigation, which SameSite=Lax deliberately sends cookies on. Nothing about
the returning request proves it belongs to the visitor who started the flow,
and that gap is an account-takeover vector rather than a CSRF nit: an attacker
who gets a signed-in victim's browser to complete a callback carrying the
ATTACKER's provider identity has just linked that identity to the victim's
account, and can sign in as them from then on.

A handshake row is what closes it. It is created before the browser leaves,
consumed exactly once when it comes back, and carries everything the callback
needs to know — so nothing has to be trusted from the URL beyond the opaque
state value that names the row.
"""
from __future__ import annotations

import secrets

from fastapi import Request, Response

from .. import db
from ..config import Settings
from . import cookies

# Long enough to approve on the provider's site at a human pace, short enough
# that a state value left in a browser history or a proxy log is worthless by
# the time anybody reads it.
HANDSHAKE_TTL_SECONDS = 10 * 60


class HandshakeError(Exception):
    """A callback could not be matched to a handshake this app started."""


# The one message for every cause — missing, unknown, expired, already used, and
# bound to somebody else's session alike. Distinguishing them would tell an
# attacker probing callbacks which of their guesses was closest.
HANDSHAKE_REJECTED = (
    "This sign-in link is not valid any more. Start again from the sign-in page."
)


async def create_handshake(
    *,
    provider: str,
    purpose: str,
    session_id: str | None = None,
    invite_token: str | None = None,
    pkce_verifier: str | None = None,
    plex_pin_id: str | None = None,
    now: int | None = None,
) -> str:
    """Record an in-flight authorization and return its `state` value.

    `session_id` is REQUIRED for purpose='link' and must be the session that
    asked to link — the callback refuses unless the same session comes back. It
    is a real foreign key, so revoking a session also kills the link handshake
    it had in flight.

    `invite_token` is how an invite reaches a registration that happens through
    a provider: it travels in this row rather than in a cookie or the redirect
    URL, neither of which the visitor is prevented from editing.
    """
    if purpose == "link" and not session_id:
        raise ValueError("A link handshake must be bound to the session that started it.")
    ts = db.now() if now is None else now
    state = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO auth_handshakes (state, provider, purpose, session_id, invite_token, "
        "pkce_verifier, plex_pin_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (state, provider, purpose, session_id, invite_token, pkce_verifier, plex_pin_id,
         ts, ts + HANDSHAKE_TTL_SECONDS),
    )
    return state


def _check_handshake_row(row, *, provider: str, session_id: str | None, ts: int) -> None:
    """The binding checks shared by consume_handshake and peek_handshake: right
    provider, not expired, not already consumed, and — for a link handshake —
    bound to the session making this request."""
    if row is None:
        raise HandshakeError(HANDSHAKE_REJECTED)
    if row["provider"] != provider or row["consumed_at"] is not None:
        raise HandshakeError(HANDSHAKE_REJECTED)
    if ts >= int(row["expires_at"]):
        raise HandshakeError(HANDSHAKE_REJECTED)
    if row["purpose"] == "link":
        bound = row["session_id"] or ""
        if not (session_id and secrets.compare_digest(str(bound), str(session_id))):
            raise HandshakeError(HANDSHAKE_REJECTED)


async def consume_handshake(
    state: str | None,
    *,
    provider: str,
    session_id: str | None = None,
    now: int | None = None,
):
    """Claim a handshake exactly once, returning its row.

    Raises HandshakeError — with one message for every cause — when the state is
    missing, unknown, for another provider, expired, already consumed, or bound
    to a session other than the one making this request. There is deliberately
    no "no state, so assume this is a login" path: that would restore the exact
    hole this table exists to close.

    The lookup and the consuming write happen in ONE transaction, so single use
    is enforced by the database rather than by a read followed by a hopeful
    write. Two callbacks racing on the same state produce one success and one
    refusal.
    """
    if not state:
        raise HandshakeError(HANDSHAKE_REJECTED)
    ts = db.now() if now is None else now

    def _work(conn: db.Connection):
        row = conn.execute("SELECT * FROM auth_handshakes WHERE state = ?", (state,)).fetchone()
        _check_handshake_row(row, provider=provider, session_id=session_id, ts=ts)
        claimed = conn.execute(
            "UPDATE auth_handshakes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (ts, row["id"]),
        )
        if claimed.rowcount != 1:  # pragma: no cover — the write lock rules it out
            raise HandshakeError(HANDSHAKE_REJECTED)
        return row

    return await db.transaction(_work)


async def peek_handshake(
    state: str | None,
    *,
    provider: str,
    session_id: str | None = None,
    now: int | None = None,
):
    """Read a handshake row without consuming it, applying every binding check
    consume_handshake does.

    For flows where the provider confirms completion asynchronously rather
    than through a one-shot callback — Plex's PIN is polled repeatedly before
    it carries a token — repeatedly consuming the row isn't an option, since
    consumption is single-use by design. Every poll instead re-validates the
    binding with this, and the caller still calls consume_handshake exactly
    once, at the moment it is ready to finish the flow, so single use remains
    enforced by the database rather than assumed by the caller.
    """
    if not state:
        raise HandshakeError(HANDSHAKE_REJECTED)
    ts = db.now() if now is None else now
    row = await db.fetch_one("SELECT * FROM auth_handshakes WHERE state = ?", (state,))
    _check_handshake_row(row, provider=provider, session_id=session_id, ts=ts)
    return row


# The handshake is also pinned to the browser it started in, with a cookie
# holding the same state value. Without it, an attacker can start a login
# handshake with their own provider account and hand the resulting callback URL
# to a signed-out victim, whose browser then completes it and signs them in as
# the attacker — everything they do next lands in the attacker's account. The
# cookie costs nothing (the callback is a top-level navigation, which Lax sends
# it on) and it means a callback must arrive in the same browser that left.
HANDSHAKE_COOKIE = "tns_handshake"
HANDSHAKE_COOKIE_SECURE = "__Host-tns_handshake"


def set_handshake_cookie(
    response: Response, state: str, settings: Settings, request: Request | None = None,
) -> None:
    secure = cookies.use_secure_cookie(settings, request)
    response.set_cookie(
        key=HANDSHAKE_COOKIE_SECURE if secure else HANDSHAKE_COOKIE,
        value=state,
        max_age=HANDSHAKE_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def read_handshake_cookie(request: Request, settings: Settings) -> str | None:
    secure = cookies.use_secure_cookie(settings, request)
    preferred = HANDSHAKE_COOKIE_SECURE if secure else HANDSHAKE_COOKIE
    other = HANDSHAKE_COOKIE if secure else HANDSHAKE_COOKIE_SECURE
    return request.cookies.get(preferred) or request.cookies.get(other)


def clear_handshake_cookie(
    response: Response, settings: Settings, request: Request | None = None,
) -> None:
    """Drop both names once a callback has been resolved, so a stale value can't
    be paired with a later state."""
    secure = cookies.use_secure_cookie(settings, request)
    response.delete_cookie(HANDSHAKE_COOKIE, path="/", httponly=True, samesite="lax", secure=False)
    response.delete_cookie(
        HANDSHAKE_COOKIE_SECURE, path="/", httponly=True, samesite="lax", secure=secure or True,
    )


def handshake_cookie_matches(request: Request, settings: Settings, state: str | None) -> bool:
    held = read_handshake_cookie(request, settings)
    return bool(held and state and secrets.compare_digest(held, state))
