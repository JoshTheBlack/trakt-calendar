"""A month rollover driven through the HTTP API, with the clock moved under it.

WHAT MAKES THESE DIFFERENT FROM tests/distrakt/test_rollover.py. Those call
`ensure_month(..., today=...)` and hand the date in as an argument, which proves
the rollover logic and nothing about whether a REQUEST ever arrives at it. Every
route resolves its own `today`, and until app/clock.py existed there was no way
to make one of them believe the date had changed — so the transition itself, a
real client asking for a month before and after a boundary, had never been
exercised at any level. That is what this file does: the only thing injected is
the environment variable, and everything after it is the app's ordinary path.

DATES ARE ABSOLUTE HERE AND THAT IS DELIBERATE. Elsewhere in this suite a
hardcoded year-month rots — one did, passing all July and failing on 1 August.
Under a clock override it cannot: the app is not reading the real date at all, so
these months mean the same thing whenever they are run. The rule is "never let
the real calendar decide the outcome", and pinning the fake one is one of the two
ways to obey it.

No network. The calendar read, the watch-history sync and the per-season detail
call are all patched, exactly as the unit-level rollover tests patch them.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import date
from unittest import mock

from app import clock, distrakt as distrakt_store
from app.calendar import state as calendar_state
from app.config import Settings
from app.distrakt import watch_history
from app.providers.base import Item, ItemKey, Media, Source

from ..support import ORIGIN, AppTestCase

# The two months the rollover happens across, and the day either side of the
# boundary between them. Named once so a test reads as a story about a boundary
# rather than a set of string literals.
CLOSING = "2026-07"
OPENING = "2026-08"
BEFORE_THE_FIRST = date(2026, 7, 20)
ON_THE_FIRST = date(2026, 8, 1)

# The month numbers those keys stand for, so a fixture's "M/D" premiere date is
# DERIVED from the month it is supposed to premiere in rather than typed out
# beside it and left to drift. A fixture below was named for the month it
# announced while carrying the previous month's date, and only rendered at all
# because of how it happened to be sorted.
CLOSING_MONTH = distrakt_store.parse_month_key(CLOSING)[1]
OPENING_MONTH = distrakt_store.parse_month_key(OPENING)[1]


def fake_today(value: date):
    """Run the block with the app believing `value` is today."""
    return mock.patch.dict(os.environ, {clock.FAKE_TODAY_ENV: value.isoformat()})


def _item(trakt_id: int, season: int, title: str, air_date: str) -> Item:
    """One calendar premiere as the shared cache hands it over.

    A real Item rather than a lookalike dict, so a field renamed out from under
    this double fails here instead of passing and then failing in production.
    """
    return Item(
        source=Source.TRAKT, media=Media.SHOW, id=f"slug-{trakt_id}",
        ids={"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
        detail_url=f"https://trakt.tv/shows/slug-{trakt_id}",
        air_date=air_date, air_ts=0.0, air_display=air_date,
        air_time="20:00", day_of_week="Saturday",
        title=title, season=season, network="Net",
    )


# Season shapes for every id these tests use. `started`/`finished` are what the
# transitions turn on, so each id is chosen to reach one state and stay there.
_SEASONS = {
    # Announced by the closing month and still going: the viewer's own work in
    # hand, which is what carries into the month after it.
    701: {"total": 8, "started": True, "finished": False, "watched": 3},
    # Finished in the closing month and fully watched — that month's verdict.
    702: {"total": 6, "started": True, "finished": True, "watched": 6},
    # Given up on by hand in the closing month — that month's verdict too.
    703: {"total": 6, "started": True, "finished": False, "watched": 1},
    # The opening month's own premiere, and it premieres IN the opening month.
    801: {"total": 10, "started": True, "finished": False, "watched": 0,
          "premiere": f"{OPENING_MONTH}/5"},
    # Finished in the closing month on six episodes, and the provider has since
    # learned of two more — the season the viewer is no longer finished with.
    704: {"total": 8, "started": True, "finished": False, "watched": 7},
    # Imported into a month that has NOT BEGUN: its premiere is still weeks off,
    # so nothing has aired and nobody has watched anything.
    911: {"total": 10, "started": False, "finished": False, "watched": 0,
          "premiere": f"{OPENING_MONTH}/8"},
    # Watched, but no record anywhere knows about it. Nothing looks this up while
    # it is merely being ASKED about — that is the whole point of the question —
    # so it is here only for the answer that says yes.
    999: {"total": 12, "started": True, "finished": False, "watched": 1},
}


async def _fake_season_detail(settings, trakt_id, season, fresh=False, client=None):
    spec = _SEASONS[int(trakt_id)]
    return {
        "season": int(season), "total": spec["total"], "cadence": "Mon",
        # "M/D" with no year, as the real season lookup gives it. Most ids here
        # premiered in the closing month and take that as the default; an id whose
        # premiere MONTH is what a test is about names its own.
        "premiere": spec.get("premiere", f"{CLOSING_MONTH}/6"),
        "finale": f"{CLOSING_MONTH}/28" if spec["finished"] else None,
        "started_airing": spec["started"], "finished_airing": spec["finished"],
        "air_dates": [],
    }


def _watch_state(roster, plays=()) -> dict:
    """Watch-history cache stand-in: report `watched` episodes for each id it has
    cause to hold counts for, keyed the way the real one keys them, and hand back
    whatever plays the history pull reported.

    THE PLAYS' IDS ARE IN IT AS WELL AS THE ROSTER'S. The real cache holds counts
    for everything it has ever baselined, and a season that has just been watched
    more of was baselined back when it was on the list — the roster only decides
    which titles a sync goes and baselines for the FIRST time. Keying this off the
    roster alone would have a season that reopens mid-load render as 0 watched.

    NO PLAYS IS THE ORDINARY CASE and the default: the activity beacon had not
    moved, no history was fetched, and there is nothing for a load to reconcile.
    """
    wanted = {int((rec.get("ids") or {}).get("tmdb") or 0) for rec in roster}
    wanted |= {int(play.key.match_id) for play in plays}
    shows: dict = {}
    for trakt_id, spec in _SEASONS.items():
        if trakt_id in wanted:
            shows[f"show:tmdb:{trakt_id}"] = {
                "ids": {"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
                "seasons": {"1": list(range(spec["watched"]))},
            }
    return {"shows": shows, "movies": {}, "last_synced": "2026-01-01", "beacons": None,
            watch_history._PLAYS: list(plays)}


async def _fake_sync_and_baseline(settings, user_id, roster, force=False, today=None):
    return _watch_state(roster)


async def _no_progress(settings, since_days=60):
    return []


class RolloverOverHttpTestCase(AppTestCase):
    """A signed-in tracker user with Trakt configured, and no live calls."""

    def make_settings(self):
        # trakt_configured is a property over these two fields, and a month is not
        # built at all without it (rollover.ensure_month). public_base_url has to
        # match the client's origin or the cross-site rules refuse the writes.
        return Settings(public_base_url=ORIGIN,
                        trakt_client_id="cid", trakt_access_token="tok")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user(
            "tracker", calendar_approved=True, distrakt_approved=True)
        self.link_identity(self.user_id, "trakt", provider_user_id=901, access_token="tok")
        self.sign_in_as(self.user_id)

    @contextlib.contextmanager
    def offline(self, premieres: dict[str, list[Item]] | None = None, plays=()):
        """Every outbound read the tracker makes, replaced. `premieres` maps
        "YYYY-MM" to what the calendar holds for that month, so a test says which
        month has what rather than writing a dispatcher each time, and `plays` is
        what that load's watch-history pull reported.

        Any request that builds a month has to run inside this — conftest's guard
        turns an unpatched one into a connection refusal rather than a silent
        network call, which is how the two omissions in this file were caught.

        The season lookup is left on `self.season_calls` so a test can say how
        many were made: what a reopening costs is part of the rule, not an
        implementation detail.
        """
        held = premieres or {}
        self.season_calls = []

        async def read_month(endpoint, settings, year=None, month=None, **kw):
            key = distrakt_store.month_key(int(year), int(month))
            return list(held.get(key, [])), None

        async def season_detail(settings, trakt_id, season, fresh=False, client=None):
            self.season_calls.append((int(trakt_id), int(season)))
            return await _fake_season_detail(settings, trakt_id, season, fresh, client)

        async def sync_and_baseline(settings, user_id, roster, force=False, today=None,
                                    since_month=None):
            return _watch_state(roster, plays)

        with contextlib.ExitStack() as stack:
            for target, fake in (
                ("app.calendar.cache.read_month", read_month),
                ("app.providers.trakt.sync.fetch_watched_progress", _no_progress),
                ("app.providers.trakt.detail.fetch_season_detail", season_detail),
                ("app.distrakt.watch_history.sync_and_baseline", sync_and_baseline),
            ):
                stack.enter_context(mock.patch(target, side_effect=fake))
            yield

    def get_month(self, key: str, premieres: dict[str, list[Item]] | None = None,
                  plays=()) -> dict:
        """GET /api/distrakt/month for a "YYYY-MM", with the reads patched."""
        year, month = distrakt_store.parse_month_key(key)
        with self.offline(premieres, plays):
            resp = self.client.get(f"/api/distrakt/month?year={year}&month={month}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def import_month(self, key: str, premieres: dict[str, list[Item]] | None = None) -> dict:
        """POST /api/distrakt/import — the control that BUILDS a month that has
        not begun. Opening one no longer builds it, so this is the only way in."""
        year, month = distrakt_store.parse_month_key(key)
        with self.offline(premieres):
            resp = self.client.post("/api/distrakt/import",
                                    json={"year": year, "month": month})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def post(self, path: str, body: dict) -> dict:
        """POST one of the row controls, with every read it makes patched.

        Patched on the modules that DEFINE these functions, which is also where
        the route reaches them: it calls across the package through the module
        object rather than through names bound at import, so patching the owner is
        enough — and a double installed there cannot be bypassed by a second
        reference nobody can see.
        """
        with self.offline(), \
                mock.patch("app.providers.trakt.sync.fetch_watched_map", return_value={}):
            resp = self.client.post(path, json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def kinds_on(self, month_key: str) -> dict[int, set[str]]:
        """{numeric id: the kinds of record the month holds for it}, read from the
        store rather than from a rendered payload — what got WRITTEN is the thing
        a rollover is about.

        A SET per id, because a month can legitimately hold two records for the
        same season: the premiere it announced, and the verdict it reached. That is
        two statements about the month rather than a duplicate.
        """
        doc = asyncio.run(distrakt_store.load_month(self.user_id, month_key))
        out: dict[int, set[str]] = {}
        for record in (doc or {}).get("shows", []):
            out.setdefault(int(record["match_id"]), set()).add(record["kind"])
        return out

    def stored_ids(self, key: str) -> set[int]:
        """The numeric ids a month actually holds a record of."""
        return set(self.kinds_on(key))

    def listed_ids(self) -> set[int]:
        """The numeric ids on the viewer's own list, which belongs to no month."""
        records = asyncio.run(distrakt_store.user_records(self.user_id))
        return {int(r["match_id"]) for r in records}

    def shown_ids(self, payload: dict) -> set[int]:
        return {int((s.get("ids") or {}).get("tmdb")) for s in payload["shows"]}

    def seed_the_closing_month(self) -> None:
        """The closing month as a lived-in open month: one title it announced and
        that is still going, one season finished in it, one given up on in it.

        The still-going season is ALSO on the viewer's list, which is where being
        part-way through something lives — the month keeps only the announcement.
        """
        asyncio.run(self._seed())

    async def _seed(self) -> None:
        for trakt_id, title, kind, watched, total in (
            (701, "Still Going", distrakt_store.RecordKind.SERIES_PREMIERE, 0, 8),
            (702, "All Done", distrakt_store.RecordKind.COMPLETED, 6, 6),
            (703, "Gave Up", distrakt_store.RecordKind.ABANDONED, 1, 6),
        ):
            await distrakt_store.add_month_record(self.user_id, CLOSING, {
                "ids": {"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
                "season": 1, "title": title, "network": "Net", "kind": kind,
                "watched": watched, "total": total,
                "abandoned_form": ("`Gave Up S01 (1/6)`"
                                   if kind is distrakt_store.RecordKind.ABANDONED else None),
            })
        await distrakt_store.add_user_record(self.user_id, {
            "ids": {"trakt": 701, "tmdb": 701, "slug": "slug-701"},
            "season": 1, "title": "Still Going", "network": "Net",
            "kind": distrakt_store.RecordKind.KEEPUP, "watched": 3, "total": 8,
        })
        # Giving up and turning the show away on the main calendar are one act —
        # whichever of the two views it is done in, both are written. A fixture
        # that recorded the verdict without the mark would be describing a state
        # the app cannot reach, and the month's next load would read the missing
        # mark as the viewer having taken the verdict back.
        await calendar_state.set_not_watching(self.user_id, "slug-703", True)

    def open_the_new_month(self) -> dict:
        """Cross the boundary the way a person does: look at the closing month
        while it is still under way, then look at the new one on its 1st."""
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        with fake_today(ON_THE_FIRST):
            return self.get_month(
                OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})


