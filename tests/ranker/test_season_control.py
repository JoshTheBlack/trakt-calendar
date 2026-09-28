"""A show row's own control for what it is: the whole show, or one season of it.

Four promises, each held here:

  - WHAT IS OFFERED. "Whole show" and every season a service lists. Every choice
    the board already holds for that show — including what the row is now — is
    offered DISABLED rather than left out, so no second row for one season can
    be made from it.
  - WHAT IS SUGGESTED. The latest season the viewer finished, where the tracker
    knows one; otherwise the latest season that has aired by the service's own
    premiere dates. Never a season still to air, and never the specials — but
    both are listed and can be chosen.
  - WHEN IT IS ASKED. The season list is a lookup at a service, so it is made
    when the control is OPENED — never when the board, a row or its ⇄ menu is
    drawn. A board draws up to a thousand rows.
  - WHAT CHOOSING DOES. The row becomes that season (or the whole show) IN PLACE:
    the same id, the same tier, the same rank. A film has no such control.
"""
from __future__ import annotations

import asyncio
import re
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from app import db
from app.providers import detail_source
from app.providers.base import SeasonsAnswer, Source
from app.ranker import core as ranker, seasons
from tests.ranker.test_removals import BOARD, key, show_ref
from tests.ranker.test_seasons import _SeasonsTestCase, season_key

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
SHOW = "7"

# A show with three aired seasons and a fourth announced far ahead (the routes
# read the real clock, so "not aired yet" has to stay true), in the
# two shapes the services date a premiere in.
LISTED = [
    {"season": 1, "episode_count": 8, "first_aired": "2022-02-18T02:00:00.000Z"},
    {"season": 2, "episode_count": 10, "first_aired": "2025-01-17"},
    {"season": 3, "episode_count": 9, "first_aired": "2026-09-01T01:00:00.000Z"},
    {"season": 4, "episode_count": 10, "first_aired": "2099-01-15T02:00:00.000Z"},
]


def by_season(options) -> dict:
    return {option.season: option for option in options}


class WhatIsOfferedTests(unittest.TestCase):

    def options(self, listed=LISTED, *, held=(None,), current=None, finished=None):
        return seasons.season_options(listed, held=held, current=current,
                                      finished=finished, now=NOW)

    def test_the_whole_show_comes_first_then_every_season_in_order(self):
        self.assertEqual([option.season for option in self.options()], [None, 1, 2, 3, 4])
        self.assertEqual([option.label for option in self.options()][:2],
                         ["Whole show", "Season 1"])

    def test_what_the_board_holds_is_disabled_and_the_row_itself_is_marked(self):
        options = by_season(self.options(held=(None, 2), current=2))
        self.assertTrue(options[2].held and options[2].current)
        self.assertTrue(options[None].held and not options[None].current)
        self.assertFalse(options[1].held or options[3].held)
        self.assertEqual(options[2].notes[0], "this row")
        self.assertEqual(options[None].notes, ("on the board",))

    def test_a_season_no_service_listed_is_still_offered_when_the_row_is_it(self):
        options = by_season(self.options([], held=(5,), current=5))
        self.assertEqual(list(options), [None, 5])
        self.assertTrue(options[5].current)

    def test_a_season_nobody_listed_is_not_called_unaired(self):
        """No list means nothing is known about it — measured in the browser on a
        failed lookup, where a held season was drawn "not aired yet"."""
        options = by_season(self.options([], held=(None, 2), current=None, finished=3))
        self.assertIsNone(options[2].aired)
        self.assertEqual(options[2].notes, ("on the board",))
        self.assertEqual(options[3].notes, ("suggested",))

    def test_the_specials_are_listed_and_named(self):
        options = by_season(self.options([{"season": 0, "first_aired": "2020-01-01"},
                                          *LISTED]))
        self.assertEqual(options[0].label, "Specials")
        self.assertEqual(options[0].value, "0")
        self.assertEqual(options[None].value, "")


