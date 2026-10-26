"""The tracker keys what a service tells it by the title store's key.

WHY THIS NEEDS SAYING: a service names a title by the ids it knows, and the
tracker files rows by the title store's answer — so a show Simkl knows only by its
tvdb id is the tracker's row filed under its tmdb id. Keyed by Simkl's ids alone,
that show's plays would fold into nothing and its library entry would read as "not
in the library"; a backup naming a key the store has since retired would restore
a second row beside the title's real one. Each test holds one of those three.
"""
from __future__ import annotations

import json

from app import db, metadata
from app.distrakt import backup, watch_history
from app.providers.base import LibraryEntry, LibraryRead, Media
from tests.distrakt.test_store import DistraktTestCase


async def known_title():
    """A show the store knows by both ids — as another service's listing taught it."""
    await metadata.ensure(Media.SHOW, {"tmdb": 900, "tvdb": 4321})


class PlaysAreKeyedByTheStoreTests(DistraktTestCase):
    async def test_a_play_naming_a_show_by_tvdb_alone_keys_as_its_tmdb_row(self):
        await known_title()
        events = [{"type": "episode", "show": {"title": "A Show", "ids": {"tvdb": 4321}},
                   "episode": {"season": 1, "number": 2}}]
        await watch_history._with_known_ids(events)
        self.assertEqual(str(watch_history._event_key(events[0]["show"], Media.SHOW)),
                         "show:tmdb:900")
        self.assertEqual(events[0]["show"]["ids"]["tvdb"], 4321)

    async def test_a_play_for_a_title_the_store_has_never_seen_is_left_alone(self):
        events = [{"type": "episode", "show": {"ids": {"tvdb": 77}},
                   "episode": {"season": 1, "number": 1}}]
        await watch_history._with_known_ids(events)
        self.assertEqual(events[0]["show"]["ids"], {"tvdb": 77})

    async def test_a_library_entry_naming_a_show_by_tvdb_alone_keys_as_its_tmdb_row(self):
        await known_title()
        read = LibraryRead(entries={"show:tvdb:4321": LibraryEntry({"tvdb": 4321, "simkl": 5}, {}),
                                    "show:tvdb:77": LibraryEntry({"tvdb": 77}, {})},
                           events=[], complete=True)
        keyed = await watch_history._keyed_by_store(read)
        self.assertEqual(set(keyed.entries), {"show:tmdb:900", "show:tvdb:77"})
        self.assertEqual(keyed.entries["show:tmdb:900"].ids["simkl"], 5)


class ARestoreLandsOnTheTitlesKeyTests(DistraktTestCase):
    async def test_a_backup_naming_a_retired_key_restores_under_the_current_one(self):
        await metadata.ensure(Media.SHOW, {"tvdb": 4321})
        await metadata.ensure(Media.SHOW, {"tvdb": 4321, "tmdb": 900},
                              filed_under="show:tvdb:4321")
        doc = {"schema": backup.EXPORT_SCHEMA, "distrakt_user_seasons": [{
            "kind": "keepup", "media": "show", "match_source": "tvdb", "match_id": "4321",
            "season": 1, "tvdb": 4321, "title": "A Show", "created_at": 1}]}
        await backup.restore_user_data(self.user_id, doc)
        row = await db.fetch_one("SELECT match_source, match_id, tmdb FROM distrakt_user_seasons")
        self.assertEqual(tuple(row), ("tmdb", "900", 900))

    async def test_a_restored_title_is_registered_with_the_store(self):
        doc = {"schema": backup.EXPORT_SCHEMA, "distrakt_user_seasons": [{
            "kind": "keepup", "media": "show", "match_source": "tmdb", "match_id": "1399",
            "season": 1, "tmdb": 1399, "trakt_id": 1390, "title": "Game of Thrones",
            "created_at": 1}]}
        await backup.restore_user_data(self.user_id, doc)
        ids = json.loads(await db.fetch_value(
            "SELECT ids_json FROM titles WHERE title_key = 'show:tmdb:1399'"))
        self.assertEqual(ids, {"tmdb": 1399, "trakt": 1390})
