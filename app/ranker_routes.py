"""The rankings page and its board API.

HTTP only: parse a request, validate its shape, delegate to app/ranker.py, and
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

import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import assets, auth, authz, posters, ranker, ranker_import, ranker_sources
from .auth import AuthLevel
from .config import load_settings
from .ranker_sources import Media

router = APIRouter()
guard = authz.Guard(router)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

# `private`: this response requires a session, so a shared cache in front of
# the app has no business holding a copy.
_POSTER_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}

# A layout for a board at its 1000-item cap is a list of short keys and comes in
# well under this; anything larger is not a board this feature can produce, so it
# is refused before it is parsed rather than after it has been held in memory.
MAX_BODY_BYTES = 1024 * 1024

# Search is the one route here that reaches a provider on every call, so it is
# the one that can be turned into an amplifier. The window is short and the
# allowance generous because the caller is a signed-in, approved account typing
# into a debounced box — this bounds a script, not a person.
#
# COUNTED IN PROCESS, not in the database. `login_attempts` is the app's other
# limiter, but its `key_type` is a closed CHECK set and this feature is not
# allowed a schema change of its own; and the budget being approximate across a
# restart is fine for a throttle whose window is a minute. The assumption this
# rests on is that ONE worker serves the instance — the Dockerfile's CMD runs
# hypercorn with no `--workers` — so this dict is the whole instance's view. Add
# workers and the effective allowance multiplies by their count, at which point
# this needs to move somewhere shared.
SEARCH_MAX_PER_WINDOW = 30
SEARCH_WINDOW_SECONDS = 60
MIN_SEARCH_LENGTH = 2
MAX_SEARCH_RESULTS = 20

# One warm request stays a bounded piece of work: the client asks for the page
# of titles it is about to show, not for the whole board.
MAX_WARM_ITEMS = 250


_search_hits: dict[int, deque[float]] = {}


def _search_over_budget(user_id: int, now: float | None = None) -> bool:
    """Whether this account has already spent its search allowance, recording
    this request either way. Volume-only — there is no failed search to count
    separately."""
    ts = time.monotonic() if now is None else now
    hits = _search_hits.setdefault(user_id, deque())
    while hits and hits[0] <= ts - SEARCH_WINDOW_SECONDS:
        hits.popleft()
    if not hits:
        # Nothing left in the window: drop the key rather than accumulate one
        # empty deque per account that has ever searched.
        _search_hits.pop(user_id, None)
        hits = _search_hits.setdefault(user_id, deque())
    hits.append(ts)
    return len(hits) > SEARCH_MAX_PER_WINDOW


def _error(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


async def _json_body(request: Request) -> dict:
    """Require a JSON object body, within the size cap."""
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="That request is too large.")
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed JSON body.") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return data


def _refusal(exc: ranker.RankerError) -> JSONResponse:
    """The one place a data-layer refusal becomes a status code.

    A board that isn't the caller's answers 404 exactly as a board that does not
    exist does — the two are indistinguishable on purpose, because a caller able
    to tell them apart could enumerate other people's boards.
    """
    if isinstance(exc, ranker.BoardNotFound):
        return _error("No such board.", 404)
    if isinstance(exc, ranker.VersionConflict):
        return _error(str(exc), 409, reason="version_conflict")
    return _error(str(exc), 400)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

@guard.get("/rankings", AuthLevel.RANKER_APPROVED)
async def rankings_page(request: Request):
    """The ranker's own page. Standalone: it needs nothing from the calendar or
    from any other feature to be useful."""
    user = await auth.current_user(request)
    return templates.TemplateResponse(request, "ranker.html", {
        "request": request,
        "is_admin": bool(user and user.is_admin),
        "boards": await ranker.fetch_boards(user.user_id),
        "asset_v": assets.ASSET_VERSION,
    })


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
    data = await _json_body(request)
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
    data = await _json_body(request)
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
    await _json_body(request)
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


@guard.post("/api/rankings/boards/{board_uid}/save", AuthLevel.RANKER_APPROVED)
async def save_board_layout(board_uid: str, request: Request):
    """Store an arrangement. 409 when another tab saved first, which the UI
    answers by reloading rather than by guessing whose version wins."""
    user = await auth.current_user(request)
    data = await _json_body(request)
    try:
        result = await ranker.save_layout(user.user_id, board_uid, data)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, **result})


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
    data = await _json_body(request)
    keys = data.get("keys")
    if keys is not None and not isinstance(keys, list):
        return _error("`keys` must be a list of item keys.")
    try:
        board = await ranker.fetch_board(user.user_id, board_uid)
    except ranker.RankerError as exc:
        return _refusal(exc)

    tiered = [item for category in board["categories"] for item in category["items"]]
    known = {item["key"]: item for item in [*tiered, *board["pool"]]}
    wanted = [known[key] for key in keys if key in known] if keys is not None else tiered
    if len(wanted) > MAX_WARM_ITEMS:
        return _error(f"Warm at most {MAX_WARM_ITEMS} titles at a time.")

    pairs = [(item["media"], item["tmdb"]) for item in wanted if item["tmdb"]]
    generated = await posters.ensure_posters(load_settings(), pairs)
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
# Every route below goes through app/ranker_sources.py (and, for the optional
# import, app/ranker_import.py). None of them names a provider, which is what
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
    sources: dict[str, object] = {"search": True}
    if await ranker_sources.ratings_available(user.user_id):
        sources["ratings"] = True
    if await ranker_import.tracker_available(user.user_id):
        sources["import"] = {
            "years": {
                str(media): await ranker_import.available_years(user.user_id, media)
                for media in Media
            },
        }
    return JSONResponse({"ok": True, "sources": sources})


@guard.post("/api/rankings/search", AuthLevel.RANKER_APPROVED)
async def search_titles(request: Request):
    """Find titles to add. Uses the INSTANCE credential, deliberately: this
    grant does not imply a linked account, and what is asked for is public
    catalogue data either way."""
    user = await auth.current_user(request)
    data = await _json_body(request)
    query = str(data.get("query") or "").strip()
    media = _media(data.get("media"), Media.SHOW)
    if len(query) < MIN_SEARCH_LENGTH:
        return _error(f"Type at least {MIN_SEARCH_LENGTH} characters to search.")
    if _search_over_budget(user.user_id):
        return _error("Too many searches just now — give it a moment.", 429)

    source = ranker_sources.search_source()
    try:
        refs = await source.search(query, media)
    except ranker_sources.SourceUnavailable as exc:
        return _error(str(exc), 502)
    # The results come back in the same shape the add endpoint takes, so the
    # client hands back exactly what the user picked rather than rebuilding it.
    results = [
        {**item, "key": ranker.item_key(item["media"], item["match_source"], item["match_id"])}
        for item in ranker_sources.items_from_refs(refs)
    ][:MAX_SEARCH_RESULTS]
    return JSONResponse({"ok": True, "results": results})


@guard.post("/api/rankings/boards/{board_uid}/items", AuthLevel.RANKER_APPROVED)
async def add_board_items(board_uid: str, request: Request):
    """Add chosen titles to a board's pool. Idempotent: a title the board
    already holds — pooled or tiered — is left exactly as it is."""
    user = await auth.current_user(request)
    data = await _json_body(request)
    refs = data.get("refs")
    if not isinstance(refs, list):
        return _error("`refs` must be a list of titles.")
    try:
        added = await ranker.add_titles(user.user_id, board_uid, refs, added_from="manual")
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "added": added})


@guard.delete("/api/rankings/boards/{board_uid}/items", AuthLevel.RANKER_APPROVED)
async def remove_board_items(board_uid: str, request: Request):
    """Take titles off a board entirely. The only true delete in this feature —
    every other removal returns something to the pool."""
    user = await auth.current_user(request)
    data = await _json_body(request)
    keys = data.get("keys")
    if not isinstance(keys, list):
        return _error("`keys` must be a list of item keys.")
    try:
        removed = await ranker.remove_items(user.user_id, board_uid, keys)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "removed": removed})


@guard.post("/api/rankings/boards/{board_uid}/import/tracker", AuthLevel.RANKER_APPROVED)
async def import_finished_titles(board_uid: str, request: Request):
    """Add everything this account has finished watching to the board's pool.

    404 WHEN THE SOURCE IS NOT AVAILABLE TO THIS ACCOUNT, rather than an error
    explaining what they are missing. The route answers as though it does not
    exist, because for that account it does not.
    """
    user = await auth.current_user(request)
    data = await _json_body(request)
    media = _media(data.get("media"), Media.SHOW)
    year = data.get("year")
    if year not in (None, ""):
        try:
            year = int(year)
        except (TypeError, ValueError):
            return _error("Year must be a whole number.")
    else:
        year = None

    if not await ranker_import.tracker_available(user.user_id):
        return _error("No such source.", 404)
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
    data = await _json_body(request)
    commit = bool(data.get("commit"))
    if not await ranker_sources.ratings_available(user.user_id):
        return _error("No such source.", 404)

    source = ranker_sources.ratings_source()
    try:
        rated = await source.fetch_ratings(user.user_id)
    except ranker_sources.SourceUnavailable as exc:
        return _error(str(exc), 502)
    entries = [
        item for rating in rated
        if (item := rating.title.as_item(user_rating=rating.rating)) is not None
    ]
    try:
        summary = await ranker.seed_ratings(user.user_id, board_uid, entries, commit=commit)
    except ranker.RankerError as exc:
        return _refusal(exc)
    return JSONResponse({"ok": True, "committed": commit, "rated": len(rated), **summary})
