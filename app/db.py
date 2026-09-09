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
#   - anything else ON A MONTH THAT FROZE -> ONE user record for the season, from
#     its latest frozen month's row (the most recent copy carries the most recent
#     counts), catchup when the season has finished airing or drops all at once,
#     keepup otherwise.
#   - anything else on a month that NEVER froze -> the stash, untouched. See below.
# A season settled on ANY month is off the user list entirely, because giving up
# on a season in March is a statement about the season and an older in-progress
# copy of it must not resurrect it.
#
# THE COPIES ARE WHAT GOES. A season carried onto three months had three rows and
# now has one user record or one verdict; that collapse is the point of the change
# and is reported rather than silent.
#
# A MONTH THAT NEVER FROZE CANNOT BE CLASSIFIED AT ALL, AND IS NOT GUESSED AT. In
# the old schema `premiere`, `finale`, `cadence`, `started_airing`,
# `finished_airing`, `watched`, `total` and `bucket` were written onto a row only
# when its month FROZE; until then they were recomputed from the provider on every
# view and never stored. So on a month with `closed = 0` all of them are empty, and
# the premiere pass above — which asks whether the row's premiere date falls in the
# row's month — can prove nothing about a single one of its rows.
#
#   THE ANSWER IS NOT IN THIS DATABASE AND NO COLUMN STANDS IN FOR IT. Whether a
#   row is that month's ANNOUNCEMENT or a season the viewer was part-way through is
#   the premiere date's question, and the premiere date is a fact about the show
#   that lives at the provider. Every local substitute was tried against a real
#   frozen month and every one of them was wrong in both directions: "the viewer has
#   watched none of it" calls a premiere they started watching a viewer record, and
#   "it appears on no earlier month" calls a years-old catch-up title that had been
#   sitting unwatched on the list a premiere. `added_by` cannot arbitrate either —
#   it is '' on every row written before that column existed.
#
#   SO THOSE ROWS ARE KEPT, VERBATIM, AND SETTLED LATER. They go to
#   `distrakt_unsettled_rows` carrying the one thing that cannot be recovered from
#   anywhere else — WHICH MONTH THEY SAT ON — and app/distrakt/unsettled.py settles
#   them on the first tracker load, where a season lookup can supply the date this
#   migration has no way to ask for. A migration that does not know must not decide;
#   what it must not do is DESTROY the evidence, and it is that — not the missing
#   date — that made the first version of this pass unrecoverable. Feeding those
#   rows to the user-records pass discarded their month, and once a month is gone no
#   later pass, provider call or operator can work out what it was.
#
# THIS PASS ASKS THE STORE WHAT FROZE, NOT THE CLOCK. `distrakt_months.closed` is
# the whole test, and it is a fact recorded at the time. The first version of this
# migration read `clock.today()` and split rows on whether their month was still
# ahead, which is a question whose answer CHANGES between the day a month is built
# and the day the migration runs: a month built ahead of time in July was a preview
# then and the month under way by August, so its announcements were read as seasons
# the viewer had in hand, and every calendar turn-away on one of them landed as
# "I was following this and gave up". It also meant this migration produced
# different output on the same database depending on the date — which is why it
# passed in development, against a store whose months had all frozen, and destroyed
# a month in production, where nobody had opened the app since before it ended.
#
# distrakt_prompt_dismissals records the viewer declining to add a season the
# tracker has never heard of. Without somewhere to record the refusal it would be
# re-derived from the same watch history on the very next load and the ✗ would do
# nothing. It is keyed by SEASON, not by episode, or every further episode of the
# same season would ask again.
# The rows a roster split could not classify, held until something can ask a
# provider what they were. Drained by app/distrakt/unsettled.py on the first
# tracker load.
#
# ONE DEFINITION, RUN FROM TWO MIGRATIONS. Migration 19 creates it and fills it;
# migration 20 creates it empty for an instance that applied the version of 19 that
# predates it, which dropped the old roster table without keeping anything. Those
# instances have nothing left to hold, but the drain pass reads this table on every
# tracker load and a missing table is an error rather than an empty answer. Written
# once here so the two cannot disagree about the shape.
#
# `month` IS WHY THIS TABLE EXISTS. Every other column can be re-fetched or
# re-derived; which month the row sat on is recorded nowhere else in this database,
# and the pass that once fed these rows to the user records dropped it on the floor.
# Carrying it is the whole job.
#
# The live columns are deliberately NOT carried. `watched`, `total`, `cadence`,
# `premiere`, `finale`, `started_airing` and `finished_airing` were written only by
# a freeze, so on these rows they are zero and NULL — copying them would move a set
# of empty fields around and invite a later reader to trust them. The season lookup
# the drain makes is what fills them, and it is the same lookup every listed season
# already costs on every load.
UNSETTLED_ROWS_DDL = """
CREATE TABLE IF NOT EXISTS distrakt_unsettled_rows (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month        TEXT    NOT NULL,
    media        TEXT    NOT NULL,
    match_source TEXT    NOT NULL,
    match_id     TEXT    NOT NULL,
    season       INTEGER NOT NULL,
    trakt_id     INTEGER,
    simkl_id     INTEGER,
    tmdb         INTEGER,
    tvdb         INTEGER,
    imdb         TEXT,
    mal          INTEGER,
    slug         TEXT,
    title        TEXT    NOT NULL DEFAULT '',
    network      TEXT    NOT NULL DEFAULT '',
    added_by     TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, month, media, match_source, match_id, season)
)
"""

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
--
-- Self-restricting to months that FROZE, with no test for it: `premiere` is one
-- of the columns only a freeze ever wrote, so a row on a month that never froze
-- carries NULL here and cannot match. Those rows reach the stash instead.
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
--
-- NOT restricted to months that froze, and the `abandoned` flag is why. A freeze
-- wrote `bucket`, so that half only ever matches a frozen month — but the flag was
-- set the moment the viewer pressed Abandon, on whatever month was open at the
-- time. Giving up is the one verdict an UNFROZEN month can prove it reached, so it
-- is honoured here rather than deferred to the stash.
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

-- The rows this migration cannot settle: everything on a month that never froze
-- and reached no verdict. The table itself is created by UNSETTLED_ROWS_DDL, just
-- before this script runs — see there for what it holds and why.
INSERT INTO distrakt_unsettled_rows
       (user_id, month, media, match_source, match_id, season,
        trakt_id, simkl_id, tmdb, tvdb, imdb, mal, slug, title, network,
        added_by, created_at)
    SELECT s.user_id, s.month, s.media, s.match_source, s.match_id, s.season,
           s.trakt_id, s.simkl_id, s.tmdb, s.tvdb, s.imdb, s.mal, s.slug,
           s.title, s.network, s.added_by, CAST(strftime('%s', 'now') AS INTEGER)
      FROM distrakt_shows s
     WHERE NOT EXISTS (
               SELECT 1 FROM distrakt_months m
                WHERE m.user_id = s.user_id AND m.month = s.month AND m.closed = 1)
       -- A season settled on ANY month is settled, and the same rule the user
       -- records pass applies: an older in-progress copy must not resurrect it.
       -- This also takes out the rows the verdict pass above just claimed from an
       -- unfrozen month, so nothing is both settled and pending.
       AND NOT EXISTS (
               SELECT 1 FROM distrakt_month_records r
                WHERE r.user_id = s.user_id AND r.media = s.media
                  AND r.match_source = s.match_source AND r.match_id = s.match_id
                  AND r.season = s.season
                  AND r.kind IN ('completed', 'abandoned'));

