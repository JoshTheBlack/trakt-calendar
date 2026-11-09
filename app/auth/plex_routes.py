"""Log in with Plex — the PIN-based flow.

Three entry points, no callback:

  GET  /auth/plex/start   public. Requests a PIN, begins a SIGN-IN (or, with an
                           invite, a registration), and answers with the waiting
                           block: the popup's address and the poll that follows it.
  GET  /auth/plex/link    signed in. Same, but begins a LINK of a Plex account
                           onto the account already in session.
  POST /auth/plex/poll    public. A form in that block posts itself every couple of
                           seconds until the PIN has been approved.

EVERY ANSWER IS MARKUP, templates/_plex_flow.html. A poll that finds the PIN still
waiting answers 204, which htmx swaps nowhere, so the form that asked asks again.
Every answer that ends the flow — a refusal, an upstream failure, running out of
time — is drawn in its place at 200, which removes the form and with it the poll.
Signing in is `204` with `HX-Redirect`, a navigation of the whole window.

WHERE THE FLOW'S STATE LIVES, because a sign-in has no session to key it to: the
handshake row this module already wrote, named by its `state` — 256 random bits
from secrets.token_urlsafe, never reused — and pinned to the browser that started
it by an HttpOnly cookie holding the same value. The waiting block carries `state`
back as a form field, exactly as the script that used to poll carried it in a
variable. That makes it no weaker than it was: the cookie cannot be read by the
page or sent by anyone else's browser, the refusal below is unchanged in every
case, and a link handshake is still bound to the session that asked.

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

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import auth, authz, db
from ..config import load_settings
from ..templating import templates
# `plex` is the flow's client; bound as plex_auth to keep it distinct from the
# Plex-shaped names in this module.
from . import plex as plex_auth
from . import provider_login
from . import routes as auth_routes
from .levels import AuthLevel

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

PROVIDER = "plex"

NOT_CONFIGURED = "Signing in with Plex is not available right now. Try again in a moment."
ALREADY_LINKED = "That Plex account is already linked to another user on this instance."
HOLDS_ANOTHER = ("A different Plex account is already linked to yours. Unlink it on your "
                 "account page first, then connect this one.")
UPSTREAM_FAILED = "Plex could not complete the sign-in. Please try again in a moment."


TOO_MANY_STARTS = "Too many sign-in attempts from this address. Try again in a few minutes."

KEY_UNHEALTHY = (
    "Stored secrets are encrypted, but the key is currently missing or wrong, "
    "so linking is refused rather than writing a fresh token in the clear. An "
    "administrator needs to restore ENCRYPTION_KEY before linking can continue."
)

TIMED_OUT = ("Timed out waiting for Plex. Press the Plex button again to start over.")

# How often the waiting block asks. Long enough not to lean on plex.tv while
# somebody finds the window and signs in; short enough that approving feels like
# it did something.
POLL_INTERVAL_SECONDS = 2

# How long a flow is waited on before the page says so. Counted from the
# handshake row's own `created_at`, so it is the same answer however many polls,
# tabs or reloads there have been — the script this replaced counted from when
# ITS timer started, which a second press restarted. Shorter than the handshake's
# ten-minute life, which is a ceiling on replaying a `state`, not a wait anybody
# should sit through.
WAIT_SECONDS = 3 * 60


def _drawn(request: Request, **context) -> Response:
    """The flow block (templates/_plex_flow.html), with this answer's context."""
    return templates.TemplateResponse(request, "_plex_flow.html", {
        "request": request, "waiting": False, **context})


def _stopped(request: Request, message: str) -> Response:
    """An answer that ends the flow: the reason, drawn where the poll was."""
    return _drawn(request, message=message)


