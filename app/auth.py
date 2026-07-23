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
from urllib.parse import urlsplit

import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

from . import db, encryption_flow, secrets_box
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


def insert_user_prefs(conn: db.Connection, user_id: int, settings: Settings,
                      *, seed_filters: bool = False) -> None:
    """SYNCHRONOUS. Seeds a user's view preferences from settings.json's app-wide
    values.

    Those settings.json fields are a SEED, not a live source: once this row
    exists, editing settings.json affects new users only, never this one.

    The genre/country/network FILTERS are excluded from that seed unless
    `seed_filters` is set, and only the first-run onboarding sets it. A filter
    removes shows from someone's calendar without ever telling them a filter
    exists, so it is not something to inherit from an instance's configuration —
    a new account starts seeing everything and narrows it down itself. Onboarding
    is the one exception, because there the settings are the operator's own from
    before this instance had accounts, and their calendar has to keep rendering
    as it did.
    """
    conn.execute(
        "INSERT INTO user_prefs (user_id, endpoint, card_style, day_packing, "
        "hide_not_watching, network_filter_json, genres, countries) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, settings.endpoint, settings.card_style, settings.day_packing,
            int(bool(settings.hide_not_watching)),
            json.dumps(list(settings.network_filter or [])) if seed_filters else "[]",
            (settings.genres or "") if seed_filters else "",
            (settings.countries or "") if seed_filters else "",
        ),
    )


async def get_user_prefs(user_id: int) -> dict:
    """A user's view preferences, in the shape the calendar read path and the
    per-user pref-write endpoint both use. Falls back to empty/default values if
    the row is somehow missing (it is created alongside every user, but a
    fallback here is cheap and keeps this a total function)."""
    row = await db.fetch_one(
        "SELECT endpoint, card_style, day_packing, hide_not_watching, "
        "network_filter_json, genres, countries FROM user_prefs WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return {
            "endpoint": None, "card_style": None, "day_packing": None,
            "hide_not_watching": False, "network_filter": [], "genres": "", "countries": "",
        }
    return {
        "endpoint": row["endpoint"],
        "card_style": row["card_style"],
        "day_packing": row["day_packing"],
        "hide_not_watching": bool(row["hide_not_watching"]),
        "network_filter": json.loads(row["network_filter_json"] or "[]"),
        "genres": row["genres"] or "",
        "countries": row["countries"] or "",
    }


# Columns a caller may update through update_user_prefs, keyed by the dict key
# it's passed under (network_filter/hide_not_watching need a transform on the
# way into their column; the rest write straight through).
_USER_PREF_FIELDS = frozenset({
    "endpoint", "card_style", "day_packing", "hide_not_watching",
    "network_filter", "genres", "countries",
})


async def update_user_prefs(user_id: int, **fields) -> None:
    """Persist a partial update to one user's view preferences. Unknown keys and
    None values are ignored, so a caller can pass through a request body's dict
    as-is without first stripping out whatever it didn't set."""
    updates = {k: v for k, v in fields.items() if k in _USER_PREF_FIELDS and v is not None}
    if not updates:
        return
    columns: list[str] = []
    params: list = []
    for key, value in updates.items():
        if key == "hide_not_watching":
            columns.append("hide_not_watching = ?")
            params.append(int(bool(value)))
        elif key == "network_filter":
            columns.append("network_filter_json = ?")
            params.append(json.dumps(list(value)))
        else:
            columns.append(f"{key} = ?")
            params.append(value)
    params.append(user_id)
    await db.execute(f"UPDATE user_prefs SET {', '.join(columns)} WHERE user_id = ?", tuple(params))


