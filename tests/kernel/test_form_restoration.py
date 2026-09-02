"""A control whose value the SERVER decides must opt out of form restoration.

Browsers keep a form control's live value across history navigation and across
a reload, and they reapply it AFTER parsing — so it wins over the `selected`
the server just rendered. On an ordinary form that is a courtesy: it saves a
half-filled form from a stray Back. On a select that ACTS on `change` it is a
trap, because the control ends up displaying a value the page is not showing,
and choosing that same entry back fires no `change` event at all. The endpoint
picker reached exactly that state — Series Premieres, Movie Premieres, Back,
reload — and the endpoint it claimed to be on was the one view unreachable
without first picking a third.

`autocomplete="off"` is the opt-out, and it belongs on every select rendering a
server-decided `selected`, not only on the ones seen to break: which of them
acts on `change` is a detail that changes without anyone remembering this.

No network, no database, no client: this reads templates.
"""
from __future__ import annotations

import re
import unittest

from app.templating import TEMPLATES_DIR

# Enough of a select's opening tag to find it and to read its attributes. The
# tags are hand-written markup with Jinja inside their bodies, never inside the
# opening tag itself, so a non-greedy run up to the first ">" is exact here.
SELECT_TAG = re.compile(r"<select\b[^>]*>", re.S)
SELECTED_OPTION = re.compile(r"\bselected\b")

# A form that navigates by putting its controls in the query string. Every
# select inside one has its value in the address bar by construction, which is
# what makes "is this select URL state" answerable from the markup rather than
# from a guess about what its onchange handler does.
GET_FORM = re.compile(r'<form\b[^>]*method="get"[^>]*>.*?</form>', re.S | re.I)


def markup(path) -> str:
    """The template with its Jinja comments stripped: the comment explaining
    this rule quotes the very attribute the test scans for."""
    return re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)


def selects_with_server_chosen_values(text: str) -> list[str]:
    """Every <select ...> opening tag in `text` whose own body renders a
    `selected` attribute — that is what "the server decides this value" looks
    like in a template. A select filled in by JavaScript renders no `selected`
    and is not covered: its script runs after restoration and overwrites it."""
    found = []
    for tag in SELECT_TAG.finditer(text):
        end = text.find("</select>", tag.end())
        body = text[tag.end():end] if end != -1 else ""
        if SELECTED_OPTION.search(body):
            found.append(tag.group(0))
    return found


class ServerChosenSelectsOptOutOfRestoration(unittest.TestCase):
    def test_every_server_rendered_select_disables_autocomplete(self):
        offenders = []
        for path in sorted(TEMPLATES_DIR.rglob("*.html")):
            for tag in selects_with_server_chosen_values(markup(path)):
                if 'autocomplete="off"' not in tag:
                    offenders.append(f"{path.name}: {tag.strip()}")
        self.assertEqual(
            [], offenders,
            "these selects render a server-chosen `selected` but let the browser "
            "restore a stale value over it; add autocomplete=\"off\":\n  "
            + "\n  ".join(offenders),
        )

    def test_every_select_that_acts_on_change_also_resyncs_after_a_restore(self):
        """`autocomplete="off"` is only half the rule, and the missing half was
        found in the browser rather than here.

        That attribute governs the browser reapplying a control's value over a
        freshly parsed page. It has nothing to say about a restore that hands
        back a whole DOM — the back/forward cache, or htmx's history cache, which
        serves a boosted Back from a snapshot and makes no request. Both bring
        back the choice made a moment before navigating away, on a page that is
        now showing something else. Observed on the share page: Back left the
        wrong endpoint named and only a reload cleared it.

        `data-url-state` is what marks a select for the resync in ui.js, and this
        asks it of the selects INSIDE A GET FORM — because those are the ones
        whose value the form literally puts in the address bar, which is the
        definition of the thing being guarded. A preference select is
        deliberately not covered: the calendar's card style and day packing act
        on change too, but they SAVE, and a restored page showing the visitor's
        own saved choice is correct rather than stale. Resyncing those would
        revert a change they made on purpose.
        """
        offenders = []
        for path in sorted(TEMPLATES_DIR.rglob("*.html")):
            for form in GET_FORM.finditer(markup(path)):
                for tag in selects_with_server_chosen_values(form.group(0)):
                    if "onchange=" in tag and "data-url-state" not in tag:
                        offenders.append(f"{path.name}: {tag.strip()}")
        self.assertEqual(
            [], offenders,
            "these selects act on `change` but would keep a restored value that "
            "the page is not showing; add data-url-state:\n  " + "\n  ".join(offenders),
        )

    def test_the_pages_carrying_those_selects_load_the_script_that_resyncs_them(self):
        """The attribute is inert without ui.js, and a page can grow one of these
        selects without growing the bundle that makes it work."""
        from app import assets

        marked = {
            path.stem for path in TEMPLATES_DIR.rglob("*.html")
            if "data-url-state" in markup(path)
        }
        # index.html is the calendar page and share_calendar.html the share one;
        # the bundle keys are the page names, not the template filenames.
        bundles = {"index": "calendar", "share_calendar": "share", "pick": "pick",
                   "sources": "sources"}
        for stem in sorted(marked):
            with self.subTest(template=stem):
                key = bundles.get(stem, stem)
                self.assertIn("static/js/ui.js", assets.PAGE_SCRIPTS[key],
                              f"{stem}.html marks a select for resync but its "
                              f"bundle does not load ui.js")

    def test_the_scan_actually_finds_the_selects_it_is_guarding(self):
        """A regex that matched nothing would pass the test above silently, and
        the templates it guards are exactly the ones being reworked."""
        total = sum(
            len(selects_with_server_chosen_values(markup(path)))
            for path in TEMPLATES_DIR.rglob("*.html")
        )
        self.assertGreaterEqual(total, 8)


if __name__ == "__main__":
    unittest.main()
