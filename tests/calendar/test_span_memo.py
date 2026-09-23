"""The short memo over a span's mark-independent half (app/calendar/cache.py).

Assembling a month is the expensive half of a calendar read — measured on real
data, about two seconds for 2,308 items across five windows — and none of it
depends on which titles the reader has marked not-watching, because marks are
applied afterwards. So a redraw that answers ONE mark was paying for the whole
month a second time.

What these tests hold is the part that is easy to get wrong: WHAT MAY BE SERVED
FROM THE MEMO AND WHAT MAY NOT. A cache whose key is missing a dimension does not
fail loudly — it silently hands one reader somebody else's calendar.

No network.
"""
from __future__ import annotations

import unittest
from datetime import date

from app import db
from app.calendar import cache as calendar_cache
from app.calendar import resolve as calendar_resolve
from app.config import Settings
from app.endpoints import get_endpoint


def _key(settings, **over):
    """A memo key for one straightforward show read, with overrides."""
    args = dict(
        tz="UTC", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
        genres="", countries="", show_certifications="", movie_certifications="",
        movie_release_countries="", movie_release_types="", network_filter=None,
        prefs=None,
    )
    args.update(over)
    return calendar_cache._span_memo_key(get_endpoint("shows"), settings, **args)


class KeyCoversEveryNarrowingTests(unittest.TestCase):
    """Two reads may share a memo entry ONLY when nothing that shapes the answer
    differs. Each test changes exactly one thing and requires the key to move."""

    def setUp(self):
        self.settings = Settings()

    def test_the_same_read_twice_is_the_same_key(self):
        self.assertEqual(_key(self.settings), _key(self.settings))

    def test_a_viewers_own_filter_moves_it(self):
        for field in ("genres", "countries", "show_certifications",
                      "movie_certifications", "movie_release_countries",
                      "movie_release_types"):
            with self.subTest(field=field):
                self.assertNotEqual(_key(self.settings),
                                    _key(self.settings, **{field: "drama"}))

    def test_a_viewers_source_selection_moves_it(self):
        # `prefs` decides WHOSE description of a title this reader sees, so two
        # accounts reading one stored window legitimately get different answers.
        self.assertNotEqual(_key(self.settings), _key(self.settings, prefs="simkl-first"))

    def test_the_span_and_the_timezone_move_it(self):
        self.assertNotEqual(_key(self.settings), _key(self.settings, tz="America/New_York"))
        self.assertNotEqual(_key(self.settings), _key(self.settings, start_date=date(2026, 9, 2)))
        self.assertNotEqual(_key(self.settings), _key(self.settings, end_date=date(2026, 9, 29)))

    def test_the_endpoint_moves_it(self):
        a = calendar_cache._span_memo_key(
            get_endpoint("shows"), self.settings, tz="UTC",
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
            genres="", countries="", show_certifications="", movie_certifications="",
            movie_release_countries="", movie_release_types="", network_filter=None,
            prefs=None)
        b = calendar_cache._span_memo_key(
            get_endpoint("movies"), self.settings, tz="UTC",
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
            genres="", countries="", show_certifications="", movie_certifications="",
            movie_release_countries="", movie_release_types="", network_filter=None,
            prefs=None)
        self.assertNotEqual(a, b)

    def test_the_operators_floor_moves_it(self):
        """The instance floor is applied at READ as well as at fill, precisely so
        that changing it reaches windows already stored. A memo that ignored it
        would suspend that promise for the length of its TTL."""
        for field in ("genres", "countries", "show_certifications",
                      "movie_certifications"):
            with self.subTest(field=field):
                tightened = Settings(**{field: "horror"})
                self.assertNotEqual(_key(self.settings), _key(tightened))

    def test_the_operators_network_list_moves_it(self):
        self.assertNotEqual(_key(self.settings), _key(Settings(network_filter=["HBO"])))

    def test_every_settings_field_the_read_uses_is_in_the_key(self):
        """THE LIST CANNOT ROT SILENTLY. `_span_memo_key` names the operator's
        fields one by one, and nothing makes that automatic — so this is the
        test that fails when a read starts consulting a setting the key does not
        carry. If it fails, add the field to the key rather than to this list."""
        used = {"genres", "countries", "show_certifications", "movie_certifications",
                "network_filter"}
        for field in sorted(used):
            with self.subTest(field=field):
                value = ["HBO"] if field == "network_filter" else "horror"
                self.assertNotEqual(
                    _key(Settings()), _key(Settings(**{field: value})),
                    f"{field} shapes a read but does not move the memo key")

    def test_the_instance_source_admission_is_in_the_key(self):
        """Which services this instance puts on its calendar is the operator's
        answer and narrows the shared rows, so it belongs in the key too."""
        base = Settings()
        admitted = tuple(sorted(calendar_resolve.instance_sources(base) or ()))
        # Nested inside the operator's half of the key, so look through it rather
        # than at the top level: what matters is that the value is CARRIED, not
        # where it sits.
        flat = repr(_key(base))
        self.assertIn(repr(admitted), flat)