def _play(trakt_id: int, season: int = 1, number: int = 7,
          title: str = "") -> watch_history.EpisodePlay:
    """One episode play as a history pull hands it over."""
    return watch_history.EpisodePlay(
        ItemKey(Media.SHOW, "tmdb", str(trakt_id)), season, number,
        title or f"Show {trakt_id}", {"trakt": trakt_id, "tmdb": trakt_id})


class ASeasonThatTurnedOutNotToBeFinishedTests(RolloverOverHttpTestCase):
    """A month said the viewer had finished a season; the provider then learned of
    more episodes and they watched one. Driven through the request, because the
    reconciliation runs off the same history pull the load already makes and there
    is no other way to see it happen.
    """

    def seed_a_finished_season(self) -> None:
        """The closing month's verdict on six episodes, and no viewer record —
        finishing a season takes it off the list, which is the state a reopening
        has to start from."""
        asyncio.run(distrakt_store.add_month_record(self.user_id, CLOSING, {
            "ids": {"trakt": 704, "tmdb": 704, "slug": "slug-704"},
            "season": 1, "title": "Came Back", "network": "Net",
            "kind": distrakt_store.RecordKind.COMPLETED, "watched": 6, "total": 6,
        }))

    def test_the_month_stops_claiming_it_and_the_season_is_back_in_hand(self):
        self.seed_a_finished_season()
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING, plays=[_play(704)])
        self.assertNotIn(704, self.stored_ids(CLOSING),
                         "the month went on recording a verdict it no longer settled")
        self.assertIn(704, self.listed_ids())
        self.assertIn(704, self.shown_ids(payload),
                      "the row was missing from the very load that brought it back")

    def test_the_row_says_it_came_back(self):
        """The completed record was the only other thing that remembered the
        season had been finished, and it is gone — the flag is what the page draws
        the marker from."""
        self.seed_a_finished_season()
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING, plays=[_play(704)])
        row = next(s for s in payload["shows"]
                   if int((s.get("ids") or {}).get("tmdb")) == 704)
        self.assertTrue(row["returned"])

    def test_a_whole_seasons_worth_of_episodes_costs_one_lookup_for_it(self):
        self.seed_a_finished_season()
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, plays=[_play(704, 1, n) for n in range(1, 9)])
        self.assertEqual([call for call in self.season_calls if call[0] == 704],
                         [(704, 1)] * 2,
                         "the reopening's lookup, and the live pass over the row "
                         "it produced — not one per episode watched")

    def test_the_next_load_neither_rewrites_it_nor_asks_again(self):
        """The steady state has to stay a read. Nothing further is watched, so the
        pull reports no plays and the row is left exactly as it was."""
        self.seed_a_finished_season()
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, plays=[_play(704)])
        before = asyncio.run(distrakt_store.user_records(self.user_id))
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING)
        self.assertEqual(asyncio.run(distrakt_store.user_records(self.user_id)), before)
        self.assertTrue(next(s for s in payload["shows"]
                             if int((s.get("ids") or {}).get("tmdb")) == 704)["returned"],
                        "the marker was cleared by a load rather than by the viewer")

    def test_the_viewer_dismissing_the_marker_is_what_clears_it(self):
        self.seed_a_finished_season()
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, plays=[_play(704)])
        resp = self.client.post("/api/distrakt/acknowledge-return",
                                json={"key": "show:tmdb:704", "season": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING)
        self.assertFalse(next(s for s in payload["shows"]
                              if int((s.get("ids") or {}).get("tmdb")) == 704)["returned"])

    def test_an_episode_nothing_knows_about_is_handed_on_untouched(self):
        """No record anywhere matches it, so nothing is looked up for it — that is
        the whole reason it is reported rather than acted on.

        The ids ride along because saying yes to the row means looking the season
        up, and the plays do not outlive the request that reported them."""
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING, plays=[_play(999, 3, 4, "Never Heard Of It")])
        self.assertEqual(payload["unknown_episodes"],
                         [{"key": "show:tmdb:999", "season": 3, "number": 4,
                           "title": "Never Heard Of It",
                           "ids": {"trakt": 999, "tmdb": 999}}])
        self.assertEqual([call for call in self.season_calls if call[0] == 999], [])