async def set_user_timezone(user_id: int, tz: str) -> None:
    """Persist the viewer's saved timezone. Validating that `tz` is a real IANA
    zone is the caller's job (it needs zoneinfo either way, to build the picker)."""
    await db.execute(
        "UPDATE users SET timezone = ?, updated_at = ? WHERE id = ?", (tz, db.now(), user_id),
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

    `provider_user_id` MUST be the provider's immutable, non-reassignable account
    handle — never a username, slug, or email, which can be changed by their
    owner, released, and re-registered by somebody else, who would then inherit
    this link. Per provider that is:

      Plex:  the numeric account id from /api/v2/user.
      Trakt: the account UUID from /users/settings. Trakt users have NO numeric
             id — `ids` on a user is `{"slug": ...}` — so the UUID is the whole
             of what is available (see trakt_auth.fetch_account).

    Stored as TEXT for exactly that reason: the two providers do not agree on a
    type, and the column has to hold whichever each one actually issues.
    """
    ts = db.now() if now is None else now
    # The token pair is sealed at rest when a key is configured (a pass-through
    # otherwise); it is opened again at the point it is used to call a provider, in
    # app/trakt_routes.py. seal(None) stays None, so an identity that carries no
    # token (e.g. a Plex link) writes NULLs exactly as before.
    cur = conn.execute(
        "INSERT INTO linked_identities (user_id, provider, provider_user_id, display_name, "
        "access_token, refresh_token, token_expires_at, created_at, last_login_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, provider, str(provider_user_id), display_name,
         secrets_box.seal(access_token), secrets_box.seal(refresh_token),
         token_expires_at, ts, ts),
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


async def list_invite_redemptions(invite_id: int):
    """Who has redeemed a given invite, newest first — for the admin invites
    screen. LEFT JOIN because the redeeming user could since have been deleted;
    the redemption row still records that it happened."""
    return await db.fetch_all(
        "SELECT r.*, u.username FROM invite_redemptions r "
        "LEFT JOIN users u ON u.id = r.user_id WHERE r.invite_id = ? ORDER BY r.redeemed_at DESC",
        (invite_id,),
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
    # A username may not shadow another user's custom share slug (§1.10's
    # cross-namespace rule, checked in the other direction by
    # share_links.slug_error) — queried directly rather than importing
    # app.share_links, which itself imports this module.
    if await db.fetch_one("SELECT 1 FROM share_links WHERE custom_slug = ?", (candidate,)):
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
# The per-address counter is a DIFFERENT job from the per-username one and needs a
# different threshold. Per-username at 5 is the precise defence: it protects one
# account from being guessed at. Per-address exists only to stop one attacker
# spraying MANY usernames from one place — and it is shared by everybody behind
# that address, which on a home instance is every user, and behind a reverse proxy
# is the entire internet-facing side of the app.
#
# At 5 it did the wrong thing spectacularly: five wrong passwords on ONE account
# locked out EVERY account from that address, administrator included, with the
# generic "invalid username or password" and nothing in the log. The address
# limit has to sit far above anything one person fumbling a password produces,
# because the cost of tripping it is borne by people who did nothing.
LOGIN_IP_MAX_ATTEMPTS = 25
REGISTER_MAX_ATTEMPTS = 10
REGISTER_WINDOW_SECONDS = 60 * 60
INVITE_MAX_ATTEMPTS = 10
INVITE_WINDOW_SECONDS = 60 * 60
# The provider sign-in START routes. They are unauthenticated GETs that write a
# handshake row and — for Plex — call plex.tv before anybody has proved anything,
# so they are the one pre-auth path that costs this instance an outbound request.
# Generous, because a person retrying a flaky popup must never hit it: 30 in ten
# minutes is far more than any human does and far less than a script wants.
HANDSHAKE_MAX_ATTEMPTS = 30
HANDSHAKE_WINDOW_SECONDS = 10 * 60
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
    followed by the right one doesn't count against whoever just succeeded.

    A pure read. check_lockout() is what the sign-in paths call — it also clears
    a lockout that has served its time.
    """
    ts = db.now() if now is None else now
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
        "AND succeeded = 0 AND attempted_at > ?",
        (key_type, key_value, ts - window_seconds), default=0,
    )
    return int(count) >= max_attempts


