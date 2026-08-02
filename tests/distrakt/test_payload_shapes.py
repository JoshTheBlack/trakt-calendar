"""The tracker month payloads that are now separately reachable.

app/distrakt/routes.py builds a month in one of three shapes — a frozen past
month from its own snapshot, an open month computed live, or last-known totals
plus a notice when Trakt cannot be reached. Only the live path needs Trakt, and
only the live path was reachable in a test before; the frozen and empty renders
are pure functions of a stored document and are pinned directly here.

Also covers app/integrations/routes.py's library cache, whose TTL and
invalidation were an in-place dict poke from another module until the module
grew a verb for it.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from app import distrakt
from app.distrakt import routes as distrakt_routes
from app.integrations import routes as integrations_routes

EMOJIS = {"HBO": "🟪"}
DEFAULT_EMOJI = "📺"


def _record(trakt_id: int, title: str, *, watched: int, total: int,
            bucket: str = "watching") -> dict:
    """A stored record as a frozen month holds it: the counts and dates were
    persisted when the month closed, which is what lets it render with no Trakt."""
    return {
        "ids": {"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
        "key": f"show:tmdb:{trakt_id}", "season": 1, "title": title,
        "network": "HBO", "media": "show", "watched": watched, "total": total,
        "cadence": "weekly", "premiere": "2026-03-01", "finale": "2026-03-29",
        "started_airing": True, "finished_airing": True, "bucket": bucket,
        "abandoned": False, "abandoned_form": None,
    }


class ClosedMonthPayloadTests(unittest.TestCase):
    """A frozen past month. The snapshot is the record of what that month WAS —
    recomputing it against today's history would rewrite history on every open."""

    def _payload(self, doc, link_url=None):
        # A frozen month is by definition one that is over, and how a month
        # stands decides which sections it may carry (discord_fmt.MONTH_BUCKETS).
        return distrakt_routes._closed_month_payload(
            doc, "2026-03", EMOJIS, DEFAULT_EMOJI, link_url, distrakt.MonthStanding.PAST)

    def test_it_renders_the_stored_roster_and_says_it_is_closed(self):
        doc = {"month": "2026-03", "closed": True,
               "shows": [_record(1, "Frozen Show", watched=6, total=6, bucket="completed")]}
        payload = self._payload(doc)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["closed"])
        # Not read-only: a closed month can still be corrected, it just is not
        # recomputed. The two are different states and the page reads both.
        self.assertFalse(payload["readonly"])
        self.assertEqual([s["title"] for s in payload["shows"]], ["Frozen Show"])
        self.assertIn("Frozen Show", payload["post1"] + payload["post2"])

    def test_it_carries_only_its_verdicts_and_not_the_work_that_was_in_hand(self):
        """A month that is over settled its Cleanup and its Keepup when it froze.
        Re-listing what was still mid-flight on the last day reads as work
        outstanding on a month nobody can do anything about."""
        doc = {"month": "2026-03", "closed": True, "shows": [
            {**_record(1, "Was Mid Season", watched=2, total=8), "finished_airing": False},
            _record(2, "Got Through It", watched=6, total=6),
        ]}
        payload = self._payload(doc)
        self.assertEqual([s["title"] for s in payload["shows"]], ["Got Through It"])
        self.assertNotIn("Cleanup", payload["post2"])
        self.assertNotIn("Keepup", payload["post2"])
        self.assertIn("**Completed**", payload["post2"])

    def test_the_months_films_travel_with_it_rather_than_only_inside_post_2(self):
        doc = {"month": "2026-03", "closed": True, "shows": [],
               "movies": [{"key": "movie:tmdb:5", "title": "A Film", "year": 2011,
                           "watched_at": "2026-03-09T12:00:00Z"}]}
        payload = self._payload(doc)
        self.assertEqual([m["title"] for m in payload["movies"]], ["A Film"])
        self.assertIn("A Film", payload["post2"])

    def test_a_month_with_no_films_reports_an_empty_list_not_none(self):
        payload = self._payload({"month": "2026-03", "closed": True, "shows": []})
        self.assertEqual(payload["movies"], [])

    def test_the_announcement_link_is_embedded_when_there_is_one(self):
        doc = {"month": "2026-03", "closed": True, "shows": []}
        with_link = self._payload(doc, link_url="https://example.test/c/abc")
        self.assertIn("https://example.test/c/abc", with_link["post1"])
        self.assertNotIn("https://example.test/c/abc", self._payload(doc)["post1"])

    def test_it_never_reports_a_degraded_read(self):
        """`notice` and `rate_limited` belong to the stale fallback alone — the
        client shows its banner off `notice`, so a frozen month must not carry
        one or every past month would look like a failed load."""
        payload = self._payload({"month": "2026-03", "closed": True, "shows": []})
        self.assertNotIn("notice", payload)
        self.assertNotIn("rate_limited", payload)


