"""Unit tests for the distrakt lazy month rollover, per user.

Covers ensure_month's orchestration (freeze prior -> carry forward minus
completed/abandoned -> premieres minus not-watching -> in-progress history) and
the staleness helpers, with every Trakt call mocked out. The main-calendar
not-watching overlay is NOT mocked: real rows go into not_watching_shows
through app/calendar_state, since "the roster is built from this user's own
calendar decisions" is exactly the wiring worth proving.

Also covers the per-user guarantees the shared-document model could not make:
two users' rosters are fully independent in both directions, and the prior-month
freeze fires per user independently on first access on/after the 1st.

No network. Each test runs against a throwaway SQLite file.
"""
from __future__ import annotations

import unittest
from dataclasses import replace as dataclasses_replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import auth, calendar_state, db, distrakt, watch_history
from app.providers.base import Item, ItemKey, Media, Source
from tests.support import new_db_path

SETTINGS = SimpleNamespace(trakt_configured=True, network_emojis={}, default_network_emoji=":tv:",
                           timezone="UTC")


def _detail(total, watched_hint=None, cadence="Mon", premiere="7/6", finale=None,
            started=True, finished=False):
    return {
        "season": 1, "total": total, "cadence": cadence, "premiere": premiere,
        "finale": finale, "started_airing": started, "finished_airing": finished,
        "air_dates": [],
    }


# Per-(trakt_id) season fixtures used by both the freeze pass and history step.
_DETAILS = {
    101: _detail(8, cadence="Mon", started=True, finished=False),           # keepup
    102: _detail(8, started=True, finished=True, finale="7/28"),            # completed (8/8)
    103: _detail(6, started=True),                                          # abandoned (flag wins)
    401: _detail(10, cadence="Tue", started=True, finished=False),          # history in-progress
}

# Watched-episode counts (the live `x`).
_WATCHED = {(101, 1): 2, (102, 1): 8, (103, 1): 0, (401, 1): 3}


async def _fake_season_detail(settings, trakt_id, season, fresh=False, client=None):
    return dict(_DETAILS[int(trakt_id)], season=int(season))


async def _fake_sync_and_baseline(settings, user_id, roster, force=False, today=None):
    """Stand-in for the watch-history cache: build a state whose watched_map()
    yields _WATCHED (filtered to the roster records it was handed).

    Takes the records the real one takes, and files its entries under the same
    shared identity, so a change to how a season is named fails here rather than
    passing with a lookup that quietly matches nothing.
    """
    wanted = {int((rec.get("ids") or {}).get("tmdb") or 0) for rec in roster}
    shows: dict = {}
    for (tid, season), cnt in _WATCHED.items():
        if tid in wanted:
            entry = shows.setdefault(_key(tid), {"ids": _ids(tid), "seasons": {}})
            entry["seasons"][str(season)] = list(range(cnt))
    return {"shows": shows, "movies": {}, "last_synced": "2026-01-01", "beacons": None}


def _key(tid) -> str:
    """The item key a fixture's numeric id resolves to (its tmdb wins the
    waterfall), so a test can name a row without spelling the triple out."""
    return f"show:tmdb:{int(tid)}"


def _ids(tid) -> dict:
    return {"trakt": int(tid), "tmdb": int(tid), "slug": f"slug-{int(tid)}"}


def _cal_item(tid, season, title, network="Net"):
    """One calendar item as the shared cache hands it to the tracker's import.

    A real Item rather than a dict shaped like one: the tracker reads it through
    the same attributes the live read path produces, so a field renamed out from
    under this fake fails here instead of passing and then failing in production.
    """
    return Item(
        source=Source.TRAKT, media=Media.SHOW, id=f"slug-{tid}",
        ids={"trakt": tid, "tmdb": tid, "slug": f"slug-{tid}"},
        detail_url=f"https://trakt.tv/shows/slug-{tid}",
        air_date="2026-08-01", air_ts=0.0, air_display="01 Aug 2026",
        air_time="20:00", day_of_week="Saturday",
        title=title, season=season, network=network,
    )


