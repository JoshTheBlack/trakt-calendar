"""Settling the rows a month that never froze left behind.

The roster split (migration 19) holds every row it cannot classify instead of
guessing, because what such a row WAS is decided by the season's premiere date and
that date lives at the provider. This is the pass that asks — so the properties
worth proving here are about the asking and about what is done with the answer:

  - a season that premiered in the month it was held on becomes that month's
    announcement, and one that premiered elsewhere becomes the viewer's own,
  - the year counts, so a season that premiered in the same month LAST year is not
    announced a year late,
  - a provider that cannot answer leaves everything held rather than settling half
    a month from a half-answer,
  - the rows are gone once settled, and the pass costs nothing thereafter,
  - a season the viewer had already finished is recorded on the month its watch
    history dates it to, in the same pass — before that month freezes on the very
    same load, and without waiting for somebody to open the month under way.

No network: the season lookup and the watch-history sync are both replaced on the
modules that own them.
"""
from __future__ import annotations

import unittest
from unittest import mock

from app import db, distrakt
from app.config import Settings
from app.distrakt import live, unsettled, watch_history
from app.providers.trakt import TraktError
from tests.support import new_db_path


def settings() -> Settings:
    """A real Settings with Trakt configured — the drain hands it to the watch
    history sync, which reads more of it than a bare stub can carry."""
    return Settings(trakt_client_id="id", trakt_client_secret="s",
                    trakt_access_token="tok")


def lookup(dates: dict[tuple[str, int], str | None], *, fail: bool = False,
           started: bool = True, watched: dict[tuple[str, int], int] | None = None,
           finished_on: dict[tuple[str, int], str] | None = None):
    """Stand-ins for the two things the drain reaches out for.

    `dates` is the season lookup: (match_id, season) -> the season's first air
    date, or None for a season the provider can say nothing about. `watched` and
    `finished_on` are the watch history: how many episodes of a season have been
    seen, and the day its last one was.

    Patched onto the modules unsettled.py calls them THROUGH — a name bound at
    import time would leave the real ones in place and the network guard would be
    what noticed.
    """
    async def fetch_season_details(settings_, records, *, fresh, allow_degrade):
        if fail:
            raise TraktError("Trakt is unreachable")
        out = []
        for rec in records:
            iso = dates.get((rec["match_id"], int(rec["season"])))
            out.append({
                "total": 8, "cadence": "Mon", "premiere": None, "finale": None,
                "started_airing": started, "finished_airing": False,
                "air_dates": [iso] if iso else [],
            })
        return out

    async def sync_and_baseline(settings_, user_id, roster, **kwargs):
        # Keyed the way the real state is: {item key: {seasons: {n: [episodes]}}}.
        shows: dict[str, dict] = {}
        for (match_id, season), count in (watched or {}).items():
            shows.setdefault(f"show:tmdb:{match_id}", {"seasons": {}})
            shows[f"show:tmdb:{match_id}"]["seasons"][str(season)] = list(range(count))
        return {"shows": shows, "movies": {}, "last_synced": None,
                "completed": {f"show:tmdb:{m}|{s}": day
                              for (m, s), day in (finished_on or {}).items()}}

    def season_completed_map(state):
        return {(key.split("|")[0], int(key.split("|")[1])): day
                for key, day in (state.get("completed") or {}).items()}

    return _all_of(
        mock.patch.object(live, "fetch_season_details", fetch_season_details),
        mock.patch.object(watch_history, "sync_and_baseline", sync_and_baseline),
        mock.patch.object(watch_history, "season_completed_map", season_completed_map),
    )


class _all_of:
    """Enter several patches as one `with`."""
    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for patch in self.patches:
            patch.start()

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        return False


class UnsettledTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("unsettled")
        await db.migrate()
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) "
            "VALUES ('tracker', 1, 1, 1, ?, ?)", (now, now))
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _held(self, month: str, *, match_id: str = "4242", season: int = 2,
                    title: str = "Old") -> None:
        """One row as migration 19 leaves it: an identity, a month, and none of the
        fields a freeze would have written."""
        await db.execute(
            "INSERT INTO distrakt_unsettled_rows (user_id, month, media, "
            "match_source, match_id, season, trakt_id, tmdb, title, network, "
            "added_by, created_at) VALUES (?, ?, 'show', 'tmdb', ?, ?, 601, ?, ?, "
            "'HBO', 'calendar', 0)",
            (self.user_id, month, match_id, season, int(match_id), title))

    async def _remaining(self) -> int:
        return await db.fetch_value(
            "SELECT COUNT(*) FROM distrakt_unsettled_rows WHERE user_id = ?",
            (self.user_id,))

    async def test_a_season_that_premiered_in_its_month_becomes_its_announcement(self):
        await self._held("2026-07", season=1)
        with lookup({("4242", 1): "2026-07-09"}):
            await unsettled.settle(self.user_id, settings())
        record, = await distrakt.month_records(self.user_id, "2026-07")
        self.assertEqual(record["kind"], "series_premiere")
        # The catalogue half comes from the lookup, which is the whole reason the
        # decision waited for one.
        self.assertEqual(record["total"], 8)
        self.assertEqual(await self._remaining(), 0)

    async def test_an_announcement_that_has_begun_airing_is_also_the_viewers(self):
        """A binge that dropped and was finished inside the same held month is the
        case this exists for. It was never carried onto a later month, so this is
        its only row — and `finish()` moves a season off the viewer's LIST onto the
        month its history names, so an announcement that never reaches the list can
        never be recorded as completed at all. The month would announce it, the
        viewer would have watched all of it, and its Completed section would not
        mention it.

        lifecycle.advance normally makes this copy, but only for the month under
        way; a held month has usually ended and is about to freeze."""
        await self._held("2026-07", season=1, title="Ride or Die")
        with lookup({("4242", 1): "2026-07-15"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual([r["kind"] for r in await distrakt.month_records(
            self.user_id, "2026-07")], ["series_premiere"])
        listed, = await distrakt.user_records(self.user_id)
        self.assertEqual(listed["title"], "Ride or Die")

    async def test_a_binge_announced_and_finished_in_one_held_month_gets_both(self):
        """The whole reported case, end to end: a season that dropped all at once
        in July, was watched through in July, and was therefore never carried onto
        August — so the held July row is the only row it has anywhere.

        Both records are written in this one pass, and the completed one matters
        most: the month is about to freeze on this very load, and the pass that
        normally settles a finished season (lifecycle.advance) runs only for the
        month UNDER WAY. Left to that, July's Completed section stays empty until
        somebody happens to open August."""
        await self._held("2026-07", season=1, title="Ride or Die")
        with lookup({("4242", 1): "2026-07-15"},
                    watched={("4242", 1): 8},
                    finished_on={("4242", 1): "2026-07-22"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual({r["kind"] for r in await distrakt.month_records(
            self.user_id, "2026-07")}, {"series_premiere", "completed"})
        # ...and settling took it off the viewer's list, because it is done with.
        self.assertEqual(await distrakt.user_records(self.user_id), [])

    async def test_a_finish_is_dated_by_the_history_and_not_by_the_month_held(self):
        """A season held on August that the history says was finished in July is
        July's record. The month a row sat on says where it was TRACKED; only the
        watch history says when it was finished."""
        await self._held("2026-08", season=1)
        with lookup({("4242", 1): "2026-02-01"},
                    watched={("4242", 1): 8},
                    finished_on={("4242", 1): "2026-07-22"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual([r["kind"] for r in await distrakt.month_records(
            self.user_id, "2026-07")], ["completed"])
        self.assertEqual(await distrakt.month_records(self.user_id, "2026-08"), [])

    async def test_a_finish_with_no_date_leaves_the_season_on_the_list(self):
        """"Finished, month unknown" would have to guess a month, and a wrong
        completed record is worse than a season that lingers."""
        await self._held("2026-08", season=1)
        with lookup({("4242", 1): "2026-02-01"}, watched={("4242", 1): 8}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual(len(await distrakt.user_records(self.user_id)), 1)

    async def test_an_announcement_that_has_not_aired_stands_alone(self):
        """A month still ahead: nothing on it has aired, so there is nothing to get
        through yet."""
        await self._held("2026-07", season=1)
        with lookup({("4242", 1): "2026-07-15"}, started=False):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual(len(await distrakt.month_records(self.user_id, "2026-07")), 1)
        self.assertEqual(await distrakt.user_records(self.user_id), [])

    async def test_a_later_season_is_a_returning_one(self):
        await self._held("2026-07", season=4)
        with lookup({("4242", 4): "2026-07-09"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual((await distrakt.month_records(
            self.user_id, "2026-07"))[0]["kind"], "season_premiere")

    async def test_a_season_that_premiered_elsewhere_is_the_viewers_own(self):
        """It announced nothing that month, so it is something they had in hand —
        and a viewer record belongs to no month at all."""
        await self._held("2026-07")
        with lookup({("4242", 2): "2026-04-02"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual(await distrakt.month_records(self.user_id, "2026-07"), [])
        listed, = await distrakt.user_records(self.user_id)
        self.assertEqual((listed["title"], listed["season"]), ("Old", 2))
        self.assertEqual(await self._remaining(), 0)

    async def test_the_year_counts(self):
        """A season that premiered in July LAST year, sitting on this July's list as
        something to catch up on. Compared on the month number alone it is
        announced as new a year late — which is what the stored 'M/D' the record
        renders would have done, and why the full air date is what is read."""
        await self._held("2026-07", season=1)
        with lookup({("4242", 1): "2025-07-09"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual(await distrakt.month_records(self.user_id, "2026-07"), [],
                         "last July's premiere was announced as this July's")
        self.assertEqual(len(await distrakt.user_records(self.user_id)), 1)

    async def test_a_season_the_provider_cannot_place_stays_the_viewers_own(self):
        """No air dates is no evidence, and a month's announcement is the wrong
        place to put the title the least is known about."""
        await self._held("2026-07", season=1)
        with lookup({("4242", 1): None}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual(await distrakt.month_records(self.user_id, "2026-07"), [])
        self.assertEqual(len(await distrakt.user_records(self.user_id)), 1)

    async def test_a_season_held_on_two_months_settles_once_on_each_question(self):
        """Carried forward: it announced July and was still in hand in August. The
        premiere record stays on the month that announced it and the viewer gets
        ONE record, not one per month it was carried onto."""
        await self._held("2026-07", season=1)
        await self._held("2026-08", season=1)
        with lookup({("4242", 1): "2026-07-09"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual([r["kind"] for r in await distrakt.month_records(
            self.user_id, "2026-07")], ["series_premiere"])
        self.assertEqual(await distrakt.month_records(self.user_id, "2026-08"), [])
        self.assertEqual(len(await distrakt.user_records(self.user_id)), 1)

    async def test_a_provider_failure_leaves_everything_held(self):
        """Settling half a month from a lookup that half worked would write
        premiere records off missing dates — the guess this whole path exists to
        avoid. The rows are not lost by waiting."""
        await self._held("2026-07", season=1)
        with lookup({}, fail=True):
            with self.assertLogs("app.distrakt.unsettled", level="WARNING") as caught:
                await unsettled.settle(self.user_id, settings())
        self.assertEqual(await self._remaining(), 1)
        self.assertEqual(await distrakt.month_records(self.user_id, "2026-07"), [])
        self.assertEqual(await distrakt.user_records(self.user_id), [])
        self.assertIn("retries", "\n".join(caught.output))

    async def test_a_second_run_settles_what_the_first_could_not(self):
        await self._held("2026-07", season=1)
        with lookup({}, fail=True):
            await unsettled.settle(self.user_id, settings())
        with lookup({("4242", 1): "2026-07-09"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual(len(await distrakt.month_records(self.user_id, "2026-07")), 1)
        self.assertEqual(await self._remaining(), 0)

    async def test_a_season_already_settled_does_not_come_back_onto_the_list(self):
        """A load between two drain attempts can complete a season still held on a
        later month, and a season settled anywhere is off the list."""
        await self._held("2026-08")
        await distrakt.add_month_record(self.user_id, "2026-07", {
            "media": "show", "match_source": "tmdb", "match_id": "4242", "season": 2,
            "title": "Old", "kind": distrakt.RecordKind.COMPLETED,
        })
        with lookup({("4242", 2): "2026-04-02"}):
            await unsettled.settle(self.user_id, settings())
        self.assertEqual(await distrakt.user_records(self.user_id), [])
        self.assertEqual(await self._remaining(), 0)

    async def test_nothing_held_asks_the_provider_nothing(self):
        """This sits in front of every tracker payload for ever, to pay for a
        repair that happens once."""
        with lookup({}, fail=True):
            await unsettled.settle(self.user_id, settings())  # would raise if asked

    async def test_without_trakt_the_rows_wait(self):
        """Nothing can be worked out without a season lookup, and an instance with
        no credentials must not drop what it cannot yet classify."""
        await self._held("2026-07", season=1)

        with lookup({}, fail=True):
            await unsettled.settle(self.user_id, Settings())
        self.assertEqual(await self._remaining(), 1)

    async def test_one_users_held_rows_are_not_another_s(self):
        now = db.now()
        other = (await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) "
            "VALUES ('other', 0, 1, 1, ?, ?)", (now, now))).lastrowid
        await self._held("2026-07", season=1)
        with lookup({("4242", 1): "2026-07-09"}):
            await unsettled.settle(other, settings())
        self.assertEqual(await self._remaining(), 1,
                         "another account's settle drained these rows")
        self.assertEqual(await distrakt.month_records(other, "2026-07"), [])


if __name__ == "__main__":
    unittest.main()
