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
setUp/tearDown (see e.g. tests/calendar/test_routes.py). Existing files are NOT
rewritten to use these; only tests written against this file adopt them.

The outbound-network guard below is the one thing here that applies to every
test whether it asks for it or not — see its own docstring for why.
"""
from __future__ import annotations

import asyncio
import asyncio.selector_events
import ipaddress
import itertools
import os
import socket
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


# ---------------------------------------------------------------------------
# outbound network guard
# ---------------------------------------------------------------------------

class OutboundNetworkBlocked(RuntimeError):
    """A test tried to open a connection to something that is not this machine.

    Always a bug in the test, never in the app: every provider call this suite
    exercises is supposed to be patched out, and a patch that does not patch
    what it names leaves a real call behind. That failure is otherwise INVISIBLE
    — the transport swallows the provider's refusal and the test passes on it —
    so the suite silently starts depending on whether the machine running it has
    a network and whether the provider happens to be reachable.

    The address is in the message because the host names which call escaped:
    api.trakt.tv is a Trakt patch, image.tmdb.org an artwork one.
    """


def _host_is_local(host) -> bool:
    """Whether `host` is this machine talking to itself.

    Loopback has to stay open: asyncio's own event loop wakes itself through a
    127.0.0.1 socket pair on Windows, so blocking it would take down every async
    test rather than the outbound calls this guard is about. AF_UNIX addresses
    are a filesystem path, not a host, and are local by definition.
    """
    if not isinstance(host, str):  # an AF_UNIX path arrives as bytes
        return True
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _connect_target_is_local(address) -> bool:
    return _host_is_local(address[0] if isinstance(address, tuple) else address)


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """Fail any test that opens a connection off this machine.

    Blocks below every HTTP client rather than at httpx, so it catches whatever
    the app reaches for — and RECORDS each refusal as well as raising, since the
    app's transports deliberately swallow provider failures and would otherwise
    turn a blocked call straight back into a passing test.

    THREE CHOKE POINTS, NOT ONE, and each covers a hole the others leave:
      - socket.connect / connect_ex   blocking clients (httpx's sync transport).
      - the event loops' sock_connect  async clients, which is what the app
        actually uses. On Windows the proactor loop reaches the network through
        overlapped IO and NEVER calls socket.connect, so a guard with only the
        first choke point silently passes every async call through — measured,
        not assumed.
      - socket.getaddrinfo             names the HOST for a call made by name,
        which is the useful half of the report ("api.trakt.tv" says which patch
        is wrong; an IP does not). A literal-IP call skips resolution entirely,
        which is why it cannot be the only one either.

    Yields the list of targets it refused, so the tests that exercise the guard
    itself can assert on it and then clear it; every other test wants the list
    to stay empty and the teardown assertion enforces that.
    """
    refused: list[str] = []

    def _refuse(target, description) -> None:
        refused.append(str(description))
        raise OutboundNetworkBlocked(
            f"This test tried to reach {target}. Something it means to patch is "
            f"not patched — see tests/conftest.py."
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _guarded_connect(self, address):
        if not _connect_target_is_local(address):
            _refuse(repr(address), address)
        return real_connect(self, address)

    def _guarded_connect_ex(self, address):
        if not _connect_target_is_local(address):
            _refuse(repr(address), address)
        return real_connect_ex(self, address)

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if not _host_is_local(host):
            _refuse(repr(host), host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)

    # Patched on the loop CLASSES rather than on a running loop: each test makes
    # its own loop (asyncio.run, IsolatedAsyncioTestCase), so there is no loop
    # instance to reach for at fixture time.
    for loop_class in (asyncio.selector_events.BaseSelectorEventLoop,
                       getattr(asyncio, "proactor_events", None)
                       and asyncio.proactor_events.BaseProactorEventLoop):
        if loop_class is None:  # pragma: no cover — POSIX has no proactor loop
            continue
        real_sock_connect = loop_class.sock_connect

        def _guarded_sock_connect(self, sock, address, *, _real=real_sock_connect):
            if not _connect_target_is_local(address):
                _refuse(repr(address), address)
            return _real(self, sock, address)

        monkeypatch.setattr(loop_class, "sock_connect", _guarded_sock_connect)

    yield refused
    assert not refused, f"outbound network calls escaped this test: {refused}"


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
