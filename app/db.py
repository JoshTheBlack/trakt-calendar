"""SQLite foundation — connection policy, migrations, and async helpers.

THIS IS THE ONLY MODULE IN THE APP THAT MAY `import sqlite3`, and there is a test
that enforces it. Everything else goes through the async helpers below, which
push every blocking call onto a worker thread via `anyio.to_thread.run_sync`.
The stdlib driver is blocking and every route in this app is `async def`, so a
direct call from a route would stall the whole event loop for the duration of
the query.

Connection policy, applied to EVERY connection:
  - journal_mode=WAL       — readers don't block on a writer.
  - foreign_keys=ON        — SQLite defaults this OFF, and it is a PER-CONNECTION
                             setting, not a property of the database file. Every
                             ON DELETE CASCADE in the schema is inert without it,
                             which is why it is set here rather than once at
                             creation time.
  - busy_timeout=5000      — wait for a competing writer instead of failing.
  - synchronous=NORMAL     — safe under WAL, much faster than FULL.

One connection per thread (`threading.local`), NOT one shared connection:
`check_same_thread` stays at its default True and the async helpers hand work to
a pool of threads.

Migrations are a forward-only ordered list of (version, sql-or-callable) applied
inside a transaction at startup, with the applied version recorded in
`schema_version`. Later work APPENDS to MIGRATIONS — an entry that has shipped is
never edited, and nothing outside this module creates its own schema.

TIMESTAMP CONVENTION: every timestamp column this schema owns is an INTEGER of
whole UTC seconds since the epoch (see now()). Timestamps that arrive from a
third party and are stored verbatim (Trakt's `watched_at`, for instance) stay
TEXT, because they are payload rather than our clock.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import anyio.to_thread

from .config import DATA_DIR, _ensure_data_dir

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Re-exported so other modules can type-annotate and catch constraint violations
# without importing sqlite3 themselves. `Connection` is here purely for
# annotations on the synchronous helpers that callers compose into a
# transaction() body.
Connection = sqlite3.Connection
IntegrityError = sqlite3.IntegrityError
DatabaseError = sqlite3.DatabaseError

DB_FILENAME = "app.db"

BUSY_TIMEOUT_MS = 5000

# Bumped by set_db_path() so a thread still holding a connection to the OLD path
# drops it on next use rather than silently reading a stale database.
_generation = 0
_db_path: Path = DATA_DIR / DB_FILENAME
_path_lock = threading.Lock()
_local = threading.local()


def now() -> int:
    """Current UTC time as whole seconds since the epoch — the one timestamp
    representation every column in this schema uses."""
    return int(time.time())


def db_path() -> Path:
    with _path_lock:
        return _db_path


def set_db_path(path: str | Path) -> None:
    """Point the module at a different database file.

    The generation bump makes every thread rebuild its connection lazily, so this
    is safe to call while other threads still hold connections to the old path.
    Tests use it to get a fresh database per case.
    """
    global _db_path, _generation
    with _path_lock:
        _db_path = Path(path)
        _generation += 1
    _drop_local_connection()


def _drop_local_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover — closing an already-dead handle
            pass
    _local.conn = None
    _local.generation = None


def _new_connection(path: Path) -> sqlite3.Connection:
    _ensure_data_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    # isolation_level=None means autocommit: transactions are opened explicitly
    # by transaction() with BEGIN IMMEDIATE rather than implicitly by the driver,
    # which is the only way to be sure where one starts and ends.
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    if fresh:
        # This file holds password hashes and third-party access tokens in the
        # clear, so it is owner-only. Same trust boundary as settings.json, which
        # already holds a plaintext Trakt token: whoever has filesystem access to
        # this instance.
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover — no-op on filesystems without modes
            pass
    return conn


def connection() -> sqlite3.Connection:
    """This thread's connection, opened on first use.

    SYNCHRONOUS and blocking — only call it from inside a worker function handed
    to run() or transaction().
    """
    with _path_lock:
        path, generation = _db_path, _generation
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) == generation:
        return conn
    if conn is not None:
        _drop_local_connection()
    conn = _new_connection(path)
    _local.conn = conn
    _local.generation = generation
    return conn


def close_thread_connection() -> None:
    """Close this thread's connection, if any. Tests use it between cases; the
    running app doesn't need it, since connections die with the process."""
    _drop_local_connection()


