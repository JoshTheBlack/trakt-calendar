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
import logging
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

# One warm request stays a bounded piece of work: the client asks for the page
# of titles it is about to show, not for the whole board.
MAX_WARM_ITEMS = 250

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


def _export_limits() -> dict:
    """The geometry the export modal's live size readout works from.

    SENT RATHER THAN RESTATED IN JAVASCRIPT. The pre-render refusal and the
    number the modal shows beside it have to agree, and the only way to be sure
    of that is for both to come from the constants the renderer itself uses.
    """
    return {
        "tile_w": grid_builder.TILE_W, "tile_h": grid_builder.TILE_H,
        "header_h": grid_builder.HEADER_H, "label_h": grid_builder.LABEL_H,
        "caption_h": grid_builder.CAPTION_H, "gutter": grid_builder.GUTTER,
        "margin": grid_builder.MARGIN, "podium_ranks": grid_builder.PODIUM_RANKS,
        "max_dimension": grid_builder.MAX_DIMENSION,
        "columns": list(EXPORT_COLUMNS), "scales": list(EXPORT_SCALES),
        "max_top_x": ranker_export.MAX_TOP_X,
        "tiers": ranker.MAX_CATEGORIES_PER_BOARD,
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


def _removals_url(board_uid: str) -> str:
    return f"/rankings/fragments/removed?board={quote(board_uid)}"


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
        "next_url": _pool_url(board["uid"], page + 1) if remaining > 0 else None,
        # The sentinel reserves the height its page will take, so arriving cards
        # slot into space already made for them instead of shoving the page down.
        "next_rows": min(remaining, POOL_PAGE_SIZE),
    }


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
        sources["import"] = {
            "years": {
                str(media): await ranker_import.available_years(user_id, media)
                for media in Media
            },
        }
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
                             removals_open: bool = False):
    """The rankings page for the board `wanted`, or for the account's first.

    ONE BUILDER FOR EVERY ANSWER THAT IS THE WHOLE PAGE: opening it, and putting a
    removed title back, which answers with the board redrawn. A second builder
    would be a second idea of what the page carries, and the version a save echoes
    is exactly the kind of thing two of them would disagree about.

    `notice` is a sentence about what the request that drew this page did, and
    `notice_ok` whether it is good news or a refusal, which only decides its
    colour; `removals_open` draws the removal history already open. All three are
    for the restore, whose button sits inside that history.
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
        "pool_page_size": POOL_PAGE_SIZE,
        "board_state": _board_state(board) if board else None,
        "sources": await _sources(user.user_id),
        "rows_open": tiered <= EAGER_ROW_LIMIT,
        "export_limits": _export_limits(),
        "tier_template": ranker.TIER_TEMPLATE,
        "notice": notice,
        "notice_ok": notice_ok,
        "removals_open": removals_open,
    }
    if board:
        context.update(_pool_context(board, 0))
        context.update(await _removals_context(user.user_id, board["uid"]))
        context["removals_url"] = _removals_url(board["uid"])
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
    return templates.TemplateResponse(
        request, "_ranker_pool_page.html", {"request": request, **_pool_context(board, page)},
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
        {"request": request, "items": found[0]["items"], "board_uid": board["uid"]},
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
    except ranker.RankerError:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "_ranker_row.html",
        {"request": request, "item": item, "board_uid": board_uid},
    )


@guard.get("/rankings/fragments/removed", AuthLevel.RANKER_APPROVED)
async def removals_fragment(request: Request):
    """What this board could put back, redrawn after something was removed or
    restored. The history's own container asks for it (see
    _ranker_board_shell.html), so the page's script names an event and never
    this address."""
    user = await auth.current_user(request)
    board_uid = request.query_params.get("board") or ""
    return templates.TemplateResponse(
        request, "_ranker_removed.html",
        {"request": request, **await _removals_context(user.user_id, board_uid)},
    )


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


@guard.post("/api/rankings/boards", AuthLevel.RANKER_APPROVED)
async def create_board(request: Request):
    """Create a board, or clone an existing one when `clone_of` names one.

    Both live on one endpoint because a clone IS a create — same caps, same uid
    rules, same response — differing only in what the new board starts out
    holding.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    try:
        if clone_of := data.get("clone_of"):
            board = await ranker.clone_board(
                user.user_id, str(clone_of), uid=data.get("uid"), name=data.get("name") or "",
            )
        else:
            board = await ranker.create_board(
                user.user_id,
                uid=data.get("uid"),
                name=data.get("name") or "",
                year=data.get("year"),
                media_scope=data.get("media_scope") or "mixed",
            )
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "board": board})


