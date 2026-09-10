"""The details panel, rendered. One template, one context, three surfaces.

templates/_details.html draws what a title IS — chips, overview, trailer, cast,
the season's episodes — and the calendar, the tracker and the public share page
all open it. Each of them used to build that markup in JavaScript, from the same
payload, and the three copies had drifted twice over: a certification chip on two
of them, an IMDb rating on one. The template is the fix for that; this module is
what stops the CONTEXT drifting in the same way, by naming its keys once instead
of once per route.

IN THE KERNEL BECAUSE IT BELONGS TO NO FEATURE. Three features render this panel
and none of them owns it, so filing it inside one would make the other two look
like they depended on that feature. It reaches nothing above itself: `progress`
is passed straight through to the template and is never inspected here, which is
what lets the tracker hand over its own record of a season without the kernel
knowing the tracker exists.
"""
from __future__ import annotations

from fastapi import Request

from .templating import templates


def panel(request: Request, details: dict, *, media: str, season: int | None,
          title: str = "", poster: str | None = "", progress: object | None = None):
    """The panel's contents for one title.

    `title` and `poster` are the CARD's, not the answer's, and the surfaces that
    have a card pass them: which service answered decides what is in the payload —
    Simkl's catalogue record is stored without a title, and its artwork is not
    what this viewer's own field order picked — so the panel is drawn with what
    the reader is already looking at.

    `poster=None` MEANS THIS SURFACE HAS NO PICTURE AT ALL, and "" means it has
    one it does not know yet. The tracker identifies a show by its network logo
    and its payload carries no image, so it passes None and gets the text-only
    hero; a card whose artwork has not resolved passes "" and gets the same
    placeholder the card beside it drew.

    `progress` is this viewer's own record of the season or None, and it is the
    tracker's `episode_progress.SeasonProgress`. Passed rather than built: the
    TEMPLATE renders ticks if it is handed them and the ROUTE decides whether to
    look, so a surface opting in is one declared read rather than a template that
    knows whose record it is drawing.
    """
    # THE SEASON THAT WAS ANSWERED, not the one that was asked about, and the
    # decision is made here rather than in the template so it is one reading of
    # one rule. They are the same for a card whose listing stated a season; where
    # it did not, a source whose episode list holds exactly one season answers
    # with that one (Simkl's calendar files omit the season on anime), and the
    # heading has to say which season the rows below it are. A source that does
    # not state the key at all is the same case as one that states it empty.
    answered = details.get("season")
    return templates.TemplateResponse(request, "_details.html", {
        "request": request, "d": details, "media": media,
        "season": season if answered is None else answered,
        "title": title, "poster": poster, "progress": progress,
    })


def gap(request: Request, message: str):
    """A panel with nothing in it, and the sentence saying why.

    AT 200, like every other fragment in this app. A failure is something the
    server draws, not something the browser infers from a status: a non-200 is
    not swapped (static/js/boost.js's `noSwap`), so answering one here would
    leave the panel sitting on its loading line for ever.

    Each surface owns its own wording, because they refuse for different reasons
    a reader can act on differently — and the share page's deliberately does not,
    for a reason of its own (see share_routes._describe_shared_title).
    """
    return templates.TemplateResponse(request, "_details_gap.html",
                                      {"request": request, "message": message})