def _viewing(month_key: str) -> dict:
    """The {year, month} a row control names as the month on screen, derived from
    the key rather than typed beside it so the two cannot drift apart."""
    year, month = distrakt_store.parse_month_key(month_key)
    return {"year": year, "month": month}


class AnsweringTheUntrackedEpisodeTests(RolloverOverHttpTestCase):
    """The thin row asks a question, and the two answers cost very different
    things: the expensive half happens only if the viewer says yes."""

    ROW = {"key": "show:tmdb:999", "season": 3, "ids": {"trakt": 999, "tmdb": 999},
           "title": "Never Heard Of It"}

    def ask(self) -> dict:
        with fake_today(ON_THE_FIRST):
            return self.get_month(OPENING, plays=[_play(999, 3, 4, "Never Heard Of It")])

    def test_saying_yes_puts_the_season_on_the_list_and_asks_once(self):
        self.ask()
        before = len([c for c in self.season_calls if c[0] == 999])
        with fake_today(ON_THE_FIRST):
            self.post("/api/distrakt/unknown-add", {**self.ROW, **_viewing(OPENING)})
        self.assertIn(999, self.listed_ids())
        self.assertGreater(len([c for c in self.season_calls if c[0] == 999]), before,
                           "saying yes never looked the season up")

    def test_saying_yes_stops_it_being_asked_about_again(self):
        """It is on the list now, so the search finds it and there is nothing left
        to ask — no refusal is needed to silence it."""
        self.ask()
        with fake_today(ON_THE_FIRST):
            self.post("/api/distrakt/unknown-add", {**self.ROW, **_viewing(OPENING)})
        self.assertEqual(self.ask()["unknown_episodes"], [])

    def test_saying_no_is_remembered_across_loads(self):
        """The row is derived from viewing every time viewing is read, so a
        refusal that was not written down would come straight back."""
        self.ask()
        resp = self.client.post("/api/distrakt/unknown-dismiss",
                                json={"key": "show:tmdb:999", "season": 3})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.ask()["unknown_episodes"], [])

    def test_saying_no_writes_no_record_anywhere(self):
        """Declining is not a verdict. Nothing is added to the month, and nothing
        reaches the viewer's list."""
        self.ask()
        self.client.post("/api/distrakt/unknown-dismiss",
                         json={"key": "show:tmdb:999", "season": 3})
        self.assertNotIn(999, self.listed_ids())
        self.assertNotIn(999, self.stored_ids(OPENING))

    def test_declining_one_season_leaves_the_next_one_asking(self):
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, plays=[_play(999, 3, 4)])
        self.client.post("/api/distrakt/unknown-dismiss",
                         json={"key": "show:tmdb:999", "season": 3})
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING, plays=[_play(999, 4, 1)])
        self.assertEqual([u["season"] for u in payload["unknown_episodes"]], [4])


