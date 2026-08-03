"""Unit tests for the distrakt lazy month rollover, per user.

Covers ensure_month's orchestration (freeze the month the calendar has passed ->
this month's premieres minus not-watching -> recent viewing onto the viewer's own
list) and the staleness helpers, with every Trakt call mocked out. The
main-calendar not-watching overlay is NOT mocked: real rows go into
not_watching_shows through app/calendar/state.py, since "a month is built from
this user's own calendar decisions" is exactly the wiring worth proving.

Also covers the per-user guarantees the shared-document model could not make:
two users' records are fully independent in both directions, and the prior-month
freeze fires per user independently on first access on/after the 1st.

No network. Each test runs against a throwaway SQLite file.
"""
from __future__ import annotations

import unittest
from dataclasses import replace as dataclasses_replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import auth, db, distrakt
from app.distrakt import rollover, watch_history
from app.calendar import state as calendar_state
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
    # A premiere that has not aired yet — what a month ahead is allowed to hold.
    201: _detail(6, cadence="Fri", premiere="8/15", started=False),
}

# Watched-episode counts (the live `x`).
_WATCHED = {(101, 1): 2, (102, 1): 8, (103, 1): 0, (401, 1): 3}


async def _fake_season_detail(settings, trakt_id, season, fresh=False, client=None):
    return dict(_DETAILS[int(trakt_id)], season=int(season))