def _raw_entry(tid, season, title, *, first_aired, certification=None, network="Net"):
    """A calendar entry in the shape calendar_cache.fetch_window_raw returns:
    pruned, un-normalized, still carrying whatever real filter_entries reads
    (certification included) so mocking at this layer exercises the real
    per-user filter inside calendar_cache.read_month rather than a test double
    standing in for it."""
    return {
        "show": {
            "title": title, "network": network, "certification": certification,
            "ids": {"slug": f"slug-{tid}", "trakt": tid, "tmdb": tid},
        },
        "first_aired": first_aired,
        "episode": {"season": season, "number": 1, "title": "Pilot"},
    }


async def _seed_user_prefs(user_id: int, **overrides) -> None:
    """A minimal user_prefs row for tests that need a real (non-default) per-user
    filter, seeded the same way onboarding seeds one (auth.insert_user_prefs)."""
    fields = dict(
        endpoint="shows/new", card_style="poster", day_packing="auto",
        hide_not_watching=False, network_filter=[], genres="", countries="",
        show_certifications="", movie_certifications="",
    )
    fields.update(overrides)
    prefs_settings = SimpleNamespace(**fields)
    await db.transaction(lambda conn: auth.insert_user_prefs(
        conn, user_id, prefs_settings, seed_filters=True,
    ))


async def _make_user(username: str) -> int:
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
        "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


class RolloverTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh database + one distrakt user per test."""
    async def asyncSetUp(self):
        new_db_path("rollover")
        await db.migrate()
        self.user_id = await _make_user("tracker")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _keys(self, doc):
        """(numeric id, season) pairs. The fixtures' tmdb ids are what the rows
        are keyed on, so this reads the stored identity rather than a second field
        that might happen to agree with it."""
        return {(int(s["match_id"]), s["season"]) for s in doc["shows"]}

    async def _mark_not_watching(self, user_id, year, month, item_id,
                                 endpoint="shows/new"):
        """A real main-calendar "not watching" mark, the same write the calendar
        page makes. The year/month/endpoint are accepted and ignored: a mark is a
        statement about the show, not about the view it was made in."""
        await calendar_state.set_not_watching(user_id, item_id, True)


class RolloverTests(RolloverTestCase):
    async def test_first_month_init_from_scratch(self):
        """No prior month: seed premieres (new/returning) + in-progress history."""
        async def fake_read_month(endpoint, settings, **kw):
            if endpoint.key == "shows/new":
                return [_cal_item(201, 1, "New One")], None
            return [_cal_item(201, 1, "New One"), _cal_item(301, 2, "Returner")], None

        async def fake_progress(settings, since_days=60):
            return [{"ids": _ids(401), "season": 1, "watched": 3,
                     "title": "Backlog", "network": "Net"}]

        with patch("app.calendar_cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail):
            doc = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS)

        self.assertFalse(doc["closed"])
        self.assertEqual(await self._keys(doc), {(201, 1), (301, 2), (401, 1)})
        self.assertIsNotNone(doc["totals_refreshed_at"])

    async def test_not_watching_premiere_excluded(self):
        """A premiere toggled not-watching BEFORE commit is simply excluded."""
        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "New One"), _cal_item(202, 1, "Skip Me")], None

        async def fake_progress(settings, since_days=60):
            return []

        # not-watching stores the calendar card id = slug (preferred) or trakt id.
        await self._mark_not_watching(self.user_id, 2026, 8, "slug-202")

        with patch("app.calendar_cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail):
            doc = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS)

        self.assertEqual(await self._keys(doc), {(201, 1)})

    async def test_rollover_freezes_prior_and_drops_completed_abandoned(self):
        """July -> August: freeze July, carry active only, add August premieres."""
        # Seed an OPEN July with an active, a completed, and an abandoned show.
        for tid, title in ((101, "Active"), (102, "Done"), (103, "Gone")):
            await distrakt.add_show(self.user_id, "2026-07", {
                "ids": _ids(tid), "season": 1, "title": title,
                "network": "Net"})
        await distrakt.set_abandoned(self.user_id, "2026-07", ItemKey("show", "tmdb", "103"), 1, True,
                                     abandoned_form="`Gone S01 (0/6)`")

        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "Aug New")], None

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.calendar_cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            # today >= Aug 1 -> August has begun, so July freezes on this access.
            aug = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=date(2026, 8, 1))

        # July is now frozen with buckets persisted.
        july = await distrakt.load_month(self.user_id, "2026-07")
        self.assertTrue(july["closed"])
        self.assertIsNotNone(july["totals_refreshed_at"])
        by_id = {s["ids"]["trakt"]: s for s in july["shows"]}
        self.assertEqual(by_id[101]["bucket"], "keepup")
        self.assertEqual(by_id[102]["bucket"], "completed")
        self.assertEqual(by_id[103]["bucket"], "abandoned")
        self.assertIn("started_airing", by_id[101])  # frozen snapshot renders offline
        self.assertTrue(by_id[101]["started_airing"])

        # August: dropped 102 (completed) + 103 (abandoned); kept 101; added 201.
        self.assertEqual(await self._keys(aug), {(101, 1), (201, 1)})
        carried = next(s for s in aug["shows"] if s["key"] == _key(101))
        self.assertFalse(carried["abandoned"])
        self.assertIsNone(carried["abandoned_form"])

    async def test_frozen_month_returns_untouched(self):
        """An already-closed month is returned as-is with no Trakt calls."""
        await distrakt.add_show(self.user_id, "2026-05",
                                {"ids": _ids(900), "season": 1, "title": "X"})
        doc = await distrakt.load_month(self.user_id, "2026-05")
        doc["closed"] = True
        await distrakt.save_month(self.user_id, doc)
        with patch("app.calendar_cache.read_month", new=AsyncMock()) as cal, \
             patch("app.providers.trakt.sync.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(self.user_id, 2026, 5, SETTINGS)
        self.assertTrue(out["closed"])
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_unconfigured_month_not_persisted(self):
        out = await distrakt.ensure_month(self.user_id, 2026, 9, SimpleNamespace(trakt_configured=False))
        self.assertEqual(out["shows"], [])
        self.assertIsNone(await distrakt.load_month(self.user_id, "2026-09"))  # nothing written

    async def test_backward_nav_does_not_backfill(self):
        """Navigating to a never-tracked PAST month must NOT create it."""
        await distrakt.add_show(self.user_id, "2026-08",
                                {"ids": _ids(700), "season": 1, "title": "Seed"})
        # July is earlier than the only tracked month (Aug) -> blocked.
        self.assertTrue(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))
        with patch("app.calendar_cache.read_month", new=AsyncMock()) as cal, \
             patch("app.providers.trakt.sync.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(self.user_id, 2026, 7, SETTINGS)
        self.assertEqual(out["shows"], [])
        self.assertIsNone(await distrakt.load_month(self.user_id, "2026-07"))  # not written
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_preview_does_not_freeze_prior(self):
        """Accessing August BEFORE Aug 1 must NOT freeze July (still current)."""
        await distrakt.add_show(self.user_id, "2026-07", {
            "ids": _ids(101), "season": 1, "title": "Active",
            "network": "Net"})

        async def fake_read_month(endpoint, settings, **kw):
            return [], None  # no August premieres in this scenario

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.calendar_cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            aug = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=date(2026, 7, 20))

        july = await distrakt.load_month(self.user_id, "2026-07")
        self.assertFalse(july["closed"])                      # stays open during preview
        self.assertEqual(await self._keys(aug), {(101, 1)})   # still carried forward live

    async def test_import_premieres_merges_skipping_existing_and_not_watching(self):
        await distrakt.add_show(self.user_id, "2026-08",
                                {"ids": _ids(201), "season": 1, "title": "Already"})

        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "Already"), _cal_item(202, 1, "Fresh"), _cal_item(203, 1, "Skip")], None

        await self._mark_not_watching(self.user_id, 2026, 8, "slug-203")

        with patch("app.calendar_cache.read_month", side_effect=fake_read_month):
            await distrakt.import_premieres(self.user_id, "2026-08", SETTINGS)

        doc = await distrakt.load_month(self.user_id, "2026-08")
        keys = {(int(s["match_id"]), s["season"]) for s in doc["shows"]}
        self.assertEqual(keys, {(201, 1), (202, 1)})  # 201 not duplicated, 203 excluded

    async def test_remove_show(self):
        await distrakt.add_show(self.user_id, "2026-06",
                                {"ids": _ids(55), "season": 1, "title": "Oops"})
        await distrakt.add_show(self.user_id, "2026-06",
                                {"ids": _ids(66), "season": 2, "title": "Keep"})
        self.assertTrue(await distrakt.remove_show(self.user_id, "2026-06", ItemKey("show", "tmdb", "55"), 1))
        self.assertFalse(await distrakt.remove_show(self.user_id, "2026-06", ItemKey("show", "tmdb", "55"), 1))  # already gone
        doc = await distrakt.load_month(self.user_id, "2026-06")
        self.assertEqual({(int(s["match_id"]), s["season"]) for s in doc["shows"]}, {(66, 2)})


class _Resp:
    """A minimal stand-in for the httpx response fetch_window_raw reads."""
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.headers: dict = {}

    def json(self):
        return self._data


class _FixedClient:
    """Replies with the same canned window body to every request, regardless of
    the window's date range — fetch_window_raw's own in_window trim (see
    app/calendar_cache.py) is what actually decides which entries survive for
    which window, so this only needs to hand back the full candidate set."""
    def __init__(self, entries):
        self.entries = entries

    async def get(self, url, headers=None, timeout=None):
        return _Resp(self.entries)


class DistraktImportFilterTests(RolloverTestCase):
    """distrakt's premiere import reads through the shared calendar cache, so it
    must honor both the instance-wide content floor (applied at fetch time,
    before anything is cached) and the importing user's own
    genre/country/certification prefs (applied at read time, same as their
    main calendar) — not just import everything the floor happens to allow."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.config import Settings
        self.settings = Settings(trakt_client_id="cid", trakt_access_token="tok", timezone="UTC")

    async def _open_month(self, month_key: str) -> None:
        await distrakt.save_month(self.user_id, distrakt.new_month_doc(month_key))

    async def test_own_certification_filter_excludes_a_show_from_import(self):
        """The importing user's own show_certifications spec keeps a matching
        show out of their roster, even though the instance floor allows it."""
        entries = [
            _raw_entry(201, 1, "Mature One", first_aired="2026-08-05T20:00:00.000Z",
                      certification="TV-MA"),
            _raw_entry(202, 1, "General One", first_aired="2026-08-12T20:00:00.000Z",
                      certification="TV-G"),
        ]
        await _seed_user_prefs(self.user_id, show_certifications="-tv-ma")
        await self._open_month("2026-08")

        with patch("app.providers.trakt.transport.shared_client", return_value=_FixedClient(entries)):
            await distrakt.import_premieres(self.user_id, "2026-08", self.settings)

        doc = await distrakt.load_month(self.user_id, "2026-08")
        self.assertEqual(await self._keys(doc), {(202, 1)})

    async def test_instance_floor_excludes_a_show_regardless_of_user_filters(self):
        """A show the instance-wide floor excludes never reaches the shared
        cache, so it stays out of import even for a user with NO personal
        filter of their own — the floor isn't something import has to
        re-check, it's structurally already gone by the time import reads."""
        floored_settings = dataclasses_replace(self.settings, show_certifications="-tv-ma")
        entries = [
            _raw_entry(201, 1, "Mature One", first_aired="2026-08-05T20:00:00.000Z",
                      certification="TV-MA"),
            _raw_entry(202, 1, "General One", first_aired="2026-08-12T20:00:00.000Z",
                      certification="TV-G"),
        ]
        # No per-user filter set at all — the floor alone must do the work.
        await self._open_month("2026-08")

        with patch("app.providers.trakt.transport.shared_client", return_value=_FixedClient(entries)):
            await distrakt.import_premieres(self.user_id, "2026-08", floored_settings)

        doc = await distrakt.load_month(self.user_id, "2026-08")
        self.assertEqual(await self._keys(doc), {(202, 1)})