class GoingBackToSomethingYouGaveUpOnTests(RolloverOverHttpTestCase):
    """Watching a thing again after giving up on it is offered back, not taken
    back — and saying yes has to undo the calendar turn-away too, or the next load
    would give it up all over again."""

    GIVEN_UP = {"key": "show:tmdb:703", "season": 1}

    def setUp(self):
        super().setUp()
        self.seed_the_closing_month()  # 703 is the season given up on, with its mark

    def ask(self) -> dict:
        with fake_today(ON_THE_FIRST):
            return self.get_month(OPENING, plays=[_play(703, 1, 2, "Gave Up")])

    def test_it_is_asked_about_rather_than_relisted(self):
        payload = self.ask()
        self.assertEqual([u["key"] for u in payload["given_up_episodes"]],
                         ["show:tmdb:703"])
        self.assertEqual(payload["unknown_episodes"], [],
                         "a season the tracker holds a verdict on is not unknown")
        self.assertNotIn(703, self.listed_ids(), "it was put back without being asked")
        self.assertIn(703, self.stored_ids(CLOSING), "the verdict was withdrawn silently")

    def test_asking_costs_no_lookup(self):
        """The row says only what the history event said. Looking the season up
        merely to offer it back would be paid on every load that saw the play."""
        self.ask()
        self.assertEqual([c for c in self.season_calls if c[0] == 703], [])

    def test_saying_yes_relists_it_and_takes_the_verdict_off_the_month(self):
        self.ask()
        with fake_today(ON_THE_FIRST):
            self.post("/api/distrakt/unknown-resume",
                      {**self.GIVEN_UP, **_viewing(OPENING)})
        self.assertIn(703, self.listed_ids())
        self.assertNotIn(703, self.stored_ids(CLOSING),
                         "the month went on recording a verdict that was withdrawn")

    def test_saying_yes_un_turns_it_away_on_the_calendar(self):
        """Giving up wrote the mark; leaving it standing would have the next
        load's turn-away reconciliation give the season up all over again."""
        self.ask()
        with fake_today(ON_THE_FIRST):
            self.post("/api/distrakt/unknown-resume",
                      {**self.GIVEN_UP, **_viewing(OPENING)})
        marks = asyncio.run(calendar_state.not_watching_ids(self.user_id))
        self.assertNotIn("slug-703", marks)

    def test_saying_yes_holds_across_the_next_load(self):
        """The whole reason the mark is cleared: without it the row comes back
        given-up with nothing to say why."""
        self.ask()
        with fake_today(ON_THE_FIRST):
            self.post("/api/distrakt/unknown-resume",
                      {**self.GIVEN_UP, **_viewing(OPENING)})
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING)
        self.assertIn(703, self.listed_ids())

    def test_saying_no_leaves_the_verdict_and_stops_the_asking(self):
        self.ask()
        resp = self.client.post("/api/distrakt/unknown-dismiss", json=self.GIVEN_UP)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.ask()["given_up_episodes"], [])
        self.assertIn(703, self.stored_ids(CLOSING))
        self.assertNotIn(703, self.listed_ids())


