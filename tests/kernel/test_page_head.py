"""The shared <head> macro, the asset registry it renders from, and the
app-wide invariants that make a boosted navigation safe.

Every page swaps only <body>, so the <head> a session's FIRST page loaded is the
one every later page has to work inside. That turns "all the heads agree" from a
tidiness preference into a correctness property, and these tests are what keeps
it true — none of it fails visibly until somebody boosts between two pages that
disagreed, and by then the symptom is an unstyled page rather than a diff.

No network, no database, no client: this reads templates and rendered markup.
"""
from __future__ import annotations

import re
import unittest

from app import assets
from app.templating import TEMPLATES_DIR, templates

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
        self.assertEqual(assets.url("static/js/ui.js"),
                         f"/static/js/ui.js?v={assets.ASSET_VERSION}")

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
        html = self.render(call="'Title', scripts=('static/js/ui.js',)")
        srcs = re.findall(r'<script src="([^"]+)"', html)
        # htmx, then the loader that fetches a page's own scripts on a boosted
        # arrival, then this page's.
        self.assertEqual(srcs, [assets.url(assets.HTMX), assets.url(assets.BOOST),
                                assets.url("static/js/ui.js")])

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
    def render(self, *, image_alt=None, **ctx) -> str:
        alt = f", image_alt={image_alt!r}" if image_alt else ""
        source = ("{% import '_head.html' as page with context %}"
                  "{{ page.og('A title', 'A description'" + alt + ") }}")
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

    def test_an_image_is_advertised_with_its_size(self):
        """Without them Slack and Discord size the picture themselves, and one
        that guesses wrong renders a thumbnail rather than the wide card the
        `summary_large_image` claim beside it just promised."""
        markup = self.render(og_image="https://example.test/i.jpg",
                             og_image_w=1200, og_image_h=630)
        self.assertIn('property="og:image:width" content="1200"', markup)
        self.assertIn('property="og:image:height" content="630"', markup)

    def test_a_size_is_never_emitted_without_the_image_it_describes(self):
        """A width for a picture that is not there is a contradiction rather
        than a hint, and the pages with no configured base URL are exactly the
        ones that would carry it."""
        markup = self.render(og_image_w=1200, og_image_h=630)
        self.assertNotIn("og:image", markup)

    def test_alt_text_rides_along_when_a_page_supplies_it(self):
        markup = self.render(og_image="https://example.test/i.jpg",
                             image_alt="A preview card for August 2026")
        self.assertIn('property="og:image:alt" content="A preview card for August 2026"', markup)
        self.assertIn('name="twitter:image:alt"', markup)

    def test_the_environment_escapes_what_it_interpolates(self):
        """The property every assertion below rests on, checked directly rather
        than assumed: the shared environment autoescapes, so a string reaching a
        template is escaped at the point it is written out."""
        self.assertTrue(templates.env.autoescape)

    def test_untrusted_text_cannot_escape_a_meta_attribute(self):
        """og:title carries the share owner's chosen display name, which is free
        text they typed and which lands inside a double-quoted attribute. A
        quote that closed the attribute would let the owner put markup of their
        choosing into every crawler's copy of the page — including one embedding
        somebody else's calendar, since a page is only ever fetched by strangers.
        """
        source = ("{% import '_head.html' as page with context %}"
                  "{{ page.og(title, description) }}")
        html = templates.env.from_string(source).render(
            title='" onmouseover="x', description="<b>bold</b>")
        self.assertIn('<meta property="og:title" content="&#34; onmouseover=&#34;x">', html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", html)
        self.assertNotIn("<b>", html)

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

    def test_no_page_carries_styling_of_its_own_in_its_head(self):
        """A page's own <style> applies only when that page is the one the
        browser loaded cold. Reached by a click, the swap replaces <body> and
        leaves <head> exactly as the session's first page left it — so the rules
        never exist, and a page whose whole layout was in that block arrives with
        no styling at all. Page CSS goes in the bundled stylesheet, which every
        <head> links; error_lobby.html is the one exception and is excluded above
        for a reason that does not generalize."""
        for name in PAGES:
            source = markup(name)
            with self.subTest(page=name):
                self.assertNotIn("<style", source[:source.index("<body")],
                                 f"{name} carries CSS a boosted arrival will not apply")

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

    def test_a_link_that_leaves_the_app_opts_out_of_boosting(self):
        """A boosted link is an XHR, and an XHR cannot follow a redirect to
        another origin — the browser's preflight refuses it over the `hx-boosted`
        header and the click does nothing at all, silently.

        These are the routes that hand the browser to a provider's own approval
        screen, so every link to one has to be a real navigation. Enumerated
        rather than pattern-matched: the property is "this route ends up
        somewhere else", which a URL cannot be read off.
        """
        for name in PAGES:
            source = read(name)
            for path in ("/auth/trakt/start", "/auth/trakt/link", "/auth/plex/start"):
                for tag in re.findall(rf'<a [^>]*href="{re.escape(path)}[^"]*"[^>]*>', source):
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


VENDORED = ("htmx.min.js", "sortable.min.js")

JS_DIR = assets.BASE_DIR / "static/js"

# A page's <head> is built from assets.scripts('<page>'), so this is how a
# template says which bundle it loads.
PAGE_KEY = re.compile(r"assets\.scripts\('([^']+)'\)")


def js_source(name: str) -> str:
    return (assets.BASE_DIR / name).read_text(encoding="utf-8")


# `onclick="if(event.target===this) closeSettings()"` is the app's click-outside
# idiom, so an attribute's calls are not all function calls of ours.
JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "typeof", "new",
               "do", "else", "function", "await", "delete", "void"}

