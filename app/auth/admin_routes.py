"""The admin screen: accounts, invites, and retired identifiers.

Every route here is `AuthLevel.ADMIN`. The business rules — the last-admin
guards chief among them — live in admin.py beside it; this module is just the HTTP
surface over them: a thin route that validates the request and hands off, so the
rule lives in one place no matter which URL reaches it.

Two of the destructive actions below are deliberately distinct and are NOT
interchangeable — see admin_wipe_user and admin_delete_user.

EVERY CONTROL ON THE PAGE POSTS ITSELF AND IS ANSWERED WITH THE WHOLE ADMIN BODY
(templates/_admin_shell.html), drawn from the same context as the page, with the
action's sentence in the section it came from. The whole body rather than a row,
because the sections are not independent: deleting an account retires its
username (the third table), revoking an invite changes the second, and a promote or
a disable changes what the last-admin guard allows on every other row. The page
used to be a script over JSON routes that reloaded after almost every one of them,
which drew the same thing a request later and lost the sentence doing it.

A REFUSAL IS DRAWN, AT 200, because a non-200 is never swapped (static/js/boost.js's
`noSwap`) — so the sentence an administrator needs would otherwise be a press that
did nothing.

THERE ARE NO JSON READS HERE, and that is a deletion rather than an omission. The
accounts, invites, retired identifiers and one account's sessions each had a GET
under /api/admin, and one session could be revoked by id; the page draws the first
three whole and signs an account out everywhere through a form, so nothing called
any of them, and an authenticated route nobody calls is a surface with no caller
exercising its refusals.

THE ASKING IS MARKUP. Every confirmation is templates/_confirm.html, and the two
questions that used to be window.prompt() — a temporary password, and typing an
account's name to delete it — are small forms opened the same way.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import auth, authz, chrome, clock, db, route_params
from ..config import load_settings
from ..templating import templates
from . import revocation
from .levels import AuthLevel

router = APIRouter()
guard = authz.Guard(router)


# ---------------------------------------------------------------------------
# the page, and the body every action answers with
# ---------------------------------------------------------------------------

# The sections a sentence can land in, each drawn at the top of its own table.
SECTIONS = ("users", "invites", "retired")

def spell_time(timestamp: int | None, zone) -> dict | None:
    """A stored timestamp as a table cell says it, or None when it was never set.

    SHORT, because three of these columns sit side by side and a full locale string
    is most of the row's width, and seconds are noise at this granularity. The exact
    instant goes in the cell's tooltip and its `datetime` attribute."""
    moment = clock.moment(timestamp, zone)
    if moment is None:
        return None
    return {
        "short": f"{moment:%b} {moment.day} {moment.year}, {moment:%H:%M}",
        "exact": f"{moment:%Y-%m-%d %H:%M:%S %Z}",
        "iso": moment.isoformat(),
    }


async def _viewer_zone(request: Request):
    """The zone the administrator reads times in: their saved timezone, then the
    instance's — the zone every other page shows them times in. The browser used to
    format these in its own locale, which could disagree with every other page for
    somebody whose device is set differently from their account."""
    me = await auth.current_user(request)
    return clock.first_zone(me.timezone, (await load_settings(open_secrets=False)).timezone)


async def _admin_context(request: Request, *, section: str = "", message: str = "",
                         ok: bool = False, **extra) -> dict:
    """Everything the admin screen draws."""
    me = await auth.current_user(request)
    zone = await _viewer_zone(request)
    return {
        "request": request,
        # is_admin, calendar_available, ranker_available, version, build
        # for the shared header. Reaching this route at all means is_admin, but
        # the header asks for the flag by name rather than assuming it.
        **chrome.page_context(me),
        "me": me,
        "users": await auth.list_users_overview(),
        "invites": await auth.list_invites(),
        "retired": await auth.list_retired_identifiers(),
        "when": lambda timestamp: spell_time(timestamp, zone),
        "invite_base": await _invite_base(request),
        # The services an administrator can unlink, from the one table that
        # also decides what unlinking each does (app/auth/revocation.py) —
        # every linkable one, ways in or not.
        "linkable_providers": revocation.LINKABLE,
        "notice": {"section": section, "message": message, "ok": ok},
        **extra,
    }