class MonthFreezesWhenItsDatePassesTests(RolloverOverHttpTestCase):
    """The month under way stays editable; once the calendar has passed it, it
    closes — whether or not any later month has been built or looked at.

    This is the transition the author could not watch happen: it needs two
    requests separated by a date change, which is precisely what could not be
    arranged before.
    """

    def test_the_closing_month_is_open_while_it_is_still_under_way(self):
        self.seed_the_closing_month()
        with fake_today(BEFORE_THE_FIRST):
            payload = self.get_month(CLOSING)
        self.assertFalse(payload.get("closed"), "the month froze while it was still on")

    def test_it_freezes_when_the_next_month_is_first_opened(self):
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(ON_THE_FIRST):
            closed = self.get_month(CLOSING)
        self.assertTrue(closed["closed"], "it did not freeze once the next month opened")

    def test_it_freezes_on_its_own_without_the_next_month_ever_being_opened(self):
        # The reason the rule is the clock's and not the next month's: a tracker
        # nobody touches for three weeks used to keep the closing month open and
        # editable the whole time, because the thing that closed it was a side
        # effect of opening a month the user had no reason to open.
        self.seed_the_closing_month()
        with fake_today(BEFORE_THE_FIRST):
            self.assertFalse(self.get_month(CLOSING)["closed"])
        with fake_today(date(2026, 8, 21)):
            closed = self.get_month(CLOSING)
        self.assertTrue(closed["closed"], "it stayed open because nobody opened the next month")
        self.assertEqual(self.stored_ids(OPENING), set(),
                         "freezing one month built the month after it")

    def test_a_month_two_months_back_freezes_the_first_time_it_is_looked_at(self):
        # The snapshot is lazy, not scheduled: this app runs no background job, so
        # a month that settled while nobody was looking materialises whenever
        # somebody first is — however long that takes.
        self.seed_the_closing_month()
        with fake_today(date(2026, 9, 30)):
            closed = self.get_month(CLOSING)
        self.assertTrue(closed["closed"])

    def test_a_frozen_month_stays_frozen_on_a_later_day(self):
        # A freeze is a one-way door; re-reading it later must not reopen it, or
        # a closed month would start moving again every time somebody looked.
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(date(2026, 9, 15)):
            closed = self.get_month(CLOSING)
        self.assertTrue(closed["closed"])


class AMonthHoldsOnlyItsOwnPremieresTests(RolloverOverHttpTestCase):
    """A month holds what STARTS in it, whether or not it has begun.

    What the viewer is in the middle of is a fact about the viewer and lives on
    their own list, so there is nothing for a new month to take from the one
    before it. Taking it anyway made a season that began in one month get
    announced as new in the next, gave a month built ahead a list frozen at build
    time, and read a calendar turn-away made during that wait as giving up on a
    show that had never started.
    """

    def test_opening_a_month_that_has_not_begun_does_not_build_it(self):
        self.seed_the_closing_month()
        with fake_today(BEFORE_THE_FIRST):
            payload = self.get_month(OPENING)
        self.assertEqual(payload.get("shows", []), [])
        self.assertEqual(self.stored_ids(OPENING), set(),
                         "merely looking at next month wrote records into it")

    def test_importing_it_early_takes_premieres_and_nothing_else(self):
        self.seed_the_closing_month()
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
            self.import_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        self.assertEqual(
            self.stored_ids(OPENING), {801},
            "a month that has not begun took titles from the month before it")

    def test_the_same_month_takes_nothing_extra_once_it_has_begun(self):
        # The month opening changes nothing about what it HOLDS. It used to be the
        # moment the previous month's rows were copied across, which is how a
        # preview built early could never gain what opening was supposed to bring.
        self.seed_the_closing_month()
        self.open_the_new_month()
        self.assertEqual(self.stored_ids(OPENING), {801},
                         "the month under way copied the closing month's records in")

    def test_an_announcement_is_split_by_which_premiere_it_is(self):
        """A first season is a series premiere and a later one is a season
        premiere, and which it is is decided once, when the record is made —
        never re-derived from the season number at render time."""
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(OPENING, {OPENING: [
                _item(801, 1, "August New", "2026-08-05"),
                _item(911, 4, "August Returner", "2026-08-08"),
            ]})
        self.assertEqual(self.kinds_on(OPENING), {
            801: {distrakt_store.RecordKind.SERIES_PREMIERE},
            911: {distrakt_store.RecordKind.SEASON_PREMIERE},
        })

    def test_the_still_going_title_is_on_the_new_months_page(self):
        # And this is why nothing needs copying: the show the viewer is part-way
        # through is on the new month's page as THEIR list, read off the list it
        # lives on. Its finished and given-up neighbours are not.
        self.seed_the_closing_month()
        payload = self.open_the_new_month()
        shown = self.shown_ids(payload)
        self.assertIn(701, shown, "the viewer's own list lost the title it was holding")
        self.assertNotIn(702, shown)  # finished: the closing month's verdict
        self.assertNotIn(703, shown)  # given up on: the closing month's verdict
        self.assertEqual(self.stored_ids(OPENING), {801}, "showing it wrote it in")

    def test_a_premiere_that_has_started_airing_joins_the_viewers_list(self):
        """The month keeps its announcement and the season becomes something the
        viewer is keeping up with — two records, saying two different things."""
        self.seed_the_closing_month()
        self.open_the_new_month()
        self.assertEqual(self.kinds_on(OPENING),
                         {801: {distrakt_store.RecordKind.SERIES_PREMIERE}})
        self.assertIn(801, self.listed_ids())