HANDLER_ATTR = re.compile(r"""\bon[a-z]+=(["'])(.*?)\1""", re.S)
# Not preceded by a dot: `this.form.requestSubmit()` is the DOM's, not ours.
CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")


def handlers_in(source: str) -> set[str]:
    """Every function an on*= attribute in `source` calls."""
    called = set()
    for _, value in HANDLER_ATTR.findall(source):
        called.update(CALL.findall(value))
    return called - JS_KEYWORDS


def declares(source: str) -> set[str]:
    """Every name a script declares at TOP LEVEL — the ones that land in the one
    scope a page's scripts share. Indented declarations are inside something and
    cannot collide."""
    names = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", source, re.M))
    for group in re.findall(r"^(?:let|const|var)\s+([^=;\n]+)", source, re.M):
        names.update(part.strip() for part in group.split(",") if part.strip().isidentifier())
    return names


class PageScriptTests(unittest.TestCase):
    """The three ways splitting one page script into many can break silently.

    They are ordinary scripts sharing one global scope (see assets.PAGE_SCRIPTS
    for why they are not modules), so a page is only correct if its files agree
    about names, its order lets each file see what it calls, and every function an
    inline handler names is actually on the page.
    """

    def page_scripts(self, page: str) -> list[str]:
        return [n for n in assets.scripts(page)
                if not n.endswith(VENDORED)]

    def every_script(self) -> list[str]:
        """Every first-party script, each once, plus the loader the head macro
        puts on every page — it shares the same global scope as all of them."""
        names = [assets.BOOST]
        for page in assets.PAGE_SCRIPTS:
            names.extend(n for n in self.page_scripts(page) if n not in names)
        return names

    def test_every_script_on_disk_is_loaded_by_some_page(self):
        """A file nothing loads is dead code that still looks alive. Splitting a
        page script into a directory is exactly when one gets left behind."""
        loaded = {n for scripts in assets.PAGE_SCRIPTS.values() for n in scripts}
        loaded.update({assets.HTMX, assets.BOOST})  # the head macro's own two
        for path in sorted(JS_DIR.rglob("*.js")):
            name = path.relative_to(assets.BASE_DIR).as_posix()
            if path.name in VENDORED:
                continue
            with self.subTest(script=name):
                self.assertIn(name, loaded, f"{name} is served but no page loads it")

    def test_each_page_loads_its_boot_file_last(self):
        """boot.js runs an init the moment it executes, so everything it calls has
        to have been declared by an earlier file."""
        for page, scripts in assets.PAGE_SCRIPTS.items():
            boots = [n for n in scripts if n.endswith("/boot.js")]
            if not boots:
                continue
            with self.subTest(page=page):
                self.assertEqual(boots, [scripts[-1]],
                                 f"{page}: boot.js must be the last script")

    def test_no_two_scripts_anywhere_declare_the_same_name(self):
        """One name, one file — across every page, not just within one.

        A boosted navigation leaves the scripts it has already run in place and
        adds the next page's beside them, so any two pages can end up sharing one
        global scope. A second top-level `let`/`const` of a name is then a
        SyntaxError that stops that whole file executing, every function in it
        silently missing; a second `function` of one is worse, because it is legal
        — whichever page was visited last quietly owns the name, and the button
        that calls it does the other page's job.
        """
        owner: dict[str, str] = {}
        for name in self.every_script():
            for declared in declares(js_source(name)):
                with self.subTest(name=declared):
                    # assertNotIn would print the whole name table.
                    if declared in owner:
                        self.fail(f"{declared} is declared at top level by both "
                                  f"{owner[declared]} and {name}")
                owner[declared] = name

    def test_every_page_states_its_body_class_inside_its_body(self):
        """A swap replaces the body's CHILDREN, so the <body> element keeps the
        previous page's classes. #pageMeta is inside the swapped region and so
        arrives with the new page; the class it carries has to be the one the
        <body> tag renders, or a boosted arrival wears something the cold load
        never would."""
        for template in PAGES:
            source = markup(template)
            body = re.search(r"<body[^>]*>", source)
            meta = re.search(r'<div id="pageMeta"[^>]*>', source)
            with self.subTest(page=template):
                self.assertIsNotNone(meta, f"{template} has no #pageMeta, so a "
                                           "boosted arrival keeps the previous "
                                           "page's body classes")
                self.assertIn('class="{{ body_class }}"', body.group(0), template)
                self.assertIn('data-body-class="{{ body_class }}"', meta.group(0), template)

    def test_a_page_with_scripts_names_itself_on_its_meta_element(self):
        """data-page is how a boosted arrival is told which scripts to load: the
        <head> is never swapped, so the page landed on has to say what it needs
        from inside the swapped region. A page whose template loads scripts but
        does not name itself lands inert."""
        for template in PAGES:
            page = PAGE_KEY.search(read(template))
            meta = re.search(r'<div id="pageMeta"[^>]*>', markup(template)).group(0)
            declared = re.search(r'data-page="([^"]*)"', meta)
            with self.subTest(page=template):
                if not page:
                    self.assertIsNone(declared, f"{template} names a page but loads "
                                                "no scripts")
                    continue
                self.assertIsNotNone(declared, f"{template} loads scripts but its "
                                               "#pageMeta carries no data-page")
                self.assertEqual(declared.group(1), page.group(1),
                                 f"{template} asks for one page's scripts and names "
                                 "another")

    def test_every_page_with_an_init_registers_it_with_the_loader(self):
        """boot.js used to run its init from DOMContentLoaded, which a boosted
        arrival never fires. The loader calls the registered init instead — on the
        cold load and on every arrival — so a boot file that still waits for the
        event would only ever run on a full page load."""
        for page in assets.PAGE_SCRIPTS:
            boots = [n for n in assets.scripts(page) if n.endswith("/boot.js")]
            for name in boots:
                # Comments stripped: both files explain in prose why they no
                # longer wait for the event.
                source = re.sub(r"//[^\n]*", "", js_source(name))
                with self.subTest(page=page):
                    self.assertIn(f"registerPage('{page}'", source,
                                  f"{name} does not register an init for {page}")
                    self.assertNotIn("DOMContentLoaded", source,
                                     f"{name} still waits for DOMContentLoaded, which "
                                     "a boosted arrival does not fire")

    def test_every_function_an_inline_handler_calls_is_on_that_page(self):
        """The markup calls these functions from onclick/onerror attributes, which
        reach the global scope only. A page whose script list is missing the file
        that declares one looks fine until somebody clicks — so the handlers are
        checked against what the page actually loads. Handlers in markup the page
        BUILDS in the browser count too: they are the same promise made from a
        template literal instead of a template."""
        for template in PAGES:
            page = PAGE_KEY.search(read(template))
            scripts = self.page_scripts(page.group(1)) if page else []
            sources = [js_source(n) for n in scripts]
            available = set()
            for source in sources:
                available |= declares(source)
            # A page's own inline <script> is part of the page too. Scanned
            # unanchored, since an inline script's declarations are indented — it
            # over-counts nested names, which can only ever hide a problem here,
            # never invent one.
            for inline in re.findall(r"<script>(.*?)</script>", read(template), re.S):
                available |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", inline))
            called = set()
            for source in [markup(template), *sources]:
                called |= handlers_in(source)
            for name in sorted(called - available):
                with self.subTest(page=template, handler=name):
                    self.fail(f"{template} calls {name}() from an attribute, but no "
                              f"script it loads declares it")


