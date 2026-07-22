"""Auth core — password hashing, sessions, cookies, client IP, and the FastAPI
dependencies that express the app's five authorization levels.

This module owns the primitives; the routes attach the dependencies at their own
definitions (see app/main.py and app/auth_routes.py), and the provider login
flows build on `linked_identities` through the helpers here.

Three things in here are security-load-bearing and are explained where they are
defined rather than here: the off-thread Argon2id hashing, the two-clock session
lifetime, and the cookie Secure policy.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

from . import db
from .config import TRUSTED_PROXY_IPS_DEFAULT, Settings, load_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------

# Argon2id at the library's own defaults. Those defaults track current guidance
# and move as the library is updated, which is the whole point of
# check_needs_rehash below — hand-tuning here would freeze this instance at
# whatever was reasonable on the day it was written.
_hasher = PasswordHasher()

# Verified against when the submitted username doesn't exist, so an unknown
# username costs the same ~50-200ms as a wrong password and login can't be used
# to enumerate accounts by timing. Built on first use rather than at import, so
# a process that never sees a login doesn't pay for it.
_dummy_hash: str | None = None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hasher.hash("timing-parity-placeholder")
    return _dummy_hash


async def hash_password(password: str) -> str:
    """Hash a password, off-thread.

    Argon2 is memory-hard and deliberately costs 50-200ms of CPU. Called inline
    from an async route it would stall the event loop for that whole time, which
    turns every login request into a denial-of-service lever. Every hash and
    verify in this module is offloaded for that reason.
    """
    return await anyio.to_thread.run_sync(_hasher.hash, password)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    # Set when the password was correct but its stored hash was made with
    # outdated parameters. The caller persists it; None means nothing to do.
    new_hash: str | None = None


async def verify_password(stored_hash: str | None, password: str) -> VerifyResult:
    """Verify a password off-thread, upgrading the stored hash when the hashing
    library's defaults have moved on since it was written.

    A missing or empty stored hash still burns a full verify against the dummy
    hash, so an account with no password set is indistinguishable by timing from
    an account whose password was simply wrong.
    """
    def _work() -> VerifyResult:
        candidate = stored_hash or _dummy()
        try:
            _hasher.verify(candidate, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return VerifyResult(False)
        if not stored_hash:
            return VerifyResult(False)
        try:
            if _hasher.check_needs_rehash(candidate):
                return VerifyResult(True, _hasher.hash(password))
        except InvalidHashError:  # pragma: no cover — verify() already accepted it
            pass
        return VerifyResult(True)

    return await anyio.to_thread.run_sync(_work)


async def burn_dummy_verify(password: str) -> None:
    """Spend a verify against the dummy hash. Call it when the username is
    unknown so that failure costs the same as a real one."""
    await verify_password(None, password)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def user_count(conn: db.Connection) -> int:
    """SYNCHRONOUS — for use inside a db.transaction() body, where the write lock
    is already held and the count can't go stale before you act on it."""
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


async def any_users_exist() -> bool:
    return bool(await db.fetch_value("SELECT EXISTS (SELECT 1 FROM users)", (), default=0))


def insert_user(
    conn: db.Connection,
    *,
    username: str | None,
    password_hash: str | None,
    is_admin: bool = False,
    is_bootstrap: bool = False,
    calendar_approved: bool = False,
    distrakt_approved: bool = False,
    timezone: str | None = None,
    now: int | None = None,
) -> int:
    """SYNCHRONOUS insert, returning the new user id.

    Takes a connection rather than opening its own transaction so callers can
    compose it with the rest of theirs — first-run setup creates the user, its
    preferences, and its Trakt identity as one atomic unit.
    """
    ts = db.now() if now is None else now
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, password_changed_at, is_admin, "
        "is_bootstrap, calendar_approved, distrakt_approved, is_disabled, timezone, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            username, password_hash, ts if password_hash else None,
            int(is_admin), int(is_bootstrap), int(calendar_approved), int(distrakt_approved),
            timezone, ts, ts,
        ),
    )
    return int(cur.lastrowid)


def insert_user_prefs(conn: db.Connection, user_id: int, settings: Settings) -> None:
    """SYNCHRONOUS. Seeds a user's view preferences from settings.json's app-wide
    values.

    Those settings.json fields are a SEED, not a live source: once this row
    exists, editing settings.json affects new users only, never this one.
    """
    conn.execute(
        "INSERT INTO user_prefs (user_id, endpoint, card_style, day_packing, "
        "hide_not_watching, network_filter_json, genres, countries) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, settings.endpoint, settings.card_style, settings.day_packing,
            int(bool(settings.hide_not_watching)),
            json.dumps(list(settings.network_filter or [])),
            settings.genres or "", settings.countries or "",
        ),
    )


