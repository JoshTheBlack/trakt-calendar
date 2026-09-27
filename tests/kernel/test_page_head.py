"""The base template every page extends, the asset registry it renders from, and
the app-wide invariants that make a boosted navigation safe.

Every page swaps only <body>, so the <head> a session's FIRST page loaded is the
one every later page has to work inside. That turns "all the heads agree" from a
tidiness preference into a correctness property. It is true by construction now —
there is one <head>, in templates/_base.html — so what these tests hold is that
every page is built on it, and that what it renders is right.

No network, no database, no client: this reads templates and rendered markup.
"""
from __future__ import annotations

import re
import unittest

from app import assets
from app import chrome
from app.templating import TEMPLATES_DIR, templates

# error_lobby.html is the one page that deliberately does NOT extend the base: it
# inlines its styles because it has to render when something else is already
# wrong, and a 404 for the stylesheet would leave a white screen and no way home.
STANDALONE = {"error_lobby.html"}

PAGES = sorted(
    p.name for p in TEMPLATES_DIR.glob("*.html")
    if not p.name.startswith("_") and p.name not in STANDALONE
)

BASE = "_base.html"


def read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def uncommented(name: str) -> str:
    return re.sub(r"\{#.*?#\}", "", read(name), flags=re.S)


def block(name: str, block_name: str) -> str:
    """What a page puts in one of the base's blocks, or "" when it does not
    override it."""
    found = re.search(rf"\{{%-?\s*block {block_name}\s*-?%\}}(.*?)\{{%-?\s*endblock",
                      uncommented(name), re.S)
    return found.group(1) if found else ""


def markup(name: str) -> str:
    """The template with its Jinja comments removed, and with the bodies of the
    partials it imports appended.

    COMMENTS GO because several of them quote the very tags these tests scan for
    — index.html's explains what rides on <body> and what is inside one <form>
    — and a comment is not markup.

    IMPORTS COME IN because an element a page is responsible for carrying may be
    DRAWN by a macro: the calendar's #pageMeta is, because the answer to changing
    a view preference re-draws it out of band and one element cannot be spelled
    in two files. Scanning the page alone would read that as the element being
    absent, which is the opposite of what moving it into one place achieved.
    """
    source = re.sub(r"\{#.*?#\}", "", read(name), flags=re.S)
    for imported in re.findall(r"\{%-?\s*import\s+'([^']+)'", source):
        path = TEMPLATES_DIR / imported
        if path.exists():
            source += re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"),
                             flags=re.S)
    return source


def expanded(name: str, seen=None) -> str:
    """A page and every partial it reaches, transitively, through both `import`
    and `include`.

    `markup` above follows imports one level, which is all it needs. This follows
    both and keeps going, because the thing being asked about is drawn three
    partials down: a calendar card is included by the month, which is included by
    the page.
    """
    seen = seen if seen is not None else set()
    if name in seen:
        return ""
    seen.add(name)
    path = TEMPLATES_DIR / name
    if not path.exists():
        return ""
    source = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)
    # EITHER QUOTE. This codebase writes imports with single quotes and includes
    # with double, and a pattern that knew only one found a single page.
    reached = re.findall(r"""\{%-?\s*(?:import|include)\s+['"]([^'"]+)['"]""", source)
    return source + "".join(expanded(child, seen) for child in reached)