@guard.patch("/api/rankings/boards/{board_uid}", AuthLevel.RANKER_APPROVED)
async def patch_board(board_uid: str, request: Request):
    """Rename a board, or change its year or media scope.

    Only the keys present are applied, so the client sends what changed. An empty
    string for `year` clears it; omitting `year` leaves it alone.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    try:
        board = await ranker.update_board(
            user.user_id, board_uid,
            name=data.get("name"), year=data.get("year"),
            media_scope=data.get("media_scope"),
        )
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "board": board})


@guard.delete("/api/rankings/boards/{board_uid}", AuthLevel.RANKER_APPROVED)
async def remove_board(board_uid: str, request: Request):
    user = await auth.current_user(request)
    await authz.json_body(request)
    try:
        await ranker.delete_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True})


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


@guard.post("/api/rankings/restore", AuthLevel.RANKER_APPROVED)
async def restore_backup(request: Request):
    """Replace the requesting user's boards with an exported document's.

    REPLACE, not merge, in one transaction — see ranker.restore_user_data for
    why merging an arrangement is not a coherent thing to ask for."""
    user = await auth.current_user(request)
    data = await authz.json_body(request, MAX_RESTORE_BYTES)
    try:
        boards = await ranker.restore_user_data(user.user_id, data)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "boards": boards})


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


@guard.post("/api/rankings/boards/{board_uid}/warm", AuthLevel.RANKER_APPROVED)
async def warm_board_posters(board_uid: str, request: Request):
    """Pre-generate the poster tiles a slice of this board is about to show.

    The client names the titles it is rendering — a page of the pool, or the
    tiered items on first open — because a board may hold a thousand and
    warming all of them on every visit would spend the instance's provider
    budget on tiles nobody is looking at. With no keys given it warms whatever
    is tiered, which is the first-open case.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    keys = data.get("keys")
    if keys is not None and not isinstance(keys, list):
        return authz.error("`keys` must be a list of item keys.")
    try:
        board = await ranker.fetch_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return _refusal(exc)

    tiered = [item for category in board["categories"] for item in category["items"]]
    known = {item["key"]: item for item in [*tiered, *board["pool"]]}
    wanted = [known[key] for key in keys if key in known] if keys is not None else tiered
    if len(wanted) > MAX_WARM_ITEMS:
        return authz.error(f"Warm at most {MAX_WARM_ITEMS} titles at a time.")

    pairs = [(item["media"], item["tmdb"]) for item in wanted if item["tmdb"]]
    # THE HEAVIEST THING THE RANKER ASKS FOR, and until now the least visible: up
    # to MAX_WARM_ITEMS titles, each a provider lookup plus an image download.
    # It is bounded in count but not in time, and a cold board pays all of it at
    # once. (It was heavier still until the resize moved to the image proxy —
    # see app/media/posters.py — which took a Pillow decode and re-encode per
    # title off this path.)
    with span("ranker.warm", n=len(pairs)) as sp:
        generated = await posters.ensure_posters(load_settings(), pairs)
        sp.set(generated=generated)
    cached = sum(1 for media, tmdb in pairs if posters.cached_poster(media, tmdb))
    # `missing` counts the titles that will render a placeholder: the ones with
    # no tmdb at all as well as the ones whose lookup came back with nothing.
    return JSONResponse({
        "ok": True, "generated": generated, "cached": cached,
        "missing": len(wanted) - cached,
    })


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
    return _search_results(request, query=query, results=results)


def _search_results(request: Request, *, query: str = "", results=(), message: str = ""):
    return templates.TemplateResponse(request, "_ranker_search_results.html", {
        "request": request, "query": query, "results": results, "message": message,
    })


@guard.post("/api/rankings/boards/{board_uid}/items", AuthLevel.RANKER_APPROVED)
async def add_board_items(board_uid: str, request: Request):
    """Add a title a search on this page found to the board's pool. Idempotent: a
    title the board already holds — pooled or tiered — is left exactly as it is.

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
    query = str(data.get("query") or "").strip()
    try:
        wanted = ranker.parse_item_key(data.get("key"))
        media = ranker_sources.parse_media(wanted.media)
    except (ranker.RankerError, ValueError) as exc:
        return authz.error(str(exc))
    item = await ranker_sources.title_from_search(query, media, str(wanted))
    if item is None:
        return authz.error("That search result is out of date — search for it again.")
    try:
        added = await ranker.add_titles(user.user_id, board_uid, [item], added_from="manual")
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "added": added})


@guard.delete("/api/rankings/boards/{board_uid}/items", AuthLevel.RANKER_APPROVED)
async def remove_board_items(board_uid: str, request: Request):
    """Take titles off a board entirely. The only true delete in this feature —
    every other removal returns something to the pool."""
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    keys = data.get("keys")
    if not isinstance(keys, list):
        return authz.error("`keys` must be a list of item keys.")
    try:
        removed = await ranker.remove_items(user.user_id, board_uid, keys)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "removed": removed})


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


@guard.post("/api/rankings/boards/{board_uid}/import/tracker", AuthLevel.RANKER_APPROVED)
async def import_finished_titles(board_uid: str, request: Request):
    """Add everything this account has finished watching to the board's pool.

    404 WHEN THE SOURCE IS NOT AVAILABLE TO THIS ACCOUNT, rather than an error
    explaining what they are missing. The route answers as though it does not
    exist, because for that account it does not.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    media = _media(data.get("media"), Media.SHOW)
    year = data.get("year")
    if year not in (None, ""):
        try:
            year = int(year)
        except (TypeError, ValueError):
            return authz.error("Year must be a whole number.")
    else:
        year = None

    if not await ranker_import.tracker_available(user.user_id):
        return authz.error("No such source.", 404)
    source = ranker_import.finished_titles_source()
    refs = await source.finished_titles(user.user_id, media=media, year=year)
    try:
        added = await ranker.add_titles(
            user.user_id, board_uid, ranker_sources.items_from_refs(refs),
            added_from="tracker",
        )
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "found": len(refs), "added": added})


