"""Unit tests for the distrakt data layer.

Covers the correctness-critical parts: season cadence + premiere/finale
detection (binge vs weekly vs unknown-date tail) and the per-user store round
trip against distrakt_months, distrakt_month_records and distrakt_user_seasons.
No network — _derive_season is pure, and the store runs against a throwaway
SQLite file per test.

MONTHS ARE DERIVED FROM THE CLOCK, never spelled out. This suite has twice been
bitten by a test that hardcoded one, passed all month and went red on the 1st:
app/clock.py is the seam, and the helpers below turn "the month in progress" and
"two months back" into keys rather than into constants that rot.
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import clock, db, distrakt
from app.distrakt import store
from app.providers.base import ItemKey
from app.providers.trakt.detail import _derive_season
from tests.support import new_db_path

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 8, 1, tzinfo=UTC)  # fixed "today" so started/finished is stable


def month_back(count: int) -> str:
    """The key of the month `count` months before the one in progress.

    Derived from the clock so a test that means "an earlier month" keeps meaning
    it after the calendar rolls; count=0 is the month in progress.
    """
    today = clock.today()
    total = today.year * 12 + (today.month - 1) - count
    return store.month_key(total // 12, total % 12 + 1)


def _ep(number: int, iso_date: str | None):
    """A minimal Trakt season episode. iso_date=None => air date unknown."""
    return {"number": number, "first_aired": f"{iso_date}T18:00:00.000Z" if iso_date else None}


async def make_user(username: str) -> int:
    """A distrakt-approved account to hang tracker records off. They are keyed by
    user_id with a real foreign key, so the row has to exist."""
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
        "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


class DistraktTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh database + one distrakt user per test."""
    async def asyncSetUp(self):
        new_db_path("distrakt")
        await db.migrate()
        self.user_id = await make_user("tracker")

    async def asyncTearDown(self):
        db.close_thread_connection()


class VocabularyTests(unittest.TestCase):
    """The two closed sets and the one mapping between them.

    RecordKind is what a record IS and reaches the `kind` column; Bucket is what
    a SECTION is called and reaches app/static/js/tracker/rows.js and the Discord
    headers. Both are stored or shipped as bare strings, so neither may change.
    """

    def test_a_kind_is_the_string_that_goes_in_the_column(self):
        """No conversion at any boundary, which is what a StrEnum buys over a
        class of constants."""
        self.assertEqual(distrakt.RecordKind.COMPLETED, "completed")
        self.assertEqual(f"{distrakt.RecordKind.SERIES_PREMIERE}", "series_premiere")
        self.assertEqual(json.dumps({"k": distrakt.RecordKind.CATCHUP}), '{"k": "catchup"}')

    def test_a_bucket_is_the_string_the_browser_groups_by(self):
        self.assertEqual(distrakt.Bucket.COMPLETED, "completed")
        self.assertEqual(json.dumps({"b": distrakt.Bucket.CLEANUP}), '{"b": "cleanup"}')

    def test_every_kind_renders_under_exactly_one_section(self):
        """The mapping is total: a kind with no bucket would be a record nothing
        could draw, and the table is the ONE place the two vocabularies meet."""
        self.assertEqual(set(distrakt.BUCKET_OF_KIND), set(distrakt.RecordKind))
        self.assertEqual(set(distrakt.BUCKET_OF_KIND.values()), set(distrakt.Bucket))

    def test_catching_up_still_renders_as_cleanup(self):
        """What the viewer's own notices call that section is established. The
        storage word changed; the rendered one deliberately did not."""
        self.assertEqual(distrakt.bucket_of_kind(distrakt.RecordKind.CATCHUP),
                         distrakt.Bucket.CLEANUP)

    def test_the_two_tables_hold_disjoint_kinds(self):
        """Which table a kind lives in is one fact, not two: a kind in both sets
        would be storable in either and findable in neither."""
        self.assertEqual(distrakt.MONTH_KINDS & distrakt.USER_KINDS, frozenset())
        self.assertEqual(distrakt.MONTH_KINDS | distrakt.USER_KINDS,
                         frozenset(distrakt.RecordKind))

    def test_a_first_season_is_a_series_premiere_and_a_later_one_is_not(self):
        """The split is decided ONCE, when the record is made, rather than
        re-derived from the season number at every render — that is how the two
        sections of the first notice come to disagree."""
        self.assertIs(distrakt.premiere_kind(1), distrakt.RecordKind.SERIES_PREMIERE)
        self.assertIs(distrakt.premiere_kind(2), distrakt.RecordKind.SEASON_PREMIERE)
        self.assertIs(distrakt.premiere_kind(0), distrakt.RecordKind.SERIES_PREMIERE)


