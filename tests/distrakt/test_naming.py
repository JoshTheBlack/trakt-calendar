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

from datetime import date

from app import db, distrakt
from app.calendar import cache as calendar_cache
from app.distrakt import naming, store
from app.providers.base import ItemKey
from tests.distrakt.test_store import DistraktTestCase, a_record, month_back

# One stored calendar group in the shape group_records writes: the match result
# hoisted onto `ids`, and each source's record kept WHOLE under its own name.
# The slugs are read from the per-source records rather than the merge, because
# the merge cannot say whose a bare `slug` was.
GROUP = {
    "key": "k1",
    "ids": {"tmdb": 900, "simkl": 222, "simkl_slug": "a-show",
            "trakt": 111, "trakt_slug": "a-show-2019"},
    "by_source": {
        "simkl": {"media": "show", "ids": {"simkl": 222, "simkl_slug": "a-show"}},
        "trakt": {"media": "show", "ids": {"trakt": 111, "trakt_slug": "a-show-2019"}},
    },
}

# The same title as a window stored BEFORE the two slugs were told apart: each
# source's record carries only the ambiguous shared key. Provenance is what makes
# it readable anyway — a record filed under `simkl` states Simkl's slug.
LEGACY_GROUP = {
    "key": "k1",
    "ids": {"tmdb": 900, "simkl": 222, "trakt": 111, "slug": "a-show-2019"},
    "by_source": {
        "simkl": {"media": "show", "ids": {"simkl": 222, "slug": "a-show"}},
        "trakt": {"media": "show", "ids": {"trakt": 111, "slug": "a-show-2019"}},
    },
}