async def _fake_sync_and_baseline(settings, user_id, roster, force=False, today=None,
                                  since_month=None):
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
    """Fresh database + one distrakt user per test.

    THE DATE IS HANDED IN, NEVER READ OFF THE WALL. Every rule below compares a
    month key against today, so a test that let the real calendar answer that
    would be exercising a different rule each month and the wrong one most of
    them — this suite has twice gone red on the 1st for exactly that. `TODAY` is
    the day these tests stand on: CLOSING is behind it, UNDER_WAY is around it
    and AHEAD is in front, whenever the suite is run.
    """
    TODAY = date(2026, 8, 15)
    CLOSING = "2026-07"
    UNDER_WAY = "2026-08"
    AHEAD = "2026-09"

    async def asyncSetUp(self):
        new_db_path("rollover")
        await db.migrate()
        self.user_id = await _make_user("tracker")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _keys(self, doc):
        """(numeric id, season) pairs. The fixtures' tmdb ids are what the records
        are keyed on, so this reads the stored identity rather than a second field
        that might happen to agree with it."""
        return {(int(s["match_id"]), s["season"]) for s in doc["shows"]}

    async def _premiere(self, user_id, month_key, tid, season=1, title="Show", **extra):
        """One title announced as premiering in `month_key` — what an import
        leaves behind. The kind is stated, because the store refuses to guess
        one."""
        return await distrakt.add_month_record(user_id, month_key, {
            "ids": _ids(tid), "season": season, "title": title, "network": "Net",
            "media": "show", "kind": distrakt.premiere_kind(season), **extra})

    async def _settled(self, user_id, month_key, tid, kind, season=1, title="Show",
                       **extra):
        """One verdict `month_key` reached: completed, or given up on."""
        return await distrakt.add_month_record(user_id, month_key, {
            "ids": _ids(tid), "season": season, "title": title, "network": "Net",
            "media": "show", "kind": kind, **extra})

    async def _listed(self, user_id, tid, season=1, title="Show",
                      kind=distrakt.RecordKind.KEEPUP, **extra):
        """One season on the viewer's own list, which belongs to no month."""
        return await distrakt.add_user_record(user_id, {
            "ids": _ids(tid), "season": season, "title": title, "network": "Net",
            "media": "show", "kind": kind, **extra})

    async def _held(self, user_id):
        """(numeric id, season) for everything on the viewer's own list."""
        return {(int(s["match_id"]), s["season"])
                for s in await distrakt.user_records(user_id)}

    async def _mark_not_watching(self, user_id, item_id):
        """A real main-calendar "not watching" mark, the same write the calendar
        page makes.

        A MARK IS A STATEMENT ABOUT THE SHOW and carries no month and no view:
        neither which endpoint it was made from nor when it was made is read by
        anything. What it does here is keep the title out of a month being built
        at all — there is no record yet for a verdict to be about.
        """
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

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail):
            doc = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=self.TODAY)

        self.assertFalse(doc["closed"])
        # The month holds what BEGINS in it. The season somebody is part-way
        # through is a fact about them and lands on their own list instead.
        self.assertEqual(await self._keys(doc), {(201, 1), (301, 2)})
        self.assertEqual(await self._held(self.user_id), {(401, 1)})
        self.assertIsNotNone(doc["totals_refreshed_at"])

    async def test_not_watching_premiere_excluded(self):
        """A premiere the viewer has turned away is simply not built in."""
        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "New One"), _cal_item(202, 1, "Skip Me")], None

        async def fake_progress(settings, since_days=60):
            return []

        # not-watching stores the calendar card id = slug (preferred) or trakt id.
        await self._mark_not_watching(self.user_id, "slug-202")

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail):
            doc = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=self.TODAY)

        self.assertEqual(await self._keys(doc), {(201, 1)})

    async def test_rollover_freezes_prior_and_takes_nothing_from_it(self):
        """The closing month freezes, and the one under way is built from its own
        premieres.

        What the viewer is still in the middle of is a fact about THEM and lives
        on their own list, so a new month has nothing to inherit — the closing
        month keeps its own records, and the new one holds what starts in it."""
        # The closing month as a lived-in open month: one title it announced, one
        # season finished in it, one given up on in it. The still-running season
        # is on the viewer's list and on no month at all.
        await self._premiere(self.user_id, self.CLOSING, 101, title="Active")
        await self._listed(self.user_id, 101, title="Active")
        await self._settled(self.user_id, self.CLOSING, 102,
                            distrakt.RecordKind.COMPLETED, title="Done",
                            watched=8, total=8)
        await self._settled(self.user_id, self.CLOSING, 103,
                            distrakt.RecordKind.ABANDONED, title="Gone",
                            abandoned_form="`Gone S01 (0/6)`")

        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "Aug New")], None

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.distrakt.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            # today >= Aug 1 -> August has begun, so July freezes on this access.
            aug = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=date(2026, 8, 1))

        # The closing month is frozen, and each of its records still says what it
        # was: an announcement, a completion, an abandon.
        closing = await distrakt.load_month(self.user_id, self.CLOSING)
        self.assertTrue(closing["closed"])
        self.assertIsNotNone(closing["totals_refreshed_at"])
        by_id = {s["ids"]["trakt"]: s for s in closing["shows"]}
        self.assertEqual(by_id[101]["kind"], distrakt.RecordKind.SERIES_PREMIERE)
        self.assertEqual(by_id[102]["kind"], distrakt.RecordKind.COMPLETED)
        self.assertEqual(by_id[103]["kind"], distrakt.RecordKind.ABANDONED)
        # The freeze corrects the catalogue half of a premiere record, so the
        # frozen month renders its announcement offline afterwards.
        self.assertTrue(by_id[101]["started_airing"])

        # The new month holds its own premiere and nothing the closing month had.
        self.assertEqual(await self._keys(aug), {(201, 1)})
        # And the still-running season is still the viewer's, read off their own
        # list rather than off a copy the new month was given.
        self.assertEqual(await self._held(self.user_id), {(101, 1)})

    async def test_frozen_month_returns_untouched(self):
        """An already-closed month is returned as-is with no Trakt calls."""
        await self._premiere(self.user_id, "2026-05", 900, title="X")
        doc = await distrakt.load_month(self.user_id, "2026-05")
        doc["closed"] = True
        await distrakt.save_month(self.user_id, doc)
        with patch("app.calendar.cache.read_month", new=AsyncMock()) as cal, \
             patch("app.providers.trakt.sync.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(self.user_id, 2026, 5, SETTINGS,
                                              today=self.TODAY)
        self.assertTrue(out["closed"])
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_unconfigured_month_not_persisted(self):
        out = await distrakt.ensure_month(
            self.user_id, 2026, 9, SimpleNamespace(trakt_configured=False),
            today=self.TODAY)
        self.assertEqual(out["shows"], [])
        self.assertIsNone(await distrakt.load_month(self.user_id, self.AHEAD))

    async def test_backward_nav_does_not_backfill(self):
        """Navigating to a never-tracked PAST month must NOT create it."""
        await self._premiere(self.user_id, self.UNDER_WAY, 700, title="Seed")
        # The closing month is earlier than the only tracked month -> blocked.
        self.assertTrue(await distrakt.is_backfill_blocked(
            self.user_id, self.CLOSING, self.TODAY))
        with patch("app.calendar.cache.read_month", new=AsyncMock()) as cal, \
             patch("app.providers.trakt.sync.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(self.user_id, 2026, 7, SETTINGS,
                                              today=self.TODAY)
        self.assertEqual(out["shows"], [])
        self.assertIsNone(await distrakt.load_month(self.user_id, self.CLOSING))
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_preview_does_not_freeze_prior(self):
        """Accessing August BEFORE Aug 1 must NOT freeze July (still current)."""
        await self._premiere(self.user_id, self.CLOSING, 101, title="Active")

        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "Aug New")], None

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.distrakt.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            aug = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=date(2026, 7, 20))

        july = await distrakt.load_month(self.user_id, self.CLOSING)
        self.assertFalse(july["closed"])                      # stays open during preview
        # The month ahead takes what premieres in it and nothing else — see
        # _initialize_month, and WhatAMonthThatHasNotBegunTakesTests.
        self.assertEqual(await self._keys(aug), {(201, 1)})

    async def test_import_premieres_merges_skipping_existing_and_not_watching(self):
        await self._premiere(self.user_id, self.UNDER_WAY, 201, title="Already")

        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "Already"), _cal_item(202, 1, "Fresh"), _cal_item(203, 1, "Skip")], None

        await self._mark_not_watching(self.user_id, "slug-203")

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month):
            await distrakt.import_premieres(self.user_id, self.UNDER_WAY, SETTINGS)

        doc = await distrakt.load_month(self.user_id, self.UNDER_WAY)
        keys = {(int(s["match_id"]), s["season"]) for s in doc["shows"]}
        self.assertEqual(keys, {(201, 1), (202, 1)})  # 201 not duplicated, 203 excluded

    async def test_removing_a_season_takes_every_copy_of_it(self):
        """Deliberately blunt: a season can hold a premiere record on one month
        and a row on the viewer's list at the same time, and anything narrower
        leaves the copy that puts the row straight back."""
        await self._premiere(self.user_id, "2026-06", 55, title="Oops")
        await self._listed(self.user_id, 55, title="Oops")
        await self._premiere(self.user_id, "2026-06", 66, season=2, title="Keep")

        self.assertEqual(
            await distrakt.remove_season_everywhere(
                self.user_id, ItemKey("show", "tmdb", "55"), 1),
            ["2026-06"])
        self.assertEqual(
            await distrakt.remove_season_everywhere(
                self.user_id, ItemKey("show", "tmdb", "55"), 1),
            [])  # already gone

        doc = await distrakt.load_month(self.user_id, "2026-06")
        self.assertEqual({(int(s["match_id"]), s["season"]) for s in doc["shows"]}, {(66, 2)})
        self.assertEqual(await self._held(self.user_id), set())


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
    app/calendar/cache.py) is what actually decides which entries survive for
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

    async def test_two_users_records_are_independent_in_both_directions(self):
        await self._premiere(self.user_id, self.UNDER_WAY, 111, title="Mine")
        await self._premiere(self.other_id, self.UNDER_WAY, 222, title="Theirs")
        await self._listed(self.user_id, 111, title="Mine")

        mine = await distrakt.load_month(self.user_id, self.UNDER_WAY)
        theirs = await distrakt.load_month(self.other_id, self.UNDER_WAY)
        self.assertEqual({s["ids"]["trakt"] for s in mine["shows"]}, {111})
        self.assertEqual({s["ids"]["trakt"] for s in theirs["shows"]}, {222})
        self.assertEqual(await self._held(self.other_id), set())

        # A verdict on one side is invisible on the other, both ways round.
        await self._settled(self.user_id, self.UNDER_WAY, 111,
                            distrakt.RecordKind.ABANDONED, title="Mine",
                            abandoned_form="`f`")
        self.assertFalse(
            (await distrakt.load_month(self.other_id, self.UNDER_WAY))["shows"][0]["abandoned"])

        # Deleting the other user's only season leaves this user's records intact.
        self.assertEqual(
            await distrakt.remove_season_everywhere(
                self.other_id, ItemKey("show", "tmdb", "222"), 1),
            [self.UNDER_WAY])
        self.assertEqual(
            await distrakt.remove_season_everywhere(
                self.other_id, ItemKey("show", "tmdb", "111"), 1),
            [])
        self.assertEqual(
            len((await distrakt.load_month(self.user_id, self.UNDER_WAY))["shows"]), 2)
        self.assertEqual(await self._held(self.user_id), {(111, 1)})

    async def test_month_lists_and_backfill_gates_are_per_user(self):
        await self._premiere(self.user_id, self.UNDER_WAY, 1)
        self.assertEqual(await distrakt.list_months(self.user_id), [self.UNDER_WAY])
        self.assertEqual(await distrakt.list_months(self.other_id), [])
        # A month that is backward/blocked for one user is a clean first seed for
        # the other, whose store is still empty.
        self.assertTrue(await distrakt.is_backfill_blocked(
            self.user_id, self.CLOSING, self.TODAY))
        self.assertFalse(await distrakt.is_backfill_blocked(
            self.other_id, self.CLOSING, self.TODAY))

    async def test_not_watching_overlay_is_per_user(self):
        """One user's calendar not-watching mark must not drop a premiere from
        anyone else's month."""
        async def fake_read_month(endpoint, settings, **kw):
            return [_cal_item(201, 1, "Shared Premiere")], None

        async def fake_progress(settings, since_days=60):
            return []

        await self._mark_not_watching(self.user_id, "slug-201")

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail):
            mine = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                               today=self.TODAY)
            theirs = await distrakt.ensure_month(self.other_id, 2026, 8, SETTINGS,
                                                 today=self.TODAY)

        self.assertEqual(await self._keys(mine), set())          # excluded for me
        self.assertEqual(await self._keys(theirs), {(201, 1)})   # still there for them

    async def test_freeze_prior_month_fires_per_user_independently(self):
        """Reaching the 1st freezes only the accessing user's prior month; another
        user's stays open until THEY first reach the new one."""
        for uid in (self.user_id, self.other_id):
            await self._premiere(uid, self.CLOSING, 101, title="Active")

        async def fake_read_month(endpoint, settings, **kw):
            return [], None

        async def fake_progress(settings, since_days=60):
            return []

        patches = (
            patch("app.calendar.cache.read_month", side_effect=fake_read_month),
            patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress),
            patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail),
            patch("app.distrakt.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS, today=date(2026, 8, 1))

            self.assertTrue((await distrakt.load_month(self.user_id, "2026-07"))["closed"])
            self.assertFalse((await distrakt.load_month(self.other_id, "2026-07"))["closed"])

            # The other user's own first access on/after the 1st freezes theirs.
            await distrakt.ensure_month(self.other_id, 2026, 8, SETTINGS, today=date(2026, 8, 1))
            self.assertTrue((await distrakt.load_month(self.other_id, "2026-07"))["closed"])


class CanInitializeTests(RolloverTestCase):
    """What may be built, with the date handed in so the real calendar cannot
    decide the answer. `TODAY` is the month the store is standing in."""

    TODAY = date(2026, 8, 15)

    async def test_empty_store_seeds_anything(self):
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2026-07", self.TODAY))
        self.assertFalse(await distrakt.is_backfill_blocked(self.user_id, "2026-07", self.TODAY))

    async def test_the_month_under_way_and_every_month_ahead_are_allowed(self):
        await distrakt.save_month(self.user_id, distrakt.new_month_doc("2026-08"))
        for key in ("2026-08", "2026-09", "2027-01", "2029-06"):
            with self.subTest(month=key):
                self.assertTrue(await distrakt.can_initialize(self.user_id, key, self.TODAY))

    async def test_a_gap_left_by_a_month_built_out_ahead_can_be_filled_in(self):
        """The rule this replaces was forward-only, so a December built during
        August put September through November permanently out of reach."""
        await distrakt.save_month(self.user_id, distrakt.new_month_doc("2026-12"))
        for key in ("2026-09", "2026-10", "2026-11"):
            with self.subTest(month=key):
                self.assertTrue(await distrakt.can_initialize(self.user_id, key, self.TODAY))
                self.assertFalse(await distrakt.is_backfill_blocked(self.user_id, key, self.TODAY))

    async def test_a_past_month_never_tracked_is_still_refused(self):
        await distrakt.save_month(self.user_id, distrakt.new_month_doc("2026-08"))
        self.assertFalse(await distrakt.can_initialize(self.user_id, "2026-07", self.TODAY))
        self.assertTrue(await distrakt.is_backfill_blocked(self.user_id, "2026-07", self.TODAY))

    async def test_a_past_month_later_than_everything_tracked_still_seeds(self):
        """Forward growth from wherever the store starts, which is older than the
        rule that was relaxed and is still what a store that began mid-year does."""
        await distrakt.save_month(self.user_id, distrakt.new_month_doc("2026-02"))
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2026-05", self.TODAY))
        self.assertFalse(await distrakt.can_initialize(self.user_id, "2026-01", self.TODAY))


class FreezeEligibilityTests(unittest.TestCase):
    """What settles a month is the calendar passing it, and nothing else — no
    later month has to exist, and none has to have been opened."""

    def test_a_month_settles_the_day_after_its_last(self):
        self.assertFalse(distrakt.freeze_eligible("2026-07", date(2026, 7, 31)))
        self.assertTrue(distrakt.freeze_eligible("2026-07", date(2026, 8, 1)))
        self.assertTrue(distrakt.freeze_eligible("2026-07", date(2026, 11, 4)))

    def test_a_month_still_ahead_never_is(self):
        self.assertFalse(distrakt.freeze_eligible("2026-09", date(2026, 8, 1)))


class MonthFreezesOnTheClockTests(RolloverTestCase):
    """The lazy half: eligibility is the clock's, the snapshot is taken on the
    next look."""

    async def _open_july(self) -> None:
        await self._premiere(self.user_id, self.CLOSING, 101, title="Active")

    async def _ensure(self, year: int, month: int, today: date) -> dict:
        async def fake_read_month(endpoint, settings, **kw):
            return [], None

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.distrakt.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            return await distrakt.ensure_month(self.user_id, year, month, SETTINGS, today=today)

    async def test_looking_at_a_month_the_calendar_has_passed_freezes_it(self):
        """Nothing later has to exist. The month itself is over, so reading it is
        what writes down what it settled."""
        await self._open_july()
        doc = await self._ensure(2026, 7, date(2026, 8, 20))
        self.assertTrue(doc["closed"])
        self.assertTrue((await distrakt.load_month(self.user_id, "2026-07"))["closed"])

    async def test_the_month_under_way_is_left_open(self):
        await self._open_july()
        doc = await self._ensure(2026, 7, date(2026, 7, 20))
        self.assertFalse(doc["closed"])

    async def test_a_gap_does_not_stop_it(self):
        """A month built far out ahead is not the month after July, so the
        prior-month freeze cannot be what closes July."""
        await self._open_july()
        await distrakt.save_month(self.user_id, distrakt.new_month_doc("2026-12"))
        await self._ensure(2026, 7, date(2026, 8, 20))
        self.assertTrue((await distrakt.load_month(self.user_id, "2026-07"))["closed"])


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
            doc = distrakt.new_month_doc(self.CLOSING)
            doc["totals_refreshed_at"] = db.now() - 30 * 3600
            await distrakt.save_month(uid, doc)

        await distrakt.stamp_refreshed(self.user_id, self.CLOSING)

        self.assertFalse(distrakt.is_stale(
            await distrakt.load_month(self.user_id, self.CLOSING)))
        self.assertTrue(distrakt.is_stale(
            await distrakt.load_month(other, self.CLOSING)))


class FinishingASeasonSettlesTheMonthItHappenedInTests(RolloverTestCase):
    """A season the viewer finishes leaves their list and becomes a COMPLETED
    record on the month the watch history dates it to — not on the month that
    happens to be on screen.

    THE MONTHS HERE COME FROM THE REAL CLOCK, because the month payload resolves
    its own `today` and nothing is injected into it. A written-out month would
    mean something different every time the calendar rolled, which is the rot this
    file's neighbours were bitten by twice.
    """

    def setUp(self):
        self.today = date.today()
        self.current = distrakt.month_key(self.today.year, self.today.month)
        self.prior = rollover.prev_month_key(self.current)

    def _settings(self):
        """SETTINGS plus the field the payload's share link reads."""
        return SimpleNamespace(**vars(SETTINGS), public_base_url="")

    async def _payload_for_the_month_under_way(self, progress: dict):
        """The month under way as the page asks for it, with Trakt itself the
        only thing mocked: the watch-history cache, the completion dates it
        derives and the transitions they drive are all real."""
        from app.distrakt import routes as distrakt_routes

        async def no_premieres(endpoint, settings, **kw):
            return [], None

        with patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value=progress), \
             patch("app.providers.trakt.sync.fetch_last_activities", return_value={}), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.calendar.cache.read_month", side_effect=no_premieres), \
             patch("app.media.logos.ensure_logos", new=AsyncMock(return_value=None)):
            payload, status = await distrakt_routes._distrakt_month_payload(
                self.user_id, self.today.year, self.today.month, self._settings())
        self.assertEqual(status, 200)
        return payload

    def _finished_last_month(self) -> dict:
        """102's whole season watched, the last episode of it during the month
        before this one."""
        day = date(*distrakt.parse_month_key(self.prior), 14)
        return {102: {1: {n: f"{day.isoformat()}T00:00:00Z" for n in range(1, 9)}}}

    async def test_the_completion_lands_on_the_month_the_history_names(self):
        await self._listed(self.user_id, 102, title="Finished Last Month")

        await self._payload_for_the_month_under_way(self._finished_last_month())

        settled = await distrakt.month_records(
            self.user_id, self.prior, [distrakt.RecordKind.COMPLETED])
        self.assertEqual([int(r["match_id"]) for r in settled], [102])
        self.assertEqual(await self._keys(
            await distrakt.load_month(self.user_id, self.current) or {"shows": []}), set())

    async def test_it_leaves_the_viewers_list_and_the_page_under_way(self):
        await self._listed(self.user_id, 102, title="Finished Last Month")

        payload = await self._payload_for_the_month_under_way(self._finished_last_month())

        self.assertEqual(payload["shows"], [])
        self.assertEqual(await self._held(self.user_id), set())

    async def test_finishing_it_this_month_is_this_month_own_verdict(self):
        await self._listed(self.user_id, 102, title="Finished This Month")
        day = date(*distrakt.parse_month_key(self.current), 1)
        progress = {102: {1: {n: f"{day.isoformat()}T00:00:00Z" for n in range(1, 9)}}}

        payload = await self._payload_for_the_month_under_way(progress)

        self.assertEqual([int((s.get("ids") or {}).get("tmdb")) for s in payload["shows"]],
                         [102])
        self.assertEqual([s["kind"] for s in payload["shows"]],
                         [distrakt.RecordKind.COMPLETED])
        self.assertEqual(await self._held(self.user_id), set())

    async def test_a_season_with_no_dated_last_episode_stays_on_the_list(self):
        """"Finished, month unknown" would have to guess a month, and a completed
        record on the wrong one is worse than a season that lingers."""
        await self._listed(self.user_id, 102, title="Finished, Date Unknown")
        progress = {102: {1: {n: "" for n in range(1, 9)}}}

        await self._payload_for_the_month_under_way(progress)

        self.assertEqual(await self._held(self.user_id), {(102, 1)})

    async def test_only_a_finished_season_carries_a_finish_date(self):
        """The date comes from the last episode watched, which on a partly
        watched season is just "last time I watched something" — compute_live_shows
        keeps it only for a season it has bucketed as completed, so a half-watched
        show can never be settled by it."""
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


