"""The tracker month payloads that are now separately reachable.

app/distrakt_routes.py builds a month in one of three shapes — a frozen past
month from its own snapshot, an open month computed live, or last-known totals
plus a notice when Trakt cannot be reached. Only the live path needs Trakt, and
only the live path was reachable in a test before; the frozen and empty renders
are pure functions of a stored document and are pinned directly here.

Also covers app/integrations_routes.py's library cache, whose TTL and
invalidation were an in-place dict poke from another module until the module
grew a verb for it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TRAKT_DATA_DIR", tempfile.mkdtemp(prefix="tns-payload-"))

from app import distrakt_routes, integrations_routes  # noqa: E402

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
        return distrakt_routes._closed_month_payload(
            doc, "2026-03", EMOJIS, DEFAULT_EMOJI, link_url)

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


class EmptyMonthPayloadTests(unittest.TestCase):
    """A month with no roster and no Trakt call at all."""

    def test_a_never_tracked_past_month_is_read_only(self):
        """The tracker only rolls a month forward, never backfills one after the
        fact, so an old month nobody was tracking stays empty AND uneditable —
        `readonly` is what hides the add affordances."""
        payload = distrakt_routes._empty_month_payload(
            "2024-01", EMOJIS, DEFAULT_EMOJI, readonly=True)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readonly"])
        self.assertEqual(payload["shows"], [])
        self.assertEqual(payload["movies"], [])

    def test_an_unconfigured_current_month_is_empty_but_still_editable(self):
        payload = distrakt_routes._empty_month_payload("2026-07", EMOJIS, DEFAULT_EMOJI)
        self.assertFalse(payload["readonly"])
        self.assertFalse(payload["closed"])


class MonthKeyTests(unittest.TestCase):
    def test_it_zero_pads_so_month_keys_sort_and_compare_as_strings(self):
        """Several routes compare month keys with `>=` to tell a past month from
        the current one, which only works while every key is the same width."""
        self.assertEqual(distrakt_routes._month_key(2026, 3), "2026-03")
        self.assertEqual(distrakt_routes._month_key(2026, 12), "2026-12")
        self.assertLess(distrakt_routes._month_key(2026, 3),
                        distrakt_routes._month_key(2026, 12))


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
