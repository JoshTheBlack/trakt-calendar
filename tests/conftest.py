"""Shared pytest fixtures for the app's test suite.

TRAKT_DATA_DIR is set to a fresh temp directory HERE, at collection time,
before any test module imports anything from app/ — app/config.py reads it
once, at import, into config.DATA_DIR, and a value set after that first import
has no effect on the rest of the process. Every existing test file guards for
exactly this today with its own `os.environ.setdefault("TRAKT_DATA_DIR",
tempfile.mkdtemp(...))` line before its app imports, each hoping to be first.
Setting it here, before collection touches any of them, means that guard is no
longer needed by anything written from here on: this file lands before every
test module pytest collects, so it always wins the race the individual
setdefault calls were each trying to win alone.

The fixtures below extract the per-test setup — a migrated database, saved
settings, an app client — that most test files currently rebuild by hand in
setUp/tearDown (see e.g. tests/test_calendar_route.py). Existing files are NOT
rewritten to use these; only tests written against this file adopt them.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TRAKT_DATA_DIR", tempfile.mkdtemp(prefix="tns-test-"))

ORIGIN = "https://testserver"
_db_counter = itertools.count(1)


@pytest.fixture
def db_path(tmp_path):
    """A freshly migrated, test-only database, distinct from every other test's."""
    from app import db

    path = tmp_path / f"test-{next(_db_counter)}.db"
    db.set_db_path(path)
    asyncio.run(db.migrate())
    yield path
    db.close_thread_connection()


@pytest.fixture
def settings(db_path):
    """A saved Settings row pointing at ORIGIN, the base URL `client` uses."""
    from app.config import Settings, save_settings

    s = Settings(public_base_url=ORIGIN)
    save_settings(s)
    return s


@pytest.fixture
def client(settings):
    """A TestClient wired to the real app, with the Origin header the app's
    CSRF check requires on state-changing requests.

    Not entered as a context manager — same as every existing test file — so
    the app's lifespan (the startup heartbeat) does not run during a route
    test that has no reason to exercise it.
    """
    from app.main import app

    c = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    yield c
    c.close()