class AnyMonthAheadCanBeAskedForTests(RolloverOverHttpTestCase):
    """No bound on how far ahead the ask may point, and no order the months have
    to be built in. What the ask gathers is only what is already known about that
    month's calendar, so distance costs nothing."""

    FAR_AHEAD = "2026-11"

    def test_a_month_months_ahead_is_built_by_asking_for_it(self):
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(self.FAR_AHEAD,
                              {self.FAR_AHEAD: [_item(801, 1, "November New", "2026-11-05")]})
        self.assertEqual(self.stored_ids(self.FAR_AHEAD), {801})

    def test_the_months_it_skipped_are_not_stranded_behind_it(self):
        # The reason the bound could not simply be widened: the store grew forward
        # only, so a month built out ahead put every month between here and it
        # permanently out of reach — the month under way included.
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(self.FAR_AHEAD,
                              {self.FAR_AHEAD: [_item(801, 1, "November New", "2026-11-05")]})
            self.import_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        self.assertEqual(self.stored_ids(OPENING), {801})

    def test_a_month_the_calendar_has_passed_is_still_refused(self):
        # The one refusal left: working out a month nobody was tracking from its
        # premieres would be inventing it, and there is a sweep of what was
        # actually watched for that.
        with fake_today(ON_THE_FIRST):
            self.import_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
            with self.offline():
                resp = self.client.post("/api/distrakt/import", json={"year": 2026, "month": 7})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("never tracked", resp.json()["error"])
        self.assertEqual(self.stored_ids(CLOSING), set())


class AVerdictStaysInTheMonthItWasReachedInTests(RolloverOverHttpTestCase):
    """A verdict belongs to the month it was reached in and does not travel."""

    def test_a_finished_season_does_not_arrive_in_the_new_month(self):
        self.seed_the_closing_month()
        self.open_the_new_month()
        self.assertNotIn(702, self.stored_ids(OPENING))

    def test_a_season_given_up_on_does_not_arrive_either(self):
        self.seed_the_closing_month()
        self.open_the_new_month()
        self.assertNotIn(703, self.stored_ids(OPENING))

    def test_both_are_still_on_the_closing_month_afterwards(self):
        # "Does not travel" has to mean stayed put, not disappeared — a rollover
        # that dropped the record from both months would pass the two tests above.
        self.seed_the_closing_month()
        self.open_the_new_month()
        self.assertEqual(self.stored_ids(CLOSING), {701, 702, 703})

    def test_the_frozen_month_keeps_each_record_saying_what_it_is(self):
        # Read off the stored doc rather than the rendered payload: a month that
        # renders correctly today from live data while having stored nothing would
        # look identical from the outside and go wrong the moment Trakt is
        # unreachable.
        self.seed_the_closing_month()
        self.open_the_new_month()
        doc = asyncio.run(distrakt_store.load_month(self.user_id, CLOSING))
        self.assertTrue(doc["closed"])
        self.assertEqual(self.kinds_on(CLOSING), {
            701: {distrakt_store.RecordKind.SERIES_PREMIERE},
            702: {distrakt_store.RecordKind.COMPLETED},
            703: {distrakt_store.RecordKind.ABANDONED},
        })

    def test_a_frozen_month_still_announces_every_premiere_it_had(self):
        """The first notice is about what BEGAN in the month, whatever each of
        those titles has since become — so freezing does not empty it."""
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(ON_THE_FIRST):
            closed = self.get_month(CLOSING)
        self.assertIn("Still Going", closed["post1"])

    def test_the_still_going_title_reads_as_work_in_hand(self):
        # The closing month's verdict on 703 travels nowhere, and neither does its
        # absence of one on 701: the title the viewer is part-way through reads as
        # work in hand on the new month's page, not as something walked away from.
        self.seed_the_closing_month()
        payload = self.open_the_new_month()
        row = next(s for s in payload["shows"]
                   if int((s.get("ids") or {}).get("tmdb")) == 701)
        self.assertFalse(row["abandoned"])
        self.assertIn(row["bucket"], ("cleanup", "keepup"))