class WhatIsSuggestedTests(unittest.TestCase):

    def suggested(self, listed=LISTED, *, finished=None):
        return [option.season for option in seasons.season_options(
            listed, held=(None,), current=None, finished=finished, now=NOW)
            if option.suggested]

    def test_the_latest_aired_season_by_the_services_own_dates(self):
        self.assertEqual(self.suggested(), [3])

    def test_never_a_season_still_to_air_but_it_is_listed_with_its_date(self):
        options = by_season(seasons.season_options(
            LISTED, held=(), current=None, finished=None, now=NOW))
        self.assertFalse(options[4].suggested)
        self.assertFalse(options[4].aired)
        self.assertEqual(options[4].premiere, date(2099, 1, 15))
        self.assertIn("airs 15 Jan 2099", options[4].notes)

    def test_the_viewers_finished_season_wins_over_the_latest_aired(self):
        self.assertEqual(self.suggested(finished=1), [1])

    def test_nothing_aired_suggests_nothing(self):
        self.assertEqual(self.suggested([LISTED[3]]), [])
        self.assertEqual(self.suggested([{"season": 1, "first_aired": ""}]), [])

    def test_the_specials_are_never_the_suggestion(self):
        self.assertEqual(self.suggested([{"season": 0, "first_aired": "2026-09-20"}]), [])

    def test_a_suggestion_already_on_the_board_says_both(self):
        options = by_season(seasons.season_options(
            LISTED, held=(None, 3), current=None, finished=None, now=NOW))
        self.assertEqual(options[3].notes, ("on the board", "suggested"))

    def test_a_premiere_is_compared_as_the_service_states_it(self):
        self.assertTrue(seasons.has_aired("2026-09-27T11:59:00.000Z", NOW))
        self.assertFalse(seasons.has_aired("2026-09-27T12:01:00.000Z", NOW))
        self.assertTrue(seasons.has_aired("2026-09-27", NOW))
        self.assertFalse(seasons.has_aired("2026-09-28", NOW))
        self.assertFalse(seasons.has_aired("", NOW))
        self.assertFalse(seasons.has_aired("soon", NOW))


def answer(listed=LISTED) -> SeasonsAnswer:
    return SeasonsAnswer(seasons=list(listed), named_season=None, ids={}, network="")


def stub_seasons(listed=LISTED, cached=None):
    """A season list answering `listed`; `cached`, when given, collects the
    `cached_only` each call was made with."""
    calls = []

    async def _seasons(settings, chosen, media, *, cached_only=False):
        calls.append((chosen.source, chosen.source_id))
        if cached is not None:
            cached.append(cached_only)
        return answer(listed)

    return calls, _seasons


def exploding(*args, **kwargs):
    raise AssertionError("a season list was asked for before the control was opened")


class _ControlTestCase(_SeasonsTestCase):

    def setUp(self):
        super().setUp()
        # Somebody to ask: the row carries a Trakt id, and choose() picks Trakt.
        self.chosen = detail_source.Answerer(Source.TRAKT, "70", stored_only=False)
        patch = mock.patch.object(detail_source, "choose", lambda settings, ids: self.chosen)
        patch.start()
        self.addCleanup(patch.stop)

    def menu(self, row_key: str) -> str:
        return self.client.get(f"/rankings/fragments/move-menu?board={BOARD}&key={row_key}").text

    def season_list(self, row_key: str, listed=LISTED) -> tuple[str, list]:
        calls, fake = stub_seasons(listed)
        with mock.patch.object(detail_source, "seasons", fake):
            body = self.client.get(f"/rankings/fragments/seasons?board={BOARD}&key={row_key}").text
        return body, calls

    def choices(self, body: str) -> list[tuple[str, bool, str]]:
        """(value, disabled, notes) for every choice the list drew."""
        return [(value, bool(disabled), re.sub(r"<[^>]+>", "", notes or "").strip())
                for value, disabled, notes in re.findall(
                    r'<button name="season" value="([^"]*)"[^>]*?( disabled)?>'
                    r'.*?(?:<span class="ranker-season-note">(.*?)</span>)?\s*</button>',
                    body, re.S)]

    def set_season(self, row_key: str, season) -> str:
        return self.client.post(f"/rankings/boards/{BOARD}/items/season",
                                json={"key": row_key, "season": season}).text