class DetailsPanelShellTests(unittest.TestCase):
    """A surface that offers the details panel carries the panel.

    THE SHELL AND ITS TWO TEMPLATES TRAVEL TOGETHER. `data-details-open` on a card
    or a row is the whole of what an opener needs, which is what makes adding one
    to a new surface easy — and easy to do without the box it opens into.
    static/js/ui.js clones `detailsPending` and `detailsUnreachable` with NO guard
    on their being there, deliberately, so a surface that forgot them throws on
    the first click. This is what says so before a browser has to.

    THE FOUR ARE NAMED RATHER THAN DISCOVERED, and the tracker is why: its rows
    reach the page as a FRAGMENT it fetches, so nothing a reader can follow from
    distrakt.html says it offers the panel at all. A discovered list would have
    quietly covered three surfaces and called it four.
    """

    OFFERS_PANEL = ("index.html", "distrakt.html", "ranker.html",
                    "share_calendar.html")
    OPENER = "data-details-open"
    REQUIRED = ('id="detailsBody"', 'id="detailsTitle"',
                'id="detailsPending"', 'id="detailsUnreachable"')

    def test_each_offering_page_carries_the_whole_shell(self):
        for page in self.OFFERS_PANEL:
            source = expanded(page)
            for needed in self.REQUIRED:
                with self.subTest(page=page, needs=needed):
                    self.assertIn(needed, source)

    def test_no_other_page_carries_an_opener(self):
        """The live half. A new surface growing an opener is the easy mistake —
        the attribute is all it takes — and this fails until that page is named
        above, which is where somebody has to notice the shell goes with it."""
        for page in PAGES:
            if page in self.OFFERS_PANEL:
                continue
            with self.subTest(page=page):
                self.assertNotIn(self.OPENER, expanded(page))

    def test_the_named_pages_are_pages(self):
        """A name that has stopped being a template would make the pairing above
        pass by testing nothing."""
        for page in self.OFFERS_PANEL:
            with self.subTest(page=page):
                self.assertIn(page, PAGES)


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


