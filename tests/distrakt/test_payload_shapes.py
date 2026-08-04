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
from app.distrakt import lifecycle
from app.distrakt import routes as distrakt_routes
from app.integrations import routes as integrations_routes

EMOJIS = {"HBO": "🟪"}
DEFAULT_EMOJI = "📺"


def _record(trakt_id: int, title: str, *, kind: str, watched: int = 0, total: int = 0,
            season: int = 1, premiere: str = "3/1", finale: str = "3/29",
            abandoned_form: str | None = None) -> dict:
    """A stored month record exactly as `frozen_shows` hands it to the renderers
    — a plain pass-through of `doc["shows"]`, with no computation of its own.

    `kind` IS REQUIRED, on purpose: every record a real month ever holds carries
    one (app/distrakt/store.py's `normalize_show` refuses to write one without
    it), so a fixture built here without a kind would not be standing in for
    anything the store can actually produce. `bucket` is likewise not left for
    discord_fmt to derive — a real closed-month record already carries it,
    written once from `kind` at storage time (store.BUCKET_OF_KIND), and this
    fixture states the same fact the same way rather than leaving it to be
    recomputed."""
    kind = str(kind)
    return {
        "ids": {"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
        "key": f"show:tmdb:{trakt_id}", "season": season, "title": title,
        "network": "HBO", "media": "show", "kind": kind,
        "bucket": str(distrakt.bucket_of_kind(kind)),
        "watched": watched, "total": total,
        "cadence": "weekly", "premiere": premiere, "finale": finale,
        "started_airing": True, "finished_airing": True,
        "abandoned": kind == str(distrakt.RecordKind.ABANDONED),
        "abandoned_form": abandoned_form,
    }


class ClosedMonthPayloadTests(unittest.TestCase):
    """A frozen past month. The snapshot is the record of what that month WAS —
    recomputing it against today's history would rewrite history on every open."""

    def _payload(self, doc, link_url=None):
        # A frozen month is by definition one that is over, and how a month
        # stands decides which sections it may carry (discord_fmt.READER_BUCKETS).
        return distrakt_routes._closed_month_payload(
            doc, "2026-03", EMOJIS, DEFAULT_EMOJI, link_url, distrakt.MonthStanding.PAST)

    def test_it_renders_the_stored_roster_and_says_it_is_closed(self):
        doc = {"month": "2026-03", "closed": True,
               "shows": [_record(1, "Frozen Show", kind="completed", watched=6, total=6)]}
        payload = self._payload(doc)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["closed"])
        # Not read-only: a closed month can still be corrected, it just is not
        # recomputed. The two are different states and the page reads both.
        self.assertFalse(payload["readonly"])
        self.assertEqual([s["title"] for s in payload["shows"]], ["Frozen Show"])
        self.assertIn("Frozen Show", payload["post1"] + payload["post2"])

    def test_it_carries_only_its_verdicts_and_not_the_work_that_was_in_hand(self):
        """A season still in the viewer's hand when a month froze belongs to
        `distrakt_user_seasons`, not to any month record — a closed month's own
        rows are only ever premiere, completed or abandoned kinds, so a keepup
        record simply has no month here to have been carried on. This fixture
        stands in for a record that was, despite that, mistakenly written onto
        the month (a bug, or a pre-migration leftover): it must still not
        render, because `kind` alone decides what a month record IS."""
        doc = {"month": "2026-03", "closed": True, "shows": [
            _record(1, "Was Mid Season", kind="keepup", watched=2, total=8),
            _record(2, "Got Through It", kind="completed", watched=6, total=6),
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

    def test_a_month_that_froze_on_a_disagreement_still_shows_both_numbers(self):
        """The whole point of writing each service's number down: years later,
        with nothing to re-ask, the row can still say the two of them counted
        this season differently instead of quietly picking one."""
        record = _record(1, "Frozen Show", kind="completed", watched=6, total=8)
        record["watched_by_source"] = {"trakt": 6, "simkl": 7}
        payload = self._payload({"month": "2026-03", "closed": True, "shows": [record]})
        self.assertEqual(payload["shows"][0]["counts"], "6/8 (Trakt) · 7/8 (Simkl)")

    def test_a_month_that_recorded_one_number_still_shows_one(self):
        """Every month written before a second service existed, and every month
        an account with one linked service will ever write."""
        payload = self._payload({"month": "2026-03", "closed": True, "shows": [
            _record(1, "Frozen Show", kind="completed", watched=6, total=6)]})
        self.assertEqual(payload["shows"][0]["counts"], "6/6")

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

    FIRST: every premiere record the month holds, whatever became of it since.
    A season that premiered AND was settled in the same month holds TWO records
    on it — its premiere and its verdict — which is the expected shape and not a
    duplicate (see app/distrakt/store.py's module docstring); the first notice
    reads the premiere one and does not care what the verdict record says.
    SECOND: the verdicts the month settled — Completed, Abandoned and its films.

    They were briefly one filtered list shared between both notices, and the
    announcement of a closed month came out as its two finished titles and an
    empty Returning section."""

    def setUp(self):
        self.month_key, _ = _last_month()
        self.doc = {"month": self.month_key, "closed": True, "shows": [
            # Premiered and finished in the same month: two records, one title.
            _record(1, "Saw It Through", kind="series_premiere"),
            _record(1, "Saw It Through", kind="completed", watched=6, total=6),
            # Premiered and was given up on in the same month: two records.
            _record(2, "Turned Away", kind="series_premiere"),
            _record(2, "Turned Away", kind="abandoned", watched=2, total=8,
                    abandoned_form="`Turned Away S01 (2/8)`"),
            # A later season's premiere with no verdict recorded when the month
            # froze — nothing settled it, so only the announcement exists.
            _record(3, "Left Half Done", kind="season_premiere", season=4),
            # A first season's premiere, likewise unsettled.
            _record(4, "Still Going", kind="series_premiere"),
        ]}
        self.payload = distrakt_routes._closed_month_payload(
            self.doc, self.month_key, EMOJIS, DEFAULT_EMOJI, None,
            distrakt.MonthStanding.PAST)

    def test_the_first_notice_announces_every_premiere_the_month_had(self):
        """The regression this reproduces: the announcement was being put
        through the buckets a past month's SECOND notice may present, which
        left it holding only the titles that happened to be Completed or
        Abandoned by now."""
        for title in ("Saw It Through", "Turned Away", "Left Half Done", "Still Going"):
            self.assertIn(title, self.payload["post1"])

    def test_the_first_notice_no_longer_drops_what_was_turned_away(self):
        """Reversed from the old rule on purpose: a premiere record is
        permanent and carries no verdict of its own, so it stays announced
        whatever the SEPARATE verdict record says. Only the viewer's ✕ removes
        a record, and that happens well before either notice is built."""
        self.assertIn("Turned Away", self.payload["post1"])

    def test_the_first_notice_still_separates_new_from_returning(self):
        """The reported symptom included an empty Returning section; a later
        season premiering that month belongs in it."""
        new_block, returning_block = self.payload["post1"].split("**Returning**")
        self.assertIn("Left Half Done", returning_block)
        self.assertNotIn("Left Half Done", new_block)

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
        """The page and the second notice read the same declaration
        (discord_fmt.READER_BUCKETS); the announcement does not, so a title
        only the announcement names is still absent here."""
        self.assertEqual(sorted(s["title"] for s in self.payload["shows"]),
                         ["Saw It Through", "Turned Away"])


class APremiereSettledInItsOwnMonthTests(unittest.TestCase):
    """A season that premiered and was settled in the SAME month holds two
    records on it. That is the shape working as intended — the announcement and
    the verdict are two different statements — but the PAGE must not draw both."""

    def _rows(self, *records) -> list[dict]:
        shape = lifecycle.shape_of(list(records))
        return distrakt_routes._rows_for(shape, distrakt.MonthStanding.CURRENT)

    def _unaired_premiere(self) -> dict:
        """Turned away before a single episode of it aired — the case that showed
        up on the page as both something still to come and something given up."""
        return {**_record(1, "Sterling Point",
                          kind=distrakt.RecordKind.SERIES_PREMIERE),
                "started_airing": False, "finished_airing": False}

    def test_the_verdict_wins_the_page_over_the_announcement(self):
        rows = self._rows(self._unaired_premiere(),
                          _record(1, "Sterling Point",
                                  kind=distrakt.RecordKind.ABANDONED))
        self.assertEqual([r["bucket"] for r in rows], ["abandoned"])

    def test_an_unsettled_premiere_still_shows_as_one_to_come(self):
        rows = self._rows(self._unaired_premiere())
        self.assertEqual([r["bucket"] for r in rows], ["new"])

    def test_another_seasons_verdict_does_not_hide_this_premiere(self):
        """The match is on the season, not the title: a later season being given
        up on says nothing about the one starting."""
        rows = self._rows(self._unaired_premiere(),
                          _record(1, "Sterling Point", season=2,
                                  kind=distrakt.RecordKind.ABANDONED))
        self.assertEqual(sorted(r["bucket"] for r in rows), ["abandoned", "new"])


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
