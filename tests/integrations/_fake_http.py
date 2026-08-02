"""A recording stand-in for a pooled httpx client, shared by the outbound tests.

WHY A DOUBLE RATHER THAN httpx.MockTransport. What these tests are mostly about
is what the app SENDS — the exact URL a base URL and a path concatenate into, and
whether the API key travels in a header or a query string. A transport-level mock
would answer that too, but it would also drag in building a real client per test,
and the modules under test take their client from a Pool that must not be closed.
Replacing `Pool.client` with an object that records calls is the smaller lie.

WHAT IT DOES NOT FAKE: the response. Those are real `httpx.Response` objects, so
`.json()`, `.status_code`, `.content` and `.text` behave exactly as they do in
production — including raising ValueError on a malformed body, which is a branch
several of these tests are specifically about.

The module name carries the underscore rather than the names inside it: everything
here is for this package's tests and nothing outside should import it, which is the
convention the standard library uses (`_collections_abc`).
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from unittest import mock

import httpx


@dataclass
class Call:
    """One outbound request, as the module under test asked for it."""
    method: str
    url: str
    params: dict | None = None
    headers: dict | None = None
    json: object = None
    timeout: object = None

    @property
    def query(self) -> dict:
        """The query string actually built, so a test can assert a key is NOT in
        it without knowing whether it arrived via `params` or was concatenated on."""
        return dict(httpx.URL(self.url).params) | dict(self.params or {})


@dataclass
class RecordingClient:
    """Answers `get`/`post` from a queue of responses and records every call.

    A single response is reused for every call; a list is consumed in order, which
    is what the paginating reads need. Running out is an error rather than a
    silent repeat — a test that expected three round trips and got four should
    fail loudly rather than pass against a response it never meant to serve.
    """
    responses: list[httpx.Response] | httpx.Response
    calls: list[Call] = field(default_factory=list)
    raises: Exception | None = None

    def _next(self) -> httpx.Response:
        if self.raises is not None:
            raise self.raises
        if isinstance(self.responses, httpx.Response):
            return self.responses
        if not self.responses:
            raise AssertionError("the client was called more times than the test provided for")
        return self.responses.pop(0)

    async def get(self, url, *, params=None, headers=None, timeout=None, **kw):
        self.calls.append(Call("GET", str(url), params, headers, None, timeout))
        return self._next()

    async def post(self, url, *, json=None, headers=None, timeout=None, **kw):
        self.calls.append(Call("POST", str(url), None, headers, json, timeout))
        return self._next()

    @property
    def only(self) -> Call:
        """The single call this test expected, asserted to be single."""
        if len(self.calls) != 1:
            raise AssertionError(f"expected exactly one call, got {len(self.calls)}")
        return self.calls[0]


def response(status: int = 200, *, json=None, content: bytes | None = None,
             text: str | None = None) -> httpx.Response:
    """A real httpx.Response, built the way the library would hand one back."""
    if content is not None:
        return httpx.Response(status, content=content)
    if text is not None:
        return httpx.Response(status, text=text)
    return httpx.Response(status, json=json if json is not None else {})


@dataclass
class FakePool:
    """Stands in for an http_pool.Pool, handing out one recording client.

    A whole pool is replaced rather than its `client` method patched, because Pool
    declares __slots__ and has no room for a per-instance override. Replacing the
    pool OBJECT also keeps the substitution per service: arr.py holds one pool per
    service on purpose, and a double installed for Sonarr must not be able to
    answer a call Radarr made.
    """
    recorder: RecordingClient

    def client(self) -> RecordingClient:
        return self.recorder

    def gate(self):
        return contextlib.nullcontext()


@contextlib.contextmanager
def pooled(owner, attribute: str, client: RecordingClient, *, key: str | None = None):
    """Run the block with the pool at `owner.attribute` replaced by a fake.

    `key` names an entry when the attribute is a dict of pools (arr.py's
    per-service map) rather than a single one.
    """
    if key is None:
        with mock.patch.object(owner, attribute, FakePool(client)):
            yield client
    else:
        with mock.patch.dict(getattr(owner, attribute), {key: FakePool(client)}):
            yield client