class BaseTemplateTests(unittest.TestCase):
    """Rendered output, not the template source — a layout that compiles but
    renders the wrong tags passes a source grep. Each render is a page reduced to
    what it tells the base: the variables it sets and the blocks it fills."""

    def render(self, page: str = "", **context) -> str:
        source = "{% extends '_base.html' %}" + page
        return templates.env.from_string(source).render(**context)

    def head(self, page: str = "", **context) -> str:
        return self.render(page, **context).split("</head>", 1)[0]

    def test_it_renders_the_bundled_stylesheet_and_every_font(self):
        html = self.head()
        self.assertEqual(re.findall(r'rel="stylesheet" href="([^"]+)"', html),
                         [assets.url(assets.STYLESHEET)])
        self.assertEqual(re.findall(r'rel="preload" href="([^"]+)"', html),
                         [assets.font_url(f) for f in assets.FONT_PRELOADS])

    def test_the_stylesheet_is_requested_before_the_font_preloads(self):
        """Order matters: the stylesheet is the only resource gating first paint,
        and the ~86 KB of high-priority font preloads would compete with it for
        the connection pool on a cold load."""
        html = self.head()
        self.assertLess(html.index('rel="stylesheet"'), html.index('rel="preload"'))

    def srcs(self, page: str = "") -> list[str]:
        return re.findall(r'<script src="([^"]+)"', self.head(page))

    def test_a_signed_in_page_loads_the_whole_app_list_in_its_order(self):
        """Every page a boost can reach loads the same list: that sameness is the
        whole reason a boosted arrival needs nothing fetched."""
        self.assertEqual(self.srcs(), [assets.url(name) for name in assets.APP_SCRIPTS])

    def test_a_public_page_loads_the_public_list_and_does_not_boost(self):
        """THE TWO GO TOGETHER OR NEITHER IS SAFE. A session that starts on a
        public page keeps its shorter <head> for as long as it swaps; not
        boosting is what makes every way out of it a full load."""
        page = "{% set public = true %}"
        self.assertEqual(self.srcs(page), [assets.url(name) for name in assets.PUBLIC_SCRIPTS])
        self.assertIn('<body class="" hx-boost="false"', self.render(page))
        self.assertIn('<body class="" hx-boost="true"', self.render())

    def test_the_public_list_is_the_head_of_the_app_list(self):
        """A public page reached by a boost from a signed-in one is running inside
        the app list, so everything a public page needs must be in it — and in
        the same order, so a file never runs before one it ran after elsewhere."""
        self.assertEqual(assets.APP_SCRIPTS[:len(assets.PUBLIC_SCRIPTS)], assets.PUBLIC_SCRIPTS)

    def test_htmx_ships_first_then_its_extension_then_the_arrival(self):
        """A page without htmx is a one-way door out of a boosted session: you
        can arrive there boosted, but every link on it is then a full reload.

        THE ORDER IS THE ASSERTION, not just the membership. An htmx extension
        registers itself against the global htmx publishes as it executes, and
        boost.js sets at its top level the config htmx reads when the document
        finishes parsing; both are wrong if they run first, and neither says
        so. Held on the lists themselves, since the base renders exactly a list."""
        for scripts in (assets.PUBLIC_SCRIPTS, assets.APP_SCRIPTS):
            self.assertEqual(scripts[:3], (assets.HTMX, assets.JSON_ENC, assets.BOOST))

    def test_every_script_is_deferred(self):
        for page in ("", "{% set public = true %}"):
            for tag in re.findall(r"<script src=[^>]+>", self.head(page)):
                with self.subTest(tag=tag):
                    self.assertIn(" defer", tag)

    def test_the_site_name_comes_from_the_base(self):
        """Asserted against app/chrome.py's constant rather than a literal: the
        point of this test is that the base supplies the name, and spelling the
        name again here would make a rename fail in a test that is not about
        renaming."""
        self.assertIn(f"<title>Account &ndash; {chrome.PRODUCT_NAME}</title>",
                      self.head("{% block title %}Account{% endblock %}"))

    def test_a_page_carrying_its_own_subject_opts_out_of_the_suffix(self):
        self.assertIn("<title>Distrakkl – July</title>",
                      self.head("{% set site_suffix = false %}"
                                "{% block title %}{{ product }} – July{% endblock %}"))

    def test_the_tracker_reveal_script_can_be_gated_off(self):
        """The account page must not mention the tracker to an account without
        the grant, and the reveal script names it."""
        self.assertIn("distraktRevealed", self.head())
        self.assertNotIn("distraktRevealed", self.head("{% set nav_head = false %}"))

    def test_a_page_adds_to_the_head_through_its_block(self):
        self.assertIn('<meta name="x-probe">',
                      self.head('{% block head %}<meta name="x-probe">{% endblock %}'))

    def test_the_body_and_page_meta_wear_the_one_class_string(self):
        """A cold load wears <body>'s class and a boosted arrival wears
        #pageMeta's; one variable feeding both is what keeps them the same."""
        html = self.render("{% set body_class = 'auth-page has-nav' %}")
        self.assertIn('<body class="auth-page has-nav"', html)
        self.assertRegex(html, r'<div id="pageMeta"[^>]*data-body-class="auth-page has-nav"')

    def test_a_page_names_itself_only_when_it_says_so(self):
        """`data-page` is what a script tells pages apart by. A page nothing asks
        about carries none, rather than a name nothing reads."""
        self.assertNotIn("data-page", self.render())
        self.assertIn('data-page="rankings"', self.render("{% set page_name = 'rankings' %}"))

    def test_the_progress_bar_is_marked_only_by_pages_that_draw_it(self):
        """The bar is the site header's, so a page with no header pointing its
        requests at it would point them at nothing."""
        self.assertNotIn("hx-indicator", self.render())
        self.assertIn('hx-indicator="#pageProgress"',
                      self.render("{% set progress_bar = true %}"))

    def test_the_document_opens_with_its_doctype(self):
        """The base's own explanation and settings are Jinja, so nothing may be
        written ahead of the doctype."""
        self.assertTrue(self.render().startswith("<!DOCTYPE html>"))


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


class PublicPageTests(unittest.TestCase):
    """The pages a stranger lands on, which load the short script list.

    NAMED RATHER THAN DISCOVERED, like the details panel's pages above: "who can
    reach this page without a session" is a fact about its route, not its
    template, so a page joining the list is a decision somebody has to write down
    here — and a page leaving it has to come off it."""

    PUBLIC_PAGES = ("auth_invite_invalid.html", "auth_login.html", "auth_onboarding.html",
                    "auth_register.html", "share_calendar.html", "share_not_found.html")

    def test_exactly_the_named_pages_are_public(self):
        public = [name for name in PAGES
                  if re.search(r"\{%\s*set public = true\s*%\}", uncommented(name))]
        self.assertEqual(public, list(self.PUBLIC_PAGES))


