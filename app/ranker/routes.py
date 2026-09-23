"""The rankings page and its board API.

HTTP only: parse a request, validate its shape, delegate to core.py, and
turn the answer — or the refusal — into a response. No SQL and no business rules
live here; a rule that appears in this module is a rule the data layer cannot
enforce for the callers that bypass a route.

Every route is RANKER_APPROVED. The grant is the whole gate: unlike the tracker
there is no provider identity to also insist on, because a ranker user builds
their lists by searching with the instance's own credential.

MUTATING REQUESTS CARRY A JSON BODY, INCLUDING DELETE. The app-wide request-shape
middleware refuses any POST/PUT/PATCH/DELETE that is not exactly
`application/json`, which is a deliberate anti-CSRF control — so a client
deleting a board sends `{}` rather than nothing at all.
"""
from __future__ import annotations

import functools
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import anyio
import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import (core as ranker, exports as ranker_export, grid_builder,
               imports as ranker_import, sources as ranker_sources)
from .sources import Media
from .. import auth, authz, chrome, details_panel, route_params
from ..auth import AuthLevel
from ..config import load_settings
from ..media import posters, user_images
from ..perftrace import span
from ..providers import detail_source
# THE DETAIL PORTS' failure type, which is not `ranker_sources.SourceUnavailable`:
# that one is this feature's own translation for the search and ratings seams,
# while a details panel asks the shared detail ports directly (see
# app/providers/detail_source.py) and they raise the app-wide one.
from ..providers.base import SourceUnavailable
from ..templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# `private`: this response requires a session, so a shared cache in front of
# the app has no business holding a copy.
_POSTER_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}

# A restore is the one request that legitimately carries whole boards rather than
# a list of keys: fifty boards of a thousand titles, each with its own title,
# network and provenance map, is several megabytes of perfectly ordinary
# document. The caps in the data layer are what actually bound what may be
# written; this only stops something absurd being read into memory first.
MAX_RESTORE_BYTES = 32 * 1024 * 1024

# Search is the one route here that reaches a provider on every call, so it is
# the one that can be turned into an amplifier. The window is short and the
# allowance generous because the caller is a signed-in, approved account typing
# into a debounced box — this bounds a script, not a person.
#
# Stored in `login_attempts` via `auth.rate_limited`/`auth.record_attempt`, the
# same house pattern `share_routes._share_rate_limited` uses for its per-IP
# volume throttle, rather than an in-process counter: a dict's budget would
# silently multiply if the instance ever ran more than one worker.
SEARCH_MAX_PER_WINDOW = 30
SEARCH_WINDOW_SECONDS = 60
MIN_SEARCH_LENGTH = 2
MAX_SEARCH_RESULTS = 20

# How much of the unranked pool comes down in the first response. Everything past
# it arrives a page at a time behind an intersect sentinel, because a board may
# hold a thousand titles and each card is a poster request. Sixty is more than a
# screenful at any window size, so the first page is never what a viewer waits on.
POOL_PAGE_SIZE = 60

# Above this many tiered titles a board draws its tiers closed and each one
# fetches its rows the first time it is opened. Below it the board simply shows
# itself: rendering a couple of hundred rows costs less than making somebody
# click through six tiers to see the arrangement they came to look at.
EAGER_ROW_LIMIT = 200

# What an export may ask for. Columns below three make a canvas taller than any
# format will encode at the item ceiling, and above six the tiles are too small
# to be worth the pixels.
EXPORT_COLUMNS = (3, 4, 5, 6)
# Full size, or half for somewhere with an attachment limit. Arbitrary scales are
# not offered because every one of them is a different set of rounded constants
# to have looked at.
EXPORT_SCALES = (0.5, 1.0)
MAX_EXPORT_TEXT = 80

# The preview is rendered by exactly the same code as the export, at a size that
# fits beside the options that produced it. Deriving the scale from the column
# count keeps the preview the same shape as the thing it is previewing.
PREVIEW_WIDTH = 600
PREVIEW_FORMAT = "webp"

# TWO CONCURRENT RENDERS, INSTANCE-WIDE — the real memory control. A full-size
# grid at the caps is a canvas of tens of megapixels and a few hundred megabytes
# through encode; a third request waiting a moment is much better than a third
# request the machine cannot hold. Preview renders take a slot too: they are
# smaller, but they are the ones that arrive in bursts as options change.
_render_slots = anyio.Semaphore(2)

# Long enough that holding down the export button cannot queue a stream of
# full-size renders behind the semaphore, short enough that trying a different
# format is not a wait. The semaphore bounds how much memory is in use at once;
# this bounds how much work one account can line up.
EXPORT_COOLDOWN_SECONDS = 20


async def _search_over_budget(user_id: int) -> bool:
    """Whether this account has already spent its search allowance, recording
    this request either way. Volume-only — there is no failed search to count
    separately."""
    key = str(user_id)
    limited = await auth.rate_limited(
        "ranker_search", key, max_attempts=SEARCH_MAX_PER_WINDOW, window_seconds=SEARCH_WINDOW_SECONDS,
    )
    await auth.record_attempt("ranker_search", key, True)
    return limited


def _refusal(exc: ranker.RankerError) -> JSONResponse:
    """The one place a data-layer refusal becomes a status code.

    A board that isn't the caller's answers 404 exactly as a board that does not
    exist does — the two are indistinguishable on purpose, because a caller able
    to tell them apart could enumerate other people's boards.
    """
    if isinstance(exc, ranker.BoardNotFound):
        return authz.error("No such board.", 404)
    if isinstance(exc, ranker.VersionConflict):
        return authz.error(str(exc), 409, reason="version_conflict")
    return authz.error(str(exc), 400)


# ---------------------------------------------------------------------------
# the page and its fragments
# ---------------------------------------------------------------------------
# The board renders SERVER-SIDE and is usable that way. Everything below adds to
# a page that already works: the pool pages itself in behind a sentinel and a
# closed tier fetches its rows, both through the same htmx idioms the calendar
# uses, and neither is load-bearing for seeing what is on a board.

def _tiers_by_priority(categories: list[dict]) -> list[dict]:
    """A board's tiers in the order they are read in: highest priority first.

    Priority is what a tier MEANS relative to the others, and it is already what
    the ranking itself is built from — exports.py orders by exactly this key.
    Drawing the tiers in the order they happened to be CREATED in left the board
    disagreeing with its own export, and left no way to move a tier at all,
    since nothing reorders them by hand.

    Sorted on the way to the page rather than written back, so changing a tier's
    priority in its settings moves it on the next render and no stored order can
    drift out of step with the priorities it is supposed to reflect. The sort is
    stable, so tiers sharing a priority keep the order they already had instead
    of reshuffling under the viewer.
    """
    return sorted(categories, key=lambda c: -int(c["rank_priority"] or 0))


def _board_state(board: dict) -> dict:
    """What the client needs to build a save from, including the parts of the
    board it has not drawn.

    A closed tier renders no rows, so the DOM alone cannot say what is in one —
    and a save that omitted a tier would be refused, while one that named a tier
    with an empty list and the wrong label would overwrite the label. The keys
    and the tier settings are the whole answer and cost almost nothing to send:
    a board at its thousand-title cap is a thousand short strings.
    """
    return {
        "uid": board["uid"],
        "name": board["name"],
        "year": board["year"],
        "media_scope": board["media_scope"],
        "version": board["version"],
        "categories": [
            {
                "uid": category["uid"], "label": category["label"],
                "rank_priority": category["rank_priority"],
                "is_isolated": category["is_isolated"], "colour": category["colour"],
                "items": [item["key"] for item in category["items"]],
            }
            for category in board["categories"]
        ],
        "pool": [item["key"] for item in board["pool"]],
    }


def _board_groups(boards: list[dict]) -> list[dict]:
    """The switcher's year groupings.

    Grouped HERE rather than with a template filter because a board's year is
    optional, and sorting a mixed list of integers and Nones raises. The data
    layer already returns them in the order the switcher wants — years newest
    first, undated last — so this only has to break the run into groups.
    """
    groups: list[dict] = []
    for entry in boards:
        if not groups or groups[-1]["year"] != entry["year"]:
            groups.append({"year": entry["year"], "boards": []})
        groups[-1]["boards"].append(entry)
    return groups


def _pool_url(board_uid: str, page: int) -> str:
    return f"/rankings/fragments/pool?board={quote(board_uid)}&page={page}"


def _rows_url(board_uid: str, category_uid: str) -> str:
    return (f"/rankings/fragments/tier?board={quote(board_uid)}"
            f"&tier={quote(category_uid)}")


def _strips_url(board_uid: str) -> str:
    return f"/rankings/fragments/tier-strips?board={quote(board_uid)}"


def _move_choices(board: dict) -> list[dict]:
    """Where a title on this board can be sent, in the order the board draws its
    tiers, with the pool first.

    EVERY ROW CARRIES THIS, so it is built once here rather than four times: the
    shell renders rows inline, the pool pages them in, a tier fetches its own, and
    a title added after first paint comes from row_fragment — and a row has to be
    the same row whichever of those drew it, which is the claim row_fragment makes
    in its own docstring.

    THE POOL IS THE EMPTY UID, which is what move_items already means by it: "or
    back to the pool when `category_uid` is empty". So a move out of a tier and a
    move between tiers are one request shape rather than two.
    """
    return [{"uid": "", "label": "Pool (unranked)"}] + [
        {"uid": c["uid"], "label": c.get("label") or "Untitled"}
        for c in board["categories"]
    ]


