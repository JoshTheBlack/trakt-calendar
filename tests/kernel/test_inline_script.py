"""Nothing in a template runs as script or applies as style except what the
Content-Security-Policy admits by hash.

The policy (app/csp.py) has no 'unsafe-inline' and no 'unsafe-eval' in either
script-src or style-src, so a handler attribute, a style attribute, an htmx
expression built with the Function constructor or an unlisted inline block does
not fail loudly: the browser refuses it, logs one console line, and a button
does nothing. Nothing in the rest of the suite clicks anything. That is why this
file reads the templates instead, so a reintroduced one fails here rather than
in somebody's browser.

AND THE HASHES ARE CHECKED AGAINST WHAT A BROWSER IS ACTUALLY SENT. csp.py
derives each one from its template once per server start, rendered with no
context; a block that came to depend on what the page around it is given would
render differently in a real response, and the browser would refuse it. So the
blocks are read back out of real responses here and hashed again.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from app import assets, changelog, csp

from tests.support import AppTestCase

TEMPLATES = assets.BASE_DIR / "templates"

# Jinja comments go first: several templates explain in prose what they no
# longer carry, and prose naming an onclick is not an onclick.
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# And then Jinja's own statements and expressions, which are template code
# rather than markup — `{% if style == view.card_style %}` names no attribute.
# An attribute a condition guards is literal text beside the tags and survives.
JINJA_CODE = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)

# An attribute is whitespace, a name, then `=`. Prose never has the `=`.
HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)
STYLE_ATTR = re.compile(r"\sstyle\s*=", re.I)
# The three htmx spellings its source builds with the Function constructor: an
# event filter (`click[...]`), an hx-on expression, and a `js:` value.
EVAL_FILTER = re.compile(r"""hx-trigger\s*=\s*(["'])[^"']*\[""")
EVAL_ON = re.compile(r"\shx-on[:-]")
EVAL_JS = re.compile(r"""\shx-(?:vals|headers|confirm|vars)\s*=\s*(["'])\s*js:""")
# An executable block: a <script> with no src and not declared JSON data, or any
# <style>. csp.py's pattern is narrower (a bare tag) because it reads rendered
# output; this one is wide on purpose, so an attribute cannot slip a block past.
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.I)
STYLE_TAG = re.compile(r"<style\b", re.I)


def source(path: Path) -> str:
    text = JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))
    return JINJA_CODE.sub("", HTML_COMMENT.sub("", text))


def templates() -> list[Path]:
    found = sorted(TEMPLATES.glob("*.html"))
    assert found, "no templates were found; the path is wrong"
    return found


def has_inline_block(text: str) -> bool:
    for attrs in SCRIPT_TAG.findall(text):
        if "src=" in attrs or 'type="application/json"' in attrs:
            continue
        return True
    return bool(STYLE_TAG.search(text))


class NothingInMarkupRunsTests(unittest.TestCase):
    def assert_absent(self, pattern: re.Pattern, what: str):
        for path in templates():
            with self.subTest(template=path.name):
                match = pattern.search(source(path))
                self.assertIsNone(
                    match, f"{path.name} carries {what} ({match and match.group(0)!r}). "
                    "The Content-Security-Policy refuses it (app/csp.py): put a data- "
                    "attribute on the element and listen for it on `document` from the "
                    "file that owns the behaviour.")

    def test_no_template_carries_an_event_handler_attribute(self):
        self.assert_absent(HANDLER, "an event-handler attribute")

    def test_no_template_carries_a_style_attribute(self):
        # A per-render value travels as a data- attribute that style.css reads
        # with typed attr() — see the day placeholder and the ranker's tiers.
        self.assert_absent(STYLE_ATTR, "a style attribute")

    def test_no_template_asks_htmx_to_evaluate_anything(self):
        for pattern in (EVAL_FILTER, EVAL_ON, EVAL_JS):
            self.assert_absent(pattern, "an htmx expression built with the Function constructor")


class OnlyTheListedInlineBlocksTests(unittest.TestCase):
    def test_an_inline_block_appears_only_in_a_listed_template(self):
        for path in templates():
            if path.name in csp.INLINE_BLOCKS:
                continue
            with self.subTest(template=path.name):
                self.assertFalse(
                    has_inline_block(source(path)),
                    f"{path.name} carries an inline <script> or <style>. Put it in a "
                    "file, or — if it cannot be one — add the template to "
                    "csp.INLINE_BLOCKS with the reason.")

    def test_every_listed_template_still_carries_one(self):
        # A name left on the list after its block moved out is a hash admitting
        # nothing today and whatever that template grows tomorrow.
        for name in csp.INLINE_BLOCKS:
            with self.subTest(template=name):
                self.assertTrue(has_inline_block(source(TEMPLATES / name)))

    def test_a_listed_block_carries_no_attributes(self):
        # csp.py hashes bare <script> and <style> tags only; one given an
        # attribute would drop out of the header and be refused.
        for name in csp.INLINE_BLOCKS:
            text = source(TEMPLATES / name)
            with self.subTest(template=name):
                for attrs in SCRIPT_TAG.findall(text):
                    if "src=" not in attrs and 'type="application/json"' not in attrs:
                        self.assertEqual(attrs.strip(), "")
                self.assertNotRegex(text, r"<style\s+[^>]")


class ChangelogMarkupTests(unittest.TestCase):
    """CHANGELOG.md is real content a maintainer edits, not a template, so the
    scan above — which reads app/templates — cannot see what it renders. It
    goes through markdown-it instead (app/changelog.py), and markdown-it's
    table extension renders an aligned column as
    `<th style="text-align:left">`: the same style-src refusal as a template's
    style attribute, from a source this file otherwise never looks at.
    """

    def test_the_rendered_changelog_carries_no_refused_markup(self):
        text = changelog.CHANGELOG_PATH.read_text(encoding="utf-8")
        for release in changelog.parse(text):
            for pattern, what in ((STYLE_ATTR, "a style attribute"), (HANDLER, "an event-handler attribute")):
                match = pattern.search(release.html)
                self.assertIsNone(
                    match,
                    f"{release.version} renders {what} ({match and match.group(0)!r}) via "
                    "markdown-it. The Content-Security-Policy refuses it (app/csp.py) — for "
                    "a style attribute, an aligned table column (`:---`/`---:`) is the known "
                    "way markdown-it produces one; CHANGELOG.md must not use column alignment.")


class ServedBlocksMatchTheHeaderTests(AppTestCase):
    def assert_admitted(self, resp):
        header = resp.headers["Content-Security-Policy"]
        directives = {part.split()[0]: part.split()[1:] for part in header.split("; ")}
        blocks = csp.inline_blocks(resp.text)
        self.assertTrue(blocks, "the response carried no inline block to check")
        for tag, text in blocks:
            with self.subTest(tag=tag):
                self.assertIn(csp.source_hash(text), directives[f"{tag}-src"])

    def test_the_tracker_reveal_on_a_signed_in_page(self):
        # The rankings page carries the shared header, and with it the reveal,
        # for every account it admits.
        self.sign_in_as(self.make_user("ranker", ranker_approved=True))
        resp = self.client.get("/rankings")
        self.assertEqual(resp.status_code, 200)
        self.assert_admitted(resp)

    def test_the_error_page_styles_and_music(self):
        self.sign_in_as(self.make_user("viewer", calendar_approved=True))
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("lobby_audio", resp.text)
        self.assert_admitted(resp)


if __name__ == "__main__":
    unittest.main()
