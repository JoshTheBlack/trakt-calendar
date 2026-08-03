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

import json
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

from . import clock
from .config import DATA_DIR, ensure_data_dir

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
    ensure_data_dir()
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
    -- working link; the human-readable forms are opt-in.
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

# Migration 7 — widen login_attempts.key_type to admit 'handshake_ip', the
# volume limiter over the provider sign-in start routes (those mint a handshake
# row and, for Plex, call out to plex.tv, all before anyone has authenticated).
# SQLite cannot alter a CHECK constraint in place, so the table is rebuilt. The
# rows are ephemeral rate-limit state and are copied across anyway, since a
# rebuild that silently forgot an in-progress lockout would be a way to clear one.
MIGRATION_7 = """
CREATE TABLE login_attempts_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_type     TEXT    NOT NULL CHECK (key_type IN
                     ('username', 'ip', 'register_ip', 'invite_ip', 'share_ip',
                      'handshake_ip')),
    key_value    TEXT    NOT NULL,
    attempted_at INTEGER NOT NULL,
    succeeded    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO login_attempts_new (id, key_type, key_value, attempted_at, succeeded)
    SELECT id, key_type, key_value, attempted_at, succeeded FROM login_attempts;
DROP TABLE login_attempts;
ALTER TABLE login_attempts_new RENAME TO login_attempts;
CREATE INDEX ix_login_attempts_lookup ON login_attempts(key_type, key_value, attempted_at);
"""

# Migration 8 — two corrections to how share links behave.
#
# 1. EVERY link form answers. The Share panel offers a single dropdown that picks
#    which URL it hands you, and that is presentation only: a link already given
#    to somebody must not stop working because its owner later looked at a
#    different one. Existing rows are opened up to match, since they were created
#    under the old rule where only the token form started enabled.
# 2. `retired_identifiers` gains the account the identifier came from, so an
#    owner can reclaim a slug they themselves retired while it stays blocked for
#    everybody else. Nullable: rows written before this (and by account deletion,
#    where there is deliberately no owner left) carry no user.
MIGRATION_8 = """
UPDATE share_links SET enabled_token = 1, enabled_username = 1, enabled_slug = 1;
ALTER TABLE retired_identifiers ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
"""

# Migration 9 — the network->emoji map becomes PER USER.
#
# It was app-wide in settings.json, which made every tracker user edit the same
# map: importing a roster on one account registered dozens of networks into the
# operator's, and one person's emoji choices rendered in everybody's Discord
# posts. The tracker is per-user in every other respect (roster, watch state,
# view preferences), and this is the last piece of it that was not.
#
# Nothing seeds a new account's map — it starts empty and fills in from that
# user's own roster. But an instance upgrading from the app-wide version has a
# real curated map in settings.json, and it belongs to the operator who built it,
# so it is MOVED (once, here) onto the bootstrap admin rather than discarded.
# After this the settings.json fields are gone and the per-user rows are the only
# copy; the tracker's Backup export is what carries a map anywhere else.
MIGRATION_9_SQL = """
CREATE TABLE distrakt_prefs (
    user_id               INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    -- JSON object of network name -> Discord emoji token.
    network_emojis_json   TEXT    NOT NULL DEFAULT '{}',
    -- The fallback for a network with no entry of its own.
    default_network_emoji TEXT    NOT NULL DEFAULT ':tv:',
    updated_at            INTEGER NOT NULL
);
"""


