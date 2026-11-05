"""The shared template environment's own behaviour, as opposed to any one page's.

A TEMPLATE THAT OPENS WITH app/templating.py's DEDENT_MARKER IS SENT WITHOUT ITS
INDENTATION, and only that template. The row on a rankings board is drawn up to a
thousand times per page and redrawn on every action that is not a drag, so its
source indentation was a fifth of every row; the rule is opt-in because
whitespace is content inside <pre> and <textarea>. One that opens with
TIGHT_MARKER loses the line breaks between its tags as well, for the calendar
card, where every container lays out by flex or grid.
"""
from __future__ import annotations

import unittest

from jinja2 import DictLoader, Environment

from app.templating import DEDENT_MARKER, TEMPLATES_DIR, TIGHT_MARKER, Dedent


def render(source: str) -> str:
    env = Environment(loader=DictLoader({"t.html": source}), extensions=[Dedent])
    return env.get_template("t.html").render(name="x")


class DedentTests(unittest.TestCase):
    def test_a_marked_template_loses_its_indentation_and_keeps_its_lines(self):
        source = DEDENT_MARKER + "\n<div>\n    <span>{{ name }}</span>\n        <b>y</b>\n</div>"
        self.assertEqual(render(source), "<div>\n<span>x</span>\n<b>y</b>\n</div>")

    def test_an_unmarked_template_is_left_exactly_as_written(self):
        """Literal text in a <pre> keeps its shape, which is why this is opt-in."""
        source = "<pre>\n    indented\n</pre>"
        self.assertEqual(render(source), source)

    def test_the_marker_draws_nothing(self):
        self.assertEqual(render(DEDENT_MARKER + "\n<p>x</p>"), "<p>x</p>")

    def test_the_ranker_row_asks_for_it(self):
        """The one template drawn a thousand times over. If the marker is moved
        below its first line it silently stops applying."""
        row = (TEMPLATES_DIR / "_ranker_row.html").read_text(encoding="utf-8")
        self.assertTrue(row.startswith(DEDENT_MARKER))


class TightTests(unittest.TestCase):
    """A TEMPLATE THAT OPENS WITH TIGHT_MARKER ALSO LOSES THE LINE BREAKS BETWEEN
    ITS TAGS — each one a text node the browser holds for nothing where every
    container lays out by flex or grid — and never one beside an expression,
    whose output may be text the newline was spacing."""

    def test_the_breaks_between_tags_go(self):
        source = TIGHT_MARKER + "\n<div>\n    <span>a</span>\n    <b>y</b>\n</div>"
        self.assertEqual(render(source), "<div><span>a</span><b>y</b></div>")

    def test_the_breaks_around_a_statement_or_comment_tag_go(self):
        source = (TIGHT_MARKER + "\n<p>\n{% if name %}\n<i>i</i>\n{% endif %}\n"
                  "{# a note #}\n<u>u</u>\n</p>")
        self.assertEqual(render(source), "<p><i>i</i><u>u</u></p>")

    def test_a_break_beside_an_expression_stays(self):
        """`{{ name }}` may be text, and text beside text needs its space."""
        source = TIGHT_MARKER + "\n<p>\n{{ name }}\n<b>y</b>\n</p>"
        self.assertEqual(render(source), "<p>\nx\n<b>y</b></p>")

    def test_a_crlf_break_between_tags_goes_too(self):
        """An editor that saves CRLF must not quietly switch the rule off."""
        source = TIGHT_MARKER + "\r\n<div>\r\n    <span>a</span>\r\n</div>"
        self.assertEqual(render(source), "<div><span>a</span></div>")

    def test_a_break_inside_text_stays(self):
        source = TIGHT_MARKER + "\n<p>one\ntwo</p>"
        self.assertEqual(render(source), "<p>one\ntwo</p>")

    def test_the_card_and_what_it_is_built_from_ask_for_it(self):
        """Drawn once per card, over a thousand times a month. Moved below the
        first line, the marker silently stops applying."""
        for name in ("_card.html", "_filter_badge.html", "_filter_offer.html",
                     "_season_line.html", "_source_logo.html", "_library_button.html"):
            with self.subTest(name):
                source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
                self.assertTrue(source.startswith(TIGHT_MARKER))


if __name__ == "__main__":
    unittest.main()
