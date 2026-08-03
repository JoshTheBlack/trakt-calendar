"""Unit tests for app/distrakt/discord_fmt.py.

Pure/offline — no network, no persistence. The render_post1/render_post2 tests
hand-verify against a real sample month's posts, with two corrections applied:
  - the sample's old "(x/?)" totals are superseded — real totals are invented
    here for the three shows that had them (TRINITY, President Curtis,
    Last Week Tonight), since discord_fmt always renders a live numeric total,
    never "?".
  - "Young Sherlock (1/8)" (no season tag) is a seasonless edge case the sample
    doesn't otherwise exercise; it is rendered here with "S01" like every other
    show, matching discord_fmt's normal behavior rather than carrying the
    sample's omission forward.
"""
from __future__ import annotations

import unittest

from app.distrakt import discord_fmt as fmt

_NEXT_ID = [1000]


def show(title, season, network="Net", watched=0, total=0, cadence=None,
         premiere=None, finale=None, started=False, finished=False,
         abandoned=False, abandoned_form=None):
    """A LIVE SHOW SHAPE dict, the same flat shape live.py merges before anything
    has written a stored record for it.

    `bucket` is computed the same way live.py computes it — via `bucket_of` on
    the merged fields — because every render_post2/`_group_by_bucket` caller in
    production reads a STORED record's `bucket`, which was decided once from its
    `kind` (store.BUCKET_OF_KIND) rather than re-derived at render time. Doing
    the same here is what makes a fixture built by this helper look like what
    the renderers actually receive, instead of the raw live merge they never
    see directly.
    """
    _NEXT_ID[0] += 1
    rec = {
        "trakt_id": _NEXT_ID[0], "title": title, "season": season, "network": network,
        "watched": watched, "total": total, "cadence": cadence,
        "premiere": premiere, "finale": finale,
        "started_airing": started, "finished_airing": finished,
        "abandoned": abandoned, "abandoned_form": abandoned_form,
    }
    rec["bucket"] = str(fmt.bucket_of(rec, rec))
    return rec


class BucketOfTests(unittest.TestCase):
    def test_new_season_one_not_started(self):
        s = show("Foo", 1, total=8, started=False)
        self.assertEqual(fmt.bucket_of(s, s), "new")

    def test_returning_season_two_plus_not_started(self):
        s = show("Foo", 3, total=8, started=False)
        self.assertEqual(fmt.bucket_of(s, s), "returning")

    def test_binge_started_goes_straight_to_cleanup(self):
        s = show("Foo", 1, watched=1, total=8, cadence="b", started=True, finished=False)
        self.assertEqual(fmt.bucket_of(s, s), "cleanup")

    def test_weekly_started_not_finished_is_keepup(self):
        s = show("Foo", 1, watched=2, total=8, cadence="Sun", started=True, finished=False)
        self.assertEqual(fmt.bucket_of(s, s), "keepup")

    def test_weekly_finale_aired_is_cleanup(self):
        s = show("Foo", 1, watched=5, total=8, cadence="Sun", started=True, finished=True)
        self.assertEqual(fmt.bucket_of(s, s), "cleanup")

    def test_fully_watched_is_completed(self):
        s = show("Foo", 1, watched=8, total=8, cadence="Sun", started=True, finished=True)
        self.assertEqual(fmt.bucket_of(s, s), "completed")

    def test_abandoned_overrides_everything(self):
        s = show("Foo", 1, watched=8, total=8, started=True, finished=True, abandoned=True)
        self.assertEqual(fmt.bucket_of(s, s), "abandoned")
        s2 = show("Bar", 1, started=False, abandoned=True)
        self.assertEqual(fmt.bucket_of(s2, s2), "abandoned")

    def test_zero_total_never_completed(self):
        # total=0 (no episodes yet) must not read as "0 >= 0 watched" completed.
        s = show("Foo", 1, watched=0, total=0, started=False)
        self.assertEqual(fmt.bucket_of(s, s), "new")


