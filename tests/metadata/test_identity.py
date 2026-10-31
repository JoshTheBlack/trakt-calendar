"""app/metadata/identity.py — which title a set of ids names, and how a key moves.

THE RULES, one per test: the waterfall picks a key; a lower id joins the ONE title
above it that holds it, and two titles claiming it are left apart; a title keyed
by a lower id the incoming ids state is merged in; a key only ever moves UP the
waterfall and two titles in the same id space never merge; a retired key is an
alias one hop long; and the per-service key a title with no shared id is filed
under is spelled the way the calendar and a card already spell it.

No network: nothing here asks a service.
"""
from __future__ import annotations

import json

from app import db
from app.calendar import cache as calendar_cache
from app.metadata import identity
from app.metadata import ids as id_rules
from app.providers.base import Media, Record, Source, render
from tests.support import DatabaseTestCase, migrated_db, statements_per_transaction


async def settle(media, ids, **kwargs):
    return await db.transaction(
        lambda conn: identity.settle(conn, media, ids, now=1000, **kwargs))


async def title_row(key):
    row = await db.fetch_one("SELECT media, ids_json FROM titles WHERE title_key = ?", (key,))
    return None if row is None else json.loads(row["ids_json"])


async def alias_of(key):
    return await db.fetch_value("SELECT title_key FROM title_aliases WHERE old_key = ?", (key,))


class TheWaterfallPicksTheKeyTests(DatabaseTestCase):
    async def test_the_first_shared_id_names_the_title(self):
        found = await settle(Media.SHOW, {"tvdb": "4321", "imdb": "tt1", "trakt": 7})
        self.assertEqual(found.key, "show:tvdb:4321")
        self.assertEqual(await title_row("show:tvdb:4321"),
                         {"tvdb": 4321, "imdb": "tt1", "trakt": 7})

    async def test_ids_are_stored_in_their_canonical_types(self):
        """The indexes compare json_extract's answer, and SQLite never finds the
        text '4321' equal to the integer 4321."""
        await settle(Media.SHOW, {"tmdb": "900", "tvdb": "4321", "slug": "x"})
        self.assertEqual(await title_row("show:tmdb:900"), {"tmdb": 900, "tvdb": 4321})

    async def test_ids_naming_nothing_and_no_key_answer_none(self):
        self.assertIsNone(await settle(Media.SHOW, {"trakt": 7}))

    async def test_a_title_with_no_shared_id_is_filed_under_its_service_key(self):
        found = await settle(Media.SHOW, {"simkl": 55}, fallback="simkl:55")
        self.assertEqual(found.key, "simkl:55")


class ALowerIdJoinsTheTitleAboveItTests(DatabaseTestCase):
    async def test_a_tvdb_listing_joins_the_one_tmdb_title_holding_that_tvdb_id(self):
        """A {tvdb} listing and a {tmdb, tvdb} title of one show are the same
        title — and the store says so without asking anybody."""
        await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321})
        found = await settle(Media.SHOW, {"tvdb": 4321, "simkl": 55})
        self.assertEqual(found.key, "show:tmdb:900")
        self.assertEqual(found.ids["simkl"], 55)

    async def test_two_titles_claiming_one_lower_id_are_not_chosen_between(self):
        """MEASURED: fifteen pairs of different TMDB titles share a tvdb, imdb or
        mal id on the development copy. A listing naming only that id is neither."""
        await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321})
        await settle(Media.SHOW, {"tmdb": 901, "tvdb": 4321})
        found = await settle(Media.SHOW, {"tvdb": 4321})
        self.assertEqual(found.key, "show:tvdb:4321")

    async def test_the_other_medium_is_another_title(self):
        await settle(Media.MOVIE, {"tmdb": 900, "imdb": "tt9"})
        found = await settle(Media.SHOW, {"imdb": "tt9"})
        self.assertEqual(found.key, "show:imdb:tt9")


