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
import itertools
import os
import shutil
import unittest
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
    return [r for r in (trakt_calendar.to_record(e, endpoint) for e in entries)
            if r is not None]


def window_fetch(entries):
    """A stand-in for app.calendar.cache.fetch_window_records.

    `entries` is either the raw entries every window should answer with, or a
    callable (endpoint, start) -> raw entries for a test that wants one window to
    differ from another. The answer names EVERY SOURCE THAT COULD HAVE ANSWERED
    THIS ENDPOINT as having replied — `trakt` always, and `simkl` for the four
    endpoints its calendar covers — because that is what a real fill against two
    registered sources returns, and because the caller records who it ASKED
    independently: a stub answering for fewer sources than are in play stores a
    window that was asked-but-silent, and the read path then marks the span
    partial. A test exercising a genuinely partial fetch (one source refused)
    wants that, and builds its own stub instead of this one.

    Patching HERE rather than at the transport keeps the floor filter and the
    window trim out of the way, which is what a test about the read path wants;
    a test about the fill itself patches the client instead and gets both.

    Every record it hands back is attributed to `trakt`, which is what makes a
    span read the same whichever sources answered; a test about per-viewer source
    selection needs records from two services and builds them itself.
    """
    async def fetch(endpoint, settings, start):
        from app.providers.simkl import _SimklProvider

        rows = entries(endpoint, start) if callable(entries) else entries
        sources = ["trakt"]
        if _SimklProvider.capabilities.answers(endpoint.key):
            sources.append("simkl")
        return calendar_records(rows, endpoint), sources
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

        return Settings()

    def setUp(self):
        from app.config import save_settings
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
        """
        from app import auth, db

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