class LineFormatTests(unittest.TestCase):
    EMOJI = {"Netflix": ":_nf:"}

    def test_new_binge_line(self):
        s = show("Little House on the Prairie", 1, "Netflix", 0, 8, "b", "7/9", None, False, False)
        self.assertEqual(
            fmt._new_returning_line(s, self.EMOJI, ":tv:"),
            "> :_nf:`Little House on the Prairie S01 (0/8, b)` 7/9",
        )

    def test_new_weekly_line(self):
        s = show("The Westies", 1, "MGM+", 0, 8, "Sun", "7/12", "8/23", False, False)
        self.assertEqual(
            fmt._new_returning_line(s, {}, ":tv:"),
            "> :tv:`The Westies S01 (0/8, Sun)` 7/12 - 8/23",
        )

    def test_new_weekly_unknown_finale_renders_question_marks(self):
        s = show("President Curtis", 1, "AS", 0, 13, "Sun", "7/26", None, False, False)
        self.assertEqual(
            fmt._new_returning_line(s, {}, ":tv:"),
            "> :tv:`President Curtis S01 (0/13, Sun)` 7/26 - ?/?",
        )

    def test_keepup_line_drops_cad_and_premiere_keeps_finale(self):
        s = show("The Audacity", 1, "Amazon", 3, 8, "Sun", "5/28", "5/31", True, False)
        self.assertEqual(
            fmt._keepup_line(s, {}, ":tv:"),
            "> :tv:`The Audacity S01 (3/8)` 5/31",
        )

    def test_keepup_line_unknown_finale(self):
        s = show("Last Week Tonight", 13, "HBO", 17, 54, "Sun", "2/1", None, True, False)
        self.assertEqual(
            fmt._keepup_line(s, {}, ":tv:"),
            "> :tv:`Last Week Tonight S13 (17/54)` ?/?",
        )

    def test_cleanup_line_no_dates(self):
        s = show("The Agency", 2, "Paramount+", 0, 10, "Fri", "6/1", "7/1", True, True)
        self.assertEqual(
            fmt._cleanup_line(s, {}, ":tv:"),
            "> :tv:`The Agency S02 (0/10)`",
        )

    def test_completed_line_struck_through_no_counts(self):
        s = show("The Bear", 5, "Hulu", 8, 8, "Thu", "1/1", "2/1", True, True)
        self.assertEqual(
            fmt._completed_line(s, {}, ":tv:"),
            "> :tv: ~~`The Bear S05`~~",
        )

    def test_abandoned_line_uses_frozen_form(self):
        s = show("Bel-Air", 4, "Peacock", 0, 6, None, None, None, True, False,
                  abandoned=True, abandoned_form="`Bel-Air S04 (0/6)`")
        self.assertEqual(
            fmt._abandoned_line(s, {}, ":tv:"),
            "> :tv: ~~`Bel-Air S04 (0/6)`~~",
        )

    def test_abandoned_line_falls_back_to_freeze_form_when_none(self):
        # pre-Chat-4 abandon: abandoned_form never got captured.
        s = show("Bel-Air", 4, "Peacock", 0, 6, None, None, None, True, False, abandoned=True)
        self.assertEqual(
            fmt._abandoned_line(s, {}, ":tv:"),
            "> :tv: ~~`Bel-Air S04 (0/6)`~~",
        )


class FreezeFormTests(unittest.TestCase):
    def test_pre_air_keeps_counts_and_cadence(self):
        s = show("Foo", 1, "X", 0, 8, "Sun", "7/1", "8/1", started=False)
        self.assertEqual(fmt.freeze_form(s), "`Foo S01 (0/8, Sun)`")

    def test_started_drops_cadence(self):
        s = show("Foo", 1, "X", 2, 8, "Sun", "7/1", "8/1", started=True, finished=False)
        self.assertEqual(fmt.freeze_form(s), "`Foo S01 (2/8)`")

    def test_fully_watched_drops_counts_too(self):
        s = show("Foo", 1, "X", 8, 8, "Sun", "7/1", "8/1", started=True, finished=True)
        self.assertEqual(fmt.freeze_form(s), "`Foo S01`")


