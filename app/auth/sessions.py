"""Server-side sessions: the `sessions` row and the CurrentUser it resolves to.

The cookie holds nothing but an opaque random id and every fact about the
session lives in the `sessions` table. Revoking one is a row delete — instant,
rather than waiting out a signed token's lifetime.

Two clocks, and both are enforced on every validate:
  - a sliding window, refreshed on use but at most once an hour so an active
    session doesn't cause a database write on every single request;
  - an absolute cap measured from creation, which sliding can never extend, so
    a session cannot live forever just by being used forever.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .. import db

SESSION_SLIDING_SECONDS = 14 * 24 * 3600
SESSION_ABSOLUTE_SECONDS = 60 * 24 * 3600
SESSION_REFRESH_INTERVAL = 3600


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller, as every dependency in app/auth/levels.py
    returns it."""
    user_id: int
    session_id: str
    username: str | None
    # The chosen label, or None when none is set. Display only — never look an
    # account up by it. Use `.label` rather than reading this directly when what
    # you want is "what to call this person on screen".
    display_name: str | None
    is_admin: bool
    calendar_approved: bool
    distrakt_approved: bool
    # The ranker is a standalone feature with its own grant. Unlike
    # distrakt_approved it carries no provider requirement: a ranker user builds
    # their lists by searching, so an account with no linked identity at all is
    # a fully working one.
    ranker_approved: bool
    timezone: str | None
    # distrakt reads the requesting user's own Trakt watch history through their
    # own token, so an account with no linked Trakt identity has nothing for it
    # to read — approval alone isn't enough to make the page work.
    has_trakt_identity: bool
    expires_at: int
    absolute_expires_at: int

    @property
    def label(self) -> str:
        """What to call this person on screen: their chosen display name, else
        their username, else nothing. One place so every surface agrees, rather
        than each one spelling out its own `or` chain and drifting."""
        return self.display_name or self.username or ""


async def create_session(
    user_id: int,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
    now: int | None = None,
) -> str:
    """Create a session row and return its opaque id, which is the cookie value."""
    ts = db.now() if now is None else now
    session_id = secrets.token_urlsafe(32)
    absolute = ts + SESSION_ABSOLUTE_SECONDS
    expires = min(ts + SESSION_SLIDING_SECONDS, absolute)
    await db.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at, absolute_expires_at, "
        "last_seen_at, user_agent, ip_address) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, user_id, ts, expires, absolute, ts, (user_agent or "")[:400], ip_address),
    )
    return session_id


_SESSION_SELECT = """
SELECT s.id                AS session_id,
       s.user_id           AS user_id,
       s.expires_at        AS expires_at,
       s.absolute_expires_at AS absolute_expires_at,
       s.last_seen_at      AS last_seen_at,
       u.username          AS username,
       u.display_name      AS display_name,
       u.is_admin          AS is_admin,
       u.calendar_approved AS calendar_approved,
       u.distrakt_approved AS distrakt_approved,
       u.ranker_approved   AS ranker_approved,
       u.is_disabled       AS is_disabled,
       u.timezone          AS timezone,
       EXISTS (SELECT 1 FROM linked_identities li
                WHERE li.user_id = u.id AND li.provider = 'trakt') AS has_trakt
  FROM sessions s
  JOIN users u ON u.id = s.user_id
 WHERE s.id = ?
"""


async def validate_session(
    session_id: str | None,
    *,
    now: int | None = None,
    touch: bool = True,
) -> CurrentUser | None:
    """Resolve a cookie value to a CurrentUser, sliding the expiry if it is due.

    Returns None for unknown, expired on either clock, and disabled alike. The
    caller can't tell which, and neither can anyone probing it.
    """
    if not session_id:
        return None
    ts = db.now() if now is None else now

    def _work(conn: db.Connection) -> CurrentUser | None:
        row = conn.execute(_SESSION_SELECT, (session_id,)).fetchone()
        if row is None:
            return None
        # The index lookup already matched; this repeats the comparison in
        # constant time, so the one equality check done in Python on a secret
        # isn't a byte-at-a-time one.
        if not secrets.compare_digest(str(row["session_id"]), session_id):
            return None  # pragma: no cover — would need a collation/affinity surprise
        if row["is_disabled"]:
            return None
        if ts >= int(row["expires_at"]) or ts >= int(row["absolute_expires_at"]):
            return None

        expires_at = int(row["expires_at"])
        absolute = int(row["absolute_expires_at"])
        if touch and (ts - int(row["last_seen_at"])) >= SESSION_REFRESH_INTERVAL:
            # Clamped to the absolute cap: sliding extends the window, never the
            # ceiling.
            expires_at = min(ts + SESSION_SLIDING_SECONDS, absolute)
            conn.execute(
                "UPDATE sessions SET expires_at = ?, last_seen_at = ? WHERE id = ?",
                (expires_at, ts, session_id),
            )
        return CurrentUser(
            user_id=int(row["user_id"]),
            session_id=str(row["session_id"]),
            username=row["username"],
            display_name=row["display_name"],
            is_admin=bool(row["is_admin"]),
            calendar_approved=bool(row["calendar_approved"]),
            distrakt_approved=bool(row["distrakt_approved"]),
            ranker_approved=bool(row["ranker_approved"]),
            timezone=row["timezone"],
            has_trakt_identity=bool(row["has_trakt"]),
            expires_at=expires_at,
            absolute_expires_at=absolute,
        )

    return await db.run(_work)


async def revoke_session(session_id: str) -> None:
    """Hard delete on logout — no tombstone, no waiting for an expiry to pass."""
    await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


async def revoke_user_session(user_id: int, session_id: str) -> bool:
    """Delete one session, but only if it belongs to `user_id`. False when it
    doesn't exist or belongs to somebody else — so an admin screen showing one
    account cannot act on another account's session by id."""
    result = await db.execute(
        "DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id),
    )
    return result.rowcount > 0


async def revoke_user_sessions(user_id: int) -> int:
    """Log a user out everywhere. Returns how many sessions were deleted."""
    result = await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return result.rowcount


async def sweep_expired_sessions(now: int | None = None) -> int:
    """Delete rows dead on either clock. Run periodically from the heartbeat loop
    in app/main.py, since nothing else would ever remove them."""
    ts = db.now() if now is None else now
    result = await db.execute(
        "DELETE FROM sessions WHERE expires_at <= ? OR absolute_expires_at <= ?", (ts, ts),
    )
    return result.rowcount
