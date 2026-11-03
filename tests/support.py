"""The setup every test file used to build for itself.

conftest.py owns the things that must happen before collection (the data
directory, the import path, the outbound-network guard). This module owns the
things a test file asks for BY NAME: where the repo is on disk, the origin the
app is configured for, a database nothing else is using, and the two base
classes most of the suite's unittest classes were each re-declaring.

WHY NOT ALL FIXTURES: most of this suite is unittest.TestCase, and a pytest
fixture cannot be requested from one. Rewriting nine hundred assertions into
pytest style to reach the fixtures would be a far larger change than the
duplication it removes, and the base classes below give the same three lines of
setup one home either way. New tests can use conftest's fixtures instead; both
run in the same process against the same data directory.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import re
import shutil
import threading
import unittest
from html import unescape
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The one temp directory conftest created before anything imported app.config,
# which is therefore the directory app.config.DATA_DIR is bound to for the whole
# process. Test databases go in it; a file's own mkdtemp would be a second
# directory the app never looks at.
TMP = Path(os.environ["TRAKT_DATA_DIR"])

# The base URL the app is configured for in tests. It is https because the
# session cookie is Secure by default, and it is sent as Origin because the
# app's cross-site rules refuse a mutating request without one.
ORIGIN = "https://testserver"

_db_counter = itertools.count(1)

# The Argon2 hasher the app builds for itself, captured by conftest before it
# swaps in a cheap one for the duration of the run. Everything in the suite
# hashes with the cheap one; this is here so that the COST of the real one stays
# assertable, since it is a security property and nothing else exercises it now.
PRODUCTION_HASHER = None


def calendar_records(entries, endpoint):
    """Raw calendar entries, as the Records a source hands the cache.

    The calendar cache stores each source's NORMALIZED record, so a test that
    wants "these shows air in this window" has to say it in records. Fixtures
    stay written as the payload a real source returns — that is what makes them
    checkable against a captured response — and this is the one place they are
    turned into what the cache actually sees.
    """
    from app.providers.trakt import calendar as trakt_calendar
    return [r for r in (trakt_calendar.to_record(e, endpoint.media) for e in entries)
            if r is not None]


def window_fetch(entries):
    """A stand-in for app.calendar.cache.fetch_window_records.

    `entries` is either the raw entries every window should answer with, or a
    callable (endpoint, start) -> raw entries for a test that wants one window to
    differ from another. The answer names EVERY SOURCE THAT COULD HAVE ANSWERED
    THIS ENDPOINT as having replied — `trakt` always, `simkl` for the four
    endpoints its calendar covers, and `tmdb` for its three when the test's
    Settings hold a TMDB read token (it is in play only then) — because that is
    what a real fill against the registered sources returns, and because the
    caller records who it ASKED independently: a stub answering for fewer
    sources than are in play stores a window that was asked-but-silent, and the
    read path then marks the span partial. A test exercising a genuinely partial
    fetch (one source refused) wants that, and builds its own stub instead of
    this one.

    Patching HERE rather than at the transport keeps the floor filter and the
    window trim out of the way, which is what a test about the read path wants;
    a test about the fill itself patches the client instead and gets both.

    Every record it hands back is attributed to `trakt`, which is what makes a
    span read the same whichever sources answered; a test about per-viewer source
    selection needs records from two services and builds them itself.
    """
    async def fetch(endpoint, settings, start, *, covered=(), sources=None):
        from app.providers.simkl import _SimklProvider
        from app.providers.tmdb import _TmdbProvider

        rows = entries(endpoint, start) if callable(entries) else entries
        answering = ["trakt"]
        if _SimklProvider.capabilities.answers(endpoint.key):
            answering.append("simkl")
        if (_TmdbProvider.capabilities.answers(endpoint.key)
                and getattr(settings, "tmdb_configured", False)):
            answering.append("tmdb")
        # A fill asking only the services a window's coverage says are due
        # (cache.ensure_window) hears from those alone — and the records are
        # Trakt's, so a fill that did not ask Trakt brings none.
        if sources is not None:
            answering = [name for name in answering if name in set(map(str, sources))]
        # (records, answered, unchanged) — the stub never reports a source as
        # unchanged, because a test that wants that case is testing the
        # conditional GET and builds its own stub for it.
        return (calendar_records(rows, endpoint) if "trakt" in answering else []), answering, []
    return fetch


def new_db_path(prefix: str = "test") -> Path:
    """Point app.db at a database no other test is using, and return its path.

    Counted rather than named after the caller: two classes that picked the same
    name would share a file, and a test that left a row behind would then fail
    its neighbour instead of itself.
    """
    from app import db

    path = TMP / f"{prefix}-{next(_db_counter)}.db"
    db.set_db_path(path)
    return path


_template_path: Path | None = None


def _schema_template() -> Path:
    """A migrated but empty database, built once, to be COPIED per test.

    Migrating from nothing costs ~60ms, and almost every test in this suite
    wants a fresh database, so the suite was replaying the same forty-odd
    migration steps well over a thousand times to arrive at the identical empty
    schema. Copying the finished file instead costs about a millisecond.

    This is safe only because the migrations are a pure function of the code:
    they build schema and seed no rows that vary by clock, environment or
    randomness. A migration that inserted a timestamp would be frozen at
    whenever the template was built — if one is ever added, it belongs after the
    copy, not inside it.

    Checkpointed and closed before it is used as a source, because the app runs
    SQLite in WAL mode: the schema lives in a sibling -wal file until then, and
    copying the main file alone would hand every test a database that had never
    been migrated.
    """
    global _template_path
    if _template_path is None:
        from app import db

        path = TMP / "schema-template.db"
        db.set_db_path(path)
        asyncio.run(db.migrate())
        asyncio.run(db.run(lambda conn: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")))
        db.close_thread_connection()
        _template_path = path
    return _template_path


def migrated_db(prefix: str = "test") -> Path:
    """new_db_path plus the migrations, for a synchronous test."""
    template = _schema_template()
    path = new_db_path(prefix)
    shutil.copyfile(template, path)
    return path


# ---------------------------------------------------------------------------
# settings, from a synchronous test
# ---------------------------------------------------------------------------

_settings_loop: asyncio.AbstractEventLoop | None = None
_settings_loop_lock = threading.Lock()


def run_coroutine(coroutine):
    """Run `coroutine` to completion from synchronous code — a test method, a
    setUp, a fixture — whether or not an event loop is running on this thread:
    on one loop of its own, in a daemon thread, for the whole process."""
    global _settings_loop
    with _settings_loop_lock:
        if _settings_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="tests-settings",
                             daemon=True).start()
            _settings_loop = loop
    return asyncio.run_coroutine_threadsafe(coroutine, _settings_loop).result()


def load_settings(open_secrets: bool = True, *, apply_env_override: bool = True):
    """`app.config.load_settings`, for a test that is not itself a coroutine. The
    app reads its settings through db.run, so the read is a coroutine; the suite
    asked for them synchronously in over two hundred places, and this is the one
    door they all go through."""
    from app import config
    return run_coroutine(
        config.load_settings(open_secrets, apply_env_override=apply_env_override))


def save_settings(settings, *, clear_unset_secrets: bool = False) -> None:
    """`app.config.save_settings`, for a test that is not itself a coroutine."""
    from app import config
    run_coroutine(config.save_settings(settings,
                                               clear_unset_secrets=clear_unset_secrets))


@contextlib.contextmanager
def statements_per_transaction():
    """How many statements each `db.transaction` body runs while the block is
    open, one count per transaction in the order they ran (sqlite3's trace
    callback, which fires once per statement — once per row of an executemany).
    Only the body is counted — what holds the write lock; a `prepare` runs before
    it is taken.

    FOR THE WRITERS WHOSE STATEMENT COUNT MUST NOT GROW WITH WHAT THEY WRITE. A
    transaction holds the database's one write lock for as long as its statements
    take, and each can wait a GIL switch interval while the event loop is busy;
    a test storing ten and a hundred of something and finding the same count is
    what holds a writer to that.
    """
    counts: list[int] = []
    with _transactions_traced(lambda statements: counts.append(len(statements))):
        yield counts


@contextlib.contextmanager
def statements_run():
    """Every statement each `db.transaction` body runs while the block is open —
    a list per transaction, in the order they ran, each one's SQL text. Only the
    body is recorded, as `statements_per_transaction` counts it."""
    run: list[list[str]] = []
    with _transactions_traced(run.append):
        yield run


@contextlib.contextmanager
def _transactions_traced(done):
    """`db.transaction` with each body's statements (sqlite3's trace callback,
    which fires once per statement — once per row of an executemany) handed to
    `done` as the body ends."""
    from unittest import mock

    from app import db

    real = db.transaction

    async def traced(fn, *, prepare=None):
        def body(conn, *prepared):
            seen: list[str] = []
            conn.set_trace_callback(seen.append)
            try:
                return fn(conn, *prepared)
            finally:
                conn.set_trace_callback(None)
                done(seen)
        return await real(body, prepare=prepare)

    with mock.patch.object(db, "transaction", traced):
        yield


# ---------------------------------------------------------------------------
# the title store, as a test reads and seeds it
# ---------------------------------------------------------------------------
#
# ONE ROW PER TITLE, SEASON AND EPISODE, a column group per service
# (app/metadata/groups.py). A test asks about one service's group by name rather
# than spelling `{source}_{column}` — the helpers below read a group back under
# its columns' own names, and list every listing the episode rows hold one per
# (service, instant, calendar), the shape a test asserting "what is listed"
# wants. Seeding goes through the live writers where one fits (`put_listing`:
# the store's own fold; `put_title`: the store's own `service_ids` filing), and
# writes a group's columns directly only for what no writer would put there —
# a clock in the past, a failure count.

def as_stored(records):
    """`records` as the calendar's read hands them over: each carrying the
    episode row the title store files it on (`Record.card`) — its title by the
    shared waterfall, and its row by the store's own filing and fold
    (app/metadata/episodes.py's `Book`), run in memory on one calendar. For a
    test that builds Records by hand and groups them with no database."""
    from app.calendar import cache
    from app.metadata import episodes

    records = list(records)
    book = episodes.Book()
    for record in records:
        instant = episodes.Instant(float(record.air_ts), calendars=1, entry=None)
        book.place(cache.group_base(record), str(record.media), str(record.source),
                   record.season, record.episode_number, instant)
    for record in records:
        book.refold(cache.group_base(record), 1, episodes.utc_day(float(record.air_ts)))
    for record in records:
        stated = ((None, None) if str(record.media) == "movie"
                  else (record.season, record.episode_number))
        record.card = next(
            f"{row.key}|{row.unit}" for row in book.of_title(cache.group_base(record))
            if any(held.at == float(record.air_ts)
                   and (row.stated(held) if row.media != "movie" else (None, None)) == stated
                   for held in row.instants[str(record.source)]))
    return records


def stored_groups(records) -> list[dict]:
    """The calendar's groups of hand-built `records`, as `as_stored` files them."""
    from app.calendar import cache
    return cache.group_records(as_stored(records))


def _title_columns() -> tuple[str, ...]:
    from app.metadata import groups, tables
    return (*groups.NOT_WORDS[tables.TITLES], *groups.WORDS[tables.TITLES])


async def title_groups(source: str | None = None) -> list[dict]:
    """Every service group in `titles` (only `source`'s, when named), each with
    `source`, `title_key`, `media`, `source_id` (the group's own id) and every
    group column under its own name."""
    from app import db
    from app.metadata import groups, rows

    names = [source] if source else list(groups.sources())
    found = await db.fetch_all(rows.title_groups_sql(_title_columns(), sources=names))
    return [dict(row) for row in found]


async def title_group(title_key: str, source: str) -> dict | None:
    """One title's group of one service, or None where it has none."""
    return next((group for group in await title_groups(source)
                 if group["title_key"] == str(title_key)), None)


_SEASON_COLUMNS = ("name", "overview", "episode_count", "premiere", "air_dates_json", "poster",
                   "cast_json", "videos_json", "fetched_at", "stale_after", "failed_at",
                   "fail_count")


async def season_groups(source: str | None = None) -> list[dict]:
    """Every service's group of every season row (only `source`'s, when named):
    `source`, `title_key`, `season` and the group's columns."""
    from app import db
    from app.metadata import groups, tables

    out = []
    for name in ([source] if source else groups.sources()):
        for row in await db.fetch_all(
                f"SELECT title_key, season, {groups.select(tables.SEASONS, name, _SEASON_COLUMNS)} "
                f"FROM {tables.SEASONS} WHERE {groups.present(tables.SEASONS, name)} "
                "ORDER BY title_key, season"):
            out.append({"source": name, **dict(row)})
    return out


async def season_group(title_key: str, season: int, source: str) -> dict | None:
    return next((group for group in await season_groups(source)
                 if group["title_key"] == str(title_key) and group["season"] == int(season)),
                None)


_EPISODE_COLUMNS = ("title", "overview", "first_aired", "episode_type", "runtime", "rating",
                    "votes", "fetched_at", "stale_after")


async def episode_groups(source: str | None = None) -> list[dict]:
    """Every service's DESCRIPTION of every episode row (only `source`'s, when
    named) — a listing's stub (`fetched_at` 0) included: `source`, `title_key`,
    `unit`, `season`, `number` and the description's columns."""
    from app import db
    from app.metadata import groups, tables

    out = []
    for name in ([source] if source else groups.sources()):
        for row in await db.fetch_all(
                f"SELECT title_key, unit, season, number, "
                f"{groups.select(tables.EPISODES, name, _EPISODE_COLUMNS)} "
                f"FROM {tables.EPISODES} WHERE {groups.present(tables.EPISODES, name)} "
                "ORDER BY title_key, season, number"):
            out.append({"source": name, **dict(row)})
    return out


async def listings(endpoint: str | None = None) -> list[dict]:
    """Every listing the episode rows hold, one per (service, instant, calendar)
    — on `endpoint` alone when one is named — as `source`, `media`, `source_id`
    (the id the listing is filed under), `endpoint`, `title_key`, `unit`,
    `air_ts`, `date_only`, `season` and `episode_number` (what the listing
    stated, None for a half it left out), `from_search`, `label`, `stored_at`."""
    from app import db, providers
    from app.endpoints import ENDPOINTS
    from app.metadata import episodes, groups, tables
    from app.providers.base import Source

    rows = await db.fetch_all(f"SELECT * FROM {tables.EPISODES} ORDER BY title_key, unit")
    filed_as = {}
    for row in await db.fetch_all(f"SELECT title_key, "
                                  + ", ".join(f"{s}_ids_json, {s}_id" for s in groups.sources())
                                  + f" FROM {tables.TITLES}"):
        for source in groups.sources():
            # The read's own rule (app/calendar/entries.py): the service's
            # spelling of its ids, else the group's entry.
            filed_as[(row["title_key"], source)] = (
                providers.registered()[Source(source)].listing_id(
                    json.loads(row[f"{source}_ids_json"] or "{}"))
                or str(row[f"{source}_id"] or ""))
    out = []
    for row in rows:
        stored = episodes.Row.read(row)
        for source, held in stored.instants.items():
            for instant in held:
                season, number = stored.stated(instant)
                for key, spec in ENDPOINTS.items():
                    if not instant.calendars & spec.bit or (endpoint and key != endpoint):
                        continue
                    out.append({
                        "source": source, "media": stored.media,
                        "source_id": instant.entry or filed_as.get((stored.key, source), ""),
                        "endpoint": key, "title_key": stored.key, "unit": stored.unit,
                        "air_ts": instant.at, "date_only": instant.date_only,
                        "season": season, "episode_number": number,
                        "from_search": int(bool(instant.searched & spec.bit)),
                        "label": instant.label, "stored_at": instant.listed_at})
    return out


def put_title(conn, source: str, media: str, source_id, title_key: str, *,
              ids: dict | None = None, **values) -> None:
    """A service's group of a title, inside the caller's connection: the entry
    `source_id` names filed in `service_ids` by the store's own writer, the
    group's columns as `values` give them (`fetched_at` now unless given)."""
    import time

    from app.metadata import groups, rows, tables

    own = rows.own_id(source, source_id, ids or {})
    stated = dict(ids or {rows.ID_NAMESPACE[source]: int(own) if own.isdigit() else own})
    columns = {"id": own, "ids_json": json.dumps(stated), "fetched_at": int(time.time()),
               **values}
    if "title" in values and "title_fold" not in values:
        columns["title_fold"] = rows.fold_title(values["title"])
    conn.execute(f"INSERT INTO {tables.TITLES} (title_key, media, created_at) VALUES (?, ?, 0) "
                 "ON CONFLICT(title_key) DO NOTHING", (title_key, media))
    conn.execute(f"UPDATE {tables.TITLES} SET "
                 + ", ".join(f"{source}_{name} = ?" for name in columns
                             if groups.has(tables.TITLES, source, name))
                 + " WHERE title_key = ?",
                 (*(value for name, value in columns.items()
                    if groups.has(tables.TITLES, source, name)), title_key))
    rows.file_ids(conn, [[source, media, str(source_id), own, title_key]])


def put_season(conn, source: str, title_key: str, season: int, **values) -> None:
    """A service's group of one season, inside the caller's connection."""
    import time

    from app.metadata import groups, tables

    columns = {"fetched_at": int(time.time()), **values}
    conn.execute(f"INSERT INTO {tables.SEASONS} (title_key, season) VALUES (?, ?) "
                 "ON CONFLICT(title_key, season) DO NOTHING", (title_key, int(season)))
    conn.execute(f"UPDATE {tables.SEASONS} SET "
                 + ", ".join(f"{source}_{name} = ?" for name in columns
                             if groups.has(tables.SEASONS, source, name))
                 + " WHERE title_key = ? AND season = ?",
                 (*(value for name, value in columns.items()
                    if groups.has(tables.SEASONS, source, name)), title_key, int(season)))


def put_episode(conn, source: str, title_key: str, season: int, number: int, *,
                media: str = "show", **values) -> None:
    """A service's description of one numbered episode, inside the caller's
    connection (`fetched_at` 0 — a listing's stub — unless given)."""
    from app.metadata import groups, tables

    unit = f"S{int(season)}E{int(number)}"
    columns = {"fetched_at": 0, **values}
    conn.execute(f"INSERT INTO {tables.EPISODES} (title_key, unit, media, season, number) "
                 "VALUES (?, ?, ?, ?, ?) ON CONFLICT(title_key, unit) DO NOTHING",
                 (title_key, unit, media, int(season), int(number)))
    conn.execute(f"UPDATE {tables.EPISODES} SET "
                 + ", ".join(f"{source}_{name} = ?" for name in columns
                             if groups.has(tables.EPISODES, source, name))
                 + " WHERE title_key = ? AND unit = ?",
                 (*(value for name, value in columns.items()
                    if groups.has(tables.EPISODES, source, name)), title_key, unit))


def put_listing(conn, source: str, media: str, source_id, endpoint: str, title_key: str,
                air_ts: float, *, season: int | None = None, number: int | None = None,
                date_only: bool = False, from_search: bool = False, stored_at: int = 0,
                label: str = "") -> None:
    """A service's listing of an episode on a calendar, inside the caller's
    connection — filed on its episode's row, and folded, by the store's own
    writer (app/metadata/episodes.py). The title's group of the service, where
    it has one, says whether `source_id` is its entry."""
    from app.endpoints import ENDPOINTS
    from app.metadata import episodes, rows, tables

    bit = ENDPOINTS[endpoint].bit
    own = rows.own_id(source, source_id, {})
    held = conn.execute(f"SELECT {source}_id FROM {tables.TITLES} WHERE title_key = ?",
                        (title_key,)).fetchone()
    entry = None if held is None or held[0] in (None, own) else own
    book = episodes.Book()
    book.hold(episodes.read_titles(conn, [title_key]))
    instant = episodes.Instant(float(air_ts), int(date_only), bit, bit if from_search else 0,
                               0, label, int(stored_at), entry)
    book.place(title_key, media, source, season, number, instant)
    book.refold(title_key, bit, instant.day)
    book.write(conn)


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """A freshly migrated database per test, for async tests with no HTTP."""

    async def asyncSetUp(self):
        self.db_path = migrated_db(type(self).__name__.lower())

    async def asyncTearDown(self):
        from app import db

        db.close_thread_connection()


PASSWORD = "hunter2hunter2"


class AppTestCase(unittest.TestCase):
    """A migrated database, saved settings and a TestClient, per test.

    The client speaks https because the session cookie is Secure by default and
    a client honouring that will not send it back over plain http, and it sends
    an Origin header because the app refuses a mutating request without one.
    Both were re-explained in a comment in most of the copies this replaces.

    Override `make_settings` to change what is saved — what the copies actually
    differed on was whether public_base_url was set.
    """

    def make_settings(self):
        from app.config import Settings

        # AN INSTANCE WITH NOTHING SET UP, AND THAT NOW HAS TO BE SAID OUT LOUD
        # FOR THE ONE SOURCE THAT NEEDS NOTHING. Simkl's calendar is public CDN
        # files, so it is a usable calendar source on a Settings with every
        # credential blank — which is correct for the app and wrong for a shared
        # fixture: it would have every page render in this suite reach for a real
        # CDN, and conftest's network guard is what would tell you, one file at a
        # time. A test that wants that source switches it back on.
        return Settings(simkl_public_calendar_enabled=False)

    def setUp(self):
        from app.main import app

        self.db_path = migrated_db(type(self).__name__.lower())
        save_settings(self.make_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def tearDown(self):
        from app import db

        self.client.close()
        db.close_thread_connection()

    def make_user(self, username: str, password: str = PASSWORD, **flags) -> int:
        """An account with whatever grants the test needs."""
        from app import auth
        from app.config import Settings

        return asyncio.run(auth.create_user(
            username=username, password=password, settings=Settings(), **flags))

    def link_identity(self, user_id: int, provider: str, provider_user_id: int,
                      access_token: str | None = None) -> None:
        """Give an account a linked provider identity, as a real handshake would.

        Written straight to the table rather than through a flow: the flows have
        their own tests, and everything that gates on "has this person linked
        Trakt?" only needs the row to exist.

        A SIMKL TOKEN IS NORMALIZED TO LOOK LIKE A V2 ONE, unless it is already
        empty (a test deliberately asking for "linked, but no usable token") or
        already carries the real prefix. simkl_routes.access_token_for_user
        reads a token's local SHAPE to tell a v2 grant from a legacy v1 one it
        must never send (the app keeps no V1 registration to send it with) — see app/auth/simkl.py's
        V2_ACCESS_TOKEN_PREFIX — and every caller here is asking for an
        ordinarily-working link, not testing that specific legacy-token path
        (tests/auth/test_simkl_login.py's LegacyTokenTests does that directly,
        against auth.insert_linked_identity, bypassing this helper).
        """
        from app import auth, db
        from app.auth import simkl as simkl_auth

        if (provider == "simkl" and access_token
                and not access_token.startswith(simkl_auth.V2_ACCESS_TOKEN_PREFIX)):
            access_token = simkl_auth.V2_ACCESS_TOKEN_PREFIX + access_token

        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider=provider,
            provider_user_id=provider_user_id, access_token=access_token)))

    def sign_in_as(self, user_id: int) -> str:
        """Put a real session cookie on the client, skipping the login form.

        Does NOT clear the jar first: the session cookie replaces itself by
        name, and some tests are about what a handshake cookie set earlier does
        once a session exists. A test that wants a clean jar says so itself.
        """
        from app import auth

        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)
        return session_id