# ---------------------------------------------------------------------------
# async helpers — the only sanctioned way for other modules to touch the DB
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Result:
    """What a write returns: `lastrowid` after an INSERT, `rowcount` after an
    UPDATE or DELETE."""
    lastrowid: int | None
    rowcount: int


async def run(fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run `fn(conn)` on a worker thread with that thread's connection.

    The escape hatch for anything the helpers below don't express, such as a
    multi-statement read. A write that spans more than one statement belongs in
    transaction() instead, so a failure halfway through can't leave the database
    half-updated.
    """
    return await anyio.to_thread.run_sync(lambda: fn(connection()))


async def transaction(fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run `fn(conn)` inside BEGIN IMMEDIATE / COMMIT, rolling back on any error.

    IMMEDIATE rather than DEFERRED takes the write lock up front, so a
    read-then-write body — every "check whether this exists, then insert it" in
    this app — can't lose a race to a writer that slipped in between the two
    halves.
    """
    def _work() -> T:
        conn = connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result

    return await anyio.to_thread.run_sync(_work)


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return await run(lambda conn: conn.execute(sql, params).fetchone())


async def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return await run(lambda conn: conn.execute(sql, params).fetchall())


async def fetch_value(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    """First column of the first row, or `default` when there is no row."""
    row = await fetch_one(sql, params)
    return default if row is None else row[0]


async def execute(sql: str, params: Sequence[Any] = ()) -> Result:
    def _work(conn: sqlite3.Connection) -> Result:
        cur = conn.execute(sql, params)
        return Result(lastrowid=cur.lastrowid, rowcount=cur.rowcount)

    return await run(_work)


async def executemany(sql: str, rows: Iterable[Sequence[Any]]) -> Result:
    materialized = list(rows)

    def _work(conn: sqlite3.Connection) -> Result:
        cur = conn.executemany(sql, materialized)
        return Result(lastrowid=cur.lastrowid, rowcount=cur.rowcount)

    return await run(_work)


# ---------------------------------------------------------------------------
# migrations
# ---------------------------------------------------------------------------

# Migration 1 — accounts, sessions, linked provider identities, invites, and the
# supporting tables for login rate limiting and OAuth/PIN handshakes. Tables
# only; the flows that read and write them are built on top separately.
MIGRATION_1 = """
CREATE TABLE users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Nullable: an account created purely by logging in with Plex or Trakt may
    -- never set one. NOCASE because `Admin` and `admin` must be the same
    -- account, not two.
    username            TEXT UNIQUE COLLATE NOCASE,
    password_hash       TEXT,
    -- Exists so "log out everywhere on password change" is enforceable after
    -- the fact rather than only at the moment of the change.
    password_changed_at INTEGER,
    is_admin            INTEGER NOT NULL DEFAULT 0,
    is_bootstrap        INTEGER NOT NULL DEFAULT 0,
    calendar_approved   INTEGER NOT NULL DEFAULT 0,
    distrakt_approved   INTEGER NOT NULL DEFAULT 0,
    is_disabled         INTEGER NOT NULL DEFAULT 0,
    timezone            TEXT,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    last_login_at       INTEGER
);
-- At most one bootstrap account, ever. This is the database half of the
-- first-run race guard: two simultaneous "create the first admin" posts cannot
-- both succeed even if both pass the application's own count check.
CREATE UNIQUE INDEX ux_users_bootstrap ON users(is_bootstrap) WHERE is_bootstrap = 1;

-- Per-user view preferences. Their own table rather than columns on `users` so
-- the account model and the view model stay separable.
CREATE TABLE user_prefs (
    user_id             INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    endpoint            TEXT    NOT NULL,
    card_style          TEXT    NOT NULL,
    day_packing         TEXT    NOT NULL,
    hide_not_watching   INTEGER NOT NULL DEFAULT 0,
    network_filter_json TEXT    NOT NULL DEFAULT '[]',
    -- Kept in the same `-anime,-music` string format Trakt accepts as a query
    -- parameter, so the existing settings values and the existing Settings UI
    -- widget carry over verbatim.
    genres              TEXT    NOT NULL DEFAULT '',
    countries           TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE linked_identities (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider         TEXT    NOT NULL CHECK (provider IN ('plex', 'trakt')),
    -- The provider's immutable numeric account id, stored as text. NEVER a
    -- username, slug, or email: Trakt usernames and slugs are user-changeable
    -- and can be released and re-registered by someone else, so keying on one
    -- would let a released name inherit the linked account.
    provider_user_id TEXT    NOT NULL,
    -- Display only, refreshed on each login. Nothing may key off it.
    display_name     TEXT,
    access_token     TEXT,
    refresh_token    TEXT,
    token_expires_at INTEGER,
    -- Held while a token refresh is in flight, so two concurrent requests can't
    -- both spend the same single-use refresh token and invalidate each other.
    refreshing_until INTEGER,
    created_at       INTEGER NOT NULL,
    last_login_at    INTEGER,
    -- What makes "this Plex/Trakt account is already known -> log in as its
    -- owner" a single lookup.
    UNIQUE (provider, provider_user_id)
);
CREATE INDEX ix_linked_identities_user ON linked_identities(user_id);

CREATE TABLE sessions (
    id                  TEXT PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at          INTEGER NOT NULL,
    expires_at          INTEGER NOT NULL,
    absolute_expires_at INTEGER NOT NULL,
    last_seen_at        INTEGER NOT NULL,
    user_agent          TEXT,
    -- Personal data. It exists for the admin's session list and is deleted with
    -- the session row; nothing else retains it.
    ip_address          TEXT
);
CREATE INDEX ix_sessions_user ON sessions(user_id);
CREATE INDEX ix_sessions_expires ON sessions(expires_at);

-- Login/registration throttling state. A table rather than an in-memory window
-- so it survives a restart and the admin UI can show current lockouts.
CREATE TABLE login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_type     TEXT    NOT NULL CHECK (key_type IN
                     ('username', 'ip', 'register_ip', 'invite_ip', 'share_ip')),
    key_value    TEXT    NOT NULL,
    attempted_at INTEGER NOT NULL,
    succeeded    INTEGER NOT NULL DEFAULT 0
);
-- A lockout is computed by counting over this index, never stored — one fewer
-- piece of state that can drift out of sync with the attempts it summarizes.
CREATE INDEX ix_login_attempts_lookup ON login_attempts(key_type, key_value, attempted_at);

-- In-flight OAuth redirects and Plex PIN pairings. An unbound callback is an
-- account-takeover vector: if an attacker can get a logged-in victim's browser
-- to complete a callback carrying the ATTACKER's provider identity, that
-- identity becomes linked to the victim's account. These rows are what bind a
-- callback to the request that started it.
CREATE TABLE auth_handshakes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    state          TEXT    NOT NULL UNIQUE,
    provider       TEXT    NOT NULL CHECK (provider IN ('plex', 'trakt')),
    purpose        TEXT    NOT NULL CHECK (purpose IN ('login', 'link')),
    -- Set only when linking a provider to an account that is already signed in;
    -- the callback must match it against the session making the callback
    -- request. Null for a plain login.
    session_id     TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    invite_token   TEXT,
    pkce_verifier  TEXT,
    plex_pin_id    TEXT,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    -- Stamped in the same transaction that reads the row, so single-use is
    -- enforced by the database rather than by a read-then-write.
    consumed_at    INTEGER
);
CREATE INDEX ix_auth_handshakes_expires ON auth_handshakes(expires_at);

CREATE TABLE invites (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    token                     TEXT    NOT NULL UNIQUE,
    label                     TEXT,
    -- SET NULL rather than CASCADE: deleting the admin who issued an invite must
    -- not silently revoke invites other people are part-way through redeeming.
    created_by                INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at                INTEGER NOT NULL,
    expires_at                INTEGER,
    max_uses                  INTEGER,
    used_count                INTEGER NOT NULL DEFAULT 0,
    revoked                   INTEGER NOT NULL DEFAULT 0,
    -- Defaults on: issuing an invite is already a deliberate act of trust, so
    -- making the invitee then wait in an approval queue is friction with no
    -- added safety. There is deliberately no distrakt counterpart — distrakt
    -- exposes a user's private watch history and is always a separate, manual
    -- grant after the fact.
    grants_calendar_on_accept INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE invite_redemptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invite_id   INTEGER NOT NULL REFERENCES invites(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    redeemed_at INTEGER NOT NULL,
    ip_address  TEXT
);
CREATE INDEX ix_invite_redemptions_invite ON invite_redemptions(invite_id);

-- Usernames, share slugs, and share tokens belonging to deleted accounts.
-- Blocked from reuse by default, otherwise a new user could claim a deleted
-- user's username and silently inherit every link already shared in the wild.
CREATE TABLE retired_identifiers (
    kind       TEXT    NOT NULL CHECK (kind IN ('username', 'slug', 'token')),
    value      TEXT    NOT NULL COLLATE NOCASE,
    retired_at INTEGER NOT NULL,
    PRIMARY KEY (kind, value)
);

-- Instance-scoped values that are neither per-user config nor admin-editable,
-- and so don't belong in settings.json (which stays hand-editable for recovery).
CREATE TABLE app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Migration 2 — the calendar data model. A generic TTL blob cache that both the
# per-show detail lookups (which used to write one file each under data/cache/)
# and the new calendar window cache share, so there is one TTL-blob-cache
# mechanism in the app rather than two; plus the per-user "not watching" marks
# and change-detection fields that replace the shared per-(endpoint,year,month)
# state_*.json files, keyed additionally by user_id.
MIGRATION_2 = """
CREATE TABLE api_cache (
    cache_key   TEXT PRIMARY KEY,
    -- zlib-compressed JSON. Trakt's calendar/detail payloads are highly
    -- repetitive and compress well, so the bytes are stored compressed from the
    -- start rather than retrofitted.
    payload     BLOB    NOT NULL,
    cached_at   INTEGER NOT NULL,
    -- Per-entry lifetime, because one global constant will not do: a calendar
    -- window wants ~10 minutes while a season lookup is good for a day. NULL for
    -- entries whose reader decides freshness itself (the detail lookups, which
    -- pass a TTL to get() at read time) — those are aged out by the size cap only.
    ttl_seconds INTEGER,
    -- Stored per row so the size cap is a single SUM(byte_size) rather than
    -- stat-ing the database file or decompressing every payload to weigh it.
    byte_size   INTEGER NOT NULL
);
-- The size-cap eviction walks oldest-stored first; the TTL sweep filters on the
-- same column, so both are index-served.
CREATE INDEX ix_api_cache_cached_at ON api_cache(cached_at);

-- One row per calendar item a user has marked "not watching", replacing the
-- shared notWatching array in each state_*.json. Rows rather than a document is
-- what makes a single toggle a delta (INSERT/DELETE of one item_id) instead of
-- a whole-array read-modify-write that loses updates across two open tabs.
CREATE TABLE calendar_not_watching (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint   TEXT    NOT NULL,
    year       INTEGER NOT NULL,
    month      INTEGER NOT NULL,
    -- The calendar card's data-id: the show/movie slug when Trakt gave one, else
    -- str(trakt_id) — exactly what the normalizer emits as an item's "id".
    item_id    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, endpoint, year, month, item_id)
);

-- The per-viewer change-detection fields ("N new since YOU last looked"). These
-- are inherently per-user, so they live here and not in the shared window cache.
CREATE TABLE calendar_view_state (
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint           TEXT    NOT NULL,
    year               INTEGER NOT NULL,
    month              INTEGER NOT NULL,
    last_count         INTEGER,
    last_show_ids_json TEXT,
    history_json       TEXT,
    updated_at         INTEGER NOT NULL,
    PRIMARY KEY (user_id, endpoint, year, month)
);
"""

# Migration 3 — public share links. One row per user who has ever opened the
# share panel (created lazily, not for every account up front). The three
# public URL shapes (/s/<token>, /u/<username>, /c/<slug>) each resolve to one
# user's calendar; `enabled_*` controls which shapes actually answer, and
# `preferred_kind` is only which one the UI's copy button reaches for — all
# enabled shapes keep working regardless of which is preferred. The trailing
# columns are the owner's OWN view-option defaults, used when a share request
# doesn't override them with a query param.
MIGRATION_3 = """
CREATE TABLE share_links (
    user_id             INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    token               TEXT    NOT NULL UNIQUE,
    -- NULL until the owner opts into a custom slug. NOCASE so it collides
    -- correctly with both other slugs and usernames, which are also NOCASE.
    custom_slug         TEXT    UNIQUE COLLATE NOCASE,
    preferred_kind      TEXT    NOT NULL DEFAULT 'token'
                            CHECK (preferred_kind IN ('token', 'username', 'slug')),
    -- The token form defaults on so a brand-new share panel already has a
    -- working link; the human-readable forms are opt-in (§1.10).
    enabled_token       INTEGER NOT NULL DEFAULT 1,
    enabled_username    INTEGER NOT NULL DEFAULT 0,
    enabled_slug        INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    token_rotated_at    INTEGER NOT NULL,
    -- Owner defaults for the public view. A query param on the share request
    -- always wins; these are the fallback before the app-wide default.
    endpoint            TEXT,
    card_style          TEXT,
    day_packing         TEXT,
    hide_not_watching   INTEGER NOT NULL DEFAULT 0,
    network_filter_json TEXT    NOT NULL DEFAULT '[]',
    timezone            TEXT
);
"""

# Migration 4 — the distrakt tracker's per-user data model. The tracker used to
# be one shared set of per-month JSON documents plus a single watch_state.json;
# every user now keeps their own independent roster, buckets, and watch history,
# so all five tables are keyed by user_id. The month-level fields (whether a
# month is frozen, when its totals were last refreshed, the movies watched during
# it) live on distrakt_months; the per-show fields on distrakt_shows. The three
# watch-history tables hold the incremental progress cache each user's counts are
# derived from.
MIGRATION_4 = """
-- One row per (user, month) — the month-level state that used to sit at the top
-- of each YYYY-MM.json document.
CREATE TABLE distrakt_months (
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month               TEXT    NOT NULL,          -- 'YYYY-MM'
    -- Once a month is frozen it renders forever from the stored snapshot with no
    -- Trakt calls; the "still live" months are the ones where this is 0.
    closed              INTEGER NOT NULL DEFAULT 0,
    -- When the open month's live totals were last recomputed (whole UTC seconds),
    -- so a routine load can skip the refetch until they age out. NULL until first
    -- stamped.
    totals_refreshed_at INTEGER,
    -- The movies watched during this month, snapshotted at freeze time so the
    -- frozen Discord Post 2 keeps its Movies section offline forever. NULL while
    -- the month is still open (its movies come from the live watch-history cache).
    movies_json         TEXT,
    created_at          INTEGER NOT NULL,
    PRIMARY KEY (user_id, month)
);

-- One row per (user, month, show, season) — the roster records. Keyed by
-- (trakt_id, season) within a user's month, mirroring how a show+season was
-- addressed inside the old document.
CREATE TABLE distrakt_shows (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month           TEXT    NOT NULL,
    trakt_id        INTEGER NOT NULL,
    tmdb            INTEGER,
    slug            TEXT    NOT NULL DEFAULT '',
    media           TEXT    NOT NULL DEFAULT 'show',
    title           TEXT    NOT NULL DEFAULT '',
    season          INTEGER NOT NULL,
    network         TEXT    NOT NULL DEFAULT '',
    abandoned       INTEGER NOT NULL DEFAULT 0,
    -- The rendered inline Discord line, frozen at the moment of abandoning so it
    -- stays stable even after the show would otherwise change buckets. NULL when
    -- not abandoned.
    abandoned_form  TEXT,
    watched         INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    cadence         TEXT,
    premiere        TEXT,
    finale          TEXT,
    bucket          TEXT,
    -- Persisted onto each record at freeze time (and dropped by revision 1's
    -- draft schema): without them a frozen month re-renders every show as
    -- not-yet-aired / not-finished and its bucket rendering silently changes.
    started_airing  INTEGER NOT NULL DEFAULT 0,
    finished_airing INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, month, trakt_id, season)
);