class AKeyMovesUpOnceEverywhereTests(DatabaseTestCase):
    async def test_a_title_learning_a_higher_id_is_re_keyed_and_the_old_key_aliased(self):
        await settle(Media.SHOW, {"tvdb": 4321, "simkl": 55})
        found = await settle(Media.SHOW, {"tvdb": 4321, "tmdb": 900},
                             filed_under="show:tvdb:4321")
        self.assertEqual(found.key, "show:tmdb:900")
        self.assertEqual(found.ids["simkl"], 55)
        self.assertIsNone(await title_row("show:tvdb:4321"))
        self.assertEqual(await alias_of("show:tvdb:4321"), "show:tmdb:900")

    async def test_ids_that_state_a_lower_keyed_titles_id_merge_it_in(self):
        """Another service names the show by both ids: the title filed under the
        lower one is this title."""
        await settle(Media.SHOW, {"tvdb": 4321, "simkl": 55})
        found = await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321, "trakt": 7})
        self.assertEqual(found.key, "show:tmdb:900")
        self.assertEqual((found.ids["simkl"], found.ids["trakt"]), (55, 7))
        self.assertEqual(await alias_of("show:tvdb:4321"), "show:tmdb:900")

    async def test_a_lower_keyed_title_two_higher_titles_claim_is_left_alone(self):
        await settle(Media.SHOW, {"tvdb": 4321})
        await settle(Media.SHOW, {"tmdb": 901, "tvdb": 4321})
        # The first higher title absorbed it; a second claiming the same id later
        # does not take it from the first.
        await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321})
        self.assertEqual(await alias_of("show:tvdb:4321"), "show:tmdb:901")
        self.assertIsNotNone(await title_row("show:tmdb:900"))

    async def test_two_tmdb_titles_never_merge_whatever_else_they_share(self):
        await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321})
        found = await settle(Media.SHOW, {"tmdb": 901, "tvdb": 4321})
        self.assertEqual(found.key, "show:tmdb:901")
        self.assertIsNotNone(await title_row("show:tmdb:900"))
        self.assertIsNone(await alias_of("show:tmdb:900"))

    async def test_a_key_never_moves_down(self):
        """A row filed under tmdb, handed ids that lack it, stays where it is."""
        await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321})
        found = await settle(Media.SHOW, {"tvdb": 4321}, filed_under="show:tmdb:900")
        self.assertEqual(found.key, "show:tmdb:900")

    async def test_a_filing_the_ids_contradict_is_not_evidence(self):
        """A service that now names the title by a different tvdb id is telling
        the row something new; the old filing is not added to it."""
        await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321})
        found = await settle(Media.SHOW, {"tvdb": 9999}, filed_under="show:tmdb:900")
        self.assertEqual(found.key, "show:tvdb:9999")

    async def test_a_per_service_key_moves_when_its_title_learns_a_shared_id(self):
        await settle(Media.SHOW, {"simkl": 55}, fallback="simkl:55")
        found = await settle(Media.SHOW, {"simkl": 55, "tvdb": 4321},
                             filed_under="simkl:55", fallback="simkl:55")
        self.assertEqual(found.key, "show:tvdb:4321")
        self.assertEqual(await alias_of("simkl:55"), "show:tvdb:4321")

    async def test_an_unregistered_per_service_key_is_not_aliased(self):
        """Every listing has a per-service key; only one a title was registered
        under is held by anything, so only that one moves."""
        await settle(Media.SHOW, {"tmdb": 900, "trakt": 7}, fallback="trakt:the-show")
        self.assertIsNone(await alias_of("trakt:the-show"))

    async def test_an_alias_is_one_hop_after_a_second_re_key(self):
        await settle(Media.SHOW, {"imdb": "tt1"})
        await settle(Media.SHOW, {"imdb": "tt1", "tvdb": 4321}, filed_under="show:imdb:tt1")
        await settle(Media.SHOW, {"tvdb": 4321, "tmdb": 900}, filed_under="show:tvdb:4321")
        self.assertEqual(await alias_of("show:imdb:tt1"), "show:tmdb:900")
        self.assertEqual(await alias_of("show:tvdb:4321"), "show:tmdb:900")

    async def test_settling_what_is_already_known_writes_nothing(self):
        """The commonest case by far — a refill naming a title it named yesterday —
        and a write per listing would be a write per airing."""
        def _twice(conn) -> int:
            identity.settle(conn, Media.SHOW, {"tmdb": 900, "tvdb": 4321}, now=1000)
            before = conn.total_changes
            identity.settle(conn, Media.SHOW, {"tmdb": 900, "tvdb": 4321}, now=2000)
            return conn.total_changes - before

        self.assertEqual(await db.run(_twice), 0)