class PerUserIsolationTests(RolloverTestCase):
    """The guarantees the shared-document model could not make."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other_id = await _make_user("other")

    async def test_two_users_rosters_are_independent_in_both_directions(self):
        await distrakt.add_show(self.user_id, "2026-08",
                                {"ids": _ids(111), "season": 1, "title": "Mine"})
        await distrakt.add_show(self.other_id, "2026-08",
                                {"ids": _ids(222), "season": 1, "title": "Theirs"})

        mine = await distrakt.load_month(self.user_id, "2026-08")
        theirs = await distrakt.load_month(self.other_id, "2026-08")
        self.assertEqual({s["ids"]["trakt"] for s in mine["shows"]}, {111})
        self.assertEqual({s["ids"]["trakt"] for s in theirs["shows"]}, {222})

        # A mutation on one side is invisible on the other, both ways round.
        await distrakt.set_abandoned(self.user_id, "2026-08", ItemKey("show", "tmdb", "111"), 1, True, abandoned_form="`f`")
        self.assertFalse((await distrakt.load_month(self.other_id, "2026-08"))["shows"][0]["abandoned"])
        self.assertIsNone(await distrakt.set_abandoned(self.other_id, "2026-08", ItemKey("show", "tmdb", "111"), 1, True))

        # Deleting the other user's only show leaves this user's roster intact.
        self.assertTrue(await distrakt.remove_show(self.other_id, "2026-08", ItemKey("show", "tmdb", "222"), 1))
        self.assertFalse(await distrakt.remove_show(self.other_id, "2026-08", ItemKey("show", "tmdb", "111"), 1))
        self.assertEqual(len((await distrakt.load_month(self.user_id, "2026-08"))["shows"]), 1)

    async def test_month_lists_and_backfill_gates_are_per_user(self):
        await distrakt.add_show(self.user_id, "2026-08", {"ids": _ids(1), "season": 1})
        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-08"])
        self.assertEqual(await distrakt.list_months(self.other_id), [])
        # A month that is backward/blocked for one user is a clean first seed for
        # the other, whose store is still empty.
        self.assertTrue(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))
        self.assertFalse(await distrakt.is_backfill_blocked(self.other_id, "2026-07"))

    async def test_not_watching_overlay_is_per_user(self):
        """One user's calendar not-watching mark must not drop a premiere from
        anyone else's roster."""
        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "Shared Premiere")], None

        async def fake_progress(settings, since_days=60):
            return []

        await self._mark_not_watching(self.user_id, 2026, 8, "slug-201")

        with patch("app.calendar_cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail):
            mine = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS)
            theirs = await distrakt.ensure_month(self.other_id, 2026, 8, SETTINGS)

        self.assertEqual(await self._keys(mine), set())          # excluded for me
        self.assertEqual(await self._keys(theirs), {(201, 1)})   # still there for them

    async def test_freeze_prior_month_fires_per_user_independently(self):
        """Reaching the 1st freezes only the accessing user's prior month; another
        user's July stays open until THEY first access August."""
        for uid in (self.user_id, self.other_id):
            await distrakt.add_show(uid, "2026-07", {
                "ids": _ids(101), "season": 1, "title": "Active",
                "network": "Net"})

        async def fake_read_month(endpoint, settings, **kw):
            return [], None

        async def fake_progress(settings, since_days=60):
            return []

        patches = (
            patch("app.calendar_cache.read_month", side_effect=fake_read_month),
            patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress),
            patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail),
            patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS, today=date(2026, 8, 1))

            self.assertTrue((await distrakt.load_month(self.user_id, "2026-07"))["closed"])
            self.assertFalse((await distrakt.load_month(self.other_id, "2026-07"))["closed"])

            # The other user's own first access on/after the 1st freezes theirs.
            await distrakt.ensure_month(self.other_id, 2026, 8, SETTINGS, today=date(2026, 8, 1))
            self.assertTrue((await distrakt.load_month(self.other_id, "2026-07"))["closed"])


