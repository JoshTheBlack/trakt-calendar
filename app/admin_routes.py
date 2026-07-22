"""The admin screen: accounts, invites, and retired identifiers.

Every route here is `AuthLevel.ADMIN`. The business rules — the last-admin
guards chief among them — live in app/auth.py; this module is just the HTTP
surface over them, following the pattern chat C's minimal invite-mint endpoint
(POST /api/admin/invites) already set for this URL prefix.

Two of the destructive actions below are deliberately distinct and are NOT
interchangeable — see admin_wipe_user and admin_delete_user.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from . import assets, auth, authz
from .auth import AuthLevel

router = APIRouter()
guard = authz.Guard(router)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _error(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


async def _json_body(request: Request) -> dict:
    """Require a JSON body, rejecting anything else with 415.

    Every mutating route in this app enforces this the same way, so a
    form-encoded cross-origin POST is never reachable with only SameSite
    standing in the way.
    """
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        raise HTTPException(status_code=415, detail="Send application/json.")
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed JSON body.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return data


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

@guard.get("/admin", AuthLevel.ADMIN)
async def admin_page(request: Request):
    me = await auth.current_user(request)
    users = await auth.list_users_overview()
    invites = await auth.list_invites()
    retired = await auth.list_retired_identifiers()
    return templates.TemplateResponse(request, "admin.html", {
        "request": request,
        "me": me,
        "users": users,
        "invites": invites,
        "retired": retired,
        "asset_v": assets.ASSET_VERSION,
    })


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

@guard.get("/api/admin/users", AuthLevel.ADMIN)
async def admin_list_users():
    return JSONResponse({"ok": True, "users": await auth.list_users_overview()})


@guard.post("/api/admin/users/{user_id}/approval", AuthLevel.ADMIN)
async def admin_set_approval(user_id: int, request: Request):
    """Toggle calendar and/or distrakt approval, independently — a request may
    include either key, both, or neither."""
    if await auth.get_user(user_id) is None:
        return _error("No such account.", 404)
    data = await _json_body(request)
    if "calendar" in data:
        await auth.set_calendar_approved(user_id, bool(data["calendar"]))
    if "distrakt" in data:
        await auth.set_distrakt_approved(user_id, bool(data["distrakt"]))
    return JSONResponse({"ok": True})


@guard.post("/api/admin/users/{user_id}/admin", AuthLevel.ADMIN)
async def admin_set_admin(user_id: int, request: Request):
    data = await _json_body(request)
    try:
        await auth.set_admin(user_id, bool(data.get("is_admin")))
    except auth.UserNotFound:
        return _error("No such account.", 404)
    except auth.LastAdmin:
        return _error("The last remaining administrator can't be demoted.", 409)
    return JSONResponse({"ok": True})


@guard.post("/api/admin/users/{user_id}/disabled", AuthLevel.ADMIN)
async def admin_set_disabled(user_id: int, request: Request):
    data = await _json_body(request)
    try:
        await auth.set_disabled(user_id, bool(data.get("disabled")))
    except auth.UserNotFound:
        return _error("No such account.", 404)
    except auth.LastAdmin:
        return _error("The last remaining administrator can't be disabled.", 409)
    return JSONResponse({"ok": True})


@guard.post("/api/admin/users/{user_id}/password", AuthLevel.ADMIN)
async def admin_reset_password(user_id: int, request: Request):
    """Set a temporary password directly, no email flow.

    Also the only path to give an OAuth-only account (no username, no
    password) a username, since a password has nothing to attach a
    username-based login to without one — pass `username` alongside
    `password` to set both in one request. Resetting the password revokes
    every session that account holds (auth.set_password() does this itself),
    so the temporary password is the only thing that still gets a stolen
    session anywhere.
    """
    target = await auth.get_user(user_id)
    if target is None:
        return _error("No such account.", 404)
    data = await _json_body(request)

    username = str(data.get("username") or "").strip()
    if username and not target["username"]:
        if err := await auth.username_availability_error(username):
            return _error(err)
        await auth.admin_set_username(user_id, username)
    elif username and target["username"]:
        return _error("That account already has a username.")

    password = str(data.get("password") or "")
    generated = False
    if not password:
        password = secrets.token_urlsafe(9)
        generated = True
    elif len(password) < auth.MIN_PASSWORD_LENGTH:
        return _error(f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")

    await auth.set_password(user_id, password)
    return JSONResponse({"ok": True, "password": password, "generated": generated})


@guard.post("/api/admin/users/{user_id}/wipe", AuthLevel.ADMIN)
async def admin_wipe_user(user_id: int, request: Request):
    """Reversible: clears the user's calendar/distrakt data and disables the
    account, but keeps the account, its identities, its username/slug, and its
    share links intact. Distinct from DELETE below, which is permanent."""
    await _json_body(request)
    try:
        await auth.wipe_user_data(user_id)
    except auth.UserNotFound:
        return _error("No such account.", 404)
    return JSONResponse({"ok": True})


@guard.post("/api/admin/users/{user_id}/delete", AuthLevel.ADMIN)
async def admin_delete_user(user_id: int, request: Request):
    """Permanent. Requires typing the account's own display name back —
    exactly what the account list shows for it — as `confirm_username`."""
    me = await auth.current_user(request)
    data = await _json_body(request)
    confirm = str(data.get("confirm_username") or "").strip()
    expected = await auth.display_name_for(user_id)
    if expected is None:
        return _error("No such account.", 404)
    if confirm.lower() != expected.lower():
        return _error("Type the account's name exactly to confirm deletion.")
    try:
        await auth.delete_user(user_id, actor_user_id=me.user_id)
    except auth.CannotDeleteSelf:
        return _error("You can't delete your own account.", 409)
    except auth.LastAdmin:
        return _error("The last remaining administrator can't be deleted.", 409)
    except auth.UserNotFound:
        return _error("No such account.", 404)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

@guard.get("/api/admin/users/{user_id}/sessions", AuthLevel.ADMIN)
async def admin_list_sessions(user_id: int):
    rows = await auth.list_sessions(user_id)
    return JSONResponse({"ok": True, "sessions": [
        {
            "id": row["id"], "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"], "expires_at": row["expires_at"],
            "user_agent": row["user_agent"], "ip_address": row["ip_address"],
        }
        for row in rows
    ]})


@guard.post("/api/admin/users/{user_id}/sessions/revoke", AuthLevel.ADMIN)
async def admin_revoke_session(user_id: int, request: Request):
    """Revoke ONE session belonging to this account.

    Scoped to `user_id` rather than deleting whatever id was posted: the caller
    is looking at one account's session list, and an id that isn't on it is a
    stale row or a mistake, not an instruction to log somebody else out.
    """
    data = await _json_body(request)
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return _error("session_id is required.")
    if not await auth.revoke_user_session(user_id, session_id):
        return _error("That session isn't on this account.", 404)
    return JSONResponse({"ok": True})


@guard.post("/api/admin/users/{user_id}/sessions/revoke-all", AuthLevel.ADMIN)
async def admin_revoke_all_sessions(user_id: int, request: Request):
    await _json_body(request)
    count = await auth.revoke_user_sessions(user_id)
    return JSONResponse({"ok": True, "revoked": count})


# ---------------------------------------------------------------------------
# linked identities
# ---------------------------------------------------------------------------

@guard.post("/api/admin/users/{user_id}/identities/unlink", AuthLevel.ADMIN)
async def admin_unlink_identity(user_id: int, request: Request):
    """Unlike the self-service /api/me/identities/unlink, this can remove an
    account's last way of signing in — but only when the caller explicitly
    confirms with `force`. A first request without it gets the same warning
    the self-service endpoint would have raised, so the admin UI can show it
    and ask before resubmitting with force=true."""
    data = await _json_body(request)
    provider = str(data.get("provider") or "").strip().lower()
    if provider not in ("trakt", "plex"):
        return _error("Unknown provider.")
    force = bool(data.get("force"))
    from . import trakt_routes  # deferred: trakt_routes reads app.auth_routes

    # Read before the unlink (the token lives on the row it deletes), spent only
    # after one actually happened — a first call that comes back asking for
    # `force` must not kill the token and leave the identity linked to it.
    token = await trakt_routes.stored_access_token(user_id) if provider == "trakt" else None
    try:
        removed = await auth.unlink_identity(user_id, provider, force=force)
    except auth.LastLoginMethod:
        return JSONResponse({
            "ok": False,
            "orphan_warning": True,
            "error": ("This is that account's only way to sign in. Unlinking it "
                      "will lock the account out until a password or another "
                      "identity is added. Send force=true to unlink anyway."),
        }, status_code=409)
    if not removed:
        return _error("That account isn't linked.", 404)
    return JSONResponse({"ok": True, "warning": await trakt_routes.revoke_token_value(token)})


# ---------------------------------------------------------------------------
# invites
# ---------------------------------------------------------------------------
# Issuing one is chat C's POST /api/admin/invites, kept in app/auth_routes.py.
# The rest of the screen — list, revoke, redemptions — extends the same prefix.

@guard.get("/api/admin/invites", AuthLevel.ADMIN)
async def admin_list_invites():
    rows = await auth.list_invites()
    return JSONResponse({"ok": True, "invites": [
        {
            "id": row["id"], "token": row["token"], "label": row["label"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "expires_at": row["expires_at"], "max_uses": row["max_uses"],
            "used_count": row["used_count"], "revoked": bool(row["revoked"]),
            "grants_calendar_on_accept": bool(row["grants_calendar_on_accept"]),
            "redemption_count": row["redemption_count"],
            "usable": auth.invite_is_usable(row),
        }
        for row in rows
    ]})


@guard.post("/api/admin/invites/{invite_id}/revoke", AuthLevel.ADMIN)
async def admin_revoke_invite(invite_id: int, request: Request):
    await _json_body(request)
    if not await auth.revoke_invite(invite_id):
        return _error("No such invite.", 404)
    return JSONResponse({"ok": True})


@guard.get("/api/admin/invites/{invite_id}/redemptions", AuthLevel.ADMIN)
async def admin_invite_redemptions(invite_id: int):
    rows = await auth.list_invite_redemptions(invite_id)
    return JSONResponse({"ok": True, "redemptions": [
        {
            "user_id": row["user_id"], "username": row["username"],
            "redeemed_at": row["redeemed_at"], "ip_address": row["ip_address"],
        }
        for row in rows
    ]})


# ---------------------------------------------------------------------------
# retired identifiers
# ---------------------------------------------------------------------------

@guard.get("/api/admin/retired", AuthLevel.ADMIN)
async def admin_list_retired():
    rows = await auth.list_retired_identifiers()
    return JSONResponse({"ok": True, "retired": [
        {"kind": row["kind"], "value": row["value"], "retired_at": row["retired_at"]}
        for row in rows
    ]})


@guard.post("/api/admin/retired/release", AuthLevel.ADMIN)
async def admin_release_retired(request: Request):
    data = await _json_body(request)
    kind = str(data.get("kind") or "").strip()
    value = str(data.get("value") or "").strip()
    if kind not in ("username", "slug"):
        return _error("Only usernames and slugs can be released.")
    if not value:
        return _error("value is required.")
    if not await auth.release_retired_identifier(kind, value):
        return _error("That identifier isn't retired.", 404)
    return JSONResponse({"ok": True})