def _months_ahead(today: date, n: int) -> tuple[int, int]:
    """The (year, month) `n` months after `today`'s own.

    Derived rather than written out as a literal: every rule below compares a
    month against the real clock, so a fixed "2026-10" would be a different
    distance from today every month and the wrong one most of them.
    """
    index = today.month - 1 + n
    return today.year + index // 12, index % 12 + 1


class MonthsAheadAreBuiltWhenAskedForTests(RolloverTestCase):
    """Building a month is asked for by name, so there is no distance at which the
    tracker stops obliging, and no order the months have to be built in.

    What stops a month being built by ACCIDENT is that opening one gathers nothing
    (see WhatAMonthThatHasNotBegunTakesTests) — the ask is the safeguard, so a
    bound on how far ahead it could reach only cost the months it stranded.
    """

    def setUp(self):
        self.today = date.today()

    async def _ensure(self, offset: int):
        """ensure_month for the month `offset` after this one, with every Trakt
        read recorded so what a build actually asked for can be read back."""
        calls: list[str] = []

        async def fake_read_month(endpoint, settings, **kw):
            calls.append(f"calendar:{kw.get('year')}-{kw.get('month'):02d}")
            return [_cal_item(201, 1, "Premiere")], None

        async def fake_progress(settings, since_days=60):
            calls.append("history")
            return []

        year, month = _months_ahead(self.today, offset)
        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.distrakt.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            doc = await distrakt.ensure_month(self.user_id, year, month, SETTINGS, today=self.today)
        return doc, calls, distrakt.month_key(year, month)

    async def test_a_month_well_out_ahead_is_built_and_takes_that_month(self):
        """The calendar read it makes names the month asked for, so a month five
        ahead gathers its own premieres rather than whatever is nearest."""
        doc, calls, key = await self._ensure(5)
        year, month = _months_ahead(self.today, 5)
        self.assertEqual(await self._keys(doc), {(201, 1)})
        self.assertIsNotNone(await distrakt.load_month(self.user_id, key))
        self.assertIn(f"calendar:{year}-{month:02d}", calls)

    async def test_the_preview_month_is_still_built(self):
        """The month immediately ahead is the ordinary case — asked for before the
        1st, it auto-populates so the roster tracks the calendar."""
        doc, _calls, key = await self._ensure(1)
        self.assertEqual(await self._keys(doc), {(201, 1)})
        self.assertIsNotNone(await distrakt.load_month(self.user_id, key))

    async def test_the_month_in_progress_is_still_built(self):
        doc, _calls, key = await self._ensure(0)
        self.assertEqual(await self._keys(doc), {(201, 1)})
        self.assertIsNotNone(await distrakt.load_month(self.user_id, key))

    async def test_building_out_ahead_does_not_consume_the_months_in_between(self):
        """The half that has to keep working. The store used to grow forward only,
        so a month built out ahead put every month before it permanently out of
        reach — including the one actually in progress."""
        await self._ensure(3)
        doc, _calls, key = await self._ensure(0)
        self.assertEqual(await self._keys(doc), {(201, 1)})
        self.assertIsNotNone(await distrakt.load_month(self.user_id, key))

    async def test_a_month_skipped_over_can_be_filled_in_later(self):
        """And the months in between are not merely still readable — they can be
        built afterwards, in any order."""
        await self._ensure(3)
        doc, _calls, key = await self._ensure(1)
        self.assertEqual(await self._keys(doc), {(201, 1)})
        self.assertIsNotNone(await distrakt.load_month(self.user_id, key))