async def store_groups(groups: list[dict]) -> None:
    """Put groups in the calendar cache the way a fill does, so the read under
    test is reading real stored state rather than a stub."""
    await calendar_cache.store_groups("shows", date(2026, 7, 1), groups,
                                      ttl_seconds=3600, now=db.now())


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
        await store_groups([GROUP])

        # Two names on one row: the return counts names written, not rows.
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 2)
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
        await store_groups([LEGACY_GROUP])

        self.assertEqual(await naming.fill_from_calendar(self.user_id), 2)
        ids = await self._month_ids(distrakt.RecordKind.COMPLETED)
        self.assertEqual(ids["simkl_slug"], "a-show")
        self.assertEqual(ids["trakt_slug"], "a-show-2019")

    async def test_the_ambiguous_merged_slug_is_never_attributed_to_a_service(self):
        """The hoisted `slug` is first-writer-wins across sources, so for a title
        both services listed it belongs to whichever the declared order reached
        first — and nothing records which. A group with no per-source record to
        read it from teaches nothing rather than guessing."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222, "trakt": 111}))
        await store_groups([{"key": "k1", "by_source": {},
                             "ids": {"tmdb": 900, "simkl": 222, "trakt": 111,
                                     "slug": "whose-is-this"}}])

        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)
        ids = await self._month_ids()
        self.assertNotIn("simkl_slug", ids)
        self.assertNotIn("trakt_slug", ids)

    async def test_it_never_writes_a_shared_id_the_record_is_filed_under(self):
        """A calendar match may say what a title is CALLED on a service. It may
        not say what the title IS: the shared spaces are the identity waterfall,
        the record is filed under one of them, and a window's match arriving there
        would be a re-identification wearing a repair's clothes."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        await store_groups([{**GROUP, "ids": {**GROUP["ids"], "imdb": "tt-from-calendar"}}])

        await naming.fill_from_calendar(self.user_id)
        self.assertNotIn("imdb", await self._month_ids())

    async def test_it_never_overwrites_a_slug_the_record_already_carries(self):
        """`store.learn_ids` owns this rule and this pins that the caller has not
        found a way around it. A stored slug came off the payload the record was
        built from; this one came off a cross-service match."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222, "simkl_slug": "already-known"}))
        await store_groups([GROUP])

        await naming.fill_from_calendar(self.user_id)
        self.assertEqual((await self._month_ids())["simkl_slug"], "already-known")

    async def test_a_slug_is_owed_only_where_that_service_named_the_title(self):
        """Otherwise the guard could never reach zero: a Simkl-only title would
        owe a Trakt name for ever, nothing could ever settle the debt, and every
        pass would go on inflating every stored window to discover that again."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))  # no Trakt id: Trakt never named it
        await store_groups([GROUP])

        await naming.fill_from_calendar(self.user_id)
        ids = await self._month_ids()
        self.assertEqual(ids["simkl_slug"], "a-show")
        self.assertNotIn("trakt_slug", ids)
        self.assertEqual(await store.identities_missing_slugs(self.user_id), [])

    async def test_a_second_pass_writes_nothing(self):
        """It sits on an ordinary load, so once the work is done it has to cost
        nothing rather than rewrite what it already wrote."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222, "trakt": 111}))
        await store_groups([GROUP])

        self.assertEqual(await naming.fill_from_calendar(self.user_id), 2)
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)

    async def test_an_empty_calendar_cache_is_not_an_error(self):
        """A fresh instance has stored no window yet. Nothing is owed that can be
        paid, and the next load tries again once a window exists."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)

    async def test_a_title_the_calendar_does_not_name_is_left_alone(self):
        """An older show in no stored window keeps whatever it has. It must not
        pick up the slugs of whichever title happened to be in the cache."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            tmdb=555, ids={"tmdb": 555, "simkl": 999}))
        await store_groups([GROUP])

        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)
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
        """Count inflations of the stored windows without stubbing out what they
        return — the point is how OFTEN the walk happens, not what it finds."""
        real = calendar_cache.cached_calendar_groups

        async def counting():
            self.walks += 1
            return await real()

        naming.calendar_cache.cached_calendar_groups = counting
        self.addCleanup(setattr, naming.calendar_cache,
                        "cached_calendar_groups", real)

    async def test_an_unpayable_debt_is_not_re_examined_every_pass(self):
        """A title the calendar has never named. The first pass has to look; the
        second has the same rows and the same windows, so it cannot find anything
        the first did not."""
        self._count_walks()
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            tmdb=555, ids={"tmdb": 555, "simkl": 999}))
        await store_groups([GROUP])

        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)
        self.assertEqual(self.walks, 1)

    async def test_a_newly_stored_window_reopens_the_question(self):
        """The calendar moved, so a title that could not be named before may be
        nameable now. Skipping here would strand it until something else happened
        to change what is owed."""
        self._count_walks()
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        await store_groups([])
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)

        await calendar_cache.store_groups("shows", date(2026, 8, 1), [GROUP],
                                          ttl_seconds=3600, now=db.now() + 1)
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 1)
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
        await store_groups([GROUP])
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 0)

        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 1)
        self.assertEqual(self.walks, 2)

    async def test_one_viewers_settled_answer_does_not_silence_another(self):
        """The marker is per viewer because the debt is: the calendar half of the
        signature is shared, but what is outstanding is not."""
        self._count_walks()
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        await store_groups([GROUP])
        self.assertEqual(await naming.fill_from_calendar(self.user_id), 1)

        other = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("someone-else", db.now(), db.now()))
        await distrakt.add_month_record(other.lastrowid, self.month, a_record(
            ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await naming.fill_from_calendar(other.lastrowid), 1)


class WhoIsOwedANameTests(DistraktTestCase):
    """store.identities_missing_slugs — the guard that decides whether any of the
    above is worth doing at all."""

    def setUp(self):
        self.month = month_back(0)

    async def test_it_answers_per_identity_rather_than_per_row(self):
        """A name is a fact about the title, and learn_ids writes every row of one
        at a stroke. Answering per row would have the caller ask the same question
        once per season."""
        await distrakt.add_month_record(self.user_id, self.month,
                                        a_record(season=1, ids={"tmdb": 900, "simkl": 222}))
        await distrakt.add_month_record(self.user_id, self.month,
                                        a_record(season=2, ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await store.identities_missing_slugs(self.user_id),
                         [(ItemKey("show", "tmdb", "900"), ("simkl_slug",))])

    async def test_it_finds_a_row_on_the_viewers_own_list_as_well_as_a_month(self):
        """Both record tables, because a title can be listed, settled, or both."""
        await distrakt.add_user_record(self.user_id, a_record(
            season=3, kind=distrakt.RecordKind.KEEPUP, ids={"tmdb": 901, "trakt": 111}))
        self.assertEqual(await store.identities_missing_slugs(self.user_id),
                         [(ItemKey("show", "tmdb", "901"), ("trakt_slug",))])

    async def test_an_identity_short_of_a_different_name_in_each_table_owes_both(self):
        """The two tables are asked separately and their answers merged, not
        concatenated. A title whose settled record knows only Trakt and whose
        listed row knows only Simkl is one title owing two names; reporting it
        twice would have the caller write each half over the other's rows."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            season=1, kind=distrakt.RecordKind.COMPLETED,
            ids={"tmdb": 900, "trakt": 111}))
        await distrakt.add_user_record(self.user_id, a_record(
            season=2, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await store.identities_missing_slugs(self.user_id),
                         [(ItemKey("show", "tmdb", "900"),
                           ("simkl_slug", "trakt_slug"))])

    async def test_a_record_naming_no_service_at_all_is_owed_nothing(self):
        """A title filed from the calendar alone may carry only shared ids. There
        is no service to link it to, so there is no name outstanding."""
        await distrakt.add_month_record(self.user_id, self.month,
                                        a_record(ids={"tmdb": 900}))
        self.assertEqual(await store.identities_missing_slugs(self.user_id), [])

    async def test_another_viewers_rows_are_not_this_viewers_work(self):
        """The tables are shared and keyed by user. A sweep that answered for
        everybody would write another account's records."""
        other = await store.db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("someone-else", db.now(), db.now()))
        await distrakt.add_month_record(other.lastrowid, self.month,
                                        a_record(ids={"tmdb": 900, "simkl": 222}))
        self.assertEqual(await store.identities_missing_slugs(self.user_id), [])
