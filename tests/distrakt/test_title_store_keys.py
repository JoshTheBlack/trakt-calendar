"""The tracker keys what a service tells it as the tracker files that title.

WHY THIS NEEDS SAYING: a service names a title by the ids it knows, and the
tracker files rows by the title store's answer — so a show Simkl knows only by its
tvdb id is the tracker's row filed under its tmdb id. Keyed by Simkl's ids alone,
that show's plays would fold into nothing and its library entry would read as "not
in the library"; a backup naming a key the store has since retired would restore
a second row beside the title's real one.

AND A SERVICE'S OWN ID COMES FIRST. Which TMDB title a service says its show is,
is a pointer it can move: Simkl moved The Great British Bake Off's (Simkl 21506,
one show of seventeen series) from TMDB's Channel 4 title to its BBC one, and the
season the tracker held under the first stopped counting while its plays came back
as a question about a show already on the page.
"""
from __future__ import annotations

import json
from zoneinfo import ZoneInfo

from app import clock, db, metadata
from app.distrakt import backup, lifecycle, store, watch_history
from app.providers.base import ItemKey, LibraryEntry, LibraryRead, Media
from tests.distrakt.test_store import DistraktTestCase, a_record
from tests.support import statements_per_transaction

TRACKED = "show:tmdb:87012"


async def known_title():
    """A show the store knows by both ids — as another service's listing taught it."""
    await metadata.ensure(Media.SHOW, {"tmdb": 900, "tvdb": 4321})


def _state(*keys: str, simkl: int = 21506) -> dict:
    """Watch state holding each of `keys` by the one Simkl id."""
    return {"shows": {key: {"ids": {"simkl": simkl}, "seasons": {}} for key in keys},
            "movies": {}}


def _simkl_play(number: int = 5) -> dict:
    """A Simkl play after Simkl moved its pointer: its own id is the tracked
    title's, its tmdb id is the other TMDB title."""
    return {"type": "episode", "watched_at": "2026-10-25T20:12:07Z",
            "show": {"title": "The Great British Bake Off",
                     "ids": {"simkl": 21506, "tmdb": 34549, "tvdb": 184871}},
            "episode": {"season": 17, "number": number}}


class PlaysAreKeyedByTheStoreTests(DistraktTestCase):
    async def test_a_play_naming_a_show_by_tvdb_alone_keys_as_its_tmdb_row(self):
        await known_title()
        events = [{"type": "episode", "show": {"title": "A Show", "ids": {"tvdb": 4321}},
                   "episode": {"season": 1, "number": 2}}]
        await watch_history._keyed_as_tracked(events, _state(), "simkl")
        self.assertEqual(str(watch_history._event_key(events[0]["show"], Media.SHOW)),
                         "show:tmdb:900")
        self.assertEqual(events[0]["show"]["ids"]["tvdb"], 4321)

    async def test_a_play_for_a_title_the_store_has_never_seen_is_left_alone(self):
        events = [{"type": "episode", "show": {"ids": {"tvdb": 77}},
                   "episode": {"season": 1, "number": 1}}]
        await watch_history._keyed_as_tracked(events, _state(), "simkl")
        self.assertEqual(events[0]["show"]["ids"], {"tvdb": 77})

    async def test_a_library_entry_naming_a_show_by_tvdb_alone_keys_as_its_tmdb_row(self):
        await known_title()
        read = LibraryRead(entries={"show:tvdb:4321": LibraryEntry({"tvdb": 4321, "simkl": 5}, {}),
                                    "show:tvdb:77": LibraryEntry({"tvdb": 77}, {})},
                           events=[], complete=True)
        keyed = await watch_history._keyed_as_tracked_library(read, {}, "simkl")
        self.assertEqual(set(keyed.entries), {"show:tmdb:900", "show:tvdb:77"})
        self.assertEqual(keyed.entries["show:tmdb:900"].ids["simkl"], 5)