async def check_lockout(
    key_type: str, key_value: str, *, max_attempts: int, window_seconds: int, now: int | None = None,
) -> bool:
    """Whether this key is locked out right now, RESETTING the counter when a
    lockout has expired.

    Without the reset, a lockout does not really end after `window_seconds`: the
    failures that caused it age out one at a time, so the count sits at
    max_attempts-1 and the very next mistake re-locks the key immediately. That
    is a lockout that quietly becomes permanent for anyone still using the
    account. Once a key has served a full window without reaching the threshold
    again, its history is dropped and it starts from zero.

    The caller must NOT record an attempt when this returns True — see the
    sign-in handlers. Counting attempts made while locked out lets a retry loop
    keep refilling the window and hold the lockout open indefinitely, which is
    denial of service against the account holder rather than protection for them.
    """
    ts = db.now() if now is None else now
    cutoff = ts - window_seconds

    def _work(conn: db.Connection) -> bool:
        recent = int(conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
            "AND succeeded = 0 AND attempted_at > ?",
            (key_type, key_value, cutoff),
        ).fetchone()[0])
        if recent >= max_attempts:
            return True
        # Not locked now, but there is history. If it ever reached the threshold
        # it was a lockout that has since lapsed, so wipe the slate rather than
        # leaving a primed counter behind.
        total = int(conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
            "AND succeeded = 0",
            (key_type, key_value),
        ).fetchone()[0])
        if total >= max_attempts:
            conn.execute(
                "DELETE FROM login_attempts WHERE key_type = ? AND key_value = ?",
                (key_type, key_value),
            )
        return False

    return await db.transaction(_work)


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


async def handshake_start_limited(request: Request, settings: Settings | None = None) -> bool:
    """Whether this address has started too many provider sign-ins, recording
    this one either way.

    Shared by both providers' start routes so one address cannot get a fresh
    budget by alternating between them. Volume-only, like the registration and
    share-page limiters: there is no notion of a "failed" start.
    """
    cfg = settings or load_settings()
    ip = client_ip(request, cfg)
    limited = await rate_limited(
        "handshake_ip", ip,
        max_attempts=HANDSHAKE_MAX_ATTEMPTS, window_seconds=HANDSHAKE_WINDOW_SECONDS,
    )
    await record_attempt("handshake_ip", ip, True)
    return limited


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
# provider's immutable account handle (see insert_linked_identity), and the
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


class IdentityWritesBlocked(Exception):
    """A link/relink was refused because the encryption key is unhealthy.

    Linking an identity that already exists here calls _refresh_identity, which
    overwrites the row's stored tokens outright — exactly the same overwrite
    save_settings() already refuses for app-level secrets while the key is
    missing or wrong, and for the same reason: sealing is a pass-through
    without a working key, so the fresh tokens would land as plaintext over
    ciphertext the original key could still recover.
    """


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
        (identity.display_name, secrets_box.seal(identity.access_token),
         secrets_box.seal(identity.refresh_token), identity.token_expires_at, ts, identity_id),
    )


async def link_provider_identity(*, identity: ProviderIdentity, user_id: int) -> LoginOutcome:
    """Attach a provider account to the signed-in account, or refuse.

    Raises IdentityInUse when the provider account already belongs to someone
    else here. Re-linking one this account already holds is not an error — it
    just refreshes the stored token, which is what a user clicking "reconnect"
    is asking for — which is exactly why IdentityWritesBlocked is checked here
    first: that refresh overwrites the row's existing tokens unconditionally,
    so it is refused up front rather than partway through, the same guard
    save_settings() already applies to app-level secrets.
    """
    if encryption_flow.secret_writes_blocked():
        raise IdentityWritesBlocked()
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


