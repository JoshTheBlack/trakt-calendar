"""Direct tests for the outbound-network guard in tests/conftest.py.

A guard that silently stops engaging is worse than no guard at all: every test
it was meant to protect goes on passing, and the suite quietly returns to
depending on whether the machine running it has a network. These pin both
halves of the deal — remote is refused, loopback is not.
"""
from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest

from tests.conftest import OutboundNetworkBlocked


def test_a_connection_off_this_machine_is_refused(no_outbound_network):
    """203.0.113.0/24 is TEST-NET-3, reserved for documentation, so this asserts
    the refusal without depending on anything out there being unreachable."""
    with socket.socket() as sock, pytest.raises(OutboundNetworkBlocked) as caught:
        sock.connect(("203.0.113.1", 80))

    # The address is in the message because it is what names the escaped call.
    assert "203.0.113.1" in str(caught.value)
    assert no_outbound_network == ["('203.0.113.1', 80)"]
    # This test tripped the guard deliberately; the teardown assertion is for
    # tests that did not mean to.
    no_outbound_network.clear()


def test_connect_ex_is_guarded_too(no_outbound_network):
    """The error-returning sibling of connect(). Left open it would be a way
    around the guard that reports a failure code rather than raising."""
    with socket.socket() as sock, pytest.raises(OutboundNetworkBlocked):
        sock.connect_ex(("203.0.113.1", 80))
    no_outbound_network.clear()


def test_an_async_httpx_request_is_caught(no_outbound_network):
    """The shape that actually matters: the app's provider calls are async httpx
    over anyio, several layers above the socket. The assertion is on what the
    guard RECORDED rather than on what escaped the client, because httpx maps
    connection failures into its own exception type — and recording is what
    makes the teardown fail a test that swallowed the error, which is exactly
    how an unpatched call hid before.
    """
    async def _attempt():
        async with httpx.AsyncClient(timeout=5) as client:
            await client.get("http://203.0.113.1/calendars/all/shows/new")

    with pytest.raises(Exception):
        asyncio.run(_attempt())

    assert no_outbound_network == ["('203.0.113.1', 80)"]
    no_outbound_network.clear()


def test_a_call_made_by_name_reports_the_host(no_outbound_network):
    """A hostname is refused at resolution, which is what puts the provider's
    own name in the report — "api.trakt.tv" says which patch is wrong in a way
    an IP address never does."""
    with pytest.raises(OutboundNetworkBlocked) as caught:
        socket.getaddrinfo("api.trakt.tv", 443)

    assert "api.trakt.tv" in str(caught.value)
    assert no_outbound_network == ["api.trakt.tv"]
    no_outbound_network.clear()


def test_loopback_still_connects():
    """Loopback must stay open. asyncio wakes its own event loop through a
    127.0.0.1 socket pair on Windows, so a guard that blocked this would take
    down every async test in the suite rather than the outbound calls it is
    about."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        accepted: list[socket.socket] = []
        thread = threading.Thread(target=lambda: accepted.append(listener.accept()[0]))
        thread.start()
        with socket.socket() as client:
            client.connect(listener.getsockname())
        thread.join(timeout=5)

    assert accepted, "the loopback connection never arrived"
    accepted[0].close()