def _pool_context(board: dict, page: int) -> dict:
    """One page of the pool, plus the sentinel that asks for the one after it.
    The last page carries no sentinel, which is what ends the chain."""
    start = page * POOL_PAGE_SIZE
    items = board["pool"][start:start + POOL_PAGE_SIZE]
    remaining = len(board["pool"]) - (start + len(items))
    return {
        "items": items,
        # Every row names its board, because its details button asks for a title
        # by board and key and a key is only unique within one board.
        "board_uid": board["uid"],
        # And where it can be moved to. The whole page takes its row context from
        # here too (_rankings_response updates itself with this), so the tiers a
        # row offers are the tiers the board drew, in that order.
        "tiers": _move_choices(board),
        "next_url": _pool_url(board["uid"], page + 1) if remaining > 0 else None,
        # The sentinel reserves the height its page will take, so arriving cards
        # slot into space already made for them instead of shoving the page down.
        "next_rows": min(remaining, POOL_PAGE_SIZE),
    }


# NOTHING PRE-WARMS A POSTER ANY MORE, and that is a deletion rather than a move.
# The page used to post back the keys of the rows it had just drawn and ask for
# their tiles, which is a second statement of what is on screen — and it raced the
# browser's own requests for the same pictures rather than getting ahead of them.
# GET /api/rankings/poster already generates a tile it has not got, every row's
# <img> carries `loading="lazy"` so only what is on screen is asked for, and a
# browser opens about six connections to one origin at a time, which is the same
# pacing the batch warm's own semaphore gave (posters._FAN_OUT is eight). So the
# requests the page makes anyway are the warm.


async def _removals_context(user_id: int, board_uid: str) -> dict:
    """The removal history as _ranker_removed.html draws it. One builder for the
    page and the fragment that redraws it, so the two cannot disagree about what
    a board could put back — or about the caps the page states beside it."""
    try:
        removals = await ranker.fetch_removals(user_id, board_uid)
    except ranker.RankerError:
        # A board that went away in another tab has nothing to put back, and the
        # history is not where that should be discovered.
        removals = []
    return {
        "removals": removals,
        # Each Restore button posts to its board, so the list has to know which.
        "board_uid": board_uid,
        "removal_cap": ranker.REMOVAL_HISTORY_PER_BOARD,
        "removal_days": ranker.REMOVAL_HISTORY_DAYS,
    }


async def _sources(user_id: int) -> dict[str, object]:
    """Which ways of adding titles this account actually has, ANSWERING BY
    OMISSION: a source that is missing from this map is one the UI renders
    nothing for at all, rather than a disabled button that advertises a feature
    the account cannot reach.

    Shared by the page and by GET /api/rankings/sources so first paint and any
    later refresh can never disagree about what is on offer.
    """
    sources: dict[str, object] = {"search": True}
    if await ranker_sources.ratings_available(user_id):
        sources["ratings"] = True
    if await ranker_import.tracker_available(user_id):
        # WHETHER, NOT WHICH YEARS. The import dialog's year list is a fragment of
        # its own now (`import_years_fragment`), asked for when a medium is chosen
        # — so every page load stopped paying for two years-per-medium queries to
        # fill a <select> most visits never open.
        sources["import"] = True
    return sources


@guard.get("/rankings", AuthLevel.RANKER_APPROVED)
async def rankings_page(request: Request):
    """The ranker's own page. Standalone: it needs nothing from the calendar or
    from any other feature to be useful.

    `board` selects which one is open, so the switcher is a set of real links
    that work without any script at all — and with htmx they boost into a body
    swap that leaves the stylesheet, the scripts and the drag machinery resident.
    """
    user = await auth.current_user(request)
    return await _rankings_response(request, user, request.query_params.get("board"))


async def _rankings_response(request: Request, user, wanted: str | None, *,
                             notice: str = "", notice_ok: bool = True,
                             removals_open: bool = False, undo_key: str = "",
                             open_tier: str = "", search: dict | None = None,
                             pool_first: str = ""):
    """The rankings page for the board `wanted`, or for the account's first.

    ONE BUILDER FOR EVERY ANSWER THAT IS THE WHOLE PAGE, and that is now every
    action on this page which is not a drag: making a board, renaming, cloning,
    adding a tier, applying the template, writing a tier's settings, taking a
    title off, moving a selection, importing, seeding, and putting a removed title
    back. A second builder would be a second idea of what the page carries, and
    the version a save echoes is exactly the kind of thing two of them would
    disagree about.

    `notice` is a sentence about what the request that drew this page did, and
    `notice_ok` whether it is good news or a refusal, which only decides its
    colour; `removals_open` draws the removal history already open.
    `undo_key` names a title the toast should offer to put back — it draws an Undo
    beside the sentence, and that Undo is the SAME button the removal history
    carries, posting to the same route, so a title comes back one way however it
    is asked for.
    `open_tier` is a tier this request put a title INTO, drawn open with its rows
    whatever the board's size says: a move answers with the page, so a closed
    destination would take the title out of the pool and show it landing nowhere.
    It costs no extra read — the rows are already in `board`.
    `search` is a search to draw the page WITH — the query, what it was over, and
    its results. Adding a title answers with the page like everything else here,
    and a page that came back with the search box empty would make adding a second
    title from one search mean searching for it again. Its results are read from
    the cache the search box's own request filled, so redrawing them asks nobody
    and spends nothing.
    `pool_first` is a title to draw at the HEAD of the pool. The pool is paged —
    sixty at a time — and a title just added lands after everything already there,
    so on any board with a full page of unranked titles it would arrive on a page
    nobody is looking at: the count would tick up and nothing else would happen.
    Hoisting it costs nothing that means anything, because the pool stores no
    order at all (every pooled title shares one rank), and it is done before both
    the markup and the copy the client saves from are built so the two cannot
    disagree about it.
    """
    boards = await ranker.fetch_boards(user.user_id)
    wanted = wanted or (boards[0]["uid"] if boards else None)
    board = None
    if wanted:
        # A board may hold a thousand titles, and this reads all of them plus
        # every category, so it is the one part of opening this page that grows
        # with how much somebody has ranked.
        try:
            with span("ranker.fetch_board"):
                board = await ranker.fetch_board(user.user_id, wanted)
        except ranker.RankerError:
            # A uid that is not this account's lands on the board list rather than
            # a 404 page: somebody following a stale link still gets a working
            # switcher, which is what they need in order to go somewhere real.
            board = None
    if board:
        # Once, here, so the markup and the copy the client saves from (below)
        # are built from the same ordering and cannot disagree about it.
        board["categories"] = _tiers_by_priority(board["categories"])
        # By KEY, over the item dicts the pool actually holds — `_board_state`
        # is what turns those into bare keys, further down.
        if pool_first:
            board["pool"] = ([item for item in board["pool"] if item["key"] == pool_first]
                             + [item for item in board["pool"] if item["key"] != pool_first])

    tiered = sum(len(c["items"]) for c in board["categories"]) if board else 0
    context = {
        "request": request,
        # is_admin, calendar_available, ranker_available, version, build
        # for the shared header.
        **chrome.page_context(user),
        # The name the export dialog prefills with: the chosen display name if
        # there is one, else the username. Only a DEFAULT — the dialog's field
        # stays editable, so a one-off export can still say something else.
        "username": user.label,
        "boards": boards,
        "board_groups": _board_groups(boards),
        "board": board,
        "rows_url": _rows_url,
        "board_state": _board_state(board) if board else None,
        "sources": await _sources(user.user_id),
        "rows_open": tiered <= EAGER_ROW_LIMIT,
        "open_tier": open_tier,
        # An empty search draws an empty box and no results, which is the page as
        # it arrives by every other route.
        "search": search or {"query": "", "media": "", "results": (), "held": frozenset()},
        "notice": notice,
        "notice_ok": notice_ok,
        "undo_key": undo_key,
        "removals_open": removals_open,
        "restore_ack": RESTORE_ACK,
    }
    if board:
        context.update(_pool_context(board, 0))
        context.update(await _removals_context(user.user_id, board["uid"]))
        context.update(_export_context(user.user_id, board))
        context["strips_url"] = _strips_url(board["uid"])
    return templates.TemplateResponse(request, "ranker.html", context)


@guard.get("/rankings/fragments/pool", AuthLevel.RANKER_APPROVED)
async def pool_page_fragment(request: Request):
    """The next page of a board's unranked pool.

    A VIEW, NOT AN API: it answers with the same cards the shell renders inline,
    and nothing else, so a page that arrives late is indistinguishable from one
    that came with the document.
    """
    user = await auth.current_user(request)
    board_uid = request.query_params.get("board") or ""
    page = max(0, _query_int(request.query_params.get("page"), 0))
    try:
        board = await ranker.fetch_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return _fragment_failure(request, str(exc), _pool_url(board_uid, page),
                                 "ranker-pool-sentinel")
    context = _pool_context(board, page)
    return templates.TemplateResponse(
        request, "_ranker_pool_page.html", {"request": request, **context},
    )


@guard.get("/rankings/fragments/tier", AuthLevel.RANKER_APPROVED)
async def category_rows_fragment(request: Request):
    """One tier's rows, fetched the first time it is opened."""
    user = await auth.current_user(request)
    board_uid = request.query_params.get("board") or ""
    category_uid = request.query_params.get("tier") or ""
    retry = _rows_url(board_uid, category_uid)
    try:
        board = await ranker.fetch_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return _fragment_failure(request, str(exc), retry, "ranker-rows")
    found = [c for c in board["categories"] if c["uid"] == category_uid]
    if not found:
        return _fragment_failure(request, "No such tier.", retry, "ranker-rows")
    return templates.TemplateResponse(
        request, "_ranker_category_rows.html",
        {"request": request, "items": found[0]["items"], "board_uid": board["uid"],
         "tiers": _move_choices(board)},
    )