class WhichDatabaseTests(unittest.TestCase):
    def test_pointing_at_another_database_moves_the_key(self):
        """The memo is per PROCESS and the rows are per FILE. The suite swaps
        databases between tests, which is exactly where a memo that could not
        tell them apart served one test's calendar to the next."""
        from tests.support import new_db_path

        before = _key(Settings())
        new_db_path("spanmemo")
        self.assertNotEqual(before, _key(Settings()))
        db.close_thread_connection()


class TheTtlIsBoundedByStalenessTests(unittest.TestCase):
    def test_the_ttl_stays_well_under_the_shortest_staleness_tier(self):
        """THE WHOLE REASON A PAGE LOAD MAY BE SERVED FROM THE MEMO.

        An unexpired entry was written either by a read that found nothing due
        for a refresh, or by one that refreshed, stored (clearing the memo) and
        then wrote the post-fill answer. Nothing was due when it was written, and
        nothing can BECOME due inside the TTL — but only while the TTL is shorter
        than the soonest a span can go stale. Raise it past that and a page load
        starts serving a month it should have refetched, silently.
        """
        shortest = min(seconds for _, seconds in calendar_cache._STALENESS_TIERS)
        self.assertLess(calendar_cache._SPAN_MEMO_TTL, shortest)


class AFillDoesNotCostTheReaderTheirMemoTests(unittest.TestCase):
    """THE SHAPE THAT MADE THIS USELESS ON A LIVE INSTANCE, and invisible in
    development.

    A live calendar WRITES while it reads: a month opened on stale windows fills
    them, one window at a time, as its day blocks arrive. The first version of
    this memo threw ALL of it away on any window write, so the mark a reader made
    a second after the page finished drawing paid the full cost of rebuilding a
    month that had just been built. It looked perfect in development only because
    nothing there can reach a provider, so nothing is ever written.

    The rule now: a read that may FILL must be current, because it is the one
    that went and got the new rows. A read that may not fill is redrawing what is
    already on screen, and the page it is redrawing came from those same rows.
    """

    def setUp(self):
        calendar_cache._forget_spans()

    def tearDown(self):
        calendar_cache._forget_spans()

    def test_a_write_retires_only_its_own_endpoint(self):
        calendar_cache._forget_spans("shows")
        self.assertEqual(calendar_cache._endpoint_generation.get("shows"), 1)
        self.assertIsNone(calendar_cache._endpoint_generation.get("movies"))

    def test_a_write_to_one_endpoint_leaves_anothers_spans_alone(self):
        """The collateral that made a films page slow because a shows window
        happened to refill."""
        calendar_cache._forget_spans("shows")
        calendar_cache._forget_spans("shows")
        self.assertEqual(calendar_cache._endpoint_generation.get("shows"), 2)
        self.assertEqual(calendar_cache._endpoint_generation.get("movies", 0), 0)

    def test_nothing_is_dropped_outright_by_a_write(self):
        """Entries are RETIRED by the generation travelling with them, not
        deleted — which is what lets a redraw still be answered from the span the
        page was drawn from while a fill is in flight."""
        calendar_cache._span_memo[("k",)] = (("k",), 0.0, 0, [], None, False, 0)
        calendar_cache._forget_spans("shows")
        self.assertIn(("k",), calendar_cache._span_memo,
                      "a write must not empty the memo a redraw still needs")

    def test_forgetting_everything_still_works_for_a_caller_that_cannot_say(self):
        calendar_cache._span_memo[("k",)] = (("k",), 0.0, 0, [], None, False, 0)
        calendar_cache._forget_spans()
        self.assertEqual(calendar_cache._span_memo, {})
        self.assertEqual(calendar_cache._endpoint_generation, {})