class CanInitializeTests(RolloverTestCase):
    async def test_empty_store_seeds_anything(self):
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2026-07"))
        self.assertFalse(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))

    async def test_forward_allowed_backward_blocked(self):
        await distrakt.save_month(self.user_id, distrakt.new_month_doc("2026-08"))
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2026-09"))   # forward
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2027-01"))   # forward (year wrap)
        self.assertFalse(await distrakt.can_initialize(self.user_id, "2026-07"))  # backward
        self.assertFalse(await distrakt.can_initialize(self.user_id, "2026-08"))  # already latest
        self.assertTrue(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))
        self.assertFalse(await distrakt.is_backfill_blocked(self.user_id, "2026-09"))


class MonthCommittedTests(unittest.TestCase):
    def test_committed_boundary(self):
        self.assertFalse(distrakt.month_committed("2026-08", date(2026, 7, 31)))
        self.assertTrue(distrakt.month_committed("2026-08", date(2026, 8, 1)))
        self.assertTrue(distrakt.month_committed("2026-08", date(2026, 9, 15)))
        self.assertFalse(distrakt.month_committed("2027-01", date(2026, 12, 31)))
        self.assertTrue(distrakt.month_committed("2027-01", date(2027, 1, 1)))


class StalenessTests(unittest.TestCase):
    """totals_refreshed_at is whole UTC seconds, the representation every
    timestamp column in this schema uses."""

    def test_missing_timestamp_is_stale(self):
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": None}))
        self.assertTrue(distrakt.is_stale({}))
        self.assertTrue(distrakt.is_stale(None))

    def test_recent_is_fresh(self):
        self.assertFalse(distrakt.is_stale({"totals_refreshed_at": db.now()}))

    def test_old_is_stale(self):
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": db.now() - 25 * 3600}))

    def test_unparseable_timestamp_is_stale(self):
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": "not-a-number"}))