async def _begin(request: Request, *, purpose: str, session_id: str | None = None,
                 invite_token: str | None = None) -> Response:
    """Request a PIN and record a handshake carrying it, before the browser ever
    leaves this app.

    The PIN is requested FIRST specifically so the handshake row can be created
    with `plex_pin_id` already set — nothing has to come back later and update
    it.

    Throttled per address ahead of that, because this is the one unauthenticated
    path that spends an outbound call to plex.tv and writes a row: without a
    limit it is a free way to make this instance hammer a third party.

    `popup=blocked` is the page telling us the browser refused the window. The
    flow starts regardless — the link in the answer is a way to approve the PIN
    that no browser blocks, and the poll does not care which window approved it.
    """
    if await auth.handshake_start_limited(request):
        return _stopped(request, TOO_MANY_STARTS)
    client_id = await plex_auth.ensure_client_identifier()
    try:
        pin = await plex_auth.request_pin(client_id)
    except plex_auth.PinError as exc:
        logger.warning("Plex PIN request failed: %s", exc)
        return _stopped(request, NOT_CONFIGURED)

    state = await auth.create_handshake(
        provider=PROVIDER, purpose=purpose, session_id=session_id,
        invite_token=invite_token or None, plex_pin_id=str(pin["id"]),
    )
    response = _drawn(
        request, waiting=True, state=state, interval=POLL_INTERVAL_SECONDS,
        popup_url=plex_auth.popup_url(client_id, pin["code"]),
        popup_blocked=request.query_params.get("popup") == "blocked",
    )
    # The answer names a handshake, so no cache along the way may keep a copy.
    response.headers["Cache-Control"] = "no-store"
    # Pins the handshake to this browser as well as to this row, so a poll
    # arriving from anywhere else is refused before the row is even read.
    auth.set_handshake_cookie(response, state, await load_settings(), request)
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
    settings = await load_settings()
    data = await authz.json_body(request)
    state = str(data.get("state") or "").strip()

    if not auth.handshake_cookie_matches(request, settings, state):
        return _stopped(request, auth.HANDSHAKE_REJECTED)

    current = await auth.current_user(request)
    try:
        row = await auth.peek_handshake(
            state, provider=PROVIDER, session_id=current.session_id if current else None,
        )
    except auth.HandshakeError:
        return _stopped(request, auth.HANDSHAKE_REJECTED)

    # AFTER the binding checks, so how long somebody else's flow has been waiting
    # is not something a refused poll can learn.
    if db.now() - int(row["created_at"]) > WAIT_SECONDS:
        return _stopped(request, TIMED_OUT)

    client_id = await plex_auth.ensure_client_identifier()
    try:
        auth_token = await plex_auth.poll_pin(int(row["plex_pin_id"]), client_id)
    except plex_auth.PinError as exc:
        logger.warning("Plex PIN poll failed: %s", exc)
        return _stopped(request, UPSTREAM_FAILED)

    if auth_token is None:
        # Nothing to draw: the block asking stays as it is and asks again.
        return Response(status_code=204)

    try:
        account = await plex_auth.fetch_account(auth_token, client_id)
    except plex_auth.AccountLookupError as exc:
        logger.warning("Plex account lookup failed: %s", type(exc).__name__)
        return _stopped(request, UPSTREAM_FAILED)

    try:
        handshake = await auth.consume_handshake(
            state, provider=PROVIDER, session_id=current.session_id if current else None,
        )
    except auth.HandshakeError:
        # Expired, or another poll already finished this one — a real race, not
        # an attack, but there is nothing left to complete here either way.
        return _stopped(request, auth.HANDSHAKE_REJECTED)

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


async def _finish_link(request: Request, settings, identity: auth.ProviderIdentity, current):
    """Render provider_login's link completion as this flow's answer.

    The policy — who may link what, and the refusals — is in
    app/auth/provider_login.py, shared with Trakt. What is left here is the
    medium: a refusal is drawn where the poll was, and a link navigates.
    """
    outcome = await provider_login.complete_provider_link(
        identity=identity, current=current,
        already_linked=ALREADY_LINKED, holds_another=HOLDS_ANOTHER,
        key_unhealthy=KEY_UNHEALTHY,
    )
    if isinstance(outcome, provider_login.Refusal):
        return _stopped(request, outcome.message)
    response = auth_routes.go_to(outcome.redirect_target)
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _finish_login(request: Request, settings, identity: auth.ProviderIdentity, handshake):
    """Render provider_login's sign-in completion as this flow's answer.

    `HX-Redirect` rather than a 303: the caller is a request from a page that is
    already open, and a redirect status would be followed and swapped into it.
    """
    outcome = await provider_login.complete_provider_login(
        identity=identity, handshake=handshake, request=request, settings=settings,
        already_linked=ALREADY_LINKED,
    )
    if isinstance(outcome, provider_login.Refusal):
        return _stopped(request, outcome.message)
    response = auth_routes.go_to(outcome.redirect_target)
    provider_login.attach_session(response, outcome, settings, request)
    return response