class SignInFormTests(unittest.TestCase):
    """The one form a password is typed into, and the window before htmx is ready.

    Until htmx has wired it, it is an ordinary HTML form — and an ordinary form
    with no method is a GET, which put the password in the address bar, the
    history and the access log of anybody who pressed Enter early on a slow
    connection. Held from both sides, because either alone leaves a way through."""

    def render(self) -> str:
        source = ("{% import '_sign_in_form.html' as sign_in %}"
                  "{% call sign_in.form('/login', 'Sign in') %}"
                  '<input name="password" type="password">{% endcall %}')
        return templates.env.from_string(source).render()

    def test_it_can_never_send_the_password_in_an_address(self):
        form = re.search(r"<form[^>]*>", self.render()).group(0)
        self.assertIn('method="post"', form)

    def test_it_cannot_be_sent_until_htmx_has_wired_it(self):
        """A browser will not submit a form by Enter while its submit button is
        disabled, so a button drawn disabled closes the window entirely."""
        button = re.search(r'<button[^>]*type="submit"[^>]*>', self.render()).group(0)
        self.assertRegex(button, r"\sdisabled[\s>]")
        self.assertIn("data-awaits-htmx", button)

    def test_a_reload_cannot_bring_the_button_back_enabled(self):
        """Firefox restores a button's live disabled state across a reload, so a
        button htmx had switched on comes back switched on before htmx has
        loaded — measured, and the press then sent the form the browser's way.
        `autocomplete="off"` is what tells Firefox not to."""
        button = re.search(r'<button[^>]*type="submit"[^>]*>', self.render()).group(0)
        self.assertIn('autocomplete="off"', button)

    def test_the_script_that_enables_it_is_on_every_page_that_draws_it(self):
        """Every page drawing the form is public, and ui.js is on the public list;
        without it the button would never come on."""
        self.assertIn("static/js/ui.js", assets.PUBLIC_SCRIPTS)
        source = js_source("static/js/ui.js")
        self.assertIn("'htmx:after:init'", source)
        self.assertIn("[data-awaits-htmx]", source)


class EveryPageAgreesTests(unittest.TestCase):
    """The invariants that make a body swap between any two pages land intact."""

    def test_every_page_extends_the_base(self):
        """FIRST, before anything else in the file: Jinja writes out whatever a
        child template puts ahead of `extends`, so a page that opened with markup
        would put it in front of the doctype. And a page that did not extend the
        base at all would be building a <head> of its own — the one thing every
        page must agree on."""
        for name in PAGES:
            with self.subTest(page=name):
                self.assertRegex(uncommented(name), r"^\{% extends '_base\.html' %\}",
                                 f"{name} does not open by extending the base")

    def test_no_page_opens_a_document_of_its_own(self):
        """The doctype, <head> and <body> tags are the base's. A page spelling one
        again would nest a second document inside the first."""
        for name in PAGES:
            source = uncommented(name)
            for tag in ("<!DOCTYPE", "<html", "<head>", "<body"):
                with self.subTest(page=name, tag=tag):
                    self.assertNotIn(tag, source)

    def test_no_page_carries_styling_of_its_own_in_its_head(self):
        """A page's own <style> applies only when that page is the one the
        browser loaded cold. Reached by a click, the swap replaces <body> and
        leaves <head> exactly as the session's first page left it — so the rules
        never exist, and a page whose whole layout was in that block arrives with
        no styling at all. Page CSS goes in the bundled stylesheet, which every
        <head> links; error_lobby.html is the one exception and is excluded above
        for a reason that does not generalize. A script in that block would fail
        the same way, so neither is allowed there."""
        for name in PAGES:
            extra = block(name, "head")
            for tag in ("<style", "<script", "<link"):
                with self.subTest(page=name, tag=tag):
                    self.assertNotIn(tag, extra,
                                     f"{name} adds to <head> what a boosted arrival will not apply")

    def test_no_page_writes_an_asset_path_of_its_own(self):
        """Asset filenames live in app/assets.py. A template restating one is a
        second place to edit when a file is renamed, and the copy does not
        announce itself when it goes stale."""
        for name in PAGES:
            with self.subTest(page=name):
                found = re.findall(r'(?:href|src)="(/static/(?:css|js)/[^"]+)"', read(name))
                self.assertEqual(found, [], f"{name} writes asset paths directly")

    def test_every_signed_in_page_boosts_from_its_body(self):
        """The base's <body>, and every page extends the base (above). Rendered,
        because the attribute is an expression: the public pages turn it off, and
        PublicPageTests holds which pages those are."""
        html = templates.env.from_string("{% extends '_base.html' %}").render()
        self.assertIn('hx-boost="true"', re.search(r"<body[^>]*>", html).group(0))

    def test_the_body_attributes_every_page_inherits_from_are_turned_on(self):
        """A plain `hx-boost` on <body> reaches the links under it only because
        the loader asks for it: htmx wants the parent to opt in per attribute
        now, and an attribute nothing opted into is simply not inherited.

        THE FAILURE IS SILENT, WHICH IS WHY THIS IS PINNED HERE. Nothing throws
        and nothing logs — every link on every page quietly goes back to being a
        full reload, and the progress bar stops lighting, and the page still
        works well enough that it can ship. Either the flag stays or the
        attributes below grow the `:inherited` suffix; this fails if neither is
        true."""
        loader = js_source(assets.BOOST)
        self.assertRegex(
            loader, r"htmx\.config\.implicitInheritance\s*=\s*true",
            "the loader no longer turns implicit inheritance on")
        body = re.search(r"<body[^>]*>", uncommented(BASE)).group(0)
        for attr in ("hx-boost", "hx-indicator"):
            with self.subTest(attr=attr):
                self.assertNotIn(f"{attr}:inherited", body,
                                 f"the base declares {attr} explicitly inheritable — "
                                 "the loader's implicitInheritance flag is now dead weight")

    def test_an_inline_body_script_that_declares_anything_is_wrapped(self):
        """htmx re-executes scripts inside the swapped body, so a top-level
        `const` becomes a redeclaration on the second arrival and the whole
        script fails to parse — leaving that page's controls bound to nothing.
        A function wrapper is what makes a second arrival safe; `function` and
        `var` declarations are legal to repeat and need no wrapper. A page's
        content block IS its body."""
        for name in PAGES:
            for match in re.finditer(r"<script>\n(.*?)</script>", block(name, "content"), re.S):
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