class MonthKeyTests(unittest.TestCase):
    """The "YYYY-MM" format, written and read by the one pair that owns it.

    The parse used to be hand-rolled at every call site — nine copies of
    `int(key[:4]), int(key[5:7])` and one written a different way again — so what
    is worth pinning is the round trip and the refusals, since a blind slice
    accepts all of the latter and answers nonsense.
    """

    def test_the_round_trip_is_the_identity(self):
        for year, month in ((2026, 1), (2026, 12), (1999, 7), (2030, 10)):
            with self.subTest(year=year, month=month):
                self.assertEqual(
                    distrakt.parse_month_key(distrakt.month_key(year, month)),
                    (year, month))

    def test_it_reads_the_padded_form_the_writer_produces(self):
        self.assertEqual(distrakt.parse_month_key("2026-07"), (2026, 7))

    def test_the_first_day_is_the_first_of_that_month(self):
        self.assertEqual(distrakt.month_first_day("2026-07"), date(2026, 7, 1))

    def test_an_unpadded_month_is_refused_rather_than_guessed(self):
        # The padding is load-bearing — month keys are compared with < and >= and
        # the backward walk orders by the column — so "2026-7" is not a month key
        # with a typo, it is not a month key.
        with self.assertRaises(ValueError):
            distrakt.parse_month_key("2026-7")

    def test_a_longer_string_is_refused(self):
        # A slice would happily read (2026, 7) out of a full date and carry on.
        with self.assertRaises(ValueError):
            distrakt.parse_month_key("2026-07-15")

    def test_an_impossible_month_is_refused(self):
        for bad in ("2026-00", "2026-13", "", "not-a-month", "202607"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    distrakt.parse_month_key(bad)


class DeriveSeasonTests(unittest.TestCase):
    def test_binge_all_same_date(self):
        eps = [_ep(n, "2026-07-10") for n in range(1, 9)]  # 8 eps, one drop date
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 8)
        self.assertEqual(res["cadence"], "b")
        self.assertEqual(res["premiere"], "7/10")
        self.assertEqual(res["finale"], "7/10")
        self.assertTrue(res["started_airing"])
        self.assertTrue(res["finished_airing"])

    def test_weekly_fully_scheduled(self):
        # 2026-07-05 is a Sunday; +7d steps keep the same weekday.
        eps = [_ep(1, "2026-07-05"), _ep(2, "2026-07-12"),
               _ep(3, "2026-07-19"), _ep(4, "2026-07-26")]
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 4)
        self.assertEqual(res["cadence"], "Sun")
        self.assertEqual(res["premiere"], "7/5")
        self.assertEqual(res["finale"], "7/26")
        self.assertTrue(res["started_airing"])
        self.assertTrue(res["finished_airing"])
        self.assertNotEqual(res["cadence"], "b")

    def test_weekly_unknown_tail(self):
        # Two aired, two announced-but-unscheduled -> finale unknown ("?/?").
        eps = [_ep(1, "2026-07-05"), _ep(2, "2026-07-12"), _ep(3, None), _ep(4, None)]
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 4)          # y counts undated eps too
        self.assertEqual(res["cadence"], "Sun")    # still weekly, from known dates
        self.assertEqual(res["premiere"], "7/5")
        self.assertIsNone(res["finale"])           # renderer shows "?/?"
        self.assertTrue(res["started_airing"])
        self.assertFalse(res["finished_airing"])   # no known finale -> not finished

    def test_no_dates_known(self):
        eps = [_ep(1, None), _ep(2, None)]
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 2)
        self.assertIsNone(res["cadence"])
        self.assertIsNone(res["premiere"])
        self.assertIsNone(res["finale"])
        self.assertFalse(res["started_airing"])

    def test_not_yet_started_future_premiere(self):
        eps = [_ep(1, "2026-09-06"), _ep(2, "2026-09-13")]  # after NOW
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["premiere"], "9/6")
        self.assertFalse(res["started_airing"])
        self.assertFalse(res["finished_airing"])

    def test_empty_season(self):
        res = _derive_season([], UTC, now=NOW)
        self.assertEqual(res["total"], 0)
        self.assertIsNone(res["cadence"])
        self.assertIsNone(res["premiere"])


def a_record(tmdb: int = 900, *, season: int = 1, kind=distrakt.RecordKind.SERIES_PREMIERE,
             **fields) -> dict:
    """A record as a caller states one: ids rather than a resolved key.

    Only the fields a record cannot be built without are filled in; everything
    else is stated by the test that cares about it, so a test asserting that an
    untouched column survives an update is not quietly re-supplying it.
    """
    return {"ids": {"tmdb": tmdb}, "season": season, "kind": str(kind), **fields}


