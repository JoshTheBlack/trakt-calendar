"""The one Jinja environment every rendered page goes through.

Nine route modules each built their own Jinja2Templates over the same directory.
That was harmless while a template only read the context a route handed it, but
templates/_base.html renders the stylesheet, font and script tags FROM
app/assets.py rather than restating the filenames, and reaching a Python module
from a template means an environment global. Installing that global nine times
is the same fact in nine places, with the same failure mode as the head block it
replaces: the tenth route module renders a page whose <head> is empty of assets
and nothing says why.

So the environment is shared. It is also the natural home for anything else every
page needs to be able to call, and one environment means one template cache.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

import anyio.to_thread
from fastapi.templating import Jinja2Templates
from jinja2.ext import Extension

from . import assets
from . import chrome
from .providers.base import Source

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# A TEMPLATE THAT OPENS WITH THIS IS SENT WITHOUT ITS INDENTATION. Jinja copies a
# template's source whitespace into every render, so the nesting that keeps a
# template readable is paid for by every page that draws it — once for most
# templates, and once PER ROW for one drawn a thousand times. MEASURED on the
# rankings board: 869 of a row's 3,971 bytes were indentation, 870 KB of a
# thousand-row page, all of it parsed again on every redraw.
# OPT-IN, NOT ENVIRONMENT-WIDE: whitespace is content inside <pre> and
# <textarea>, and a template that holds literal text in one of those must keep
# it. A template that draws neither, many times over, asks for this.
DEDENT_MARKER = "{#- dedent -#}"
_LEADING_WHITESPACE = re.compile(r"^[ \t]+", re.M)

# AND ONE THAT OPENS WITH THIS IS SENT WITHOUT THE LINE BREAKS BETWEEN ITS TAGS
# TOO: a newline that follows a tag's `>` or a statement or comment tag (`%}`,
# `#}`) and precedes a tag or a statement or comment tag goes — never one beside
# an EXPRESSION, whose output may be text a newline was spacing. Each such
# newline is a text node of its own in the browser, which a card drew sixty of:
# MEASURED on one October's All Episodes, 61 of a card's 84 text nodes were
# whitespace between its tags. FOR A TEMPLATE WHOSE EVERY CONTAINER LAYS OUT BY
# FLEX OR GRID, OR POSITIONS WHAT IT HOLDS, where whitespace between tags draws
# nothing — the calendar card and what it is built from — and only those: in
# ordinary inline flow a line break between two tags is a visible space. A line
# break is `\r\n` as well as `\n`: the loader hands the source over undecoded, and
# a template saved by an editor that writes CRLF would otherwise silently stop
# being tightened.
TIGHT_MARKER = "{#- tight -#}"
_BETWEEN_TAGS = re.compile(r"(?:(?<=>)|(?<=%\})|(?<=#\}))\r?\n(?=<|\{%|\{#)")


class Dedent(Extension):
    """Strips each line's leading whitespace from a template that opens with
    DEDENT_MARKER or TIGHT_MARKER, AT COMPILE TIME — once per template, not once
    per render — and, behind TIGHT_MARKER, the line breaks between its tags.

    Only LEADING whitespace, and the newline stays. HTML collapses a run of
    whitespace between tags to the same single space a newline already is, so
    nothing drawn changes; a Jinja expression does not care how it is indented.
    What it would break is a value written across lines — an attribute or a
    string literal wrapped mid-way — which is why a template opts in rather than
    every template being changed under its author."""

    def preprocess(self, source: str, name: str | None, filename: str | None = None) -> str:
        if source.startswith(TIGHT_MARKER):
            return _BETWEEN_TAGS.sub("", _LEADING_WHITESPACE.sub("", source))
        if not source.startswith(DEDENT_MARKER):
            return source
        return _LEADING_WHITESPACE.sub("", source)


templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.add_extension(Dedent)


async def drawn(request, name: str, context: dict, **kwargs):
    """`templates.TemplateResponse`, built on a worker thread.

    JINJA RENDERS WHEN THE RESPONSE IS BUILT, so building one on the event loop
    holds every other request while it runs: MEASURED on a copy of dev, a month's
    shell drew its 268 inline cards in 0.26 s and the loop answered nobody else
    meanwhile, and an unfiltered month's 718 in 1.2 s. A calendar answer —
    a month, a late day, a mark's redraw — is drawn here instead; the loop is
    free while it is. The response is the same object, built the same way."""
    return await anyio.to_thread.run_sync(
        lambda: templates.TemplateResponse(request, name, context, **kwargs))

# `assets` is the module itself, so _base.html calls assets.url(assets.STYLESHEET)
# and there is no second copy of any asset path anywhere in the templates.
templates.env.globals["assets"] = assets

# A GLOBAL RATHER THAN PART OF chrome.page_context, because the pages that need
# it most are the ones with no context to speak of: the sign-in screen, the
# invite-expired page and the error pages all render the name and several of
# them render before there is a user to build a context from. A global is also
# what makes it reachable from inside a MACRO such as _head.html's og(), which
# receives only its own arguments and would otherwise have to be passed the name
# by every one of its callers — which is the fifteen-literals problem again, one
# level up.
templates.env.globals["product"] = chrome.PRODUCT_NAME


class SourceLook(NamedTuple):
    """How one service is SHOWN: its name, and its two pictures under
    /static/images/ — see templates/_source_logo.html for which goes where."""
    label: str
    mark: str
    wordmark: str


# EVERY SERVICE'S TWO PICTURES, BY NAME, AND ONE ENTRY EACH. The template these
# feed used to draw every service that was not Simkl as Trakt — its name, its
# mark and its wordmark — so a third service would have been credited to the
# wrong company on every card and button it touched. A service with no entry
# here now fails the render (`source_look`) rather than borrowing another's
# face; tests/kernel/test_source_look.py holds the table to the registry.
#   tmdb  TMDB's own "primary short" square logo (themoviedb.org's logos and
#         attribution page) as the wordmark, and the same logo on TMDB's navy
#         tile as the mark, so it stays legible over a poster the way the
#         other two marks' discs and tiles do. Sized by the same CSS classes as
#         theirs, so it is never drawn larger, which TMDB's terms ask.
SOURCE_IMAGES: dict[str, tuple[str, str]] = {
    "trakt": ("trakt-circlemark-dark.svg", "trakt-logo-dark.svg"),
    "simkl": ("simkllogo.svg", "simkl_white_with_shaddow_transparent.png"),
    "tmdb": ("tmdb-mark.svg", "tmdb-wordmark.svg"),
}


def source_look(source) -> SourceLook:
    """`source`'s name and pictures. Raises KeyError for a name that is not a
    registered service with pictures: a card is better refused than drawn under
    somebody else's logo.

    THE NAME IS THE PROVIDER'S OWN LABEL, read from the registry, so a badge can
    never spell a service differently from the rest of the app. Function-local
    because the registry imports config, which this kernel module must not close
    a loop through at load time."""
    from . import providers
    name = str(source)
    mark, wordmark = SOURCE_IMAGES[name]
    return SourceLook(providers.registered()[name].label, mark, wordmark)


templates.env.globals["source_look"] = source_look


def details_url(base: str, media, ids, season, key: str = "") -> str:
    """Where a card asks for its details panel, built by the SERVER.

    ONE PARAMETER PER SOURCE, NAMED FOR THAT SOURCE — `?trakt=203330&simkl=2601798`
    — rather than the `?id=` a single-source app could get away with. A card two
    services described has two ids and neither is "the" id; sending one plus a
    service name would put "who describes this title" on the client, which is the
    one side that cannot see whose credentials this instance has filled in (see
    app/providers/detail_source.py). THE NAMESPACES COME FROM `Source` rather than
    from a list here, so a third service is a registration and not an edit.

    This was two identical JavaScript functions — one on the calendar, one on the
    share page, renamed only to dodge a clash in the shared global scope, and the
    rename was the tell. The card already knows every id it carries, so the query
    is markup and needs no builder on the other side at all.

    IN THE TEMPLATE ENVIRONMENT rather than beside the panel's own render helpers
    (app/details_panel.py) for one reason: those import this module, so installing
    a global from there would close the loop, and a global has to be installed
    before anything renders rather than whenever some route module first happens
    to be imported.
    """
    params = [("media", str(media or "show"))]
    params += [(str(source), value) for source in Source
               if (value := str((ids or {}).get(str(source)) or ""))]
    # AND EACH SERVICE'S OWN NAME FOR THE TITLE, where the card has it. The panel
    # draws an outbound button per service and both of them ask that the readable
    # name be sent rather than the id — resolving the number costs them a lookup
    # and a redirect. Namespaced, never the shared `slug`: the two services do not
    # agree about what a title is called, so an unattributed name builds a URL for
    # a title the other one has never heard of.
    params += [(f"{source}_slug", value) for source in Source
               if (value := str((ids or {}).get(f"{source}_slug") or ""))]
    if season not in (None, ""):
        params.append(("season", str(season)))
    # AND THE CARD'S SHARED IDENTITY, where the surface asking has one. The
    # service ids above say who can DESCRIBE the title; this says which title it
    # is in the id space nobody here owns (app/providers/base.py's MATCH_SOURCES),
    # which is what the tracker files a person's watched episodes under. A
    # calendar card sends it so its panel can draw the same ticks the tracker
    # does for the same show; the public share page sends none, because there is
    # no viewer whose viewing it could be.
    if key:
        params.append(("key", str(key)))
    return f"{base}?{urlencode(params)}"


templates.env.globals["details_url"] = details_url


def details_query_names(*, names: bool = True) -> tuple[str, ...]:
    """Every parameter name `details_url` can put on a details request.

    THE SAME WALK OVER `Source`, so the two cannot disagree about what a details
    request is made of — a third service registers once and both the builder and
    this answer it. `season` and `key` are not here: the first is what a season
    tab REPLACES, and the second is an identity a route validates for itself
    rather than copying.

    It exists because a fragment that carries a request's parameters forward has
    to carry the ones it NAMES rather than the ones it was sent. Copying a query
    string wholesale means a value a caller invented is rendered back into the
    page — inert in an attribute, but it is still the owner's page echoing a
    stranger's text, which is the rule the poster and the outbound links already
    follow.

    `names` is the `{source}_slug` half, and it is separable because it is the
    half that only decides how an outbound LINK IS SPELLED. A surface whose links
    come from somewhere other than the request reads none of them — the public
    share page builds its links from the month it has read — so it asks for the
    ids alone and a stranger's spelling is not carried forward.
    """
    ids = ("media",) + tuple(str(source) for source in Source)
    if not names:
        return ids
    return ids + tuple(f"{source}_slug" for source in Source)