def insert_linked_identity(
    conn: db.Connection,
    *,
    user_id: int,
    provider: str,
    provider_user_id: str | int,
    display_name: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    token_expires_at: int | None = None,
    now: int | None = None,
) -> int:
    """SYNCHRONOUS.

    `provider_user_id` MUST be the provider's immutable numeric account id, never
    a username, slug, or email — those can be changed by their owner, released,
    and re-registered by somebody else, who would then inherit this link.
    """
    ts = db.now() if now is None else now
    cur = conn.execute(
        "INSERT INTO linked_identities (user_id, provider, provider_user_id, display_name, "
        "access_token, refresh_token, token_expires_at, created_at, last_login_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, provider, str(provider_user_id), display_name, access_token,
         refresh_token, token_expires_at, ts, ts),
    )
    return int(cur.lastrowid)


async def create_user(
    *,
    username: str | None,
    password: str | None,
    settings: Settings | None = None,
    is_admin: bool = False,
    is_bootstrap: bool = False,
    calendar_approved: bool = False,
    distrakt_approved: bool = False,
    timezone: str | None = None,
) -> int:
    """Create a user plus its seeded preferences row in one transaction.

    Hashing happens before the transaction opens: 200ms of Argon2 must not be
    holding SQLite's write lock while every other writer waits behind it.
    """
    cfg = settings or load_settings()
    password_hash = await hash_password(password) if password else None
    tz = timezone if timezone is not None else (cfg.timezone or None)

    def _work(conn: db.Connection) -> int:
        user_id = insert_user(
            conn, username=username, password_hash=password_hash, is_admin=is_admin,
            is_bootstrap=is_bootstrap, calendar_approved=calendar_approved,
            distrakt_approved=distrakt_approved, timezone=tz,
        )
        insert_user_prefs(conn, user_id, cfg)
        return user_id

    return await db.transaction(_work)


async def find_user_by_username(username: str):
    return await db.fetch_one("SELECT * FROM users WHERE username = ?", (username,))


async def get_user(user_id: int):
    return await db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))


async def update_password_hash(user_id: int, password_hash: str) -> None:
    """Persist a transparently-upgraded hash after a successful verify.

    NOT a password change: the secret itself didn't change, so this deliberately
    leaves `password_changed_at` alone and does not revoke sessions.
    """
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id),
    )


async def set_password(user_id: int, password: str) -> None:
    """Change a password and delete every session that user has.

    The session delete is part of the operation rather than the caller's
    responsibility, so "changing my password logs out whoever stole my session"
    holds everywhere it is called from, including admin-driven resets.
    """
    password_hash = await hash_password(password)
    ts = db.now()

    def _work(conn: db.Connection) -> None:
        conn.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = ?, updated_at = ? "
            "WHERE id = ?",
            (password_hash, ts, ts, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    await db.transaction(_work)


async def mark_logged_in(user_id: int) -> None:
    await db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now(), user_id))


# ---------------------------------------------------------------------------
# identifier rules
# ---------------------------------------------------------------------------
# Usernames and public share slugs are validated against ONE set of rules, and
# against each other, so a slug can never shadow somebody else's username. Kept
# here in one place because registration, slug editing, and availability checks
# all need the identical answer.

IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

# Names that are, or could become, a top-level route segment or an obvious
# impersonation risk.
RESERVED_IDENTIFIERS = frozenset({
    "admin", "administrator", "api", "auth", "static", "healthz", "login", "logout",
    "register", "onboarding", "settings", "distrakt", "shared", "s", "u", "c", "pick",
    "me", "new", "null", "undefined", "root", "system", "support", "help",
})

# Long enough to be meaningful, short enough not to fight an operator setting up
# their own instance.
MIN_PASSWORD_LENGTH = 8


def identifier_error(value: str) -> str | None:
    """None when `value` is a usable username or slug, otherwise why not.

    Case-insensitive throughout, matching the NOCASE columns these end up in.
    """
    candidate = (value or "").strip().lower()
    if not candidate:
        return "Pick a username."
    if not IDENTIFIER_RE.match(candidate):
        return ("Usernames are 2-32 characters, lowercase letters/numbers/underscore/"
                "hyphen, starting with a letter or number.")
    if candidate in RESERVED_IDENTIFIERS:
        return "That name is reserved."
    return None


async def identifier_is_retired(kind: str, value: str) -> bool:
    """Whether a deleted account's username or slug is blocking reuse.

    Blocked by default: otherwise a new user claims a deleted user's name and
    silently inherits every link that was already shared under it. An admin can
    release one deliberately.
    """
    row = await db.fetch_one(
        "SELECT 1 FROM retired_identifiers WHERE kind = ? AND value = ?",
        (kind, (value or "").strip()),
    )
    return row is not None


# ---------------------------------------------------------------------------
# invites
# ---------------------------------------------------------------------------
# A Plex or Trakt login only proves control of some account on that service —
# neither is a membership check against anything this instance cares about —
# so without a gate anyone on the internet could auto-register and sit in the
# approval queue forever. An invite is that gate.

