"""The five answers app/calendar/routes.py builds the calendar shell out of.

Each of these used to be inline in one 200-line route and could only be
exercised by loading a whole month through a TestClient. They are unit-tested
here because the interesting cases — a filter narrowing the month, a day whose
every card is hidden, a Trakt failure that arrives AFTER the cards did — are
awkward to stage through a request and trivial to state directly.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.calendar import routes as calendar_routes
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Item, Media, Source
from app.providers.trakt import TraktError

UTC = ZoneInfo("UTC")
ENDPOINT = get_endpoint("shows")
USER = SimpleNamespace(user_id=1, timezone="UTC", is_admin=False)

NO_PREFS = {
    "endpoint": None, "card_style": None, "day_packing": None,
    "hide_not_watching": False, "network_filter": [], "genres": "", "countries": "",
    "show_certifications": "", "movie_certifications": "",
}


def _item(item_id: str, title: str = "A Show") -> Item:
    """One card, as a real Item rather than a dict shaped like one — so a renamed
    field fails in this double instead of passing here and failing in production."""
    return Item(
        source=Source.TRAKT, media=Media.SHOW, id=item_id,
        ids={"trakt": 1, "slug": item_id},
        detail_url=f"https://trakt.tv/shows/{item_id}",
        air_date="2026-07-01", air_ts=0.0, air_display="01 Jul 2026",
        air_time="20:00", day_of_week="Wednesday", title=title,
    )


def _day(date_iso: str, *item_ids: str) -> dict:
    return {"date": date_iso, "items": [_item(i) for i in item_ids]}


def _meta(total: int, watching: int = 0, not_watching: int = 0,
          partial: bool = False, show_ids=()) -> dict:
    return {"total": total, "watching": watching, "not_watching": not_watching,
            "partial": partial, "show_ids": list(show_ids)}


class AssembleMonthTests(unittest.TestCase):
    """assemble_month — the month's cards plus every number stated about them."""

    def _run(self, settings=None, prefs=None, not_watching=frozenset()):
        return asyncio.run(calendar_routes.assemble_month(
            USER, settings or Settings(trakt_client_id="cid", trakt_access_token="tok"),
            prefs or NO_PREFS, ENDPOINT, UTC, 2026, 7, set(not_watching)))

    def test_no_configured_source_reports_it_and_assembles_nothing(self):
        """The one path that must not touch the cache at all — there is nobody to
        ask, so it says so instead of failing a fetch."""
        with patch("app.calendar.cache.assemble_range") as fetch:
            assembly = self._run(settings=Settings())
        fetch.assert_not_called()
        self.assertEqual(assembly.error, calendar_routes.NOT_CONFIGURED)
        self.assertEqual(assembly.grouped, [])
        self.assertEqual(assembly.total, 0)
        self.assertEqual(assembly.delta, {"text": "", "kind": "none"})

    def test_it_carries_the_months_numbers_off_the_assembly(self):
        async def fake_range(*a, **kw):
            return [_day("2026-07-01", "a", "b"), _day("2026-07-02", "a")], \
                _meta(3, watching=2, not_watching=1, show_ids=["a", "b"])

        async def fake_view(*a, **kw):
            return {"new_ids": {"b"}, "delta": {"text": "+1", "kind": "up"},
                    "history": [{"when": "now"}]}

        with patch("app.calendar.cache.assemble_range", fake_range), \
             patch("app.calendar.state.resolve_view", fake_view):
            assembly = self._run()

        self.assertIsNone(assembly.error)
        self.assertEqual(assembly.total, 3)
        self.assertEqual((assembly.watching, assembly.not_watching_count), (2, 1))
        self.assertEqual(assembly.new_ids, {"b"})
        self.assertEqual(assembly.delta["kind"], "up")
        self.assertEqual(assembly.history, [{"when": "now"}])
        # Counted over the whole month, not per day: "a" airs twice.
        self.assertEqual(dict(assembly.show_counts), {"a": 2, "b": 1})

    def test_a_partial_month_is_flagged_without_being_an_error(self):
        async def fake_range(*a, **kw):
            return [_day("2026-07-01", "a")], _meta(1, partial=True, show_ids=["a"])

        async def fake_view(*a, **kw):
            return {"new_ids": set(), "delta": {"text": "", "kind": "none"}, "history": []}

        with patch("app.calendar.cache.assemble_range", fake_range), \
             patch("app.calendar.state.resolve_view", fake_view):
            assembly = self._run()
        self.assertTrue(assembly.partial)
        self.assertIsNone(assembly.error)
        self.assertEqual(assembly.total, 1)

    def test_a_failed_fetch_leaves_an_empty_month_and_an_error(self):
        async def boom(*a, **kw):
            raise TraktError("Trakt is unreachable")

        with patch("app.calendar.cache.assemble_range", boom):
            assembly = self._run()
        self.assertEqual(assembly.error, "Trakt is unreachable")
        self.assertEqual(assembly.grouped, [])
        self.assertEqual(assembly.total, 0)

    def test_cards_survive_a_failure_that_happens_after_they_were_assembled(self):
        """A month that read fine and then failed while working out what changed
        since the last visit still has real cards, and showing them with the
        warning beats throwing a rendered month away."""
        async def fake_range(*a, **kw):
            return [_day("2026-07-01", "a")], _meta(1, watching=1, show_ids=["a"])

        async def boom(*a, **kw):
            raise TraktError("rate limited")

        with patch("app.calendar.cache.assemble_range", fake_range), \
             patch("app.calendar.state.resolve_view", boom):
            assembly = self._run()
        self.assertEqual(assembly.error, "rate limited")
        self.assertEqual(len(assembly.grouped), 1)
        self.assertEqual(assembly.total, 1)
        # The diff was never resolved, so nothing is claimed to be new.
        self.assertEqual(assembly.new_ids, set())