class MonthRecordTests(DistraktTestCase):
    """What a month announced and what it settled."""

    def setUp(self):
        self.month = month_back(0)

    async def test_a_record_is_written_and_read_back(self):
        self.assertIsNone(await distrakt.load_month(self.user_id, self.month))
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            900, kind=distrakt.RecordKind.SEASON_PREMIERE, season=2,
            ids={"trakt": 12345, "tmdb": 900, "slug": "the-westies"},
            title="The Westies", network="MGM+", total=8, cadence="Sun",
            premiere="7/12", finale="8/23"))
        doc = await distrakt.load_month(self.user_id, self.month)
        self.assertEqual(doc["month"], self.month)
        self.assertFalse(doc["closed"])
        rec, = doc["shows"]
        # Keyed on the shared id, with the service's own id kept as an attribute.
        self.assertEqual((rec["match_source"], rec["match_id"]), ("tmdb", "900"))
        self.assertEqual(rec["key"], "show:tmdb:900")
        self.assertEqual(rec["ids"]["trakt"], 12345)
        self.assertEqual((rec["season"], rec["total"], rec["cadence"]), (2, 8, "Sun"))
        # `bucket` is derived from the kind, never stored beside it.
        self.assertEqual(rec["kind"], "season_premiere")
        self.assertEqual(rec["bucket"], "returning")

    async def test_the_same_kind_and_season_updates_rather_than_doubling(self):
        await distrakt.add_month_record(self.user_id, self.month,
                                        a_record(1, season=2, title="X", total=10))
        await distrakt.add_month_record(self.user_id, self.month,
                                        a_record(1, season=2, total=12))
        rec, = (await distrakt.load_month(self.user_id, self.month))["shows"]
        self.assertEqual(rec["total"], 12)
        self.assertEqual(rec["title"], "X")  # untouched column preserved

    async def test_a_season_premiered_and_settled_in_one_month_holds_two_records(self):
        """THE WHOLE REASON `kind` IS IN THE KEY. The month both announced it and
        reached a verdict on it; those are two statements, not a duplicate."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            5, kind=distrakt.RecordKind.SERIES_PREMIERE, premiere="7/1"))
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            5, kind=distrakt.RecordKind.COMPLETED, watched=8, total=8))
        doc = await distrakt.load_month(self.user_id, self.month)
        self.assertEqual({r["kind"] for r in doc["shows"]},
                         {"series_premiere", "completed"})

    async def test_a_month_can_be_read_by_kind(self):
        """Which records a month's page shows is a list of KINDS, not a different
        query per standing."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(1))
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            2, kind=distrakt.RecordKind.COMPLETED))
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            3, kind=distrakt.RecordKind.ABANDONED))
        settled = await distrakt.month_records(self.user_id, self.month,
                                               distrakt.SETTLED_KINDS)
        self.assertEqual({r["match_id"] for r in settled}, {"2", "3"})
        premieres = await distrakt.month_records(self.user_id, self.month,
                                                 distrakt.PREMIERE_KINDS)
        self.assertEqual({r["match_id"] for r in premieres}, {"1"})

    async def test_asking_for_no_kinds_returns_nothing_rather_than_everything(self):
        """An empty selection is a caller asking for nothing, which a month whose
        standing shows no sections legitimately does."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(1))
        self.assertEqual(await distrakt.month_records(self.user_id, self.month, []), [])

    async def test_a_user_kind_cannot_be_filed_onto_a_month(self):
        """Both tables would accept the string; the record would simply never be
        found again by the reader that expected it in the other one."""
        with self.assertRaises(ValueError):
            await distrakt.add_month_record(
                self.user_id, self.month, a_record(1, kind=distrakt.RecordKind.KEEPUP))

    async def test_a_record_with_no_kind_is_refused_rather_than_guessed(self):
        """A default would file somebody's completed season as a premiere and
        nothing downstream would ever notice."""
        with self.assertRaises(ValueError):
            distrakt.normalize_show({"ids": {"tmdb": 1}, "season": 1})

    async def test_removing_one_record_leaves_the_month_s_others(self):
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            5, kind=distrakt.RecordKind.SERIES_PREMIERE))
        await distrakt.add_month_record(self.user_id, self.month, a_record(
            5, kind=distrakt.RecordKind.COMPLETED))
        key = ItemKey("show", "tmdb", "5")
        self.assertTrue(await distrakt.remove_month_record(
            self.user_id, self.month, distrakt.RecordKind.COMPLETED, key, 1))
        self.assertFalse(await distrakt.remove_month_record(
            self.user_id, self.month, distrakt.RecordKind.COMPLETED, key, 1))
        rec, = (await distrakt.load_month(self.user_id, self.month))["shows"]
        self.assertEqual(rec["kind"], "series_premiere")

    async def test_an_invalid_month_is_refused(self):
        with self.assertRaises(ValueError):
            await distrakt.load_month(self.user_id, "2026-13")
        with self.assertRaises(ValueError):
            await distrakt.add_month_record(self.user_id, "July", a_record(1))

    async def test_list_months_is_sorted(self):
        for month in (month_back(0), month_back(3), month_back(1)):
            await distrakt.add_month_record(self.user_id, month, a_record(1))
        self.assertEqual(await distrakt.list_months(self.user_id),
                         [month_back(3), month_back(1), month_back(0)])

    async def test_a_frozen_month_survives_a_round_trip(self):
        """A frozen month renders offline from these fields alone, so they have to
        come back exactly as written — including the airing flags and the
        month-level movies snapshot."""
        month = month_back(2)
        doc = distrakt.new_month_doc(month)
        doc["shows"] = [{
            "ids": {"trakt": 7, "tmdb": 42, "slug": "s"}, "media": "show", "title": "T",
            "season": 3, "network": "N", "kind": "completed",
            "watched": 8, "total": 8, "cadence": "Tue", "premiere": "5/1",
            "finale": "5/29", "started_airing": True, "finished_airing": True,
        }]
        doc["closed"] = True
        doc["totals_refreshed_at"] = db.now()
        doc["movies"] = [{"title": "A Film", "year": 2026, "watched_at": "2026-05-04T00:00:00Z"}]
        await distrakt.save_month(self.user_id, doc)

        back = await distrakt.load_month(self.user_id, month)
        self.assertTrue(back["closed"])
        self.assertEqual(back["movies"], doc["movies"])
        rec, = back["shows"]
        self.assertTrue(rec["started_airing"])
        self.assertTrue(rec["finished_airing"])
        self.assertEqual(rec["bucket"], "completed")
        self.assertEqual(rec["ids"], {"trakt": 7, "tmdb": 42, "slug": "s"})
        # frozen_shows reads them straight back with no Trakt call.
        self.assertTrue(distrakt.frozen_shows(back)[0]["started_airing"])

    async def test_a_refused_save_does_not_empty_the_month_first(self):
        """The records are validated before the transaction opens, so a doc with
        one bad record leaves the month exactly as it was."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(1))
        with self.assertRaises(ValueError):
            await distrakt.save_month(self.user_id, {
                "month": self.month,
                "shows": [a_record(2), {"ids": {"tmdb": 3}, "season": 1}]})
        self.assertEqual(len((await distrakt.load_month(self.user_id, self.month))["shows"]), 1)


