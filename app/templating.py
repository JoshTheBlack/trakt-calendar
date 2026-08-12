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

from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import assets
from . import chrome

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