@guard.get("/rankings/fragments/tier-strips", AuthLevel.RANKER_APPROVED)
async def tier_strips_fragment(request: Request):
    """Every tier header's artwork, redrawn.

    THE STRIP IS A VIEW OF WHAT A TIER HOLDS, so the server is the only thing that
    can draw it — and a drag, a nudge or the move menu changes what a tier holds
    without the server hearing about it until the save lands. Every other action
    on this page answers with the board and redraws them for free; this is what
    those three ask for once their save has been accepted.

    THE WHOLE BOARD'S IN ONE ANSWER, out of band. A drag moves a title out of one
    tier and into another, so two of them change at once, and reading the board is
    one query either way.
    """
    user = await auth.current_user(request)
    try:
        board = await ranker.fetch_board(user.user_id, request.query_params.get("board") or "")
    except ranker.RankerError:
        # A board that went away in another tab has no headers to redraw, and this
        # is not where that should be discovered.
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "_ranker_tier_strips.html",
        {"request": request, "categories": _tiers_by_priority(board["categories"])},
    )


@guard.get("/rankings/fragments/row", AuthLevel.RANKER_APPROVED)
async def row_fragment(request: Request):
    """One title's row, exactly as the shell draws it inline.

    EVERY TITLE THAT ARRIVES AFTER FIRST PAINT IS DRAWN FROM THIS: a search
    result clicked or dragged in, and a removed title put back. All of them reach
    the page only once the title is on the board, so the server renders every
    row there is and the page carries no second builder of one — what lands is
    _ranker_row.html, byte for byte.

    A TITLE THE BOARD DOES NOT HOLD DRAWS AS NOTHING, at 200 like every fragment
    here. The one way to ask about one is a row still on its way when another tab
    took the title off, and nothing is the honest row for that: the placeholder
    that asked goes away, and the version check on the next save is what says
    the board moved.
    """
    user = await auth.current_user(request)
    board_uid = request.query_params.get("board") or ""
    try:
        item = await ranker.fetch_item(user.user_id, board_uid, request.query_params.get("key"))
        # THE BOARD AS WELL, FOR THE ROW'S MOVE MENU. A row carries one choice per
        # tier, so drawing one means knowing the tiers — and a row that arrives
        # after first paint has to be the row the shell would have drawn, which is
        # this route's whole claim. Read after the item so a title the board does
        # not hold still costs nothing.
        board = await ranker.fetch_board(user.user_id, board_uid)
    except ranker.RankerError:
        return HTMLResponse("")
    board["categories"] = _tiers_by_priority(board["categories"])
    return templates.TemplateResponse(
        request, "_ranker_row.html",
        {"request": request, "item": item, "board_uid": board_uid,
         "tiers": _move_choices(board)},
    )


# THE REMOVAL HISTORY HAS NO FRAGMENT OF ITS OWN ANY MORE. It used to redraw
# itself on an event the page fired after a removal; a removal answers with the
# whole page now, and so does a restore, so the list arrives with everything else
# and there is nothing left to keep in step.


# ---------------------------------------------------------------------------
# a title's details panel
# ---------------------------------------------------------------------------

# THE SEASON A BOARD TITLE'S PANEL OPENS ON. A board holds a TITLE, not a season,
# so there is no season of its own to open on — and asking with none at all is
# not an option: a Trakt lookup naming no season answers no episode list and no
# season count, which leaves the panel nothing to build a picker from. The first
# season is the one every show with seasons has, and it is where the title
# begins; the picker reaches the rest.
_OPENING_SEASON = 1


@guard.get("/rankings/fragments/details", AuthLevel.RANKER_APPROVED)
async def item_details_fragment(request: Request):
    """The details panel for one title on a board."""
    return await _item_details(request, season_only=False)


@guard.get("/rankings/fragments/details/season", AuthLevel.RANKER_APPROVED)
async def item_details_season_fragment(request: Request):
    """One season of a board title's panel — what a season tab fetches. The same
    question as the panel, asked about another season, and a route of its own
    for the reason the calendar's twin gives: the tab's address is this panel's
    address with `/season` on the end (details_panel.season_tabs)."""
    return await _item_details(request, season_only=True)


async def _item_details(request: Request, *, season_only: bool) -> Response:
    """The details panel for one board title, or one season of it.

    THE SAME PANEL EVERY OTHER SURFACE OPENS — templates/_details.html, drawn by
    app/details_panel.py — so a title reads the same here as on the calendar.

    A TITLE IS NAMED BY ITS BOARD AND ITS KEY, AND EVERYTHING ELSE COMES FROM THE
    ROW. The ids that decide who is asked, the medium, the name and the picture
    are read off the stored tier_items row the caller's own board holds, which is
    the tracker's rule for its roster and for the same reason: a request cannot
    point the lookup at a title the board does not have. A season tab carries
    those two names and the season (`carry`), and nothing else a caller spelled is
    rendered back.

    THE STRIP IS THE WIDEST OF THREE COUNTS, each a floor: the row's own (for a
    title imported from finished viewing that is the seasons WATCHED, not the
    seasons there are), the season opened, and the describing service's.

    NO WATCH MARKS. This page does not know the tracker exists —
    tests/ranker/test_standalone.py holds that — so nobody's viewing is drawn on
    it.

    The instance's catalogue credential, for the reason a board search uses it:
    everything this draws is public, shared and cached for every viewer.
    """
    user = await auth.current_user(request)
    params = request.query_params
    try:
        item = await ranker.fetch_item(user.user_id, params.get("board") or "", params.get("key"))
    except ranker.RankerError:
        return details_panel.gap(request, "That title is no longer on this board.")
    media = item["media"]
    season = (None if media == Media.MOVIE
              else route_params.season(params.get("season")) or _OPENING_SEASON)
    settings = load_settings()
    chosen = detail_source.choose(settings, item["ids"])
    if chosen is None:
        return _described_by_the_board(request, item, season_only, reachable=False)
    try:
        details = await detail_source.describe(settings, chosen, media, season)
    except SourceUnavailable:
        # Drawn rather than answered with a status, like every fragment here, and
        # in the sentence every other surface's panel uses for a failed lookup:
        # a service's own rate-limit message is not something a reader can act on.
        logger.warning("Detail lookup failed for %s", chosen.source, exc_info=True)
        return details_panel.gap(request, "⚠️ Could not load details for this item.")
    if details is None:
        return _described_by_the_board(request, item, season_only, reachable=True)
    if season_only:
        return details_panel.season_pane(request, details, media=media, season=season)
    return details_panel.panel(
        request, details, media=media, season=season,
        title=item["title"] or "", poster=_tile_url(item),
        # The row's ids first and the answer's beside them: which services know a
        # title is a fact about the row, and a Trakt-described answer carries no
        # Simkl id to build that button from.
        links=detail_source.outbound_links({**item["ids"], **(details.get("ids") or {})}, media),
        season_count=item["season_count"] or 0,
        carry=("board", "key"))


def _described_by_the_board(request: Request, item: dict, season_only: bool, *,
                            reachable: bool) -> Response:
    """The panel drawn from what the board's own row knows, for a title nobody
    here can describe.

    NOT A BLANK AND NOT A REFUSAL. The row stores the title's name, network,
    runtime and usually a picture, which is the start of what the panel shows, so
    it opens on those — every other key the template reads is simply absent — and
    says, where the episodes would have been, why there are none. A panel that
    opened empty would read as a lookup that failed rather than as a title with
    nobody to ask.

    `reachable` tells the two cases apart, because a reader can act on them
    differently: a title added with no id any service issues can never be looked
    up, while one whose services are not set up on this instance could be.
    """
    if season_only:
        # Tabs are drawn only on a panel a service described, so this is a tab
        # whose title lost its answer between one press and the next.
        return details_panel.gap(request, "Nothing here can describe this item.")
    media = item["media"]
    lead = "Nothing more to show" if media == Media.MOVIE else "No seasons or episodes to show"
    reason = ("the services that know this title are not set up here, and nothing "
              "about it has been looked up before" if reachable else
              "it was added to this board without an id from any service this app "
              "can ask, so there is nobody to ask about it")
    facts = {"title": item["title"] or "", "network": item["network"] or "",
             "runtime": item["runtime"]}
    return details_panel.panel(
        request, facts, media=media, season=None,
        title=item["title"] or "", poster=_tile_url(item),
        links=detail_source.outbound_links(item["ids"], media),
        note=f"{lead}: {reason}.")


def _tile_url(item: dict) -> str:
    """The picture the row itself is drawn with, when there is one to show.

    ONLY A TILE ALREADY ON DISK. A row's <img> hides itself when its tile turns
    out not to exist; the panel's does not, so an address that will 404 would
    draw a broken picture where the empty string — which the panel reads as
    "use the describing service's own poster" — would have drawn a real one.
    """
    tmdb = item["tmdb"]
    path = posters.cached_poster(item["media"], tmdb) if tmdb else None
    if path is None or not path.exists():
        return ""
    return f"/api/rankings/poster?media={quote(str(item['media']))}&tmdb={tmdb}"


def _fragment_failure(request: Request, message: str, retry_url: str, target: str):
    """A fragment that could not be built renders AS A GAP with a Retry button
    that re-requests exactly itself, following the calendar's day fragments. One
    unreachable piece of a board must not take the board down with it."""
    return templates.TemplateResponse(request, "_ranker_failed.html", {
        "request": request, "error": message, "retry_url": retry_url, "target": target,
    })


def _query_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# boards
# ---------------------------------------------------------------------------

@guard.get("/api/rankings/boards", AuthLevel.RANKER_APPROVED)
async def list_boards(request: Request):
    user = await auth.current_user(request)
    return JSONResponse({"ok": True, "boards": await ranker.fetch_boards(user.user_id)})


# ---------------------------------------------------------------------------
# what a board's own controls do
# ---------------------------------------------------------------------------
# EVERY ACTION ON THIS PAGE THAT IS NOT A DRAG POSTS ITSELF AND IS ANSWERED WITH
# THE PAGE. They used to edit the page's copy of the board and then save the whole
# layout, which meant the browser held the rules — what a tier may be called,
# which uid a new one gets, what "apply the template" means — and the server saw
# only the arrangement that came out of them.
#
# THE ANSWER IS THE WHOLE PAGE (`_rankings_response`) rather than a patch of it,
# for the reason the restore already gave: the layout the next save reads and the
# version it echoes both come back from the server, so nothing on this side has to
# adjust two copies of the board and keep them agreeing. What it costs is a drag
# made in the last moment — and that is what the flush in static/js/ranker/save.js
# is for, which writes a pending arrangement before any of these is sent.
#
# WHICH BOARD IS ON SCREEN RIDES ON THE ADDRESS (`?board=`), never in the body: it
# is a reference the server resolves, so a refusal can redraw the page the reader
# was looking at instead of guessing at one.