async def create_invite(
    *,
    created_by: int,
    label: str | None = None,
    expires_at: int | None = None,
    max_uses: int | None = None,
    grants_calendar_on_accept: bool = True,
) -> dict:
    """Mint a new invite token. Returns {"id", "token"}.

    `grants_calendar_on_accept` defaults to True: issuing an invite is already
    a deliberate act of trust, so making the invitee then wait in the approval
    queue is friction with no added safety. There is deliberately no distrakt
    equivalent — distrakt exposes a user's private watch history, so that
    grant is always a separate, manual step taken after the account exists.
    """
    token = secrets.token_urlsafe(32)
    ts = db.now()
    await db.execute(
        "INSERT INTO invites (token, label, created_by, created_at, expires_at, max_uses, "
        "used_count, revoked, grants_calendar_on_accept) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
        (token, label or None, created_by, ts, expires_at, max_uses, int(grants_calendar_on_accept)),
    )
    invite_id = await db.fetch_value("SELECT id FROM invites WHERE token = ?", (token,))
    return {"id": int(invite_id), "token": token}


async def find_invite_by_token(token: str):
    return await db.fetch_one("SELECT * FROM invites WHERE token = ?", (token,))


def invite_is_usable(invite, now: int | None = None) -> bool:
    """Whether an invite row can still be redeemed right now.

    Takes the row rather than a token so a caller who already fetched it inside
    a transaction — to re-check quota against a concurrent redemption — doesn't
    pay for a second lookup. A missing row (None) reads as unusable, so a
    find_invite_by_token() result can be passed straight through with no
    separate None check.
    """
    if invite is None or invite["revoked"]:
        return False
    ts = db.now() if now is None else now
    if invite["expires_at"] is not None and ts >= int(invite["expires_at"]):
        return False
    if invite["max_uses"] is not None and int(invite["used_count"]) >= int(invite["max_uses"]):
        return False
    return True


def redeem_invite(
    conn: db.Connection,
    *,
    invite,
    user_id: int,
    ip_address: str | None = None,
    now: int | None = None,
) -> None:
    """SYNCHRONOUS. Increments the invite's use count and records who redeemed
    it. Call inside the same transaction that creates the account, against a
    row read inside that same transaction — the caller's earlier
    invite_is_usable() check ran before the transaction opened, and quota may
    have moved since."""
    ts = db.now() if now is None else now
    conn.execute("UPDATE invites SET used_count = used_count + 1 WHERE id = ?", (invite["id"],))
    conn.execute(
        "INSERT INTO invite_redemptions (invite_id, user_id, redeemed_at, ip_address) "
        "VALUES (?, ?, ?, ?)",
        (invite["id"], user_id, ts, ip_address),
    )


async def revoke_invite(invite_id: int) -> bool:
    result = await db.execute("UPDATE invites SET revoked = 1 WHERE id = ?", (invite_id,))
    return result.rowcount > 0


async def list_invites():
    """Newest first, with each invite's redemption count alongside it — enough
    for an admin listing without a second query per row."""
    return await db.fetch_all(
        "SELECT i.*, "
        "(SELECT COUNT(*) FROM invite_redemptions r WHERE r.invite_id = i.id) AS redemption_count "
        "FROM invites i ORDER BY i.created_at DESC"
    )


async def username_availability_error(username: str) -> str | None:
    """None when `username` can be registered right now, otherwise why not.

    Composes identifier_error's format/reserved rules with the two things only
    a database lookup can answer: an account already using it, and a retired
    identifier blocking it. Not authoritative under concurrency — the
    registration transaction re-checks both before it commits.
    """
    if err := identifier_error(username):
        return err
    candidate = username.strip().lower()
    if await find_user_by_username(candidate):
        return "That username is taken."
    if await identifier_is_retired("username", candidate):
        return "That username is taken."
    return None


# ---------------------------------------------------------------------------
# login / registration throttling
# ---------------------------------------------------------------------------
# One table backs three independent limiters — login, registration, invite
# redemption — each keyed by its own key_type so their counts never mix. A
# lockout is a COUNT over a trailing window, recomputed on every check, rather
# than a stored "locked until" timestamp that could drift out of sync with the
# attempts it is supposed to summarize.

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
REGISTER_MAX_ATTEMPTS = 10
REGISTER_WINDOW_SECONDS = 60 * 60
INVITE_MAX_ATTEMPTS = 10
INVITE_WINDOW_SECONDS = 60 * 60
# Old enough that no limiter above still needs the row, so one sweep interval
# covers all three (plus the share-page limiter built on the same table).
ATTEMPT_RETENTION_SECONDS = 24 * 60 * 60


async def record_attempt(key_type: str, key_value: str, succeeded: bool, now: int | None = None) -> None:
    ts = db.now() if now is None else now
    await db.execute(
        "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) VALUES (?, ?, ?, ?)",
        (key_type, key_value, ts, int(succeeded)),
    )


async def clear_attempts(key_type: str, key_value: str) -> None:
    """Drop every recorded attempt for this key. Called on a successful login so
    a string of earlier failures can't combine with one later mistyped password
    to lock out someone who just proved they own the account."""
    await db.execute(
        "DELETE FROM login_attempts WHERE key_type = ? AND key_value = ?", (key_type, key_value),
    )