def MIGRATION_9(conn: sqlite3.Connection) -> None:
    _run_script(conn, MIGRATION_9_SQL)
    # Read settings.json directly rather than through app.config: those fields are
    # being deleted from the Settings model in this same change, so the model can
    # no longer describe the file this is reading.
    settings_file = DATA_DIR / "settings.json"
    try:
        raw = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    emojis = raw.get("network_emojis")
    default_emoji = (raw.get("default_network_emoji") or ":tv:").strip() or ":tv:"
    if not isinstance(emojis, dict) or not emojis:
        return
    owner = conn.execute(
        "SELECT id FROM users WHERE is_bootstrap = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if owner is None:
        # A fresh install: there is no operator to inherit it and no roster to
        # apply it to. The map goes nowhere, which is the intended new behavior.
        return
    conn.execute(
        "INSERT INTO distrakt_prefs (user_id, network_emojis_json, default_network_emoji, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
        (int(owner["id"]), json.dumps({str(k): str(v) for k, v in emojis.items()}),
         default_emoji, int(time.time())),
    )
    logger.info(
        "Moved the app-wide network emoji map (%d entries) onto the bootstrap "
        "administrator; it is per-user from now on.", len(emojis),
    )

# Migration 10 — "not watching" becomes a property of the SHOW, not of one cell
# in the (endpoint, year, month) grid.
#
# calendar_not_watching keyed a mark by the view it was made in, so marking a
# series premiere hid it on Series Premieres and nowhere else: the same show's
# episodes still filled All Episodes, and next month's rows started clean. That
# is not what the toggle says. It says "I'm not watching this", which is a fact
# about the show and has no month in it.
#
# The item_id was ALREADY the show's slug on every endpoint (the normalizer
# reads the show object, not the episode), so folding the grid away is a pure
# widening — every existing mark survives, keeping its earliest created_at, and
# now applies everywhere that show appears.
MIGRATION_10 = """
CREATE TABLE not_watching_shows (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The calendar card's data-id: the show/movie slug when Trakt gave one, else
    -- str(trakt_id) — exactly what the normalizer emits as an item's "id".
    item_id    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, item_id)
);
INSERT INTO not_watching_shows (user_id, item_id, created_at)
    SELECT user_id, item_id, MIN(created_at)
      FROM calendar_not_watching
     GROUP BY user_id, item_id;
DROP TABLE calendar_not_watching;
"""

# Migration 11 — consolidate configuration out of settings.json and into the DB.
#
# settings.json historically held everything: the Trakt/Sonarr/Radarr/Seerr/TMDB
# credentials, every non-secret global, and the two file-only recovery settings.
# This splits that one file into three homes so each class has a single storage
# location and one read/write boundary. The credentials move to app_secrets (a
# store a later change can encrypt at rest); the non-secret globals move to
# app_settings; only cookie_secure + allow_open_registration stay in the file,
# deliberately file-only so an operator can edit them to recover from a lockout
# with no app running and no sqlite tooling.
#
# This migration copies the values INTO the DB but does NOT rewrite the file:
# load_settings() reduces settings.json to the two recovery fields on the first
# boot after the copy is committed. Doing the file shrink there rather than here is
# what keeps the move crash-safe — a crash can never leave the file already
# shrunk while the table inserts were rolled back, so the file stays the intact
# source of truth to retry from.
MIGRATION_11_SQL = """
CREATE TABLE app_secrets (
    -- One row per config.SECRET_FIELDS name. `value` is plaintext until at-rest
    -- encryption is enabled, after which it is `enc:v1:`-prefixed ciphertext; the
    -- two coexist the same way the linked_identities tokens do. An unset secret
    -- has no row rather than an empty one.
    name  TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE app_settings (
    -- A single row, name='app', whose value is the JSON document of every
    -- non-secret global. One row rather than key-per-field keeps load/save a
    -- straight move of the JSON that used to sit in settings.json and leaves the
    -- Settings dataclass API untouched. Never sealed — these carry no secret.
    name  TEXT PRIMARY KEY,
    value TEXT
);
"""


def MIGRATION_11(conn: sqlite3.Connection) -> None:
    _run_script(conn, MIGRATION_11_SQL)
    # Imported here, not at module top, to avoid a cycle: config imports db lazily
    # for exactly this store, so db must not import config's Settings model eagerly.
    from .config import SECRET_FIELDS, Settings, global_field_names
    settings_file = DATA_DIR / "settings.json"
    try:
        raw = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Fresh install, or an unreadable file: nothing to copy. The empty tables
        # are enough — load_settings() seeds from the environment and defaults, and
        # the first save populates them.
        return
    if not isinstance(raw, dict):
        return
    settings = Settings.from_dict(raw)
    globals_doc = {name: getattr(settings, name) for name in sorted(global_field_names())}
    conn.execute(
        "INSERT INTO app_settings (name, value) VALUES ('app', ?) "
        "ON CONFLICT(name) DO NOTHING",
        (json.dumps(globals_doc),),
    )
    for name in sorted(SECRET_FIELDS):
        value = getattr(settings, name, "")
        if value:
            conn.execute(
                "INSERT INTO app_secrets (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO NOTHING",
                (name, value),
            )
    logger.info(
        "Consolidated configuration from settings.json into the database; "
        "settings.json is reduced to its recovery fields on next load."
    )


# Migration 12 — per-user certification filter, alongside the existing
# genres/countries columns. Two columns, not one: shows and movies use
# different rating vocabularies (TV Parental Guidelines vs. the MPA film
# ratings), so a show's rating and a movie's rating are never comparable
# values and must never share a spec string.
MIGRATION_12 = """
ALTER TABLE user_prefs ADD COLUMN show_certifications TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN movie_certifications TEXT NOT NULL DEFAULT '';
"""

# Migration 13 — the tier ranker: its own per-feature approval, and the tables
# behind boards, tiers and the titles in them, plus the shared poster-URL
# registry those tiles are drawn from.
#
# ranker_approved is granted to ADMINS ONLY as part of this migration. A plain
# DEFAULT 0 would lock the operator out of the feature the moment they deployed
# it, with no account able to reach the screen that hands out the grant;
# granting it to everyone would hand a brand-new feature to every account on the
# instance without anyone reviewing that. Admins can pass it on from the admin
# screen.
#
# invites.grants_ranker_on_accept sits with grants_calendar_on_accept rather
# than with the deliberately-absent distrakt counterpart: the ranker exposes
# nothing about anyone's watch history — its optional import is separately gated
# on distrakt_approved — so an invite can hand it over the way it already hands
# over the calendar. The column DEFAULTS TO 0 while the UI checkbox ships
# checked: invites already outstanding must not silently start granting a
# feature their issuer never chose, but a newly issued one behaves like the
# calendar's, where issuing an invite is already a deliberate act of trust.
MIGRATION_13 = """
ALTER TABLE users ADD COLUMN ranker_approved INTEGER NOT NULL DEFAULT 0;
UPDATE users SET ranker_approved = 1 WHERE is_admin = 1;
ALTER TABLE invites ADD COLUMN grants_ranker_on_accept INTEGER NOT NULL DEFAULT 0;

-- Every poster URL the app has ever seen, so a lookup already paid for is never
-- paid for twice. GLOBAL rather than per user: most accounts on an instance
-- watch overlapping titles, so one shared record serves everyone and the table
-- does not grow with the user count. Its own table specifically so the
-- size-capped LRU on api_cache can never evict it.
CREATE TABLE show_posters (
    -- TMDB ids are namespaced per media type: movie 550 and TV 550 are
    -- different titles, so the identity is the PAIR and never the id alone.
    media          TEXT    NOT NULL,
    tmdb           INTEGER NOT NULL,
    -- Which provider handed us this URL. An open set of names rather than a
    -- CHECK constraint, so adding a provider is not a migration.
    source         TEXT    NOT NULL,
    url            TEXT    NOT NULL,
    first_seen_at  INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    last_ok_at     INTEGER,
    last_failed_at INTEGER,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (media, tmdb, source)
);
-- Retention is swept by age, so the sweep is index-served.
CREATE INDEX ix_show_posters_seen ON show_posters(last_seen_at);

CREATE TABLE tier_boards (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Client-generated and the ONLY identifier a request may name a board by.
    -- An autoincrement id is guessable, so addressing by (user_id, uid) is what
    -- makes a cross-tenant reference impossible to even express; it also
    -- survives a restore into a database whose rowids came out different.
    uid         TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    year        INTEGER,
    media_scope TEXT    NOT NULL DEFAULT 'mixed',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    -- Bumped on every accepted layout write; the client echoes it back so a
    -- second tab's stale save is refused rather than silently clobbering the
    -- arrangement the first tab made.
    version     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, uid)
);

CREATE TABLE tier_categories (
    id            INTEGER PRIMARY KEY,
    board_id      INTEGER NOT NULL REFERENCES tier_boards(id) ON DELETE CASCADE,
    uid           TEXT    NOT NULL,
    label         TEXT    NOT NULL DEFAULT '',
    -- Higher competes above lower when tiers are consolidated into one ranking.
    rank_priority INTEGER NOT NULL DEFAULT 0,
    -- An isolated tier keeps its own 1..X order and stays out of the
    -- consolidated ranking unless it is exported on its own.
    is_isolated   INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    colour        TEXT,
    created_at    INTEGER NOT NULL,
    UNIQUE (board_id, uid)
);

CREATE TABLE tier_items (
    id            INTEGER PRIMARY KEY,
    board_id      INTEGER NOT NULL REFERENCES tier_boards(id) ON DELETE CASCADE,
    -- NULL means the board's unranked pool. SET NULL rather than CASCADE so
    -- deleting a tier returns its titles to the pool instead of destroying
    -- curated work over a mis-click.
    category_id   INTEGER REFERENCES tier_categories(id) ON DELETE SET NULL,
    media         TEXT    NOT NULL DEFAULT 'show',
    -- The first shared id the identity waterfall found: tmdb, then tvdb, then
    -- imdb, then mal. A title with no tmdb can still be ranked; it just has no
    -- artwork, because ranking must not be gated on a poster existing.
    match_source  TEXT    NOT NULL,
    match_id      TEXT    NOT NULL,
    -- Artwork key specifically, because TMDB is the artwork source and its id is
    -- what indexes the image. NULL renders the placeholder tile.
    tmdb          INTEGER,
    -- The whole id map as it arrived, so an id this feature does not use today
    -- is still there for a future match.
    ids_json      TEXT    NOT NULL DEFAULT '{}',
    -- Self-contained by design: a manually searched title has no other row in
    -- this database to join against. Refreshed opportunistically, never
    -- authoritative.
    title         TEXT    NOT NULL DEFAULT '',
    year          INTEGER,
    network       TEXT    NOT NULL DEFAULT '',
    season_count  INTEGER,
    episode_count INTEGER,
    runtime       INTEGER,
    user_rating   INTEGER,
    added_from    TEXT    NOT NULL DEFAULT 'manual',
    rank_in_category INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    -- One entry per title per board. Board-scoped rather than user-scoped on
    -- purpose: the same film appearing in both "Top 2026" and "All-Time" is a
    -- normal thing to want.
    UNIQUE (board_id, media, match_source, match_id)
);
CREATE INDEX ix_tier_items_category ON tier_items(category_id, rank_in_category);
CREATE INDEX ix_tier_items_pool ON tier_items(board_id) WHERE category_id IS NULL;
"""

# Migration 14 — widen login_attempts.key_type to admit 'ranker_search' and
# 'ranker_export', so both of the ranker's volume throttles live in the same
# table every other feature's rate limiter uses instead of an in-process
# counter. SQLite cannot alter a CHECK constraint in place, so the table is
# rebuilt exactly as migration 7 rebuilt it for 'handshake_ip'; the rows are
# copied across for the same reason that migration's comment gives — a rebuild
# that silently forgot an in-progress lockout would be a way to clear one.
# 'ranker_export' has no caller yet; the export cooldown lands in a later
# change and needs no schema work of its own once this ships.
MIGRATION_14 = """
CREATE TABLE login_attempts_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_type     TEXT    NOT NULL CHECK (key_type IN
                     ('username', 'ip', 'register_ip', 'invite_ip', 'share_ip',
                      'handshake_ip', 'ranker_search', 'ranker_export')),
    key_value    TEXT    NOT NULL,
    attempted_at INTEGER NOT NULL,
    succeeded    INTEGER NOT NULL DEFAULT 0
);
INSERT INTO login_attempts_new (id, key_type, key_value, attempted_at, succeeded)
    SELECT id, key_type, key_value, attempted_at, succeeded FROM login_attempts;
DROP TABLE login_attempts;
ALTER TABLE login_attempts_new RENAME TO login_attempts;
CREATE INDEX ix_login_attempts_lookup ON login_attempts(key_type, key_value, attempted_at);
"""

# Migration 15 — a chosen display name, separate from the username.
#
# The username stays what it has always been: the login identifier, NOCASE and
# UNIQUE, and what /u/<name> share links are built from. It could never carry a
# capital letter or a space without either breaking that case-insensitive
# identity or changing what a share URL means.
#
# So this is a SEPARATE column rather than a relaxation of that one, and it is
# deliberately the opposite in all three respects: case-SENSITIVE (no NOCASE, so
# `Josh Black` is stored and shown exactly as typed), NOT unique (two people may
# call themselves the same thing — nothing keys off this), and NULLABLE, where
# NULL means "no name chosen, fall back to the username". Nothing may be looked
# up by it; it is display only, the same rule linked_identities.display_name
# already carries.
MIGRATION_15 = """
ALTER TABLE users ADD COLUMN display_name TEXT;
"""

# Migration 16 — where a roster row came from.
#
# Removing a row from the tracker also marks the show not-watching on the
# calendar, because on a preview month the roster re-imports the month's
# premieres on every load and that mark is the only thing the import skips —
# without it the row came straight back and the ✕ looked broken. But that mark
# hides the show on the calendar too, which is right for a row the calendar put
# there and wrong for one the user added by hand: removing a mistaken manual add
# must not quietly hide a show they still want to see.
#
# So each row records who added it: 'calendar' (premiere import), 'history'
# (in-progress from watch history), 'manual' (added on the tracker), or '' for a
# row written before this column existed. Only 'calendar' rows write the mark;
# see app/distrakt/routes.py's api_distrakt_remove for how the legacy '' case is resolved.
MIGRATION_16 = """
ALTER TABLE distrakt_shows ADD COLUMN source TEXT NOT NULL DEFAULT '';
"""

# Migration 17 — drop the cached episode progress so it is re-baselined WITH the
# dates each episode was watched.
#
# distrakt_show_progress.watched_episodes_json held a bare list of episode
# numbers, which cannot answer the question the Completed bucket actually asks:
# was this season finished THIS month? Without it, a season finished in July sat
# in August's Completed for good. The column now holds {episode: watched_at}.
#
# The rows are a CACHE of Trakt's own answer, so the honest fix is to throw them
# away rather than migrate undated data into a dated shape: watch_history's
# sync_and_baseline re-baselines any roster show it finds no progress for, from
# /shows/{id}/progress/watched, which carries last_watched_at per episode. The
# cost is one slower tracker load per user, once, after which past months read
# correctly too. Nothing here is user-entered — there is nothing to lose.
MIGRATION_17 = """
DELETE FROM distrakt_show_progress;
"""

# Migration 18 — the tracker's rows stop being keyed on one service's id.
#
# All three tracker tables were keyed on `trakt_id`, which makes the tracker
# permanently single-source: the same season arriving from anywhere else would be
# a second row forever, with no way to tell it was the same season. They are now
# keyed on (media, match_source, match_id) — the id NAMESPACE the identity
# waterfall landed in, plus the id in it (see app/providers/base.py's
# MATCH_SOURCES). tmdb wins whenever it is present, which on real data is
# essentially always, so the same title from two services is ONE row with no
# merge step. The service's own id stays on the row as a plain attribute, because
# it is still what you need to CALL that service — it is just no longer what
# identifies the season.
#
# `distrakt_shows.source` becomes `added_by`. It always meant WHO PUT THE ROW ON
# THE ROSTER ('calendar' | 'history' | 'manual' | ''), and "source" is the word
# for WHICH SERVICE everywhere else; leaving the collision in place would have
# been a trap for exactly the change that adds a second one.
#
# THE KEY COLUMNS ARE NOT NULL. The nullable `tmdb` they replace was vestigial —
# residue from an era when tmdb was pruned during ingest — and carrying that
# nullability into the key would recreate the ambiguity the waterfall exists to
# remove.
#
# WHAT HAPPENS TO EXISTING ROWS, table by table, and why they differ:
#   distrakt_shows is USER DATA — abandons, manual adds, hand-filled past months
#     — so every row is carried across, with its key resolved from the ids it
#     already holds. A row that resolves to NO id cannot be addressed by this
#     app afterwards, and rather than invent a key or silently drop somebody's
#     roster this REFUSES to migrate and says how many rows it found.
#   distrakt_show_progress is a CACHE of the service's own answer, so its rows
#     are resolved through the roster's ids where possible and dropped where not
#     — watch_history re-baselines any roster season it finds no progress for.
#   distrakt_movie_watches is a cache too, and the only one that cannot be
#     resolved at all: nothing in this database has ever recorded a shared id for
#     a film. It is emptied and `last_synced` cleared so the next sync re-seeds
#     films from the start of the current month, exactly as a forced refresh
#     does. Migration 17 discarded the same table's sibling for the same reason:
#     re-fetching a cache is honest where migrating it into a shape it never had
#     is guesswork. Films in months that are already FROZEN are unaffected —
#     those live in distrakt_months.movies_json, which this does not touch.
MIGRATION_18_SQL = """
CREATE TABLE distrakt_show_progress_new (
    user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    media                 TEXT    NOT NULL,
    match_source          TEXT    NOT NULL,
    match_id              TEXT    NOT NULL,
    season                INTEGER NOT NULL,
    watched_episodes_json TEXT    NOT NULL DEFAULT '[]',
    -- The service's own id, so the progress record can be refetched. Only the
    -- ids a sync actually calls with live on a cache row; the shared ids the
    -- waterfall did not pick are on the ROSTER row, which is where a later
    -- resolution pass would read them.
    trakt_id              INTEGER,
    simkl_id              INTEGER,
    PRIMARY KEY (user_id, media, match_source, match_id, season)
);
INSERT INTO distrakt_show_progress_new
       (user_id, media, match_source, match_id, season, watched_episodes_json, trakt_id)
    SELECT p.user_id, 'show', 'tmdb',
           CAST((SELECT s.tmdb FROM distrakt_shows s
                  WHERE s.trakt_id = p.trakt_id AND s.tmdb IS NOT NULL AND s.tmdb != 0
                  LIMIT 1) AS TEXT),
           p.season, p.watched_episodes_json, p.trakt_id
      FROM distrakt_show_progress p
     WHERE EXISTS (SELECT 1 FROM distrakt_shows s
                    WHERE s.trakt_id = p.trakt_id AND s.tmdb IS NOT NULL AND s.tmdb != 0);
DROP TABLE distrakt_show_progress;
ALTER TABLE distrakt_show_progress_new RENAME TO distrakt_show_progress;

CREATE TABLE distrakt_shows_new (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month           TEXT    NOT NULL,
    -- The identity: which kind of title, which shared id namespace, and the id.
    media           TEXT    NOT NULL,
    match_source    TEXT    NOT NULL,
    match_id        TEXT    NOT NULL,
    season          INTEGER NOT NULL,
    -- Every id the row is known by, so a later pass can upgrade a season first
    -- seen with only a weak id, and so a second service can be called about a
    -- season this one put on the roster. None of these identifies the row.
    trakt_id        INTEGER,
    simkl_id        INTEGER,
    tmdb            INTEGER,
    tvdb            INTEGER,
    imdb            TEXT,
    mal             INTEGER,
    -- Nullable like the ids beside it, and for the same reason: the row records
    -- what the source actually named, and NULL says "it named none" where '' would
    -- claim the source gave an empty slug. Readers collect the non-empty ones.
    slug            TEXT,
    title           TEXT    NOT NULL DEFAULT '',
    network         TEXT    NOT NULL DEFAULT '',
    abandoned       INTEGER NOT NULL DEFAULT 0,
    abandoned_form  TEXT,
    watched         INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    cadence         TEXT,
    premiere        TEXT,
    finale          TEXT,
    bucket          TEXT,
    started_airing  INTEGER NOT NULL DEFAULT 0,
    finished_airing INTEGER NOT NULL DEFAULT 0,
    -- Was `source`. Who put this row on the roster, which decides whether taking
    -- it off says anything to the calendar; see app/distrakt/routes.py's remove route.
    added_by        TEXT    NOT NULL DEFAULT '',
    UNIQUE (user_id, month, media, match_source, match_id, season)
);
INSERT INTO distrakt_shows_new
       (user_id, month, media, match_source, match_id, season, trakt_id, tmdb, slug,
        title, network, abandoned, abandoned_form, watched, total, cadence,
        premiere, finale, bucket, started_airing, finished_airing, added_by)
    SELECT user_id, month, COALESCE(NULLIF(media, ''), 'show'), 'tmdb', CAST(tmdb AS TEXT),
           season, trakt_id, tmdb, slug, title, network, abandoned, abandoned_form,
           watched, total, cadence, premiere, finale, bucket, started_airing,
           finished_airing, source
      FROM distrakt_shows;
DROP TABLE distrakt_shows;
ALTER TABLE distrakt_shows_new RENAME TO distrakt_shows;

CREATE TABLE distrakt_movie_watches_new (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    media        TEXT    NOT NULL,
    match_source TEXT    NOT NULL,
    match_id     TEXT    NOT NULL,
    watched_at   TEXT,
    title        TEXT    NOT NULL DEFAULT '',
    year         INTEGER,
    trakt_id     INTEGER,
    simkl_id     INTEGER,
    PRIMARY KEY (user_id, media, match_source, match_id)
);
DROP TABLE distrakt_movie_watches;
ALTER TABLE distrakt_movie_watches_new RENAME TO distrakt_movie_watches;
UPDATE distrakt_watch_state SET last_synced = NULL;
"""


def MIGRATION_18(conn: sqlite3.Connection) -> None:
    # Counted BEFORE anything is rebuilt, because the answer decides whether the
    # rebuild may happen at all. A roster row with no shared id is unaddressable
    # after this change: every read path keys on the triple, so keeping it would
    # leave a row nothing can reach and the export would carry it forward for
    # ever. Refusing is the only option that neither destroys it nor pretends.
    unkeyable = conn.execute(
        "SELECT COUNT(*) FROM distrakt_shows WHERE tmdb IS NULL OR tmdb = 0"
    ).fetchone()[0]
    if unkeyable:
        raise RuntimeError(
            f"{unkeyable} tracker roster row(s) carry none of the shared ids the "
            "tracker is now keyed on (tmdb/tvdb/imdb/mal), so they cannot be "
            "migrated without inventing an identity for them. Nothing has been "
            "changed. Fill in the missing ids, or delete those rows, and start "
            "the app again."
        )
    dropped_progress = conn.execute(
        "SELECT COUNT(*) FROM distrakt_show_progress p WHERE NOT EXISTS ("
        "SELECT 1 FROM distrakt_shows s WHERE s.trakt_id = p.trakt_id "
        "AND s.tmdb IS NOT NULL AND s.tmdb != 0)"
    ).fetchone()[0]
    dropped_movies = conn.execute("SELECT COUNT(*) FROM distrakt_movie_watches").fetchone()[0]
    _run_script(conn, MIGRATION_18_SQL)
    if dropped_progress or dropped_movies:
        logger.info(
            "Re-keyed the tracker onto shared title ids; discarded %d cached "
            "progress row(s) whose show is on no roster month and %d cached film "
            "watch(es). Both re-fetch on the next tracker load; frozen months "
            "keep the films they snapshotted.",
            dropped_progress, dropped_movies,
        )


# Migration 19 — month-facts and user-facts stop sharing one table.
#
# distrakt_shows held BOTH kinds of statement in one row: "this season premiered
# in July" (a fact about July, true for ever) and "you are four episodes into it"
# (a fact about the viewer, true only right now). Because they shared a row, the
# only way to keep the second up to date was to copy the row onto every month the
# season was live in, and the only way to ask what the viewer was behind on was to
# union every month and dedupe. A title's presence on today's list then depended
# on which month had last copied it forward.
#
# They are now two tables:
#   distrakt_month_records  what a month announced or settled — its premieres and
#     its completed/abandoned verdicts. `kind` is IN the primary key, so a season
#     that premiered and was settled in the SAME month holds two rows on it. That
#     is correct rather than a duplicate: the month both announced it and reached
#     a verdict on it, and dropping either statement loses one of them.
#   distrakt_user_seasons   what the viewer is in the middle of, belonging to no
#     month at all. `kind` here is keepup or catchup — two states of ONE row, so a
#     season that finishes airing is an UPDATE and can never be on the list twice.
#
# A PREMIERE RECORD CARRIES NO VIEWER PROGRESS. `watched` is written 0 on one and
# means nothing there: it is a snapshot of the show as it premiered, which is
# exactly what makes it safe to keep for ever and safe to correct when a later
# season lookup reports a different episode total.
#
# HOW EVERY EXISTING ROW IS CLASSIFIED, and why a row can produce two records:
#   - it premiered in the month it sits on (its "M/D" premiere date's month equals
#     the month key's) -> a premiere record, series when season <= 1 and season
#     otherwise. Independent of everything below, which is where the two-record
#     case comes from.
#   - the viewer gave up on it (the `abandoned` flag, or the frozen bucket) -> an
#     abandoned record on that month. The flag and the bucket are both read
#     because either alone is enough: the flag is what the viewer pressed and the
#     bucket is what the month wrote down when it froze.
#   - the month recorded it finished -> a completed record on that month.
#   - anything else -> ONE user record for the season, from its latest month's row
#     (the most recent copy carries the most recent counts), catchup when the
#     season has finished airing or drops all at once, keepup otherwise.
# A season settled on ANY month is off the user list entirely, because giving up
# on a season in March is a statement about the season and an older in-progress
# copy of it must not resurrect it.
#
# THE COPIES ARE WHAT GOES. A season carried onto three months had three rows and
# now has one user record or one verdict; that collapse is the point of the change
# and is reported rather than silent.
#
# A MONTH THAT HAD NOT YET FROZEN CANNOT BE CLASSIFIED BY PREMIERE DATE. In the old
# schema the live fields — premiere, finale, cadence, started_airing,
# finished_airing — were written back onto a row only when its month FROZE; until
# then they were recomputed from the provider on every view and never stored. So on
# any month with `closed = 0` those columns are empty, and the premiere pass above,
# which asks whether the row's premiere date falls in the row's month, can prove
# nothing about them. Those seasons are carried across as user records — which is
# where an unfinished season belongs — and the month keeps no premiere records.
#
#   THIS IS NOT REPAIRED HERE, DELIBERATELY. The date is not in this database, so
#   restoring it would mean a provider call from inside a migration, and `added_by`
#   is '' on rows written before that column existed, so provenance cannot say
#   which of them the calendar put there either. Inventing a premiere date would
#   put a title in a month's announcement on a guess. Classifying only what the row
#   can prove is the honest outcome; the migration LOGS the affected months so an
#   operator can see why an open month's announcement is empty, and re-importing
#   that month's premieres from the calendar is what fills it back in.
#
#   THE KEEPUP/CATCHUP SPLIT HEALS ITSELF, and for the same reason it is skewed to
#   begin with. It is decided here from `finished_airing` and `cadence`, which are
#   empty on those same rows, so every one of them starts as keepup. A user record
#   is refreshed from a season lookup that reports the episode count and the last
#   episode's air date, so the split is re-derived from those dates rather than
#   read back from what this migration wrote. The initial skew is stale, not wrong.
#
# distrakt_prompt_dismissals records the viewer declining to add a season the
# tracker has never heard of. Without somewhere to record the refusal it would be
# re-derived from the same watch history on the very next load and the ✗ would do
# nothing. It is keyed by SEASON, not by episode, or every further episode of the
# same season would ask again.
MIGRATION_19_SQL = """
CREATE TABLE distrakt_month_records (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month           TEXT    NOT NULL,          -- 'YYYY-MM'
    -- 'series_premiere' | 'season_premiere' | 'completed' | 'abandoned'.
    kind            TEXT    NOT NULL,
    media           TEXT    NOT NULL,
    match_source    TEXT    NOT NULL,
    match_id        TEXT    NOT NULL,
    season          INTEGER NOT NULL,
    trakt_id        INTEGER,
    simkl_id        INTEGER,
    tmdb            INTEGER,
    tvdb            INTEGER,
    imdb            TEXT,
    mal             INTEGER,
    slug            TEXT,
    title           TEXT    NOT NULL DEFAULT '',
    network         TEXT    NOT NULL DEFAULT '',
    -- Meaningless on a premiere record and written 0 there; see above.
    watched         INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    cadence         TEXT,
    premiere        TEXT,
    finale          TEXT,
    started_airing  INTEGER NOT NULL DEFAULT 0,
    finished_airing INTEGER NOT NULL DEFAULT 0,
    -- The rendered inline Discord line, frozen at the moment of giving up so it
    -- stays stable afterwards. NULL on every kind but 'abandoned'.
    abandoned_form  TEXT,
    added_by        TEXT    NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (user_id, month, kind, media, match_source, match_id, season)
);

CREATE TABLE distrakt_user_seasons (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    media           TEXT    NOT NULL,
    match_source    TEXT    NOT NULL,
    match_id        TEXT    NOT NULL,
    season          INTEGER NOT NULL,
    trakt_id        INTEGER,
    simkl_id        INTEGER,
    tmdb            INTEGER,
    tvdb            INTEGER,
    imdb            TEXT,
    mal             INTEGER,
    slug            TEXT,
    title           TEXT    NOT NULL DEFAULT '',
    network         TEXT    NOT NULL DEFAULT '',
    -- 'keepup' | 'catchup'. Two states of one row, never two rows.
    kind            TEXT    NOT NULL,
    watched         INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    cadence         TEXT,
    premiere        TEXT,
    finale          TEXT,
    started_airing  INTEGER NOT NULL DEFAULT 0,
    finished_airing INTEGER NOT NULL DEFAULT 0,
    -- Set when a season the viewer had finished turns out to have grown. The
    -- month record that proved it was ever finished is deleted as part of that
    -- move, so this flag is the only thing left that remembers; it is cleared by
    -- the viewer acknowledging the marker and by nothing else.
    came_back       INTEGER NOT NULL DEFAULT 0,
    added_by        TEXT    NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (user_id, media, match_source, match_id, season)
);

CREATE TABLE distrakt_prompt_dismissals (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    media        TEXT    NOT NULL,
    match_source TEXT    NOT NULL,
    match_id     TEXT    NOT NULL,
    season       INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, media, match_source, match_id, season)
);

-- The premieres. Deliberately not exclusive with the verdict pass below: a
-- season that premiered and was settled in the same month gets both records.
INSERT INTO distrakt_month_records
       (user_id, month, kind, media, match_source, match_id, season,
        trakt_id, simkl_id, tmdb, tvdb, imdb, mal, slug, title, network,
        watched, total, cadence, premiere, finale, started_airing, finished_airing,
        abandoned_form, added_by, created_at)
    SELECT user_id, month,
           CASE WHEN season <= 1 THEN 'series_premiere' ELSE 'season_premiere' END,
           media, match_source, match_id, season,
           trakt_id, simkl_id, tmdb, tvdb, imdb, mal, slug, title, network,
           0, total, cadence, premiere, finale, started_airing, finished_airing,
           NULL, added_by, CAST(strftime('%s', 'now') AS INTEGER)
      FROM distrakt_shows
     WHERE premiere IS NOT NULL AND instr(premiere, '/') > 1
       AND CAST(substr(premiere, 1, instr(premiere, '/') - 1) AS INTEGER)
           = CAST(substr(month, 6, 2) AS INTEGER);

-- The verdicts a month reached.
INSERT INTO distrakt_month_records
       (user_id, month, kind, media, match_source, match_id, season,
        trakt_id, simkl_id, tmdb, tvdb, imdb, mal, slug, title, network,
        watched, total, cadence, premiere, finale, started_airing, finished_airing,
        abandoned_form, added_by, created_at)
    SELECT user_id, month,
           CASE WHEN abandoned = 1 OR bucket = 'abandoned' THEN 'abandoned'
                ELSE 'completed' END,
           media, match_source, match_id, season,
           trakt_id, simkl_id, tmdb, tvdb, imdb, mal, slug, title, network,
           watched, total, cadence, premiere, finale, started_airing, finished_airing,
           abandoned_form, added_by, CAST(strftime('%s', 'now') AS INTEGER)
      FROM distrakt_shows
     WHERE abandoned = 1 OR bucket IN ('abandoned', 'completed');

"""

# A month the calendar has not reached yet can hold NOTHING BUT ITS PREMIERES, and
# that is provable rather than assumed: the recent-viewing sweep that is the only
# other way a row reaches a month was deliberately skipped for a month that had
# not begun, because what somebody is part-way through today says nothing about a
# month nobody has reached. So every row on such a month is one of its
# announcements, whatever its stored fields do or do not say — and they say
# nothing at all, because a month that never froze never had them written.
#
# WITHOUT THIS THOSE ROWS BECOME VIEWER RECORDS, and that does not stay quiet. A
# month pre-filled ahead of time is exactly where a title turned away on the
# calendar sits: a turn-away there means "never put this in that month", which the
# month-ahead rule honours by taking the row off. Read as something the viewer has
# in hand instead, the very same mark reads as "I was following this and stopped"
# and lands as a verdict on the month UNDER WAY — so opening the current month
# fills its Abandoned section with next month's titles.
#
# Parameterised on the month under way rather than written into the script,
# because "has the calendar reached this month" is a question about today and the
# answer is different on the day this runs than it was when it was written.
_MIGRATION_19_MONTHS_AHEAD = """
INSERT INTO distrakt_month_records
       (user_id, month, kind, media, match_source, match_id, season,
        trakt_id, simkl_id, tmdb, tvdb, imdb, mal, slug, title, network,
        watched, total, cadence, premiere, finale, started_airing, finished_airing,
        abandoned_form, added_by, created_at)
    SELECT s.user_id, s.month,
           CASE WHEN s.season <= 1 THEN 'series_premiere' ELSE 'season_premiere' END,
           s.media, s.match_source, s.match_id, s.season,
           s.trakt_id, s.simkl_id, s.tmdb, s.tvdb, s.imdb, s.mal, s.slug,
           s.title, s.network,
           0, s.total, s.cadence, s.premiere, s.finale,
           s.started_airing, s.finished_airing,
           NULL, s.added_by, CAST(strftime('%s', 'now') AS INTEGER)
      FROM distrakt_shows s
     WHERE s.month > ?
       AND NOT EXISTS (
               SELECT 1 FROM distrakt_month_records r
                WHERE r.user_id = s.user_id AND r.month = s.month
                  AND r.media = s.media AND r.match_source = s.match_source
                  AND r.match_id = s.match_id AND r.season = s.season
                  AND r.kind IN ('series_premiere', 'season_premiere'));
"""

# Everything still in the middle: one record per season, from its latest month.
# Months still ahead are excluded on both counts — a row on one is that month's
# announcement and was filed as such above, and it must not win the "latest month"
# race either, or a season the viewer is part-way through would take its counts
# from a month that has not happened.
_MIGRATION_19_USER_RECORDS = """
INSERT INTO distrakt_user_seasons
       (user_id, media, match_source, match_id, season,
        trakt_id, simkl_id, tmdb, tvdb, imdb, mal, slug, title, network, kind,
        watched, total, cadence, premiere, finale, started_airing, finished_airing,
        came_back, added_by, created_at)
    SELECT s.user_id, s.media, s.match_source, s.match_id, s.season,
           s.trakt_id, s.simkl_id, s.tmdb, s.tvdb, s.imdb, s.mal, s.slug,
           s.title, s.network,
           CASE WHEN s.finished_airing = 1 OR s.cadence = 'b' THEN 'catchup'
                ELSE 'keepup' END,
           s.watched, s.total, s.cadence, s.premiere, s.finale,
           s.started_airing, s.finished_airing,
           0, s.added_by, CAST(strftime('%s', 'now') AS INTEGER)
      FROM distrakt_shows s
     WHERE s.month <= ?
       AND NOT EXISTS (
               SELECT 1 FROM distrakt_shows v
                WHERE v.user_id = s.user_id AND v.media = s.media
                  AND v.match_source = s.match_source AND v.match_id = s.match_id
                  AND v.season = s.season
                  AND (v.abandoned = 1 OR v.bucket IN ('abandoned', 'completed')))
       AND s.month = (
               SELECT MAX(t.month) FROM distrakt_shows t
                WHERE t.user_id = s.user_id AND t.media = s.media
                  AND t.match_source = s.match_source AND t.match_id = s.match_id
                  AND t.season = s.season AND t.month <= ?);
"""


def MIGRATION_19(conn: sqlite3.Connection) -> None:
    # Counted BEFORE anything is rebuilt, because the answer decides whether the
    # rebuild may happen at all. Both checks are about a row that could not be
    # ADDRESSED afterwards: the new tables file a record under its identity triple
    # and, for a month record, under its month key. A row missing either would
    # land somewhere nothing can reach it, and a migration that silently strands
    # somebody's roster is worse than one that stops and says what is wrong.
    unaddressable = conn.execute(
        "SELECT COUNT(*) FROM distrakt_shows "
        "WHERE match_id IS NULL OR match_id = '' "
        "   OR month IS NULL OR month NOT GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'"
    ).fetchone()[0]
    if unaddressable:
        raise RuntimeError(
            f"{unaddressable} tracker roster row(s) carry no shared id or no "
            "readable 'YYYY-MM' month, so they cannot be filed under the identity "
            "the tracker's records are now keyed on. Nothing has been changed. "
            "Fix or delete those rows and start the app again."
        )
    total_rows = conn.execute("SELECT COUNT(*) FROM distrakt_shows").fetchone()[0]
    distinct_seasons = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT user_id, media, match_source, "
        "match_id, season FROM distrakt_shows)"
    ).fetchone()[0]
    # The rows a month that never froze cannot prove a premiere date for. Counted
    # here, before the rebuild, because afterwards the evidence is gone: the rows
    # have become user records and nothing left says which month they sat on. The
    # complement of the premiere pass's own test, so the two cannot disagree about
    # which rows it skipped.
    # The month under way, as a "YYYY-MM" key. Zero-padded so it compares
    # chronologically against the stored keys with `<=` and `>`, which is the same
    # reason the tracker pads its own; an unpadded month would sort "2026-9" after
    # "2026-12" and mis-file a whole month's rows.
    #
    # Read through the clock seam rather than from date.today(), so an instance
    # running on an overridden date classifies its rows by the same calendar the
    # app will then read them with. Split one way and rendered the other, a month
    # ahead would be rebuilt as premieres and then read as the month under way.
    today = clock.today()
    under_way = f"{today.year:04d}-{today.month:02d}"
    # Rows on a month that HAS begun and never froze, so no premiere date was ever
    # written for them. Counted before the rebuild, because afterwards the evidence
    # is gone. Months still ahead are excluded: their rows are filed as that
    # month's premieres below and lose nothing, so naming them here would report a
    # gap that is not there.
    undated = conn.execute(
        "SELECT s.month, COUNT(*) FROM distrakt_shows s "
        "JOIN distrakt_months m ON m.user_id = s.user_id AND m.month = s.month "
        "WHERE m.closed = 0 AND s.month <= ? "
        "  AND (s.premiere IS NULL OR instr(s.premiere, '/') <= 1) "
        "GROUP BY s.month ORDER BY s.month",
        (under_way,),
    ).fetchall()
    _run_script(conn, MIGRATION_19_SQL)
    # The two passes that turn on where the calendar stands, so they take the month
    # under way as a parameter rather than being written into the script above.
    conn.execute(_MIGRATION_19_MONTHS_AHEAD, (under_way,))
    conn.execute(_MIGRATION_19_USER_RECORDS, (under_way, under_way))
    conn.execute("DROP TABLE distrakt_shows")
    # The copies are what the split removes: a season carried onto four months had
    # four rows saying the same thing about the viewer, and now has one record.
    # Reported because it is a large drop in the row count and a reader finding it
    # in the logs should not have to guess whether something was lost.
    carried = total_rows - distinct_seasons
    logger.info(
        "Split the tracker's rows into month records and user records: %d row(s) "
        "covering %d season(s) became %d month record(s) and %d user record(s); "
        "%d were repeat copies of a season carried onto a later month.",
        total_rows, distinct_seasons,
        conn.execute("SELECT COUNT(*) FROM distrakt_month_records").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM distrakt_user_seasons").fetchone()[0],
        carried,
    )
    if undated:
        # Said plainly and named by month, because the visible symptom is a
        # month's announcement going quiet and nothing else would explain it.
        logger.info(
            "%s had not frozen yet, so their rows never stored a premiere date and "
            "none of them could be filed as that month's premieres here: %s. Their "
            "seasons were kept as the viewer's own records and nothing was lost. "
            "Opening such a month files back the ones that have not aired yet — by "
            "then a season lookup has supplied the date this could not — so only "
            "the titles that had ALREADY premiered stay unannounced, and "
            "re-importing that month's premieres from the calendar is what "
            "restores those.",
            "One month" if len(undated) == 1 else f"{len(undated)} months",
            ", ".join(f"{month} ({count} row(s))" for month, count in undated),
        )


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
    (7, MIGRATION_7),
    (8, MIGRATION_8),
    (9, MIGRATION_9),
    (10, MIGRATION_10),
    (11, MIGRATION_11),
    (12, MIGRATION_12),
    (13, MIGRATION_13),
    (14, MIGRATION_14),
    (15, MIGRATION_15),
    (16, MIGRATION_16),
    (17, MIGRATION_17),
    (18, MIGRATION_18),
    (19, MIGRATION_19),
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