# ---------------------------------------------------------------------------
# The tracker's month, read the two ways there are to read one.
# ---------------------------------------------------------------------------
class RewatchPrompt:
    """The re-watch question, read back out of the markup it is now drawn as.

    IT USED TO BE A `needs_decision` OBJECT the browser turned into a dialog, and
    every fact in it is still here — the question is drawn open over a month left
    exactly as it was, so the whole answer is out of band and this reads it.
    `asked` is the one thing every caller checks: whether the add stopped to ask
    rather than writing something.

    THE DAYS COME BACK AS THEY ARE SPELLED FOR A READER, because that is what the
    server now sends: `completed_on` was an ISO day the browser formatted, and
    the formatting moved with the markup.
    """

    _OPEN = re.compile(r'<dialog id="rewatchModal"[^>]*data-open-modal')
    _FINISHED = re.compile(r"Your history says you finished (.*?) season (\d+)\s*"
                           r"on (.*?)\.</p>", re.S)
    _FROM = re.compile(r'id="rewatchFrom"[^>]*value="([^"]*)"')
    _PRESET = re.compile(r'data-fill-value="([^"]*)"')
    # A preset carries its DAY and no longer its spelling: pressing one fills the
    # field, and the field asks the server what the button should say.
    _ASKS_LABEL = re.compile(r'id="rewatchFrom"[^>]*hx-get="([^"]*)"', re.S)
    _SUBMIT = re.compile(r'id="rewatchSubmit".*?>(.*?)</button>', re.S)
    _EPISODE = re.compile(r"<li>S\d+E(\d+)\s*<span class=\"rewatch-when\">([^<]*)</span>")
    _POST = re.compile(r'hx-post="([^"]*)"')

    def __init__(self, html: str):
        self.html = html

    @property
    def asked(self) -> bool:
        return bool(self._OPEN.search(self.html))

    @property
    def label_source(self) -> str:
        """Where the submit button's wording comes from as the field is typed
        into. Empty when the field asks nobody, which is the shape this replaced:
        a date formatter in the browser."""
        found = self._ASKS_LABEL.search(self.html)
        return found.group(1) if found else ""

    @property
    def title(self) -> str:
        found = self._FINISHED.search(self.html)
        return " ".join(found.group(1).split()) if found else ""

    @property
    def season(self) -> int | None:
        found = self._FINISHED.search(self.html)
        return int(found.group(2)) if found else None

    @property
    def finished_on(self) -> str:
        """The day the previous run ended, as the reader is shown it."""
        found = self._FINISHED.search(self.html)
        return " ".join(found.group(3).split()) if found else ""

    @property
    def suggested_from(self) -> str:
        """The day a fresh run is offered from — the date field's own value."""
        found = self._FROM.search(self.html)
        return found.group(1) if found else ""

    @property
    def presets(self) -> list[str]:
        """The two days either side of the restart, or [] where nothing
        restarted and the plainer question is asked."""
        return self._PRESET.findall(self.html)

    @property
    def episodes(self) -> list[tuple[str, str]]:
        """(episode number, the day it was watched) for what has been seen since
        the run turned back."""
        return self._EPISODE.findall(self.html)

    @property
    def submit_label(self) -> str:
        """What the button that starts a fresh run SAYS as the question opens.

        The field arrives pre-filled, so a label that named no day — or named
        one only after somebody touched the field — was the prompt offering one
        answer and announcing another."""
        found = self._SUBMIT.search(self.html)
        return " ".join(found.group(1).split()) if found else ""

    @property
    def resubmit(self) -> str:
        """Where answering re-posts. Both buttons send the same address: the
        route that asked IS the add, and it now has the answer it was missing."""
        found = self._POST.findall(self.html)
        return found[0] if found else ""