def _elsewhere(path: str) -> Response:
    """Take the page somewhere else, the way following a link would.

    `HX-Location` is a client-side GET of `path` swapped into the body with the
    address pushed — the same arrival a boosted link produces — so a board that
    has just been made, cloned or deleted lands on ITS OWN URL. Answering with the
    page instead would draw the right board under the wrong address, and a reload
    would then go back to the board that is gone.

    204 rather than an empty 200: htmx acts on this header before it decides
    anything about swapping, and a status the no-swap list already covers is the
    safer thing to be holding if it ever stops doing so.
    """
    return Response(status_code=204, headers={"HX-Location": path})


def _viewed_board(request: Request) -> str | None:
    """The board the page making this request is showing."""
    return request.query_params.get("board")


@guard.post("/rankings/boards/new", AuthLevel.RANKER_APPROVED)
async def new_board(request: Request):
    """Make a board and open it. The uid is the server's (ranker.new_uid)."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    try:
        board = await ranker.create_board(
            user.user_id, uid=ranker.new_uid("board"), name=str(data.get("name") or ""))
    except ranker.RankerError as exc:
        return await _rankings_response(request, user, _viewed_board(request),
                                        notice=str(exc), notice_ok=False)
    return _elsewhere(f"/rankings?board={quote(board['uid'])}")


@guard.post("/rankings/boards/{board_uid}/rename", AuthLevel.RANKER_APPROVED)
async def rename_board(board_uid: str, request: Request):
    """Rename the open board. The address does not change, so this answers with
    the page rather than going anywhere."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    notice, ok = "Renamed.", True
    try:
        await ranker.update_board(user.user_id, board_uid, name=str(data.get("name") or ""))
    except ranker.RankerError as exc:
        notice, ok = str(exc), False
    return await _rankings_response(request, user, board_uid, notice=notice, notice_ok=ok)


@guard.post("/rankings/boards/{board_uid}/clone", AuthLevel.RANKER_APPROVED)
async def clone_board(board_uid: str, request: Request):
    """Copy this board, its tiers and everything in them, and open the copy."""
    user = await auth.current_user(request)
    await authz.json_body(request)
    try:
        board = await ranker.clone_board(user.user_id, board_uid, uid=ranker.new_uid("board"))
    except ranker.RankerError as exc:
        return await _rankings_response(request, user, board_uid,
                                        notice=str(exc), notice_ok=False)
    return _elsewhere(f"/rankings?board={quote(board['uid'])}")


@guard.post("/rankings/boards/{board_uid}/delete", AuthLevel.RANKER_APPROVED)
async def delete_board(board_uid: str, request: Request):
    """Delete a board and everything on it. Lands on the switcher, which is where
    a page whose board no longer exists belongs."""
    user = await auth.current_user(request)
    await authz.json_body(request)
    try:
        await ranker.delete_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return await _rankings_response(request, user, board_uid,
                                        notice=str(exc), notice_ok=False)
    return _elsewhere("/rankings")


@guard.post("/rankings/boards/{board_uid}/tiers/new", AuthLevel.RANKER_APPROVED)
async def new_tier(board_uid: str, request: Request):
    """Add one tier. Its uid is the server's, and its label is validated where
    every other tier label is (ranker.add_categories)."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    notice, ok = "Tier added.", True
    try:
        await ranker.add_categories(
            user.user_id, board_uid,
            [{"uid": ranker.new_uid("tier"), "label": data.get("label")}])
    except ranker.RankerError as exc:
        notice, ok = str(exc), False
    return await _rankings_response(request, user, board_uid, notice=notice, notice_ok=ok)


@guard.post("/rankings/boards/{board_uid}/tiers/template", AuthLevel.RANKER_APPROVED)
async def apply_tier_template(board_uid: str, request: Request):
    """Add whichever of the S/A/B/C/D/F set this board has not got.

    THE SET IS ranker.TIER_TEMPLATE AND THE UIDS ARE ITS OWN. A board seeded from
    ratings already holds tiers with those exact uids, so creating them under
    fresh ones would give it two S tiers — which is why this is the one create
    that does not go through `new_uid`.
    """
    user = await auth.current_user(request)
    await authz.json_body(request)
    try:
        created = await ranker.add_categories(user.user_id, board_uid,
                                              list(ranker.TIER_TEMPLATE))
        notice = (f"Added {created} tier(s)." if created else
                  "Those tiers are already on this board.")
        ok = True
    except ranker.RankerError as exc:
        notice, ok = str(exc), False
    return await _rankings_response(request, user, board_uid, notice=notice, notice_ok=ok)


@guard.get("/rankings/fragments/tier-settings", AuthLevel.RANKER_APPROVED)
async def tier_settings_fragment(request: Request):
    """One tier's settings dialog, DRAWN FROM THE TIER rather than filled in from
    the page's copy of it.

    THE WHOLE OVERLAY IS THE ANSWER, and it arrives already open, swapped over the
    placeholder that asked. That is what stops a dialog opening on the tier before
    this one while this one's values are still on the way: there is no standing
    dialog to leave stale, because the dialog IS the response.
    """
    user = await auth.current_user(request)
    board_uid = request.query_params.get("board") or ""
    tier_uid = request.query_params.get("tier") or ""
    category = None
    try:
        board = await ranker.fetch_board(user.user_id, board_uid)
        category = next((c for c in board["categories"] if c["uid"] == tier_uid), None)
    except ranker.RankerError:
        board = None
    return templates.TemplateResponse(request, "_ranker_tier_settings.html", {
        "request": request, "board_uid": board_uid, "category": category,
        "max_label": ranker.MAX_CATEGORY_LABEL,
        "max_priority": ranker.MAX_RANK_PRIORITY,
    })


@guard.post("/rankings/boards/{board_uid}/tiers/{tier_uid}/settings",
            AuthLevel.RANKER_APPROVED)
async def save_tier_settings(board_uid: str, tier_uid: str, request: Request):
    """Write one tier's label, priority, colour and isolation."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    notice, ok = "Tier saved.", True
    try:
        await ranker.update_category(
            user.user_id, board_uid, tier_uid,
            label=data.get("label"), rank_priority=data.get("rank_priority"),
            colour=data.get("colour"), is_isolated=route_params.ticked(data.get("is_isolated")))
    except ranker.RankerError as exc:
        notice, ok = str(exc), False
    return await _rankings_response(request, user, board_uid, notice=notice, notice_ok=ok)


@guard.post("/rankings/boards/{board_uid}/items/remove", AuthLevel.RANKER_APPROVED)
async def remove_board_item(board_uid: str, request: Request):
    """Take one title off the board, and offer to put it back.

    THE OFFER IS THE SAME BUTTON THE REMOVAL HISTORY DRAWS. The toast's Undo posts
    to the restore route with the same key, so there is one way a title comes back
    however it is asked for — and the history behind it outlives the toast, the
    page and the tab, which the nine-second offer never did.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    key = str(data.get("key") or "")
    undo_key = ""
    try:
        removed = await ranker.remove_items(user.user_id, board_uid, [key])
        notice, ok = ("Removed from the board.", True) if removed else (
            "That title was not on this board.", False)
        undo_key = key if removed else ""
    except ranker.RankerError as exc:
        notice, ok = str(exc), False
    return await _rankings_response(request, user, board_uid, notice=notice,
                                    notice_ok=ok, undo_key=undo_key)


# The prefix a bulk move names each chosen title with. ONE FIELD PER KEY, the
# shape the calendar's filter chips already use, because json-enc turns a
# repeated name into a list only when there is more than one of it — so a
# selection of one would arrive as a string where a selection of two arrives as a
# list, and every reader would have to know that.
PICK_PREFIX = "pick:"


def _picked(data: dict) -> list[str]:
    """The item keys a form named with `pick:<key>`, in the order they arrived."""
    return [name[len(PICK_PREFIX):] for name in data if name.startswith(PICK_PREFIX)]


@guard.post("/rankings/boards/{board_uid}/items/move", AuthLevel.RANKER_APPROVED)
async def move_board_items(board_uid: str, request: Request):
    """Move the pool's checked titles into a tier.

    A BULK MOVE IS A LAYOUT CHANGE LIKE A DRAG, so it bumps the board's version
    and answers with the page: the arrangement the next drag saves from is the
    server's own, and the selection is gone because the page it was made on is.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    notice, ok = "", True
    try:
        moved = await ranker.move_items(user.user_id, board_uid, _picked(data),
                                        data.get("tier"))
        notice = f"Moved {moved} title(s)."
        # WHERE THEY WENT, DRAWN OPEN. Moving into the pool names no tier and
        # needs none: the pool is always on screen.
        landed = str(data.get("tier") or "")
    except ranker.RankerError as exc:
        notice, ok = str(exc), False
        landed = ""
    return await _rankings_response(request, user, board_uid, notice=notice,
                                    notice_ok=ok, open_tier=landed)


@guard.get("/api/rankings/boards/{board_uid}", AuthLevel.RANKER_APPROVED)
async def get_board(board_uid: str, request: Request):
    """One board: its tiers with their titles in order, its unranked pool, and
    the version a save has to echo back."""
    user = await auth.current_user(request)
    try:
        board = await ranker.fetch_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "board": board})


@guard.delete("/api/rankings/boards/{board_uid}/categories/{category_uid}",
              AuthLevel.RANKER_APPROVED)
