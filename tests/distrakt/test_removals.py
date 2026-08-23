"""app/distrakt/removals.py — recording that a service stopped listing a title.

THE POINT OF EVERY TEST HERE IS THAT NOTHING IS DELETED. Simkl's guide prescribes
removing the local rows a diff does not name; this app marks them instead,
because watch history cannot be re-derived from anything it holds and the two
mistakes are not symmetrical. So the assertions are about what the row SAYS, and
the row is expected to still be there in every single case.

No network: the port is a stand-in answering with whatever listing a test wants,
including the refusal (None) that a partially-read library produces.
"""
from __future__ import annotations

import unittest

from app import distrakt
from app.distrakt import lifecycle, removals, store
from app.distrakt import routes as distrakt_routes
from app.providers.base import ItemKey, Source
from tests.distrakt.test_store import DistraktTestCase, a_record, month_back

SETTINGS = object()


class _Port:
    """A tracker port that can list its library ids, or refuse to."""

    def __init__(self, listing):
        self.listing = listing
        self.calls = 0

    async def fetch_library_ids(self, _settings):
        self.calls += 1
        return self.listing


class MarkingWhatAServiceNoLongerListsTests(DistraktTestCase):
    def setUp(self):
        self.month = month_back(0)
        self.key = ItemKey("show", "tmdb", "900")

    async def _listed(self, **ids):
        await distrakt.add_user_record(self.user_id, a_record(
            season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 900, **ids}))

    async def _record(self, key=None):
        records = await store.user_records(self.user_id)
        wanted = str(key or self.key)
        return next(r for r in records if str(store.record_key(r)) == wanted)

    async def test_a_title_the_service_no_longer_lists_is_marked_not_deleted(self):
        """THE WHOLE POLICY IN ONE ASSERTION. The row survives, keeps its counts,
        and says which service dropped it."""
        await self._listed(simkl=222)
        port = _Port({999: "something-else"})
        # Something of this viewer's IS in the listing, so it is believable.
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb=901, season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 901, "simkl": 999}))

        self.assertEqual(await removals.check(SETTINGS, self.user_id, Source.SIMKL, port), 1)
        record = await self._record()
        self.assertEqual(record["missing_sources"], ["simkl"])
        self.assertEqual(record["season"], 1)

    async def test_the_mark_clears_itself_when_the_title_comes_back(self):
        """It is a claim about what a service holds RIGHT NOW, so the service's
        next answer should overwrite it. `came_back` beside it works the other way
        — cleared only by the viewer — because it remembers something no later
        read can restate."""
        await self._listed(simkl=222)
        await store.set_missing_sources(self.user_id, self.key, ["simkl"])
        self.assertEqual((await self._record())["missing_sources"], ["simkl"])

        await removals.check(SETTINGS, self.user_id, Source.SIMKL, _Port({222: "s"}))
        self.assertEqual((await self._record())["missing_sources"], [])

    async def test_a_title_put_back_is_cleared_by_an_ordinary_read(self):
        """THE BUG THIS EXISTS FOR, found by removing a real title at Simkl and
        restoring it. Clearing used to live inside `check`, which only runs when
        the removal beacon moves — and putting a title BACK moves the watched
        stamp, never `removed_from_list`. So the check never ran again and the
        clearing logic was unreachable: a restored title stayed marked for ever.

        A library read NAMING a title is proof enough, costs nothing because the
        read happened anyway, and works on a bounded delta read too — a re-add is
        exactly the kind of change a delta returns.
        """
        await self._listed(simkl=222)
        await store.set_missing_sources(self.user_id, self.key, ["simkl"])

        cleared = await removals.clear_named(self.user_id, Source.SIMKL,
                                             [str(self.key)])
        self.assertEqual(cleared, 1)
        self.assertEqual((await self._record())["missing_sources"], [])

    async def test_clearing_leaves_another_services_mark_alone(self):
        """Simkl naming a title says nothing about whether Trakt still holds it."""
        await self._listed(simkl=222, trakt=111)
        await store.set_missing_sources(self.user_id, self.key, ["simkl", "trakt"])

        await removals.clear_named(self.user_id, Source.SIMKL, [str(self.key)])
        self.assertEqual((await self._record())["missing_sources"], ["trakt"])

    async def test_clearing_a_title_no_read_named_changes_nothing(self):
        """Absence from this read is not evidence of anything — a bounded read
        names only what moved, so most held titles are missing from it."""
        await self._listed(simkl=222)
        await store.set_missing_sources(self.user_id, self.key, ["simkl"])

        await removals.clear_named(self.user_id, Source.SIMKL, ["show:tmdb:999"])
        self.assertEqual((await self._record())["missing_sources"], ["simkl"])

    async def test_clearing_writes_nothing_when_no_row_is_marked(self):
        """The ordinary pass. A read names hundreds of titles and almost none of
        them are marked, so this must cost no writes at all."""
        await self._listed(simkl=222)
        self.assertEqual(
            await removals.clear_named(self.user_id, Source.SIMKL, [str(self.key)]), 0)

    async def test_a_listing_that_could_not_be_read_changes_nothing(self):
        """None means a bucket failed, and every title in it is absent from the
        answer — absence being the entire signal. There is no partial version of
        this listing that is safe to diff."""
        await self._listed(simkl=222)
        await store.set_missing_sources(self.user_id, self.key, ["simkl"])
        port = _Port(None)

        self.assertEqual(await removals.check(SETTINGS, self.user_id, Source.SIMKL, port), 0)
        self.assertEqual((await self._record())["missing_sources"], ["simkl"])

    async def test_a_listing_naming_none_of_our_titles_is_refused(self):
        """The same shape as watch_history._may_retire_rows: a read that names not
        one held title has demonstrated nothing about the library it claims to
        describe. Believing it would mark an entire tracker as gone at a stroke."""
        await self._listed(simkl=222)
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb=901, season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 901, "simkl": 333}))

        port = _Port({777: "a", 888: "b"})
        self.assertEqual(await removals.check(SETTINGS, self.user_id, Source.SIMKL, port), 0)
        self.assertEqual((await self._record())["missing_sources"], [])

    async def test_an_empty_listing_is_refused_for_the_same_reason(self):
        """A viewer who really did empty their library reaches this and keeps
        their rows unmarked until they add one thing back. That is a wrong answer
        they caused and can see; the alternative marks everything from an answer
        that may simply be broken."""
        await self._listed(simkl=222)
        self.assertEqual(
            await removals.check(SETTINGS, self.user_id, Source.SIMKL, _Port({})), 0)
        self.assertEqual((await self._record())["missing_sources"], [])

    async def test_one_services_answer_leaves_the_others_mark_alone(self):
        """A title dropped at Simkl may still be held at Trakt, which is why this
        is a list and not a flag. Simkl answering must not speak for Trakt."""
        await self._listed(simkl=222, trakt=111)
        await store.set_missing_sources(self.user_id, self.key, ["trakt"])
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb=901, season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 901, "simkl": 999}))

        await removals.check(SETTINGS, self.user_id, Source.SIMKL,
                             _Port({999: "other"}))
        self.assertEqual(sorted((await self._record())["missing_sources"]),
                         ["simkl", "trakt"])

    async def test_a_row_carrying_no_id_for_that_service_is_never_marked(self):
        """It was never listed there, so it cannot be missing from it. The
        ids-only payload names no shared id space either, so this row could not
        have been matched against the listing even in principle."""
        await self._listed()  # tmdb only: Simkl never named it
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb=901, season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 901, "simkl": 999}))

        await removals.check(SETTINGS, self.user_id, Source.SIMKL, _Port({999: "x"}))
        self.assertEqual((await self._record())["missing_sources"], [])

    async def test_every_season_of_one_title_is_marked_together(self):
        """A service drops a TITLE, and every season of it stops being listed at
        the same moment. Marking per season would leave the rows one pass did not
        reach disagreeing with the ones it did."""
        for season in (1, 2, 3):
            await distrakt.add_user_record(self.user_id, a_record(
                season=season, kind=distrakt.RecordKind.KEEPUP,
                ids={"tmdb": 900, "simkl": 222}))
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb=901, season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 901, "simkl": 999}))

        await removals.check(SETTINGS, self.user_id, Source.SIMKL, _Port({999: "x"}))
        marked = [r for r in await store.user_records(self.user_id)
                  if r["missing_sources"]]
        self.assertEqual(sorted(r["season"] for r in marked), [1, 2, 3])

    async def test_a_second_pass_with_the_same_answer_writes_nothing(self):
        """It runs behind a beacon, but the beacon can move for reasons unrelated
        to this title. Re-stating a mark already stored would be a write per row
        per removal anywhere in the library."""
        await self._listed(simkl=222)
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb=901, season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 901, "simkl": 999}))
        port = _Port({999: "x"})

        self.assertEqual(await removals.check(SETTINGS, self.user_id, Source.SIMKL, port), 1)
        self.assertEqual(await removals.check(SETTINGS, self.user_id, Source.SIMKL, port), 0)