class RenderPost1Tests(unittest.TestCase):
    """Reproduces POST 1 of the hand-provided July sample.

    `series_premieres`/`season_premieres` are handed to render_post1 already
    split — the sample's season numbers happen to line up with the split
    exactly (every title below is either a first season or a later one), which
    is what lets this test build the two lists straight off them."""

    def setUp(self):
        self.emoji = {
            "Netflix": ":_nf:", "MGM+": ":s_mgm:", "AppleTV+": ":_at:", "Amazon": ":_am:",
            "Peacock": ":_pe:", "Tubi": ":tubi:", "AS": ":_as:", "Hulu": ":_hu:",
            "Paramount+": ":_pa:", "SYFY": ":SYFY:",
        }
        self.series_premieres = [
            show("Little House on the Prairie", 1, "Netflix", 0, 8, "b", "7/9", None),
            show("The Westies", 1, "MGM+", 0, 8, "Sun", "7/12", "8/23"),
            show("Lucky", 1, "AppleTV+", 0, 7, "Tue", "7/14", "8/18"),
            show("Ride or Die", 1, "Amazon", 0, 8, "b", "7/15", None),
            show("TRINITY", 1, "Netflix", 0, 12, "b", "7/15", None),
            show("The Five Star Weekend", 1, "Peacock", 0, 8, "b", "7/16", None),
            show("The Hawk", 1, "Netflix", 0, 10, "b", "7/16", None),
            show("Breaking Bear", 1, "Tubi", 0, 8, "b", "7/24", None),
            show("President Curtis", 1, "AS", 0, 13, "Sun", "7/26", None),
            show("Furious", 1, "Hulu", 0, 8, "Mon", "7/27", "8/31"),
        ]
        self.season_premieres = [
            show("Silo", 3, "AppleTV+", 0, 10, "Fri", "7/3", "9/4"),
            show("King of the Hill", 15, "Hulu", 0, 10, "b", "7/20", None),
            show("Star Trek: Strange New Worlds", 4, "Paramount+", 0, 10, "Thu", "7/23", "9/24"),
            show("The Ark", 3, "SYFY", 0, 12, "Wed", "7/29", "10/14"),
        ]

    def test_matches_sample_exactly(self):
        expected = (
            "**New Shows**\n"
            "> :_nf:`Little House on the Prairie S01 (0/8, b)` 7/9\n"
            "> :s_mgm:`The Westies S01 (0/8, Sun)` 7/12 - 8/23\n"
            "> :_at:`Lucky S01 (0/7, Tue)` 7/14 - 8/18\n"
            "> :_am:`Ride or Die S01 (0/8, b)` 7/15\n"
            "> :_nf:`TRINITY S01 (0/12, b)` 7/15\n"
            "> :_pe:`The Five Star Weekend S01 (0/8, b)` 7/16\n"
            "> :_nf:`The Hawk S01 (0/10, b)` 7/16\n"
            "> :tubi:`Breaking Bear S01 (0/8, b)` 7/24\n"
            "> :_as:`President Curtis S01 (0/13, Sun)` 7/26 - ?/?\n"
            "> :_hu:`Furious S01 (0/8, Mon)` 7/27 - 8/31\n"
            "\n"
            "**Returning**\n"
            "> :_at:`Silo S03 (0/10, Fri)` 7/3 - 9/4\n"
            "> :_hu:`King of the Hill S15 (0/10, b)` 7/20\n"
            "> :_pa:`Star Trek: Strange New Worlds S04 (0/10, Thu)` 7/23 - 9/24\n"
            "> :SYFY:`The Ark S03 (0/12, Wed)` 7/29 - 10/14"
        )
        self.assertEqual(
            fmt.render_post1(self.series_premieres, self.season_premieres, self.emoji, ":tv:"),
            expected,
        )