class LateArrivingDayBlockTests(unittest.TestCase):
    """What a day block that fetches itself in gets, and how.

    The calendar's swap handler splits on purpose: a boosted arrival re-runs the
    page init, a CONTENT swap must not — re-running it would re-arm listeners and
    redo one-time setup. So everything a late block needs has to be named in the
    content-swap branch explicitly, and anything it names has to be able to work
    on one subtree rather than the whole document.
    """

    def setUp(self):
        self.boot = js_source("static/js/calendar/boot.js")
        self.arr = js_source("static/js/calendar/arr-buttons.js")

    def content_swap_branch(self) -> str:
        return self.boot.split("function applyViewStateTo(root)")[1].split("\n}")[0]

    def test_a_swapped_in_block_gets_its_sonarr_radarr_seerr_marks(self):
        """Without this the buttons on a late day showed as plain Adds — for a
        title already in the library as much as for one that is not — until the
        60s poll's next whole-document sweep happened to catch up."""
        self.assertIn("applyArrStateTo(root)", self.content_swap_branch())

    def test_the_content_swap_does_not_reach_for_the_page_init(self):
        self.assertNotIn("initCalendarPage", self.content_swap_branch())

    def test_the_arr_appliers_walk_from_a_root_rather_than_the_document(self):
        """Hard-wired to `document`, neither could be pointed at the block that
        just landed, which is the only cheap way to mark it."""
        self.assertIn("function applyLibraryStatus(root = document)", self.arr)
        self.assertIn("function applyArrStatus(root = document)", self.arr)
        self.assertNotIn("document.querySelectorAll('.arr-btn')", self.arr)


class BundledStylesheetTests(unittest.TestCase):
    def setUp(self):
        self.css = (assets.BASE_DIR / assets.STYLESHEET).read_text(encoding="utf-8")

    def test_the_per_page_stylesheets_are_gone(self):
        for name in ("auth.css", "distrakt.css", "ranker.css", "share.css"):
            with self.subTest(sheet=name):
                self.assertFalse((assets.BASE_DIR / "static/css" / name).exists(), name)

    def test_each_page_family_survived_the_merge(self):
        """One representative selector per merged file, so a section dropped
        wholesale is caught rather than showing up as an unstyled page.
        `.month-grid` is the picker's, which reached the bundle later and by the
        same argument: it was the last page styling itself in its own <head>."""
        for selector in (".auth-shell", ".distrakt-brand", ".ranker-shell", ".share-owner",
                         ".month-grid"):
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