class ReadingWithoutWritingTests(DatabaseTestCase):
    async def test_complete_adds_the_titles_shared_ids_beneath_the_services_own(self):
        await settle(Media.SHOW, {"tmdb": 900, "tvdb": 4321, "imdb": "tt1", "trakt": 7})
        done, = await identity.completed([(Media.SHOW, {"tvdb": 4321, "simkl": 55,
                                                        "imdb": "tt-own"})])
        self.assertEqual(done, {"tvdb": 4321, "simkl": 55, "imdb": "tt-own", "tmdb": 900})

    async def test_complete_follows_an_alias(self):
        await settle(Media.SHOW, {"tvdb": 4321})
        await settle(Media.SHOW, {"tvdb": 4321, "tmdb": 900}, filed_under="show:tvdb:4321")
        done, = await identity.completed([(Media.SHOW, {"tvdb": 4321})])
        self.assertEqual(done["tmdb"], 900)

    async def test_resolve_decides_and_writes_nothing(self):
        key = await db.run(lambda conn: identity.resolve(conn, Media.SHOW, {"tvdb": 1}))
        self.assertEqual(key, "show:tvdb:1")
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM titles"), 0)

    async def test_every_id_adds_the_services_own_ids_too(self):
        """Where `complete` adds only the shared ids, `every_id` is for a reader
        that only addresses a service — an outbound button — and adds every id
        the store knows, the given ids winning."""
        await settle(Media.SHOW, {"tmdb": 900, "trakt": 7, "trakt_slug": "seven",
                                  "simkl": 55})
        found = await identity.every_id(Media.SHOW, {"tmdb": "900", "simkl": 56})
        self.assertEqual(found, {"tmdb": "900", "trakt": 7, "trakt_slug": "seven",
                                 "simkl": 56})

    async def test_every_id_finds_the_title_by_a_key_followed_through_its_re_key(self):
        await settle(Media.SHOW, {"tvdb": 4321, "simkl": 55})
        await settle(Media.SHOW, {"tvdb": 4321, "tmdb": 900, "trakt": 7},
                     filed_under="show:tvdb:4321")
        found = await identity.every_id(Media.SHOW, {"simkl": 55}, key="show:tvdb:4321")
        self.assertEqual(found["trakt"], 7)
        self.assertEqual(found["tmdb"], 900)

    async def test_every_id_of_a_title_the_store_does_not_hold_is_what_was_given(self):
        self.assertEqual(await identity.every_id(Media.SHOW, {"tmdb": 1, "slug": ""}),
                         {"tmdb": 1})
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM titles"), 0)


class OneSpellingTests(DatabaseTestCase):
    async def test_the_per_service_key_is_the_calendars_and_the_cards(self):
        """A title with no shared id is named by its one listing; the title store,
        the calendar's grouping and a card's mark must spell that the same way or
        a re-key could not find what it is moving."""
        record = Record(source=Source.SIMKL, media=Media.SHOW, id="55", ids={"simkl": 55},
                        detail_url="", title="Anime", air_ts=1783540800.0)
        key = identity.per_source_key(record.source, record.id)
        self.assertEqual(calendar_cache.group_base(record), key)
        from zoneinfo import ZoneInfo
        self.assertEqual(render(record, ZoneInfo("UTC")).mark_key, key)

    def test_canonical_ids_drop_the_bare_slug_and_type_the_numbers(self):
        self.assertEqual(
            id_rules.canonical({"slug": "the-rookie", "trakt_slug": "the-rookie-2018",
                                "tmdb": "79744", "tvdb": 350665, "imdb": "tt7587890",
                                "unknown": 1}),
            {"trakt_slug": "the-rookie-2018", "tmdb": 79744, "tvdb": 350665,
             "imdb": "tt7587890"})