class RenderPost1TrustsTheStoredSplitTests(unittest.TestCase):
    """render_post1 renders each argument under its own header and applies no
    filter of its own — the split into series vs season premiere already
    happened once, when the record was written (store.premiere_kind), and this
    module re-checking it (or the airing state, or an abandon) at render time is
    exactly how the announcement and reality could come to disagree."""

    def test_a_later_season_handed_in_as_a_series_premiere_still_renders_as_new(self):
        # Deliberately mis-filed to prove render_post1 takes the caller's word
        # for which list a record is in rather than re-deriving it from the
        # season number.
        s = show("Renumbered", 3, total=8, premiere="7/1")
        out = fmt.render_post1([s], [], {}, ":tv:")
        self.assertIn("Renumbered", out.split("**Returning**")[0])

    def test_nothing_is_filtered_by_airing_state_or_by_being_given_up_on(self):
        """A season the month announced stays announced whatever it has since
        become — still airing, fully watched, or turned away. Only the ✕ removes
        a record, and that happens before render_post1 ever sees it."""
        aired = show("Already Airing", 1, watched=3, total=8, cadence="Sun",
                     premiere="7/6", finale="8/24", started=True, finished=False)
        binged = show("Bingewatched", 1, watched=8, total=8, cadence="b",
                      premiere="7/2", started=True, finished=True)
        gone = show("Dropped It", 1, watched=1, total=8, premiere="7/9",
                    started=True, abandoned=True)
        out = fmt.render_post1([aired, binged, gone], [], {}, ":tv:")
        for title in ("Already Airing", "Bingewatched", "Dropped It"):
            self.assertIn(title, out)

    def test_new_and_returning_each_sort_by_their_own_premiere_date(self):
        earlier_title_later_date = show("Second", 1, premiere="7/20")
        later_title_earlier_date = show("First", 1, premiere="7/2")
        out = fmt.render_post1(
            [earlier_title_later_date, later_title_earlier_date], [], {}, ":tv:")
        self.assertLess(out.index("First"), out.index("Second"))

    def test_link_is_appended_only_when_given(self):
        without_link = fmt.render_post1([], [], {}, ":tv:")
        with_link = fmt.render_post1([], [], {}, ":tv:", link_url="https://example.test/c/abc")
        self.assertNotIn("example.test", without_link)
        self.assertIn("https://example.test/c/abc", with_link)