async def is_locked_out(
    key_type: str, key_value: str, *, max_attempts: int, window_seconds: int, now: int | None = None,
) -> bool:
    """Whether `max_attempts` FAILURES have landed for this key within the
    trailing `window_seconds`. Failures only, so a burst of wrong passwords
    followed by the right one doesn't count against whoever just succeeded."""
    ts = db.now() if now is None else now
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
        "AND succeeded = 0 AND attempted_at > ?",
        (key_type, key_value, ts - window_seconds), default=0,
    )
    return int(count) >= max_attempts


async def rate_limited(
    key_type: str, key_value: str, *, max_attempts: int, window_seconds: int, now: int | None = None,
) -> bool:
    """Whether `max_attempts` requests — successful or not — have landed for
    this key within the trailing `window_seconds`. For registration and invite
    redemption, which throttle request volume rather than failures."""
    ts = db.now() if now is None else now
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? AND attempted_at > ?",
        (key_type, key_value, ts - window_seconds), default=0,
    )
    return int(count) >= max_attempts


async def sweep_login_attempts(now: int | None = None) -> int:
    """Delete attempt rows old enough that no limiter still consults them. Run
    from the heartbeat loop alongside the session sweep."""
    ts = db.now() if now is None else now
    result = await db.execute(
        "DELETE FROM login_attempts WHERE attempted_at <= ?", (ts - ATTEMPT_RETENTION_SECONDS,),
    )
    return result.rowcount


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
# Sessions are server-side: the cookie holds nothing but an opaque random id and
# every fact about the session lives in the `sessions` table. Revoking one is a
# row delete — instant, rather than waiting out a signed token's lifetime.
#
# Two clocks, and both are enforced on every validate:
#   - a sliding window, refreshed on use but at most once an hour so an active
#     session doesn't cause a database write on every single request;
#   - an absolute cap measured from creation, which sliding can never extend, so
#     a session cannot live forever just by being used forever.

SESSION_SLIDING_SECONDS = 14 * 24 * 3600
SESSION_ABSOLUTE_SECONDS = 60 * 24 * 3600
SESSION_REFRESH_INTERVAL = 3600