class WhatAMonthThatHasNotBegunTakesTests(RolloverTestCase):
    """Until a month opens it holds only the titles that BEGIN in it — and it is
    not built by being looked at.

    A title carried out of the month before, and a season somebody is part-way
    through, are both statements about NOW. Filed into a month nobody has reached
    they were bucketed by whether they had aired YET, which they had not, so a
    season that began in August was announced as new in October.

    Every month here is derived from the clock. All of these rules compare a
    month key against today, so a written-out one would be a different distance
    from today every month and test the wrong rule most of them.
    """

    def setUp(self):
        self.today = date.today()

    def _settings(self):
        """SETTINGS plus the field the payload's share link reads."""
        return SimpleNamespace(**vars(SETTINGS), public_base_url="")

    async def _build(self, offset: int, premieres=(), progress=()) -> dict:
        """Build the month `offset` ahead the way ⤓ Import does — asking for it
        outright rather than by opening the page."""
        async def fake_read_month(endpoint, settings, **kw):
            return list(premieres), None

        async def fake_progress(settings, since_days=60):
            return list(progress)

        year, month = _months_ahead(self.today, offset)
        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.distrakt.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            return await distrakt.ensure_month(self.user_id, year, month, SETTINGS,
                                               today=self.today)

    async def _open(self, offset: int):
        """Open the month `offset` ahead as the page does, recording every read so
        a month that should build nothing can be shown to have asked for nothing."""
        from app.distrakt import routes as distrakt_routes

        calls: list[str] = []

        async def fake_read_month(endpoint, settings, **kw):
            calls.append("calendar")
            return [_cal_item(201, 1, "Premiere")], None

        async def fake_progress(settings, since_days=60):
            calls.append("history")
            return []

        year, month = _months_ahead(self.today, offset)
        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}), \
             patch("app.providers.trakt.sync.fetch_last_activities", return_value={}), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.media.logos.ensure_logos", new=AsyncMock(return_value=None)):
            payload, status = await distrakt_routes._distrakt_month_payload(
                self.user_id, year, month, self._settings())
        self.assertEqual(status, 200)
        return payload, calls, distrakt.month_key(year, month)

    async def _seed(self, offset: int, tid: int, title: str) -> None:
        """A title already announced on the month `offset` from this one."""
        year, month = _months_ahead(self.today, offset)
        await self._premiere(self.user_id, distrakt.month_key(year, month), tid,
                             title=title)

    async def test_the_month_ahead_takes_its_premieres_and_nothing_carried_over(self):
        """The reported case: a preview month listing seasons that began two
        months earlier, filed under New Shows because they had not aired yet."""
        await self._seed(0, 101, "Still Airing")

        doc = await self._build(1, premieres=[_cal_item(201, 1, "Starts Then")])

        self.assertEqual(await self._keys(doc), {(201, 1)})

    async def test_the_month_ahead_takes_nothing_from_recent_viewing_either(self):
        """The third source is the same kind of statement about now, and it was
        the one filing this month's material under a month that has not happened."""
        doc = await self._build(1, progress=[
            {"ids": _ids(401), "season": 1, "watched": 3, "title": "Part Way", "network": "Net"},
        ])

        self.assertEqual(await self._keys(doc), set())

    async def test_the_month_under_way_takes_its_premieres_and_seeds_the_list(self):
        """The half that must not change: once a month has begun, the recent-
        viewing sweep runs — but what it finds is a fact about the VIEWER, so it
        lands on their own list and the month keeps only what premiered in it.

        The month before contributes nothing either way. Copying what was still
        going forward is what let a month claim a season that premiered somewhere
        else."""
        await self._seed(-1, 101, "Still Airing")

        doc = await self._build(0, premieres=[_cal_item(201, 1, "Starts Now")], progress=[
            {"ids": _ids(401), "season": 1, "watched": 3, "title": "Part Way", "network": "Net"},
        ])

        self.assertEqual(await self._keys(doc), {(201, 1)})
        self.assertEqual(await self._held(self.user_id), {(401, 1)})

    async def test_opening_the_month_ahead_builds_nothing_and_says_what_it_waits_for(self):
        """Building a month is the expensive act, and out ahead it is also a
        guess. Opening one shows an empty month that says so; ⤓ Import builds it."""
        from app.distrakt import routes as distrakt_routes

        payload, calls, key = await self._open(1)

        self.assertEqual(payload["shows"], [])
        self.assertEqual(payload["empty_note"], distrakt_routes.MONTH_AWAITS_IMPORT)
        self.assertFalse(payload["readonly"])                            # Import stays offered
        self.assertIsNone(await distrakt.load_month(self.user_id, key))  # nothing written
        self.assertEqual(calls, [])                                      # nothing fetched

    async def test_a_month_well_out_ahead_says_the_same_thing(self):
        """One sentence for every month ahead, at any distance: it is waiting to
        be asked. There is no longer a second state to confuse it with."""
        from app.distrakt import routes as distrakt_routes

        payload, calls, key = await self._open(5)

        self.assertEqual(payload["empty_note"], distrakt_routes.MONTH_AWAITS_IMPORT)
        self.assertFalse(payload["readonly"])                             # Import stays offered
        self.assertIsNone(await distrakt.load_month(self.user_id, key))
        self.assertEqual(calls, [])

    async def test_the_month_under_way_still_fills_itself_in_on_sight(self):
        """The month being asked about is the one case where an empty page would
        be wrong rather than merely early, so opening it still builds it."""
        payload, _calls, key = await self._open(0)

        self.assertEqual([s["ids"]["trakt"] for s in payload["shows"]], [201])
        self.assertIsNotNone(await distrakt.load_month(self.user_id, key))

    async def test_a_month_ahead_that_was_built_is_read_as_it_stands(self):
        """Once asked for, it is an ordinary month: opening it shows what it
        holds rather than the waiting line."""
        await self._seed(1, 101, "Already Asked For")

        payload, _calls, key = await self._open(1)

        self.assertFalse(payload.get("empty_note"))  # not the waiting line any more
        self.assertIsNotNone(await distrakt.load_month(self.user_id, key))