-- One row per user — the singleton fields from watch_state.json: the history
-- cursor and the last_activities beacon set the incremental sync gates on.
CREATE TABLE distrakt_watch_state (
    user_id      INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    -- The ISO date we last synced through, passed straight back to Trakt as the
    -- history start_at cursor — kept as the string Trakt speaks rather than
    -- re-encoded.
    last_synced  TEXT,
    beacons_json TEXT
);

-- One row per (user, show, season) — the completed-episode set per season,
-- stored as a sorted JSON list exactly as the in-memory cache holds it.
CREATE TABLE distrakt_show_progress (
    user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trakt_id              INTEGER NOT NULL,
    season                INTEGER NOT NULL,
    watched_episodes_json TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (user_id, trakt_id, season)
);

-- One row per (user, movie) — a watched movie. watched_at is Trakt's own ISO
-- timestamp stored verbatim (third-party payload, not our clock), so it stays
-- TEXT. title/year travel with it because the open month's Post 2 renders the
-- movie line from this cache, not from the frozen snapshot.
CREATE TABLE distrakt_movie_watches (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trakt_id   INTEGER NOT NULL,
    watched_at TEXT,
    title      TEXT NOT NULL DEFAULT '',
    year       INTEGER,
    PRIMARY KEY (user_id, trakt_id)
);
"""

# Migration 5 — the share link the tracker embeds in its announcement post. Both
# columns are NULL by default and NULL means "follow what the share panel already
# says", so an account that never touches the tracker behaves exactly as before.
MIGRATION_5 = """
-- Which of the three link forms the announcement post carries. NULL follows
-- preferred_kind; a value here is the deliberate override for that one post,
-- which is why it is not just a second write to preferred_kind (the copy button
-- on the calendar and the one on the tracker are different audiences).
ALTER TABLE share_links ADD COLUMN post_link_kind TEXT;