COOKIE_NAME = "tns_session"
# The `__Host-` prefix is enforced by the browser: it requires Secure, Path=/,
# and no Domain attribute, and in exchange it stops a sibling subdomain from
# overwriting the cookie. It is only legal when the cookie really is Secure,
# hence two names rather than one.
COOKIE_NAME_SECURE = "__Host-tns_session"


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller, as every dependency below returns it."""
    user_id: int
    session_id: str
    username: str | None
    is_admin: bool
    calendar_approved: bool
    distrakt_approved: bool
    timezone: str | None
    # distrakt reads the requesting user's own Trakt watch history through their
    # own token, so an account with no linked Trakt identity has nothing for it
    # to read — approval alone isn't enough to make the page work.
    has_trakt_identity: bool
    expires_at: int
    absolute_expires_at: int


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
       u.is_admin          AS is_admin,
       u.calendar_approved AS calendar_approved,
       u.distrakt_approved AS distrakt_approved,
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
            is_admin=bool(row["is_admin"]),
            calendar_approved=bool(row["calendar_approved"]),
            distrakt_approved=bool(row["distrakt_approved"]),
            timezone=row["timezone"],
            has_trakt_identity=bool(row["has_trakt"]),
            expires_at=expires_at,
            absolute_expires_at=absolute,
        )

    return await db.run(_work)


async def revoke_session(session_id: str) -> None:
    """Hard delete on logout — no tombstone, no waiting for an expiry to pass."""
    await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


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


# ---------------------------------------------------------------------------
# provider handshakes
# ---------------------------------------------------------------------------
# A provider sign-in leaves this app and comes back as a top-level GET
# navigation, which SameSite=Lax deliberately sends cookies on. Nothing about
# the returning request proves it belongs to the visitor who started the flow,
# and that gap is an account-takeover vector rather than a CSRF nit: an attacker
# who gets a signed-in victim's browser to complete a callback carrying the
# ATTACKER's provider identity has just linked that identity to the victim's
# account, and can sign in as them from then on.
#
# A handshake row is what closes it. It is created before the browser leaves,
# consumed exactly once when it comes back, and carries everything the callback
# needs to know — so nothing has to be trusted from the URL beyond the opaque
# state value that names the row.

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
        claimed = conn.execute(
            "UPDATE auth_handshakes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (ts, row["id"]),
        )
        if claimed.rowcount != 1:  # pragma: no cover — the write lock rules it out
            raise HandshakeError(HANDSHAKE_REJECTED)
        return row

    return await db.transaction(_work)


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
    secure = use_secure_cookie(settings, request)
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
    secure = use_secure_cookie(settings, request)
    preferred = HANDSHAKE_COOKIE_SECURE if secure else HANDSHAKE_COOKIE
    other = HANDSHAKE_COOKIE if secure else HANDSHAKE_COOKIE_SECURE
    return request.cookies.get(preferred) or request.cookies.get(other)


def clear_handshake_cookie(
    response: Response, settings: Settings, request: Request | None = None,
) -> None:
    """Drop both names once a callback has been resolved, so a stale value can't
    be paired with a later state."""
    secure = use_secure_cookie(settings, request)
    response.delete_cookie(HANDSHAKE_COOKIE, path="/", httponly=True, samesite="lax", secure=False)
    response.delete_cookie(
        HANDSHAKE_COOKIE_SECURE, path="/", httponly=True, samesite="lax", secure=secure or True,
    )


def handshake_cookie_matches(request: Request, settings: Settings, state: str | None) -> bool:
    held = read_handshake_cookie(request, settings)
    return bool(held and state and secrets.compare_digest(held, state))


# ---------------------------------------------------------------------------
# linked identities
# ---------------------------------------------------------------------------
# What a completed handshake produces. `provider_user_id` is always the
# provider's immutable numeric account id (see insert_linked_identity), and the
# UNIQUE (provider, provider_user_id) index is what makes "this account is
# already known, sign its owner in" a single lookup.


@dataclass(frozen=True)
class ProviderIdentity:
    """One provider account, as a completed authorization describes it."""
    provider: str
    provider_user_id: str
    display_name: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: int | None = None


@dataclass(frozen=True)
class LoginOutcome:
    """What a provider authorization resolved to.

    `kind` is "login" (a known identity), "registered" (a new account), or
    "linked" (an additional identity on an account that was already signed in).
    """
    kind: str
    user_id: int
    calendar_approved: bool


class IdentityInUse(Exception):
    """The provider account is already linked to a DIFFERENT local account.

    Never resolved by moving the link. Silently reassigning it would mean
    whoever authorizes last owns the identity, which is a takeover primitive
    handed out for free.
    """


class RegistrationRefused(Exception):
    """A provider sign-in would have created an account, and may not.

    Either no usable invite travelled with the handshake, or the instance is
    not accepting registrations.
    """


class AccountUnavailable(Exception):
    """The identity resolved to an account that cannot be signed in to."""


class LastLoginMethod(Exception):
    """Unlinking would leave the account with no way to sign in at all."""


async def find_identity(provider: str, provider_user_id: str | int):
    return await db.fetch_one(
        "SELECT * FROM linked_identities WHERE provider = ? AND provider_user_id = ?",
        (provider, str(provider_user_id)),
    )


async def list_identities(user_id: int):
    return await db.fetch_all(
        "SELECT * FROM linked_identities WHERE user_id = ? ORDER BY provider", (user_id,),
    )


def _refresh_identity(
    conn: db.Connection, identity_id: int, identity: ProviderIdentity, ts: int,
) -> None:
    """SYNCHRONOUS. Write the newest token pair and display name onto an
    existing identity row.

    The display name is refreshed on every sign-in because it is only ever shown
    to the user, and a stale one on the account page is confusing; nothing keys
    off it, so refreshing it is free.
    """
    conn.execute(
        "UPDATE linked_identities SET display_name = ?, access_token = ?, refresh_token = ?, "
        "token_expires_at = ?, refreshing_until = NULL, last_login_at = ? WHERE id = ?",
        (identity.display_name, identity.access_token, identity.refresh_token,
         identity.token_expires_at, ts, identity_id),
    )


async def link_provider_identity(*, identity: ProviderIdentity, user_id: int) -> LoginOutcome:
    """Attach a provider account to the signed-in account, or refuse.

    Raises IdentityInUse when the provider account already belongs to someone
    else here. Re-linking one this account already holds is not an error — it
    just refreshes the stored token, which is what a user clicking "reconnect"
    is asking for.
    """
    ts = db.now()

    def _work(conn: db.Connection) -> LoginOutcome:
        user = conn.execute(
            "SELECT calendar_approved, is_disabled FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if user is None or user["is_disabled"]:
            raise AccountUnavailable()
        existing = conn.execute(
            "SELECT * FROM linked_identities WHERE provider = ? AND provider_user_id = ?",
            (identity.provider, identity.provider_user_id),
        ).fetchone()
        if existing is not None:
            if int(existing["user_id"]) != user_id:
                raise IdentityInUse()
            _refresh_identity(conn, int(existing["id"]), identity, ts)
        else:
            insert_linked_identity(
                conn, user_id=user_id, provider=identity.provider,
                provider_user_id=identity.provider_user_id,
                display_name=identity.display_name, access_token=identity.access_token,
                refresh_token=identity.refresh_token,
                token_expires_at=identity.token_expires_at, now=ts,
            )
        return LoginOutcome("linked", user_id, bool(user["calendar_approved"]))

    return await db.transaction(_work)


async def login_with_provider_identity(
    *,
    identity: ProviderIdentity,
    invite_token: str | None = None,
    ip_address: str | None = None,
    settings: Settings | None = None,
) -> LoginOutcome:
    """Sign in with a provider account, registering one if it is unknown.

    A known identity signs its owner in. An unknown one is a REGISTRATION, and
    registration needs a usable invite unless the operator has opened the
    instance up — a provider sign-in proves only that somebody controls some
    account on that service, which is not a membership test for anything here.
    Without a usable invite this raises RegistrationRefused and NO account is
    created.

    The whole registration — account, preferences, identity, invite redemption —
    is one transaction, and the invite is re-read inside it because the quota
    check before it ran without the write lock held.
    """
    cfg = settings or load_settings()
    ts = db.now()

    existing = await find_identity(identity.provider, identity.provider_user_id)
    if existing is not None:
        user = await get_user(int(existing["user_id"]))
        if user is None or user["is_disabled"]:
            raise AccountUnavailable()

        def _sign_in(conn: db.Connection) -> LoginOutcome:
            _refresh_identity(conn, int(existing["id"]), identity, ts)
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (ts, user["id"]))
            return LoginOutcome("login", int(user["id"]), bool(user["calendar_approved"]))

        return await db.transaction(_sign_in)

    invite_required = not cfg.allow_open_registration
    token = (invite_token or "").strip()
    invite = await find_invite_by_token(token) if token else None
    if invite_required and not invite_is_usable(invite, ts):
        raise RegistrationRefused()

    def _register(conn: db.Connection) -> LoginOutcome:
        row = None
        if token:
            candidate = conn.execute(
                "SELECT * FROM invites WHERE token = ?", (token,),
            ).fetchone()
            usable = invite_is_usable(candidate, ts)
            if invite_required and not usable:
                raise RegistrationRefused()
            # Under open registration a stale token doesn't block the
            # registration; it just doesn't grant anything either.
            row = candidate if usable else None
        grants_calendar = bool(row["grants_calendar_on_accept"]) if row is not None else False
        # No username and no password: this account's only credential is the
        # provider identity below. One can be added later from the account page.
        user_id = insert_user(
            conn, username=None, password_hash=None, calendar_approved=grants_calendar,
            distrakt_approved=False, timezone=cfg.timezone or None, now=ts,
        )
        insert_user_prefs(conn, user_id, cfg)
        insert_linked_identity(
            conn, user_id=user_id, provider=identity.provider,
            provider_user_id=identity.provider_user_id, display_name=identity.display_name,
            access_token=identity.access_token, refresh_token=identity.refresh_token,
            token_expires_at=identity.token_expires_at, now=ts,
        )
        if row is not None:
            redeem_invite(conn, invite=row, user_id=user_id, ip_address=ip_address, now=ts)
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (ts, user_id))
        return LoginOutcome("registered", user_id, grants_calendar)

    try:
        return await db.transaction(_register)
    except db.IntegrityError as exc:
        # The UNIQUE (provider, provider_user_id) index: another request
        # registered this same provider account between the lookup above and
        # this insert. It belongs to that account now, not this one.
        raise IdentityInUse() from exc


async def unlink_identity(user_id: int, provider: str) -> bool:
    """Remove a linked provider account. False when there was none to remove.

    Raises LastLoginMethod when this is the account's only remaining way in — an
    account with no password and no identities cannot be signed in to by anyone,
    including its owner, and there is no self-service recovery from that. An
    administrator can still unlink anything, and is warned when it orphans an
    account.
    """
    def _work(conn: db.Connection) -> bool:
        row = conn.execute(
            "SELECT id FROM linked_identities WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ).fetchone()
        if row is None:
            return False
        user = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        remaining = int(conn.execute(
            "SELECT COUNT(*) FROM linked_identities WHERE user_id = ? AND id != ?",
            (user_id, row["id"]),
        ).fetchone()[0])
        if remaining == 0 and not (user and user["password_hash"]):
            raise LastLoginMethod()
        conn.execute("DELETE FROM linked_identities WHERE id = ?", (row["id"],))
        return True

    return await db.transaction(_work)


# ---------------------------------------------------------------------------
# token refresh serialization
# ---------------------------------------------------------------------------
# Providers issue a NEW refresh token every time one is spent and invalidate the
# old one, so two requests refreshing the same identity at the same moment both
# succeed against the provider and then overwrite each other — leaving the row
# holding a refresh token that was already replaced, and the user silently
# logged out of the integration. `refreshing_until` is a lease over the row that
# lets exactly one of them proceed.

REFRESH_LEASE_SECONDS = 60


async def claim_identity_refresh(
    identity_id: int, *, now: int | None = None, lease_seconds: int = REFRESH_LEASE_SECONDS,
) -> bool:
    """Take the refresh lease on an identity. False means somebody else has it.

    One conditional UPDATE, which SQLite runs as its own transaction, so the
    check and the claim cannot be interleaved. The lease expires on its own so
    that a process which dies mid-refresh doesn't wedge the row forever.
    """
    ts = db.now() if now is None else now
    result = await db.execute(
        "UPDATE linked_identities SET refreshing_until = ? "
        "WHERE id = ? AND (refreshing_until IS NULL OR refreshing_until <= ?)",
        (ts + lease_seconds, identity_id, ts),
    )
    return result.rowcount > 0


async def release_identity_refresh(identity_id: int) -> None:
    """Drop the lease without writing a token — for a refresh that failed."""
    await db.execute(
        "UPDATE linked_identities SET refreshing_until = NULL WHERE id = ?", (identity_id,),
    )


async def store_identity_tokens(
    identity_id: int,
    *,
    access_token: str | None,
    refresh_token: str | None,
    token_expires_at: int | None,
) -> None:
    """Persist a renewed token pair and release the refresh lease."""
    await db.execute(
        "UPDATE linked_identities SET access_token = ?, refresh_token = ?, "
        "token_expires_at = ?, refreshing_until = NULL WHERE id = ?",
        (access_token, refresh_token, token_expires_at, identity_id),
    )


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------

def use_secure_cookie(settings: Settings, request: Request | None = None) -> bool:
    """Whether the session cookie gets the Secure flag.

    "always" is the default and does not consult the request at all. That is
    deliberate: behind a TLS-terminating reverse proxy the app itself is served
    over plain HTTP, so scheme detection reports "http" and would ship session
    cookies WITHOUT Secure on exactly the deployments that most need it. "never"
    exists for genuine plain-HTTP LAN use and "auto" for anyone who wants the
    detection anyway.
    """
    mode = (getattr(settings, "cookie_secure", "always") or "always").strip().lower()
    if mode == "never":
        return False
    if mode == "auto":
        return request_is_https(request, settings)
    return True


def request_is_https(request: Request | None, settings: Settings) -> bool:
    """Whether the browser reached this app over TLS.

    Public because the same answer is needed when reconstructing this instance's
    own origin: behind a TLS-terminating proxy the request itself arrives over
    plain HTTP, so anything comparing against a browser-sent `Origin` has to
    resolve the scheme the same way this does or it will disagree with every
    real request.
    """
    if request is None:
        return True  # nothing to inspect: fail closed and keep Secure on
    if request.url.scheme == "https":
        return True
    if _peer_is_trusted_proxy(request, settings):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            return True
    return False


def session_cookie_name(settings: Settings, request: Request | None = None) -> str:
    return COOKIE_NAME_SECURE if use_secure_cookie(settings, request) else COOKIE_NAME


def read_session_cookie(request: Request, settings: Settings | None = None) -> str | None:
    """The session id from the request, checking both cookie names so that
    flipping the Secure policy doesn't log the whole instance out."""
    cfg = settings or load_settings()
    preferred = session_cookie_name(cfg, request)
    other = COOKIE_NAME if preferred == COOKIE_NAME_SECURE else COOKIE_NAME_SECURE
    return request.cookies.get(preferred) or request.cookies.get(other)