def restore_backup(client, document: dict, ack: str = "REPLACE MY DATA"):
    """POST a tracker export back, the way the page does.

    THE DOCUMENT IS ONE FIELD OF A FORM. Reading the chosen file is the one thing
    only the browser can do — app/authz.py's shape guard admits application/json
    alone — so the text arrives in a hidden input beside the typed
    acknowledgement, and everything else about the restore is markup. Two callers
    drive it, and each writing that shape out itself is two places to fix.
    """
    return client.post("/api/distrakt/restore",
                       json={"payload": json.dumps(document), "ack": ack})


_IMPORT_PICK = re.compile(r'name="(pick:[^"]+)"')


def import_everything_offered(client, year: int, month: int):
    """Press Import and keep every box ticked, the way the dialog opens: read the
    offer the page would draw, then post every row of it. Returns the answer.

    A MONTH IS NOT FILLED BY OPENING IT, and the import files only what is picked,
    so a test that means "import the month" has to say which — and "all of them"
    is what the dialog's default states."""
    offer = client.get(f"/distrakt/fragments/import?year={year}&month={month}")
    picks = {unescape(name): "1" for name in _IMPORT_PICK.findall(offer.text)}
    return client.post(f"/api/distrakt/import?year={year}&month={month}", json=picks)


async def import_offered(user_id: int, month_key: str, settings) -> int:
    """`calendar_import.import_premieres` with every offered title chosen — the
    store-level twin of `import_everything_offered`."""
    from app.distrakt import calendar_import, store
    doc = await store.load_month(user_id, month_key)
    if doc is None:
        return 0
    offer = await calendar_import.import_candidates(user_id, doc, settings)
    return await calendar_import.import_premieres(
        user_id, month_key, settings, {calendar_import.candidate_ref(r) for r in offer.offered})