class WhenItIsAskedTests(_ControlTestCase):

    def test_drawing_the_board_and_the_menu_asks_nobody(self):
        self.build_with_seasons()
        with mock.patch.object(detail_source, "seasons", exploding):
            self.assertEqual(self.client.get(f"/rankings?board={BOARD}").status_code, 200)
            menu = self.menu(key(SHOW))
        self.assertIn('class="ranker-season"', menu)
        self.assertIn('hx-trigger="click once"', menu)
        self.assertIn(f'hx-get="/rankings/fragments/seasons?board={BOARD}&amp;key=show%3Atmdb%3A7"',
                      menu)

    def test_opening_the_control_asks_the_service_that_describes_the_title(self):
        self.build_with_seasons()
        _, calls = self.season_list(key(SHOW))
        self.assertEqual(calls, [(Source.TRAKT, "70")])

    def test_a_film_row_has_no_season_control(self):
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD))
        self.add({"media": "movie", "match_source": "tmdb", "match_id": "550", "title": "F"})
        self.assertNotIn("ranker-season", self.menu("movie:tmdb:550"))
        body, calls = self.season_list("movie:tmdb:550")
        self.assertEqual((body, calls), ("", []))


class WhatTheListDrawsTests(_ControlTestCase):

    def test_held_choices_are_disabled_and_the_suggestion_is_said(self):
        # The board holds the whole show, season 2 and season 1 of it.
        self.build_with_seasons()
        body, _ = self.season_list(key(SHOW))
        self.assertEqual(self.choices(body), [
            ("", True, "this row"),
            ("1", True, "on the board"),
            ("2", True, "on the board"),
            ("3", False, "suggested"),
            ("4", False, "airs 15 Jan 2099"),
        ])
        self.assertIn(f'name="key" value="{key(SHOW)}"', body)

    def test_the_tracker_suggests_the_season_the_viewer_finished(self):
        self.build_with_seasons()
        asyncio.run(db.execute("UPDATE users SET distrakt_approved = 1 WHERE id = ?",
                               (self.user_id,)))
        for season in (1, 3):
            asyncio.run(db.execute(
                "INSERT INTO distrakt_month_records (user_id, month, kind, media, "
                "match_source, match_id, season, created_at) "
                "VALUES (?, '2026-03', 'completed', 'show', 'tmdb', ?, ?, 0)",
                (self.user_id, SHOW, season)))
        # Season 3 finished and on no row of this board: suggested.
        body, _ = self.season_list(key(SHOW))
        self.assertIn(("3", False, "suggested"), self.choices(body))

    def test_an_account_without_the_tracker_gets_the_aired_suggestion(self):
        self.build_with_seasons()
        asyncio.run(db.execute(
            "INSERT INTO distrakt_month_records (user_id, month, kind, media, "
            "match_source, match_id, season, created_at) "
            "VALUES (?, '2026-03', 'completed', 'show', 'tmdb', ?, 1, 0)",
            (self.user_id, SHOW)))
        body, _ = self.season_list(key(SHOW), [LISTED[0], LISTED[1]])
        # Not approved for the tracker, so its record is not read: season 2 is
        # the latest aired, and it is on the board already.
        self.assertIn(("2", True, "on the board · suggested"), self.choices(body))

    def test_nobody_to_ask_still_draws_the_whole_show_and_says_why(self):
        self.build_with_seasons()
        self.chosen = None
        body, calls = self.season_list(season_key(SHOW, 2))
        self.assertEqual(calls, [])
        self.assertEqual([value for value, _, _ in self.choices(body)], ["", "1", "2"])
        self.assertIn("No service here can list this show", body)

    def test_a_failed_lookup_says_so(self):
        from app.providers.base import SourceUnavailable
        self.build_with_seasons()

        async def _down(settings, chosen, media):
            raise SourceUnavailable("down")

        with mock.patch.object(detail_source, "seasons", _down):
            body = self.client.get(
                f"/rankings/fragments/seasons?board={BOARD}&key={key(SHOW)}").text
        self.assertIn("Couldn&#39;t list this show&#39;s seasons", body)