class StampRefreshedTests(RolloverTestCase):
    async def test_stamp_refreshed_clears_staleness_for_that_user_only(self):
        other = await _make_user("other")
        for uid in (self.user_id, other):
            doc = distrakt.new_month_doc("2026-07")
            doc["totals_refreshed_at"] = db.now() - 30 * 3600
            await distrakt.save_month(uid, doc)

        await distrakt.stamp_refreshed(self.user_id, "2026-07")

        self.assertFalse(distrakt.is_stale(await distrakt.load_month(self.user_id, "2026-07")))
        self.assertTrue(distrakt.is_stale(await distrakt.load_month(other, "2026-07")))


class CompletedBelongsToTheMonthItHappenedInTests(RolloverTestCase):
    """Completed means completed THIS month.

    Rollover refuses to carry a completed show into a new month, but it decides
    that once, when the month is created. A show carried into August during the
    July preview and finished in July afterwards stayed on August's roster and,
    being fully watched, sat in August's Completed for good — and in September's,
    and every month opened early after that.
    """

    async def _roster(self, month: str, *, completed_on: str) -> list[dict]:
        await distrakt.add_show(self.user_id, month, {
            "ids": _ids(700), "season": 1, "title": "Finished",
            "network": "Net", "media": "show",
        })
        doc = await distrakt.load_month(self.user_id, month)
        shows = [{**s, "watched": 8, "total": 8, "bucket": "completed",
                  "completed_on": completed_on} for s in doc["shows"]]
        return await distrakt.drop_seasons_finished_earlier(self.user_id, month, shows)

    async def test_a_season_finished_last_month_leaves_this_month(self):
        kept = await self._roster("2026-08", completed_on="2026-07-30")
        self.assertEqual(kept, [])
        # and the roster row goes with it, so it stops costing a season fetch
        doc = await distrakt.load_month(self.user_id, "2026-08")
        self.assertEqual(doc["shows"], [])

    async def test_a_season_finished_this_month_stays(self):
        kept = await self._roster("2026-08", completed_on="2026-08-01")
        self.assertEqual([s["key"] for s in kept], [_key(700)])

    async def test_a_future_month_does_not_inherit_it_either(self):
        """September opened in July showed the same rows for the same reason."""
        self.assertEqual(await self._roster("2026-09", completed_on="2026-08-06"), [])

    async def test_a_season_with_no_known_finish_date_is_left_alone(self):
        """A show whose history has not been re-baselined yet has no date to
        judge by, and guessing would silently delete a roster row."""
        kept = await self._roster("2026-08", completed_on="")
        self.assertEqual([s["key"] for s in kept], [_key(700)])

    async def test_the_whole_path_from_watch_history_to_the_month_payload(self):
        """The reported bug, through the real machinery: watch history says the
        season was finished on 30 July, the show is on August's roster, and
        August must not show it.

        Only Trakt itself is mocked — the watch-history cache, the completion
        dates it derives, the bucketing and the payload assembly are all real.
        """
        from app import distrakt_routes

        await distrakt.add_show(self.user_id, "2026-08", {
            "ids": _ids(102), "season": 1, "title": "Finished In July",
            "network": "Net", "media": "show",
        })
        # 8 of 8 episodes, the last of them watched on 30 July.
        progress = {1: {n: f"2026-07-{n + 10:02d}T00:00:00Z" for n in range(1, 8)}}
        progress[1][8] = "2026-07-30T20:00:00Z"

        async def no_premieres(endpoint, settings, **kw):
            return [], None

        # The payload builds the post's share link too, which reads a couple of
        # fields the rest of this file's SETTINGS stand-in has never needed.
        settings = SimpleNamespace(**vars(SETTINGS), public_base_url="")

        with patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={102: progress}), \
             patch("app.providers.trakt.sync.fetch_last_activities", return_value={}), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.calendar_cache.read_month", side_effect=no_premieres), \
             patch("app.logos.ensure_logos", new=AsyncMock(return_value=None)):
            payload, status = await distrakt_routes._distrakt_month_payload(self.user_id, 2026, 8, settings)

        self.assertEqual(status, 200)
        self.assertEqual(payload["shows"], [])
        self.assertEqual(await self._keys(await distrakt.load_month(self.user_id, "2026-08")), set())

    async def test_only_a_finished_season_carries_a_finish_date(self):
        """The date comes from the last episode watched, which on a partly
        watched season is just "last time I watched something" — compute_live_shows
        keeps it only for a season it has bucketed as completed, so a half-watched
        show can never be dropped by this rule."""
        state = {"shows": {_key(102): {"ids": _ids(102),
                                       "seasons": {"1": {"1": "2026-07-02T00:00:00Z"}}}}}
        self.assertEqual(watch_history.season_completed_map(state),
                         {(_key(102), 1): "2026-07-02"})

        # 102's season has 8 episodes; one of them is watched.
        records = [{"ids": _ids(102), "season": 1, "title": "Half"}]
        with patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail):
            shows = await distrakt.compute_live_shows(
                self.user_id, records, SETTINGS,
                watched_lookup={(_key(102), 1): 1},
                completed_lookup=watch_history.season_completed_map(state),
            )
        self.assertNotEqual(shows[0]["bucket"], "completed")
        self.assertEqual(shows[0]["completed_on"], "")


if __name__ == "__main__":
    unittest.main()
