"""The one Jinja environment every rendered page goes through.

Nine route modules each built their own Jinja2Templates over the same directory.
That was harmless while a template only read the context a route handed it, but
templates/_head.html renders the stylesheet, font and script tags FROM
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

from . import assets
from . import chrome
from .providers.base import Source

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# `assets` is the module itself, so _head.html calls assets.url(assets.STYLESHEET)
# and there is no second copy of any asset path anywhere in the templates.
templates.env.globals["assets"] = assets

# A GLOBAL RATHER THAN PART OF chrome.page_context, because the pages that need
# it most are the ones with no context to speak of: the sign-in screen, the
# invite-expired page and the error pages all render the name and several of
# them render before there is a user to build a context from. A global is also
# what makes it reachable from inside the _head.html MACRO, which receives only
# its own arguments and would otherwise have to be passed the name by every one
# of its callers — which is the fifteen-literals problem again, one level up.
templates.env.globals["product"] = chrome.PRODUCT_NAME


# The eleven-character video id out of any of the URL shapes a source returns for
# a trailer, or None when it is not a YouTube link at all.
#
# A FILTER ON THE ENVIRONMENT RATHER THAN A STEP IN A ROUTE. The details panel is
# one template rendered by three routes, and "is this a YouTube URL and which
# video is it" is the panel's own question — asking it in each route would put
# the same regex back in three places, which is where it was when three
# JavaScript renderers each carried a copy of it.
_YOUTUBE_URL = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|v/)|youtu\.be/)([\w-]{11})")


def youtube_id(url: object) -> str | None:
    match = _YOUTUBE_URL.search(str(url or ""))
    return match.group(1) if match else None


templates.env.filters["youtube_id"] = youtube_id


def details_url(base: str, media, ids, season) -> str:
    """Where a card asks for its details panel, built by the SERVER.

    ONE PARAMETER PER SOURCE, NAMED FOR THAT SOURCE — `?trakt=203330&simkl=2601798`
    — rather than the `?id=` a single-source app could get away with. A card two
    services described has two ids and neither is "the" id; sending one plus a
    service name would put "who describes this title" on the client, which is the
    one side that cannot see whose credentials this instance has filled in (see
    app/calendar/detail_source.py). THE NAMESPACES COME FROM `Source` rather than
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
    if season not in (None, ""):
        params.append(("season", str(season)))
    return f"{base}?{urlencode(params)}"


templates.env.globals["details_url"] = details_url
