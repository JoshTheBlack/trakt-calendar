"""The shared <head> macro, the asset registry it renders from, and the
app-wide invariants that make a boosted navigation safe.

Every page swaps only <body>, so the <head> a session's FIRST page loaded is the
one every later page has to work inside. That turns "all the heads agree" from a
tidiness preference into a correctness property, and these tests are what keeps
it true — none of it fails visibly until somebody boosts between two pages that
disagreed, and by then the symptom is an unstyled page rather than a diff.

No network, no database, no client: this reads templates and rendered markup.

Run: ./.venv/Scripts/python.exe -m pytest tests/test_page_head.py -q
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TRAKT_DATA_DIR", tempfile.mkdtemp(prefix="tns-head-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import assets  # noqa: E402
from app.templating import TEMPLATES_DIR, templates  # noqa: E402

# error_lobby.html is the one page that deliberately does NOT use the macro: it
# inlines its styles because it has to render when something else is already
# wrong, and a 404 for the stylesheet would leave a white screen and no way home.
STANDALONE = {"error_lobby.html"}

PAGES = sorted(
    p.name for p in TEMPLATES_DIR.glob("*.html")
    if not p.name.startswith("_") and p.name not in STANDALONE
)


def read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def markup(name: str) -> str:
    """The template with its Jinja comments removed. Several of them quote the
    very tags these tests scan for — index.html's explains what rides on <body>
    and what is inside one <form> — and a comment is not markup."""
    return re.sub(r"\{#.*?#\}", "", read(name), flags=re.S)


class AssetRegistryTests(unittest.TestCase):
    def test_url_carries_the_cache_busting_token(self):
        self.assertEqual(assets.url("static/js/app.js"),
                         f"/static/js/app.js?v={assets.ASSET_VERSION}")

    def test_url_refuses_a_file_it_does_not_track(self):
        """The whole point of the list is that a `?v=` means the token really
        covers that file. Serving one it does not track looks busted and never
        busts, so the page fails to render instead."""
        with self.assertRaises(KeyError):
            assets.url("static/js/not-registered.js")

    def test_every_registered_asset_exists_on_disk(self):
        for name in assets._CACHED_ASSETS:
            with self.subTest(asset=name):
                self.assertTrue((assets.BASE_DIR / name).exists(), name)

    def test_the_preloaded_fonts_exist_and_are_deliberately_untokened(self):
        """A woff2's version is in its filename, so there is nothing to bust —
        and tracking them would move the token every time a face is added."""
        for name in assets.FONT_PRELOADS:
            with self.subTest(font=name):
                self.assertTrue((assets.BASE_DIR / name).exists(), name)
                self.assertNotIn(name, assets._CACHED_ASSETS)
                self.assertEqual(assets.font_url(name), f"/{name}")

    def test_the_stylesheet_is_one_bundled_file(self):
        self.assertEqual(assets.STYLESHEET, "static/css/style.css")
        sheets = [n for n in assets._CACHED_ASSETS if n.endswith(".css")]
        self.assertEqual(sheets, [assets.STYLESHEET])


class HeadMacroTests(unittest.TestCase):
    """Rendered output, not the template source — a macro that compiles but
    renders the wrong tags passes a source grep."""

    def render(self, **kwargs) -> str:
        source = "{% import '_head.html' as page with context %}{{ page.head(" + kwargs.pop("call") + ") }}"
        return templates.env.from_string(source).render(**kwargs)

    def test_it_renders_the_bundled_stylesheet_and_every_font(self):
        html = self.render(call="'Title'")
        self.assertEqual(re.findall(r'rel="stylesheet" href="([^"]+)"', html),
                         [assets.url(assets.STYLESHEET)])
        self.assertEqual(re.findall(r'rel="preload" href="([^"]+)"', html),
                         [assets.font_url(f) for f in assets.FONT_PRELOADS])

    def test_the_stylesheet_is_requested_before_the_font_preloads(self):
        """Order matters: the stylesheet is the only resource gating first paint,
        and the ~86 KB of high-priority font preloads would compete with it for
        the connection pool on a cold load."""
        html = self.render(call="'Title'")
        self.assertLess(html.index('rel="stylesheet"'), html.index('rel="preload"'))

    def test_htmx_ships_on_every_page_and_ships_first(self):
        """A page without htmx is a one-way door out of a boosted session: you
        can arrive there boosted, but every link on it is then a full reload."""
        html = self.render(call="'Title', scripts=('static/js/app.js',)")
        srcs = re.findall(r'<script src="([^"]+)"', html)
        self.assertEqual(srcs, [assets.url("static/js/htmx.min.js"),
                                assets.url("static/js/app.js")])

    def test_every_script_is_deferred(self):
        html = self.render(call="'Title', scripts=('static/js/nav.js',)")
        for tag in re.findall(r"<script src=[^>]+>", html):
            with self.subTest(tag=tag):
                self.assertIn(" defer", tag)

    def test_the_site_name_comes_from_the_macro(self):
        self.assertIn("<title>Account &ndash; Trakt New Shows</title>",
                      self.render(call="'Account'"))

    def test_a_page_carrying_its_own_subject_opts_out_of_the_suffix(self):
        self.assertIn("<title>New Shows – July</title>",
                      self.render(call="'New Shows – July', site_suffix=false"))

    def test_the_tracker_reveal_script_can_be_gated_off(self):
        """The account page must not mention the tracker to an account without
        the grant, and the reveal script names it."""
        self.assertIn("distraktRevealed", self.render(call="'Account'"))
        self.assertNotIn("distraktRevealed", self.render(call="'Account', nav_head=false"))

    def test_a_script_the_asset_list_does_not_track_fails_the_render(self):
        with self.assertRaises(KeyError):
            self.render(call="'Title', scripts=('static/js/ghost.js',)")


class OpenGraphMacroTests(unittest.TestCase):
    def render(self, **ctx) -> str:
        source = ("{% import '_head.html' as page with context %}"
                  "{{ page.og('A title', 'A description') }}")
        return templates.env.from_string(source).render(**ctx)

    def test_the_absolute_tags_appear_only_with_a_public_base_url(self):
        """og:url and og:image have to be absolute, so they are omitted rather
        than emitted relative when the instance has no base URL to build them
        from — and the twitter card drops to `summary` with no image to show."""
        without = self.render()
        self.assertNotIn("og:url", without)
        self.assertNotIn("og:image", without)
        self.assertIn('name="twitter:card" content="summary"', without)

        with_url = self.render(og_url="https://example.test/x", og_image="https://example.test/i.png")
        self.assertIn('content="https://example.test/x"', with_url)
        self.assertIn('name="twitter:card" content="summary_large_image"', with_url)

    def test_it_reads_the_route_context_rather_than_taking_it_as_an_argument(self):
        """Imported `with context`, which is what lets four pages pass only the
        two strings that actually differ between them. A plain import would drop
        both tags silently instead of failing."""
        self.assertIn("https://example.test/x", self.render(og_url="https://example.test/x"))


class EveryPageAgreesTests(unittest.TestCase):
    """The invariants that make a body swap between any two pages land intact."""

    def test_every_page_renders_its_head_through_the_macro(self):
        for name in PAGES:
            with self.subTest(page=name):
                self.assertIn("page.head(", read(name),
                              f"{name} builds its own <head>")

    def test_no_page_writes_an_asset_path_of_its_own(self):
        """Asset filenames live in app/assets.py. A template restating one is a
        second place to edit when a file is renamed, and the copy does not
        announce itself when it goes stale."""
        for name in PAGES:
            with self.subTest(page=name):
                found = re.findall(r'(?:href|src)="(/static/(?:css|js)/[^"]+)"', read(name))
                self.assertEqual(found, [], f"{name} writes asset paths directly")

    def test_every_page_boosts_from_its_body(self):
        for name in PAGES:
            with self.subTest(page=name):
                body = re.search(r"<body[^>]*>", markup(name))
                self.assertIsNotNone(body, f"{name} has no <body> at the top level")
                self.assertIn('hx-boost="true"', body.group(0), name)

    def test_an_inline_body_script_that_declares_anything_is_wrapped(self):
        """htmx re-executes scripts inside the swapped body, so a top-level
        `const` becomes a redeclaration on the second arrival and the whole
        script fails to parse — leaving that page's controls bound to nothing.
        A function wrapper is what makes a second arrival safe; `function` and
        `var` declarations are legal to repeat and need no wrapper."""
        for name in PAGES:
            source = read(name)
            body_at = source.index("\n<body")
            for match in re.finditer(r"<script>\n(.*?)</script>", source, re.S):
                if match.start() < body_at:
                    continue
                script = match.group(1)
                if not re.search(r"^(const|let|class)\s+[A-Za-z_$]", script, re.M):
                    continue
                # Comments first, then the wrapper: nothing else may precede it.
                code = re.sub(r"^\s*//[^\n]*\n", "", script, flags=re.M).lstrip()
                with self.subTest(page=name):
                    self.assertTrue(code.startswith(("(function", "(()")),
                                    f"{name}: this script declares names and is not wrapped")
                    self.assertIn("})();", script, name)

    def test_a_form_that_is_not_a_navigation_opts_out_of_boosting(self):
        """htmx boosts every form under a boosted <body>. The app's real
        navigations are GET forms whose query the route already reads; every
        other form here is submitted by JS and writes over the API, so a boost
        would turn a save into an attempted navigation."""
        for name in PAGES:
            for tag in re.findall(r"<form[^>]*>", markup(name)):
                if 'method="get"' in tag:
                    continue
                with self.subTest(page=name, tag=tag[:60]):
                    self.assertIn('hx-boost="false"', tag)

    def test_the_download_links_opt_out_of_boosting(self):
        """htmx boosts every link under a boosted <body> and ignores `download`,
        so a boosted export would swap its JSON into the page instead of saving
        a file."""
        for name in PAGES:
            source = read(name)
            for tag in re.findall(r"<a [^>]*\bdownload\b[^>]*>", source):
                with self.subTest(page=name, tag=tag[:60]):
                    self.assertIn('hx-boost="false"', tag)


class BundledStylesheetTests(unittest.TestCase):
    def setUp(self):
        self.css = (assets.BASE_DIR / assets.STYLESHEET).read_text(encoding="utf-8")

    def test_the_per_page_stylesheets_are_gone(self):
        for name in ("auth.css", "distrakt.css", "ranker.css", "share.css"):
            with self.subTest(sheet=name):
                self.assertFalse((assets.BASE_DIR / "static/css" / name).exists(), name)

    def test_each_page_family_survived_the_merge(self):
        """One representative selector per merged file, so a section dropped
        wholesale is caught rather than showing up as an unstyled page."""
        for selector in (".auth-shell", ".distrakt-brand", ".ranker-shell", ".share-owner"):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)

    def test_the_duplicated_danger_button_is_defined_once(self):
        """Two files carried `.btn-danger` with identical base rules and hover
        rules that differed. Bundled, both would apply everywhere; the narrower
        hover is the one kept."""
        self.assertEqual(len(re.findall(r"^\.btn-danger \{", self.css, re.M)), 1)
        self.assertIn(".btn-danger:hover:not(:disabled)", self.css)
        self.assertNotIn(".btn-danger:hover {", self.css)


if __name__ == "__main__":
    unittest.main()