class UserRecordTests(DistraktTestCase):
    """The viewer's own list: one table, no month, one record per season."""

    async def _list(self, kinds=None):
        return {(r["match_id"], r["kind"])
                for r in await distrakt.user_records(self.user_id, kinds)}

    async def test_a_season_is_on_the_list_once_and_changes_state_in_place(self):
        """keepup and catchup are two STATES of one record, so a season that
        finishes airing is an UPDATE and can never appear twice."""
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP, watched=2, total=8))
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.CATCHUP, watched=5))
        self.assertEqual(await self._list(), {("7", "catchup")})
        rec = await distrakt.find_user_record(self.user_id, ItemKey("show", "tmdb", "7"), 1)
        self.assertEqual((rec["watched"], rec["total"]), (5, 8))

    async def test_only_what_actually_differs_is_written_back(self):
        """The live pass hands the same counts back for every record on the page
        whether or not anything moved, so an unconditional update turned a plain
        read of a month into one write per row. Asserted on the function that
        decides it, because the alternative — watching the row not change — would
        pass whether or not the statement was issued."""
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP, watched=2, total=8))
        row = await db.fetch_one(
            "SELECT * FROM distrakt_user_seasons WHERE user_id = ? AND match_id = '7'",
            (self.user_id,))

        self.assertEqual(store._differences(row, {"watched": 2, "total": 8}), {})
        self.assertEqual(store._differences(row, {"watched": 3, "total": 8}),
                         {"watched": 3})

    async def test_a_bool_matches_the_0_or_1_it_is_stored_as(self):
        """Otherwise every record would differ on every airing flag for ever, and
        the check would save nothing at all."""
        await distrakt.add_user_record(self.user_id, a_record(
            8, kind=distrakt.RecordKind.CATCHUP, finished_airing=True))
        row = await db.fetch_one(
            "SELECT * FROM distrakt_user_seasons WHERE user_id = ? AND match_id = '8'",
            (self.user_id,))
        self.assertEqual(store._differences(row, {"finished_airing": True}), {})
        self.assertEqual(store._differences(row, {"finished_airing": False}),
                         {"finished_airing": False})

    async def test_a_real_change_still_lands(self):
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP, watched=2, total=8))
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP, watched=3, total=8))
        stored = await distrakt.find_user_record(
            self.user_id, ItemKey("show", "tmdb", "7"), 1)
        self.assertEqual(stored["watched"], 3)

    async def test_the_state_change_has_its_own_verb(self):
        """Nothing extra is fetched to decide it: the season lookup made for every
        listed season already reports the last episode's air date."""
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP))
        key = ItemKey("show", "tmdb", "7")
        self.assertTrue(await distrakt.set_user_kind(
            self.user_id, key, 1, distrakt.RecordKind.CATCHUP))
        self.assertEqual(await self._list(), {("7", "catchup")})
        self.assertFalse(await distrakt.set_user_kind(
            self.user_id, ItemKey("show", "tmdb", "404"), 1, distrakt.RecordKind.CATCHUP))

    async def test_the_list_can_be_read_by_kind(self):
        await distrakt.add_user_record(self.user_id, a_record(
            1, kind=distrakt.RecordKind.KEEPUP))
        await distrakt.add_user_record(self.user_id, a_record(
            2, kind=distrakt.RecordKind.CATCHUP))
        self.assertEqual(await self._list([distrakt.RecordKind.CATCHUP]),
                         {("2", "catchup")})

    async def test_seasons_of_one_show_are_held_apart(self):
        await distrakt.add_user_record(self.user_id, a_record(
            1, season=1, kind=distrakt.RecordKind.KEEPUP))
        await distrakt.add_user_record(self.user_id, a_record(
            1, season=2, kind=distrakt.RecordKind.KEEPUP))
        self.assertEqual(len(await distrakt.user_records(self.user_id)), 2)

    async def test_a_month_kind_cannot_be_filed_onto_the_list(self):
        with self.assertRaises(ValueError):
            await distrakt.add_user_record(
                self.user_id, a_record(1, kind=distrakt.RecordKind.COMPLETED))

    async def test_a_counts_refresh_does_not_dismiss_the_came_back_marker(self):
        """Only the viewer's acknowledge control clears it. A routine write of the
        live counts must not quietly retire a marker nobody has read."""
        key = ItemKey("show", "tmdb", "7")
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP))
        await distrakt.set_came_back(self.user_id, key, 1, True)
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP, watched=3))
        rec = await distrakt.find_user_record(self.user_id, key, 1)
        self.assertTrue(rec["came_back"])
        self.assertEqual(rec["watched"], 3)

    async def test_the_marker_is_cleared_by_the_verb_that_sets_it(self):
        key = ItemKey("show", "tmdb", "7")
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP, came_back=True))
        self.assertTrue((await distrakt.find_user_record(self.user_id, key, 1))["came_back"])
        self.assertTrue(await distrakt.set_came_back(self.user_id, key, 1, False))
        self.assertFalse((await distrakt.find_user_record(self.user_id, key, 1))["came_back"])

    async def test_a_season_that_is_not_listed_is_not_found(self):
        self.assertIsNone(await distrakt.find_user_record(
            self.user_id, ItemKey("show", "tmdb", "404"), 1))

    async def test_removing_one_takes_it_off_the_list(self):
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP))
        key = ItemKey("show", "tmdb", "7")
        self.assertTrue(await distrakt.remove_user_record(self.user_id, key, 1))
        self.assertFalse(await distrakt.remove_user_record(self.user_id, key, 1))