def set_session_cookie(
    response: Response,
    session_id: str,
    settings: Settings,
    request: Request | None = None,
) -> None:
    """Issue the session cookie: HttpOnly, SameSite=Lax, Path=/, no Domain, and
    Secure (with the `__Host-` name) unless the Secure policy says otherwise.

    Max-Age is the absolute cap rather than the sliding window. The session row
    is the authority on both clocks, so a cookie that outlives its row is simply
    rejected — whereas a cookie expiring at the sliding window would log out the
    active user that sliding exists to keep signed in.
    """
    secure = use_secure_cookie(settings, request)
    response.set_cookie(
        key=COOKIE_NAME_SECURE if secure else COOKIE_NAME,
        value=session_id,
        max_age=SESSION_ABSOLUTE_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(
    response: Response,
    settings: Settings,
    request: Request | None = None,
) -> None:
    """Delete both cookie names, since the browser may be holding either."""
    secure = use_secure_cookie(settings, request)
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=False)
    response.delete_cookie(
        COOKIE_NAME_SECURE, path="/", httponly=True, samesite="lax", secure=secure or True,
    )


# ---------------------------------------------------------------------------
# client IP
# ---------------------------------------------------------------------------

_FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")
_warned_default_proxy = False


def parse_trusted_networks(spec: str | None) -> list[ipaddress._BaseNetwork]:
    """Parse the comma-separated CIDR list.

    An unparseable entry is dropped with a warning rather than raising: a typo in
    an admin-editable settings field must not make the instance unbootable.
    """
    networks: list[ipaddress._BaseNetwork] = []
    for raw in (spec or "").split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("Ignoring unparseable trusted_proxy_ips entry: %r", token)
    return networks