def tracker_month(user_id: int, year: int, month: int, *, fresh: bool = False) -> dict:
    """The payload a month is computed into, called directly.

    THERE IS NO LONGER A ROUTE THAT HANDS THIS BACK. The tracker's page reads a
    RENDERED month (GET /distrakt/fragments/month) and every mutation answers
    with the same markup, so the JSON that used to be both the page's state and
    a test's window into a month is gone — it was state only because the rows
    were assembled in the browser.

    WHAT THAT SPLITS IS WHICH QUESTION A TEST IS ASKING, and the split is worth
    having. A test about what a month CONTAINS — which bucket a season landed
    in, what its counts say, which service could not be read — asks this, the
    function that works it out, with no HTTP and no markup in the way. A test
    about what a VIEWER is told asks the fragment and reads its markup, the way
    every details-panel test already does.
    """
    from app.auth import viewer
    from app.distrakt import routes as distrakt_routes

    async def _run() -> dict:
        settings = await viewer.settings_for_user(user_id)
        payload, _status = await distrakt_routes._distrakt_month_payload(
            user_id, year, month, settings, force_fresh=fresh)
        return payload

    return asyncio.run(_run())


class TrackerMonth:
    """A rendered month fragment, read back out of its own markup.

    The same bargain DetailsPanel makes one panel down: what a viewer is told is
    now markup, so a test that is about what the page SAYS reads the page.
    """

    def __init__(self, html: str):
        self.html = html

    @property
    def headings(self) -> list[str]:
        """Every section head and weekday head, in the order they are drawn."""
        return re.findall(r'<div class="distrakt-(?:bucket|weekday)-head">([^<]*)</div>',
                          self.html)

    @property
    def titles(self) -> list[str]:
        """Every row's title, in the order the page draws them."""
        return re.findall(r'<span class="tt">([^<]*)</span>', self.html)

    def section(self, label: str) -> list[str]:
        """The titles drawn under one heading, up to the next heading."""
        parts = re.split(r'<div class="distrakt-(?:bucket|weekday)-head">([^<]*)</div>',
                         self.html)
        for index in range(1, len(parts), 2):
            if parts[index] == label:
                return re.findall(r'<span class="tt">([^<]*)</span>', parts[index + 1])
        return []

    @property
    def notice(self) -> str:
        """What the degraded-month banner says, or "" when it is hidden."""
        found = re.search(r'<div id="distraktNotice"[^>]*>(.*?)</div>', self.html, re.S)
        return unescape((found.group(1) if found else "").strip())

    @property
    def banner(self) -> str:
        """What the read-only banner says, or "" when the month is editable."""
        found = re.search(r'<div class="distrakt-frozen-note">(.*?)</div>', self.html, re.S)
        return unescape((found.group(1) if found else "").strip())

    @property
    def toast(self) -> str:
        """The line this answer flashed, or ""."""
        found = re.search(r'<div class="toast [^"]*"[^>]*>(.*?)</div>', self.html, re.S)
        return unescape((found.group(1) if found else "").strip())

    @property
    def asks(self) -> list[str]:
        """The text of each question drawn above the list."""
        return [unescape(re.sub(r"<[^>]+>", "", block)).strip()
                for block in re.findall(
                    r'<span class="distrakt-unknown-text">(.*?)</span>', self.html, re.S)]

    def posts_to(self) -> list[str]:
        """Every address this fragment's controls post to."""
        return re.findall(r'hx-post="([^"]*)"', self.html)