class TheAgesAreBoundedTests(unittest.TestCase):
    """The two clocks a served span lives by.

    `_SPAN_SOFT_AGE` is when a read still gets what is here and a rebuild starts
    behind it; `_SPAN_MEMO_TTL` is the safety rail past which nobody is served at
    all, which is only reached when refreshes have been failing. Both have to
    stay under the soonest the ROWS were due to be refetched, or serving from
    here would outlast the data it was built from.
    """

    def test_the_soft_age_comes_before_the_hard_max(self):
        self.assertLess(calendar_cache._SPAN_SOFT_AGE, calendar_cache._SPAN_MEMO_TTL)

    def test_both_stay_under_the_shortest_staleness_tier(self):
        shortest = min(seconds for _, seconds in calendar_cache._STALENESS_TIERS)
        self.assertLess(calendar_cache._SPAN_MEMO_TTL, shortest)
        self.assertLess(calendar_cache._SPAN_SOFT_AGE, shortest)

    def test_only_so_many_rebuilds_run_at_once(self):
        """A rebuild is seconds of pure Python, which the GIL keeps on the one
        core the event loop needs — so an unbounded number of them would become
        the thing readers wait behind."""
        self.assertGreaterEqual(calendar_cache._SPAN_REFRESH_LIMIT, 1)
        self.assertLessEqual(calendar_cache._SPAN_REFRESH_LIMIT, 4)


class TheRefresherDoesNotAnswerItselfTests(unittest.TestCase):
    """THE DEFECT THAT MADE SERVE-THEN-REBUILD A NO-OP, and it was silent.

    The background job rebuilds by calling `_assembled_span` again. Without
    `rebuild=True` it asks exactly the question its own reader just asked, is
    handed the very entry it was scheduled to replace, and returns having done
    nothing — so the span never moves, and every read forever schedules another
    refresh that also does nothing. Everything looks fast, because it is; it is
    just serving the same stale month for the rest of the process's life.

    Caught by watching the entry's generation stay at 0 while the endpoint's went
    to 1, which is the only place the failure is visible from.
    """

    def test_a_rebuild_ignores_what_is_already_there(self):
        import inspect
        src = inspect.getsource(calendar_cache._assembled_span)
        self.assertIn("None if rebuild else _span_memo.get(key)", src,
                      "a rebuild that reads the memo can be answered by the "
                      "entry it exists to replace")

    def test_the_refresher_asks_for_a_rebuild(self):
        import inspect
        src = inspect.getsource(calendar_cache._refresh_span_later)
        self.assertIn("rebuild=True", src)

    def test_one_refresh_per_key_and_a_cap_across_the_process(self):
        """Every read of a stale span would otherwise start its own rebuild of
        the same month — a busy page is thirty requests — all doing identical
        work on a loop none of them left alone."""
        import inspect
        src = inspect.getsource(calendar_cache._refresh_span_later)
        self.assertIn("key in _span_refreshing", src)
        self.assertIn("_SPAN_REFRESH_LIMIT", src)


class ForgettingTests(unittest.TestCase):
    def test_forgetting_empties_the_memo(self):
        calendar_cache._span_memo[("a",)] = (("a",), 0.0, 0, [], None, False, 0)
        calendar_cache._forget_spans()
        self.assertEqual(calendar_cache._span_memo, {})

    def test_the_cap_is_bounded(self):
        """The key carries a viewer's own filters, so a caller asking for a
        hundred months must not be able to hold them all."""
        self.assertLessEqual(calendar_cache._SPAN_MEMO_MAX, 256)
        self.assertGreater(calendar_cache._SPAN_MEMO_MAX, 0)


if __name__ == "__main__":
    unittest.main()