class ActingOnARowFromTheViewersOwnListTests(RolloverOverHttpTestCase):
    """The page shows what the viewer has in hand alongside what the month
    announced, so the two controls on such a row have to reach the right table."""

    def _abandon(self, abandoned: bool) -> None:
        self.post("/api/distrakt/abandon", {
            "year": 2026, "month": 8, "key": "show:tmdb:701", "season": 1,
            "abandoned": abandoned})

    def test_giving_up_on_it_is_recorded_against_the_month_it_happened_in(self):
        # Giving up is an ACT and it happens today. The month it is recorded on is
        # the clock's — not the month the season was announced in, which has
        # already settled.
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(ON_THE_FIRST):
            self._abandon(True)
        self.assertEqual(self.kinds_on(OPENING).get(701),
                         {distrakt_store.RecordKind.ABANDONED})
        self.assertNotIn(701, self.listed_ids(), "it stayed on the list after being dropped")

    def test_the_month_that_announced_it_is_left_as_it_was(self):
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(ON_THE_FIRST):
            self._abandon(True)
        self.assertEqual(self.kinds_on(CLOSING).get(701),
                         {distrakt_store.RecordKind.SERIES_PREMIERE})

    def test_taking_it_back_puts_it_on_the_list_and_removes_the_month_row(self):
        # A month records what it settled, and this one no longer settled it.
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(ON_THE_FIRST):
            self._abandon(True)
            self._abandon(False)
        self.assertNotIn(701, self.kinds_on(OPENING))
        self.assertIn(701, self.listed_ids())

    def test_it_is_still_taken_back_after_the_month_is_read_again(self):
        # THE ROUND TRIP. Un-abandoning and then opening the month is what a
        # person does, and it is where anything left behind would undo the click
        # they just made.
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(ON_THE_FIRST):
            self._abandon(True)
            self._abandon(False)
            payload = self.get_month(OPENING)
        row = next(s for s in payload["shows"]
                   if int((s.get("ids") or {}).get("tmdb")) == 701)
        self.assertFalse(row["abandoned"], "it was given up on again by being looked at")
        self.assertIn(row["bucket"], ("cleanup", "keepup"))

    def test_removing_it_takes_every_copy_so_it_does_not_come_back(self):
        # Taking it off one place leaves the copy that put it on the page, and it
        # returns on the next load with the ✕ looking broken.
        self.seed_the_closing_month()
        self.open_the_new_month()
        with fake_today(ON_THE_FIRST):
            self.post("/api/distrakt/remove", {
                "year": 2026, "month": 8, "key": "show:tmdb:701", "season": 1})
            payload = self.get_month(OPENING)
        self.assertNotIn(701, self.stored_ids(CLOSING))
        self.assertNotIn(701, self.listed_ids())
        self.assertNotIn(701, self.shown_ids(payload))


class AMonthThatHasNotBegunHasNoWorkInHandTests(RolloverOverHttpTestCase):
    """A season imported into a month still ahead has not aired, nobody is behind
    on it, and the month has nothing in hand to report — all it can honestly
    announce is what premieres in it.

    Both halves used to arrive through one shared list, so 26 titles premiering
    the following month were written onto the month under way and given up on in
    the time it takes to open the page. The row was hidden afterwards by the
    filter that decides what a month may show, which is why nothing looked wrong —
    the write had already happened.
    """

    def test_an_unaired_premiere_is_never_treated_as_something_being_watched(self):
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(OPENING, {OPENING: [_item(911, 1, "Not Aired Yet", "2026-08-08")]})
            payload = self.get_month(OPENING)
        self.assertEqual(self.kinds_on(OPENING),
                         {911: {distrakt_store.RecordKind.SERIES_PREMIERE}})
        self.assertEqual(self.listed_ids(), set(),
                         "a season that has not aired reached the viewer's list")
        self.assertIn(911, self.shown_ids(payload), "the month lost its own announcement")

    def test_what_the_viewer_has_in_hand_is_not_that_months_to_show(self):
        self.seed_the_closing_month()
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(OPENING, {OPENING: [_item(911, 1, "Not Aired Yet", "2026-08-08")]})
            payload = self.get_month(OPENING)
        self.assertNotIn(701, self.shown_ids(payload))
        # Kept off that month's view, not taken away.
        self.assertIn(701, self.listed_ids())


