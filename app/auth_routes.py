"""First-run setup, registration, sign-in, and the account page.

Three things here carry real weight and must survive any future restyling:

  - The first-run race guard. Creating the first account re-checks the user
    count with the write lock held, and the database carries a partial unique
    index on the bootstrap flag as a backstop, so two simultaneous setup
    requests cannot both create an administrator.

  - The upgrade path for an instance that already has data. Before accounts
    existed the app had no login at all, so an existing instance starts with
    an empty `users` table and setup is where its operator first gets
    credentials. Setup therefore also adopts the Trakt token already in
    settings.json, seeds the new account's view preferences and timezone from
    settings.json so the calendar looks exactly as it did, and calls the hook
    that imports the legacy per-month state files.

  - JSON-only request bodies. A form-encoded POST is a CORS "simple request"
    that browsers send with no preflight, so accepting one would leave these
    endpoints defended by SameSite alone. Everything here posts JSON via
    fetch; there is no HTML form POST anywhere in this app and none may be
    added.

Registration is gated by an invite unless the operator has turned that off,
and every sign-in / registration failure is throttled per username and per IP
independently, using the same generic response and timing class as an
ordinary failure — see the login and register handlers for why.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, authz, calendar_state, db, trakt_auth
from .auth import AuthLevel
from .config import load_settings

logger = logging.getLogger(__name__)

router = APIRouter()
# Every route here declares its access level through the same registrar the rest
# of the app uses, so the startup audit and the deny-by-default middleware see
# them exactly as they see the routes defined on the app itself.
guard = authz.Guard(router)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

# One message for every sign-in failure — unknown username, wrong password,
# locked out, and disabled account alike — so none of them can be used to tell
# whether an account exists or is merely rate-limited.
INVALID_CREDENTIALS = "Invalid username or password."

# One message for every unusable invite — missing, malformed, expired, revoked,
# exhausted, or never existed. An invite is not the accepted enumeration
# exception (a taken username is, deliberately, per the registration handler).
INVALID_INVITE = "This invite link is not valid. Ask your admin for a new one."

# app_meta key, set when setup could not adopt the Trakt token already in
# settings.json, so the Settings screen can prompt for a reconnect.
TRAKT_RECONNECT_NOTICE = "trakt_reconnect_notice"


async def _json_body(request: Request) -> dict:
    """Require a JSON body, rejecting anything else with 415.

    A form-encoded cross-origin POST needs no CORS preflight, so accepting one
    would make every mutating endpoint reachable from a hostile page with only
    the cookie's SameSite attribute standing in the way.
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


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


# ---------------------------------------------------------------------------
# first-run setup
# ---------------------------------------------------------------------------

@guard.get("/onboarding", AuthLevel.PUBLIC)
async def onboarding_page(request: Request):
    if await auth.any_users_exist():
        return RedirectResponse("/login", status_code=303)
    settings = load_settings()
    return templates.TemplateResponse(request, "auth_onboarding.html", {
        "request": request,
        "has_trakt_token": bool(settings.trakt_access_token.strip()),
    })


