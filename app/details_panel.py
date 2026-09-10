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

from typing import NamedTuple, Sequence
from urllib.parse import urlencode

from fastapi import Request

from .templating import templates


class SeasonTab(NamedTuple):
    """One season the panel offers, and where its episode rows come from."""
    number: int
    url: str
    current: bool


def season_tabs(request: Request, count: int, current: int | None, *,
                carry: Sequence[str] = (),
                pinned: dict[str, str] | None = None) -> tuple[SeasonTab, ...]:
    """A tab per season from 1 to `count`, each addressed at this surface's own
    season fragment.

    THE TABS COST NOTHING TO OFFER, which is the whole reason they can be on
    every surface: the count is already on the thing that was clicked — a
    calendar card knows its own season, a roster row knows its own, a board item
    stores one — so drawing the strip needs no lookup, and a season's rows are
    fetched only if somebody opens that tab.

    THE ADDRESS IS THIS REQUEST'S OWN with `/season` on the end and the season
    swapped, so a tab asks for exactly what the panel asked for about a different
    season. One implementation of that rather than one per route: whatever a
    surface needs on its details request it needs on its season request too, and
    the public share page proves it — the bound on what may be looked up cannot
    be evaluated without knowing which month is on screen, so the view has to
    ride along.

    `carry` NAMES WHAT RIDES, AND IT IS NAMED RATHER THAN COPIED. Copying the
    query string wholesale would put a parameter a caller invented into the
    markup of a page served from the owner's origin — inert inside an attribute,
    but the same class of thing as rendering back a poster address a stranger
    spelled, which every surface here already refuses to do. A surface names the
    parameters its own fragment reads; anything else is dropped.

    `pinned` is for what a surface's FIRST request did not have to carry because
    the panel could work it out, and a tab's cannot. The tracker is the case:
    its panel is opened by naming a roster row, which is a title AND a season, so
    a tab asking about a different season would otherwise be asking about a row
    that may not exist — an earlier season of a show somebody started tracking
    part-way through is exactly the season these tabs are for. It pins the season
    the row was opened from, so authorization stays "this title is on your
    roster" while the question moves.

    Root-relative, like every other address this app writes, so it resolves
    against whatever origin served the page.
    """
    fixed = dict(pinned or {})
    base = request.url.path.rstrip("/") + "/season"
    carried = [(name, value) for name in carry if name not in fixed
               and (value := request.query_params.get(name))]
    carried += [(name, str(value)) for name, value in fixed.items()]
    return tuple(
        SeasonTab(number,
                  f"{base}?{urlencode(carried + [('season', str(number))])}",
                  number == current)
        for number in range(1, count + 1))


def panel(request: Request, details: dict, *, media: str, season: int | None,
          title: str = "", poster: str = "", progress: object | None = None,
          links=(), season_count: int = 0, picker: bool = True,
          carry: Sequence[str] = (), pinned: dict[str, str] | None = None):
    """The panel's contents for one title.

    `title` and `poster` are the CARD's, not the answer's, and the surfaces that
    have a card pass them: which service answered decides what is in the payload —
    Simkl's catalogue record is stored without a title, and its artwork is not
    what this viewer's own field order picked — so the panel is drawn with what
    the reader is already looking at.

    A SURFACE WITH NO CARD PASSES NO POSTER and still gets one: the template
    falls back to the describing service's own, which rode in on the lookup that
    was made anyway. Only a surface showing a card has a better answer than that
    — the picture already on screen, picked by this viewer's own field order.

    `links` is where this title lives at each service that knows it
    (`detail_source.outbound_links`). Passed rather than derived: the panel is
    handed one source's ANSWER, and which services know the title is a fact about
    the CARD — a Trakt-described title's payload carries no Simkl id, so a panel
    working it out for itself could only ever offer one of the two buttons.

    `progress` is this viewer's own record of the season or None, and it is the
    tracker's `episode_progress.SeasonProgress`. Passed rather than built: the
    TEMPLATE renders ticks if it is handed them and the ROUTE decides whether to
    look, so a surface opting in is one declared read rather than a template that
    knows whose record it is drawing.

    `season_count` is what the SURFACE knows about how many seasons there are, and
    it is a floor rather than the answer: the describing service states its own
    count and that is the better number, because a card for season 2 of a
    five-season show knows only about two. 0 from both means no picker. One tab is
    not drawn either — a strip naming the only season there is offers nothing to
    move between. `carry` and `pinned` are what a tab's address is made of — see
    `season_tabs`.

    `picker=False` turns the strip off however many seasons there are, and the
    public share page is why it exists: a shared calendar publishes ONE MONTH, so
    most of the seasons a title has are not seasons that link shows. Offering
    tabs there would be offering controls that mostly refuse — the bound is per
    view and a season the month has no airing of may not be looked up — so the
    honest thing is not to offer them. It is a parameter rather than something
    that surface decides in the template because the template must not know which
    surface it is on.
    """
    shown = answered_season(details, season)
    return templates.TemplateResponse(request, "_details.html", {
        "request": request, "d": details, "media": media, "season": shown,
        "title": title, "poster": poster, "progress": progress, "links": links,
        # `shown` rather than the asked-for season, so the tab that reads as
        # current is the one whose rows are actually below it.
        #
        # THE WIDEST OF THE THREE ANSWERS, and each is a floor the others can
        # raise: the SERVICE's count is the true one, the season being viewed
        # proves at least that many exist, and the surface's own is what a board
        # item stores. Taking the largest is what stopped the strip stopping at
        # the season somebody happened to open — a card for season 2 of a
        # five-season show used to offer two tabs.
        "tabs": season_tabs(
            request,
            max(season_count, shown or 0, int(details.get("season_count") or 0)),
            shown, carry=carry, pinned=pinned)
                if picker and media != "movie" and shown else (),
    })


def season_pane(request: Request, details: dict, *, media: str, season: int | None,
                progress: object | None = None):
    """One season, on its own — what a season tab fetches.

    THE SAME MARKUP THE PANEL DREW FOR THE SEASON IT OPENED ON
    (templates/_details_season.html, included by _details.html), because a tab
    swapping in something assembled differently from the block beside it is the
    drift this whole panel exists to have stopped.

    IT IS A SEASON RATHER THAN A LIST OF EPISODES, and the name says so: the
    description and the cast vary by season and travel with the rows. What does
    not vary — the picture, the chips, the outbound buttons, the trailer — stays
    above the picker and is not re-sent.
    """
    return templates.TemplateResponse(request, "_details_season.html", {
        "request": request, "d": details, "media": media,
        "season": answered_season(details, season), "progress": progress,
    })


def answered_season(details: dict, season: int | None) -> int | None:
    """The season the source actually came back with, falling back to the one the
    click asked about.

    DECIDED HERE RATHER THAN IN THE TEMPLATE so it is one reading of one rule.
    They are the same for a card whose listing stated a season; where it did not,
    a source whose episode list holds exactly one season answers with that one
    (Simkl's calendar files omit the season on anime), and the heading has to say
    which season the rows below it are. A source that does not state the key at
    all is the same case as one that states it empty.

    ALWAYS AN int OR None, because the number is arithmetic now and not only a
    heading: the season picker counts up to it. A source that spelled it as text
    is read as a number, and one that spelled it as something else at all falls
    back to what was asked rather than taking the panel down.
    """
    answered = details.get("season")
    if answered is None:
        answered = season
    try:
        return int(answered) if answered not in (None, "") else None
    except (TypeError, ValueError):
        return season


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