# A batch holding every way one title of it can depend on another: a re-key of
# an earlier title (2 of 1), a lower id joining a title registered by the batch
# (4 to 3), two titles sharing a lower id (5, 6) and a third left apart by it (7),
# a per-service key re-keyed (9 of 8), a mal id joining a tmdb title (11 to 10),
# and a row filed under a key the batch retired (12).
BATCH = [
    identity.Settling("show", {"tvdb": 5}),
    identity.Settling("show", {"tmdb": 9, "tvdb": 5}),
    identity.Settling("show", {"tvdb": 6, "imdb": "tt6"}),
    identity.Settling("show", {"imdb": "tt6"}),
    identity.Settling("show", {"tmdb": 10, "tvdb": 7}),
    identity.Settling("show", {"tmdb": 11, "tvdb": 7}),
    identity.Settling("show", {"tvdb": 7}),
    identity.Settling("show", {"simkl": 55}, fallback="simkl:55"),
    identity.Settling("show", {"simkl": 55, "tvdb": 8}, fallback="simkl:55"),
    identity.Settling("show", {"tmdb": 12, "mal": 3}),
    identity.Settling("show", {"mal": 3}),
    identity.Settling("show", {"imdb": "tt99"}, filed_under="show:tvdb:5"),
]


async def identity_tables() -> tuple[list, list]:
    titles = [tuple(row) for row in await db.fetch_all(
        "SELECT title_key, media, ids_json, created_at FROM titles ORDER BY title_key")]
    aliases = [tuple(row) for row in await db.fetch_all(
        "SELECT old_key, title_key FROM title_aliases ORDER BY old_key")]
    return titles, aliases


class ABatchSettlesAsIfOneAtATimeTests(DatabaseTestCase):
    """identity.settle_many decides each title against what the ones before it
    decided — rows it registered, keys it retired — without writing until the
    end, so a fill's identity costs a fixed number of statements."""

    async def test_a_batch_leaves_the_tables_settling_each_in_turn_leaves(self):
        one_at_a_time = [await settle(title.media, title.ids, filed_under=title.filed_under,
                                      fallback=title.fallback) for title in BATCH]
        expected = await identity_tables()
        followed = [None if found is None else await db.run(
            lambda conn, key=found.key: identity.follow(conn, key)) for found in one_at_a_time]
        migrated_db("batch")
        batch = await db.transaction(lambda conn: identity.settle_many(conn, BATCH, now=1000))
        self.assertEqual(await identity_tables(), expected)
        # EVERY ANSWER IS THE KEY ITS TITLE HAS ONCE THE BATCH IS DONE — what each
        # one-at-a-time answer follows to now.
        self.assertEqual([None if found is None else found.key for found in batch], followed)

    async def test_a_title_re_keyed_later_in_the_batch_answers_with_its_new_key(self):
        first, second = await db.transaction(lambda conn: identity.settle_many(
            conn, BATCH[:2], now=1000))
        self.assertEqual((first.key, second.key), ("show:tmdb:9", "show:tmdb:9"))
        self.assertEqual(first.ids, {"tmdb": 9, "tvdb": 5})
        self.assertEqual(await alias_of("show:tvdb:5"), "show:tmdb:9")

    async def test_thirty_titles_cost_what_three_do(self):
        def titles(ids):
            return [identity.Settling("show", {"tmdb": n, "tvdb": 10_000 + n, "imdb": f"tt{n}"})
                    for n in ids]

        with statements_per_transaction() as few:
            await db.transaction(lambda conn: identity.settle_many(conn, titles(range(1, 4)),
                                                                   now=1000))
        with statements_per_transaction() as many:
            await db.transaction(lambda conn: identity.settle_many(conn, titles(range(4, 34)),
                                                                   now=1000))
        self.assertEqual(few, many)