# Third-party files served as they were published. The rules the rest of this
# module enforces — one declaration of a name across the lists, a page name
# asked for only where a page states it — are rules about code this repository
# writes, and holding somebody else's library to them would only teach us to
# edit it.
VENDORED = ("htmx.min.js", "htmx-json-enc.js", "sortable.min.js")

JS_DIR = assets.BASE_DIR / "static/js"


def first_party() -> list[str]:
    """Every script this repository wrote, in the order a signed-in page loads
    them."""
    return [name for name in assets.APP_SCRIPTS if not name.endswith(VENDORED)]


def js_source(name: str) -> str:
    return (assets.BASE_DIR / name).read_text(encoding="utf-8")


# Not preceded by a dot: `this.form.requestSubmit()` is the DOM's, not ours.
CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")


def declares(source: str) -> set[str]:
    """Every name a script declares at TOP LEVEL — the ones that land in the one
    scope every script shares. Indented declarations are inside something and
    cannot collide."""
    names = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", source, re.M))
    for group in re.findall(r"^(?:let|const|var)\s+([^=;\n]+)", source, re.M):
        names.update(part.strip() for part in group.split(",") if part.strip().isidentifier())
    return names


# `{{ body_class }}` in both places is the simple way to keep <body> and
# #pageMeta from disagreeing, and the base does exactly that for every page that
# leaves #pageMeta to it. The CALENDAR does something stronger: both come from ONE
# macro, because the answer to changing a view preference re-draws #pageMeta out
# of band and the class string cannot be spelled in two files. So the test for a
# page that draws its own is that the two name the SAME THING, not that they both
# say a particular thing.
_EXPR = re.compile(r'(?:data-)?body-?class="\{\{\s*(.+?)\s*\}\}"')
_SET = re.compile(r"\{%-?\s*set body_class\s*=\s*(.+?)\s*-?%\}")


def _unqualify(expr: str) -> str:
    """`calendar_meta.classes(view)` and `classes(view)` are one macro; the
    prefix is only how the importing file reaches it."""
    return re.sub(r"^[a-z_]+\.", "", expr.strip())


def _class_expression(tag: str) -> str:
    found = _EXPR.search(tag)
    return _unqualify(found.group(1)) if found else ""


