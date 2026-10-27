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

AND A HAND ADD WRITES NO PREMIERE INTO THE CALENDAR unless a test says so:
arrival.write_missing_premieres asks the service through the calendar search's
fill_one_gap, which answers "" (nothing written) here by default.

AND NO SERVICE HAS A PICTURE FOR A TITLE NOTHING HAS RECORDED ONE FOR: a month
gives each such row its one poster lookup before it draws, and that lookup's
Trakt search answers nothing here — so the title is recorded as having none, the
path a real empty answer takes.

AND A ROW A TEST WRITES STRAIGHT INTO THE TRACKER ARRIVED DESCRIBED. A row on the
viewer's own list keeps no words of its own: it is drawn with the title store's
words for its title (app/distrakt/store.py's _SNAPSHOT_COLUMNS), which arrival
files when the title arrives. A test that writes a row with
`store.add_user_record` and a title is standing in for that arrival, so the words
it states seed the title store (app/metadata/descriptions.py's seed_words —
nothing is written where the store already has words for the title). A test
ABOUT where a row's words come from writes the store itself.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import db, distrakt
from app.distrakt import store as distrakt_store
from app.metadata import descriptions
from app.providers.base import TitleFacts

_NOTHING_MORE = {
    "app.providers.trakt.detail.find_by_shared_id": AsyncMock(return_value={}),
    "app.providers.simkl.by_id.find_by_shared_id": AsyncMock(return_value={}),
    "app.providers.tmdb.search.find_by_shared_id": AsyncMock(return_value={}),
    "app.providers.trakt.detail.fetch_title_facts": AsyncMock(return_value=TitleFacts("", "")),
    "app.providers.simkl.detail.fetch_title_facts": AsyncMock(return_value=TitleFacts("", "")),
    "app.calendar.search.fill_one_gap": AsyncMock(return_value=""),
}

# Each service's poster lookup by tmdb id answers nothing, entry by entry.
_NO_PICTURE_FROM = ("tmdb", "trakt")


@pytest.fixture(autouse=True)
def services_know_nothing_more(monkeypatch):
    for target, stand_in in _NOTHING_MORE.items():
        module, name = target.rsplit(".", 1)
        monkeypatch.setattr(f"{module}.{name}", AsyncMock(
            return_value=stand_in.return_value))
    from app.media import posters
    for source in _NO_PICTURE_FROM:
        monkeypatch.setitem(posters._ADDRESS_LOOKUPS, source, AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def a_row_written_by_a_test_arrived_described(monkeypatch):
    """See the module docstring's last paragraph."""
    written_straight = distrakt_store.add_user_record

    async def add_user_record(user_id, record):
        stored = await written_straight(user_id, record)
        ids = {**(record.get("ids") or {}), **(stored.get("ids") or {})}
        seeded = await db.transaction(lambda conn: descriptions.seed_words(
            conn, stored["media"], ids, title=str(record.get("title") or ""),
            network=str(record.get("network") or ""), now=db.now()))
        if seeded is None:
            return stored
        return await distrakt_store.find_user_record(
            user_id, distrakt_store.record_key(stored), stored["season"])

    # Both spellings a test reaches it by: the store module, and the package that
    # re-exports it (`from app import distrakt`).
    monkeypatch.setattr(distrakt_store, "add_user_record", add_user_record)
    monkeypatch.setattr(distrakt, "add_user_record", add_user_record)