class AServicesOwnIdPlacesItsPlaysTests(DistraktTestCase):
    async def test_a_play_keeps_to_the_title_the_tracker_holds_by_its_services_id(self):
        events = [_simkl_play()]
        await watch_history._keyed_as_tracked(events, _state(TRACKED), "simkl")
        play = watch_history._episode_play(events[0])
        self.assertEqual(str(play.key), TRACKED)
        # What the service said is kept as it said it; only the filing moved.
        self.assertEqual(play.ids["tmdb"], 34549)

    async def test_only_the_reporting_services_own_id_places_it(self):
        """A Trakt pass places by Trakt's id; a Simkl id on its payload is not
        Trakt's statement about its own title."""
        events = [_simkl_play()]
        await watch_history._keyed_as_tracked(events, _state(TRACKED), "trakt")
        self.assertEqual(str(watch_history._episode_play(events[0]).key), "show:tmdb:34549")

    async def test_an_id_two_tracked_titles_hold_places_nothing(self):
        events = [_simkl_play()]
        await watch_history._keyed_as_tracked(
            events, _state(TRACKED, "show:tmdb:34549000"), "simkl")
        self.assertEqual(str(watch_history._episode_play(events[0]).key), "show:tmdb:34549")

    async def test_a_library_entry_keeps_to_the_title_the_tracker_holds_by_its_services_id(self):
        """And it wins the key over an entry whose shared ids merely land there."""
        read = LibraryRead(entries={
            "show:tmdb:34549": LibraryEntry({"simkl": 21506, "tmdb": 34549},
                                            {17: {5: "2026-10-25T20:12:07Z"}}),
            "show:tmdb:87012": LibraryEntry({"simkl": 999, "tmdb": 87012}, {}),
        }, events=[], complete=True)
        keyed = await watch_history._keyed_as_tracked_library(read, _state(TRACKED)["shows"],
                                                              "simkl")
        self.assertEqual(set(keyed.entries), {TRACKED})
        self.assertEqual(keyed.entries[TRACKED].ids["simkl"], 21506)

    async def test_a_question_stored_under_the_services_old_pointer_is_spent(self):
        """The season is on the list under the tracked key; the question about
        it was stored under the title Simkl's shared ids named. Judged where the
        tracker holds it, it is no question at all."""
        await store.add_user_record(self.user_id, a_record(
            87012, season=17, kind=store.RecordKind.KEEPUP,
            ids={"tmdb": 87012, "simkl": 21506}, title="The Great British Bake Off"))
        state = await watch_history.load_state(self.user_id)
        watch_history._set_show_baseline(state, TRACKED, {"simkl": 21506},
                                         {17: {1: "2026-09-23T01:29:27Z"}}, "simkl")
        await watch_history._save(self.user_id, state)
        await store.open_prompt(self.user_id, ItemKey("show", "tmdb", "34549"), 17, 5,
                                "The Great British Bake Off",
                                {"simkl": 21506, "tmdb": 34549}, "2026-10-25T20:12:07Z")
        today = clock.today()
        questions = await lifecycle.standing_questions(
            self.user_id, month=store.month_key(today.year, today.month), tz=ZoneInfo("UTC"))
        self.assertEqual(questions.unknown, [])
        self.assertEqual(await store.open_prompts(self.user_id), [])


async def _titles() -> dict[str, dict]:
    return {row["title_key"]: json.loads(row["ids_json"]) for row in
            await db.fetch_all("SELECT title_key, ids_json FROM titles")}


class WhatTheTrackerWritesIsRegisteredTests(DistraktTestCase):
    """Every title a history save or a month's film list names is registered as
    it is written — a show watched and never tracked included."""

    async def test_a_history_save_registers_every_show_and_film_it_holds(self):
        state = {"shows": {"show:tmdb:100757": {"ids": {"tmdb": 100757, "trakt": 42},
                                                "seasons": {}}},
                 "movies": {"movie:tmdb:550": {"ids": {"tmdb": 550, "trakt": 9},
                                               "watched_at": "2026-10-01T20:00:00Z",
                                               "source": "trakt"}}}
        await watch_history._save(self.user_id, state)
        titles = await _titles()
        self.assertEqual(titles["show:tmdb:100757"], {"tmdb": 100757, "trakt": 42})
        self.assertEqual(titles["movie:tmdb:550"], {"tmdb": 550, "trakt": 9})

    async def test_a_saved_row_moves_with_a_title_that_answers_to_a_higher_key(self):
        """The store knows tvdb 4321 as tmdb 900; progress saved under the tvdb
        key lands on the title's key."""
        await known_title()
        await watch_history._save(self.user_id, {
            "shows": {"show:tvdb:4321": {"ids": {"tvdb": 4321, "tmdb": 900}, "seasons": {}}},
            "movies": {}})
        keys = {tuple(row) for row in await db.fetch_all(
            "SELECT match_source, match_id FROM distrakt_show_progress")}
        self.assertEqual(keys, {("tmdb", "900")})

    async def test_registering_a_history_costs_the_lock_no_more_for_more_titles(self):
        """A save holds the one write lock for a fixed number of statements
        however many titles it registers — ten shows or a hundred."""
        counts = []
        for many in (10, 100):
            state = {"shows": {f"show:tmdb:{n}": {"ids": {"tmdb": n}, "seasons": {}}
                               for n in range(1000 * many, 1000 * many + many)},
                     "movies": {}}
            with statements_per_transaction() as counted:
                await watch_history._save(self.user_id, state)
            counts.append(counted[-1])
        self.assertEqual(counts[0], counts[1])

    async def test_a_months_films_are_registered_by_both_writers(self):
        film = {"key": "movie:tmdb:603", "ids": {"tmdb": 603, "imdb": "tt0133093"},
                "title": "The Matrix", "year": 1999, "watched_at": "2026-09-02T20:00:00Z"}
        await store.save_month(self.user_id, {"month": "2026-09", "closed": True,
                                              "movies": [film], "shows": []})
        self.assertEqual((await _titles())["movie:tmdb:603"], {"tmdb": 603, "imdb": "tt0133093"})
        old_shape = {"trakt_id": 11, "title": "Before films had keys", "year": 1999}
        await store.set_month_movies(self.user_id, "2026-09", [
            old_shape, {**film, "key": "movie:tmdb:604", "ids": {"tmdb": 604}}])
        titles = await _titles()
        self.assertEqual(titles["movie:tmdb:604"], {"tmdb": 604})
        self.assertEqual(len(titles), 2, "an entry naming no key registers nothing")


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