def _body_class_source(source: str) -> str:
    """What `body_class` is set FROM \u2014 so a page that indirects through it is
    compared on the thing itself rather than on the name."""
    found = _SET.search(source)
    return _unqualify(found.group(1)) if found else "body_class"


class ScriptListTests(unittest.TestCase):
    """What loading one list on every signed-in page asks of the files in it.

    They are ordinary scripts sharing one global scope (app/assets.py says why
    they are not modules), and every one of them runs on every page of its list.
    So the list is only correct if its files agree about names, every page a
    script asks about is a page that says who it is, and every function an inline
    handler names is somewhere in it.
    """

    def test_every_script_on_disk_is_in_the_app_list(self):
        """A file nothing loads is dead code that still looks alive. The app list
        holds the public one, so it is every script there is."""
        for path in sorted(JS_DIR.rglob("*.js")):
            name = path.relative_to(assets.BASE_DIR).as_posix()
            with self.subTest(script=name):
                self.assertIn(name, assets.APP_SCRIPTS, f"{name} is served but no list loads it")

    def test_typing_in_the_calendar_search_cannot_reach_a_service(self):
        """WHAT SPENDS A REQUEST MUST BE A DELIBERATE ACT, and this is the
        markup rule that keeps it one.

        The search box reads what this instance already holds on every
        keystroke, which costs an index lookup. Asking the SERVICES spends
        requests against somebody else's rate limit, so it belongs to Enter and
        the button — the form's own submit.

        THE BUG THIS CLOSES: the live flag was an `hx-vals` on the FORM, and
        htmx inherits attributes down to descendants. The box's own as-you-type
        request picked it up, so every keystroke went out to the services. As a
        hidden FIELD it is submitted with the form and with nothing else — the
        box's request carries only itself and what its `hx-include` names — so
        the separation holds by construction rather than by an override
        somebody has to remember.
        """
        page = markup("index.html")
        box = re.search(r'<input[^>]*id="searchInput"[^>]*>', page, re.S)
        self.assertIsNotNone(box, "the calendar search box is gone")
        box = box.group(0)
        self.assertNotIn("live", box,
                         "the search box itself names the live flag")
        self.assertIn('name="live"', page,
                      "nothing carries the live flag to the services")
        # The flag must not sit on any ANCESTOR as an hx-vals either, which is
        # the exact shape that leaked. A hidden field is the only form allowed.
        self.assertNotRegex(
            page, r"hx-vals=[^>]*live",
            "the live flag is an inherited hx-vals again — it must be a field")

    def test_no_two_scripts_declare_the_same_name(self):
        """One name, one file, across every script there is.

        STILL WORTH HOLDING WITH ONE LIST, though the reason has narrowed. Every
        file shares one global scope on every page of its list, so a second
        top-level `let`/`const` of a name is a SyntaxError that stops that whole
        file executing on every page — loud in a browser, silent in this suite. A
        second `function` of one is worse, because it is legal: whichever file
        the list loads later quietly owns the name, and the button that calls it
        does the other file's job. Nothing about that is visible from the page
        the button is on.
        """
        owner: dict[str, str] = {}
        for name in first_party():
            for declared in declares(js_source(name)):
                with self.subTest(name=declared):
                    # assertNotIn would print the whole name table.
                    if declared in owner:
                        self.fail(f"{declared} is declared at top level by both "
                                  f"{owner[declared]} and {name}")
                owner[declared] = name

    def test_no_script_calls_another_as_it_loads(self):
        """WHY THE LISTS' FIRST-PARTY ORDER IS NOT LOAD-BEARING, held rather
        than hoped. A file that called a function of another file's at its top
        level would need that file to have run first — the ordering contract a
        page's boot.js going last used to be. Every call between files happens
        on an event instead, after every file has run.

        Checked as: no line at the top level of a file (unindented, and not a
        declaration, an assignment into htmx's config or window, or the start of
        a listener) calls a function another first-party file declares."""
        declared_by = {name: declares(js_source(name)) for name in first_party()}
        for name in first_party():
            others = set().union(*(names for other, names in declared_by.items() if other != name))
            code = re.sub(r"/\*.*?\*/", "", js_source(name), flags=re.S)
            for line in code.splitlines():
                if not line or line[0].isspace() or line.startswith(("//", "}", ")")):
                    continue
                if re.match(r"(?:async\s+)?function\b|(?:let|const|var)\s|document\.addEventListener"
                            r"|window\.(?:addEventListener|fetch\s*=)|htmx\.config\.", line):
                    continue
                with self.subTest(script=name, line=line[:60]):
                    called = set(CALL.findall(line)) & others
                    self.assertFalse(called, f"{name} calls {sorted(called)} as it loads")

    def test_every_page_a_script_asks_about_names_itself(self):
        """A script tells pages apart by `data-page`: boost.js announces the
        arrival under it, and a boot file starts its page when its own name is
        announced. A page a boot file waits for that never names itself would
        land INERT, which is the failure the old per-page loader existed to
        prevent; a name nothing waits for is a declaration nothing reads. So the
        two sets must be equal."""
        asked = set()
        for name in first_party():
            asked |= set(re.findall(r"detail\.page\s*[!=]==\s*'([^']+)'", js_source(name)))
        stated = set()
        for template in TEMPLATES_DIR.glob("*.html"):
            source = uncommented(template.name)
            stated |= set(re.findall(r"\{%-?\s*set page_name\s*=\s*'([^']+)'", source))
            stated |= set(re.findall(r'<div id="pageMeta"[^>]*\bdata-page="([^"{]+)"', source))
        self.assertTrue(asked, "no script asks which page is on screen; the scan is wrong")
        self.assertEqual(asked, stated)

    def test_every_page_states_its_body_class_inside_its_body(self):
        """A swap replaces the body's CHILDREN, so the <body> element keeps the
        previous page's classes. #pageMeta is inside the swapped region and so
        arrives with the new page; the class it carries has to be the one the
        <body> tag renders, or a boosted arrival wears something the cold load
        never would. The base draws both from `body_class`; a page that draws its
        own #pageMeta has to draw it from what it set `body_class` to."""
        base = uncommented(BASE)
        self.assertIn('class="{{ body_class }}"', re.search(r"<body[^>]*>", base).group(0))
        self.assertEqual(_class_expression(re.search(r'<div id="pageMeta"[^>]*>', base).group(0)),
                         "body_class")
        for template in PAGES:
            if not block(template, "page_meta"):
                continue
            meta = re.search(r'<div id="pageMeta"[^>]*>', markup(template))
            with self.subTest(page=template):
                self.assertIsNotNone(meta, f"{template} replaces #pageMeta with nothing")
                self.assertEqual(
                    _class_expression(meta.group(0)), _body_class_source(uncommented(template)),
                    f"{template}: <body> and #pageMeta name different things, so "
                    "a boosted arrival can wear a class the cold load never would")

    def test_every_page_init_starts_on_the_arrival(self):
        """boot.js used to run its init from DOMContentLoaded, which a boosted
        arrival never fires. static/js/boost.js announces `page-arrived` on the
        cold load and on every arrival alike, so a boot file that waits for the
        document event instead would only ever run on a full page load."""
        boots = [name for name in first_party() if name.endswith("/boot.js")]
        self.assertTrue(boots)
        for name in boots:
            # Comments stripped: both files explain in prose why they no longer
            # wait for the event.
            source = re.sub(r"//[^\n]*", "", js_source(name))
            with self.subTest(script=name):
                self.assertIn("'page-arrived'", source)
                self.assertNotIn("DOMContentLoaded", source)