@guard.post("/api/rankings/boards/{board_uid}/seed/ratings", AuthLevel.RANKER_APPROVED)
async def seed_from_ratings(board_uid: str, request: Request):
    """Arrange titles into tiers from the scores this account has already given
    them, using ITS OWN credential — ratings are private data, unlike a search.

    Previews by default and only writes when `commit` is true, so the counts are
    seen before a board changes. 404 when the account has nothing linked to read
    ratings from: an action that can never work is not offered.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    commit = bool(data.get("commit"))
    if not await ranker_sources.ratings_available(user.user_id):
        return authz.error("No such source.", 404)

    source = ranker_sources.ratings_source()
    try:
        rated = await source.fetch_ratings(user.user_id)
    except ranker_sources.SourceUnavailable as exc:
        return authz.error(str(exc), 502)
    entries = [
        item for rating in rated
        if (item := rating.title.as_item(user_rating=rating.rating)) is not None
    ]
    try:
        summary = await ranker.seed_ratings(user.user_id, board_uid, entries, commit=commit)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "committed": commit, "rated": len(rated), **summary})


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


def _parse_export(data: dict) -> _ExportOptions:
    """Validate an export body, raising ExportError with a message meant for the
    person who asked. Nothing here reaches the database or the renderer."""
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
    scope = str(data.get("scope") or "global")
    if scope not in ranker_export.SCOPES:
        raise ranker_export.ExportError("Export scope must be either the whole board or one tier.")
    category_uid = data.get("category_uid") or None
    if scope == "category" and not category_uid:
        raise ranker_export.ExportError("Choose which tier to export.")

    return _ExportOptions(
        top_x=top_x, columns=columns,
        title=_export_text(data.get("title"), "title"),
        username=_export_text(data.get("username"), "name"),
        scope=scope, category_uid=str(category_uid) if category_uid else None,
        scale=scale, fmt=fmt,
        show_titles=bool(data.get("show_titles")),
        podium=bool(data.get("podium")),
        header_image=data.get("header_image"),
    )


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


@guard.post("/api/rankings/boards/{board_uid}/preview", AuthLevel.RANKER_APPROVED)
async def preview_grid(board_uid: str, request: Request):
    """A small render of exactly what the export would produce.

    Same consolidation, same renderer, same options — only the scale differs, so
    what is previewed is what is downloaded. Exempt from the export cooldown: a
    preview is what stops somebody exporting repeatedly to find the settings
    they wanted.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    try:
        options = _parse_export(data)
        _, ranked = await _consolidated(user.user_id, board_uid, options)
        header = _header_bytes(user.user_id, options.header_image)
    except ranker.RankerError as exc:
        return _refusal(exc)
    except ranker_export.ExportError as exc:
        return authz.error(str(exc))

    scale = PREVIEW_WIDTH / (options.columns * grid_builder.TILE_W)
    try:
        payload = await _render(_grid_entries(ranked), options, scale=scale,
                                fmt=PREVIEW_FORMAT, header=header)
    except grid_builder.GridError as exc:
        # A preview is small enough that no allowed request reaches a format
        # ceiling, but the renderer is the authority on that rather than this
        # route's arithmetic about it.
        return authz.error(str(exc))
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


@guard.post("/api/rankings/boards/{board_uid}/export/markdown", AuthLevel.RANKER_APPROVED)
async def export_markdown(board_uid: str, request: Request):
    """The same ranking as a pasteable text block.

    A ranking is already an ordered list, so this is the cheapest export there
    is: no renderer, no semaphore, no cooldown. Network emoji come from the
    account's own preferences WHERE THEY EXIST — an account that has none gets
    plain text rather than an error, because nothing in this feature may require
    them.
    """
    user = await auth.current_user(request)
    data = await authz.json_body(request)
    try:
        options = _parse_export(data)
        board, ranked = await _consolidated(user.user_id, board_uid, options)
    except ranker.RankerError as exc:
        return _refusal(exc)
    except ranker_export.ExportError as exc:
        return authz.error(str(exc))

    emojis, default_emoji = await ranker_import.network_emojis(user.user_id)
    markdown = ranker_export.to_markdown(
        ranked, title=options.title, emojis=emojis, default_emoji=default_emoji,
    )
    return JSONResponse({
        "ok": True,
        "markdown": markdown,
        "filename": ranker_export.download_name((board["name"], options.title), "md"),
        "count": len(ranked),
    })
