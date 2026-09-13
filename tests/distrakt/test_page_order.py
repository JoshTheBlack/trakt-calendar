"""The page and the second notice sort the same way, because they sort through
the same two functions.

THE DEFECT THIS CLOSES, MEASURED BEFORE IT WAS FIXED. The tracker's rows were
ordered in the browser and the notices in Python, and the two implementations
had drifted in exactly two ways:

  - THE ARTICLE MATCH. The browser stripped a leading "the"/"a"/"an" followed by
    ANY WHITESPACE (`/^(the|a|an)\\s+/`); `_sort_title` matches the literal
    strings "the ", "a ", "an ". A title separated from its article by a
    NON-BREAKING SPACE therefore lost the article on the page and kept it in the
    post — "The\\u00a0Bear" sorted under B on one and under T on the other.
  - THE COMPARISON. The browser used `localeCompare`, which folds case and
    accents; Python compares code points, where every accented letter sorts after
    every unaccented one. "Émilie" came before "Zoe" on the page and after it in
    the post.

PYTHON IS AUTHORITATIVE AND THE PAGE FOLLOWS IT, which is the owner's ruling and
the only one that can be tested: the notice is what gets published, and a post
that disagrees with the page it was generated from is the wrong half to keep.

ASKED OF THE TWO RENDERERS TOGETHER rather than of a shared helper, because a
shared helper is what the fix IS and a test of it would pass just as well if one
of the two callers stopped using it.
"""
from __future__ import annotations

import unittest

from app.distrakt import discord_fmt
from app.distrakt.store import Bucket, MonthStanding

NBSP = " "


def _row(title: str, bucket=Bucket.CLEANUP, **extra) -> dict:
    """One row in the shape both renderers read — a stored record's own."""
    return {"title": title, "season": 1, "bucket": str(bucket), "network": "Net",
            "watched": 1, "total": 6, "cadence": None, "premiere": None,
            "finale": None, "started_airing": True, "finished_airing": False,
            **extra}


def _page_order(rows: list[dict]) -> list[str]:
    """The titles in the order the page's own section draws them."""
    section, = discord_fmt.page_sections(rows)
    return [row["title"] for row in section.rows]


def _notice_order(rows: list[dict]) -> list[str]:
    """The titles in the order the second notice lists them."""
    text = discord_fmt.render_post2(rows, standing=MonthStanding.CURRENT)
    return [line.split("`")[1].rsplit(" S", 1)[0]
            for line in text.splitlines() if line.startswith("> ")]


class TheTwoOrdersAgreeTests(unittest.TestCase):
    def test_a_leading_article_followed_by_a_non_breaking_space(self):
        """The whitespace class was the whole disagreement: one reader saw an
        article to ignore and the other did not."""
        rows = [_row(f"The{NBSP}Bear"), _row("Adolescence"), _row("Zodiac")]
        self.assertEqual(_page_order(rows), _notice_order(rows))
        # And it is PYTHON's answer both of them give: a non-breaking space is
        # not the space `_sort_title` strips an article before, so the title
        # sorts under T.
        self.assertEqual(_page_order(rows), ["Adolescence", f"The{NBSP}Bear", "Zodiac"])

    def test_an_accented_title(self):
        """`localeCompare` folds accents and code-point order does not, so this
        pair came out in opposite orders on the two surfaces."""
        rows = [_row("Émilie"), _row("Zodiac"), _row("Adolescence")]
        self.assertEqual(_page_order(rows), _notice_order(rows))
        self.assertEqual(_page_order(rows), ["Adolescence", "Zodiac", "Émilie"])

    def test_an_ordinary_leading_article_is_still_ignored_by_both(self):
        """The behaviour that was never in dispute, kept: title-alphabetization
        style ignores The, A and An, confirmed against a real month's post."""
        rows = [_row("The Bear"), _row("Adolescence"), _row("A Zodiac")]
        self.assertEqual(_page_order(rows), _notice_order(rows))
        self.assertEqual(_page_order(rows), ["Adolescence", "The Bear", "A Zodiac"])

    def test_case_is_folded_by_both(self):
        rows = [_row("zodiac"), _row("Adolescence")]
        self.assertEqual(_page_order(rows), _notice_order(rows))
        self.assertEqual(_page_order(rows), ["Adolescence", "zodiac"])