class LateArrivingDayBlockTests(unittest.TestCase):
    """What a day block that fetches itself in gets from the page, and it is now
    NOTHING AT ALL.

    The server draws everything onto the block: each card's not-watching state
    from the stored marks, is-new from the shell's own answer, the day's packing
    and every library button's state. There used to be one exception — a mark
    this page had made whose save had not landed when the day's request read the
    marks — and it went when marking stopped being something the page did ahead
    of the server. A mark is a button posting itself, answered with every card
    for that title redrawn.

    So the calendar's swap handler has ONE branch left: a filters save, which
    replaces the whole of #calendarBody and is a new month to re-init on. A day
    block arriving is not that and is left alone.
    """

    def setUp(self):
        self.boot = js_source("static/js/calendar/boot.js")

    def test_a_late_block_is_not_walked_by_the_page_at_all(self):
        """Named by IDENTIFIER rather than by the words around them: this file is
        comment-dense and says what the server draws, so matching prose would
        fail on its own explanation."""
        for gone in ("applyViewStateTo", "pendingMarks", "setCardState",
                     "applyArrStateTo", "readViewData", "updateStats"):
            with self.subTest(name=gone):
                self.assertNotIn(gone, self.boot)

    def test_the_one_swap_it_still_acts_on_is_the_filters_save(self):
        """Named by what arrived, which is the only thing that distinguishes a
        whole-body replacement from one day landing."""
        self.assertIn("calendarBody", self.boot)
        self.assertIn("initCalendarPage", self.boot)


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


