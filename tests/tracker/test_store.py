"""Unit tests for the distrakt data layer.

Covers the correctness-critical parts: season cadence + premiere/finale
detection (binge vs weekly vs unknown-date tail) and the per-user store round
trip against distrakt_months + distrakt_shows. No network — _derive_season is
pure, and the store runs against a throwaway SQLite file per test.
"""
from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db, distrakt
from app.providers.base import ItemKey
from app.providers.trakt.detail import _derive_season
from tests.support import new_db_path

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 8, 1, tzinfo=UTC)  # fixed "today" so started/finished is stable


def _ep(number: int, iso_date: str | None):
    """A minimal Trakt season episode. iso_date=None => air date unknown."""
    return {"number": number, "first_aired": f"{iso_date}T18:00:00.000Z" if iso_date else None}


async def make_user(username: str) -> int:
    """A distrakt-approved account to hang tracker rows off. The rows are keyed by
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


class StoreTests(DistraktTestCase):
    async def test_add_show_creates_and_reads_back(self):
        self.assertIsNone(await distrakt.load_month(self.user_id, "2026-07"))
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"trakt": 12345, "tmdb": 900, "slug": "the-westies"},
            "title": "The Westies",
            "season": 1, "network": "MGM+", "watched": 0, "total": 8,
            "cadence": "Sun", "premiere": "7/12", "finale": "8/23", "bucket": "returning",
        })
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["month"], "2026-07")
        self.assertFalse(doc["closed"])
        self.assertEqual(len(doc["shows"]), 1)
        rec = doc["shows"][0]
        # Keyed on the shared id, with the service's own id kept as an attribute.
        self.assertEqual(rec["match_source"], "tmdb")
        self.assertEqual(rec["match_id"], "900")
        self.assertEqual(rec["key"], "show:tmdb:900")
        self.assertEqual(rec["ids"]["trakt"], 12345)
        self.assertEqual(rec["season"], 1)
        self.assertEqual(rec["total"], 8)
        self.assertEqual(rec["cadence"], "Sun")
        self.assertFalse(rec["abandoned"])
        self.assertIsNone(rec["abandoned_form"])

    async def test_add_show_upserts_by_identity_and_season(self):
        await distrakt.add_show(self.user_id, "2026-07",
                                {"ids": {"tmdb": 1}, "season": 2, "title": "X", "watched": 3, "total": 10})
        await distrakt.add_show(self.user_id, "2026-07",
                                {"ids": {"tmdb": 1}, "season": 2, "watched": 7, "total": 12})
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertEqual(len(doc["shows"]), 1)          # updated, not duplicated
        self.assertEqual(doc["shows"][0]["watched"], 7)
        self.assertEqual(doc["shows"][0]["total"], 12)
        self.assertEqual(doc["shows"][0]["title"], "X")  # untouched column preserved

    async def test_different_season_is_a_new_record(self):
        await distrakt.add_show(self.user_id, "2026-07", {"ids": {"tmdb": 1}, "season": 1, "title": "X"})
        await distrakt.add_show(self.user_id, "2026-07", {"ids": {"tmdb": 1}, "season": 2, "title": "X"})
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertEqual(len(doc["shows"]), 2)

    async def test_set_abandoned_toggle(self):
        await distrakt.add_show(self.user_id, "2026-07", {"ids": {"tmdb": 9}, "season": 1, "title": "Bel-Air"})
        key = ItemKey("show", "tmdb", "9")
        rec = await distrakt.set_abandoned(self.user_id, "2026-07", key, 1, True,
                                           abandoned_form="`Bel-Air S01 (0/6)`")
        self.assertIsNotNone(rec)
        self.assertTrue(rec["abandoned"])
        self.assertEqual(rec["abandoned_form"], "`Bel-Air S01 (0/6)`")
        # Persisted
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertTrue(doc["shows"][0]["abandoned"])
        # Un-abandon clears the frozen form
        rec2 = await distrakt.set_abandoned(self.user_id, "2026-07", key, 1, False)
        self.assertFalse(rec2["abandoned"])
        self.assertIsNone(rec2["abandoned_form"])

    async def test_set_abandoned_missing_returns_none(self):
        missing = ItemKey("show", "tmdb", "404")
        self.assertIsNone(await distrakt.set_abandoned(self.user_id, "2026-07", missing, 1, True))  # no month
        await distrakt.add_show(self.user_id, "2026-07", {"ids": {"tmdb": 1}, "season": 1})
        self.assertIsNone(await distrakt.set_abandoned(self.user_id, "2026-07", missing, 1, True))  # no such show

    async def test_list_months_sorted(self):
        await distrakt.add_show(self.user_id, "2026-08", {"ids": {"tmdb": 1}, "season": 1})
        await distrakt.add_show(self.user_id, "2026-07", {"ids": {"tmdb": 1}, "season": 1})
        await distrakt.add_show(self.user_id, "2026-12", {"ids": {"tmdb": 1}, "season": 1})
        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-07", "2026-08", "2026-12"])

    async def test_invalid_month_rejected(self):
        with self.assertRaises(ValueError):
            await distrakt.load_month(self.user_id, "2026-13")
        with self.assertRaises(ValueError):
            await distrakt.add_show(self.user_id, "July", {"ids": {"tmdb": 1}, "season": 1})

    async def test_remove_show(self):
        await distrakt.add_show(self.user_id, "2026-06", {"ids": {"tmdb": 55}, "season": 1, "title": "Oops"})
        await distrakt.add_show(self.user_id, "2026-06", {"ids": {"tmdb": 66}, "season": 2, "title": "Keep"})
        gone = ItemKey("show", "tmdb", "55")
        self.assertTrue(await distrakt.remove_show(self.user_id, "2026-06", gone, 1))
        self.assertFalse(await distrakt.remove_show(self.user_id, "2026-06", gone, 1))  # already gone
        doc = await distrakt.load_month(self.user_id, "2026-06")
        self.assertEqual({(s["key"], s["season"]) for s in doc["shows"]}, {("show:tmdb:66", 2)})

    async def test_frozen_snapshot_columns_survive_a_round_trip(self):
        """A frozen month renders offline from these columns alone, so they have to
        come back exactly as written — including the airing flags and the
        month-level movies snapshot."""
        doc = distrakt.new_month_doc("2026-05")
        doc["shows"] = [{
            "ids": {"trakt": 7, "tmdb": 42, "slug": "s"}, "media": "show", "title": "T",
            "season": 3, "network": "N", "abandoned": False, "abandoned_form": None,
            "watched": 5, "total": 8, "cadence": "Tue", "premiere": "5/1",
            "finale": "5/29", "bucket": "keepup",
            "started_airing": True, "finished_airing": False,
        }]
        doc["closed"] = True
        doc["totals_refreshed_at"] = db.now()
        doc["movies"] = [{"title": "A Film", "year": 2026, "watched_at": "2026-05-04T00:00:00Z"}]
        await distrakt.save_month(self.user_id, doc)

        back = await distrakt.load_month(self.user_id, "2026-05")
        self.assertTrue(back["closed"])
        self.assertEqual(back["movies"], doc["movies"])
        rec = back["shows"][0]
        self.assertTrue(rec["started_airing"])
        self.assertFalse(rec["finished_airing"])
        self.assertEqual(rec["bucket"], "keepup")
        self.assertEqual(rec["ids"], {"trakt": 7, "tmdb": 42, "slug": "s"})
        # frozen_shows reads them straight back with no Trakt call.
        self.assertTrue(distrakt.frozen_shows(back)[0]["started_airing"])


class RecordIdentityTests(unittest.TestCase):
    """How a record gets the identity it is filed under. Pure — no database."""

    def test_it_runs_the_waterfall_over_whatever_ids_the_record_has(self):
        key = distrakt.record_key({"ids": {"trakt": 9, "tvdb": 5, "tmdb": 7}, "season": 1})
        self.assertEqual(str(key), "show:tmdb:7")

    def test_a_record_that_already_carries_a_triple_keeps_it(self):
        """A row read back from storage must resolve to the identity it was WRITTEN
        under, even if it has since learned a better id — re-running the waterfall
        on it would silently address a different row."""
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
        """It is what the browser names a row by, so every record carries it
        rather than each caller spelling it out."""
        rec = distrakt.normalize_show({"ids": {"tmdb": 7}, "season": 2, "title": "T"})
        self.assertEqual(rec["key"], "show:tmdb:7")
        self.assertEqual((rec["match_source"], rec["match_id"]), ("tmdb", "7"))


class StoredIdentityTests(DistraktTestCase):
    async def test_two_services_reporting_one_title_are_one_row(self):
        """THE POINT OF THE RE-KEY. The same season arriving with a different
        provider id but the same shared id updates the row rather than doubling
        it — which is what keying on the provider's id could never do."""
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"trakt": 111, "tmdb": 900}, "season": 1, "title": "A Show"})
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"simkl": 222, "tmdb": 900}, "season": 1, "watched": 4})

        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertEqual(len(doc["shows"]), 1)
        rec = doc["shows"][0]
        self.assertEqual(rec["watched"], 4)
        # and it now knows both services' ids, so either can be called about it
        self.assertEqual(rec["ids"], {"trakt": 111, "simkl": 222, "tmdb": 900})

    async def test_a_row_keyed_on_a_weaker_id_stays_its_own_row(self):
        """The residual risk this design accepts, pinned so it is a known
        property rather than a surprise: a title first seen with only a tvdb is
        keyed on it, and a later record carrying tmdb is a different row. The ids
        stored on both are what a resolution pass would use to join them later."""
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"tvdb": 5}, "season": 1, "title": "Early"})
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"tvdb": 5, "tmdb": 900}, "season": 1, "title": "Later"})
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertEqual({s["key"] for s in doc["shows"]},
                         {"show:tvdb:5", "show:tmdb:900"})

    async def test_a_row_can_learn_an_id_it_did_not_have(self):
        """The upgrade path the stored ids exist for."""
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"tmdb": 900}, "season": 1, "title": "A Show"})
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"tmdb": 900, "imdb": "tt42"}, "season": 1})
        rec, = (await distrakt.load_month(self.user_id, "2026-07"))["shows"]
        self.assertEqual(rec["ids"], {"tmdb": 900, "imdb": "tt42"})

    async def test_a_movie_and_a_show_sharing_a_tmdb_id_are_different_titles(self):
        """TMDB ids are namespaced per media kind, so the media type has to be
        part of the key or one would overwrite the other."""
        await distrakt.add_show(self.user_id, "2026-07", {
            "media": "show", "ids": {"tmdb": 550}, "season": 1, "title": "The Show"})
        await distrakt.add_show(self.user_id, "2026-07", {
            "media": "movie", "ids": {"tmdb": 550}, "season": 1, "title": "The Film"})
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertEqual({s["title"] for s in doc["shows"]}, {"The Show", "The Film"})

    async def test_provenance_is_not_rewritten_by_a_later_write(self):
        """`added_by` records who FIRST put the row there: the live-counts writer
        goes through the same upsert, and a calendar row quietly becoming a manual
        one would change what removing it does to the calendar."""
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"tmdb": 900}, "season": 1, "added_by": distrakt.ADDED_BY_CALENDAR})
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"tmdb": 900}, "season": 1, "watched": 3,
            "added_by": distrakt.ADDED_BY_MANUAL})
        rec, = (await distrakt.load_month(self.user_id, "2026-07"))["shows"]
        self.assertEqual(rec["added_by"], distrakt.ADDED_BY_CALENDAR)
        self.assertEqual(rec["watched"], 3)


if __name__ == "__main__":
    unittest.main()