class ThePremiereOrderAgreesTooTests(unittest.TestCase):
    """New and Returning go by premiere date on the page. The second notice
    never carries them — announcing a premiere is the first notice's job in
    every standing — so the agreement to hold is with THAT one."""

    def _premieres(self) -> list[dict]:
        return [
            _row("The Zodiac", Bucket.NEW, premiere="7/16"),
            _row(f"A{NBSP}Bear", Bucket.NEW, premiere="7/1"),
            _row("Adolescence", Bucket.NEW, premiere="7/16"),
        ]

    def test_the_page_and_the_announcement_agree(self):
        rows = self._premieres()
        section, = discord_fmt.page_sections(rows)
        announced = [line.split("`")[1].rsplit(" S", 1)[0]
                     for line in discord_fmt.render_post1(rows, []).splitlines()
                     if line.startswith("> ")]
        self.assertEqual([row["title"] for row in section.rows], announced)

    def test_a_same_day_tie_breaks_on_the_same_article_rule(self):
        """The tie-break is `_sort_title`, so the non-breaking space behaves here
        exactly as it does everywhere else rather than by a second rule."""
        section, = discord_fmt.page_sections(self._premieres())
        self.assertEqual([row["title"] for row in section.rows],
                         [f"A{NBSP}Bear", "Adolescence", "The Zodiac"])


class TheVocabularyHasOneHomeTests(unittest.TestCase):
    """What a bucket is CALLED, and which order the page draws them in.

    Both were spelled three times — the enum's lower-case identifiers, the
    notices' inline markdown headers, and a table in the browser — so a bucket
    renamed would have been renamed in one of three places.
    """

    def test_every_bucket_has_a_label(self):
        self.assertEqual(set(discord_fmt.BUCKET_LABELS), set(Bucket))

    def test_the_notices_headers_are_the_same_words_the_page_draws(self):
        rows = [_row("A", Bucket.CLEANUP), _row("B", Bucket.KEEPUP, cadence="Tue"),
                _row("C", Bucket.COMPLETED), _row("D", Bucket.ABANDONED)]
        text = discord_fmt.render_post2(rows, standing=MonthStanding.CURRENT)
        drawn = {section.label for section in discord_fmt.page_sections(rows)}
        for bucket in (Bucket.CLEANUP, Bucket.KEEPUP, Bucket.COMPLETED, Bucket.ABANDONED):
            with self.subTest(bucket=bucket):
                label = discord_fmt.BUCKET_LABELS[bucket]
                self.assertIn(f"**{label}**", text)
                self.assertIn(label, drawn)

    def test_the_pages_order_covers_every_bucket_exactly_once(self):
        self.assertEqual(sorted(discord_fmt.PAGE_BUCKET_ORDER, key=str),
                         sorted(Bucket, key=str))

    def test_keepup_is_the_only_section_that_groups_by_weekday(self):
        """The one shape that differs, and it differs the same way in both
        readers — the same seven strings, in the same order."""
        rows = [_row("Tuesday Show", Bucket.KEEPUP, cadence="Tue"),
                _row("Sunday Show", Bucket.KEEPUP, cadence="Sun"),
                _row("Cleanup Show", Bucket.CLEANUP)]
        sections = {s.label: s for s in discord_fmt.page_sections(rows)}
        self.assertEqual([day for day, _rows in sections["Keepup"].days], ["Sun", "Tue"])
        self.assertEqual(sections["Keepup"].rows, [])
        self.assertEqual(sections["Cleanup"].days, [])
        text = discord_fmt.render_post2(rows, standing=MonthStanding.CURRENT)
        self.assertLess(text.index("*Sun*"), text.index("*Tue*"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