class ViewPreferencesTests(unittest.TestCase):
    def test_the_users_choice_wins_over_the_app_wide_default(self):
        prefs = {**NO_PREFS, "card_style": "poster", "day_packing": "packed"}
        view = calendar_routes._view_preferences(
            prefs, Settings(card_style="vertical", day_packing="stacked"))
        self.assertEqual(view["card_style"], "poster")
        self.assertEqual(view["day_packing"], "packed")

    def test_an_unset_choice_falls_back_to_the_app_wide_default(self):
        view = calendar_routes._view_preferences(
            NO_PREFS, Settings(card_style="horizontal", day_packing="packed"))
        self.assertEqual(view["card_style"], "horizontal")
        self.assertEqual(view["day_packing"], "packed")

    def test_filters_are_reported_as_active_and_named_by_dimension(self):
        view = calendar_routes._view_preferences(
            {**NO_PREFS, "genres": "drama", "network_filter": ["HBO"]}, Settings())
        self.assertTrue(view["filters_active"])
        self.assertEqual(view["filters_summary"], "genre, network")

    def test_both_certification_specs_collapse_to_one_label(self):
        """The tooltip names the DIMENSION, and the endpoint decides which of the
        two specs is in play — so "certification" appears once, never twice."""
        for key in ("show_certifications", "movie_certifications"):
            with self.subTest(key=key):
                view = calendar_routes._view_preferences({**NO_PREFS, key: "TV-MA"}, Settings())
                self.assertEqual(view["filters_summary"], "certification")

    def test_no_filters_reports_inactive_with_an_empty_summary(self):
        view = calendar_routes._view_preferences(NO_PREFS, Settings())
        self.assertFalse(view["filters_active"])
        self.assertEqual(view["filters_summary"], "")


class ViewDataTests(unittest.TestCase):
    def test_it_mirrors_the_assembly_for_the_client(self):
        assembly = calendar_routes.MonthAssembly(
            new_ids={"b", "a"}, show_counts={"a": 2, "b": 1},
            watching=2, not_watching_count=1)
        data = calendar_routes._view_data(assembly, {"a"})
        self.assertEqual(data["newIds"], ["a", "b"])  # sorted, so the JSON is stable
        self.assertEqual(data["showCounts"], {"a": 2, "b": 1})
        self.assertEqual(data["watching"], 2)
        self.assertEqual(data["notWatchingCount"], 1)

    def test_marks_for_shows_not_in_this_month_are_left_out(self):
        """A viewer's marks are their whole set, but the tiles only count this
        month — so a mark on something that does not air here must not be sent."""
        assembly = calendar_routes.MonthAssembly(show_counts={"a": 1})
        data = calendar_routes._view_data(assembly, {"a", "elsewhere"})
        self.assertEqual(data["notWatching"], ["a"])


