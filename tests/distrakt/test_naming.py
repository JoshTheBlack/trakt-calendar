"""app/distrakt/naming.py — teaching stored records what each service CALLS a
title, from calendar windows the instance has already paid for.

The two gaps this closes are both "a record no live pass visits": a settled
verdict, whose counts are decided once and never recomputed, and a title nobody
has played lately, since Trakt's slug rides history events. Both are exercised
here directly rather than through a sync, because going through a sync would
prove the sync reaches them — which is the thing that is not true.

No network: the calendar windows are written straight into the cache table, which
is exactly the state a fill leaves behind.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

from app import db, distrakt
from app.config import Settings
from app.calendar import cache as calendar_cache
from app.calendar import entries as calendar_entries
from app.metadata import rows as title_rows
from app.distrakt import naming, store
from app.providers.base import ItemKey, Media, Record, Source
from tests.distrakt.test_store import DistraktTestCase, a_record, month_back

SETTINGS = Settings(trakt_client_id="cid", simkl_client_id="scid")

# 2026-07-15 12:00Z, inside the span these are stored under.
_AIR_DAY = date(2026, 7, 15)


def _noon(day: date) -> float:
    """Midday UTC on `day`. An airing is filed under its own UTC date now, so a
    fixture dated outside the span it is seeded into is trimmed rather than
    stored — the timestamp and the span have to agree."""
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()


def a_calendar_record(source: str, ids: dict, *, media: str = "show") -> Record:
    """One source's record for the title these tests share, as a fill stores it."""
    return Record(
        source=Source(source), media=Media(media),
        id=str(ids.get(source) or source), ids=dict(ids), detail_url="",
        title="A Show", air_ts=_noon(_AIR_DAY), season=1, episode_number=1,
        episode_label="S01E01")


def named() -> list[Record]:
    """Both services listing one title, each naming it in its OWN slug namespace.

    They are one group because they share tmdb 900, which is also why the
    tracker's row — filed under tmdb — is reachable from either. Built fresh per
    call rather than shared as a constant: these are mutable records and a test
    that stored one must not be able to change what the next test seeds.
    """
    return [
        a_calendar_record("simkl", {"tmdb": 900, "simkl": 222, "simkl_slug": "a-show"}),
        a_calendar_record("trakt", {"tmdb": 900, "trakt": 111, "trakt_slug": "a-show-2019"}),
    ]


def legacy() -> list[Record]:
    """The same title as it was stored before the two slugs were told apart: each
    record carries only the ambiguous bare `slug`. Provenance is what makes it
    readable anyway — a record filed under `simkl` states Simkl's slug, by
    construction and with nothing to resolve."""
    return [
        a_calendar_record("simkl", {"tmdb": 900, "simkl": 222, "slug": "a-show"}),
        a_calendar_record("trakt", {"tmdb": 900, "trakt": 111, "slug": "a-show-2019"}),
    ]


async def store_calendar(records: list[Record], *, day: date = _AIR_DAY,
                         now: int | None = None, endpoint: str = "shows") -> None:
    """Put the calendar in the state a fill leaves it in.

    STRAIGHT TO `store_window`, because that is what a fill calls. This used to
    be an adapter that wrote pre-grouped entries: storage held finished groups,
    so a test could hand one over directly. Grouping happens at READ now, so
    seeding groups would be seeding a shape production never writes.
    """
    for record in records:
        record.air_ts = _noon(day)
    await calendar_cache.store_window(
        endpoint, calendar_cache.window_start(day), list(records), 3600,
        db.now() if now is None else now, sources=["trakt", "simkl"])


