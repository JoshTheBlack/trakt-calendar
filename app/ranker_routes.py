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

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import assets, auth, authz, posters, ranker
from .auth import AuthLevel
from .config import load_settings

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