"""

# Everything still in the middle ON A MONTH THAT FROZE: one record per season, from
# its latest such month, since the most recent copy carries the most recent counts.
#
# A MONTH THAT NEVER FROZE IS EXCLUDED FROM BOTH HALVES, and that exclusion is the
# fix this pass exists in. Its rows go to the stash, so they must not be written
# here — and they must not win the "latest month" race either, or a season the
# viewer is part-way through would take its counts from a row whose count columns
# were never written and read as 0 of 0.
#
# The old version of this asked the clock instead: `month <= today`, which swept in
# every unfrozen month up to and including the one under way. That is how an entire
# month's roster lost its month.
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
     WHERE EXISTS (
               SELECT 1 FROM distrakt_months m
                WHERE m.user_id = s.user_id AND m.month = s.month AND m.closed = 1)
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
                  AND t.season = s.season
                  AND EXISTS (SELECT 1 FROM distrakt_months m2
                               WHERE m2.user_id = t.user_id AND m2.month = t.month
                                 AND m2.closed = 1));
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
    conn.execute(UNSETTLED_ROWS_DDL)
    _run_script(conn, MIGRATION_19_SQL)
    conn.execute(_MIGRATION_19_USER_RECORDS)
    # Read AFTER the script, because the script is what fills the stash. Nothing
    # here reads the clock: what this migration does to a row is decided entirely
    # by what is written in the database, so the same database migrates to the same
    # thing on any day, which is what makes the result testable at all.
    pending = conn.execute(
        "SELECT month, COUNT(*) FROM distrakt_unsettled_rows "
        "GROUP BY month ORDER BY month"
    ).fetchall()
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
    if pending:
        # Said plainly and named by month. An operator who opens the tracker in the
        # seconds before the first load drains this would otherwise find a month
        # short of its titles with nothing anywhere saying why, and "wait for the
        # first load" is only reassuring if you know it is coming.
        logger.info(
            "%s had not frozen yet, so nothing stored says which of their rows were "
            "announcements and which were seasons in hand: %s. Those rows are held "
            "as they were and settled on the first tracker load, when a season "
            "lookup can supply the premiere dates this could not ask for. Nothing "
            "is lost in the meantime and nothing needs doing.",
            "One month" if len(pending) == 1 else f"{len(pending)} months",
            ", ".join(f"{month} ({count} row(s))" for month, count in pending),
        )


# The held-rows table, for an instance that applied the FIRST version of migration
# 19 — the one that classified an unfrozen month's rows by comparing their month
# against the clock, turned every one of them into a viewer record, and dropped the
# old roster table. Nothing here can give those instances their months back: the
# rows are gone and no migration can invent what is not written down. What it does
# is make them consistent with an instance that migrated after the correction, so
# the drain pass finds an empty table rather than no table.
#
# Migration 19 was corrected in place rather than superseded, which is a departure
# from the rule right below this and is recorded in CLAUDE.md with the reason: a
# corrective migration cannot help anyone here, because 19 had already dropped the
# only copy of the data by the time one could run.
def MIGRATION_20(conn: sqlite3.Connection) -> None:
    conn.execute(UNSETTLED_ROWS_DDL)


# Migration 21 — which services may issue an identity stops being written into
# the schema.
#
# `linked_identities.provider` and `auth_handshakes.provider` both carried
# `CHECK (provider IN ('plex', 'trakt'))`, and SQLite cannot alter a CHECK in
# place, so admitting a third service means rebuilding both tables.
#
# THE CHECK IS REMOVED RATHER THAN WIDENED, following show_posters.source: an
# open set of names rather than a CHECK constraint, so adding a provider is not a
# migration. Listing three names buys nothing a fourth would not cost this same
# rebuild for. The set of services that may mint an identity is decided in the
# auth code that mints one — each provider's route module owns its own PROVIDER
# constant and there is exactly one place per service that writes it — and a
# constraint restating that decision only makes it expensive to change, not
# safer: a value the application never produces cannot arrive here anyway.
#
# `auth_handshakes.purpose` KEEPS its CHECK. That one is a genuinely closed set
# this app defines ('login' or 'link', and there is no third thing a handshake
# can be for), not an open list of other people's service names.
#
# Both rebuilds are the shape migration 14 used and 18 repeated: build `_new`,
# INSERT ... SELECT every column by name, DROP, RENAME, and RE-CREATE THE INDEX —
# a rebuild drops the old table's indexes with it, and a missing
# ix_linked_identities_user is a silent full scan on every account page.
MIGRATION_21_SQL = """
CREATE TABLE linked_identities_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Which service authorized this account. An open set of names rather than a
    -- CHECK constraint, so adding a provider is not a migration; the valid set
    -- is enforced where it is decided, in the auth flow that mints an identity.
    provider         TEXT    NOT NULL,
    -- The provider's immutable numeric account id, stored as text. NEVER a
    -- username, slug, or email: those are user-changeable and can be released
    -- and re-registered by someone else, so keying on one would let a released
    -- name inherit the linked account.
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
    -- What makes "this provider account is already known -> log in as its
    -- owner" a single lookup.
    UNIQUE (provider, provider_user_id)
);
INSERT INTO linked_identities_new
    (id, user_id, provider, provider_user_id, display_name, access_token,
     refresh_token, token_expires_at, refreshing_until, created_at, last_login_at)
    SELECT id, user_id, provider, provider_user_id, display_name, access_token,
           refresh_token, token_expires_at, refreshing_until, created_at, last_login_at
      FROM linked_identities;
DROP TABLE linked_identities;
ALTER TABLE linked_identities_new RENAME TO linked_identities;
CREATE INDEX ix_linked_identities_user ON linked_identities(user_id);

CREATE TABLE auth_handshakes_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    state          TEXT    NOT NULL UNIQUE,
    -- Open, for the same reason as linked_identities.provider above.
    provider       TEXT    NOT NULL,
    -- Closed, and staying closed: a handshake is begun either to sign in or to
    -- attach an identity to an account already in session, and there is no
    -- third thing it can be for. This one is our own vocabulary, not a list of
    -- other people's service names.
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
INSERT INTO auth_handshakes_new
    (id, state, provider, purpose, session_id, invite_token, pkce_verifier,
     plex_pin_id, created_at, expires_at, consumed_at)
    SELECT id, state, provider, purpose, session_id, invite_token, pkce_verifier,
           plex_pin_id, created_at, expires_at, consumed_at
      FROM auth_handshakes;