def _is_trusted(addr: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def _peer_is_trusted_proxy(request: Request, settings: Settings) -> bool:
    peer = request.client.host if request.client else None
    if not peer:
        return False
    return _is_trusted(peer, parse_trusted_networks(getattr(settings, "trusted_proxy_ips", "")))


def client_ip(request: Request, settings: Settings | None = None) -> str:
    """The caller's IP address, honoring X-Forwarded-For only when the immediate
    peer is a configured trusted proxy.

    Walks the forwarded chain right to left, skipping hops that are themselves
    trusted, so the result is the last address the trusted infrastructure
    actually observed rather than whatever the client claimed at the front of the
    header. An untrusted peer gets its own address and its forwarded headers
    ignored entirely — trusting them would let anyone spoof an IP and slip a
    per-IP rate limit.
    """
    cfg = settings or load_settings()
    peer = (request.client.host if request.client else "") or "unknown"
    networks = parse_trusted_networks(getattr(cfg, "trusted_proxy_ips", ""))
    _maybe_warn_default_proxy(request, cfg)

    if not _is_trusted(peer, networks):
        return peer

    forwarded = request.headers.get("x-forwarded-for") or ""
    chain = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not chain:
        real = (request.headers.get("x-real-ip") or "").strip()
        return real or peer
    for candidate in reversed(chain):
        if not _is_trusted(candidate, networks):
            return candidate
    # Every hop is our own infrastructure; the leftmost is the closest thing to a
    # real client address available.
    return chain[0]


def _maybe_warn_default_proxy(request: Request, settings: Settings) -> None:
    """Warn once when the trusted-proxy list is still at its default while
    forwarded headers are actually arriving.

    That combination is almost always a misconfiguration, and it fails quietly in
    a way that matters: the headers get ignored, so every user collapses onto the
    proxy's address and per-IP rate limiting silently becomes global. Forwarded
    headers only exist per request, so this fires on the first request that shows
    the combination rather than at startup.
    """
    global _warned_default_proxy
    if _warned_default_proxy:
        return
    if (getattr(settings, "trusted_proxy_ips", "") or "").strip() != TRUSTED_PROXY_IPS_DEFAULT:
        return
    if not any(h in request.headers for h in _FORWARDED_HEADERS):
        return
    peer = request.client.host if request.client else "?"
    _warned_default_proxy = True
    logger.warning(
        "Forwarded headers are present but trusted_proxy_ips is still the default %s "
        "(request peer %s). The forwarded headers are being IGNORED, so every user "
        "looks like %s to rate limiting and the admin session list. Set "
        "trusted_proxy_ips in Settings to the proxy's address/CIDR.",
        TRUSTED_PROXY_IPS_DEFAULT, peer, peer,
    )


# ---------------------------------------------------------------------------
# authorization levels
# ---------------------------------------------------------------------------

class AuthLevel(str, Enum):
    """The authorization levels a route can require.

    Every route declares exactly one. PUBLIC is a declaration too, not an
    absence of one, so that a route which simply forgot to say can be told apart
    from a route that means it.
    """
    PUBLIC = "public"
    SESSION = "session"
    CALENDAR_APPROVED = "calendar_approved"
    DISTRAKT_APPROVED = "distrakt_approved"
    ADMIN = "admin"


class AuthError(HTTPException):
    """401/403 carrying a machine-readable `reason`, so an HTML route can turn a
    refusal into the right redirect without re-deriving why it was refused."""

    def __init__(self, status_code: int, reason: str, message: str):
        super().__init__(status_code=status_code, detail={"reason": reason, "error": message})
        self.reason = reason


_NO_USER = object()


async def current_user(request: Request) -> CurrentUser | None:
    """The signed-in user, or None. Never raises — for public routes that render
    differently when somebody is signed in.

    Cached on `request.state`, so several dependencies on one route cost one
    query between them rather than one each.
    """
    cached = getattr(request.state, "auth_user", _NO_USER)
    if cached is not _NO_USER:
        return cached  # type: ignore[return-value]
    settings = load_settings()
    user = await validate_session(read_session_cookie(request, settings))
    request.state.auth_user = user
    return user


async def require_session(request: Request) -> CurrentUser:
    """Signed in, whatever their approval state."""
    user = await current_user(request)
    if user is None:
        raise AuthError(401, "login_required", "Sign in to continue.")
    return user


async def require_calendar(request: Request) -> CurrentUser:
    """Signed in and approved for calendar access."""
    user = await require_session(request)
    if not user.calendar_approved:
        raise AuthError(403, "awaiting_approval", "Your account is awaiting admin approval.")
    return user


async def require_distrakt(request: Request) -> CurrentUser:
    """Signed in, approved for distrakt, and linked to Trakt.

    The Trakt link is not decoration: distrakt reads the requesting user's own
    watch history using their own token, so an account that only ever signed in
    with a password or with Plex has nothing for it to read.
    """
    user = await require_session(request)
    if not user.distrakt_approved:
        raise AuthError(403, "distrakt_not_approved", "distrakt access not yet approved.")
    if not user.has_trakt_identity:
        raise AuthError(403, "trakt_link_required", "Link your Trakt account to use distrakt.")
    return user


async def require_admin(request: Request) -> CurrentUser:
    """Signed in and an administrator."""
    user = await require_session(request)
    if not user.is_admin:
        raise AuthError(403, "admin_required", "Administrator access required.")
    return user


# Resolves a declared level to the dependency that enforces it. PUBLIC maps to
# None: a public route runs with no dependency at all and calls current_user()
# itself if it wants to know who is looking.
DEPENDENCY_FOR_LEVEL: dict[AuthLevel, Callable | None] = {
    AuthLevel.PUBLIC: None,
    AuthLevel.SESSION: require_session,
    AuthLevel.CALENDAR_APPROVED: require_calendar,
    AuthLevel.DISTRAKT_APPROVED: require_distrakt,
    AuthLevel.ADMIN: require_admin,
}