def _last_month(today: date | None = None) -> tuple[str, int]:
    """(month key, month number) for a month that is certainly over.

    Derived from the clock rather than written down: a fixed year-month in a test
    about past/current/future stops meaning what it was chosen to mean the moment
    the calendar walks past it, and this suite has been bitten by exactly that."""
    first_of_this_month = (today or date.today()).replace(day=1)
    previous = first_of_this_month - timedelta(days=1)
    return f"{previous.year:04d}-{previous.month:02d}", previous.month


class ClosedMonthNoticesAnswerDifferentQuestionsTests(unittest.TestCase):
    """The two notices of a month that is over are NOT two views of one list.

    FIRST: everything that premiered in that month and was not turned away,
    whatever became of it since — it belongs because it started then, so a title
    sitting in Cleanup or Keepup is still one of the month's premieres.
    SECOND: the verdicts the month settled — Completed, Abandoned and its films.

    They were briefly the same list, and the announcement of a closed month came
    out as its two finished titles and an empty Returning section."""

    def setUp(self):
        self.month_key, month = _last_month()
        self.premiered_that_month = f"{month}/8"
        # Every row below premiered in the closed month except the carryover.
        self.doc = {"month": self.month_key, "closed": True, "shows": [
            {**_record(1, "Saw It Through", watched=6, total=6, bucket="completed"),
             "premiere": self.premiered_that_month},
            {**_record(2, "Turned Away", watched=2, total=8, bucket="abandoned"),
             "premiere": self.premiered_that_month, "abandoned": True,
             "abandoned_form": "`Turned Away S01 (2/8)`"},
            # Finale aired, not finished: a Cleanup row when the month froze.
            {**_record(3, "Left Half Done", watched=2, total=8),
             "premiere": self.premiered_that_month, "season": 4},
            # Still airing on the last day: a Keepup row when the month froze.
            {**_record(4, "Still Going", watched=2, total=8),
             "premiere": self.premiered_that_month, "finished_airing": False,
             "cadence": "Sun"},
            # Premiered the month BEFORE and merely carried on into this one.
            {**_record(5, "Started Earlier", watched=3, total=8),
             "premiere": f"{(month - 2) % 12 + 1}/8"},
        ]}
        self.payload = distrakt_routes._closed_month_payload(
            self.doc, self.month_key, EMOJIS, DEFAULT_EMOJI, None,
            distrakt.MonthStanding.PAST)

    def test_the_first_notice_announces_every_premiere_the_month_had(self):
        """The regression: the announcement was being put through the buckets a
        past month's SECOND notice may present, which left it holding only the
        titles that happened to be Completed or Abandoned by now."""
        for title in ("Saw It Through", "Left Half Done", "Still Going"):
            self.assertIn(title, self.payload["post1"])

    def test_the_first_notice_still_drops_what_was_turned_away(self):
        """The one thing that DOES remove a premiere from the announcement, and
        it is not a bucket rule: it was explicitly turned away."""
        self.assertNotIn("Turned Away", self.payload["post1"])

    def test_the_first_notice_still_separates_new_from_returning(self):
        """The reported symptom included an empty Returning section; a later
        season premiering that month belongs in it."""
        new_block, returning_block = self.payload["post1"].split("**Returning**")
        self.assertIn("Left Half Done", returning_block)
        self.assertNotIn("Left Half Done", new_block)

    def test_the_first_notice_still_leaves_out_what_did_not_premiere_then(self):
        """Membership is by premiere date, which the repair does not loosen."""
        self.assertNotIn("Started Earlier", self.payload["post1"])

    def test_the_second_notice_keeps_only_the_verdicts_the_month_settled(self):
        """Unchanged by the repair, and asserted beside the first notice so the
        difference between the two is documented where both can be seen."""
        post2 = self.payload["post2"]
        self.assertIn("**Completed**", post2)
        self.assertIn("Saw It Through", post2)
        self.assertIn("**Abandoned**", post2)
        self.assertIn("Turned Away", post2)
        for absent in ("Cleanup", "Keepup", "New Shows", "Returning",
                       "Left Half Done", "Still Going"):
            self.assertNotIn(absent, post2)

    def test_the_pages_row_list_follows_the_second_notice_not_the_first(self):
        """The page and the second notice read one table; the announcement does
        not, so a title only the announcement names is still absent here."""
        self.assertEqual(sorted(s["title"] for s in self.payload["shows"]),
                         ["Saw It Through", "Turned Away"])