async def remove_category(board_uid: str, category_uid: str, request: Request):
    """Delete one tier. Its titles RETURN TO THE POOL rather than going with it,
    so a mis-click costs the arrangement of one tier and not the titles in it —
    which is also what makes undoing this a matter of re-creating the tier and
    putting the same keys back."""
    user = await auth.current_user(request)
    await authz.json_body(request)
    try:
        returned = await ranker.delete_category(user.user_id, board_uid, category_uid)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "returned": returned})


@guard.post("/api/rankings/boards/{board_uid}/save", AuthLevel.RANKER_APPROVED)
async def save_board_layout(board_uid: str, request: Request):
    """Store an arrangement. 409 when another tab saved first, which the UI
    answers by reloading rather than by guessing whose version wins."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    try:
        result = await ranker.save_layout(user.user_id, board_uid, data)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, **result})


# ---------------------------------------------------------------------------
# backup and restore
# ---------------------------------------------------------------------------
# Its own pair rather than a section of another feature's backup: an account may
# use the rankings and nothing else, and would then have no other document to
# live inside. The restoring user always comes from the session.

@guard.get("/api/rankings/backup", AuthLevel.RANKER_APPROVED)
async def download_backup(request: Request):
    """Download the requesting user's boards, tiers and titles as one JSON
    document — the input POST /api/rankings/restore takes back.

    Everything in it is keyed by uid, so it restores into a database whose
    autoincrement ids came out completely different. It carries no credentials,
    nobody else's data, and no images."""
    user = await auth.current_user(request)
    doc = await ranker.export_user_data(user.user_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return JSONResponse(doc, headers={
        "Content-Disposition": f'attachment; filename="rankings-backup-{stamp}.json"',
    })


# The phrase a restore has to be confirmed with. A phrase that has to be READ and
# typed cannot be dismissed by reflex the way a confirmation button can, and a
# restore REPLACES every board this account has rather than merging into them.
RESTORE_ACK = "REPLACE MY BOARDS"


def _restore_refusal(request: Request, message: str) -> Response:
    """Why a restore did not happen, said in the panel it was asked from.

    OUT OF BAND, AND THE ONLY THING IN THE ANSWER. The form aims at the body,
    because a restore that WORKS has replaced every board and the page has to come
    back from the server — but a refusal has changed nothing, so it must not
    replace the page the reader is standing on. `swapEmpty:false` on the form is
    what makes an answer whose whole content is out-of-band leave the body alone;
    the same pairing the tracker's own refusals use.
    """
    return templates.TemplateResponse(request, "_ranker_restore.html", {
        "request": request, "message": message,
    })


@guard.post("/api/rankings/restore", AuthLevel.RANKER_APPROVED)
async def restore_backup(request: Request):
    """Replace the requesting user's boards with an exported document's.

    REPLACE, not merge, in one transaction — see ranker.restore_user_data for
    why merging an arrangement is not a coherent thing to ask for.

    THE DOCUMENT ARRIVES AS ONE FIELD OF THE FORM, which is what lets the whole
    restore be a form. The browser reads the chosen file — the one thing only it
    can do, because app/authz.py's shape guard admits application/json alone and a
    cross-origin multipart upload is exactly what that rule exists to refuse — and
    puts the text in a hidden input. The typed phrase, the submit and the answer
    are all markup.
    """
    user = await auth.current_user(request)
    body = await authz.json_body(request, MAX_RESTORE_BYTES)
    try:
        data = json.loads(str(body.get("payload") or ""))
    except ValueError:
        return _restore_refusal(request, "That file isn't readable as a backup.")
    if not isinstance(data, dict):
        return _restore_refusal(request, "That file is not a rankings backup.")
    # CHECKED HERE TOO, not only by the form's own `pattern`. A browser validation
    # is a courtesy to the person typing; this is the one thing standing between a
    # mis-aimed request and every board this account has, and it used to be
    # checked nowhere but in the page.
    if str(body.get("ack") or "").strip() != RESTORE_ACK:
        return _restore_refusal(request, f"Type {RESTORE_ACK} to confirm.")
    try:
        boards = await ranker.restore_user_data(user.user_id, data)
    except ranker.RankerError as exc:
        return _restore_refusal(request, str(exc))
    # EVERY BOARD ON THIS PAGE HAS JUST BEEN REPLACED, so the page comes back from
    # the server rather than being patched: the switcher, the board and the
    # version a save echoes are all somebody else's now.
    return await _rankings_response(request, user, None,
                                    notice=f"Restored {boards} board(s).")


# ---------------------------------------------------------------------------
# posters
# ---------------------------------------------------------------------------

@guard.get("/api/rankings/poster", AuthLevel.RANKER_APPROVED)
async def rankings_poster(request: Request):
    """A cached 500x750 poster tile for (media, tmdb). Mirrors /api/network-logo:
    serves the disk cache, generating it from the resolution chain on first
    request; 404 -> the caller falls back to the placeholder tile."""
    media = (request.query_params.get("media") or "").strip()
    tmdb = request.query_params.get("tmdb")
    if media not in posters.MEDIA_VALUES or not tmdb:
        return Response(status_code=404)
    path = posters.cached_poster(media, tmdb)
    if path is None and not posters.is_negative(media, tmdb):
        path = await posters.ensure_poster(load_settings(), media, tmdb)
    if path is None or not path.exists():
        return Response(status_code=404, headers=_POSTER_CACHE_HEADERS)
    return FileResponse(path, media_type="image/jpeg", headers=_POSTER_CACHE_HEADERS)


# ---------------------------------------------------------------------------
# where titles come from
# ---------------------------------------------------------------------------
# Every route below goes through sources.py (and, for the optional import,
# imports.py). None of them names a provider, which is what
# makes adding a second one a change to those modules alone.

def _media(value, default: Media | None = None) -> Media:
    try:
        return ranker_sources.parse_media(value, default)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@guard.get("/api/rankings/sources", AuthLevel.RANKER_APPROVED)
async def list_sources(request: Request):
    """Which ways of adding titles this account actually has.

    ABSENCE, NOT A DISABLED BUTTON. A source the caller cannot use is missing
    from this response entirely, so the UI has nothing to render for it. Search
    is always here; the other two depend on what the account has linked and
    what it has done elsewhere.
    """
    user = await auth.current_user(request)
    return JSONResponse({"ok": True, "sources": await _sources(user.user_id)})


@guard.get("/rankings/fragments/search", AuthLevel.RANKER_APPROVED)
async def search_fragment(request: Request):
    """Titles to add, drawn as the list under the pool's search box.

    A VIEW, NOT AN API, like every fragment on this page: the box asks for this
    as it is typed into (hx-get in _ranker_board_shell.html) and the answer is the
    results themselves. They used to arrive as JSON that a script turned into
    markup, and each one carried a serialized copy of its title for the add to
    hand back.

    Uses the INSTANCE credential, deliberately: this grant does not imply a linked
    account, and what is asked for is public catalogue data either way.

    WHICH RESULTS THE BOARD ALREADY HOLDS IS DRAWN HERE. It used to be written on
    afterwards by a script reading the page's own copy of the board — a copy that
    is only ever BUMPED, never refreshed, so a result could be marked against a
    board that had moved. Everything that changes the held set is a request now
    (an add answers with the result redrawn, and a removal answers with the whole
    page), so there is nothing left for that sweep to catch that this does not
    already know. `board_keys` is membership alone, not fetch_board.

    EVERY REFUSAL IS DRAWN, at 200: a non-200 is not swapped, so a status would
    leave the previous results standing under a query they no longer answer.
    Too short a query draws nothing, which is what the box shows before anything
    has been typed.
    """
    user = await auth.current_user(request)
    query = str(request.query_params.get("q") or "").strip()
    try:
        media = ranker_sources.parse_media(request.query_params.get("media"), Media.SHOW)
    except ValueError as exc:
        return _search_results(request, message=str(exc))
    if len(query) < MIN_SEARCH_LENGTH:
        return _search_results(request)
    if await _search_over_budget(user.user_id):
        return _search_results(request, message="Too many searches just now — give it a moment.")
    try:
        refs = await ranker_sources.search_source().search(query, media)
    except ranker_sources.SourceUnavailable as exc:
        return _search_results(request, message=str(exc))
    results = [
        {**item, "key": ranker.item_key(item["media"], item["match_source"], item["match_id"])}
        for item in ranker_sources.items_from_refs(refs)
    ][:MAX_SEARCH_RESULTS]
    board_uid = request.query_params.get("board") or ""
    return _search_results(request, query=query, results=results, board_uid=board_uid,
                           held=await ranker.board_keys(user.user_id, board_uid))


def _search_results(request: Request, *, query: str = "", results=(), message: str = "",
                    board_uid: str = "", held=frozenset()):
    return templates.TemplateResponse(request, "_ranker_search_results.html", {
        "request": request, "query": query, "results": results, "message": message,
        "board_uid": board_uid, "held": held,
    })


async def _add_searched_title(user, board_uid: str, data: dict):
    """The add itself, for the two ways of asking. Answers (added, refusal), where
    a refusal is the response to send back and `added` is how many titles the
    board gained — nought when it already held it.

    ONE ADD, TWO ANSWERS, and that is the split rather than two adds. A result
    CLICKED is answered with the page, because the page is where the title now is
    and the search has to still be on screen to add a second one. A result DRAGGED
    into a tier cannot be: the row has to land where the pointer dropped it, which
    is a position among siblings no swap can name, so that path takes the count
    back as JSON and puts the row there itself (static/js/ranker/dnd.js).
    """
    query = str(data.get("query") or "").strip()
    try:
        wanted = ranker.parse_item_key(data.get("key"))
        media = ranker_sources.parse_media(wanted.media)
    except (ranker.RankerError, ValueError) as exc:
        return 0, authz.error(str(exc))
    item = await ranker_sources.title_from_search(query, media, str(wanted))
    if item is None:
        return 0, authz.error("That search result is out of date — search for it again.")
    try:
        return await ranker.add_titles(user.user_id, board_uid, [item],
                                       added_from="manual"), None
    except ranker.RankerError as exc:
        return 0, _refusal(exc)


@guard.post("/rankings/boards/{board_uid}/items/add", AuthLevel.RANKER_APPROVED)
async def add_searched_title(board_uid: str, request: Request):
    """Add the title a result names, and answer with the page — the search still
    on it, the result now drawn as held, and the board carrying its new version.

    THE SEARCH IS REDRAWN FROM THE CACHE its own request filled, which is what
    makes answering with the whole page affordable here: nothing is asked
    upstream and the search budget never sees it. Without that the box would come
    back empty and adding a second title from one search would mean typing it
    again.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    query = str(data.get("query") or "").strip()
    media_name = str(data.get("media") or "")
    _, refusal = await _add_searched_title(user, board_uid, data)
    if refusal is not None:
        return refusal
    results = ()
    try:
        media = ranker_sources.parse_media(media_name, Media.SHOW)
        refs = await ranker_sources.search_source().search(query, media, cached_only=True)
        results = [
            {**item, "key": ranker.item_key(item["media"], item["match_source"],
                                            item["match_id"])}
            for item in ranker_sources.items_from_refs(refs)
        ][:MAX_SEARCH_RESULTS]
    except (ValueError, ranker_sources.SourceUnavailable):
        # The search cannot be redrawn, which is not a reason to refuse the add
        # that already happened: the page comes back without it.
        query = ""
    return await _rankings_response(
        request, user, board_uid, notice="Added to the pool.",
        pool_first=str(ranker.parse_item_key(data.get("key"))),
        search={"query": query, "media": media_name, "results": results,
                "held": await ranker.board_keys(user.user_id, board_uid)})


@guard.post("/api/rankings/boards/{board_uid}/items", AuthLevel.RANKER_APPROVED)
async def add_board_items(board_uid: str, request: Request):
    """Add a title to the board's pool for the DRAG, which needs the count back
    rather than a page. Idempotent: a title the board already holds — pooled or
    tiered — is left exactly as it is.

    THE PAGE NAMES A RESULT; IT DOES NOT HAND OVER A TITLE. It sends the query it
    searched for and the key of the result picked, and the title is read back out
    of that search's own stored answer (`ranker_sources.title_from_search`). What
    is stored is what the service said, not fields a page sent — a page that sends
    a title or an id map alongside has them ignored — and the add costs no second
    lookup. A result no longer stored is refused with a sentence rather than
    looked up afresh, so this cannot become a search the budget does not count.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    added, refusal = await _add_searched_title(user, board_uid, data)
    if refusal is not None:
        return refusal
    return JSONResponse({"ok": True, "added": added})


@guard.post("/rankings/boards/{board_uid}/removals/restore", AuthLevel.RANKER_APPROVED)
async def restore_board_item(board_uid: str, request: Request):
    """Put a removed title back where it sat — see ranker.restore_removal for what
    that means in each case — and answer with the board redrawn.

    THE WHOLE PAGE, NOT JSON, AND THAT IS WHAT TAKES THE RESTORE OUT OF SCRIPT.
    The Restore button is markup (hx-post with hx-json-enc, _ranker_removed.html)
    and its answer is what switching to this board draws, so the page's copy of
    the layout and the version a save echoes are read back from the server rather
    than adjusted by a script. WHAT THAT COSTS, and it was accepted by the owner:
    a drag made in the last moment and not saved yet is lost, and so are the
    search results, a selection and the tiers opened by hand on a board large
    enough to draw them closed. Switching boards already costs exactly that.

    ALWAYS 200, WITH THE OUTCOME SAID ON THE PAGE (`notice`), including a refusal.
    This answer replaces the page, and a non-200 is not swapped, so a status code
    would leave the viewer on a page that did nothing and said nothing.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    notice_ok = True
    try:
        restored = await ranker.restore_removal(user.user_id, board_uid, data.get("key"))
        notice = ("Put back on the board." if restored else
                  "That title is already on this board, so there was nothing to put back.")
    except ranker.RankerError as exc:
        notice, notice_ok = str(exc), False
    return await _rankings_response(request, user, board_uid, notice=notice,
                                    notice_ok=notice_ok, removals_open=True)


@guard.get("/rankings/fragments/import-years", AuthLevel.RANKER_APPROVED)
async def import_years_fragment(request: Request):
    """The years this account actually has finished titles for, for one medium.

    A FRAGMENT RATHER THAN BOTH LISTS DRAWN AND ONE CHOSEN BY CSS, and that is not
    a preference: a `display: none` <select> still submits its value, so two lists
    in the page would put two `year` fields in the body and the reader's choice
    would be whichever the browser serialized last. One list is the only shape
    that serializes one answer.
    """
    user = await auth.current_user(request)
    media = _media(request.query_params.get("media"), Media.SHOW)
    years: list[int] = []
    if await ranker_import.tracker_available(user.user_id):
        years = await ranker_import.available_years(user.user_id, media)
    return templates.TemplateResponse(
        request, "_ranker_import_years.html", {"request": request, "years": years})


@guard.post("/rankings/boards/{board_uid}/import", AuthLevel.RANKER_APPROVED)
async def import_finished_titles(board_uid: str, request: Request):
    """Add everything this account has finished watching to the board's pool.

    ABSENT RATHER THAN REFUSED when the account has no such source: the page draws
    neither the button nor the dialog, and this says the same thing in a sentence
    for the request that arrived anyway.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    media = _media(data.get("media"), Media.SHOW)
    year = data.get("year")
    if year in (None, ""):
        year = None
    else:
        try:
            year = int(year)
        except (TypeError, ValueError):
            return await _rankings_response(request, user, board_uid, notice_ok=False,
                                            notice="Year must be a whole number.")
    if not await ranker_import.tracker_available(user.user_id):
        return await _rankings_response(request, user, board_uid, notice_ok=False,
                                        notice="There is nothing to import from.")
    source = ranker_import.finished_titles_source()
    refs = await source.finished_titles(user.user_id, media=media, year=year)
    try:
        added = await ranker.add_titles(
            user.user_id, board_uid, ranker_sources.items_from_refs(refs),
            added_from="tracker",
        )
        notice, ok = f"Found {len(refs)}, added {added}.", True
    except ranker.RankerError as exc:
        notice, ok = str(exc), False
    return await _rankings_response(request, user, board_uid, notice=notice, notice_ok=ok)


@guard.get("/rankings/fragments/seed", AuthLevel.RANKER_APPROVED)
async def seed_control_fragment(request: Request):
    """The seed control back in its resting state — what Cancel asks for."""
    return _seed_answer(request, request.query_params.get("board") or "")


@guard.post("/rankings/boards/{board_uid}/seed", AuthLevel.RANKER_APPROVED)
async def seed_from_ratings(board_uid: str, request: Request):
    """Arrange titles into tiers from the scores this account has already given
    them, using ITS OWN credential — ratings are private data, unlike a search.

    THE DRY RUN ANSWERS A CONFIRMATION WHOSE BUTTON COMMITS, replacing the control
    that asked (_ranker_seed.html, the shape templates/_add_all.html gave the
    calendar's own month action). So the counts a seed is about to produce are the
    server's sentence about its own dry run rather than one composed in the
    browser from a JSON answer, and pressing the confirmation is the same request
    with `commit` on.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    commit = route_params.ticked(data.get("commit"))
    if not await ranker_sources.ratings_available(user.user_id):
        return await _rankings_response(request, user, board_uid, notice_ok=False,
                                        notice="There are no ratings to seed from.")
    source = ranker_sources.ratings_source()
    try:
        rated = await source.fetch_ratings(user.user_id)
    except ranker_sources.SourceUnavailable as exc:
        return _seed_answer(request, board_uid, message=str(exc), ok=False)
    entries = [
        item for rating in rated
        if (item := rating.title.as_item(user_rating=rating.rating)) is not None
    ]
    try:
        summary = await ranker.seed_ratings(user.user_id, board_uid, entries, commit=commit)
    except ranker.RankerError as exc:
        if commit:
            return await _rankings_response(request, user, board_uid,
                                            notice=str(exc), notice_ok=False)
        return _seed_answer(request, board_uid, message=str(exc), ok=False)
    if commit:
        return await _rankings_response(
            request, user, board_uid,
            notice=f"Seeded {summary['titles_placed']} title(s) into "
                   f"{summary['tiers_created']} new tier(s).")
    return _seed_answer(
        request, board_uid,
        confirm=f"This would add {summary['titles_added']} title(s), place "
                f"{summary['titles_placed']} and create {summary['tiers_created']} "
                f"tier(s). {summary['already_placed']} are already in a tier and "
                "will not be moved.")


def _seed_answer(request: Request, board_uid: str, *, confirm: str = "",
                 message: str = "", ok: bool = True) -> Response:
    """The seed control in whichever of its states this answer leaves it in: the
    button, the confirmation it turns into, or the button again with a line
    saying why nothing happened. One partial, because each answer REPLACES the
    element that was pressed."""
    return templates.TemplateResponse(request, "_ranker_seed.html", {
        "request": request, "board_uid": board_uid, "confirm": confirm,
        "message": message, "ok": ok,
    })


# ---------------------------------------------------------------------------
# taking a ranking away: the image, the preview and the Markdown
# ---------------------------------------------------------------------------
# All three consolidate through exports.py and only then diverge, so
# the picture and the text block a user posts beside it are always the same
# ranking. Nothing below decides an ordering of its own.

@dataclass(frozen=True)
class _ExportOptions:
    """A validated export request. Parsed once and shared by the three routes,
    so the preview cannot be rendered from different options than the download
    it is previewing."""
    top_x: int
    columns: int
    title: str
    username: str
    scope: str
    category_uid: str | None
    scale: float
    fmt: str
    show_titles: bool
    podium: bool
    header_image: Any


def _parse_export(data: Mapping[str, Any]) -> _ExportOptions:
    """Validate an export request, raising ExportError with a message meant for
    the person who asked. Nothing here reaches the database or the renderer.

    IT READS A FORM'S OWN FIELDS, which is what took the export options out of
    script: every value arrives as the string a control holds, the two checkboxes
    arrive only when they are ticked, and the two compound choices — which tier to
    export, which picture to put at the top — arrive as the one value their
    <select> can carry. Both used to be split into a pair of keys in the browser.
    """
    top_x = _whole_number(data.get("top_x"), "Top X", 1, ranker_export.MAX_TOP_X)
    columns = _whole_number(data.get("columns"), "Columns", min(EXPORT_COLUMNS),
                            max(EXPORT_COLUMNS))
    if columns not in EXPORT_COLUMNS:
        raise ranker_export.ExportError(
            "Columns must be one of: " + ", ".join(str(c) for c in EXPORT_COLUMNS) + "."
        )
    scale = data.get("scale", 1.0)
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        raise ranker_export.ExportError("Size must be a number.") from None
    if scale not in EXPORT_SCALES:
        raise ranker_export.ExportError("Size must be either full or half.")

    fmt = str(data.get("fmt") or "webp").lower()
    if fmt not in grid_builder.FORMATS:
        raise ranker_export.ExportError("Format must be WebP, JPEG or PNG.")
    scope, category_uid = _export_scope(data.get("scope"))

    return _ExportOptions(
        top_x=top_x, columns=columns,
        title=_export_text(data.get("title"), "title"),
        username=_export_text(data.get("username"), "name"),
        scope=scope, category_uid=category_uid,
        scale=scale, fmt=fmt,
        show_titles=route_params.ticked(data.get("show_titles")),
        podium=route_params.ticked(data.get("podium")),
        header_image=_header_choice(data.get("header_image")),
    )


# What the two compound <select>s mean, spelled where the request is read rather
# than where it is built: `cat:<uid>` is one tier and anything else is the whole
# board; `img:<uid>` and `provider:<name>` are the two kinds of saved picture, and
# `avatar` is the account's own. The bare prefixes are the markup's too — the
# options are rendered from the same constants — so there is one spelling of each.
CATEGORY_SCOPE_PREFIX = "cat:"
SAVED_IMAGE_PREFIX = "img:"
PROVIDER_IMAGE_PREFIX = "provider:"


def _export_scope(value: Any) -> tuple[str, str | None]:
    chosen = str(value or "global")
    if chosen.startswith(CATEGORY_SCOPE_PREFIX):
        uid = chosen[len(CATEGORY_SCOPE_PREFIX):]
        if not uid:
            raise ranker_export.ExportError("Choose which tier to export.")
        return "category", uid
    if chosen != "global":
        raise ranker_export.ExportError(
            "Export scope must be either the whole board or one tier.")
    return "global", None


def _header_choice(value: Any) -> Any:
    """The header picture as `_header_bytes` names it: the bare string for the
    avatar, and one keyed object each for a saved image and a service's picture.
    Neither name is trusted here — both are checked against what this account
    actually owns before either becomes a path."""
    chosen = str(value or "")
    if chosen == "avatar":
        return "avatar"
    if chosen.startswith(SAVED_IMAGE_PREFIX):
        return {"image_uid": chosen[len(SAVED_IMAGE_PREFIX):]}
    if chosen.startswith(PROVIDER_IMAGE_PREFIX):
        return {"provider": chosen[len(PROVIDER_IMAGE_PREFIX):]}
    return None


def _whole_number(value: Any, what: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ranker_export.ExportError(f"{what} must be a whole number.") from None
    if not low <= number <= high:
        raise ranker_export.ExportError(f"{what} must be between {low} and {high}.")
    return number


def _export_text(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_EXPORT_TEXT:
        raise ranker_export.ExportError(
            f"The {what} can be at most {MAX_EXPORT_TEXT} characters."
        )
    return text


def _header_bytes(user_id: int, spec: Any) -> bytes | None:
    """The icon for the banner: this account's avatar, or one of the images it
    has saved, or nothing.

    A SAVED IMAGE IS NAMED, NOT UPLOADED HERE. The uid has to be one this
    account actually owns — checked against its own list rather than trusted —
    which also means the bytes were validated and normalized when they were
    uploaded, and this route never has to decode anything a client just sent it.

    A PROVIDER PICTURE IS THE THIRD SHAPE, AND IT IS VALIDATED THE SAME WAY: the
    name is checked for membership of a FIXED set before it can become a path
    component, exactly as a uid is checked against this account's own list.
    Neither one is ever interpolated into a path on the strength of the request
    alone, which is the property worth stating rather than the mechanism.
    """
    if not spec:
        return None
    if spec == "avatar":
        path = user_images.avatar_path(user_id)
    elif isinstance(spec, dict) and (uid := spec.get("image_uid")):
        if str(uid) not in user_images.list_images(user_id):
            raise ranker_export.ExportError("That saved image is not available.")
        path = user_images.image_path(user_id, str(uid))
    elif isinstance(spec, dict) and (slot := spec.get("provider")):
        if str(slot) not in user_images.list_provider_avatars(user_id):
            raise ranker_export.ExportError("That service picture is not available.")
        path = user_images.provider_avatar_path(user_id, str(slot))
    else:
        raise ranker_export.ExportError(
            "Choose the avatar, a service picture, or one of your saved images.")
    try:
        return path.read_bytes()
    except OSError:
        # An avatar that was never uploaded is the ordinary case, not an error:
        # the header simply lays out without an icon.
        return None


def _grid_entries(ranked: list[ranker_export.RankedTitle]) -> list[grid_builder.GridEntry]:
    """The ranking as the renderer wants it, with each poster resolved from the
    cache on disk.

    NO NETWORK HERE, and none inside the renderer either. A title whose poster
    has not been warmed renders the placeholder tile; warming is its own
    explicit step, so an export never waits on a provider.
    """
    return [
        grid_builder.GridEntry(
            rank=entry.rank,
            title=entry.title,
            poster=posters.cached_poster(entry.media, entry.tmdb) if entry.tmdb else None,
            colour=entry.colour,
        )
        for entry in ranked
    ]


async def _render(entries: list[grid_builder.GridEntry], options: _ExportOptions, *,
                  scale: float, fmt: str, header: bytes | None) -> bytes:
    """Run a render off the event loop, one of at most two at a time.

    Pillow is synchronous CPU work measured in seconds at full size. Calling it
    directly would stall every other request on this worker for the duration —
    not slow them, stop them.
    """
    work = functools.partial(
        grid_builder.build_grid, entries,
        columns=options.columns, title=options.title, username=options.username,
        header_image=header, scale=scale, fmt=fmt,
        show_titles=options.show_titles, podium=options.podium,
    )
    # THE WAIT AND THE WORK ARE TIMED APART, because there are two render slots
    # instance-wide: a third export does nothing at all until one frees, and a
    # single span covering both would report that queueing as Pillow being slow
    # and send the next person reading it to optimise the wrong thing.
    with span("ranker.export_wait", entries=len(entries)):
        await _render_slots.acquire()
    try:
        with span("ranker.export_render", entries=len(entries), scale=scale, fmt=fmt):
            return await anyio.to_thread.run_sync(work)
    finally:
        _render_slots.release()


async def _export_cooldown_remaining(user_id: int) -> int:
    """Seconds this account still has to wait before another render, or 0.

    ASKING DOES NOT RESTART THE WAIT. Recording the refusal would push the window
    forward on every rejected click, so somebody pressing the button while they
    wait would never be let through — the countdown has to count down. The
    attempt is recorded where the work actually begins, below.

    Stored in `login_attempts` like every other volume throttle here, so it is
    one budget however many workers the instance runs.
    """
    return await auth.cooldown_remaining(
        "ranker_export", str(user_id), window_seconds=EXPORT_COOLDOWN_SECONDS,
    )


async def _consolidated(user_id: int, board_uid: str, options: _ExportOptions):
    """The board and the ordered slice an export is of."""
    board = await ranker.fetch_board(user_id, board_uid)
    ranked = ranker_export.consolidate(
        board, scope=options.scope, category_uid=options.category_uid,
        top_x=options.top_x,
    )
    return board, ranked


def _export_context(user_id: int, board: dict) -> dict:
    """Everything the export dialog's form is drawn from.

    THE CHOICES ARE THE SERVER'S, ALL OF THEM: the column counts and the scales it
    accepts, each format with the ceiling it is refused at, the tiers this board
    has, and the pictures this account owns — which used to be fetched from
    /api/me/images and turned into <option>s in the browser. Nothing on the page
    decides what may be asked for any more.
    """
    default = ("Movies" if board["media_scope"] == "movie" else
               "Shows" if board["media_scope"] == "show" else "Titles")
    year = board["year"] or datetime.now(timezone.utc).year
    return {
        "board_uid": board["uid"],
        "export_title": f"Top {default} of {year}",
        "export_columns": EXPORT_COLUMNS,
        "export_scales": EXPORT_SCALES,
        "export_formats": [
            {"value": fmt, "label": label,
             "limit": f"{grid_builder.MAX_DIMENSION[fmt]:,}"}
            for fmt, label in (("webp", "WebP — smallest"),
                               ("jpeg", "JPEG — widest support"),
                               ("png", "PNG — lossless, largest"))
        ],
        "export_images": user_images.describe_images(user_id),
        "export_providers": user_images.list_provider_avatars(user_id),
        "export_image_cap": user_images.MAX_IMAGES_PER_USER,
        "export_name_cap": user_images.MAX_IMAGE_NAME,
        "max_top_x": ranker_export.MAX_TOP_X,
        "max_export_text": MAX_EXPORT_TEXT,
    }


@guard.post("/rankings/boards/{board_uid}/header-image", AuthLevel.RANKER_APPROVED)
async def add_header_image(board_uid: str, request: Request):
    """Save a picture for the top of an exported grid, and choose it.

    THE BYTES ARE VALIDATED AND NORMALIZED IN ONE PLACE — user_images.add_image,
    which the account page's own library calls too — so this route decides
    nothing about them. What it decides is what the reader sees next: the picker
    redrawn with the new picture already chosen, and an `export-changed` event so
    the readout and the preview beside it follow without a second press.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request, user_images.MAX_UPLOAD_BODY_BYTES,
                                 user_images.TOO_LARGE_MESSAGE)
    chosen, message = "", ""
    try:
        board = await ranker.fetch_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return authz.error(str(exc), 404)
    try:
        uid = await user_images.add_image(
            user.user_id, str(data.get("image_b64") or ""), str(data.get("name") or ""))
        chosen = f"{SAVED_IMAGE_PREFIX}{uid}"
    except user_images.TooManyImages:
        message = (f"You can save up to {user_images.MAX_IMAGES_PER_USER} pictures. "
                   "Remove one on your account page first.")
    except user_images.ValidationError as exc:
        message = str(exc)
    return templates.TemplateResponse(
        request, "_ranker_header_pick.html",
        {"request": request, "chosen": chosen, "message": message,
         **_export_context(user.user_id, board)},
        headers={"HX-Trigger": "export-changed"} if chosen else None,
    )