# ---------------------------------------------------------------------------
# The details panel, read back out of its own markup.
# ---------------------------------------------------------------------------
class DetailsPanel:
    """What a rendered details fragment (templates/_details.html) says.

    THE ROUTES THAT DRAW IT ANSWER WITH THE PANEL RATHER THAN WITH A PAYLOAD, so
    a test asserting what a viewer is told has to read the markup. Three test
    files need the same handful of readings — which episodes carry a tick, whose
    tick it is, where it points — and a copy of these patterns in each of them
    would be three things to fix the day a class name changes.

    Deliberately not an HTML parser: what is asserted here is the panel's own
    class names, which ARE the contract between the template and the stylesheet,
    so a reading that stopped matching is a change worth noticing.
    """

    _ROW = re.compile(r'<div class="ep-row(?P<flags>[^"]*)">(?P<body>.*?)</div>', re.S)
    _NUM = re.compile(r'<span class="ep-num">E(\d+)</span>')
    _TICK = re.compile(
        r'<(?:a|span) class="ep-check svc-(?P<source>[a-z]+)(?P<flags>[^"]*)"'
        r'(?P<attrs>[^>]*)>', re.S)
    _HREF = re.compile(r'href="(?P<url>[^"]*)"')

    def __init__(self, html: str):
        self.html = html

    def _rows(self):
        for match in self._ROW.finditer(self.html):
            number = self._NUM.search(match.group("body"))
            if number:
                yield int(number.group(1)), match.group("flags"), match.group("body")

    @property
    def episodes(self) -> list[int]:
        return [number for number, _flags, _body in self._rows()]

    @property
    def watched(self) -> list[int]:
        return [number for number, flags, _body in self._rows() if "watched" in flags]

    @property
    def by_source(self) -> dict[str, list[int]]:
        """{service: the episodes its tick is drawn FILLED on}, which is the
        rendered form of what each service recorded."""
        found: dict[str, list[int]] = {}
        for number, _flags, body in self._rows():
            for tick in self._TICK.finditer(body):
                if "unseen" not in tick.group("flags"):
                    found.setdefault(tick.group("source"), []).append(number)
        return found

    @property
    def unasked(self) -> list[str]:
        """The services whose ticks are drawn as NOT ASKED — no id for this
        title — rather than as an answer."""
        return sorted({tick.group("source") for tick in self._TICK.finditer(self.html)
                       if "unasked" in tick.group("flags")})

    @property
    def services(self) -> list[str]:
        """Every service a tick is drawn for, filled or hollow."""
        return sorted({tick.group("source") for tick in self._TICK.finditer(self.html)})

    @property
    def tick_links(self) -> dict[str, str]:
        """{service: where its first tick points}, omitting a service whose ticks
        are drawn as plain text because the title carries no id for it."""
        found: dict[str, str] = {}
        for tick in self._TICK.finditer(self.html):
            url = self._HREF.search(tick.group("attrs"))
            if url and tick.group("source") not in found:
                # Unescaped, because what is being asserted is the ADDRESS a
                # press goes to and not how an attribute spells an ampersand.
                found[tick.group("source")] = unescape(url.group("url"))
        return found

    _TAB = re.compile(
        r'<input type="radio" name="detailsSeason" class="ep-season-radio"\s*'
        r'id="epSeason-(?P<number>\d+)"(?P<attrs>[^>]*)>', re.S)
    _TAB_GET = re.compile(r'hx-get="(?P<url>[^"]*)"')

    @property
    def season_tabs(self) -> list[int]:
        """The seasons the picker offers, in the order it draws them. Empty where
        there is nothing to pick between — one season draws no strip."""
        return [int(tab.group("number")) for tab in self._TAB.finditer(self.html)]

    @property
    def open_tab(self) -> int | None:
        """The season whose rows came with the panel."""
        for tab in self._TAB.finditer(self.html):
            if "checked" in tab.group("attrs"):
                return int(tab.group("number"))
        return None

    @property
    def tab_urls(self) -> dict[int, str]:
        """{season: where its tab fetches its rows}, omitting the open one — that
        tab carries no request at all, because its rows are already here."""
        found: dict[int, str] = {}
        for tab in self._TAB.finditer(self.html):
            url = self._TAB_GET.search(tab.group("attrs"))
            if url:
                # Unescaped, because what is asserted is the ADDRESS a press goes
                # to and not how an attribute spells an ampersand.
                found[int(tab.group("number"))] = unescape(url.group("url"))
        return found
