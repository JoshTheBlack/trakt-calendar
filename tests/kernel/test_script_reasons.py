"""Adding JavaScript to this app is a claim, and this is where it has to be written.

CLAUDE.md's rule is that script is for what ONLY script can do, and that the
answer to "why can this not be markup, a stylesheet rule, a Jinja template or a
route" goes in a comment beside whatever survives. A rule like that decays the
moment it is only a rule: the cost of adding a file is one line in
app/assets.py's APP_SCRIPTS, and nothing made the person adding it say why.

So every first-party script — a .js file, or an inline <script> a template
carries — must OPEN with prose that engages with that question. Not a label: a
comment naming the file passes nothing here. It has to mention at least two of
the things it is saying it could not have been, because "this is the browser's"
is a claim you can only check against the alternatives it rules out.

WHAT THIS CANNOT DO, said plainly so nobody mistakes it for more than it is: it
cannot tell a true reason from a false one. A determined author can write a
paragraph of nonsense and pass. What it CAN do is make adding script a visible,
deliberate act with an argument attached — which is what
tests/kernel/test_page_head.py already does for the asset list, one level up.

JSON IS DATA, NOT CODE. A script block carrying `type="application/json"` is how
the server hands the page a value, and the two templates that have one are not
running anything. They are exempt BY THAT TYPE ATTRIBUTE rather than by name, so
a third needs no edit here and one that drops the type is caught.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from app import assets

JS_DIR = assets.BASE_DIR / "static" / "js"
TEMPLATES = assets.BASE_DIR / "templates"

# Vendored libraries are somebody else's code and carry somebody else's header.
# Named rather than pattern-matched: "is this ours" is a fact about where a file
# came from, and a filename cannot be asked it.
VENDORED = {"htmx.min.js", "htmx-json-enc.js", "sortable.min.js"}

# The alternatives CLAUDE.md says to rule out, each with the spellings this
# codebase actually uses for it. A reason has to engage with at least TWO — one
# is an assertion, two is an argument.
ALTERNATIVES = {
    "markup": ("markup", "attribute", "html", "<button", "<dialog", "<details", "popover"),
    "stylesheet": ("stylesheet", "css", "style.css", "selector"),
    "template": ("template", "jinja", ".html", "fragment", "partial"),
    "server": ("server", "route", "hx-", "htmx", "request", "response"),
    "browser": ("browser", "pointer", "clipboard", "keyboard", "measur",
                "local file", "top layer", "focus"),
}

MIN_REASON_CHARS = 200


def first_comment(source: str) -> str:
    """The comment block a JavaScript file opens with, or "" if it opens with code."""
    text = source.lstrip()
    if text.startswith("/*"):
        return text[: text.find("*/") + 2] if "*/" in text else text
    if not text.startswith("//"):
        return ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or not stripped:
            lines.append(stripped)
            continue
        break
    return "\n".join(lines)


def engaged_with(reason: str) -> set[str]:
    """Which of the alternatives this prose actually mentions."""
    lowered = reason.lower()
    return {name for name, spellings in ALTERNATIVES.items()
            if any(word in lowered for word in spellings)}


class EveryFirstPartyScriptStatesItsReasonTests(unittest.TestCase):
    def files(self) -> list[Path]:
        found = [p for p in sorted(JS_DIR.rglob("*.js")) if p.name not in VENDORED]
        self.assertTrue(found, "no first-party scripts were found; the path is wrong")
        return found

    def test_every_file_opens_with_a_comment(self):
        for path in self.files():
            with self.subTest(script=path.name):
                self.assertNotEqual(
                    first_comment(path.read_text(encoding="utf-8")), "",
                    f"{path.name} opens with code. Every script in this app opens "
                    "with the reason it cannot be markup, CSS, Jinja or a route.")

    def test_every_reason_is_prose_rather_than_a_label(self):
        for path in self.files():
            reason = first_comment(path.read_text(encoding="utf-8"))
            with self.subTest(script=path.name):
                self.assertGreaterEqual(
                    len(reason), MIN_REASON_CHARS,
                    f"{path.name}'s opening comment is {len(reason)} characters. "
                    "A name is not a reason.")

    def test_every_reason_rules_out_at_least_two_alternatives(self):
        for path in self.files():
            reason = first_comment(path.read_text(encoding="utf-8"))
            with self.subTest(script=path.name):
                met = engaged_with(reason)
                self.assertGreaterEqual(
                    len(met), 2,
                    f"{path.name}'s opening comment mentions {sorted(met) or 'none'} of "
                    f"{sorted(ALTERNATIVES)}. Say what it could not have been instead.")

    def test_every_file_is_declared_in_the_app_list(self):
        """A script nothing serves is a script nobody reviewed the need for, and
        the app list is every script there is — every signed-in page loads all
        of it, so a file in it is a file every one of those pages pays for."""
        for path in self.files():
            served = path.relative_to(assets.BASE_DIR).as_posix()
            with self.subTest(script=path.name):
                self.assertIn(served, assets.APP_SCRIPTS,
                              f"{served} is on disk but no list loads it")


# Jinja comments are stripped FIRST and deliberately: _base.html carries one that
# mentions a script tag, and a scanner that pairs an opening tag with the next
# closing one reads seven lines of prose as script.
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)


def inline_scripts(path: Path):
    """(attrs, body, the prose before it) for each <script> with a body."""
    raw = path.read_text(encoding="utf-8")
    for match in SCRIPT.finditer(JINJA_COMMENT.sub("", raw)):
        attrs, body = match.group(1), match.group(2)
        if not body.strip():
            continue
        # The comment before it, read from the ORIGINAL text: the prose this test
        # is about is exactly what the strip above removed.
        before = raw.split(f"<script{attrs}>")[0]
        comments = JINJA_COMMENT.findall(before)
        yield attrs, body, (comments[-1] if comments else "")


class EveryInlineScriptStatesItsReasonTests(unittest.TestCase):
    def blocks(self):
        for path in sorted(TEMPLATES.rglob("*.html")):
            for attrs, body, reason in inline_scripts(path):
                if "application/json" in attrs:
                    continue          # data the server handed the page, not code
                yield path, body, reason

    def test_there_are_inline_scripts_to_check(self):
        """A guard on the scanner rather than on the app: if this ever reaches
        zero legitimately, delete this class rather than letting the two below
        pass by finding nothing to look at."""
        self.assertTrue(list(self.blocks()))

    def test_each_is_preceded_by_prose_that_rules_out_two_alternatives(self):
        for path, _body, reason in self.blocks():
            with self.subTest(template=path.name):
                self.assertGreaterEqual(
                    len(reason), MIN_REASON_CHARS,
                    f"{path.name} carries an inline <script> with no reason before it.")
                met = engaged_with(reason)
                self.assertGreaterEqual(
                    len(met), 2,
                    f"{path.name}'s script comment mentions {sorted(met) or 'none'} of "
                    f"{sorted(ALTERNATIVES)}.")

    def test_a_json_block_is_data_and_says_so_in_its_type(self):
        """The exemption is the type attribute, so a block that drops it is
        caught rather than inheriting a pass by sitting in the same file."""
        seen = 0
        for path in sorted(TEMPLATES.rglob("*.html")):
            for attrs, body, _ in inline_scripts(path):
                if "application/json" not in attrs:
                    continue
                seen += 1
                with self.subTest(template=path.name):
                    self.assertNotIn("function", body)
                    self.assertNotIn("=>", body)
        self.assertGreater(seen, 0, "no JSON blocks found; the scanner is wrong")


if __name__ == "__main__":
    unittest.main()