DROP TABLE auth_handshakes;
ALTER TABLE auth_handshakes_new RENAME TO auth_handshakes;
CREATE INDEX ix_auth_handshakes_expires ON auth_handshakes(expires_at);
"""


def MIGRATION_21(conn: sqlite3.Connection) -> None:
    # COUNTED EITHER SIDE OF THE REBUILD, and a mismatch refuses.
    #
    # These two tables are how a person gets into their account. An identity row
    # quietly lost here is a login method gone — for an account created purely by
    # signing in with a provider, and holding no password, it is the whole
    # account gone, with nothing anywhere saying it happened and nothing to
    # re-derive it from. Migration 19 is this repository's standing example of
    # exactly that, so the loud refusal is cheap insurance against the shape of
    # failure that costs the most.
    #
    # The realistic way a copy loses rows is an identity whose user_id no longer
    # names a user: the new table's foreign key would reject it. That is counted
    # FIRST so the operator is told which rows and why, rather than reading a
    # bare FOREIGN KEY constraint failed and guessing.
    orphans = conn.execute(
        "SELECT COUNT(*) FROM linked_identities li "
        "WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = li.user_id)"
    ).fetchone()[0]
    if orphans:
        raise RuntimeError(
            f"{orphans} linked provider identity row(s) belong to a user account "
            "that no longer exists, so they cannot be carried into the rebuilt "
            "table. Nothing has been changed. Delete those rows, or restore the "
            "accounts they name, and start the app again."
        )
    identities = conn.execute("SELECT COUNT(*) FROM linked_identities").fetchone()[0]
    handshakes = conn.execute("SELECT COUNT(*) FROM auth_handshakes").fetchone()[0]

    _run_script(conn, MIGRATION_21_SQL)

    after_identities = conn.execute("SELECT COUNT(*) FROM linked_identities").fetchone()[0]
    after_handshakes = conn.execute("SELECT COUNT(*) FROM auth_handshakes").fetchone()[0]
    if (after_identities, after_handshakes) != (identities, handshakes):
        # Raising rolls the whole migration back — the runner wraps each step in
        # its own transaction — so the old tables survive intact and the operator
        # still has the rows to look at.
        raise RuntimeError(
            "Rebuilding the provider tables did not carry every row across "
            f"({identities} -> {after_identities} linked identities, "
            f"{handshakes} -> {after_handshakes} handshakes). Nothing has been "
            "changed. This should be impossible; please report it rather than "
            "working around it."
        )


# Migration 22 — the tracker stops assuming one service answered.
#
# Migration 18 re-keyed the tracker onto shared title ids, which made the same
# season from two services ONE row. That was the right move for identity and the
# wrong one for progress: two services genuinely know different things about a
# season, and a single row per season has nowhere to put the second answer. It
# would have to overwrite the first, silently, on whichever sync ran last.
#
# So WHICH SERVICE SAID SO joins the key of the two cache tables. `source` is IN
# the primary key rather than beside it: the row is one service's statement about
# a season, and two statements about the same season are two rows that must both
# survive. Existing rows are Trakt's — nothing else has ever written them — which
# is what the literal in the INSERT and the column default both state.
#
# SQLITE CANNOT ADD A COLUMN TO A PRIMARY KEY, so both are table rebuilds in
# migration 18's shape: build `_new`, INSERT ... SELECT with the literal source,
# DROP, RENAME. Neither table carries an index (their primary key is the only way
# anything reaches them), so there is none to re-create — checked against
# sqlite_master rather than assumed.
#
# THE SINGLETON STATE GAINS THE SAME DIMENSION. `last_synced` was one cursor
# because there was one history feed to read; two sources have two, gated by two
# beacons, and a shared cursor would make an unchanged Trakt history re-read from
# Simkl's position. It becomes `cursors_json`, {source: cursor}, and the existing
# value moves under "trakt". `beacons_json` is already JSON and is nested the
# same way, in Python rather than in SQL: the transform reads a stored document
# and writes a different one, which json_object()/json() would express less
# clearly and only on a build with JSON1 compiled in.
#
# A FROZEN MONTH RECORDS EVERY SOURCE'S NUMBER. `watched_by_source` and
# `total_by_source` are JSON objects keyed by source. `watched` and `total` STAY
# and keep holding the primary source's number: every existing reader — the
# Discord post above all, which is prose and not a ledger — asks for one number
# and must keep getting one. The pair beside them is what lets a month that froze
# while two services disagreed still show the disagreement years later, instead of
# claiming a number neither of them reported.
MIGRATION_22_SQL = """
-- Which services this account wants answering for it, and whose value wins when
-- two of them fill the same field with different things. Per ACCOUNT rather than
-- per view: it is a statement about which services are yours, not about how one
-- page is drawn.
--
-- An account with no row here has no opinion yet and reads as every default
-- below, so nothing has to create a row when an account is created.
CREATE TABLE source_prefs (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    -- 'auto' | 'trakt' | 'simkl' | 'both'.
    --
    -- 'auto' AND 'both' ARE DELIBERATELY DIFFERENT VALUES and the difference is
    -- what happens when a link lapses. 'auto' means "every service this account
    -- has linked" — the right default for somebody who has just linked a second
    -- one and has no opinion — and it follows the links, so unlinking a service
    -- quietly stops asking it. 'both' is a STATED preference for two services,
    -- and a stated preference must not silently become single-source because a
    -- token expired; it keeps asking, and the missing one shows as missing.
    -- Collapsing them into one value would make an unlink and a decision
    -- indistinguishable afterwards.
    calendar_source TEXT NOT NULL DEFAULT 'auto',
    tracker_source  TEXT NOT NULL DEFAULT 'auto',
    -- {"default": <source>, "fields": {field: source}, "show_both": [field, ...]}.
    -- Resolved at READ over already-cached data, so changing any of it is
    -- instant and invalidates nothing.
    precedence_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE distrakt_show_progress_new (
    user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    media                 TEXT    NOT NULL,
    match_source          TEXT    NOT NULL,
    match_id              TEXT    NOT NULL,
    season                INTEGER NOT NULL,
    -- WHICH SERVICE REPORTED THIS. In the key, so two services' answers about
    -- one season are two rows and neither overwrites the other. An open set of
    -- names rather than a CHECK, following linked_identities.provider: the valid
    -- set is decided in the registry that hands out sync ports.
    source                TEXT    NOT NULL DEFAULT 'trakt',
    watched_episodes_json TEXT    NOT NULL DEFAULT '[]',
    -- The service's own id, so the progress record can be refetched. Only the
    -- ids a sync actually calls with live on a cache row; the shared ids the
    -- waterfall did not pick are on the ROSTER row.
    trakt_id              INTEGER,
    simkl_id              INTEGER,
    PRIMARY KEY (user_id, media, match_source, match_id, season, source)
);
INSERT INTO distrakt_show_progress_new
       (user_id, media, match_source, match_id, season, source,
        watched_episodes_json, trakt_id, simkl_id)
    SELECT user_id, media, match_source, match_id, season, 'trakt',
           watched_episodes_json, trakt_id, simkl_id
      FROM distrakt_show_progress;
DROP TABLE distrakt_show_progress;
ALTER TABLE distrakt_show_progress_new RENAME TO distrakt_show_progress;

CREATE TABLE distrakt_movie_watches_new (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    media        TEXT    NOT NULL,
    match_source TEXT    NOT NULL,
    match_id     TEXT    NOT NULL,
    -- In the key for the same reason as the season table's: two services can
    -- both have seen the same film, on different dates, and both are true.
    source       TEXT    NOT NULL DEFAULT 'trakt',
    watched_at   TEXT,
    title        TEXT    NOT NULL DEFAULT '',
    year         INTEGER,
    trakt_id     INTEGER,
    simkl_id     INTEGER,
    PRIMARY KEY (user_id, media, match_source, match_id, source)
);
INSERT INTO distrakt_movie_watches_new
       (user_id, media, match_source, match_id, source, watched_at, title, year,
        trakt_id, simkl_id)
    SELECT user_id, media, match_source, match_id, 'trakt', watched_at, title,
           year, trakt_id, simkl_id
      FROM distrakt_movie_watches;
DROP TABLE distrakt_movie_watches;
ALTER TABLE distrakt_movie_watches_new RENAME TO distrakt_movie_watches;

-- Every source's number, beside the one number `watched`/`total` already hold.
-- Both are JSON objects keyed by source, and both are NULL on a record written
-- before this — which reads as "nobody recorded a breakdown", the truth.
ALTER TABLE distrakt_month_records ADD COLUMN watched_by_source TEXT;
ALTER TABLE distrakt_month_records ADD COLUMN total_by_source TEXT;
"""

# The singleton state, rebuilt around per-source cursors. Separate from the script
# above because the two columns it carries forward are TRANSFORMED rather than
# copied, and that transform is done in Python (see MIGRATION_22).
MIGRATION_22_STATE_SQL = """
CREATE TABLE distrakt_watch_state_new (
    user_id      INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    -- {source: cursor} — the point in each service's history we have read
    -- through, as the string that service speaks, kept per source because two
    -- services are read independently and one being unreachable must not wind
    -- the other's position back.
    cursors_json TEXT,
    -- {source: beacon} — each service's own "last changed at" blob, the gate an
    -- incremental sync opens with. Nested by source for the same reason: two
    -- sources means two beacon calls, not one shared answer.
    beacons_json TEXT
);
"""


def MIGRATION_22(conn: sqlite3.Connection) -> None:
    # COUNTED BEFORE THE REBUILD, as migration 18 does, and compared after.
    #
    # This is a person's viewing history and it cannot be re-derived: a season's
    # watched-episode set with its dates is the answer a service gave at a moment
    # that has passed, and nothing in this app or outside it can reconstruct a
    # dropped row. A rebuild that carried fewer rows than it found must therefore
    # stop rather than finish — raising rolls the whole migration back, because
    # the runner wraps each step in its own transaction, so the old tables survive
    # intact and the operator still has the rows to look at.
    progress = conn.execute("SELECT COUNT(*) FROM distrakt_show_progress").fetchone()[0]
    movies = conn.execute("SELECT COUNT(*) FROM distrakt_movie_watches").fetchone()[0]
    states = conn.execute("SELECT COUNT(*) FROM distrakt_watch_state").fetchone()[0]

    _run_script(conn, MIGRATION_22_SQL)

    # Read before the old table is dropped, transformed here, written after. One
    # row per user, so holding them all is bounded by the account count.
    carried = [
        (row["user_id"], row["last_synced"], row["beacons_json"])
        for row in conn.execute(
            "SELECT user_id, last_synced, beacons_json FROM distrakt_watch_state"
        ).fetchall()
    ]
    _run_script(conn, MIGRATION_22_STATE_SQL)
    for user_id, last_synced, beacons_json in carried:
        cursors = json.dumps({"trakt": last_synced}) if last_synced else None
        # A beacon document that will not parse is left behind rather than nested
        # blind. It is a cache of one service's "something changed" marker, and
        # losing it costs exactly one unnecessary history pull on the next load —
        # where nesting an unreadable string would hand the loader a shape it
        # cannot use and would have to guess about for ever.
        nested = None
        if beacons_json:
            try:
                nested = json.dumps({"trakt": json.loads(beacons_json)})
            except (TypeError, ValueError):
                logger.info(
                    "A stored activity beacon could not be read while giving it a "
                    "source, so it was dropped; the next tracker load fetches a "
                    "fresh one. Nothing about what was watched lives here."
                )
        conn.execute(
            "INSERT INTO distrakt_watch_state_new (user_id, cursors_json, beacons_json) "
            "VALUES (?, ?, ?)",
            (user_id, cursors, nested),
        )
    conn.execute("DROP TABLE distrakt_watch_state")
    conn.execute("ALTER TABLE distrakt_watch_state_new RENAME TO distrakt_watch_state")

    after = (
        conn.execute("SELECT COUNT(*) FROM distrakt_show_progress").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM distrakt_movie_watches").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM distrakt_watch_state").fetchone()[0],
    )
    if after != (progress, movies, states):
        raise RuntimeError(
            "Giving the tracker's cached rows a source did not carry every row "
            f"across ({progress} -> {after[0]} season progress row(s), "
            f"{movies} -> {after[1]} film watch(es), {states} -> {after[2]} sync "
            "state row(s)). Nothing has been changed. This should be impossible; "
            "please report it rather than working around it."
        )


MIGRATION_23 = """
-- {source: {source's own show id: plays}} — how many plays each service last
-- reported against each title, and NOTHING about which episodes they were.
--
-- IT IS A CHANGE DETECTOR, WHICH IS WHY IT EARNS A COLUMN. One service publishes
-- no cheap whole-library watch record at all: the only way to learn what somebody
-- has seen of a title is to ask about that title, one call each, so re-reading a
-- roster grew with everything ever tracked and a forced refresh spent seconds on
-- it. It DOES publish a per-title play count for the whole library in a handful
-- of calls, and that count moves in both directions with the watched set — so
-- comparing this pass's counts against the stored ones names exactly the titles
-- worth asking about properly.
--
-- NULL IS THE ORDINARY STATE AND MEANS "no sweep has been stored", which reads as
-- "nothing can be concluded about what changed" and falls back to asking about
-- every title once. That is what an instance sees on the first load after this,
-- and what a restored backup sees: the column is deliberately NOT in the tracker's
-- export, because it is a cache of one service's answer that costs one sweep to
-- rebuild, and a stored map carried alongside restored progress rows could only
-- claim that nothing had changed when everything might have.
ALTER TABLE distrakt_watch_state ADD COLUMN play_counts_json TEXT;
"""


MIGRATION_24 = """
-- One row per Simkl title, keyed on the id space and the media kind — never
-- on an airing. A show listed twenty times in a month is twenty calendar
-- entries and ONE row here, because the fields kept (genres, network,
-- country, certification, runtime, status, overview) are facts about the
-- TITLE, not about any one showing of it.
--
-- FILLED BY A BACKGROUND DRAIN, NEVER BY THE FILL OR READ PATH. Simkl's
-- calendar CDN files carry none of these fields at all (see
-- app/providers/simkl/calendar.py's Record docstring), so a calendar entry
-- from that source is stored with them empty and this table is the only place
-- they are ever filled in — read-time overlay, over already-cached windows,
-- costing one batched DB read and no network call.
--
-- `payload` IS NEVER NULL, EVEN FOR A FAILED LOOKUP. An id Simkl does not
-- recognise, or a request that failed outright, still gets a row (with an
-- empty payload) the moment it is attempted — that row's mere existence is
-- what stops the same id being queued again on every single read; only
-- `failed_at`/`fail_count` say whether what it holds is real data or a
-- recorded failure waiting out its backoff.
CREATE TABLE simkl_titles (
    simkl_id    INTEGER NOT NULL,
    -- 'show' or 'movie' — the same two-value vocabulary Record.media already
    -- uses. An anime title has no THIRD value here: Simkl's own /tv/{id}
    -- answers for an anime id exactly as it does for an ordinary show
    -- (measured live 2026-08-06, see app/providers/simkl/titles.py), so which
    -- endpoint a lookup used is not a fact this table needs to remember.
    media       TEXT    NOT NULL,
    payload     BLOB    NOT NULL,
    fetched_at  INTEGER NOT NULL,
    failed_at   INTEGER,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (simkl_id, media)
);
-- The retention sweep and the backoff check both judge age off this column,
-- so both are index-served rather than a table scan every heartbeat.
CREATE INDEX ix_simkl_titles_fetched ON simkl_titles(fetched_at);
"""


MIGRATION_25 = """
-- WHICH SERVICES ANSWER FOR ONE PARTICULAR CALENDAR, when the account-wide
-- `calendar_source` beside it is not the right answer for all of them.
-- {endpoint key: selection}, the same vocabulary that column holds, and empty
-- for every account that has never said otherwise.
--
-- WHY THE ACCOUNT-WIDE VALUE IS NOT ENOUGH, and this is measured rather than
-- anticipated: on one real month a second service contributed 1773 MOVIE records
-- against the first service's 46, because its movie calendar is a global release
-- calendar while the other's is curated. That same account's SHOW calendar is
-- where the second service adds coverage genuinely worth having. Those are
-- opposite answers about one service, and one column can only give one of them,
-- so an account would be choosing between an unreadable movies page and losing
-- that service's shows entirely.
--
-- KEYED ON THE ENDPOINT KEY, which is what a stored calendar window is keyed on
-- too, so "which services answer for movies" has one answer wherever it is
-- asked. A key this version does not recognise is ignored on the way out rather
-- than erroring — an endpoint can be retired, and a preference naming one should
-- not stop a page rendering.
--
-- NOT A SECOND PRECEDENCE MAP. Whose spelling of a title wins does not change
-- between calendars; how MANY items one of them lists does. Only the second is
-- per endpoint.
ALTER TABLE source_prefs ADD COLUMN endpoint_sources_json TEXT NOT NULL DEFAULT '{}';
"""


MIGRATION_26 = """
-- WHICH MARKETS AND WHICH RELEASE FORMATS A VIEWER'S FILMS CALENDAR SHOWS.
-- Two more per-viewer filter specs beside `genres` and `countries` above, in
-- the identical `us,gb,-br` format and read by the identical parser, because
-- they are the same kind of statement: what this ONE person's calendar
-- contains, applied at read against a cache shared with everybody else.
--
-- WHY A FILM NEEDS ITS OWN COUNTRY DIMENSION WHEN `countries` ALREADY EXISTS.
-- `countries` is where a title was MADE — one value, and on a live sample only
-- 67% of films have one at all. This is where and how it is being RELEASED,
-- which is a list per title and is the only thing that makes a global release
-- calendar readable: measured on one real August, 1314 films, of which 444 have
-- a US release block at all and 219 have one that is theatrical or digital.
-- Same word, different fact, so a viewer who wants American films and a viewer
-- who wants films out in America are asking two different questions.
--
-- THE TYPES ARE TMDB'S NUMBERING, WHICH SIMKL REPRODUCES: 1 premiere,
-- 2 limited theatrical, 3 theatrical, 4 digital, 5 physical, 6 TV. Stored as
-- the numbers rather than names so the stored value is the vocabulary the
-- service actually publishes and no translation table sits between the two.
--
-- EMPTY IS EVERY MARKET AND EVERY FORMAT, so nothing changes for an account
-- that never opens the filters, and neither column has any effect at all on a
-- show calendar — a release format is not a fact about an episode.
ALTER TABLE user_prefs ADD COLUMN movie_release_countries TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN movie_release_types TEXT NOT NULL DEFAULT '';
"""


# Ordered and forward-only. APPEND ONLY: new work adds entries here; an entry
# that has shipped is never edited, because instances in the field have already
# applied it and will never apply it again.
MIGRATION_27 = """
-- WHERE A FILM IS RELEASED, ACCORDING TO TRAKT.
--
-- WHY A SECOND TABLE AND NOT A ROW IN simkl_titles: that one is keyed on a
-- Simkl id and holds a Simkl payload, and a film both services list has one of
-- each. Widening it to hold either service's answer would make its primary key
-- a (source, id) pair and every reader ask which source a row came from before
-- trusting a field — for a table whose whole shape is "what does Simkl say
-- about this id". Two narrow tables keyed on their own service's id stay
-- readable, and neither has to know the other exists.
--
-- WHY IT EXISTS AT ALL. Trakt's CALENDAR payload carries no release schedule —
-- a film record arrives with a production country and nothing about where or
-- how it is released — so the release filter could never judge a film Trakt
-- listed. Trakt's per-title /movies/{id}/releases endpoint does carry it, in
-- exactly the shape the filter reads (measured on three live titles
-- 2026-08-11: 7, 28 and 54 release rows, each naming a country and a type).
-- This is where that answer is kept so it is fetched once per title rather
-- than once per read.
--
-- `payload` IS NEVER NULL, EVEN FOR A FAILED LOOKUP, for the reason
-- simkl_titles gives: the row's existence is what stops the same id being
-- queued again on every read, and failed_at/fail_count are what say whether
-- what it holds is real.
CREATE TABLE trakt_releases (
    trakt_id    INTEGER NOT NULL PRIMARY KEY,
    payload     BLOB    NOT NULL,
    fetched_at  INTEGER NOT NULL,
    failed_at   INTEGER,
    fail_count  INTEGER NOT NULL DEFAULT 0
);
-- The retention sweep and the backoff check both judge age off this column.
CREATE INDEX ix_trakt_releases_fetched ON trakt_releases(fetched_at);
"""


def MIGRATION_28(conn: sqlite3.Connection) -> None:
    """Re-address every cached Simkl answer that was filed under a client id.

    Simkl takes its client id as a QUERY PARAMETER, so it was part of the URL the
    response cache keys on and every stored catalogue answer belonged to the
    credential that fetched it. It does not: the same public title comes back
    with no client id, a bogus one and the real one, so the credential selects
    nothing and only ever narrowed who could read the row back. The key is built
    without it now (app/providers/simkl/transport.py's `cache_key` is the one
    statement of the shape), which leaves every row written before that change
    addressed by a key nothing will ever ask for again.

    WITHOUT THIS THEY WOULD NEVER LEAVE. `cache.set` writes these rows with no
    per-row TTL, so the age sweep skips them by design, and the size sweep does
    not fire until `api_cache_max_bytes` — a gigabyte. On a real instance holding
    a few dozen megabytes that is never, so 13,470 unreachable rows would sit
    there for the life of the database while the same titles were fetched again
    beside them.

    REKEYED RATHER THAN DELETED. The payloads are perfectly good answers about
    titles whose content never depended on the credential, and they are exactly
    what the modal falls back to when a source cannot be reached. Deleting them
    would throw away the thing the change was made to make reachable.

    THE SHAPE IS RESTATED HERE, NOT IMPORTED, and that is deliberate rather than
    duplication: a migration transforms the keys as they were WRITTEN at a
    particular time into the shape they had to become at that time. If `cache_key`
    changes again, this must go on doing what it does now — following it would
    make an already-applied migration mean something different. (The kernel also
    may not import a provider package; that rule points the same way.)

    UPDATE OR REPLACE, because `cache_key` is the primary key: an instance that
    had rotated its client id can hold two rows for one question, and they
    collapse onto the one address. Keeping the later-written one is right — both
    are answers to the same question and neither is more this instance's than the
    other.
    """
    from urllib.parse import parse_qsl, urlencode

    rows = conn.execute(
        "SELECT cache_key FROM api_cache WHERE cache_key LIKE ?",
        ("https://api.simkl.com/%client_id=%",),
    ).fetchall()
    for row in rows:
        old = row[0]
        base, _, query = old.partition("?")
        # Sorted, which is what `cache_key` does: with the credential no longer
        # appended last there is nothing else making the order canonical, and two
        # spellings of one question must not become two rows.
        kept = sorted((name, value) for name, value in parse_qsl(query, keep_blank_values=True)
                      if name != "client_id")
        new = f"{base}?{urlencode(kept)}"
        if new != old:
            conn.execute("UPDATE OR REPLACE api_cache SET cache_key = ? WHERE cache_key = ?",
                         (new, old))


MIGRATION_29 = """
-- GIVE EVERY STORED RECORD THE SERVICE IDS ITS WATCH STATE ALREADY KNOWS.
--
-- A baseline that matches a roster title against another service's library gets
-- that service's own id back on the matched entry and keeps it on the watch
-- state. Source selection reads the RECORD, so a row could report counts from
-- both services -- proof the second one holds history for it -- and still say
-- the first was not configured the moment that credential went away. The live
-- pass writes the id onto the record now, but it only reaches what it renders:
-- the open month's premieres and the viewer's own list. It never sees a SETTLED
-- verdict (deliberately kept out, so a verdict keeps the counts it was reached
-- on) or anything in a FROZEN month (which renders from its snapshot and runs no
-- live pass at all). Those records would keep their gap for ever.
--
-- ADD-ONLY, WHICH IS THE SAME RULE store.learn_ids APPLIES. Only a column that
-- is NULL is filled: a stored id is what every per-title path has been calling
-- with and normally came off that service's own payload, while the one arriving
-- here was matched across services on the shared identity -- a join, not a
-- statement, and one service can list a series as several titles that resolve to
-- one tracker key.
--
-- THE IDENTITY IS UNTOUCHED. media/match_source/match_id are the WHERE clause
-- and never the SET, so a record learning an id stays filed exactly where it
-- was. MATCH_SOURCES excludes `simkl` for precisely this reason, so gaining one
-- cannot move a row.
--
-- MIN() RATHER THAN A BARE SUBQUERY because there is one progress row per
-- (season, source) and any of them can carry the id. They agree in every case
-- measured -- the ids describe the TITLE, not the season -- but an unordered
-- pick from several rows is a result that could differ between two runs of the
-- same migration, which is not a property a migration may have.
UPDATE distrakt_month_records AS r
   SET simkl_id = (SELECT MIN(p.simkl_id) FROM distrakt_show_progress p
                    WHERE p.user_id = r.user_id AND p.media = r.media
                      AND p.match_source = r.match_source AND p.match_id = r.match_id
                      AND p.simkl_id IS NOT NULL)
 WHERE r.simkl_id IS NULL
   AND EXISTS (SELECT 1 FROM distrakt_show_progress p
                WHERE p.user_id = r.user_id AND p.media = r.media
                  AND p.match_source = r.match_source AND p.match_id = r.match_id
                  AND p.simkl_id IS NOT NULL);

UPDATE distrakt_month_records AS r
   SET trakt_id = (SELECT MIN(p.trakt_id) FROM distrakt_show_progress p
                    WHERE p.user_id = r.user_id AND p.media = r.media
                      AND p.match_source = r.match_source AND p.match_id = r.match_id
                      AND p.trakt_id IS NOT NULL)
 WHERE r.trakt_id IS NULL
   AND EXISTS (SELECT 1 FROM distrakt_show_progress p
                WHERE p.user_id = r.user_id AND p.media = r.media
                  AND p.match_source = r.match_source AND p.match_id = r.match_id
                  AND p.trakt_id IS NOT NULL);

UPDATE distrakt_user_seasons AS r
   SET simkl_id = (SELECT MIN(p.simkl_id) FROM distrakt_show_progress p
                    WHERE p.user_id = r.user_id AND p.media = r.media
                      AND p.match_source = r.match_source AND p.match_id = r.match_id
                      AND p.simkl_id IS NOT NULL)
 WHERE r.simkl_id IS NULL
   AND EXISTS (SELECT 1 FROM distrakt_show_progress p
                WHERE p.user_id = r.user_id AND p.media = r.media
                  AND p.match_source = r.match_source AND p.match_id = r.match_id
                  AND p.simkl_id IS NOT NULL);

UPDATE distrakt_user_seasons AS r
   SET trakt_id = (SELECT MIN(p.trakt_id) FROM distrakt_show_progress p
                    WHERE p.user_id = r.user_id AND p.media = r.media
                      AND p.match_source = r.match_source AND p.match_id = r.match_id
                      AND p.trakt_id IS NOT NULL)
 WHERE r.trakt_id IS NULL
   AND EXISTS (SELECT 1 FROM distrakt_show_progress p
                WHERE p.user_id = r.user_id AND p.media = r.media
                  AND p.match_source = r.match_source AND p.match_id = r.match_id
                  AND p.trakt_id IS NOT NULL);
"""


MIGRATION_30 = """
-- WHICH LINKED TRACKER DECIDES, WHEN MORE THAN ONE ANSWERS FOR A SEASON.
--
-- Two services can report different counts for one season and both be right,
-- and one number has to be picked: it is what the bucket rule reads to decide a
-- season is finished, and it is what a frozen month and an announcement post
-- carry for ever. That pick has always been the REGISTRY's declared order --
-- app-wide, identical for everybody, and not a thing an account could state.
--
-- WHY THAT NEEDED TO BECOME A PREFERENCE. The registry order is a fact about
-- what this app supports, not about whose viewing an account trusts. Somebody
-- migrating between services has the order backwards and cannot say so; worse,
-- the registry order does not follow a LINK, so a service unlinked long ago
-- goes on deciding from whatever number it last left behind, and a season
-- finished at the service the viewer actually uses can never complete.
--
-- A LIST OF SOURCE NAMES, MOST TRUSTED FIRST, AND IT IS A REORDERING RATHER
-- THAN A SELECTION -- the same shape and the same rule as precedence_json's
-- field order beside it. A source this account does not name still answers when
-- it is the only one that can; a name this version does not recognise falls out
-- on the way past. So an empty list is the honest default for an account that
-- has said nothing, and it reads as "use the declared order", which is exactly
-- what every account got before this column existed.
--
-- WHAT IT DELIBERATELY DOES NOT REACH: a month already frozen. Those numbers
-- are the answer to "what did that month decide", not a live claim, and a
-- preference changed today must not re-answer an earlier year's record.
ALTER TABLE source_prefs ADD COLUMN tracker_order_json TEXT NOT NULL DEFAULT '[]';
"""


MIGRATION_31 = """
-- A SLUG BELONGS TO ONE SERVICE, AND THE COLUMN THAT HELD IT DID NOT SAY WHICH.
--
-- Trakt and Simkl both call a title's readable name `slug` and do not agree on
-- it: Trakt writes `the-traitors-2023` where Simkl writes `the-traitors`. A
-- roster row knows a title by BOTH services, so both wrote into the one `slug`
-- column and whichever synced last won. Every link built from it was then wrong
-- for the other service — the tracker's episode ticks open `app.trakt.tv/shows/
-- {slug}`, which lands nowhere when the value came from Simkl, and the same
-- would have been true in reverse the moment Simkl's link used it.
--
-- ADD-ONLY, AND THE BACKFILL REFUSES TO GUESS. A row only ONE service knows must
-- have taken its slug from that service, which is provable and is what these two
-- statements copy. A row BOTH services know is genuinely ambiguous — the value
-- is whichever wrote last and nothing recorded which — so it is left NULL and
-- re-learned from the next sync, which writes the namespaced key. Guessing there
-- would bake in the exact ambiguity this migration exists to remove, and a
-- confidently wrong slug is what the bug already was.
--
-- `slug` IS NOT DROPPED. It is the only value the ambiguous rows have until a
-- sync fills the new ones in, and readers fall back to it — so dropping it would
-- break links this migration is meant to fix, to reclaim one column.
ALTER TABLE distrakt_user_seasons  ADD COLUMN trakt_slug TEXT;
ALTER TABLE distrakt_user_seasons  ADD COLUMN simkl_slug TEXT;
ALTER TABLE distrakt_month_records ADD COLUMN trakt_slug TEXT;
ALTER TABLE distrakt_month_records ADD COLUMN simkl_slug TEXT;

-- THE HELD-ROWS TABLE CARRIES THE SAME IDS AND MUST GROW WITH THEM. Its reader
-- (unsettled._record) builds a record by walking store.ID_COLUMNS and taking each
-- named column off the row, so a column named there and missing here is not a
-- missing slug — it is an IndexError on every held row.
ALTER TABLE distrakt_unsettled_rows ADD COLUMN trakt_slug TEXT;
ALTER TABLE distrakt_unsettled_rows ADD COLUMN simkl_slug TEXT;

UPDATE distrakt_unsettled_rows
   SET trakt_slug = slug
 WHERE slug IS NOT NULL AND slug <> '' AND simkl_id IS NULL;
UPDATE distrakt_unsettled_rows
   SET simkl_slug = slug
 WHERE slug IS NOT NULL AND slug <> '' AND trakt_id IS NULL AND simkl_id IS NOT NULL;

UPDATE distrakt_user_seasons
   SET trakt_slug = slug
 WHERE slug IS NOT NULL AND slug <> '' AND simkl_id IS NULL;
UPDATE distrakt_user_seasons
   SET simkl_slug = slug
 WHERE slug IS NOT NULL AND slug <> '' AND trakt_id IS NULL AND simkl_id IS NOT NULL;

UPDATE distrakt_month_records
   SET trakt_slug = slug
 WHERE slug IS NOT NULL AND slug <> '' AND simkl_id IS NULL;
UPDATE distrakt_month_records
   SET simkl_slug = slug
 WHERE slug IS NOT NULL AND slug <> '' AND trakt_id IS NULL AND simkl_id IS NOT NULL;
"""


MIGRATION_32 = """
-- A SERVICE STOPPED LISTING A TITLE, WHICH IS NOT THE SAME AS THE VIEWER
-- FINISHING WITH IT.
--
-- Simkl documents that `date_from` deltas never surface removals, and prescribes
-- detecting them by diffing an ids-only re-read against what is held locally.
-- What it prescribes DOING about the difference is deleting the local rows. This
-- app records it instead, and the reason is that the two failure modes are not
-- symmetrical: watch history is not re-derivable from anything this app holds, so
-- a wrong deletion is permanent and invisible, while a wrong mark is visible and
-- costs nothing to undo. A sync hiccup, a re-catalogued title, a bucket that
-- failed in a way the partial-read logic did not catch — any of those can produce
-- an absence, and none of them is worth a viewer's history.
--
-- PER SOURCE, WHICH IS WHY IT IS A LIST AND NOT A FLAG. A title dropped at Simkl
-- may still be held at Trakt, and a row that said only "missing" could not say
-- whose statement that was — the same ambiguity the shared `slug` column was
-- split apart to remove. An empty list is the default and means every linked
-- service still lists it.
--
-- ON THE USER RECORD RATHER THAN THE MONTH, AND THE LIFECYCLE IS THE ARGUMENT.
-- distrakt_month_records FREEZES: a month closing while a title was missing would
-- say so for ever, which reintroduces exactly the irreversibility this exists to
-- avoid. This table is the viewer's living list — recomputed every load, rolled
-- forward month to month the way keepup and catchup already are — so the mark
-- travels with the row until it clears or the viewer purges it.
--
-- IT CLEARS ITSELF. A source naming the title again removes that source from the
-- list, with no acknowledgement needed; `came_back` beside it works the other way
-- (cleared only by the viewer) because it remembers something no later read can
-- restate. This one is a claim about what a service currently holds, so the
-- service's next answer is exactly what should overwrite it.
ALTER TABLE distrakt_user_seasons ADD COLUMN missing_sources_json TEXT NOT NULL DEFAULT '[]';
"""


MIGRATION_33 = """
-- "I HAVE MOVED OFF THAT SERVICE — STOP COUNTING WHAT IT LEFT BEHIND."
--
-- Unlinking a service stops it being ASKED, and that much already worked. What it
-- could not do is stop the numbers it already contributed from counting: those
-- live in the watch state, they are still per-source, and every row that ever had
-- one goes on rendering it. The row says so honestly -- `counts_freshness` reads
-- `partial`, meaning "a number here belongs to a service nobody asked, and no
-- refresh will move it" -- but that is a state with NO EXIT. An account that has
-- genuinely migrated reads as permanently degraded rather than as a healthy
-- single-service account.
--
-- A LIST OF SERVICE NAMES WHOSE STORED NUMBERS THIS ACCOUNT NO LONGER COUNTS,
-- beside tracker_order_json and shaped the same way: names this version does not
-- recognise fall out on the way past, and an empty list is the honest default
-- meaning "count everything", which is what every account had before this column.
--
-- IGNORED, NEVER DELETED. The numbers stay exactly where they are and the row
-- still shows them, marked as retired -- because deleting them would throw away
-- the only record of a service's contribution to settle a display question, and
-- because un-retiring has to be able to put things back. It is the same stance
-- the removal marks take one migration earlier: record the decision, do not act
-- destructively on it.
--
-- IT DOES NOT REACH A FROZEN MONTH, and that is the boundary this must not cross.
-- A settled month's `watched_by_source` is not a cache and not a live claim: it
-- is what that month RECORDED, and it will never be recomputed. Retiring a source
-- today must not silently re-answer what an earlier month decided -- exactly the
-- rule tracker_order_json already follows for the same reason.
ALTER TABLE source_prefs ADD COLUMN tracker_retired_json TEXT NOT NULL DEFAULT '[]';
"""


MIGRATION_34 = """
-- A QUESTION THE VIEWER HAS NOT ANSWERED YET OUTLIVES THE REQUEST THAT ASKED IT.
--
-- The untracked-season prompts are derived from what the INCREMENTAL history sync
-- just folded in (lifecycle.reconcile_history returns nothing at all for an empty
-- play list), and that pull happens once: the activity beacon moves, and the very
-- next request reports no new plays. Every prompt on the page therefore vanished
-- from the next payload built -- and each of the three answer routes ends by
-- rebuilding the month and returning it. So answering ONE question silently threw
-- away every other question beside it, with only the answered one recorded. A
-- plain reload did the same thing, for the same reason, with nothing answered.
--
-- HOLDING THE PLAY IS WHAT FIXES IT, NOT HOLDING THE QUESTION. What is stored here
-- is the raw play -- the identity, the episode, and the ids the history event
-- spelled out -- and whether it is still worth asking about is re-decided on every
-- read by the rules that already decide it. A season that has since been placed,
-- settled or declined drops out by those rules rather than by a second copy of
-- them going stale in a queue. It is the same division distrakt_prompt_dismissals
-- already draws: persist the FACT, re-derive the JUDGEMENT.
--
-- KEYED BY SEASON, like the dismissals table beside it and for the same reason:
-- reconcile_history reduces a sitting of nine episodes to one question, so a
-- second episode of a season already asked about must not add a second row.
--
-- `ids` TRAVELS AS JSON because saying yes means LOOKING THE SEASON UP, and a
-- lookup needs the id of the service being asked. The play is the only place
-- those ids ever existed -- re-deriving them would mean fetching a whole history
-- again to find them a second time.
CREATE TABLE distrakt_open_prompts (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    media        TEXT    NOT NULL,
    match_source TEXT    NOT NULL,
    match_id     TEXT    NOT NULL,
    season       INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    title        TEXT    NOT NULL DEFAULT '',
    ids_json     TEXT    NOT NULL DEFAULT '{}',
    -- WHEN THE EPISODE WAS WATCHED, as the service reported it (UTC, like every
    -- other watched_at in this app). It is what tells a play the viewer has
    -- already declined from one they have not: a refusal is a watermark, so the
    -- question is only re-raised by a play LATER than it.
    watched_at   TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, media, match_source, match_id, season)
);

-- AND WHAT A REFUSAL MEANS, WHICH IS NO LONGER ONE THING.
--
-- distrakt_prompt_dismissals was written when all three of the page's questions
-- agreed exactly about NO: do not put this season back on my list, stop asking.
-- They no longer do, because they are driven by different things.
--
--   A HISTORY QUESTION IS AN EVENT. An episode was watched. Saying no settles
--   THAT viewing -- but watching another episode afterwards is fresh evidence
--   that the viewer is following the show after all, and it should ask again.
--   So a history refusal is a WATERMARK: a play later than the refusal reopens
--   the question, while the same play re-reported does not. That distinction is
--   only decidable because a forced refresh re-reads from the first of the month
--   and would otherwise hand back evidence somebody has already answered.
--
--   AN UNBACKED VERDICT IS A STANDING CONDITION. The record asserts a completion
--   no service backs any more, and that stays true on every load until something
--   changes it. A refusal there has to be permanent or the question returns for
--   ever, which is what it was always for.
--
-- Sharing one row made the first silence the second: declining a history prompt
-- permanently suppressed a DIFFERENT question about the same season's verdict --
-- one nobody had refused.
--
-- 'verdict' IS THE DEFAULT SO EXISTING ROWS KEEP THE MEANING THEY WERE WRITTEN
-- WITH. Every row predating this column was recorded as "stop asking, full
-- stop", and defaulting the other way would quietly turn each of them into a
-- watermark and start re-asking about seasons somebody has already declined.
ALTER TABLE distrakt_prompt_dismissals
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'verdict';
"""

MIGRATION_35 = """
-- THE CALENDAR STOPS BEING A PILE OF WINDOWS AND BECOMES ROWS.
--
-- What it replaces: one compressed blob per (endpoint, 7 days) in api_cache,
-- holding every source's records for that span. Reading a month inflated four or
-- five of them and filtered the result; asking "which titles does the stored
-- calendar name" inflated EVERY window an instance held, which is why the drain
-- spent hundreds of milliseconds of event-loop time before it made a request.
-- Neither question is one a blob can answer, and both are ordinary SQL over rows.
--
-- THREE LEVELS, BECAUSE THE FACTS HAVE THREE DIFFERENT LIFETIMES. Measured
-- across 4,330 (source, title) pairs holding more than one airing: runtime
-- varies on 0.1% of them, certification 0.2%, genres 0.4%, network 0.8%, status
-- 0.8%, language and country 0.0%. Those are TITLE facts, and storing them once
-- per airing is what let one title's airings disagree with each other -- not
-- because a source said anything different, but because two airings were fetched
-- a week apart. episode_title varies 95.5%, which is the opposite finding and
-- the reason level 2 exists at all. Level 3 is the airing itself, kept separate
-- from level 2 because one episode can air more than once and collapsing them
-- would silently drop a repeat the calendar has always drawn.
--
-- PER SOURCE AT EVERY LEVEL. Two services describing one title are two rows and
-- neither overwrites the other; which one a given viewer sees is decided at READ
-- against their own precedence, exactly as the window model decided it. Storing
-- a merged answer would bake one viewer's preference into shared storage, which
-- is the invariant this whole design exists to keep.

-- LEVEL 1 -- what a source says about a TITLE.
CREATE TABLE calendar_titles (
    -- 'trakt' | 'simkl' | 'tmdb'. An open set of names rather than a CHECK,
    -- following show_posters.source: adding a provider is not a migration.
    source        TEXT    NOT NULL,
    media         TEXT    NOT NULL,
    -- The SOURCE's own id for this title (Record.id), never the shared key: it
    -- is what a refetch has to be addressed by.
    source_id     TEXT    NOT NULL,
    -- The CROSS-SOURCE identity (app/calendar/cache.py's group_base -- the
    -- waterfall in providers/base.py's resolve_key, stringified). Denormalized
    -- onto every level so grouping two services' rows into one card is a join on
    -- one column rather than a per-row id-waterfall in Python. A title the
    -- waterfall cannot key gets a per-source value here and so can never merge,
    -- which is deliberate: a visible duplicate is safer than a wrong merge.
    title_key     TEXT    NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    -- CASE-FOLDED AND ACCENT-STRIPPED, for search. Its own column rather than a
    -- function index because the folding rule lives in Python and must be the
    -- same one the query folds with; a stored column keeps one implementation.
    title_fold    TEXT    NOT NULL DEFAULT '',
    ids_json      TEXT    NOT NULL DEFAULT '{}',
    detail_url    TEXT    NOT NULL DEFAULT '',
    year          TEXT    NOT NULL DEFAULT '',
    network       TEXT    NOT NULL DEFAULT '',
    country       TEXT    NOT NULL DEFAULT '',
    language      TEXT    NOT NULL DEFAULT '',
    certification TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT '',
    overview      TEXT    NOT NULL DEFAULT '',
    poster        TEXT    NOT NULL DEFAULT '',
    runtime       INTEGER,
    -- The source's RAW SLUGS, lowercase and hyphenated ("game-show"), never the
    -- title-cased display form -- the per-viewer filter matches on the slug, and
    -- a stored "Game Show" breaks every multi-word genre filter while leaving
    -- single-word ones working. The title-casing happens in render(), on the far
    -- side of the filter.
    genres_json   TEXT    NOT NULL DEFAULT '[]',
    -- {service: score}, NOT one number. Record.rating is "one number shown under
    -- one service's mark", and the card draws two services' ratings side by side
    -- without ever averaging them; a map keeps that true while giving IMDb's
    -- score -- which Simkl hands over and this app has never kept -- somewhere to
    -- live that is not the field labelled with somebody else's name. A map rather
    -- than a column per service for the same reason `source` has no CHECK.
    ratings_json  TEXT    NOT NULL DEFAULT '{}',
    -- Simkl's own "is this actually a film" answer; only 'movie' means a film
    -- masquerading on a series endpoint. Empty for every source that never sets
    -- it and for the serial formats that must NOT be pruned.
    anime_type    TEXT    NOT NULL DEFAULT '',
    -- {country: [release type]} in TMDB's numbering. Distribution, not origin --
    -- deliberately not a plural `country`, which is where a title was MADE.
    release_types_json TEXT NOT NULL DEFAULT '{}',
    -- WHETHER THE FIELDS ABOVE ARE ANSWERS OR JUST DEFAULTS. Simkl's calendar
    -- files carry none of them, so its rows land 0 and are filled by the drain.
    -- The filter reads this to tell "nothing to say" from "nobody has looked
    -- yet", and exempts the second rather than judging it on values it cannot
    -- answer for. Storing it makes that distinction survive a restart, which the
    -- read-time overlay it replaces could only recompute.
    enriched      INTEGER NOT NULL DEFAULT 0,
    fetched_at    INTEGER NOT NULL,
    -- WHEN THIS ROW GOES STALE, as an instant rather than a policy. The tier
    -- (current month 24h, previous 7d, older than six months 30d, older than a
    -- year 90d) is decided once by the writer; storing the RESULT means "what is
    -- due a refresh" is an index range scan instead of a rule re-evaluated per
    -- row in Python.
    stale_after   INTEGER NOT NULL DEFAULT 0,
    failed_at     INTEGER,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, media, source_id)
);
-- Search (a prefix/substring match over folded titles).
CREATE INDEX ix_calendar_titles_fold ON calendar_titles(title_fold);
-- Grouping a card: every source's row for one shared identity.
CREATE INDEX ix_calendar_titles_key ON calendar_titles(title_key);
-- The drain's owed set and the refresh sweep, which is the whole reason the
-- old "inflate every window to find out" disappears.
CREATE INDEX ix_calendar_titles_due ON calendar_titles(enriched, stale_after);
-- Retention (a flat six months from last store -- see calendar_source_files).
CREATE INDEX ix_calendar_titles_fetched ON calendar_titles(fetched_at);

-- LEVEL 2 -- what a source says about ONE EPISODE. Genuinely varies per episode
-- (episode_title 95.5%), and this app has never held it: the modal shows a
-- season's worth of episode facts today by stamping the SHOW's runtime and
-- rating onto each one. Two of three sources can fill this and one can half-fill
-- it (Simkl publishes no per-episode runtime or rating), so a MISSING ROW IS THE
-- ORDINARY CASE and never an error.
CREATE TABLE calendar_episodes (
    source        TEXT    NOT NULL,
    media         TEXT    NOT NULL,
    source_id     TEXT    NOT NULL,
    season        INTEGER NOT NULL,
    number        INTEGER NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    overview      TEXT    NOT NULL DEFAULT '',
    still         TEXT    NOT NULL DEFAULT '',
    first_aired   TEXT    NOT NULL DEFAULT '',
    episode_type  TEXT    NOT NULL DEFAULT '',
    runtime       INTEGER,
    rating        REAL,
    votes         INTEGER,
    fetched_at    INTEGER NOT NULL,
    stale_after   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, media, source_id, season, number)
);
CREATE INDEX ix_calendar_episodes_due ON calendar_episodes(stale_after);
CREATE INDEX ix_calendar_episodes_fetched ON calendar_episodes(fetched_at);

-- LEVEL 3 -- one AIRING, as one source listed it on one endpoint.
CREATE TABLE calendar_airings (
    source          TEXT    NOT NULL,
    media           TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    -- Which app endpoint this appeared on. Several endpoints are different
    -- DERIVATIONS over the same source file, so the same airing legitimately
    -- exists on more than one and each is its own row.
    endpoint        TEXT    NOT NULL,
    title_key       TEXT    NOT NULL,
    -- POSIX seconds. The sort key, and the only time fact a source must supply.
    air_ts          REAL    NOT NULL,
    -- The UTC date of air_ts, stored so a month or day is a range scan on an
    -- index rather than arithmetic over every row. A VIEWER's local date is
    -- still derived at read -- this is the shared, absolute one.
    air_date        TEXT    NOT NULL,
    -- WHETHER THAT INSTANT IS REALLY AN INSTANT. A film released on the 6th is
    -- released on the 6th wherever you are; rendering a UTC-midnight timestamp
    -- in a viewer's timezone moves a UTC-8 viewer's release to the day before.
    date_only       INTEGER NOT NULL DEFAULT 0,
    -- -1 RATHER THAN NULL, and that is load-bearing: SQLite permits NULLs in a
    -- non-INTEGER PRIMARY KEY, so a nullable coordinate here would let the same
    -- airing insert twice over. A third of premiere entries state no season or
    -- no episode number -- overwhelmingly Simkl anime, where an absolute episode
    -- number is simply how the entry is spelled -- so "unstated" is the ordinary
    -- case and needs a value the key can actually compare.
    season          INTEGER NOT NULL DEFAULT -1,
    episode_number  INTEGER NOT NULL DEFAULT -1,
    -- HOW THIS AIRING SPELLS ITS EPISODE ("S01E02", or an absolute number for an
    -- anime entry that has no season). Genuinely an AIRING fact and the reason
    -- there is no episode TITLE beside it: the coordinate above already keys the
    -- calendar_episodes row, so a repeat airing points at the same episode for
    -- free, and a title stored here would be a second home for a level-2 fact.
    -- MEASURED, 45,827 stored source-records: 1,577 coordinates air more than
    -- once and 14 of them DISAGREED with themselves -- {'Once in a Blue Moon': 2,
    -- 'Episode 1': 1} for one show's S01E01 -- because two airings were fetched
    -- on different days and one caught Trakt's placeholder. One row per episode
    -- cannot produce that. Nothing is stranded by the move: of the 7,387 records
    -- stating no coordinate, ZERO carried an episode title.
    episode_label   TEXT    NOT NULL DEFAULT '',
    stored_at       INTEGER NOT NULL,
    PRIMARY KEY (source, media, source_id, endpoint, air_ts, season, episode_number)
);
-- The month read, which is the hot path.
CREATE INDEX ix_calendar_airings_month ON calendar_airings(endpoint, air_date);
-- A REFRESH DELETES BY (source, endpoint, span) AND RE-INSERTS, rather than
-- upserting row by row. It has to: a title the source has STOPPED listing leaves
-- no row to upsert, and an upsert-only refresh would keep drawing it for ever.
-- The window model got this free by replacing a whole blob; rows have to say it.
CREATE INDEX ix_calendar_airings_refresh ON calendar_airings(source, endpoint, air_date);
-- Grouping, and the tracker's name resolution.
CREATE INDEX ix_calendar_airings_key ON calendar_airings(title_key);
CREATE INDEX ix_calendar_airings_stored ON calendar_airings(stored_at);

-- THE VALIDATORS, AND THE REFRESH CLOCK -- one row per source file per month.
--
-- IT HOLDS NO BODY, AND THAT IS THE POINT. The old rows kept the whole decoded
-- file beside its ETag, because a 304 carries no body and the window that needed
-- it might have been evicted independently. Under entries a 304 needs no body at
-- all: "unchanged" means the rows already derived from it are still correct, so
-- the answer is to do nothing. Measured on the live CDN: prod held 2.57 MB
-- compressed for 15.15 MB of JSON, and the validators alone are about a
-- kilobyte.
--
-- WORTH KEEPING BECAUSE THE ARCHIVE IS TIERED, measured 2026-08-28 against
-- data.simkl.in: the current month and the two ahead of it had all been
-- regenerated 1.3 hours earlier and share one ETag prefix, while 2026-06 and
-- 2025-08 were both 38.9 days old and likewise share one -- past months are
-- frozen. So a conditional GET on an old month is a 304 essentially always, and
-- those are the big files (2025/8/tv.json is 4 MB). Eighteen of eighteen probes
-- answered 304 with a zero-byte body.
CREATE TABLE calendar_source_files (
    url           TEXT    PRIMARY KEY,
    source        TEXT    NOT NULL,
    -- The calendar month this file covers, 'YYYY-MM'. The unit the refresh
    -- schedule works in, because the month is the display unit: a rolling
    -- 14-or-42-day window would refresh half of what a viewer is looking at.
    month         TEXT    NOT NULL,
    etag          TEXT    NOT NULL DEFAULT '',
    last_modified TEXT    NOT NULL DEFAULT '',
    -- When it was last ASKED ABOUT, which a 304 advances; and when its content
    -- last actually MOVED, which only a 200 does. Two fields because they answer
    -- different questions -- "is this due a check" and "how stale can the rows
    -- derived from it be" -- and one number cannot mean both.
    fetched_at    INTEGER NOT NULL,
    changed_at    INTEGER NOT NULL DEFAULT 0,
    stale_after   INTEGER NOT NULL DEFAULT 0,
    entries       INTEGER NOT NULL DEFAULT 0,
    failed_at     INTEGER,
    fail_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_calendar_source_files_due ON calendar_source_files(stale_after);

-- WHO WAS ASKED, AND WHO ANSWERED -- one row per (endpoint, span, source).
--
-- THE TWO ARE DIFFERENT FACTS AND CONFLATING THEM IS WHAT MADE THE "incomplete
-- data" BANNER PERMANENT. A source that was asked and could not answer is the
-- only thing "partial" should ever mean. A source in neither column was not in
-- play when this span was filled -- it did not exist on the instance yet, or its
-- declared reach does not cover this span -- and that is a MISS to refill, not a
-- failure to report. Storing only "who answered" left the reader measuring
-- today's sources against a span filled weeks ago, so every span read as partial
-- the moment a source was added, and one outside a source's reach read as
-- partial for ever because refilling it changed nothing.
--
-- IT SURVIVES THE MOVE FROM BLOBS BECAUSE IT HAS TO. The window envelope carried
-- both lists; rows have nowhere to put them, and deriving coverage from "are
-- there any airings from this source" is exactly the wrong answer -- a source
-- that legitimately lists nothing in a span is indistinguishable from one that
-- was never asked, which is the confusion this table exists to end.
--
-- SPAN RATHER THAN MONTH, because the span is what a fill actually covers: the
-- aligned seven-day window the read path already stitches months out of. The
-- refresh schedule works in months by enumerating the spans inside one.
CREATE TABLE calendar_coverage (
    endpoint    TEXT    NOT NULL,
    -- The aligned window start, 'YYYY-MM-DD'.
    span_start  TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    -- Asked is written on every attempt; answered only when the source actually
    -- returned records rather than raising.
    asked       INTEGER NOT NULL DEFAULT 1,
    answered    INTEGER NOT NULL DEFAULT 0,
    stored_at   INTEGER NOT NULL,
    stale_after INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (endpoint, span_start, source)
);
-- Reading a month asks for its spans at once.
CREATE INDEX ix_calendar_coverage_span ON calendar_coverage(endpoint, span_start);
-- The refresh sweep, and retention.
CREATE INDEX ix_calendar_coverage_due ON calendar_coverage(stale_after);
CREATE INDEX ix_calendar_coverage_stored ON calendar_coverage(stored_at);

-- THE CUTOVER IS A DROP AND A REFILL, AND THE DROP IS EXPLICIT.
--
-- Not left to the TTL sweep, which would leave the dead blobs sitting until the
-- ninety-day grace expired -- 5.29 MB of them on the machine this was written
-- on. Both prefixes go: 'calendar:' is the window blobs this table replaces, and
-- 'simkl-cdn:' is the decoded file bodies the row above deliberately stops
-- keeping. Nothing else in api_cache is touched; the per-title detail lookups
-- ('https:') are a different cache with a different reason to exist.
DELETE FROM api_cache WHERE cache_key LIKE 'calendar:%' OR cache_key LIKE 'simkl-cdn:%';

-- WHERE A VIEWER'S HISTORY STARTS FOR THIS SEASON, so a re-watch can be told
-- from the tracker learning about an old completion for the first time.
--
-- A WATERMARK RATHER THAN A SECOND SET OF EPISODES, which is what makes this one
-- column instead of a table. distrakt_show_progress has held {episode:
-- watched_at} since migration 17, and the dates in it are the service's
-- last_watched_at -- so an episode watched again carries the NEW date and one
-- left behind in the old pass keeps the old. Filtering that map by a start date
-- is therefore already the whole of "progress through the current pass", and a
-- second stored pass would be a copy of something derivable.
--
-- EMPTY MEANS "COUNT EVERYTHING", so every existing row keeps exactly the
-- behaviour it was written with.
ALTER TABLE distrakt_user_seasons ADD COLUMN history_from TEXT NOT NULL DEFAULT '';
"""


# ---------------------------------------------------------------------------
# 36 — the source-preference screen's questions, reduced to the two people ask
# ---------------------------------------------------------------------------
#
# THREE PREFERENCES GO AND ONE ARRIVES. What is dropped was answerable only on a
# screen of its own, which is now gone:
#
#   - `precedence_json` held a per-FIELD map: this service for the overview,
#     that one for the poster. Eleven questions where the one anybody asks is
#     "prefer this service". Its `default` entry IS that question, so it is
#     carried across rather than discarded.
#   - `tracker_source` asked which services the tracker read, ALONGSIDE what the
#     account had linked. Reading somebody's history needs their token, so
#     linking one is already the statement; the column could only agree with the
#     links or contradict them.
#   - `endpoint_sources_json` restated the calendar choice once per calendar. It
#     existed for the movie firehose, which the filters panel now answers on the
#     axis that actually makes a movie calendar readable — release country and
#     type — for every service at once.
#
# THE CARRY-ACROSS IS WHY THIS IS NOT A BARE DROP. `precedence_json`'s `default`
# is a real preference somebody may have stated, and it means exactly what the
# new column means. A bare string is a one-element order, which is how that
# document was written before an order was possible.
MIGRATION_36 = """
ALTER TABLE source_prefs ADD COLUMN metadata_order_json TEXT NOT NULL DEFAULT '[]';

UPDATE source_prefs
   SET metadata_order_json = CASE
       WHEN json_valid(precedence_json)
            AND json_type(precedence_json, '$.default') = 'array'
            THEN json_extract(precedence_json, '$.default')
       WHEN json_valid(precedence_json)
            AND json_type(precedence_json, '$.default') = 'text'
            THEN json_array(json_extract(precedence_json, '$.default'))
       ELSE '[]'
   END;

ALTER TABLE source_prefs DROP COLUMN precedence_json;
ALTER TABLE source_prefs DROP COLUMN tracker_source;
ALTER TABLE source_prefs DROP COLUMN endpoint_sources_json;
"""

# AN AIRING NO CALENDAR FEED LISTED, AND WHY IT NEEDS SAYING SO.
#
# A service can describe a show perfectly and leave it off its own calendar --
# measured: Trakt dates Half Man's first season to 2026-04-28T20:00Z and its
# premieres calendar for that week does not carry the title at all. The calendar
# search writes such an airing when a viewer follows the result, which is the
# only way this app can show a premiere both services have missed.
#
# WITHOUT THIS FLAG THAT REPAIR LASTS UNTIL THE WINDOW REFETCHES. A fill REPLACES
# what a source holds for a span -- delete, then insert what came back -- because
# a title a source has stopped listing must stop being drawn. A repaired row is
# in the delete's path and not in the insert's, so it lasted days rather than
# indefinitely, and the viewer would have had to search for it again with nothing
# telling them why it went.
#
# IT CLEARS ITSELF, WHICH IS THE PART THAT MAKES IT SAFE. A feed row for the same
# airing has the same natural key and is written with `from_search = 0`, so the
# moment a service starts listing the title its own answer takes the row back and
# the exemption ends. Retention still reclaims these like any other airing, so a
# title genuinely dropped by everybody ages out rather than living for ever.
MIGRATION_37 = """
ALTER TABLE calendar_airings ADD COLUMN from_search INTEGER NOT NULL DEFAULT 0;
"""

# ONE CALENDAR'S FILTERS STOP BEING THE OTHER'S.
#
# `genres` and `countries` were one answer applied to whichever calendar was
# open, which made them unanswerable: somebody who never wants a reality SHOW
# had no way to say so without also losing documentary films, and the two
# questions had genuinely different answers. Certifications were already split
# (a TV rating and an MPA rating are not the same vocabulary) and the release
# pair was always films-only, so this finishes a split the schema had already
# started rather than inventing one.
#
# TV KEEPS TODAY'S VALUES AND THE FILM COLUMNS START EMPTY, which is a real
# behaviour change and the reason this note is long. An account that had filtered
# genres or countries sees a WIDER film calendar the first time it loads one
# after this runs. The alternative -- copying both ways -- keeps every calendar
# looking the same on upgrade but silently asserts that a filter written for
# shows was meant for films too, and the whole reason for the split is that it
# usually was not. The changelog says so in as many words; a person who wanted
# the old narrowing on films can restate it in one press per chip.
#
# WHICH COLUMN IS WHICH IS NOT INFERRED FROM ITS NAME AT READ TIME.
# app/calendar/vocab.py's TV_FIELDS/MOVIE_FIELDS is what maps a medium to its
# columns, so a sixth endpoint or a third medium changes that tuple and not a
# string test somewhere in the read path.
#
# `filters_paused` IS THE WHOLE OF THE STASH. Nothing is copied aside or
# restored: the specs stay exactly where they are and every read path asks this
# one boolean before applying them, so turning filters back on cannot lose an
# answer. It is per account rather than per session because the question people
# actually ask is "show me everything for a bit", and a switch that silently
# reset itself at the next sign-in would answer a different one.
MIGRATION_38 = """
ALTER TABLE user_prefs ADD COLUMN tv_genres TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN tv_countries TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN movie_genres TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN movie_countries TEXT NOT NULL DEFAULT '';
ALTER TABLE user_prefs ADD COLUMN filters_paused INTEGER NOT NULL DEFAULT 0;

UPDATE user_prefs SET tv_genres = genres, tv_countries = countries;

ALTER TABLE user_prefs DROP COLUMN genres;
ALTER TABLE user_prefs DROP COLUMN countries;

-- WHICH SERVICES ANSWER IS ALSO ASKED PER MEDIUM NOW, and `calendar_source`
-- keeps meaning the SHOW calendar so an instance that has never opened the panel
-- needs no rewrite of a column it is already using.
--
-- app/sources/prefs.py's `admits_calendar` carried a note saying this question
-- was NO LONGER asked per calendar, and that note was right about what it
-- described: a per-ENDPOINT override, five separate answers, removed because the
-- problem it was reaching for (one service's film calendar is every release in
-- every market) is better answered by the release filters. This is not that.
-- It is per MEDIUM, two answers, and it exists because the services genuinely
-- differ in what they are good for on each side -- one has the deeper show
-- coverage, the other the wider film listing -- which is a preference no
-- narrowing can express.
ALTER TABLE source_prefs ADD COLUMN movie_calendar_source TEXT NOT NULL DEFAULT 'auto';
UPDATE source_prefs SET movie_calendar_source = calendar_source;
"""


# ---------------------------------------------------------------------------
# 39 — a repair log for per-service names, kept so that its GROWTH is a symptom
# ---------------------------------------------------------------------------
#
# WHAT THIS TABLE IS FOR IS NOT ITS CONTENTS. A tracker record carries each
# service's own name for a title (`trakt_slug`, `simkl_slug`) beside that
# service's id, and every path that writes a record is supposed to write the
# name it was handed. When one does not, the record still works -- a link falls
# back to the numeric id, which both services resolve -- so the omission is
# invisible at every surface. It was found by counting rows, months after the
# writer that caused it shipped.
#
# SO THE REPAIR RECORDS ITSELF. A row here means "something stored a record
# without a name it had, and this is where the name was recovered from". A
# handful of rows dated to the repair's first run is the historical backlog; a
# row dated later means a writer is STILL dropping the name, and `evidence` says
# which recovery path caught it. Nothing else in the app would have said so.
#
# IT IS NOT SWEPT, AND IT DOES NOT NEED TO BE. A name is only ever filled into a
# column that is empty, and nothing empties one again, so an identity can be
# repaired at most once per service per account — the table is bounded by the
# roster, not by time. Sweeping it would also throw away the one thing it is
# for: the dates.
MIGRATION_39 = """
CREATE TABLE distrakt_slug_repairs (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The identity, not a row: a name is a fact about the TITLE, so one repair
    -- covers every season and every month of it (see store.learn_ids).
    media        TEXT    NOT NULL,
    match_source TEXT    NOT NULL,
    match_id     TEXT    NOT NULL,
    -- Which name was missing: 'trakt_slug' | 'simkl_slug'. An open set rather
    -- than a CHECK, following show_posters.source -- a third service is a
    -- registration, not a migration, and widening a CHECK means rebuilding a
    -- table (see migration 14).
    column_name  TEXT    NOT NULL,
    value        TEXT    NOT NULL,
    -- WHERE THE NAME CAME FROM, and the reason this column is the point of the
    -- table. Each value names a different bug: 'shared_slug' means a writer
    -- stored the old unattributed column and not the namespaced one;
    -- 'detail_lookup' means a record was short of a name the service had
    -- already handed us on an ordinary read.
    evidence     TEXT    NOT NULL,
    -- How many stored rows the repair actually changed. 0 is worth keeping: it
    -- means the name was already there by the time the write landed, which is a
    -- different story from a repair that did something.
    rows_changed INTEGER NOT NULL DEFAULT 0,
    -- Denormalized purely so the log is readable by a person. Nothing joins on
    -- it and nothing may key off it.
    title        TEXT    NOT NULL DEFAULT '',
    repaired_at  INTEGER NOT NULL
);
-- Reading the log is always "what has been repaired lately", which is the
-- question that distinguishes the backlog from a live leak.
CREATE INDEX ix_distrakt_slug_repairs_at ON distrakt_slug_repairs(repaired_at);
"""

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
    (20, MIGRATION_20),
    (21, MIGRATION_21),
    (22, MIGRATION_22),
    (23, MIGRATION_23),
    (24, MIGRATION_24),
    (25, MIGRATION_25),
    (26, MIGRATION_26),
    (27, MIGRATION_27),
    (28, MIGRATION_28),
    (29, MIGRATION_29),
    (30, MIGRATION_30),
    (31, MIGRATION_31),
    (32, MIGRATION_32),
    (33, MIGRATION_33),
    (34, MIGRATION_34),
    (35, MIGRATION_35),
    (36, MIGRATION_36),
    (37, MIGRATION_37),
    (38, MIGRATION_38),
    (39, MIGRATION_39),
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