class RenderPost2Tests(unittest.TestCase):
    """Reproduces POST 2's Cleanup/Keepup sections of the hand-provided July sample."""

    def setUp(self):
        self.emoji = {
            "Paramount+": ":_pa:", "Disney+": ":_dp:", "Netflix": ":_nf:", "Hulu": ":_hu:",
            "Peacock": ":_pe:", "Amazon": ":_am:", "AppleTV+": ":_at:", "HBO": ":_hb:",
            "MGM+": ":s_mgm:", "Apple2": ":_ap:",
        }
        cleanup_rows = [
            ("The Agency", 2, "Paramount+", 0, 10),
            ("The Artful Dodger", 2, "Disney+", 1, 8),
            ("Avatar: The Last Airbender", 2, "Netflix", 1, 7),
            ("The Bear", 5, "Hulu", 6, 8),
            ("Bel-Air", 4, "Peacock", 0, 6),
            ("Big Mistakes", 1, "Netflix", 0, 8),
            ("The Creep Tapes", 2, "Amazon", 2, 6),
            ("Criminal Record", 2, "AppleTV+", 0, 8),
            ("Dark Winds", 4, "Amazon", 0, 8),
            ("Deli Boys", 2, "Hulu", 1, 10),
            ("The Four Seasons", 2, "Netflix", 0, 8),
            ("A Good Girl's Guide to Murder", 2, "Netflix", 0, 6),
            ("Half Man", 1, "HBO", 1, 6),
            ("Man on Fire", 1, "Netflix", 0, 7),
            ("The Miniature Wife", 1, "Peacock", 3, 10),
            ("Monarch: Legacy of Monsters", 2, "AppleTV+", 0, 10),
            ("Sort Of", 3, "HBO", 0, 8),
            ("Spider-Noir", 1, "MGM+", 1, 8),
            ("Stranger Things: Tales from '85", 1, "Netflix", 1, 10),
            ("Sweet Tooth", 2, "Netflix", 0, 8),
            ("Ted", 2, "Peacock", 0, 8),
            ("Unchosen", 1, "Netflix", 1, 6),
            ("Young Sherlock", 1, "Apple2", 1, 8),  # seasonless in the sample; SXX rendered per this chat's scope
        ]
        self.shows = [
            show(t, s, net, w, tot, cadence="b", premiere="1/1", started=True, finished=True)
            for (t, s, net, w, tot) in cleanup_rows
        ]
        keepup_rows = [
            ("The Audacity", 1, "Amazon", 3, 8, "Sun", "5/31"),
            ("House of the Dragon", 3, "HBO", 2, 8, "Sun", "8/9"),
            ("Interview With The Vampire", 3, "Amazon", 1, 7, "Sun", "7/19"),
            ("Last Week Tonight", 13, "HBO", 17, 54, "Sun", None),
            ("Rick and Morty", 9, "Hulu", 5, 10, "Sun", "7/27"),
            ("Maximum Pleasure Guaranteed", 1, "AppleTV+", 7, 10, "Wed", "7/15"),
            ("Cape Fear", 1, "AppleTV+", 5, 10, "Thu", "7/30"),
            ("Dutton Ranch", 1, "Paramount+", 8, 9, "Fri", "7/3"),
            ("Star City", 1, "AppleTV+", 6, 8, "Fri", "7/10"),
            ("Sugar", 2, "Apple2", 0, 8, "Fri", "8/7"),
        ]
        self.shows += [
            show(t, s, net, w, tot, cadence=cad, finale=fin, started=True, finished=False)
            for (t, s, net, w, tot, cad, fin) in keepup_rows
        ]

    def test_cleanup_and_keepup_sections_match_sample(self):
        rendered = fmt.render_post2(self.shows, self.emoji, ":tv:")
        expected_cleanup = (
            "## **Cleanup**\n"
            "> :_pa:`The Agency S02 (0/10)`\n"
            "> :_dp:`The Artful Dodger S02 (1/8)`\n"
            "> :_nf:`Avatar: The Last Airbender S02 (1/7)`\n"
            "> :_hu:`The Bear S05 (6/8)`\n"
            "> :_pe:`Bel-Air S04 (0/6)`\n"
            "> :_nf:`Big Mistakes S01 (0/8)`\n"
            "> :_am:`The Creep Tapes S02 (2/6)`\n"
            "> :_at:`Criminal Record S02 (0/8)`\n"
            "> :_am:`Dark Winds S04 (0/8)`\n"
            "> :_hu:`Deli Boys S02 (1/10)`\n"
            "> :_nf:`The Four Seasons S02 (0/8)`\n"
            "> :_nf:`A Good Girl's Guide to Murder S02 (0/6)`\n"
            "> :_hb:`Half Man S01 (1/6)`\n"
            "> :_nf:`Man on Fire S01 (0/7)`\n"
            "> :_pe:`The Miniature Wife S01 (3/10)`\n"
            "> :_at:`Monarch: Legacy of Monsters S02 (0/10)`\n"
            "> :_hb:`Sort Of S03 (0/8)`\n"
            "> :s_mgm:`Spider-Noir S01 (1/8)`\n"
            "> :_nf:`Stranger Things: Tales from '85 S01 (1/10)`\n"
            "> :_nf:`Sweet Tooth S02 (0/8)`\n"
            "> :_pe:`Ted S02 (0/8)`\n"
            "> :_nf:`Unchosen S01 (1/6)`\n"
            "> :_ap:`Young Sherlock S01 (1/8)`"
        )
        expected_keepup = (
            "## **Keepup**\n"
            "*Sun*\n"
            "> :_am:`The Audacity S01 (3/8)` 5/31\n"
            "> :_hb:`House of the Dragon S03 (2/8)` 8/9\n"
            "> :_am:`Interview With The Vampire S03 (1/7)` 7/19\n"
            "> :_hb:`Last Week Tonight S13 (17/54)` ?/?\n"
            "> :_hu:`Rick and Morty S09 (5/10)` 7/27\n"
            "*Wed*\n"
            "> :_at:`Maximum Pleasure Guaranteed S01 (7/10)` 7/15\n"
            "*Thu*\n"
            "> :_at:`Cape Fear S01 (5/10)` 7/30\n"
            "*Fri*\n"
            "> :_pa:`Dutton Ranch S01 (8/9)` 7/3\n"
            "> :_at:`Star City S01 (6/8)` 7/10\n"
            "> :_ap:`Sugar S02 (0/8)` 8/7"
        )
        self.assertTrue(rendered.startswith(expected_cleanup + "\n\n" + expected_keepup))

    def test_completed_and_abandoned_sections_omitted_when_empty(self):
        rendered = fmt.render_post2(self.shows, self.emoji, ":tv:")
        self.assertNotIn("Completed", rendered)
        self.assertNotIn("Abandoned", rendered)

    def test_completed_and_abandoned_sections_render_struck_through_when_present(self):
        shows = list(self.shows)
        shows.append(show("Ghosted", 1, "Netflix", 8, 8, started=True, finished=True))  # completed
        shows.append(show("Cancelled Show", 1, "Netflix", 2, 8, started=True,
                           abandoned=True, abandoned_form="`Cancelled Show S01 (2/8)`"))
        rendered = fmt.render_post2(shows, self.emoji, ":tv:")
        self.assertIn("**Completed**\n> :_nf: ~~`Ghosted S01`~~", rendered)
        self.assertIn("**Abandoned**\n> :_nf: ~~`Cancelled Show S01 (2/8)`~~", rendered)


