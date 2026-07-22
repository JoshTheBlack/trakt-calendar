"""Log in with Plex — the PIN-based flow.

Three entry points, no callback:

  GET  /auth/plex/start   public. Requests a PIN, begins a SIGN-IN (or, with an
                           invite, a registration), and hands the browser a
                           popup URL plus the `state` to poll with.
  GET  /auth/plex/link    signed in. Same, but begins a LINK of a Plex account
                           onto the account already in session.
  POST /auth/plex/poll    public. A same-origin fetch the page repeats every
                           couple of seconds until the popup has been approved.

Plex has no redirect/callback the way Trakt does — the popup approves the PIN
entirely on plex.tv's own page, and this app only ever learns about it by
asking. That makes the poll endpoint the place all of §4.2's callback-binding
concerns land instead: it is reachable by anyone, repeatedly, for as long as a
handshake stays unconsumed, so every poll re-checks the same handshake-cookie
and session binding a one-shot callback would check once. The handshake
cookie is checked BEFORE the row is even looked up, exactly like Trakt's
callback, so a request for a PIN this browser didn't start costs one cookie
comparison and never reaches plex.tv or the database.

The row itself is only ever CONSUMED once — at the poll that finds the PIN
already approved — via the same auth.consume_handshake() every other poll
before it declined to call. Two polls racing on the moment of approval still
produce exactly one success, because that consumption is a single
conditional UPDATE.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import auth, authz, plex_auth
from .auth import AuthLevel
from .auth_routes import INVALID_CREDENTIALS, INVALID_INVITE
from .config import load_settings

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

PROVIDER = "plex"

NOT_CONFIGURED = "Signing in with Plex is not available right now. Try again in a moment."
ALREADY_LINKED = "That Plex account is already linked to another user on this instance."
UPSTREAM_FAILED = "Plex could not complete the sign-in. Please try again in a moment."


def _error(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed JSON body.")
    return data if isinstance(data, dict) else {}


async def _begin(request: Request, *, purpose: str, session_id: str | None = None,
                 invite_token: str | None = None) -> JSONResponse:
    """Request a PIN and record a handshake carrying it, before the browser ever
    leaves this app.

    The PIN is requested FIRST specifically so the handshake row can be created
    with `plex_pin_id` already set — nothing has to come back later and update
    it.
    """
    client_id = await plex_auth.ensure_client_identifier()
    try:
        pin = await plex_auth.request_pin(client_id)
    except plex_auth.PinError as exc:
        logger.warning("Plex PIN request failed: %s", exc)
        return _error(NOT_CONFIGURED, 503)

    state = await auth.create_handshake(
        provider=PROVIDER, purpose=purpose, session_id=session_id,
        invite_token=invite_token or None, plex_pin_id=str(pin["id"]),
    )
    response = JSONResponse({
        "ok": True,
        "state": state,
        "popup_url": plex_auth.popup_url(client_id, pin["code"]),
    })
    # Pins the handshake to this browser as well as to this row, so a poll
    # arriving from anywhere else is refused before the row is even read.
    auth.set_handshake_cookie(response, state, load_settings(), request)
    return response


@guard.get("/auth/plex/start", AuthLevel.PUBLIC)
async def plex_start(request: Request):
    """Begin a sign-in. An `invite` query parameter travels in the handshake
    row, the same way Trakt's does, so nothing in the browser can substitute a
    different one part-way through."""
    return await _begin(
        request, purpose="login",
        invite_token=(request.query_params.get("invite") or "").strip(),
    )


@guard.get("/auth/plex/link", AuthLevel.SESSION)
async def plex_link(request: Request):
    """Begin linking a Plex account to the account already signed in.

    Bound to this exact session, same as Trakt's link entry point — starting a
    link from a logged-out page is what would let a poll attach an identity to
    whoever happens to be signed in when it resolves.
    """
    user = await auth.require_session(request)
    return await _begin(request, purpose="link", session_id=user.session_id)


@guard.post("/auth/plex/poll", AuthLevel.PUBLIC)
async def plex_poll(request: Request):
    """Check whether the popup has been approved yet.

    Order matters here exactly as it does for Trakt's callback: the handshake
    cookie is checked before the row is read, and the row's own binding
    (provider, expiry, single-use, session for a link) is re-checked on every
    single call via auth.peek_handshake — a poll for a PIN that isn't bound to
    the caller's own handshake is refused before plex.tv is ever asked about
    it. The row is only actually consumed once, at the poll where plex.tv
    reports the PIN approved.
    """
    settings = load_settings()
    data = await _json_body(request)
    state = str(data.get("state") or "").strip()

    if not auth.handshake_cookie_matches(request, settings, state):
        return _error(auth.HANDSHAKE_REJECTED, 400)

    current = await auth.current_user(request)
    try:
        row = await auth.peek_handshake(
            state, provider=PROVIDER, session_id=current.session_id if current else None,
        )
    except auth.HandshakeError:
        return _error(auth.HANDSHAKE_REJECTED, 400)

    client_id = await plex_auth.ensure_client_identifier()
    try:
        auth_token = await plex_auth.poll_pin(int(row["plex_pin_id"]), client_id)
    except plex_auth.PinError as exc:
        logger.warning("Plex PIN poll failed: %s", exc)
        return _error(UPSTREAM_FAILED, 502)

    if auth_token is None:
        return JSONResponse({"ok": True, "status": "pending"})

    try:
        account = await plex_auth.fetch_account(auth_token, client_id)
    except plex_auth.AccountLookupError as exc:
        logger.warning("Plex account lookup failed: %s", type(exc).__name__)
        return _error(UPSTREAM_FAILED, 502)

    try:
        handshake = await auth.consume_handshake(
            state, provider=PROVIDER, session_id=current.session_id if current else None,
        )
    except auth.HandshakeError:
        # Expired, or another poll already finished this one — a real race, not
        # an attack, but there is nothing left to complete here either way.
        return _error(auth.HANDSHAKE_REJECTED, 400)

    identity = auth.ProviderIdentity(
        provider=PROVIDER,
        # The immutable numeric account id — never the username or email, both
        # of which Plex lets an account holder change and a later account
        # reuse.
        provider_user_id=str(account["id"]),
        display_name=account.get("name"),
        access_token=auth_token,
    )

    if handshake["purpose"] == "link":
        return await _finish_link(request, settings, identity, current)
    return await _finish_login(request, settings, identity, handshake)


async def _finish_link(request: Request, settings, identity: auth.ProviderIdentity, current):
    if current is None:  # pragma: no cover — consume_handshake already required it
        return _error(auth.HANDSHAKE_REJECTED, 400)
    try:
        await auth.link_provider_identity(identity=identity, user_id=current.user_id)
    except auth.IdentityInUse:
        return _error(ALREADY_LINKED, 409)
    except auth.AccountUnavailable:
        return _error(auth.HANDSHAKE_REJECTED, 403)
    response = JSONResponse({"ok": True, "redirect": "/me"})
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _finish_login(request: Request, settings, identity: auth.ProviderIdentity, handshake):
    ip = auth.client_ip(request, settings)
    token = handshake["invite_token"]
    # Only a REGISTRATION is throttled, the same distinction Trakt's callback
    # makes — an ordinary sign-in with a known identity costs no more than one
    # with a password, and throttling it would lock out a household sharing one
    # address.
    if await auth.find_identity(PROVIDER, identity.provider_user_id) is None:
        if await _registration_rate_limited(ip, token):
            return _error(
                "Too many sign-up attempts from this address. Try again later.", 429,
            )
    try:
        outcome = await auth.login_with_provider_identity(
            identity=identity, invite_token=token, ip_address=ip, settings=settings,
        )
    except auth.RegistrationRefused:
        await _record_registration_attempt(ip, token, False)
        return _error(INVALID_INVITE, 403)
    except auth.IdentityInUse:  # pragma: no cover — needs a concurrent registration
        return _error(ALREADY_LINKED, 409)
    except auth.AccountUnavailable:
        # A disabled account, reported exactly like a failed password sign-in so
        # a Plex poll is not an oracle for account state.
        return _error(INVALID_CREDENTIALS, 403)

    if outcome.kind == "registered":
        await _record_registration_attempt(ip, token, True)

    session_id = await auth.create_session(
        outcome.user_id, user_agent=request.headers.get("user-agent"), ip_address=ip,
    )
    response = JSONResponse({
        "ok": True,
        "redirect": "/" if outcome.calendar_approved else "/me",
    })
    auth.set_session_cookie(response, session_id, settings, request)
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _registration_rate_limited(ip: str, token: str | None) -> bool:
    if await auth.rate_limited("register_ip", ip, max_attempts=auth.REGISTER_MAX_ATTEMPTS,
                               window_seconds=auth.REGISTER_WINDOW_SECONDS):
        return True
    return bool(token) and await auth.rate_limited(
        "invite_ip", ip, max_attempts=auth.INVITE_MAX_ATTEMPTS,
        window_seconds=auth.INVITE_WINDOW_SECONDS,
    )


async def _record_registration_attempt(ip: str, token: str | None, succeeded: bool) -> None:
    await auth.record_attempt("register_ip", ip, succeeded)
    if token:
        await auth.record_attempt("invite_ip", ip, succeeded)