class DayChipTests(unittest.TestCase):
    def test_every_day_of_the_month_gets_a_chip_even_with_nothing_on_it(self):
        assembly = calendar_routes.MonthAssembly(grouped=[_day("2026-07-02", "a")])
        chips = calendar_routes._day_chips(assembly, 2026, 7, 31, set(), False)
        self.assertEqual(len(chips), 31)
        self.assertEqual(chips[0], {"day": 1, "date": "2026-07-01", "count": 0, "shown": 0})
        self.assertEqual(chips[1], {"day": 2, "date": "2026-07-02", "count": 1, "shown": 1})

    def test_without_hiding_shown_equals_count_even_for_marked_shows(self):
        assembly = calendar_routes.MonthAssembly(grouped=[_day("2026-07-01", "a", "b")])
        chips = calendar_routes._day_chips(assembly, 2026, 7, 31, {"a"}, False)
        self.assertEqual((chips[0]["count"], chips[0]["shown"]), (2, 2))

    def test_with_hiding_a_fully_marked_day_reports_nothing_to_scroll_to(self):
        """The chip has to render inert, and it can only know to if `shown` is 0
        while `count` still says the day is not actually empty."""
        assembly = calendar_routes.MonthAssembly(
            grouped=[_day("2026-07-01", "a", "b"), _day("2026-07-02", "c")])
        chips = calendar_routes._day_chips(assembly, 2026, 7, 31, {"a", "b"}, True)
        self.assertEqual((chips[0]["count"], chips[0]["shown"]), (2, 0))
        self.assertEqual((chips[1]["count"], chips[1]["shown"]), (1, 1))


class DayLayoutTests(unittest.TestCase):
    """_apply_day_layout — shared with /calendar/day so a day fetched late is
    laid out exactly like one the shell shipped inline."""

    def test_columns_are_capped_per_card_style(self):
        for style, cap in (("poster", 6), ("horizontal", 2), ("vertical", 5)):
            with self.subTest(style=style):
                grouped = [_day("2026-07-01", *(f"s{n}" for n in range(10)))]
                calendar_routes._apply_day_layout(
                    grouped, not_watching=set(), hide_not_watching=False, card_style=style)
                self.assertEqual(grouped[0]["cols"], cap)

    def test_a_thin_day_uses_only_the_columns_it_needs(self):
        grouped = [_day("2026-07-01", "a", "b")]
        calendar_routes._apply_day_layout(
            grouped, not_watching=set(), hide_not_watching=False, card_style="vertical")
        self.assertEqual(grouped[0]["cols"], 2)
        self.assertEqual(grouped[0]["rows"], 1)

    def test_rows_reserve_height_for_a_day_not_yet_fetched(self):
        """A placeholder can only hold the right amount of space — so the
        scrollbar and the jump-to strip land where the day really is — if the
        server says how many rows of cards are coming."""
        grouped = [_day("2026-07-01", *(f"s{n}" for n in range(7)))]
        calendar_routes._apply_day_layout(
            grouped, not_watching=set(), hide_not_watching=False, card_style="horizontal")
        self.assertEqual(grouped[0]["cols"], 2)
        self.assertEqual(grouped[0]["rows"], 4)  # 7 cards over 2 columns

    def test_an_empty_day_still_reserves_one_row(self):
        grouped = [{"date": "2026-07-01", "items": []}]
        calendar_routes._apply_day_layout(
            grouped, not_watching=set(), hide_not_watching=False, card_style="vertical")
        self.assertEqual(grouped[0]["cols"], 1)
        self.assertEqual(grouped[0]["rows"], 1)

    def test_a_day_collapses_only_when_hiding_and_nothing_is_visible(self):
        grouped = [_day("2026-07-01", "a"), _day("2026-07-02", "b")]
        calendar_routes._apply_day_layout(
            grouped, not_watching={"a"}, hide_not_watching=True, card_style="vertical")
        self.assertTrue(grouped[0]["collapsed"])
        self.assertEqual(grouped[0]["visible"], 0)
        self.assertFalse(grouped[1]["collapsed"])

    def test_marked_items_do_not_collapse_a_day_when_hiding_is_off(self):
        grouped = [_day("2026-07-01", "a")]
        calendar_routes._apply_day_layout(
            grouped, not_watching={"a"}, hide_not_watching=False, card_style="vertical")
        self.assertFalse(grouped[0]["collapsed"])
        # Still reported, because the client needs it to keep the tiles honest.
        self.assertEqual(grouped[0]["visible"], 0)


if __name__ == "__main__":
    unittest.main()
