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
asking. That makes the poll endpoint the place all the callback-binding
concerns a one-shot OAuth callback would normally carry land instead: it is
reachable by anyone, repeatedly, for as long as a handshake stays unconsumed,
so every poll re-checks the same handshake-cookie and session binding a
one-shot callback would check once. The handshake cookie is checked BEFORE
the row is even looked up, exactly like Trakt's callback, so a request for a
PIN this browser didn't start costs one cookie comparison and never reaches
plex.tv or the database.

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

from .. import auth, authz
from ..config import load_settings
# `plex` is the flow's client; bound as plex_auth to keep it distinct from the
# Plex-shaped names in this module.
from . import plex as plex_auth
from . import provider_login
from .levels import AuthLevel

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

PROVIDER = "plex"

NOT_CONFIGURED = "Signing in with Plex is not available right now. Try again in a moment."
ALREADY_LINKED = "That Plex account is already linked to another user on this instance."
UPSTREAM_FAILED = "Plex could not complete the sign-in. Please try again in a moment."


TOO_MANY_STARTS = "Too many sign-in attempts from this address. Try again in a few minutes."

KEY_UNHEALTHY = (
    "Stored secrets are encrypted, but the key is currently missing or wrong, "
    "so linking is refused rather than writing a fresh token in the clear. An "
    "administrator needs to restore ENCRYPTION_KEY before linking can continue."
)


async def _begin(request: Request, *, purpose: str, session_id: str | None = None,
                 invite_token: str | None = None) -> JSONResponse:
    """Request a PIN and record a handshake carrying it, before the browser ever
    leaves this app.

    The PIN is requested FIRST specifically so the handshake row can be created
    with `plex_pin_id` already set — nothing has to come back later and update
    it.

    Throttled per address ahead of that, because this is the one unauthenticated
    path that spends an outbound call to plex.tv and writes a row: without a
    limit it is a free way to make this instance hammer a third party.
    """
    if await auth.handshake_start_limited(request):
        return authz.error(TOO_MANY_STARTS, 429)
    client_id = await plex_auth.ensure_client_identifier()
    try:
        pin = await plex_auth.request_pin(client_id)
    except plex_auth.PinError as exc:
        logger.warning("Plex PIN request failed: %s", exc)
        return authz.error(NOT_CONFIGURED, 503)

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
    data = await authz.json_body(request)
    state = str(data.get("state") or "").strip()

    if not auth.handshake_cookie_matches(request, settings, state):
        return authz.error(auth.HANDSHAKE_REJECTED, 400)

    current = await auth.current_user(request)
    try:
        row = await auth.peek_handshake(
            state, provider=PROVIDER, session_id=current.session_id if current else None,
        )
    except auth.HandshakeError:
        return authz.error(auth.HANDSHAKE_REJECTED, 400)

    client_id = await plex_auth.ensure_client_identifier()
    try:
        auth_token = await plex_auth.poll_pin(int(row["plex_pin_id"]), client_id)
    except plex_auth.PinError as exc:
        logger.warning("Plex PIN poll failed: %s", exc)
        return authz.error(UPSTREAM_FAILED, 502)

    if auth_token is None:
        return JSONResponse({"ok": True, "status": "pending"})

    try:
        account = await plex_auth.fetch_account(auth_token, client_id)
    except plex_auth.AccountLookupError as exc:
        logger.warning("Plex account lookup failed: %s", type(exc).__name__)
        return authz.error(UPSTREAM_FAILED, 502)

    try:
        handshake = await auth.consume_handshake(
            state, provider=PROVIDER, session_id=current.session_id if current else None,
        )
    except auth.HandshakeError:
        # Expired, or another poll already finished this one — a real race, not
        # an attack, but there is nothing left to complete here either way.
        return authz.error(auth.HANDSHAKE_REJECTED, 400)

    identity = auth.ProviderIdentity(
        provider=PROVIDER,
        # The immutable numeric account id — never the username or email, both
        # of which Plex lets an account holder change and a later account
        # reuse.
        provider_user_id=str(account["id"]),
        display_name=account.get("name"),
        avatar_url=account.get("avatar"),
        access_token=auth_token,
    )

    if handshake["purpose"] == "link":
        return await _finish_link(request, settings, identity, current)
    return await _finish_login(request, settings, identity, handshake)


def _refusal_json(refusal: provider_login.Refusal) -> JSONResponse:
    """Render a shared refusal as this medium's error body.

    The encryption one carries a machine-readable `reason` on top of the prose,
    because it is the single refusal here the page can DO something about —
    it points an administrator at a fixable instance-level state rather than at
    anything about this account. The others deliberately look alike to a client.
    """
    if refusal.kind == provider_login.KEY_UNHEALTHY:
        return authz.error(refusal.message, refusal.status, reason="key_unhealthy")
    return authz.error(refusal.message, refusal.status)


async def _finish_link(request: Request, settings, identity: auth.ProviderIdentity, current):
    """Render provider_login's link completion as this flow's JSON answer.

    The policy — who may link what, and the refusals — is in
    app/auth/provider_login.py, shared with Trakt. What is left here is the
    medium: a poll is answered by an XHR, not by a navigation.
    """
    outcome = await provider_login.complete_provider_link(
        identity=identity, current=current,
        already_linked=ALREADY_LINKED, key_unhealthy=KEY_UNHEALTHY,
    )
    if isinstance(outcome, provider_login.Refusal):
        return _refusal_json(outcome)
    response = JSONResponse({"ok": True, "redirect": outcome.redirect_target})
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _finish_login(request: Request, settings, identity: auth.ProviderIdentity, handshake):
    """Render provider_login's sign-in completion as this flow's JSON answer.

    The browser navigates itself using `redirect` — there is no 303 to follow,
    because the caller here is a fetch inside a page that is already open.
    """
    outcome = await provider_login.complete_provider_login(
        identity=identity, handshake=handshake, request=request, settings=settings,
        already_linked=ALREADY_LINKED,
    )
    if isinstance(outcome, provider_login.Refusal):
        return _refusal_json(outcome)
    response = JSONResponse({"ok": True, "redirect": outcome.redirect_target})
    provider_login.attach_session(response, outcome, settings, request)
    return response