def _preview_url(query: str) -> str:
    return f"/rankings/preview.webp?{query}" if query else "/rankings/preview.webp"


@guard.get("/rankings/fragments/export", AuthLevel.RANKER_APPROVED)
async def export_readout_fragment(request: Request):
    """How big this export comes out, what it would look like, and whether it can
    be downloaded at all.

    THE SIZE IS THE RENDERER'S OWN ARITHMETIC. It used to be computed a second
    time in the browser, from layout constants the page was sent for no other
    reason — so the number a reader saw and the geometry the pre-render check
    refused on were two implementations of one thing, and the constants had to be
    published to keep them agreeing. `compute_layout` is now asked directly, which
    is the same call the download makes before it allocates a pixel.

    A GET, BECAUSE IT CHANGES NOTHING and because the preview beside it has to be
    an <img>. What rides on the address is the options being composed — a title
    and a name somebody is typing, which no server could look them up — and every
    other field is a choice out of a set this route owns.
    """
    user = await auth.current_user(request)
    board_uid = request.query_params.get("board") or ""
    query = str(request.url.query)
    try:
        options = _parse_export(request.query_params)
        board, ranked = await _consolidated(user.user_id, board_uid, options)
    except ranker.RankerError as exc:
        return _export_readout(request, message=str(exc))
    except ranker_export.ExportError as exc:
        return _export_readout(request, message=str(exc))

    layout = grid_builder.compute_layout(
        len(ranked), columns=options.columns, scale=options.scale,
        show_titles=options.show_titles, podium=options.podium,
    )
    # A board with nothing tiered never reaches here: `consolidate` refuses it,
    # and its sentence is the one drawn above.
    over = not layout.fits(options.fmt)
    if over:
        limit = grid_builder.MAX_DIMENSION[options.fmt]
        message = (f"{layout.width} × {layout.height}px — too tall for "
                   f"{options.fmt.upper()} (limit {limit:,}px). Try more columns, "
                   "half size, or PNG.")
    else:
        message = f"{layout.width} × {layout.height}px"
    emojis, default_emoji = await ranker_import.network_emojis(user.user_id)
    return _export_readout(
        request, message=message, over=over, count=len(ranked), query=query,
        header=_header_thumb(options.header_image),
        markdown=ranker_export.to_markdown(ranked, title=options.title, emojis=emojis,
                                           default_emoji=default_emoji),
    )


