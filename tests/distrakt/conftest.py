"""What the services know about a title BEYOND what a test hands the tracker:
nothing, unless the test says otherwise.

Every way onto the tracker now goes through app/distrakt/arrival.py's `describe`,
which asks the other service for a title by shared id and asks the describing
service for its title and network. A test about something else — a rewatch, a
month's placement, a backup — should not have to stand those in one by one, and
must not reach the network (tests/conftest.py fails any test that does).

So by default both lookups answer "nothing more": no id found by shared id, no
title or network stated. `describe` then keeps the path's own values, which is
exactly what those tests were written against. A test ABOUT arrival replaces
these with its own answers — the ports delegate through their modules, so
patching the same names inside a test wins over this.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.providers.base import TitleFacts

_NOTHING_MORE = {
    "app.providers.trakt.detail.find_by_shared_id": AsyncMock(return_value={}),
    "app.providers.simkl.by_id.find_by_shared_id": AsyncMock(return_value={}),
    "app.providers.trakt.detail.fetch_title_facts": AsyncMock(return_value=TitleFacts("", "")),
    "app.providers.simkl.detail.fetch_title_facts": AsyncMock(return_value=TitleFacts("", "")),
}


@pytest.fixture(autouse=True)
def services_know_nothing_more(monkeypatch):
    for target, stand_in in _NOTHING_MORE.items():
        module, name = target.rsplit(".", 1)
        monkeypatch.setattr(f"{module}.{name}", AsyncMock(
            return_value=stand_in.return_value))