async def _invite_base(request: Request) -> str:
    """The origin an invite link is copied with: the configured public base URL when
    there is one — it is what every other link this instance hands out is built
    from — else the address this page was reached at. The page used to read
    `window.location.origin`, which is the second of those."""
    base = ((await load_settings(open_secrets=False)).public_base_url or "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


async def _shell(request: Request, section: str, message: str = "", ok: bool = False,
                 **extra) -> Response:
    """The admin body, redrawn, with `message` in `section`."""
    return templates.TemplateResponse(request, "_admin_shell.html", await _admin_context(
        request, section=section, message=message, ok=ok, **extra))


@guard.get("/admin", AuthLevel.ADMIN)
async def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html", await _admin_context(request))


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

# Which grant each approval switch writes. The keys are what the buttons post.
APPROVALS = {
    "calendar": auth.set_calendar_approved,
    "distrakt": auth.set_distrakt_approved,
    "ranker": auth.set_ranker_approved,
}


@guard.post("/admin/users/{user_id}/approval", AuthLevel.ADMIN)
async def admin_set_approval(user_id: int, request: Request):
    """Grant or revoke one of the three approvals. A press names one grant and the
    state to put it in, rather than toggling, so a page drawn before somebody else's
    change cannot flip it the wrong way."""
    data = await authz.json_body(request)
    if await auth.get_user(user_id) is None:
        return await _shell(request, "users", "No such account.")
    grant = APPROVALS.get(str(data.get("grant") or ""))
    if grant is None:
        return await _shell(request, "users", "There is no such approval.")
    await grant(user_id, route_params.ticked(data.get("value")))
    return await _shell(request, "users")


@guard.post("/admin/users/{user_id}/admin", AuthLevel.ADMIN)
async def admin_set_admin(user_id: int, request: Request):
    data = await authz.json_body(request)
    try:
        await auth.set_admin(user_id, route_params.ticked(data.get("value")))
    except auth.UserNotFound:
        return await _shell(request, "users", "No such account.")
    except auth.LastAdmin:
        return await _shell(request, "users", "The last remaining administrator can't be demoted.")
    return await _shell(request, "users")


@guard.post("/admin/users/{user_id}/disabled", AuthLevel.ADMIN)
async def admin_set_disabled(user_id: int, request: Request):
    data = await authz.json_body(request)
    try:
        await auth.set_disabled(user_id, route_params.ticked(data.get("value")))
    except auth.UserNotFound:
        return await _shell(request, "users", "No such account.")
    except auth.LastAdmin:
        return await _shell(request, "users", "The last remaining administrator can't be disabled.")
    return await _shell(request, "users")


@guard.post("/admin/users/{user_id}/password", AuthLevel.ADMIN)
async def admin_reset_password(user_id: int, request: Request):
    """Set a temporary password directly, no email flow.

    Also the only path to give an OAuth-only account (no username, no
    password) a username, since a password has nothing to attach a
    username-based login to without one — the form offers a username field for
    exactly those accounts. Resetting the password revokes every session that
    account holds (auth.set_password() does this itself), so the temporary
    password is the only thing that still gets a stolen session anywhere.

    THE PASSWORD IS DRAWN ONCE, in this answer. It is the one secret this page ever
    shows, and it is shown because handing it to the account's owner is the whole
    of what a reset is for — as the generated encryption key is shown once by its
    own answer. It is never stored in the clear and no later answer can draw it.
    """
    target = await auth.get_user(user_id)
    if target is None:
        return await _shell(request, "users", "No such account.")
    data = await authz.json_body(request)

    username = str(data.get("username") or "").strip()
    if username and target["username"]:
        return await _shell(request, "users", "That account already has a username.")
    password = str(data.get("password") or "")
    if password and len(password) < auth.MIN_PASSWORD_LENGTH:
        return await _shell(request, "users",
                            f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
    # Checked before anything is written, so a refused username does not leave a
    # password changed behind it.
    if username and (err := await auth.username_availability_error(username)):
        return await _shell(request, "users", err)
    if username:
        await auth.admin_set_username(user_id, username)
    if not password:
        password = secrets.token_urlsafe(9)
    await auth.set_password(user_id, password)
    response = await _shell(request, "users", ok=True, temporary_password={
        "user_id": user_id, "password": password,
    }, message="Every session on that account has been signed out.")
    response.headers["Cache-Control"] = "no-store"
    return response


@guard.post("/admin/users/{user_id}/wipe", AuthLevel.ADMIN)
async def admin_wipe_user(user_id: int, request: Request):
    """Reversible: clears the user's calendar/distrakt data and disables the
    account, but keeps the account, its identities, its username/slug, and its
    share links intact. Distinct from delete below, which is permanent."""
    await authz.json_body(request)
    try:
        await auth.wipe_user_data(user_id)
    except auth.UserNotFound:
        return await _shell(request, "users", "No such account.")
    return await _shell(request, "users", "Wiped. The account is disabled until re-enabled.",
                        ok=True)


@guard.post("/admin/users/{user_id}/delete", AuthLevel.ADMIN)
async def admin_delete_user(user_id: int, request: Request):
    """Permanent. Requires typing the account's own display name back —
    exactly what the account list shows for it — as `confirm_username`."""
    me = await auth.current_user(request)
    data = await authz.json_body(request)
    confirm = str(data.get("confirm_username") or "").strip()
    expected = await auth.display_name_for(user_id)
    if expected is None:
        return await _shell(request, "users", "No such account.")
    if confirm.lower() != expected.lower():
        return await _shell(request, "users", "Type the account's name exactly to confirm deletion.")
    try:
        await auth.delete_user(user_id, actor_user_id=me.user_id)
    except auth.CannotDeleteSelf:
        return await _shell(request, "users", "You can't delete your own account.")
    except auth.LastAdmin:
        return await _shell(request, "users", "The last remaining administrator can't be deleted.")
    except auth.UserNotFound:
        return await _shell(request, "users", "No such account.")
    return await _shell(request, "users", f"Deleted {expected}.", ok=True)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

@guard.post("/admin/users/{user_id}/sessions/revoke-all", AuthLevel.ADMIN)
async def admin_revoke_all_sessions(user_id: int, request: Request):
    await authz.json_body(request)
    count = await auth.revoke_user_sessions(user_id)
    return await _shell(request, "users", f"Signed out {count} session(s).", ok=True)


# ---------------------------------------------------------------------------
# linked identities
# ---------------------------------------------------------------------------

@guard.post("/admin/users/{user_id}/identities/unlink", AuthLevel.ADMIN)
async def admin_unlink_identity(user_id: int, request: Request):
    """Unlike the account page's own unlink, this can remove an account's last way
    of signing in — but only when the caller explicitly confirms with `force`. A
    first request without it is answered with the same warning the account page
    would have raised, drawn with a second confirmation that sends `force`."""
    data = await authz.json_body(request)
    provider = str(data.get("provider") or "").strip().lower()
    if not revocation.is_linkable(provider):
        return await _shell(request, "users", "Unknown provider.")
    force = route_params.ticked(data.get("force"))

    # Read before the unlink (the token lives on the row it deletes), spent only
    # after one actually happened — a first call that comes back asking for
    # `force` must not kill the token and leave the identity linked to it.
    # EVERY LINKABLE SERVICE, from app/auth/revocation.py's one table — the ones
    # nobody signs in with as well: this route used to accept two of the three
    # sign-in services, so an administrator could not remove somebody's Simkl
    # link at all.
    token = await revocation.stored_token(provider, user_id)
    try:
        removed = await auth.unlink_identity(user_id, provider, force=force)
    except auth.LastLoginMethod:
        return await _shell(request, "users", (
            "This is that account's only way to sign in. Unlinking it will lock the "
            "account out until a password or another identity is added."),
            orphan={"user_id": user_id, "provider": provider})
    if not removed:
        return await _shell(request, "users", "That account isn't linked.")
    # Unlinked locally regardless; a warning is about the authorization still
    # standing on the provider's side, which only its owner can clear.
    warning = await revocation.revoke(provider, token)
    return await _shell(request, "users", warning or f"Unlinked {provider.title()}.", ok=not warning)


# ---------------------------------------------------------------------------
# invites
# ---------------------------------------------------------------------------

@guard.post("/admin/invites", AuthLevel.ADMIN)
async def admin_create_invite(request: Request):
    """Issue an invite from the page's form: a label, an optional use cap and an
    optional lifetime in hours — each blank for "none" — and the two grants.

    The grants are boxes with a hidden "0" before each, so an unticked box is sent
    rather than left out, and read by route_params.ticked — the one statement of
    which spellings mean "on". A number that is not one is refused in a sentence
    beside the form."""
    admin = await auth.current_user(request)
    data = await authz.json_body(request)
    label = str(data.get("label") or "").strip() or None

    def number(field: str, parse, what: str):
        raw = str(data.get(field) if data.get(field) is not None else "").strip()
        if not raw:
            return None
        try:
            value = parse(raw)
        except ValueError:
            raise _InviteRefused(f"{what} must be a number.") from None
        if value <= 0:
            raise _InviteRefused(f"{what} must be more than nought.")
        return value

    try:
        max_uses = number("max_uses", int, "Max uses")
        hours = number("expires_in_hours", float, "Expires (hours)")
    except _InviteRefused as exc:
        return await _shell(request, "invites", str(exc))

    await auth.create_invite(
        created_by=admin.user_id, label=label, max_uses=max_uses,
        expires_at=db.now() + int(hours * 3600) if hours is not None else None,
        grants_calendar_on_accept=route_params.ticked(data.get("grants_calendar_on_accept")),
        grants_ranker_on_accept=route_params.ticked(data.get("grants_ranker_on_accept")),
    )
    return await _shell(request, "invites", "Invite issued. Copy its link below.", ok=True)


class _InviteRefused(ValueError):
    """A field of the invite form that cannot be used, with the sentence to say."""


@guard.post("/admin/invites/{invite_id}/revoke", AuthLevel.ADMIN)
async def admin_revoke_invite(invite_id: int, request: Request):
    await authz.json_body(request)
    if not await auth.revoke_invite(invite_id):
        return await _shell(request, "invites", "No such invite.")
    return await _shell(request, "invites", "Invite revoked.", ok=True)


@guard.get("/admin/fragments/invites/{invite_id}/redemptions", AuthLevel.ADMIN)
async def admin_invite_redemptions(invite_id: int, request: Request):
    """Who has redeemed an invite, asked for the first time its row is opened. It
    was an alert() listing the same lines."""
    zone = await _viewer_zone(request)
    return templates.TemplateResponse(request, "_admin_redemptions.html", {
        "request": request,
        "redemptions": await auth.list_invite_redemptions(invite_id),
        "when": lambda timestamp: spell_time(timestamp, zone),
    })


# ---------------------------------------------------------------------------
# retired identifiers
# ---------------------------------------------------------------------------

@guard.post("/admin/retired/release", AuthLevel.ADMIN)
async def admin_release_retired(request: Request):
    data = await authz.json_body(request)
    kind = str(data.get("kind") or "").strip()
    value = str(data.get("value") or "").strip()
    if kind not in ("username", "slug"):
        return await _shell(request, "retired", "Only usernames and slugs can be released.")
    if not value:
        return await _shell(request, "retired", "Name the identifier to release.")
    if not await auth.release_retired_identifier(kind, value):
        return await _shell(request, "retired", "That identifier isn't retired.")
    return await _shell(request, "retired", f"Released {value}.", ok=True)