def _export_readout(request: Request, *, message: str, over: bool = True, count: int = 0,
                    query: str = "", header: str = "", markdown: str = "") -> Response:
    """The dialog's live half: the size line, the preview, the Markdown the copy
    button takes, and — out of band, because they sit elsewhere in the dialog —
    the Download button in the state this size leaves it in and the thumbnail of
    the picture now chosen."""
    return templates.TemplateResponse(request, "_ranker_export_readout.html", {
        "request": request, "message": message, "over": over, "count": count,
        "preview_url": _preview_url(query) if count and not over else "",
        "header_thumb": header, "markdown": markdown,
    })


def _header_thumb(spec: Any) -> str:
    """The address of the picture a header choice names, at thumbnail size. The
    avatar route resizes its master on demand, so this asks for 96px of it rather
    than the 512 it holds."""
    if spec == "avatar":
        return "/api/me/avatar?size=96"
    if isinstance(spec, dict) and (name := spec.get("provider")):
        return f"/api/me/avatar/source/{quote(str(name))}?size=96"
    if isinstance(spec, dict) and (uid := spec.get("image_uid")):
        return f"/api/me/images/{quote(str(uid))}"
    return ""


@guard.get("/rankings/preview.webp", AuthLevel.RANKER_APPROVED)
async def preview_grid(request: Request):
    """A small render of exactly what the export would produce.

    Same consolidation, same renderer, same options — only the scale differs, so
    what is previewed is what is downloaded. Exempt from the export cooldown: a
    preview is what stops somebody exporting repeatedly to find the settings they
    wanted.

    A GET WHERE THE DOWNLOAD IS A POST, and the difference is what each one spends.
    `export_grid` spends a per-account cooldown, so as a GET a link on another site
    could spend it for whoever followed it — SameSite=Lax cookies ride a top-level
    GET navigation. This spends a render slot and nothing that is recorded, and a
    cross-site <img> carries no cookie at all, so it cannot be spent from anywhere
    but a page of this app.
    """
    user = await auth.current_user(request)
    board_uid = request.query_params.get("board") or ""
    try:
        options = _parse_export(request.query_params)
        _, ranked = await _consolidated(user.user_id, board_uid, options)
        header = _header_bytes(user.user_id, options.header_image)
    except ranker.RankerError:
        return Response(status_code=404)
    except ranker_export.ExportError:
        # The readout beside this one says what is wrong in words; a picture that
        # cannot be drawn has nowhere to say it.
        return Response(status_code=404)

    scale = PREVIEW_WIDTH / (options.columns * grid_builder.TILE_W)
    try:
        payload = await _render(_grid_entries(ranked), options, scale=scale,
                                fmt=PREVIEW_FORMAT, header=header)
    except grid_builder.GridError:
        # A preview is small enough that no allowed request reaches a format
        # ceiling, but the renderer is the authority on that rather than this
        # route's arithmetic about it.
        return Response(status_code=404)
    return Response(payload, media_type="image/webp",
                    headers={"Cache-Control": "no-store"})