class TheMarkSurvivesAnOrdinaryWriteTests(DistraktTestCase):
    """The mark is written by the removal check alone. Every other write of a user
    record is a counts refresh built from a roster that carries no opinion about
    what a service holds — so if those could clear it, the mark would appear after
    a removal check and vanish on the very next page load."""

    def setUp(self):
        self.month = month_back(0)
        self.key = ItemKey("show", "tmdb", "900")

    async def test_a_counts_refresh_does_not_clear_it(self):
        await distrakt.add_user_record(self.user_id, a_record(
            season=1, kind=distrakt.RecordKind.KEEPUP, ids={"tmdb": 900, "simkl": 222}))
        await store.set_missing_sources(self.user_id, self.key, ["simkl"])

        await distrakt.add_user_record(self.user_id, a_record(
            season=1, kind=distrakt.RecordKind.KEEPUP,
            ids={"tmdb": 900, "simkl": 222}, watched=5, total=10))

        listed, = await store.user_records(self.user_id)
        self.assertEqual(listed["missing_sources"], ["simkl"])
        self.assertEqual(listed["watched"], 5)


class TheMarkReachesTheRowTests(unittest.TestCase):
    """COMPOSING IT CORRECTLY IS NOT THE SAME AS SHIPPING IT. The per-service watch
    dates were built correctly and reached no row for weeks, because the path that
    renders the page did its own sync and handed the results in — nothing on the
    real path ever passed them. A test of the store alone cannot catch that shape.

    PINNED AT THE ROW BUILDER RATHER THAN OVER HTTP, which is how this suite
    already covers the frozen and empty renders (tests/distrakt/test_payload_shapes
    .py says why): a LISTED row exists only on the live path, and the live path is
    the one that needs the network. This is the last function between a stored row
    and the browser, and it is a pure one.
    """

    def test_a_listed_row_carries_its_missing_sources_to_the_page(self):
        listed = {"key": "show:tmdb:900", "season": 1, "title": "Dropped",
                  "kind": str(distrakt.RecordKind.KEEPUP),
                  "bucket": str(store.Bucket.KEEPUP), "watched": 3, "total": 12,
                  "missing_sources": ["simkl"]}
        shape = lifecycle.MonthShape(series_premieres=[], season_premieres=[],
                                     settled=[], listed=[listed])

        rows = distrakt_routes._rows_for(shape, store.MonthStanding.CURRENT)

        row, = [r for r in rows if r["title"] == "Dropped"]
        self.assertEqual(row["missing_sources"], ["simkl"])
        # AND ITS COUNTS SURVIVE UNTOUCHED, which is the whole policy: the row is
        # marked, not emptied and not removed.
        self.assertEqual((row["watched"], row["total"]), (3, 12))

    def test_an_unmarked_row_says_nothing(self):
        """The browser reads this key on every row, so the ordinary case has to be
        an empty list rather than a missing key or a None."""
        listed = {"key": "show:tmdb:901", "season": 1, "title": "Fine",
                  "kind": str(distrakt.RecordKind.KEEPUP),
                  "bucket": str(store.Bucket.KEEPUP), "watched": 1, "total": 6,
                  "missing_sources": []}
        shape = lifecycle.MonthShape(series_premieres=[], season_premieres=[],
                                     settled=[], listed=[listed])

        row, = distrakt_routes._rows_for(shape, store.MonthStanding.CURRENT)
        self.assertEqual(row["missing_sources"], [])