class MigrationBetweenTheTablesTests(DistraktTestCase):
    """A season moving between the viewer's list and a month's verdicts, which is
    what the lifecycle is made of."""

    def setUp(self):
        self.month = month_back(0)
        self.key = ItemKey("show", "tmdb", "7")

    async def _list(self):
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP, watched=8, total=8,
            network="HBO", added_by=distrakt.ADDED_BY_CALENDAR))

    async def test_settling_a_season_moves_it_off_the_list_and_onto_the_month(self):
        await self._list()
        rec = await distrakt.migrate_to_month(
            self.user_id, self.key, 1, month=self.month,
            kind=distrakt.RecordKind.COMPLETED)
        self.assertEqual(rec["kind"], "completed")
        self.assertEqual(rec["month"], self.month)
        # The verdict is about the season as the viewer last had it, so its
        # counts, its network and who first filed it all travel with it.
        self.assertEqual((rec["watched"], rec["total"], rec["network"]), (8, 8, "HBO"))
        self.assertEqual(rec["added_by"], distrakt.ADDED_BY_CALENDAR)
        self.assertEqual(await distrakt.user_records(self.user_id), [])

    async def test_abandoning_freezes_the_rendered_line(self):
        await self._list()
        rec = await distrakt.migrate_to_month(
            self.user_id, self.key, 1, month=self.month,
            kind=distrakt.RecordKind.ABANDONED, abandoned_form="`A Show S01 (8/8)`")
        self.assertEqual(rec["abandoned_form"], "`A Show S01 (8/8)`")
        self.assertTrue(rec["abandoned"])
        self.assertEqual(rec["bucket"], "abandoned")

    async def test_a_completed_record_carries_no_frozen_line(self):
        """The frozen form is what an abandon renders as; nothing else has one."""
        await self._list()
        rec = await distrakt.migrate_to_month(
            self.user_id, self.key, 1, month=self.month,
            kind=distrakt.RecordKind.COMPLETED, abandoned_form="should not stick")
        self.assertIsNone(rec["abandoned_form"])

    async def test_settling_a_season_that_is_not_listed_writes_nothing(self):
        self.assertIsNone(await distrakt.migrate_to_month(
            self.user_id, self.key, 1, month=self.month,
            kind=distrakt.RecordKind.COMPLETED))
        self.assertEqual(await distrakt.month_records(self.user_id, self.month), [])

    async def test_a_premiere_record_on_the_same_month_is_left_alone(self):
        """Premiered and settled in one month is the expected case: the premiere
        record is what the first notice reads and it stays for ever."""
        await distrakt.add_month_record(self.user_id, self.month, a_record(7, premiere="7/1"))
        await self._list()
        await distrakt.migrate_to_month(self.user_id, self.key, 1, month=self.month,
                                        kind=distrakt.RecordKind.COMPLETED)
        self.assertEqual({r["kind"] for r in
                          await distrakt.month_records(self.user_id, self.month)},
                         {"series_premiere", "completed"})

    async def test_a_season_that_comes_back_loses_the_record_that_settled_it(self):
        """A month records what it settled, and this one no longer settled it:
        "completed in July" has turned out not to be true, and a record that is
        false is worse than an absent one."""
        await self._list()
        await distrakt.migrate_to_month(self.user_id, self.key, 1, month=self.month,
                                        kind=distrakt.RecordKind.COMPLETED)
        rec = await distrakt.migrate_to_user(
            self.user_id, self.key, 1, month=self.month,
            from_kind=distrakt.RecordKind.COMPLETED, kind=distrakt.RecordKind.KEEPUP,
            came_back=True)
        self.assertEqual(rec["kind"], "keepup")
        self.assertTrue(rec["came_back"])
        self.assertEqual(await distrakt.month_records(self.user_id, self.month), [])

    async def test_un_abandoning_puts_it_back_with_no_marker(self):
        """Coming back after being given up on is not the same as a season that
        turned out to have grown, so it raises no marker."""
        await self._list()
        await distrakt.migrate_to_month(self.user_id, self.key, 1, month=self.month,
                                        kind=distrakt.RecordKind.ABANDONED,
                                        abandoned_form="`A Show S01 (8/8)`")
        rec = await distrakt.migrate_to_user(
            self.user_id, self.key, 1, month=self.month,
            from_kind=distrakt.RecordKind.ABANDONED, kind=distrakt.RecordKind.CATCHUP)
        self.assertEqual(rec["kind"], "catchup")
        self.assertFalse(rec["came_back"])

    async def test_moving_back_a_record_that_is_not_there_writes_nothing(self):
        self.assertIsNone(await distrakt.migrate_to_user(
            self.user_id, self.key, 1, month=self.month,
            from_kind=distrakt.RecordKind.COMPLETED, kind=distrakt.RecordKind.KEEPUP))
        self.assertEqual(await distrakt.user_records(self.user_id), [])