@guard.post("/onboarding", AuthLevel.PUBLIC)
async def onboarding_create(request: Request):
    """Create the first administrator account.

    A username and password are REQUIRED, and the first account may not be
    provider-only. If the only way into an instance were "Log in with Trakt",
    then a Trakt outage, a revoked app registration, or a mistyped redirect URI
    would lock the operator out of their own instance with no way back in.
    """
    data = await _json_body(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    confirm = str(data.get("password_confirm") or "")

    if err := auth.identifier_error(username):
        return _error(err)
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return _error(f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
    if password != confirm:
        return _error("The two passwords don't match.")
    if await auth.any_users_exist():
        return _error("This instance has already been set up.", 409)

    settings = load_settings()
    # Both of these happen before the transaction opens: neither 200ms of Argon2
    # nor a call out to Trakt may sit inside a held write lock.
    password_hash = await auth.hash_password(password)
    trakt_identity = await _fetch_trakt_identity(settings)

    def _create(conn) -> int:
        # Re-checked here rather than only above, because up there the check and
        # the insert were two separate statements with a window between them.
        # BEGIN IMMEDIATE closes that window; the unique index closes it again.
        if auth.user_count(conn) != 0:
            raise _AlreadySetUp()
        user_id = auth.insert_user(
            conn, username=username.lower(), password_hash=password_hash,
            is_admin=True, is_bootstrap=True, calendar_approved=True,
            distrakt_approved=True, timezone=settings.timezone or None,
        )
        # Seeded from settings.json so an upgraded instance's calendar renders
        # exactly as it did before there were accounts.
        auth.insert_user_prefs(conn, user_id, settings)
        if trakt_identity:
            auth.insert_linked_identity(
                conn, user_id=user_id, provider="trakt",
                provider_user_id=trakt_identity["id"],
                display_name=trakt_identity.get("name"),
                access_token=settings.trakt_access_token,
                refresh_token=settings.trakt_refresh_token or None,
                token_expires_at=settings.trakt_token_expires_at or None,
            )
        return user_id

    try:
        user_id = await db.transaction(_create)
    except _AlreadySetUp:
        return _error("This instance has already been set up.", 409)
    except db.IntegrityError:
        # The unique index on the bootstrap flag caught a simultaneous request
        # that got past the count check. Same answer either way.
        return _error("This instance has already been set up.", 409)

    if settings.trakt_access_token.strip() and not trakt_identity:
        await db.set_meta(TRAKT_RECONNECT_NOTICE, "1")
    elif trakt_identity:
        await db.set_meta(TRAKT_RECONNECT_NOTICE, "")

    await _import_legacy_calendar_state(user_id)
    await auth.mark_logged_in(user_id)
    session_id = await auth.create_session(
        user_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=auth.client_ip(request, settings),
    )
    response = JSONResponse({
        "ok": True,
        "redirect": "/",
        "trakt_adopted": bool(trakt_identity),
    })
    auth.set_session_cookie(response, session_id, settings, request)
    return response


class _AlreadySetUp(Exception):
    """Raised inside the setup transaction to roll it back."""


class _UsernameTaken(Exception):
    """Raised inside the registration transaction to roll it back."""


class _InvalidInvite(Exception):
    """Raised inside the registration transaction to roll it back."""


async def _fetch_trakt_identity(settings) -> dict | None:
    """Look up the numeric Trakt account id for the token already in
    settings.json, via `GET /users/me`.

    settings.json holds a token but no account id, and an identity row must be
    keyed on the immutable numeric id rather than a username or slug. Returns
    None on any failure at all — an expired token, a revoked app registration, no
    network, or a response with no numeric account id in it. Setup then creates
    the account without the link and leaves a notice to reconnect, because
    blocking first-run setup on a third-party call would make an instance
    unusable for reasons entirely outside its control, and writing a row with a
    guessed id would be worse than writing none.
    """
    token = (settings.trakt_access_token or "").strip()
    client_id = (settings.trakt_client_id or "").strip()
    if not (token and client_id):
        return None
    try:
        return await trakt_auth.fetch_account(client_id, token)
    except trakt_auth.AccountLookupError as exc:
        logger.warning("%s Creating the account without a linked Trakt identity.", exc)
        return None


async def _import_legacy_calendar_state(user_id: int) -> None:
    """Import the legacy `data/state_*.json` files onto the account just created.

    Those files hold the pre-accounts "not watching" decisions and per-month
    change-detection state, keyed by endpoint/year/month with no user; the
    importer backs them up, then copies them into the per-user calendar tables
    under this account. A failure is logged rather than raised — onboarding must
    not be blocked by a local file hiccup, and the originals are left in place so
    a retry can still succeed.
    """
    try:
        await calendar_state.import_legacy_state(user_id)
    except Exception:
        logger.warning("Legacy calendar state import failed for user %s", user_id, exc_info=True)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

@guard.get("/register", AuthLevel.PUBLIC)
async def register_page(request: Request):
    """The registration form, or the "invalid invite" page when one is required
    and missing/unusable. Reachable only once an instance has been set up — an
    invite implies an admin already exists to have issued it, so this route is
    not part of the first-run path the way /login and /onboarding are."""
    settings = load_settings()
    token = (request.query_params.get("invite") or "").strip()
    if not settings.allow_open_registration:
        invite = await auth.find_invite_by_token(token) if token else None
        if not auth.invite_is_usable(invite):
            return templates.TemplateResponse(request, "auth_invite_invalid.html", {
                "request": request,
            })
    return templates.TemplateResponse(request, "auth_register.html", {
        "request": request,
        "invite_token": token,
        # The invite rides along to the provider flow in the handshake row, not
        # in a cookie or the redirect URL, so registering that way is gated
        # exactly as tightly as registering with a password.
        "trakt_login_configured": settings.trakt_login_configured,
    })


async def _registration_rate_limited(ip: str, token: str) -> bool:
    if await auth.rate_limited("register_ip", ip, max_attempts=auth.REGISTER_MAX_ATTEMPTS,
                               window_seconds=auth.REGISTER_WINDOW_SECONDS):
        return True
    if token and await auth.rate_limited("invite_ip", ip, max_attempts=auth.INVITE_MAX_ATTEMPTS,
                                         window_seconds=auth.INVITE_WINDOW_SECONDS):
        return True
    return False


@guard.post("/register", AuthLevel.PUBLIC)
async def register(request: Request):
    """Create an account, gated by an invite unless the operator turned that
    off (settings.allow_open_registration).

    Every rejection before the account exists — a bad invite, a taken
    username, a validation failure — is recorded as a failed attempt against
    this IP, the same as a login failure, so a script trying tokens or
    usernames in a loop eventually gets throttled. A taken username is still
    revealed in the response (§4.4's one accepted enumeration exception,
    unrelated to invites); an unusable invite never is — every cause looks
    identical from here.
    """
    settings = load_settings()
    ip = auth.client_ip(request, settings)
    data = await _json_body(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    confirm = str(data.get("password_confirm") or "")
    token = str(data.get("invite") or request.query_params.get("invite") or "").strip()
    invite_required = not settings.allow_open_registration

    if await _registration_rate_limited(ip, token):
        return _error("Too many attempts from this address. Try again later.", 429)

    async def _fail(message: str, status: int = 400) -> JSONResponse:
        await auth.record_attempt("register_ip", ip, False)
        if token:
            await auth.record_attempt("invite_ip", ip, False)
        return _error(message, status)

    invite = await auth.find_invite_by_token(token) if token else None
    if invite_required and not auth.invite_is_usable(invite):
        return await _fail(INVALID_INVITE)
    if err := await auth.username_availability_error(username):
        return await _fail(err)
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return await _fail(f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
    if password != confirm:
        return await _fail("The two passwords don't match.")

    # Off-thread hashing must not sit inside the transaction's held write lock.
    password_hash = await auth.hash_password(password)
    username_lower = username.lower()

    def _create(conn):
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username_lower,)).fetchone():
            raise _UsernameTaken()
        row = None
        if token:
            # Re-read inside the transaction: the pre-check above ran before the
            # write lock was held, and a concurrent redemption could have
            # exhausted the invite's quota in between.
            candidate = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
            usable = auth.invite_is_usable(candidate)
            if invite_required and not usable:
                raise _InvalidInvite()
            # Under open registration a stray/expired token doesn't block
            # registration — it just doesn't grant anything either.
            row = candidate if usable else None
        grants_calendar = bool(row["grants_calendar_on_accept"]) if row else False
        user_id = auth.insert_user(
            conn, username=username_lower, password_hash=password_hash,
            calendar_approved=grants_calendar, distrakt_approved=False,
            timezone=settings.timezone or None,
        )
        auth.insert_user_prefs(conn, user_id, settings)
        if row is not None:
            auth.redeem_invite(conn, invite=row, user_id=user_id, ip_address=ip)
        return user_id, grants_calendar

    try:
        user_id, calendar_approved = await db.transaction(_create)
    except _UsernameTaken:
        return await _fail("That username is taken.")
    except _InvalidInvite:
        return await _fail(INVALID_INVITE)
    except db.IntegrityError:
        return await _fail("That username is taken.")

    await auth.record_attempt("register_ip", ip, True)
    if token:
        await auth.record_attempt("invite_ip", ip, True)
    await auth.mark_logged_in(user_id)
    session_id = await auth.create_session(
        user_id, user_agent=request.headers.get("user-agent"), ip_address=ip,
    )
    response = JSONResponse({"ok": True, "redirect": "/" if calendar_approved else "/me"})
    auth.set_session_cookie(response, session_id, settings, request)
    return response


# ---------------------------------------------------------------------------
# sign in / sign out
# ---------------------------------------------------------------------------

@guard.get("/login", AuthLevel.PUBLIC)
async def login_page(request: Request):
    if not await auth.any_users_exist():
        return RedirectResponse("/onboarding", status_code=303)
    return templates.TemplateResponse(request, "auth_login.html", {
        "request": request,
        "trakt_login_configured": load_settings().trakt_login_configured,
    })


@guard.post("/login", AuthLevel.PUBLIC)
async def login(request: Request):
    """Username and password sign-in.

    Every failure — unknown username, wrong password, disabled account, and
    locked out alike — returns the same message, the same status, and the same
    timing class, so none of them can be used to learn whether an account
    exists or is merely rate-limited. A locked-out attempt still spends a full
    dummy verify for exactly that reason: a lockout that resolved faster than a
    real check would itself be the oracle this is built to close.

    Failed attempts are counted per username AND per IP independently (§1.18),
    so one attacker can't lock out every account by spraying one IP, and one
    victim IP can't be used to spray many usernames without tripping its own
    limit first.
    """
    data = await _json_body(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    settings = load_settings()
    ip = auth.client_ip(request, settings)
    username_key = username.lower()

    locked = False
    if username_key:
        locked = await auth.is_locked_out(
            "username", username_key,
            max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS,
        )
    if not locked:
        locked = await auth.is_locked_out(
            "ip", ip, max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS,
        )

    user = await auth.find_user_by_username(username) if (username and not locked) else None
    if locked or user is None:
        await auth.burn_dummy_verify(password)
        if username_key:
            await auth.record_attempt("username", username_key, False)
        await auth.record_attempt("ip", ip, False)
        return _error(INVALID_CREDENTIALS, 401)

    result = await auth.verify_password(user["password_hash"], password)
    if not result.ok or user["is_disabled"]:
        await auth.record_attempt("username", username_key, False)
        await auth.record_attempt("ip", ip, False)
        return _error(INVALID_CREDENTIALS, 401)
    if result.new_hash:
        # The hashing library's defaults have moved on since this hash was
        # written; upgrade it now that we have the plaintext to do it with.
        await auth.update_password_hash(int(user["id"]), result.new_hash)

    await auth.clear_attempts("username", username_key)
    await auth.mark_logged_in(int(user["id"]))
    session_id = await auth.create_session(
        int(user["id"]), user_agent=request.headers.get("user-agent"), ip_address=ip,
    )
    response = JSONResponse({
        "ok": True,
        "redirect": "/" if user["calendar_approved"] else "/me",
    })
    auth.set_session_cookie(response, session_id, settings, request)
    return response


@guard.post("/logout", AuthLevel.PUBLIC)
async def logout(request: Request):
    # Signing someone out is a state change, so it is held to the same JSON-only
    # rule as every other mutating endpoint. The body itself is ignored.
    await _json_body(request)
    settings = load_settings()
    session_id = auth.read_session_cookie(request, settings)
    if session_id:
        await auth.revoke_session(session_id)
    response = JSONResponse({"ok": True, "redirect": "/login"})
    auth.clear_session_cookie(response, settings, request)
    return response


@guard.get("/me", AuthLevel.SESSION)
async def me_page(request: Request):
    """The account page: approval state, timezone, linked identities, and sign
    out. Doubles as the awaiting-approval page — a user without
    calendar_approved lands here from /login and stays here whenever they try
    to reach a gated route."""
    user = await auth.require_session(request)
    settings = load_settings()
    identities = await auth.list_identities(user.user_id)
    linked = {row["provider"]: row["display_name"] for row in identities}
    account = await auth.get_user(user.user_id)
    return templates.TemplateResponse(request, "auth_me.html", {
        "request": request,
        "user": user,
        "linked": linked,
        "trakt_login_configured": settings.trakt_login_configured,
        # Whether unlinking is offered at all. Without a password an account's
        # linked identities are its only way in, so the last one may not be
        # removed — showing a button that always refuses would be worse than
        # showing none.
        "can_unlink": bool(account and account["password_hash"]) or len(identities) > 1,
        # Drives whether the password form asks for a current password, and
        # whether the "a password is your way back in" nudge is shown.
        "has_password": bool(account and account["password_hash"]),
        "min_password_length": auth.MIN_PASSWORD_LENGTH,
    })


@guard.post("/api/me/identities/unlink", AuthLevel.SESSION)
async def unlink_identity(request: Request):
    """Detach a linked provider account from the signed-in account.

    Refused when it would leave the account with no way to sign in at all —
    there is no self-service recovery from that, and an administrator has to
    undo it by hand.
    """
    user = await auth.require_session(request)
    data = await _json_body(request)
    provider = str(data.get("provider") or "").strip().lower()
    if provider not in ("trakt", "plex"):
        return _error("Unknown provider.")
    # Imported here rather than at module scope: app.trakt_routes reads this
    # module's message constants, so the dependency only runs one way at import
    # time.
    from . import trakt_routes

    # Read before the unlink (the token lives on the row it deletes), spent only
    # after one actually happened — an unlink that gets refused below must not
    # leave the account linked to a token this app just killed.
    token = await trakt_routes.stored_access_token(user.user_id) if provider == "trakt" else None
    try:
        removed = await auth.unlink_identity(user.user_id, provider)
    except auth.LastLoginMethod:
        return _error(
            "That's the only way you can sign in. Link another account first, or "
            "ask an administrator.", 409,
        )
    if not removed:
        return _error("That account isn't linked.", 404)
    warning = await trakt_routes.revoke_token_value(token)
    return JSONResponse({"ok": True, "redirect": "/me", "warning": warning})


@guard.post("/api/me/username", AuthLevel.SESSION)
async def set_own_username(request: Request):
    """Claim a username for an account that has none.

    An account created through Plex or Trakt has no username and no password, so
    without this it depends on an administrator for both. Claiming is one-way:
    CHANGING an existing username is deliberately not offered here, because a
    username is a public identifier — it is what /u/<name> share links are built
    from — and handing it over silently would break every link already shared and
    free the old name for somebody else to claim. That path stays with an
    administrator, who has the retired-identifier machinery to do it safely.
    """
    user = await auth.require_session(request)
    account = await auth.get_user(user.user_id)
    if account and account["username"]:
        return _error("You already have a username. An administrator can change it.", 409)
    data = await _json_body(request)
    username = str(data.get("username") or "").strip().lower()
    if err := await auth.username_availability_error(username):
        return _error(err)
    try:
        await auth.admin_set_username(user.user_id, username)
    except db.IntegrityError:
        # Lost a race between the availability check and this write; the UNIQUE
        # column is the real arbiter, exactly as registration treats it.
        return _error("That username is taken.")
    return JSONResponse({"ok": True, "username": username})


@guard.post("/api/me/password", AuthLevel.SESSION)
async def set_own_password(request: Request):
    """Set or change the signed-in account's password.

    Changing an existing password requires the current one. Setting a FIRST
    password does not, because there is nothing to prove — the live session is
    the only credential such an account has, and requiring one would make the
    feature unreachable for exactly the accounts that need it.

    Setting a password revokes every session the account holds, so a
    password-change after a compromise actually evicts the other party. The
    caller is then re-issued a fresh session, since signing the person out of the
    tab they just used would read as a failure.
    """
    user = await auth.require_session(request)
    settings = load_settings()
    ip = auth.client_ip(request, settings)
    # The current-password check is a guess-checking oracle, so it is throttled
    # on the same counter and thresholds a sign-in attempt is.
    if await auth.is_locked_out("ip", ip, max_attempts=auth.LOGIN_MAX_ATTEMPTS,
                                window_seconds=auth.LOGIN_WINDOW_SECONDS):
        return _error("Too many attempts. Try again later.", 429)

    data = await _json_body(request)
    account = await auth.get_user(user.user_id)
    stored_hash = account["password_hash"] if account else None
    if stored_hash:
        result = await auth.verify_password(stored_hash, str(data.get("current_password") or ""))
        if not result.ok:
            await auth.record_attempt("ip", ip, False)
            return _error("That isn't your current password.", 403)

    password = str(data.get("password") or "")
    if password != str(data.get("password_confirm") or ""):
        return _error("The two passwords don't match.")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return _error(f"Use at least {auth.MIN_PASSWORD_LENGTH} characters.")

    await auth.clear_attempts("ip", ip)
    await auth.set_password(user.user_id, password)
    session_id = await auth.create_session(
        user.user_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=ip,
    )
    response = JSONResponse({"ok": True})
    auth.set_session_cookie(response, session_id, settings, request)
    return response


# ---------------------------------------------------------------------------
# admin: issue an invite
# ---------------------------------------------------------------------------
# One endpoint — enough to mint a token and exercise registration end to end.
# The full issue/list/revoke admin screen is built separately on top of the
# functions in app.auth (create_invite, list_invites, revoke_invite).

@guard.post("/api/admin/invites", AuthLevel.ADMIN)
async def admin_create_invite(request: Request):
    admin = await auth.current_user(request)
    data = await _json_body(request)
    label = str(data.get("label") or "").strip() or None

    max_uses = data.get("max_uses")
    try:
        max_uses = int(max_uses) if max_uses is not None else None
    except (TypeError, ValueError):
        return _error("max_uses must be a whole number.")
    if max_uses is not None and max_uses < 1:
        return _error("max_uses must be at least 1.")

    expires_at = None
    expires_in_hours = data.get("expires_in_hours")
    if expires_in_hours is not None:
        try:
            hours = float(expires_in_hours)
        except (TypeError, ValueError):
            return _error("expires_in_hours must be a number.")
        if hours <= 0:
            return _error("expires_in_hours must be positive.")
        expires_at = db.now() + int(hours * 3600)

    invite = await auth.create_invite(
        created_by=admin.user_id, label=label, expires_at=expires_at, max_uses=max_uses,
        grants_calendar_on_accept=bool(data.get("grants_calendar_on_accept", True)),
    )
    return JSONResponse({"ok": True, **invite})