# Every page that draws a poster, a network logo or card art is drawing TMDB
# content, and TMDB's API terms require their logo and disclaimer wherever that
# happens. The set is written out rather than derived because "does this page
# show TMDB content" is not a property of the template text — distrakt.html draws
# network logos through an API route, and share_calendar.html draws poster art
# the owner's views warmed. A page added to this list without the include, or
# given the include and left off the list, fails below.
TMDB_CONTENT_PAGES = {
    "index.html",           # calendar cards: posters + network logos
    "share_calendar.html",  # the public month, same cards
    "distrakt.html",        # tracker rows: network logos
    "ranker.html",          # the board: poster tiles
    "pick.html",            # the month picker, same chrome as the calendar
    "admin.html",
    "auth_me.html",
}

# The exact sentence TMDB's terms require, verbatim. Asserted as a whole string
# so a well-meant reword is a failing test rather than a quiet breach.
TMDB_DISCLAIMER = ("This product uses TMDB and the TMDB APIs but is not "
                   "endorsed, certified, or otherwise approved by TMDB.")


class FooterAttributionTests(unittest.TestCase):
    """The shared footer, and the attribution it exists to carry.

    NOT A TIDINESS TEST. Two pages had grown their own footers with different
    content, and the obligation these assert — TMDB's logo and disclaimer
    wherever TMDB content appears — is one this app already incurred through
    poster art and network logos long before anything was written down about it.
    A page that draws TMDB content and forgets the footer is the failure mode,
    and it is invisible by inspection because nothing about it looks wrong.
    """

    def test_every_page_drawing_tmdb_content_includes_the_shared_footer(self):
        for name in sorted(TMDB_CONTENT_PAGES):
            with self.subTest(page=name):
                source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
                self.assertIn('include "_footer.html"', source)

    def test_no_page_builds_its_own_footer_beside_the_shared_one(self):
        """The two hand-rolled footers are what the partial replaced; a third
        would put the attribution back into drift."""
        for name in PAGES:
            with self.subTest(page=name):
                source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
                self.assertNotIn("<footer", source)

    def test_the_footer_carries_the_logo_and_the_exact_disclaimer(self):
        rendered = templates.get_template("_footer.html").render(
            version="1.2.3", build="dev")
        self.assertIn(TMDB_DISCLAIMER, rendered)
        self.assertIn("/static/images/tmdb-short.svg", rendered)
        self.assertIn("themoviedb.org", rendered)

    def test_the_logo_asset_exists(self):
        self.assertTrue((TEMPLATES_DIR.parent / "static" / "images"
                         / "tmdb-short.svg").is_file())

    def test_the_page_note_is_optional_and_the_attribution_is_not(self):
        """A page with nothing of its own to say still carries the attribution —
        which is the whole reason the note is the parameter and the attribution
        is not."""
        without = templates.get_template("_footer.html").render()
        self.assertIn(TMDB_DISCLAIMER, without)
        with_note = templates.get_template("_footer.html").render(note="Hello")
        self.assertIn("Hello", with_note)
        self.assertIn(TMDB_DISCLAIMER, with_note)

    def test_the_version_tag_is_omitted_rather_than_left_empty(self):
        """A page rendered without chrome context (no session to build one from)
        should print no tag at all, not an empty one."""
        self.assertNotIn("version-tag", templates.get_template("_footer.html").render())
        self.assertIn("v1.2.3", templates.get_template("_footer.html").render(
            version="1.2.3", build="dev"))