class RemovingASeasonOutrightTests(DistraktTestCase):
    """The ✕, and the only thing that ever removes a record."""

    async def test_it_takes_every_copy_of_the_season_everywhere(self):
        """A season can hold a premiere on one month, a verdict on another and a
        row on the viewer's list at once, so anything narrower leaves a copy and
        the row comes straight back on the next load."""
        premiered, settled = month_back(2), month_back(1)
        await distrakt.add_month_record(self.user_id, premiered, a_record(7))
        await distrakt.add_month_record(self.user_id, settled, a_record(
            7, kind=distrakt.RecordKind.COMPLETED))
        await distrakt.add_user_record(self.user_id, a_record(
            7, season=2, kind=distrakt.RecordKind.KEEPUP))
        await distrakt.add_user_record(self.user_id, a_record(
            7, kind=distrakt.RecordKind.KEEPUP))

        months = await distrakt.remove_season_everywhere(
            self.user_id, ItemKey("show", "tmdb", "7"), 1)

        self.assertEqual(months, [premiered, settled])
        self.assertEqual(await distrakt.month_records(self.user_id, premiered), [])
        self.assertEqual(await distrakt.month_records(self.user_id, settled), [])
        # The other season of the same show is untouched.
        self.assertEqual([r["season"] for r in await distrakt.user_records(self.user_id)], [2])

    async def test_a_season_nothing_holds_reports_no_months(self):
        self.assertEqual(await distrakt.remove_season_everywhere(
            self.user_id, ItemKey("show", "tmdb", "404"), 1), [])