class AMonthOnlyShowsWhatItCanShowTests(RolloverTestCase):
    """The whole way through, on the month the tracker can actually be pointed at
    ahead of today: what the viewer has in hand does not appear in a month that
    has not started. It stays on their list — it just is not that month's to talk
    about until the calendar gets there.
    """

    async def test_a_month_that_has_not_begun_shows_no_work_in_hand(self):
        from app.distrakt import routes as distrakt_routes

        today = date.today()
        year, month = _months_ahead(today, 1)
        preview = distrakt.month_key(year, month)
        # 101 is a weekly season part-way through: the viewer's own work in hand,
        # and Keepup in any month that is entitled to say so.
        await self._listed(self.user_id, 101, title="Still Airing")
        # And one title that genuinely premieres in the month ahead, so the
        # assertion below is about the filter rather than about an empty store.
        await self._premiere(self.user_id, preview, 201, title="Starts Then",
                             premiere="8/15")

        async def no_premieres(endpoint, settings, **kw):
            return [], None

        settings = SimpleNamespace(**vars(SETTINGS), public_base_url="")
        with patch("app.providers.trakt.sync.fetch_progress_details", return_value={}), \
             patch("app.providers.trakt.sync.fetch_last_activities", return_value={}), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.calendar.cache.read_month", side_effect=no_premieres), \
             patch("app.media.logos.ensure_logos", new=AsyncMock(return_value=None)):
            payload, status = await distrakt_routes._distrakt_month_payload(
                self.user_id, year, month, settings)

        self.assertEqual(status, 200)
        shown = {int((s.get("ids") or {}).get("tmdb")) for s in payload["shows"]}
        self.assertNotIn(101, shown, "a month that has not begun claimed work in hand")
        self.assertNotIn("Keepup", payload["post2"])
        self.assertNotIn("Cleanup", payload["post2"])
        # Kept off that month's view, not taken away: the season is still the
        # viewer's and is still on their own list.
        self.assertEqual(await self._held(self.user_id), {(101, 1)})


