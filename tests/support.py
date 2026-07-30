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


def migrated_db(prefix: str = "test") -> Path:
    """new_db_path plus the migrations, for a synchronous test."""
    from app import db

    path = new_db_path(prefix)
    asyncio.run(db.migrate())
    return path


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """A freshly migrated database per test, for async tests with no HTTP."""

    async def asyncSetUp(self):
        from app import db

        self.db_path = new_db_path(type(self).__name__.lower())
        await db.migrate()

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