class EmptyMonthPayloadTests(unittest.TestCase):
    """A month with no roster and no Trakt call at all."""

    def test_a_never_tracked_past_month_is_read_only(self):
        """The tracker only rolls a month forward, never backfills one after the
        fact, so an old month nobody was tracking stays empty AND uneditable —
        `readonly` is what hides the add affordances."""
        payload = distrakt_routes._empty_month_payload(
            "2024-01", EMOJIS, DEFAULT_EMOJI, distrakt.MonthStanding.PAST, readonly=True)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual(payload["shows"], [])
        self.assertEqual(payload["movies"], [])

    def test_an_unconfigured_current_month_is_empty_but_still_editable(self):
        payload = distrakt_routes._empty_month_payload(
            "2026-07", EMOJIS, DEFAULT_EMOJI, distrakt.MonthStanding.CURRENT)
        self.assertFalse(payload["readonly"])
        self.assertFalse(payload["closed"])


class MonthKeyTests(unittest.TestCase):
    def test_it_zero_pads_so_month_keys_sort_and_compare_as_strings(self):
        """Several routes compare month keys with `>=` to tell a past month from
        the current one, which only works while every key is the same width."""
        self.assertEqual(distrakt.month_key(2026, 3), "2026-03")
        self.assertEqual(distrakt.month_key(2026, 12), "2026-12")
        self.assertLess(distrakt.month_key(2026, 3), distrakt.month_key(2026, 12))


class LibraryCacheTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(integrations_routes.LIBRARY_CACHE)

    def tearDown(self):
        integrations_routes.LIBRARY_CACHE.clear()
        integrations_routes.LIBRARY_CACHE.update(self._saved)

    def test_invalidating_forces_the_next_read_to_re_pull(self):
        """Credentials just changed, so the held ids came from a library this
        instance may not even be able to reach any more."""
        integrations_routes.LIBRARY_CACHE["_ts"] = 1e12  # far in the future
        integrations_routes.invalidate_library_cache()
        self.assertEqual(integrations_routes.LIBRARY_CACHE["_ts"], 0.0)

    def test_invalidating_keeps_the_ids_until_something_replaces_them(self):
        """Only the timestamp is cleared. Emptying the lists as well would make
        every add button un-mark itself between the save and the next fetch."""
        integrations_routes.LIBRARY_CACHE["sonarr"] = [1, 2, 3]
        integrations_routes.invalidate_library_cache()
        self.assertEqual(integrations_routes.LIBRARY_CACHE["sonarr"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
