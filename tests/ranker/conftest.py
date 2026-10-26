"""What the services know about a title BEYOND what a test hands a board:
nothing, unless the test says otherwise.

A title added to a board from a search is registered with the title store, which
asks each service the title has no id from by shared id (metadata.ensure). A test
about something else — a season row, a drag, a restore — should not have to stand
those lookups in one by one, and must not reach the network (tests/conftest.py
fails any test that does). So by default every service answers "not listed", and
the board keeps the ids the result carried — what those tests were written
against. A test ABOUT the lookup replaces these with its own answers: the ports
delegate through their modules, so patching the same names inside a test wins.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

_NOT_LISTED = (
    "app.providers.trakt.detail.find_by_shared_id",
    "app.providers.simkl.by_id.find_by_shared_id",
    "app.providers.tmdb.search.find_by_shared_id",
)


@pytest.fixture(autouse=True)
def services_list_nothing_more(monkeypatch):
    for target in _NOT_LISTED:
        module, name = target.rsplit(".", 1)
        monkeypatch.setattr(f"{module}.{name}", AsyncMock(return_value={}))