class ATurnAwayKeepsATitleOutOfAMonthTests(RolloverTestCase):
    """A title the viewer has turned away on their calendar is never BUILT into a
    month, from either source that builds one.

    The mark is a statement about the show, so there is nothing here about which
    view it was made in or when. What a turn-away means for a record that already
    exists is a different question — that is a verdict on the season rather than a
    reason never to write one — and it is answered where such a record is acted
    on, not where a month is being assembled.

    Every month here is derived from the real clock rather than written as a
    literal. `ensure_month` compares the month against today, so a hardcoded
    'YYYY-MM' would be testing a different rule every month and the wrong one most
    of them.
    """

    def setUp(self):
        # date.today(), not a frozen date: these tests assert what the tracker does
        # on the day it runs, which is the only day the rule is ever applied on.
        self.today = date.today()
        self.current = distrakt.month_key(self.today.year, self.today.month)
        self.prior = rollover.prev_month_key(self.current)

    async def _ensure_current(self, premieres=(), progress=()):
        async def fake_read_month(endpoint, settings, **kw):
            return list(premieres), None

        async def fake_progress(settings, since_days=60):
            return list(progress)

        with patch("app.calendar.cache.read_month", side_effect=fake_read_month), \
             patch("app.providers.trakt.sync.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.distrakt.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            return await distrakt.ensure_month(self.user_id, self.today.year, self.today.month,
                                               SETTINGS, today=self.today)

    async def test_a_premiere_that_was_turned_away_never_enters_the_month(self):
        """The reported case: the marks were all made on the calendar first, and
        the month's first build listed every one of them as given up on."""
        await self._mark_not_watching(self.user_id, "slug-101")

        doc = await self._ensure_current(premieres=[_cal_item(101, 1, "Not For Me")])

        self.assertEqual(await self._keys(doc), set())
        stored = await distrakt.load_month(self.user_id, self.current)
        self.assertEqual(stored["shows"], [])  # kept out, not built in as a verdict

    async def test_a_title_from_recent_viewing_that_was_turned_away_is_kept_out_too(self):
        """The same rule for the other source. Recent watch history is a fact
        about the past; a title the viewer has since said they are not watching is
        not one to seed their list with either."""
        await self._mark_not_watching(self.user_id, "slug-401")

        doc = await self._ensure_current(progress=[
            {"ids": _ids(401), "season": 1, "watched": 3, "title": "Backlog", "network": "Net"},
        ])

        self.assertEqual(await self._keys(doc), set())
        self.assertEqual(await self._held(self.user_id), set())

    async def test_an_unmarked_title_from_recent_viewing_joins_the_viewers_list(self):
        """The filter has to be about the mark and not about the source. A title
        nobody has said anything about is still the viewer's to get through — and
        being part-way through something is a fact about them, so it lands on
        their list rather than on the month."""
        doc = await self._ensure_current(progress=[
            {"ids": _ids(401), "season": 1, "watched": 3, "title": "Part Way", "network": "Net"},
        ])

        self.assertEqual(await self._keys(doc), set())  # not one of the month's own
        self.assertEqual(await self._held(self.user_id), {(401, 1)})

    async def test_a_season_already_on_the_list_is_not_seeded_twice(self):
        """The sweep only ever adds what is not there, so a season the viewer is
        already keeping up with keeps the record it has."""
        await self._listed(self.user_id, 401, title="Already Listed",
                           kind=distrakt.RecordKind.CATCHUP)

        await self._ensure_current(progress=[
            {"ids": _ids(401), "season": 1, "watched": 3, "title": "Part Way", "network": "Net"},
        ])

        listed = await distrakt.user_records(self.user_id)
        self.assertEqual([(int(r["match_id"]), r["kind"]) for r in listed],
                         [(401, distrakt.RecordKind.CATCHUP)])


if __name__ == "__main__":
    unittest.main()
