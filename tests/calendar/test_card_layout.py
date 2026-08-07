"""Where a card's parts are DRAWN, asserted against the stylesheet itself.

A LONG DESCRIPTION STAYS INSIDE THE CARD: it is capped and it scrolls, and it
never runs on under the air date and the buttons. The cap is in the stylesheet
and the markup around it is in the template, so neither file alone says whether
the description is still bounded — which is exactly how wrapping it in a row lost
the bound in two of the three layouts while the third kept it and looked fine.

READING CSS IN A TEST IS DELIBERATE AND ITS LIMIT IS STATED: this checks the
declarations that decide the layout, not the layout a browser computes. It cannot
tell you the card looks right. It can tell you the description was given a height
to scroll inside, which is the thing that was actually got wrong.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.templating import TEMPLATES_DIR

STYLESHEET = (Path(__file__).resolve().parents[2] / "app" / "static" / "css" / "style.css")
CSS = STYLESHEET.read_text(encoding="utf-8")
CARD = (TEMPLATES_DIR / "_card.html").read_text(encoding="utf-8")

# The body classes the three card layouts are drawn under. There is no per-style
# template — the card is one piece of markup and the style is a class on <body> —
# so a layout claim has to be made about all three or it is made about none.
LAYOUTS = ("card-vertical", "card-horizontal", "card-poster")


def _blocks() -> list[tuple[list[str], str]]:
    """(selectors, declarations) for every rule in the stylesheet.

    Comments are stripped first so a selector named in prose is not read as a
    rule, and at-rule bodies are left in place — a rule inside a media query is
    still a rule, and the ones this file asks about are not nested anyway.
    """
    text = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    out = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        selectors = [s.strip() for s in match.group(1).split(",") if s.strip()]
        out.append((selectors, match.group(2)))
    return out


BLOCKS = _blocks()


def declarations(selector: str) -> str:
    """Every declaration made about `selector`, in source order, joined.

    Exact selector match rather than a substring search: `.overview` and
    `.overview-row` are two different elements and one of them is a prefix of the
    other.
    """
    return " ".join(body for selectors, body in BLOCKS if selector in selectors)


def value(body: str, prop: str) -> str | None:
    """The LAST value `prop` is given in `body` — the one that wins."""
    found = re.findall(r"(?:^|;)\s*%s\s*:\s*([^;]+)" % re.escape(prop), body)
    return found[-1].strip() if found else None


class ALongDescriptionStaysInsideTheCardTests(unittest.TestCase):
    """It truncates, it scrolls, and it never reaches the air date."""

    def test_the_description_is_wrapped_in_a_row_whatever_the_card_carries(self):
        """The row is unconditional, so the layout is one shape rather than one
        shape per card. A row that appeared only beside a flip control would mean
        two of these layouts were only ever seen on a merged card."""
        row = re.search(r'<div class="overview-row">\s*<div class="overview"',
                        CARD, re.S)
        self.assertIsNotNone(
            row, "the description is not the first thing inside .overview-row")

    def test_the_row_can_shrink_so_the_card_bottom_is_never_pushed_off(self):
        """`min-height: 0` is what lets a flex child be smaller than its content.
        Without it the row claims the height of the whole description and the air
        date and the buttons go out of the card."""
        self.assertEqual(value(declarations(".overview-row"), "min-height"), "0")

    def test_the_description_is_capped_where_it_is_not_told_to_fill(self):
        body = declarations(".overview")
        self.assertIsNotNone(value(body, "max-height"))
        self.assertIsNotNone(value(body, "overflow"))

    def test_where_the_cap_is_lifted_the_row_hands_down_a_height_instead(self):
        """THE REGRESSION THIS FILE EXISTS FOR. Two layouts lift the cap because
        the description is meant to fill what the card has left and scroll. As a
        direct child of the card body it got its height from that column; inside a
        ROW, `flex: 1 1 auto` sizes it ACROSS and decides nothing about its
        height — so unless the row stretches it, it grows to fit its text, scrolls
        nothing, and renders over everything below it."""
        lifted = 0
        for layout in LAYOUTS:
            overview = declarations(f"body.{layout} .overview")
            if value(overview, "max-height") != "none":
                continue
            lifted += 1
            with self.subTest(layout=layout):
                self.assertEqual(value(overview, "overflow-y"), "auto",
                                 "an uncapped description that does not scroll is unreachable text")
                row = declarations(f"body.{layout} .overview-row")
                self.assertEqual(value(row, "align-items"), "stretch",
                                 "nothing gives the description a height to scroll inside")
                self.assertEqual(value(row, "min-height"), "0")
        self.assertTrue(lifted, "no layout lifts the cap; this test checked nothing")

    def test_the_flip_control_beside_it_keeps_its_own_size(self):
        """It sits in the row that stretches, and a button drawn as tall as a
        paragraph is a click target nobody aimed at."""
        self.assertEqual(value(declarations(".source-swap"), "align-self"),
                         "flex-start")


if __name__ == "__main__":
    unittest.main()