@guard.post("/api/rankings/boards/{board_uid}/export", AuthLevel.RANKER_APPROVED)
async def export_grid(board_uid: str, request: Request):
    """The finished image.

    THE SIZE CHECK RUNS BEFORE ANY PIXEL IS ALLOCATED. WebP cannot encode above
    16383 pixels in either direction, so a hundred titles in three columns is a
    canvas its encoder throws on — after thirty seconds of rendering. The same
    geometry the renderer will use is computed first and checked against the
    chosen format's ceiling, and the refusal says what the canvas came to, what
    the limit is, and what would get under it.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    try:
        options = _parse_export(data)
        board, ranked = await _consolidated(user.user_id, board_uid, options)
        header = _header_bytes(user.user_id, options.header_image)
    except ranker.RankerError as exc:
        return _refusal(exc)
    except ranker_export.ExportError as exc:
        return authz.error(str(exc))

    layout = grid_builder.compute_layout(
        len(ranked), columns=options.columns, scale=options.scale,
        show_titles=options.show_titles, podium=options.podium,
    )
    if not layout.fits(options.fmt):
        return authz.error(
            str(grid_builder.CanvasTooLarge(
                layout.width, layout.height, options.fmt,
                grid_builder.MAX_DIMENSION[options.fmt],
            )),
            reason="canvas_too_large",
            width=layout.width, height=layout.height,
            limit=grid_builder.MAX_DIMENSION[options.fmt], format=options.fmt,
        )

    key = ranker_export.render_key(
        ranked, renderer_version=grid_builder.RENDERER_VERSION,
        columns=options.columns, scale=options.scale, fmt=options.fmt,
        show_titles=options.show_titles, podium=options.podium,
        title=options.title, username=options.username, header_image=header,
    )
    year = board["year"] or datetime.now(timezone.utc).year
    path = ranker_export.cache_path(user.user_id, int(year), board_uid, key, options.fmt)

    payload = await anyio.to_thread.run_sync(ranker_export.cached_render, path)
    if payload is None:
        # Checked only when something actually has to be rendered, so downloading
        # the same image again — which costs nothing — is never refused.
        wait = await _export_cooldown_remaining(user.user_id)
        if wait:
            return authz.error(
                f"Exports are limited to one every {EXPORT_COOLDOWN_SECONDS} seconds. "
                f"Try again in {wait} second{'' if wait == 1 else 's'}.",
                429, reason="export_cooldown", retry_after=wait,
            )
        # Recorded HERE, where the render is actually about to happen, so the
        # cooldown measures work done rather than requests made.
        await auth.record_attempt("ranker_export", str(user.user_id), True)
        payload = await _render(_grid_entries(ranked), options,
                                scale=options.scale, fmt=options.fmt, header=header)
        await anyio.to_thread.run_sync(ranker_export.store_render, path, payload)

    filename = ranker_export.download_name(
        (board["name"], options.title), options.fmt,
    )
    return Response(payload, media_type=f"image/{options.fmt}", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })


# THE MARKDOWN RIDES WITH THE READOUT rather than having a route of its own.
# A ranking is already an ordered list, so the text costs no renderer, no
# semaphore and no cooldown — it is computed beside the size every time an option
# changes and put in a field the copy button names (`data-copy`, static/js/ui.js).
# Writing the clipboard is the only part of copying a browser has to do, and it
# is the only part left.