class FillingFromTheStoredCalendarTests(DistraktTestCase):
    def setUp(self):
        self.month = month_back(0)
        self.key = ItemKey("show", "tmdb", "900")

    async def _month_ids(self, kind=distrakt.RecordKind.SERIES_PREMIERE, season=1):
        rec = await distrakt.find_month_record(self.user_id, self.month, kind,
                                               self.key, season)
        return rec["ids"]

    async def test_a_settled_verdict_learns_both_slugs(self):
        """THE GAP THIS EXISTS FOR. A completed season is never recomputed, so the
        pass that teaches a listed title an id it was written without never sees
        it — and it could hold a service's id for years with no name to link by."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=1, kind=distrakt.RecordKind.COMPLETED,
            ids={"tmdb": 900, "simkl": 222, "trakt": 111}))
        await store_calendar(named())

        # Two names on one row: the return counts names written, not rows.
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 2)
        ids = await self._month_ids(distrakt.RecordKind.COMPLETED)
        self.assertEqual(ids["simkl_slug"], "a-show")
        self.assertEqual(ids["trakt_slug"], "a-show-2019")

    async def test_a_window_stored_before_the_slugs_were_told_apart_still_pays(self):
        """MEASURED, NOT ASSUMED: every window on the author's live instance
        carries only the shared key, so an index built off the group's merged
        `ids` would find nothing in any of them until each expired and refilled.
        Each source's own record says whose slug it is, and always did."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=1, kind=distrakt.RecordKind.COMPLETED,
            ids={"tmdb": 900, "simkl": 222, "trakt": 111}))
        await store_calendar(legacy())

        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 2)
        ids = await self._month_ids(distrakt.RecordKind.COMPLETED)
        self.assertEqual(ids["simkl_slug"], "a-show")
        self.assertEqual(ids["trakt_slug"], "a-show-2019")

    # THE AMBIGUOUS-MERGED-SLUG TEST IS GONE, AND ITS SUBJECT WITH IT. It seeded
    # a group whose hoisted ids carried a bare `slug` with NO per-source record to
    # attribute it to, and pinned that nothing was learned from it. That group is
    # now unrepresentable: hoisted ids are the UNION of the records in a group, so
    # every hoisted slug came from a record, and every record states its own
    # source. The ambiguity the rule guarded against cannot be constructed.
    #
    # What survives of it is that a bare `slug` is read as belonging to the source
    # whose record carries it, which is exactly what the legacy test above pins.

    async def test_a_shared_id_is_added_beside_the_filing_id_never_in_its_place(self):
        """A calendar match may ADD a shared id the row lacks — a history row
        filed before the whole id map was kept had tmdb and nothing else, while
        the same title arriving any other way carried tvdb and imdb too. What it
        may never do is touch the id the row is FILED under: that addresses the
        row, and a match arriving there would be a re-identification wearing a
        repair's clothes. So the row gains imdb and stays exactly where it was,
        and an id it already holds is never replaced by the calendar's."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222, "tvdb": 1}))
        await store_calendar([
            a_calendar_record("simkl", {"tmdb": 900, "simkl": 222,
                                        "simkl_slug": "a-show", "tvdb": 2,
                                        "imdb": "tt-from-calendar"}),
        ])

        await naming.fill_from_calendar(self.user_id, SETTINGS)
        ids = await self._month_ids()
        self.assertEqual(ids["imdb"], "tt-from-calendar")
        self.assertEqual(ids["tvdb"], 1, "a held id was replaced")
        self.assertEqual(ids["tmdb"], 900)
        self.assertEqual(await distrakt.find_month_record(
            self.user_id, self.month, distrakt.RecordKind.SERIES_PREMIERE,
            ItemKey("show", "tmdb", "900"), 1) is not None, True)

    async def test_rows_of_one_title_agree_on_what_any_of_them_holds(self):
        """A settled row filed before its sibling learned an id is given it: one
        title's rows are one title's ids, whichever row learned them first."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=1, kind=distrakt.RecordKind.COMPLETED, ids={"tmdb": 900}))
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=2, ids={"tmdb": 900, "trakt": 111, "imdb": "tt9"}, network="HBO"))

        await naming.fill_from_calendar(self.user_id, SETTINGS)
        settled = await distrakt.find_month_record(
            self.user_id, self.month, distrakt.RecordKind.COMPLETED,
            ItemKey("show", "tmdb", "900"), 1)
        self.assertEqual(settled["ids"]["trakt"], 111)
        self.assertEqual(settled["ids"]["imdb"], "tt9")
        self.assertEqual(settled["network"], "HBO")

    async def test_it_never_overwrites_a_slug_the_record_already_carries(self):
        """`store.learn_ids` owns this rule and this pins that the caller has not
        found a way around it. A stored slug came off the payload the record was
        built from; this one came off a cross-service match."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222, "simkl_slug": "already-known"}))
        await store_calendar(named())

        await naming.fill_from_calendar(self.user_id, SETTINGS)
        self.assertEqual((await self._month_ids())["simkl_slug"], "already-known")

    async def test_a_row_filed_off_one_service_learns_the_others_id_and_name_together(self):
        """THE DEFECT: a row keeps whatever ids existed the day it was written,
        and one filed off Simkl alone stayed without Trakt's id for ever although
        the stored calendar held Trakt's own record of the same title — its
        ticks for Trakt dead text, its Trakt button absent. The id is learned from
        that record, matched on the shared id the row is filed under, and its NAME
        comes with it in the same write: a name without the id would make the row
        look linkable to a service it could not ask, which is the refusal this
        used to make."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))  # filed before Trakt listed it
        await store_calendar(named())

        await naming.fill_from_calendar(self.user_id, SETTINGS)
        ids = await self._month_ids()
        self.assertEqual(ids["simkl_slug"], "a-show")
        self.assertEqual(ids["trakt"], 111)
        self.assertEqual(ids["trakt_slug"], "a-show-2019")

    async def test_no_name_is_written_for_a_service_whose_id_the_calendar_lacks(self):
        """The half of the old refusal that still holds: nothing is written for
        a service the stored calendar holds no record of this title from. Only
        Simkl's record is stored here, so the row learns nothing about Trakt —
        and still owes it, which is honest: the calendar may list it tomorrow."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        await store_calendar([named()[0]])  # Simkl's record only

        await naming.fill_from_calendar(self.user_id, SETTINGS)
        ids = await self._month_ids()
        self.assertNotIn("trakt", ids)
        self.assertNotIn("trakt_slug", ids)
        owed, = await store.identities_owing(self.user_id)
        self.assertIn("trakt", owed.names)

    async def test_a_row_stored_without_a_network_learns_the_calendars(self):
        """A history-built row carries none and nothing revisits a settled row.
        MEASURED on a copy of the owner's data: 103 rows filed blank, 93 of them
        answerable from the stored calendar."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            kind=distrakt.RecordKind.COMPLETED, ids={"tmdb": 900, "trakt": 111}))
        await store_calendar([replace(named()[1], network="The CW")])

        await naming.fill_from_calendar(self.user_id, SETTINGS)
        rec = await distrakt.find_month_record(
            self.user_id, self.month, distrakt.RecordKind.COMPLETED,
            ItemKey("show", "tmdb", "900"), 1)
        self.assertEqual(rec["network"], "The CW")

    async def test_a_network_already_stored_is_never_replaced(self):
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "trakt": 111}, network="HBO"))
        await store_calendar([replace(named()[1], network="The CW")])

        await naming.fill_from_calendar(self.user_id, SETTINGS)
        rec = await distrakt.find_month_record(
            self.user_id, self.month, distrakt.RecordKind.SERIES_PREMIERE,
            ItemKey("show", "tmdb", "900"), 1)
        self.assertEqual(rec["network"], "HBO")

    async def test_a_second_pass_writes_nothing(self):
        """It sits on an ordinary load, so once the work is done it has to cost
        nothing rather than rewrite what it already wrote."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222, "trakt": 111}))
        await store_calendar(named())

        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 2)
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 0)

    async def test_an_empty_calendar_cache_is_not_an_error(self):
        """A fresh instance has stored no window yet. Nothing is owed that can be
        paid, and the next load tries again once a window exists."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 0)

    async def test_a_title_the_calendar_does_not_name_is_left_alone(self):
        """An older show in no stored window keeps whatever it has. It must not
        pick up the slugs of whichever title happened to be in the cache."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            tmdb=555, ids={"tmdb": 555, "simkl": 999}))
        await store_calendar(named())

        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 0)
        rec = await distrakt.find_month_record(
            self.user_id, self.month, distrakt.RecordKind.SERIES_PREMIERE,
            ItemKey("show", "tmdb", "555"), 1)
        self.assertNotIn("simkl_slug", rec["ids"])


class NotLookingAgainForNothingTests(DistraktTestCase):
    """The signature guard. Some debt never settles — a title no stored window
    names is owed a name for as long as it is held — so "is anything outstanding"
    answers yes for ever on a real account, and without this the walk would run on
    every load to rediscover the same nothing.

    THE FAILURE THIS GUARDS AGAINST IS THE GUARD ITSELF suppressing real work, so
    both directions are pinned: it must skip when nothing moved, and it must not
    skip when either side did.
    """

    def setUp(self):
        self.month = month_back(0)
        self.walks = 0

    def _count_walks(self):
        """Count the calendar lookups without stubbing out what they return —
        the point is how OFTEN one happens, not what it finds.

        IT COUNTS THE KEYED READ NOW, not the whole-calendar walk it replaced.
        The walk was measured at 3.8 seconds to learn two names on a live
        instance; the guard these tests protect is what kept it from running on
        every load, and it still earns its keep against a keyed read — a query
        nobody needs is still a query.
        """
        real = title_rows.known_about

        async def counting(keys):
            self.walks += 1
            return await real(keys)

        naming.title_rows.known_about = counting
        self.addCleanup(setattr, naming.title_rows, "known_about", real)

    async def test_an_unpayable_debt_is_not_re_examined_every_pass(self):
        """A title the calendar has never named. The first pass has to look; the
        second has the same rows and the same windows, so it cannot find anything
        the first did not."""
        self._count_walks()
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            tmdb=555, ids={"tmdb": 555, "simkl": 999}))
        await store_calendar(named())

        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 0)
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 0)
        self.assertEqual(self.walks, 1)

    async def test_a_newly_stored_window_reopens_the_question(self):
        """The calendar moved, so a title that could not be named before may be
        nameable now. Skipping here would strand it until something else happened
        to change what is owed."""
        self._count_walks()
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        await store_calendar([])
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 0)

        await store_calendar(named(), day=date(2026, 8, 12), now=db.now() + 1)
        # Simkl's name, and Trakt's id with Trakt's name: three values.
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 3)
        self.assertEqual((await distrakt.find_month_record(
            self.user_id, self.month, distrakt.RecordKind.SERIES_PREMIERE,
            ItemKey("show", "tmdb", "900"), 1))["ids"]["simkl_slug"], "a-show")

    async def test_a_newly_added_title_reopens_the_question(self):
        """What is owed moved instead. A title added after a pass that found
        nothing must not inherit that pass's verdict — the window naming it was
        already stored, so the answer for THIS title was never asked."""
        self._count_walks()
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            tmdb=555, ids={"tmdb": 555, "simkl": 999}))
        await store_calendar(named())
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 0)

        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 3)
        self.assertEqual(self.walks, 2)

    async def test_one_viewers_settled_answer_does_not_silence_another(self):
        """The marker is per viewer because the debt is: the calendar half of the
        signature is shared, but what is outstanding is not."""
        self._count_walks()
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        await store_calendar(named())
        self.assertEqual(await naming.fill_from_calendar(self.user_id, SETTINGS), 3)

        other = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("someone-else", db.now(), db.now()))
        await distrakt.add_month_record(other.lastrowid, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await naming.fill_from_calendar(other.lastrowid, SETTINGS), 3)


class WhoIsOwedANameTests(DistraktTestCase):
    """store.identities_owing — the guard that decides whether any of the above
    is worth doing at all, and what each identity is short of."""

    ALL_IDS = {"trakt": 111, "trakt_slug": "t", "simkl": 222, "simkl_slug": "s",
               "tvdb": 7, "imdb": "tt7", "mal": 9}

    def setUp(self):
        self.month = month_back(0)

    async def test_it_answers_per_identity_rather_than_per_row(self):
        """An id, a name and a network are facts about the title, and the store
        writes every row of one at a stroke. Answering per row would have the
        caller ask the same question once per season."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=1, ids={"tmdb": 900, "simkl": 222}, network="Fuji TV"))
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=2, ids={"tmdb": 900, "simkl": 222}, network="Fuji TV"))
        self.assertEqual(await store.identities_owing(self.user_id), [store.Owed(
            ItemKey("show", "tmdb", "900"),
            ("imdb", "mal", "simkl_slug", "trakt", "trakt_slug", "tvdb"),
            {"simkl": 222, "tmdb": 900}, "Fuji TV")])

    async def test_a_service_id_is_owed_wherever_it_is_missing(self):
        """Not only the name. Owing names alone — only where the row already held
        that service's id — is what left a row filed off one service without the
        other's id for good. Both record tables, because a title can be listed,
        settled, or both."""
        await distrakt.add_user_record(self.user_id, a_record(
            season=3, kind=distrakt.RecordKind.KEEPUP, ids={"tmdb": 901, "trakt": 111},
            network="AMC"))
        owed, = await store.identities_owing(self.user_id)
        self.assertEqual(owed.key, ItemKey("show", "tmdb", "901"))
        self.assertEqual(owed.names,
                         ("imdb", "mal", "simkl", "simkl_slug", "trakt_slug", "tvdb"))
        self.assertEqual(owed.ids, {"trakt": 111, "tmdb": 901})

    async def test_an_identity_short_of_different_things_in_each_table_owes_the_union(self):
        """The two tables are asked separately and their answers merged, not
        concatenated — reporting it twice would have the caller write each half
        over the other's rows. What either table holds is merged the same way,
        since one title's rows are one title's ids."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=1, kind=distrakt.RecordKind.COMPLETED,
            ids={"tmdb": 900, "trakt": 111, "trakt_slug": "t"}))
        await distrakt.add_user_record(self.user_id, a_record(
            season=2, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 900, "simkl": 222, "simkl_slug": "s"}, network="HBO"))
        owed, = await store.identities_owing(self.user_id)
        self.assertEqual(owed.names, ("imdb", "mal", "network", "simkl", "simkl_slug",
                                      "trakt", "trakt_slug", "tvdb"))
        self.assertEqual(owed.ids, {"trakt": 111, "trakt_slug": "t", "simkl": 222,
                                    "simkl_slug": "s", "tmdb": 900})
        self.assertEqual(owed.network, "HBO")

    async def test_a_complete_record_owes_nothing(self):
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, **self.ALL_IDS}, network="HBO"))
        self.assertEqual(await store.identities_owing(self.user_id), [])

    async def test_another_viewers_rows_are_not_this_viewers_work(self):
        """The tables are shared and keyed by user. A sweep that answered for
        everybody would write another account's records."""
        other = await store.db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("someone-else", db.now(), db.now()))
        await distrakt.add_month_record(other.lastrowid, self.month,
                                        a_record(ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await store.identities_owing(self.user_id), [])
