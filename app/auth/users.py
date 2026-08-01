"""Accounts: the `users` row, plus the rules for what a username and a display
name are allowed to be.

Those two rule sets live here together because they are constantly confused for
each other and the comments below spell out the difference: a username is an
IDENTIFIER that other things are looked up by, a display name is a LABEL that is
only ever shown.
"""
from __future__ import annotations

import re

from .. import db
from ..config import Settings, load_settings
from . import passwords, prefs


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
    ranker_approved: bool = False,
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
        "is_bootstrap, calendar_approved, distrakt_approved, ranker_approved, is_disabled, "
        "timezone, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            username, password_hash, ts if password_hash else None,
            int(is_admin), int(is_bootstrap), int(calendar_approved), int(distrakt_approved),
            int(ranker_approved), timezone, ts, ts,
        ),
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
    ranker_approved: bool = False,
    timezone: str | None = None,
) -> int:
    """Create a user plus its seeded preferences row in one transaction.

    Hashing happens before the transaction opens: 200ms of Argon2 must not be
    holding SQLite's write lock while every other writer waits behind it.
    """
    cfg = settings or load_settings()
    password_hash = await passwords.hash_password(password) if password else None
    tz = timezone if timezone is not None else (cfg.timezone or None)

    def _work(conn: db.Connection) -> int:
        user_id = insert_user(
            conn, username=username, password_hash=password_hash, is_admin=is_admin,
            is_bootstrap=is_bootstrap, calendar_approved=calendar_approved,
            distrakt_approved=distrakt_approved, ranker_approved=ranker_approved, timezone=tz,
        )
        prefs.insert_user_prefs(conn, user_id, cfg)
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
    password_hash = await passwords.hash_password(password)
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


# ---------------------------------------------------------------------------
# display names
# ---------------------------------------------------------------------------
# A username and a display name are different things and are validated by
# different rules on purpose. The username above is an IDENTIFIER: lowercased,
# unique, and the basis of /u/<name> share links. The display name is a LABEL:
# it is shown, never looked up, so it may carry capitals and spaces and need not
# be unique.

# Comfortably under the ranker export's own 80-character ceiling for the name it
# draws on the image, so a name that saves here always fits there.
DISPLAY_NAME_MAX = 32

# Anything that would let a name lie about its own shape. C0/C1 controls cover
# newlines and NULs; the rest are the bidi controls and the invisible
# separators/joiners, which can reorder or hide the text rendered beside them.
#
# Written as CODE POINTS, never as literal characters: a source file containing
# the very characters it rejects cannot be reviewed by reading it, and a stray
# NUL in one stops the module compiling at all.
_DISPLAY_NAME_BANNED = frozenset(
    [chr(c) for c in range(0x00, 0x20)]          # C0 controls, incl. NUL and newline
    + [chr(0x7F)]                                # DEL
    + [chr(c) for c in range(0x80, 0xA0)]        # C1 controls
    + [chr(c) for c in range(0x200B, 0x2010)]    # ZWSP/ZWNJ/ZWJ and the bidi marks
    + [chr(c) for c in range(0x202A, 0x2030)]    # bidi embedding/override, separators
    + [chr(c) for c in range(0x2060, 0x206A)]    # word joiner, invisibles, bidi isolates
    + [chr(0xFEFF)]                              # BOM / zero-width no-break space
)


def normalize_display_name(value: str | None) -> str | None:
    """The form a display name is stored in, or None for "no name chosen".

    Outer whitespace is stripped and internal runs are collapsed to single
    spaces, so ``Josh   Black `` and ``Josh Black`` cannot be told apart on
    screen yet stored differently. CASE IS PRESERVED EXACTLY — that is the whole
    point of the column, so nothing here lowercases or title-cases.
    """
    text = " ".join((value or "").split())
    return text or None


def display_name_error(value: str | None) -> str | None:
    """None when `value` is a usable display name, otherwise why not.

    An empty value is NOT an error: it means "clear it and go back to my
    username", which is a thing the account page offers.
    """
    name = normalize_display_name(value)
    if name is None:
        return None
    if len(name) > DISPLAY_NAME_MAX:
        return f"Display names can be at most {DISPLAY_NAME_MAX} characters."
    if any(ch in _DISPLAY_NAME_BANNED for ch in name):
        return "That display name contains characters that aren't allowed."
    return None


async def set_display_name(user_id: int, value: str | None) -> str | None:
    """Store this account's display name (None clears it). Returns what was
    stored, so a caller can echo back the normalized form rather than the raw
    text it was handed."""
    name = normalize_display_name(value)
    await db.execute(
        "UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
        (name, db.now(), user_id),
    )
    return name


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
    # A username may not shadow another user's custom share slug: both live in
    # the same public URL namespace (/u/<name>), so letting them collide would
    # let a later registration silently hijack an earlier share link's path.
    # share_links.slug_error enforces the same rule in the other direction.
    # Queried directly rather than importing app.calendar.share_links, which itself
    # imports this module.
    if await db.fetch_one("SELECT 1 FROM share_links WHERE custom_slug = ?", (candidate,)):
        return "That username is taken."
    return None