class RenderPost2NeverAnnouncesPremieresTests(unittest.TestCase):
    """POST 2 answers a different question from POST 1: what is in hand and what
    got settled, never what began the month. discord_fmt.READER_BUCKETS states
    this once for every standing, and this class exercises render_post2 directly
    against it rather than through a row list built for the page — the page's
    filtering is routes.py's job now (_rows_for), not this module's."""

    def setUp(self):
        self.emoji = {}
        self.roster = [
            show("Airing Weekly", 1, watched=2, total=8, cadence="Sun", started=True),
            show("Finale Aired", 1, watched=5, total=8, cadence="Sun", started=True, finished=True),
            show("Not Started", 1, total=8, premiere="9/4"),
            show("All Watched", 1, watched=8, total=8, started=True, finished=True),
            show("Given Up", 1, watched=2, total=8, started=True,
                 abandoned=True, abandoned_form="`Given Up S01 (2/8)`"),
        ]

    def test_the_month_in_progress_carries_keepup_cleanup_completed_and_abandoned(self):
        rendered = fmt.render_post2(self.roster, self.emoji, ":tv:",
                                    standing=fmt.MonthStanding.CURRENT)
        for header in ("## **Cleanup**", "## **Keepup**", "**Completed**", "**Abandoned**"):
            self.assertIn(header, rendered)

    def test_the_month_in_progress_never_carries_new_or_returning(self):
        """The reversal: POST 2 used to announce premieres in the one standing
        that carried everything else. It carries neither section now, ever."""
        rendered = fmt.render_post2(self.roster, self.emoji, ":tv:",
                                    standing=fmt.MonthStanding.CURRENT)
        self.assertNotIn("New Shows", rendered)
        self.assertNotIn("**Returning**", rendered)

    def test_a_month_that_is_over_carries_only_its_verdicts_and_its_films(self):
        rendered = fmt.render_post2(self.roster, self.emoji, ":tv:",
                                    movies=[{"title": "A Film", "year": 2011}],
                                    standing=fmt.MonthStanding.PAST)
        self.assertNotIn("Cleanup", rendered)
        self.assertNotIn("Keepup", rendered)
        self.assertIn("**Completed**", rendered)
        self.assertIn("**Abandoned**", rendered)
        self.assertIn("A Film", rendered)

    def test_a_month_that_has_not_begun_says_nothing_at_all(self):
        """Not merely no Cleanup/Keepup (already true before) — no New/Returning
        either, so a future month's second notice renders empty."""
        rendered = fmt.render_post2(self.roster, self.emoji, ":tv:",
                                    standing=fmt.MonthStanding.FUTURE)
        self.assertEqual(rendered, "")

    def test_the_default_is_the_month_in_progress(self):
        """A caller that only wants the markup — a test, a preview — gets the
        full shape without stating a standing, as render_post1 needs no
        argument to fall back on either."""
        self.assertEqual(fmt.render_post2(self.roster, self.emoji, ":tv:"),
                         fmt.render_post2(self.roster, self.emoji, ":tv:",
                                          standing=fmt.MonthStanding.CURRENT))


class BucketVocabularyTests(unittest.TestCase):
    """This module DECIDES a bucket for a not-yet-stored season; the closed set
    of buckets is named beside the column that stores one, and its own tests
    live with it in test_store.py."""

    def test_bucket_of_answers_with_a_member(self):
        s = {"watched": 8, "total": 8, "started_airing": True}
        self.assertIs(fmt.bucket_of(s, s), fmt.Bucket.COMPLETED)

    def test_every_bucket_gets_a_group_whether_or_not_anything_lands_in_it(self):
        """Callers read one bucket without first checking that it exists."""
        groups = fmt._group_by_bucket([])
        self.assertEqual(set(groups), set(fmt.Bucket))


if __name__ == "__main__":
    unittest.main()
