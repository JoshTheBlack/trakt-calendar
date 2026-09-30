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
from urllib.parse import urlencode

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


class Dedent(Extension):
    """Strips each line's leading whitespace from a template that opens with
    DEDENT_MARKER, AT COMPILE TIME — once per template, not once per render.

    Only LEADING whitespace, and the newline stays. HTML collapses a run of
    whitespace between tags to the same single space a newline already is, so
    nothing drawn changes; a Jinja expression does not care how it is indented.
    What it would break is a value written across lines — an attribute or a
    string literal wrapped mid-way — which is why a template opts in rather than
    every template being changed under its author."""

    def preprocess(self, source: str, name: str | None, filename: str | None = None) -> str:
        if not source.startswith(DEDENT_MARKER):
            return source
        return _LEADING_WHITESPACE.sub("", source)


templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.add_extension(Dedent)

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