class TheCalendarAndTheTrackerMirrorEachOtherTests(RolloverOverHttpTestCase):
    """Turning a show away on the main calendar and giving up on it here are one
    decision, said in two places.

    THE CALENDAR CANNOT CALL IN — it has no idea the tracker exists — so the half
    that starts over there is closed by reading the marks on the way past, every
    time a month is read. That makes the loop the thing to watch: a verdict
    reached here writes a mark, and a mark read here becomes a verdict, so the
    steady state has to be silent or two page loads would argue with each other.

    THE MONTH'S STANDING IS THE ONLY DISCRIMINATOR, never when a mark was made.
    A month that has not begun has nothing in hand and no verdicts to reach, so a
    turn-away there takes the row away instead of settling it; a month the
    calendar has passed settled what it settled and is not reopened.
    """

    def _mark(self, slug: str, turned_away: bool) -> None:
        """Turn a show away on the main calendar, or take that back — written in
        the calendar's own terms, which is the slug it keys its cards by."""
        asyncio.run(calendar_state.set_not_watching(self.user_id, slug, turned_away))

    def _marks(self) -> set:
        return asyncio.run(calendar_state.not_watching_ids(self.user_id))

    def test_a_show_turned_away_over_there_is_given_up_on_here(self):
        # 701 is on the viewer's own list and belongs to no month at all, which is
        # the case the rule this replaces could never write a verdict for: it
        # asked whether the title was one of the VIEWED month's premieres.
        self.seed_the_closing_month()
        self._mark("slug-701", True)
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        self.assertIn(distrakt_store.RecordKind.ABANDONED, self.kinds_on(CLOSING)[701])
        self.assertNotIn(701, self.listed_ids())

    def test_the_month_that_announced_it_still_says_so(self):
        # Giving up settles a season; it does not retract the announcement that
        # the season began.
        self.seed_the_closing_month()
        self._mark("slug-701", True)
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        self.assertIn(distrakt_store.RecordKind.SERIES_PREMIERE, self.kinds_on(CLOSING)[701])

    def test_taking_the_mark_back_puts_it_back_in_hand(self):
        self.seed_the_closing_month()
        self._mark("slug-701", True)
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
            self._mark("slug-701", False)
            self.get_month(CLOSING)
        self.assertNotIn(distrakt_store.RecordKind.ABANDONED, self.kinds_on(CLOSING)[701])
        self.assertIn(701, self.listed_ids())

    def test_reading_the_month_twice_over_changes_nothing(self):
        """THE LOOP. An abandon writes a mark and a mark writes an abandon, so a
        plain read has to be silent — otherwise every load rewrites the verdict it
        just read, and the counts it was reached on go with it."""
        self.seed_the_closing_month()
        self._mark("slug-701", True)
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
            settled = asyncio.run(distrakt_store.month_records(
                self.user_id, CLOSING, distrakt_store.SETTLED_KINDS))
            self.get_month(CLOSING)
            again = asyncio.run(distrakt_store.month_records(
                self.user_id, CLOSING, distrakt_store.SETTLED_KINDS))
        self.assertEqual(settled, again)
        self.assertEqual(self._marks(), {"slug-701", "slug-703"})

    def test_giving_up_here_says_so_over_there(self):
        # The other direction of the same mirror, over HTTP: the ✕ and Abandon
        # both write the mark, whatever put the row on the tracker.
        self.seed_the_closing_month()
        with fake_today(BEFORE_THE_FIRST):
            self.post("/api/distrakt/abandon", {
                "year": 2026, "month": CLOSING_MONTH, "key": "show:tmdb:701",
                "season": 1, "abandoned": True})
        self.assertIn("slug-701", self._marks())

    def test_a_verdict_reached_here_survives_the_next_load(self):
        """The half of the loop that bites hardest: the mark the abandon wrote is
        read back on the very next load, and reading it must confirm the verdict
        rather than start over from the premiere record."""
        self.seed_the_closing_month()
        with fake_today(BEFORE_THE_FIRST):
            self.post("/api/distrakt/abandon", {
                "year": 2026, "month": CLOSING_MONTH, "key": "show:tmdb:701",
                "season": 1, "abandoned": True})
            self.get_month(CLOSING)
        self.assertIn(distrakt_store.RecordKind.ABANDONED, self.kinds_on(CLOSING)[701])
        self.assertNotIn(701, self.listed_ids())

    def test_a_month_that_has_not_begun_loses_the_row_instead(self):
        """THE ONE EXCEPTION. Nothing in that month has aired, so there is no
        verdict to record — recording one would announce a decision about
        something nobody has had the chance to watch. The announcement goes
        instead, which is the same answer the import path reaches by never adding
        a marked title in the first place."""
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(OPENING, {OPENING: [_item(911, 1, "Not Aired Yet", "2026-08-08")]})
            self._mark("slug-911", True)
            self.get_month(OPENING)
        self.assertEqual(self.kinds_on(OPENING), {})
        self.assertEqual(self.listed_ids(), set())

    def test_a_month_the_calendar_has_passed_is_not_reopened(self):
        """A verdict belongs to the month it was reached in. Taking the mark back
        afterwards is a decision made now, and it does not reach back into a month
        that has already settled."""
        self.seed_the_closing_month()
        self.open_the_new_month()
        self._mark("slug-703", False)
        with fake_today(ON_THE_FIRST):
            self.get_month(CLOSING)
        self.assertEqual(self.kinds_on(CLOSING)[703], {distrakt_store.RecordKind.ABANDONED})


class TheRealClockStillGovernsWithoutTheVariableTests(RolloverOverHttpTestCase):
    """None of the above is reachable on an ordinary instance.

    The whole file sets an environment variable; this asserts that with it unset
    the app answers about the real month, so a green run here is not evidence
    that the seam leaks into a normal deployment.
    """

    def test_the_month_api_answers_about_the_real_month_by_default(self):
        # No `fake_today` around this one, and no year/month in the query either:
        # the route falls back to its own idea of today, which must be the real
        # one. DERIVED, not written down — a literal here would agree with the
        # real calendar for one month and then start failing, which is the rot
        # this file's header is about and which the months above escape only
        # because an override is in force for them.
        real = date.today()
        with self.offline():
            resp = self.client.get("/api/distrakt/month")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["month"],
                         distrakt_store.month_key(real.year, real.month))

    def test_a_faked_date_reaches_the_route_over_http(self):
        # The other direction, and the reason the rest of the file means
        # anything: with the variable set the route answers about the faked month.
        # Ten years out so it can never coincide with the real one.
        far = date(date.today().year + 10, 3, 1)
        with fake_today(far), self.offline():
            resp = self.client.get("/api/distrakt/month")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["month"], distrakt_store.month_key(far.year, far.month))