class WhatChoosingDoesTests(_ControlTestCase):

    def row(self, row_key: str) -> dict:
        return asyncio.run(db.fetch_one(
            "SELECT id, category_id, rank_in_category, season FROM tier_items "
            " WHERE match_id = ? AND IFNULL(season, -1) = ?",
            (SHOW, -1 if row_key == key(SHOW) else int(row_key.rsplit(":s", 1)[1]))))

    def test_whole_show_to_season_keeps_id_tier_and_rank(self):
        self.build_with_seasons()
        before = self.row(key(SHOW))
        version = self.version()
        body = self.set_season(key(SHOW), 3)
        self.assertIn("This row is Season 3 now.", body)
        after = self.row(season_key(SHOW, 3))
        self.assertEqual((after["id"], after["category_id"], after["rank_in_category"]),
                         (before["id"], before["category_id"], before["rank_in_category"]))
        self.assertEqual(self.keys_in("s"), [season_key(SHOW, 3), season_key(SHOW, 2), key("8")])
        self.assertEqual(self.version(), version + 1)

    def test_season_to_season_and_back_to_the_whole_show(self):
        self.build_with_seasons()
        self.set_season(key(SHOW), 3)
        before = self.row(season_key(SHOW, 2))
        self.set_season(season_key(SHOW, 2), 5)
        self.assertEqual(self.keys_in("s"), [season_key(SHOW, 3), season_key(SHOW, 5), key("8")])
        body = self.set_season(season_key(SHOW, 5), "")
        self.assertIn("This row is the whole show now.", body)
        after = self.row(key(SHOW))
        self.assertEqual((after["id"], after["rank_in_category"]),
                         (before["id"], before["rank_in_category"]))
        self.assertEqual(self.keys_in("s"), [season_key(SHOW, 3), key(SHOW), key("8")])

    def test_a_season_the_board_holds_is_refused_and_nothing_changes(self):
        self.build_with_seasons()
        version = self.version()
        body = self.set_season(key(SHOW), 2)
        self.assertIn("This board already holds that season.", body)
        self.assertEqual(self.keys_in("s"), [key(SHOW), season_key(SHOW, 2), key("8")])
        self.assertEqual(self.version(), version)

    def test_choosing_what_the_row_already_is_changes_nothing(self):
        self.build_with_seasons()
        version = self.version()
        self.set_season(season_key(SHOW, 2), "2")
        self.assertEqual(self.version(), version)

    def test_a_film_cannot_be_given_a_season(self):
        asyncio.run(ranker.create_board(self.user_id, uid=BOARD))
        self.add({"media": "movie", "match_source": "tmdb", "match_id": "550", "title": "F"})
        body = self.set_season("movie:tmdb:550", 1)
        self.assertIn("Only a show has seasons.", body)
        self.assertEqual(self.value("SELECT season FROM tier_items"), None)

    def test_the_new_key_is_what_every_later_request_names(self):
        self.build_with_seasons()
        self.set_season(key(SHOW), 3)
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 3)]))
        self.assertEqual(self.history(), [season_key(SHOW, 3)])
        asyncio.run(ranker.restore_removal(self.user_id, BOARD, season_key(SHOW, 3)))
        self.assertEqual(self.keys_in("s"), [season_key(SHOW, 3), season_key(SHOW, 2), key("8")])

    def test_a_season_row_is_labelled_on_the_board(self):
        self.build_with_seasons()
        body = self.client.get(f"/rankings/fragments/row?board={BOARD}"
                               f"&key={season_key(SHOW, 2)}").text
        self.assertIn('<span class="ranker-show">Title 7</span>'
                      '<span class="ranker-season">— Season 2</span>', body)
        self.assertIn('data-details-heading="Title 7 — Season 2"', body)
        whole = self.client.get(f"/rankings/fragments/row?board={BOARD}&key={key(SHOW)}").text
        self.assertIn('<span class="ranker-show">Title 7</span></span>', whole)
        self.assertNotIn("ranker-season", whole)


NAMED = [
    {"season": 1, "episode_count": 8, "first_aired": "2014-01-12", "name": ""},
    {"season": 4, "episode_count": 6, "first_aired": "2024-01-14", "name": "Night Country"},
]