-- Which calendar view the embedded link opens on, as the endpoint key carried in
-- its query string. NULL leaves the link bare, so it opens on whatever the owner
-- defaults already resolve to.
ALTER TABLE share_links ADD COLUMN post_link_endpoint TEXT;
"""

# Migration 6 — the view options the Share panel writes into the link it hands
# out. Deliberately NOT the owner-default columns above: those are the share
# PAGE's fallback and are mirrored from the owner's own calendar preferences, so
# writing them here would change how the owner's private calendar renders as a
# side effect of customizing a link.
MIGRATION_6 = """
-- The query string the generated share link carries, as a JSON object of the
-- public view params. NULL means "hand out a bare link", which lets the page
-- resolve the owner's own defaults — the "use my current display" case.
ALTER TABLE share_links ADD COLUMN link_view_json TEXT;
"""

# Ordered and forward-only. APPEND ONLY: new work adds entries here; an entry
# that has shipped is never edited, because instances in the field have already
# applied it and will never apply it again.
MIGRATIONS: list[tuple[int, str | Callable[[sqlite3.Connection], None]]] = [
    (1, MIGRATION_1),
    (2, MIGRATION_2),
    (3, MIGRATION_3),
    (4, MIGRATION_4),
    (5, MIGRATION_5),
    (6, MIGRATION_6),
]


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")


def _read_version(conn: sqlite3.Connection) -> int:
    _ensure_version_table(conn)
    return int(conn.execute("SELECT version FROM schema_version").fetchone()[0])


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement SQL string one statement at a time.

    Deliberately NOT Connection.executescript, which issues an implicit COMMIT
    before it runs and would silently break the migration out of the transaction
    that is supposed to contain it. sqlite3.complete_statement handles the
    splitting so a semicolon inside a string literal can't cut a statement in
    half.
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            conn.execute(buffer)
            buffer = ""
    if buffer.strip():
        conn.execute(buffer)


def migrate_sync(conn: sqlite3.Connection) -> int:
    """Apply every pending migration, each in its own transaction, and return the
    resulting schema version. Idempotent: a second call is a no-op.

    SYNCHRONOUS — async callers use migrate().
    """
    _ensure_version_table(conn)
    current = _read_version(conn)
    for version, step in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if callable(step):
                step(conn)
            else:
                _run_script(conn, step)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        logger.info("Applied database migration %s", version)
        current = version
    return current


async def migrate() -> int:
    return await run(migrate_sync)


async def schema_version() -> int:
    return await run(_read_version)


async def init() -> int:
    """Open the database and bring the schema up to date. Called once at startup,
    before anything else touches it."""
    version = await migrate()
    logger.info("Database ready at %s (schema v%s)", db_path(), version)
    return version


# ---------------------------------------------------------------------------
# app_meta
# ---------------------------------------------------------------------------

async def get_meta(key: str, default: str | None = None) -> str | None:
    row = await fetch_one("SELECT value FROM app_meta WHERE key = ?", (key,))
    return default if row is None else row["value"]


async def set_meta(key: str, value: str) -> None:
    await execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
