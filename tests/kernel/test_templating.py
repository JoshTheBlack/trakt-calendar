"""The shared template environment's own behaviour, as opposed to any one page's.

A TEMPLATE THAT OPENS WITH app/templating.py's DEDENT_MARKER IS SENT WITHOUT ITS
INDENTATION, and only that template. The row on a rankings board is drawn up to a
thousand times per page and redrawn on every action that is not a drag, so its
source indentation was a fifth of every row; the rule is opt-in because
whitespace is content inside <pre> and <textarea>.
"""
from __future__ import annotations

import unittest

from jinja2 import DictLoader, Environment

from app.templating import DEDENT_MARKER, TEMPLATES_DIR, Dedent


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


if __name__ == "__main__":
    unittest.main()