class WalkingBackForASeasonTests(DistraktTestCase):
    """The ordered backward walk a reopening does when an episode is seen for a
    season nothing currently holds."""

    async def _settle(self, month: str, tmdb: int, kind=distrakt.RecordKind.COMPLETED):
        await distrakt.add_month_record(self.user_id, month, a_record(tmdb, kind=kind))

    async def _walk(self, **kwargs):
        return [(month, [r["match_id"] for r in records])
                async for month, records in distrakt.walk_settled(self.user_id, **kwargs)]

    async def test_the_newest_month_comes_first(self):
        """The match is usually one or two months back, so the walk stops early
        rather than reading a viewing life to answer a question about last month."""
        await self._settle(month_back(3), 1)
        await self._settle(month_back(1), 2)
        await self._settle(month_back(0), 3)
        self.assertEqual(await self._walk(),
                         [(month_back(0), ["3"]), (month_back(1), ["2"]),
                          (month_back(3), ["1"])])

    async def test_it_carries_both_verdicts_and_nothing_else(self):
        month = month_back(1)
        await self._settle(month, 1)
        await self._settle(month, 2, kind=distrakt.RecordKind.ABANDONED)
        await distrakt.add_month_record(self.user_id, month, a_record(3))  # a premiere
        walked, = await self._walk()
        self.assertEqual(sorted(walked[1]), ["1", "2"])

    async def test_before_excludes_that_month_and_everything_after_it(self):
        """The months already asked about by other means are skipped rather than
        re-read."""
        await self._settle(month_back(2), 1)
        await self._settle(month_back(1), 2)
        await self._settle(month_back(0), 3)
        self.assertEqual([month for month, _ in await self._walk(before=month_back(1))],
                         [month_back(2)])

    async def test_a_month_that_settled_nothing_is_not_walked(self):
        await distrakt.add_month_record(self.user_id, month_back(1), a_record(1))
        self.assertEqual(await self._walk(), [])


class TheUntrackedEpisodePromptTests(DistraktTestCase):
    """The ✗ on the row that offers to add a season nothing knows about."""

    async def test_a_dismissal_is_remembered_so_the_row_stays_gone(self):
        """The prompt is derived from the watch history on every load, so without
        somewhere to record the refusal it would come straight back."""
        key = ItemKey("show", "tmdb", "7")
        self.assertEqual(await distrakt.dismissed_prompts(self.user_id), set())
        await distrakt.dismiss_prompt(self.user_id, key, 2)
        self.assertEqual(await distrakt.dismissed_prompts(self.user_id),
                         {("show:tmdb:7", 2)})

    async def test_it_is_per_season_not_per_episode(self):
        """Otherwise every further episode of the same season asks again."""
        key = ItemKey("show", "tmdb", "7")
        await distrakt.dismiss_prompt(self.user_id, key, 2)
        await distrakt.dismiss_prompt(self.user_id, key, 2)
        self.assertEqual(len(await distrakt.dismissed_prompts(self.user_id)), 1)
        await distrakt.dismiss_prompt(self.user_id, key, 3)
        self.assertEqual(len(await distrakt.dismissed_prompts(self.user_id)), 2)