async def unlink_identity(user_id: int, provider: str, *, force: bool = False) -> bool:
    """Remove a linked provider account. False when there was none to remove.

    Raises LastLoginMethod when this is the account's only remaining way in — an
    account with no password and no identities cannot be signed in to by anyone,
    including its owner, and there is no self-service recovery from that. Every
    caller except the admin screen leaves `force` at its default, so the
    self-service unlink endpoint keeps refusing exactly as before; the admin
    screen sets it only after showing the operator the same warning and asking
    them to confirm the orphan deliberately.
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
        if remaining == 0 and not (user and user["password_hash"]) and not force:
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
    """Persist a renewed token pair and release the refresh lease. The pair is
    sealed at rest when a key is configured, matching the other identity writers."""
    await db.execute(
        "UPDATE linked_identities SET access_token = ?, refresh_token = ?, "
        "token_expires_at = ?, refreshing_until = NULL WHERE id = ?",
        (secrets_box.seal(access_token), secrets_box.seal(refresh_token),
         token_expires_at, identity_id),
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


def browser_scheme(request: Request | None) -> str | None:
    """The scheme the BROWSER used, per its own headers — or None if it didn't say.

    This is deliberately not `request.url.scheme`, which behind a TLS-terminating
    proxy reports the plain HTTP hop between the proxy and this app rather than
    the HTTPS the browser is actually on. `Origin` carries the browser's own
    origin and is set on every mutating fetch; `Referer` covers navigations.

    Crucially it does NOT depend on `trusted_proxy_ips` being correct, which is
    what makes it usable where `X-Forwarded-Proto` is not: an instance whose
    proxy list is still at the default ignores forwarded headers entirely, and
    that is exactly the instance most likely to be misconfigured.
    """
    if request is None:
        return None
    for header in ("origin", "referer"):
        value = (request.headers.get(header) or "").strip()
        # A sandboxed or privacy-stripped Origin arrives as the literal "null".
        if not value or value.lower() == "null":
            continue
        scheme = urlsplit(value).scheme.lower()
        if scheme in ("http", "https"):
            return scheme
    return None


def detect_cookie_secure(request: Request | None) -> str:
    """The `cookie_secure` value this browser's connection calls for.

    "never" only when the browser positively reports plain HTTP. Anything else —
    HTTPS, or a request that says nothing at all — resolves to "always", because
    the cost of being wrong is asymmetric: a needlessly Secure cookie fails
    loudly and immediately during setup, while a needlessly insecure one fails
    silently and permanently for every user afterwards.
    """
    return "never" if browser_scheme(request) == "http" else "always"


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
    if peer_is_trusted_proxy(request, settings):
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


def peer_is_trusted_proxy(request: Request, settings: Settings) -> bool:
    """Whether the immediate peer is inside the configured trusted-proxy set —
    i.e. whether this request's forwarded headers are honored at all. Public
    because the Settings screen reports it back to the operator."""
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
# admin operations
# ---------------------------------------------------------------------------
# Everything the admin screen does to another account. Kept here rather than in
# the route module so the business rules — the last-admin guard chief among
# them — have exactly one implementation regardless of how many HTTP routes end
# up calling them.


class UserNotFound(Exception):
    """The target account does not exist."""


class LastAdmin(Exception):
    """The instance's last remaining administrator cannot be demoted, disabled,
    or deleted — there would be no account left able to run the admin screen at
    all, including to reverse the mistake."""


class CannotDeleteSelf(Exception):
    """An administrator cannot delete their own account.

    Demoting it and deleting it from another admin's account works; this just
    rules out the one-click way to lock yourself out of your own instance.
    """


def _admin_count(conn: db.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0])


async def list_users_overview() -> list[dict]:
    """One row per account, shaped for the admin list: a display name (the
    username, or a linked identity's display name when there is no username),
    every linked provider, the three approval/disabled flags, and both
    activity timestamps.

    Two queries rather than one GROUP_CONCAT join, because an account can hold
    up to two linked identities and the "no username" fallback needs a
    specific one's display name, not a flattened string of both.
    """
    users = await db.fetch_all("SELECT * FROM users ORDER BY created_at ASC")
    identities = await db.fetch_all(
        "SELECT user_id, provider, display_name FROM linked_identities ORDER BY provider"
    )
    by_user: dict[int, list[dict]] = {}
    for row in identities:
        by_user.setdefault(int(row["user_id"]), []).append(
            {"provider": row["provider"], "display_name": row["display_name"]}
        )
    sessions = await db.fetch_all(
        "SELECT user_id, MAX(last_seen_at) AS last_seen_at FROM sessions GROUP BY user_id"
    )
    last_seen = {int(row["user_id"]): row["last_seen_at"] for row in sessions}

    overview = []
    for u in users:
        uid = int(u["id"])
        idents = by_user.get(uid, [])
        display = u["username"] or next((i["display_name"] for i in idents if i["display_name"]), None) or f"user #{uid}"
        overview.append({
            "id": uid,
            "username": u["username"],
            "display_name": display,
            "providers": [i["provider"] for i in idents],
            "is_admin": bool(u["is_admin"]),
            "is_bootstrap": bool(u["is_bootstrap"]),
            "calendar_approved": bool(u["calendar_approved"]),
            "distrakt_approved": bool(u["distrakt_approved"]),
            "is_disabled": bool(u["is_disabled"]),
            "created_at": u["created_at"],
            "last_login_at": u["last_login_at"],
            "last_session_at": last_seen.get(uid),
        })
    return overview


async def display_name_for(user_id: int) -> str | None:
    """The same display name list_users_overview() computes, for one account —
    what an admin must type back to confirm deleting it. None when the account
    doesn't exist."""
    user = await get_user(user_id)
    if user is None:
        return None
    if user["username"]:
        return user["username"]
    for row in await list_identities(user_id):
        if row["display_name"]:
            return row["display_name"]
    return f"user #{user_id}"


async def set_calendar_approved(user_id: int, approved: bool) -> None:
    await db.execute(
        "UPDATE users SET calendar_approved = ?, updated_at = ? WHERE id = ?",
        (int(approved), db.now(), user_id),
    )


async def set_distrakt_approved(user_id: int, approved: bool) -> None:
    await db.execute(
        "UPDATE users SET distrakt_approved = ?, updated_at = ? WHERE id = ?",
        (int(approved), db.now(), user_id),
    )


async def set_admin(user_id: int, is_admin: bool) -> None:
    """Promote or demote. Raises LastAdmin rather than demoting the instance's
    only administrator."""
    def _work(conn: db.Connection) -> None:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound()
        if not is_admin and row["is_admin"] and _admin_count(conn) <= 1:
            raise LastAdmin()
        conn.execute(
            "UPDATE users SET is_admin = ?, updated_at = ? WHERE id = ?",
            (int(is_admin), db.now(), user_id),
        )

    await db.transaction(_work)


async def set_disabled(user_id: int, disabled: bool) -> None:
    """Disable or re-enable an account.

    Disabling deletes every session that account holds, on top of the flag
    itself — a disabled account that stayed signed in everywhere it already was
    would not actually be disabled. Raises LastAdmin rather than disabling the
    instance's only administrator.
    """
    def _work(conn: db.Connection) -> None:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound()
        if disabled and row["is_admin"] and _admin_count(conn) <= 1:
            raise LastAdmin()
        conn.execute(
            "UPDATE users SET is_disabled = ?, updated_at = ? WHERE id = ?",
            (int(disabled), db.now(), user_id),
        )
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    await db.transaction(_work)


async def admin_set_username(user_id: int, username: str) -> None:
    """Give an OAuth-only account a username, so it can also be given a
    password. An account created purely by a provider sign-in has neither —
    there is nothing for set_password() to attach a username-based login to
    until this has run once."""
    await db.execute(
        "UPDATE users SET username = ?, updated_at = ? WHERE id = ?",
        (username.strip().lower(), db.now(), user_id),
    )


async def list_sessions(user_id: int):
    return await db.fetch_all(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC", (user_id,),
    )


# Per-user tables cleared by wipe_user_data(), beyond the account row itself.
# Each entry is (table_name, user_id_column); a later table that holds per-user
# data appends its own entry here rather than teaching wipe_user_data a new
# special case.
WIPE_DATA_TABLES: tuple[tuple[str, str], ...] = (
    ("not_watching_shows", "user_id"),
    ("calendar_view_state", "user_id"),
    ("distrakt_shows", "user_id"),
    ("distrakt_months", "user_id"),
    ("distrakt_watch_state", "user_id"),
    ("distrakt_show_progress", "user_id"),
    ("distrakt_movie_watches", "user_id"),
    ("distrakt_prefs", "user_id"),
)


async def wipe_user_data(user_id: int) -> None:
    """Clear a user's calendar and distrakt data, disable the account, and log
    it out everywhere — while keeping the account itself, its linked
    identities, its username/slug, and its share links untouched.

    This is the reversible "start this person over" action: re-enabling the
    account afterwards is all it takes to undo it, and nothing is retired.
    delete_user() is the separate, permanent action for when that is not what
    is wanted.
    """
    def _work(conn: db.Connection) -> None:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound()
        for table, column in WIPE_DATA_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (user_id,))
        conn.execute(
            "UPDATE users SET is_disabled = 1, updated_at = ? WHERE id = ?", (db.now(), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    await db.transaction(_work)


async def delete_user(user_id: int, *, actor_user_id: int) -> None:
    """Permanently delete an account and everything under it, in one
    transaction.

    Every foreign key that references users(id) is ON DELETE CASCADE except
    invites.created_by, which is SET NULL so that deleting the admin who
    issued an invite doesn't revoke it out from under someone mid-redemption —
    so this single DELETE fans out to sessions, linked_identities,
    auth_handshakes, invite_redemptions, and share_links with no row left
    behind in any of them. The account's username, custom share slug, and
    share token are all recorded in retired_identifiers so a new registration
    can't silently inherit a `/u/<username>`, `/c/<slug>`, or `/s/<token>` link
    that was already shared. Raises CannotDeleteSelf or LastAdmin rather than
    performing either.
    """
    if user_id == actor_user_id:
        raise CannotDeleteSelf()

    def _work(conn: db.Connection) -> None:
        row = conn.execute(
            "SELECT username, is_admin FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if row is None:
            raise UserNotFound()
        if row["is_admin"] and _admin_count(conn) <= 1:
            raise LastAdmin()
        ts = db.now()
        if row["username"]:
            conn.execute(
                "INSERT OR IGNORE INTO retired_identifiers (kind, value, retired_at) "
                "VALUES ('username', ?, ?)",
                (row["username"], ts),
            )
        share = conn.execute(
            "SELECT custom_slug, token FROM share_links WHERE user_id = ?", (user_id,),
        ).fetchone()
        if share is not None:
            if share["custom_slug"]:
                conn.execute(
                    "INSERT OR IGNORE INTO retired_identifiers (kind, value, retired_at) "
                    "VALUES ('slug', ?, ?)",
                    (share["custom_slug"], ts),
                )
            conn.execute(
                "INSERT OR IGNORE INTO retired_identifiers (kind, value, retired_at) "
                "VALUES ('token', ?, ?)",
                (share["token"], ts),
            )
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    await db.transaction(_work)


async def list_retired_identifiers():
    return await db.fetch_all("SELECT * FROM retired_identifiers ORDER BY retired_at DESC")


async def release_retired_identifier(kind: str, value: str) -> bool:
    """Delete a retired-identifier block, making the name claimable again.

    Tokens are never releasable: they are random with no legitimate reason to
    reissue a specific one, unlike a username or slug someone might want back.
    """
    if kind == "token":
        raise ValueError("Share tokens cannot be released.")
    result = await db.execute(
        "DELETE FROM retired_identifiers WHERE kind = ? AND value = ?", (kind, value),
    )
    return result.rowcount > 0


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
    # Only the cookie policy is needed to read the session, never a credential, so
    # this skips decrypting the stored secrets. That keeps sign-in and the admin
    # dependency working even when a stored secret is sealed under a key the current
    # one cannot open — the state whose only fix is the admin recovery screen.
    settings = load_settings(open_secrets=False)
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