class WhatAChoiceWritesTests(_ControlTestCase):
    """Choosing a season writes what the row now COUNTS and is CALLED, read from
    the season list the control was drawn from — out of the store, never a
    second request — and never leaves the old definition's numbers behind."""

    def facts(self, row_key: str) -> tuple:
        season = None if row_key == key(SHOW) else int(row_key.rsplit(":s", 1)[1])
        row = asyncio.run(db.fetch_one(
            "SELECT season_count, episode_count, season_name FROM tier_items "
            " WHERE match_id = ? AND IFNULL(season, -1) = ?",
            (SHOW, -1 if season is None else season)))
        return row["season_count"], row["episode_count"], row["season_name"]

    def choose(self, row_key: str, season, listed=LISTED) -> tuple[str, list, list]:
        cached: list = []
        calls, fake = stub_seasons(listed, cached)
        with mock.patch.object(detail_source, "seasons", fake):
            body = self.set_season(row_key, season)
        return body, calls, cached

    def test_a_season_counts_its_own_episodes_and_no_seasons(self):
        self.build_with_seasons()
        asyncio.run(db.execute("UPDATE tier_items SET season_count = 5, episode_count = 38 "
                               " WHERE match_id = ? AND season IS NULL", (SHOW,)))
        _, calls, cached = self.choose(key(SHOW), 3)
        self.assertEqual(self.facts(season_key(SHOW, 3)), (None, 9, None))
        self.assertEqual(calls, [(Source.TRAKT, "70")])
        self.assertEqual(cached, [True], "the list must be read from the store, not asked for")

    def test_back_to_the_whole_show_counts_the_list(self):
        self.build_with_seasons()
        self.choose(key(SHOW), 3)
        body, _, _ = self.choose(season_key(SHOW, 3), "")
        self.assertIn("This row is the whole show now.", body)
        # Four listed seasons, 8 + 10 + 9 + 10 episodes.
        self.assertEqual(self.facts(key(SHOW)), (4, 37, None))

    def test_a_list_nobody_stored_leaves_the_counts_blank_not_stale(self):
        self.build_with_seasons()
        asyncio.run(db.execute("UPDATE tier_items SET season_count = 5, episode_count = 38 "
                               " WHERE match_id = ? AND season IS NULL", (SHOW,)))
        self.choose(key(SHOW), 3, listed=[])
        self.assertEqual(self.facts(season_key(SHOW, 3)), (None, None, None))

    def test_a_named_season_is_called_by_its_name(self):
        self.build_with_seasons()
        body, _, _ = self.choose(key(SHOW), 4, listed=NAMED)
        self.assertEqual(self.facts(season_key(SHOW, 4)), (None, 6, "Night Country"))
        self.assertIn('<span class="ranker-season">— Night Country</span>', body)
        self.choose(season_key(SHOW, 4), "", listed=NAMED)
        self.assertIsNone(self.facts(key(SHOW))[2], "the name goes with the season")

    def test_a_named_season_keeps_its_name_through_removal_and_restore(self):
        self.build_with_seasons()
        self.choose(key(SHOW), 4, listed=NAMED)
        asyncio.run(ranker.remove_items(self.user_id, BOARD, [season_key(SHOW, 4)]))
        removed = asyncio.run(ranker.fetch_removals(self.user_id, BOARD))
        self.assertEqual(removed[0]["title"], "Title 7 — Night Country")
        asyncio.run(ranker.restore_removal(self.user_id, BOARD, season_key(SHOW, 4)))
        self.assertEqual(self.facts(season_key(SHOW, 4)), (None, 6, "Night Country"))


class TheLabelTests(unittest.TestCase):
    """seasons.row_label — the one spelling of what a board row is called, for
    the row, its details heading and the exports."""

    def test_the_whole_show_is_its_title(self):
        self.assertEqual(seasons.row_label("The Bear", None), "The Bear")
        self.assertEqual(seasons.row_label("", None), "Untitled")

    def test_a_numbered_season(self):
        self.assertEqual(seasons.row_label("The Bear", 3), "The Bear — Season 3")

    def test_a_named_season_reads_by_its_name(self):
        self.assertEqual(seasons.row_label("True Detective", 4, "Night Country"),
                         "True Detective — Night Country")

    def test_the_specials(self):
        self.assertEqual(seasons.row_label("Doctor Who", 0), "Doctor Who — Specials")

    def test_the_two_halves_the_row_draws_apart(self):
        self.assertEqual(seasons.season_part(None), "")
        self.assertEqual(seasons.season_part(2), "Season 2")
        self.assertEqual(seasons.season_part(4, "Night Country"), "Night Country")


class TheFactsTests(unittest.TestCase):
    """seasons.season_facts — a row's counts describe what the row is."""

    def test_a_season(self):
        self.assertEqual(seasons.season_facts(NAMED, 4),
                         {"season_count": None, "episode_count": 6,
                          "season_name": "Night Country"})

    def test_the_whole_show_counts_numbered_seasons_only(self):
        listed = [{"season": 0, "episode_count": 3}, *LISTED]
        self.assertEqual(seasons.season_facts(listed, None),
                         {"season_count": 4, "episode_count": 37, "season_name": None})

    def test_nothing_known_is_none_not_zero(self):
        blank = {"season_count": None, "episode_count": None, "season_name": None}
        self.assertEqual(seasons.season_facts([], 3), blank)
        self.assertEqual(seasons.season_facts([], None), blank)


if __name__ == "__main__":
    unittest.main()