class RecordIdentityTests(unittest.TestCase):
    """How a record gets the identity it is filed under. Pure — no database."""

    def test_it_runs_the_waterfall_over_whatever_ids_the_record_has(self):
        key = distrakt.record_key({"ids": {"trakt": 9, "tvdb": 5, "tmdb": 7}, "season": 1})
        self.assertEqual(str(key), "show:tmdb:7")

    def test_a_record_that_already_carries_a_triple_keeps_it(self):
        """A record read back from storage must resolve to the identity it was
        WRITTEN under, even if it has since learned a better id — re-running the
        waterfall on it would silently address a different record."""
        rec = {"media": "show", "match_source": "tvdb", "match_id": "5",
               "ids": {"tmdb": 7}, "season": 1}
        self.assertEqual(str(distrakt.record_key(rec)), "show:tvdb:5")

    def test_media_travels_with_it(self):
        key = distrakt.record_key({"media": "movie", "ids": {"tmdb": 550}})
        self.assertEqual(str(key), "movie:tmdb:550")

    def test_a_record_with_no_shared_id_is_refused_by_name(self):
        """Refused where it is BUILT, so the message can still say which title it
        was — two layers on, nothing knows."""
        with self.assertRaises(distrakt.UnkeyableRecord) as caught:
            distrakt.record_key({"ids": {"trakt": 9, "slug": "a-show"},
                                 "title": "Unkeyable Show", "season": 1})
        self.assertIn("Unkeyable Show", str(caught.exception))

    def test_normalize_puts_the_flat_key_on_the_record(self):
        """It is what the browser names a record by, so every record carries it
        rather than each caller spelling it out."""
        rec = distrakt.normalize_show(a_record(7, season=2))
        self.assertEqual(rec["key"], "show:tmdb:7")
        self.assertEqual((rec["match_source"], rec["match_id"]), ("tmdb", "7"))


class StoredIdentityTests(DistraktTestCase):
    def setUp(self):
        self.month = month_back(0)

    async def _add(self, **fields):
        await distrakt.add_month_record(self.user_id, self.month, a_record(**fields))

    async def _records(self):
        return (await distrakt.load_month(self.user_id, self.month))["shows"]

    async def test_two_services_reporting_one_title_are_one_record(self):
        """THE POINT OF THE RE-KEY. The same season arriving with a different
        provider id but the same shared id updates the record rather than doubling
        it — which is what keying on the provider's id could never do."""
        await self._add(ids={"trakt": 111, "tmdb": 900})
        await self._add(ids={"simkl": 222, "tmdb": 900}, watched=4)
        rec, = await self._records()
        self.assertEqual(rec["watched"], 4)
        # and it now knows both services' ids, so either can be called about it
        self.assertEqual(rec["ids"], {"trakt": 111, "simkl": 222, "tmdb": 900})

    async def test_a_record_keyed_on_a_weaker_id_stays_its_own(self):
        """The residual risk this design accepts, pinned so it is a known
        property rather than a surprise: a title first seen with only a tvdb is
        keyed on it, and a later record carrying tmdb is a different one. The ids
        stored on both are what a resolution pass would use to join them later."""
        await self._add(ids={"tvdb": 5}, title="Early")
        await self._add(ids={"tvdb": 5, "tmdb": 900}, title="Later")
        self.assertEqual({r["key"] for r in await self._records()},
                         {"show:tvdb:5", "show:tmdb:900"})

    async def test_a_record_can_learn_an_id_it_did_not_have(self):
        """The upgrade path the stored ids exist for."""
        await self._add(ids={"tmdb": 900})
        await self._add(ids={"tmdb": 900, "imdb": "tt42"})
        rec, = await self._records()
        self.assertEqual(rec["ids"], {"tmdb": 900, "imdb": "tt42"})

    async def test_a_movie_and_a_show_sharing_a_tmdb_id_are_different_titles(self):
        """TMDB ids are namespaced per media kind, so the media type has to be
        part of the key or one would overwrite the other."""
        await self._add(media="show", ids={"tmdb": 550}, title="The Show")
        await self._add(media="movie", ids={"tmdb": 550}, title="The Film")
        self.assertEqual({r["title"] for r in await self._records()},
                         {"The Show", "The Film"})

    async def test_provenance_is_not_rewritten_by_a_later_write(self):
        """`added_by` records who FIRST filed the record: the live-counts writer
        goes through the same upsert, and a calendar record quietly becoming a
        manual one would change what removing it says to the calendar."""
        await self._add(added_by=distrakt.ADDED_BY_CALENDAR)
        await self._add(watched=3, added_by=distrakt.ADDED_BY_MANUAL)
        rec, = await self._records()
        self.assertEqual(rec["added_by"], distrakt.ADDED_BY_CALENDAR)
        self.assertEqual(rec["watched"], 3)


if __name__ == "__main__":
    unittest.main()
