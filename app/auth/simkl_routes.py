"""Log in with Simkl — the redirect authorization flow.

Three entry points and one exit, the same shape Trakt's flow has:

  GET /auth/simkl/start     public. Begins a SIGN-IN (or, with an invite, a
                            registration) and sends the browser to Simkl.
  GET /auth/simkl/link      signed in. Begins a LINK of a Simkl account onto the
                            account already in session.
  GET /auth/simkl/callback  public. Where Simkl sends the browser back.

The callback is the sensitive one, and everything it does before touching Simkl
is about proving the request belongs to the visitor who started the flow. It is
a top-level GET navigation, which SameSite=Lax deliberately sends cookies on, so
without that proof an attacker could hand a signed-in victim a callback URL
carrying the ATTACKER's authorization code — and the victim's account would end
up permanently linked to the attacker's Simkl identity. The handshake row and
its cookie, both handled in app.auth, are what make that impossible; this module
refuses with one generic message the moment either fails, and never falls back
to treating an unrecognized callback as an ordinary sign-in.

WHAT IS DECIDED HERE AND WHAT IS NOT. Who may sign in, who may register, and how
each refusal is worded to avoid becoming an oracle all live in
app/auth/provider_login.py, shared with Plex and Trakt. What is left in this
module is the medium — a browser navigation answered with a page or a 303 — plus
the strings that name Simkl, which are the one thing the shared policy cannot say
for itself. The notice tables below are this module's own rather than borrowed
from trakt_routes: a title and a "back" target are page chrome, and making one
provider's route module import another's would tie two independent flows
together for the sake of four strings.

There is no app-wide device flow to fall back on the way Trakt has one, because
Simkl documents no device flow. If the registered redirect URI is wrong, fixing
it on simkl.com is the only remedy.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import auth, authz, db, secrets_box
from ..config import Settings, load_settings
from ..providers.simkl import transport as simkl_transport
from ..templating import templates
# Bound as `simkl_auth` because inside app/auth/ a bare `simkl` would read as the
# Simkl SOURCE (app/providers/simkl/); this is the login flow.
from . import provider_login
from . import simkl as simkl_auth
from .levels import AuthLevel

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# ONE constant, used for the handshake row, the identity row and the unlink.
# Nothing checks it any more — the provider columns are an open set of names by
# design (see app/db.py) — so a second spelling of it somewhere in this module
# would not fail at the database, it would quietly mint an identity nothing else
# can find.
PROVIDER = "simkl"

# Shown when the operator hasn't finished setting the integration up. Distinct
# from every refusal below because it is addressed to the operator, describes a
# state they can fix, and reveals nothing about any account.
NOT_CONFIGURED = (
    "Signing in with Simkl isn't set up on this instance. An administrator needs "
    "to add the Simkl client ID, client secret, and public base URL in Settings."
)

# One message for a Simkl account that belongs to somebody else here. The link
# is refused rather than moved: whoever authorized last must not be able to take
# an identity away from the account holding it.
ALREADY_LINKED = (
    "That Simkl account is already linked to another user on this instance."
)

UPSTREAM_FAILED = (
    "Simkl could not complete the sign-in. Please try again in a moment."
)

TOO_MANY_STARTS = (
    "Too many sign-in attempts from this address. Try again in a few minutes."
)

# Refusing a LINK rather than a sign-in: the visitor already has an account and
# came from its page, so that is where "back" belongs.
KEY_UNHEALTHY = (
    "This instance's stored secrets are encrypted, but the key is currently "
    "missing or wrong, so linking is refused rather than writing a fresh token "
    "in the clear. An administrator needs to restore ENCRYPTION_KEY (see "
    "Settings) before linking can continue."
)

_BACK_TO_ACCOUNT = {"back": "/me", "back_label": "Back to your account"}

# What each shared refusal looks like as a PAGE. The message and the status ride
# on the refusal itself — this is only the chrome around them. Two tables rather
# than one because "back" genuinely differs: a refused link returns the visitor
# to the account page they started from, while a refused sign-in has no account
# page to return them to.
_LOGIN_NOTICES = {
    provider_login.THROTTLED: ("Too many attempts", {}),
    provider_login.REGISTRATION_REFUSED: ("Invite required", {}),
    provider_login.IDENTITY_IN_USE: ("Already linked", {}),
    provider_login.ACCOUNT_UNAVAILABLE: ("Sign-in failed", {}),
}

_LINK_NOTICES = {
    provider_login.NO_SESSION: ("Sign-in link not valid", {}),
    provider_login.IDENTITY_IN_USE: ("Already linked", _BACK_TO_ACCOUNT),
    provider_login.ACCOUNT_UNAVAILABLE: ("Sign-in failed", {}),
    provider_login.KEY_UNHEALTHY: ("Encryption needs attention", _BACK_TO_ACCOUNT),
}


def _notice(request: Request, title: str, message: str, *, status: int = 400,
            back: str = "/login", back_label: str = "Back to sign in"):
    """A dead-end page for a navigation that cannot continue.

    A navigation gets a page rather than a JSON body — the visitor is a person
    looking at a browser, and an unstyled status code tells them nothing about
    what to do next.

    Every one of these ends the flow, so the handshake cookie goes with it —
    leaving it behind would pair a dead state value with whatever comes next.
    """
    response = templates.TemplateResponse(request, "auth_notice.html", {
        "request": request,
        "title": title,
        "message": message,
        "back": back,
        "back_label": back_label,
    }, status_code=status)
    auth.clear_handshake_cookie(response, load_settings(), request)
    return response


def _handshake_refused(request: Request):
    return _notice(request, "Sign-in link not valid", auth.HANDSHAKE_REJECTED, status=400)


def _refusal_notice(request: Request, refusal: provider_login.Refusal, pages: dict):
    """Render a shared refusal as this medium's dead-end page."""
    title, back = pages[refusal.kind]
    return _notice(request, title, refusal.message, status=refusal.status, **back)


async def _begin(
    request: Request,
    *,
    purpose: str,
    session_id: str | None = None,
    invite_token: str | None = None,
):
    """Create a handshake and redirect the browser to Simkl's approval screen.

    Throttled per address on the same counter the other providers' start routes
    use, so an unauthenticated caller cannot mint handshake rows in a loop — or
    get a fresh budget by rotating between providers.
    """
    settings = load_settings()
    if await auth.handshake_start_limited(request, settings):
        return _notice(request, "Too many attempts", TOO_MANY_STARTS, status=429)
    if not settings.simkl_login_configured:
        return _notice(request, "Not available", NOT_CONFIGURED, status=503)
    state = await auth.create_handshake(
        provider=PROVIDER, purpose=purpose, session_id=session_id,
        invite_token=invite_token or None,
    )
    response = RedirectResponse(
        simkl_auth.authorize_url(settings.simkl_client_id, settings.public_base_url, state),
        status_code=303,
    )
    # Pins the handshake to this browser as well as to this row, so a callback
    # completed in a different browser than the one that left is refused.
    auth.set_handshake_cookie(response, state, settings, request)
    return response


@guard.get("/auth/simkl/start", AuthLevel.PUBLIC)
async def simkl_start(request: Request):
    """Begin a sign-in. An `invite` query parameter is carried in the handshake
    row so that a first-time Simkl user can register — it travels server-side
    precisely so that nothing in the browser can substitute a different one
    part-way through."""
    return await _begin(
        request, purpose="login",
        invite_token=(request.query_params.get("invite") or "").strip(),
    )


@guard.get("/auth/simkl/link", AuthLevel.SESSION)
async def simkl_link(request: Request):
    """Begin linking a Simkl account to the account already signed in.

    Signed-in only, and bound to this exact session: linking from a logged-out
    page is what would let a callback attach an identity to whoever happened to
    be signed in when it arrived.
    """
    user = await auth.require_session(request)
    return await _begin(request, purpose="link", session_id=user.session_id)


@guard.get("/auth/simkl/callback", AuthLevel.PUBLIC)
async def simkl_callback(request: Request):
    """Where Simkl returns the browser after the user approves or declines.

    Order matters here. The handshake is validated and consumed BEFORE the
    authorization code is exchanged, so a replayed or forged callback costs one
    database lookup and never reaches Simkl at all.
    """
    settings = load_settings()
    state = request.query_params.get("state")

    if request.query_params.get("error"):
        # The user pressed "deny" on Simkl's screen, or Simkl refused. Nothing
        # was authorized, so there is nothing to undo.
        return _notice(request, "Sign-in cancelled",
                       "You didn't authorize this app on Simkl. Nothing has changed.",
                       status=400)

    if not auth.handshake_cookie_matches(request, settings, state):
        return _handshake_refused(request)

    current = await auth.current_user(request)
    try:
        handshake = await auth.consume_handshake(
            state, provider=PROVIDER,
            session_id=current.session_id if current else None,
        )
    except auth.HandshakeError:
        return _handshake_refused(request)

    code = (request.query_params.get("code") or "").strip()
    if not code:
        return _handshake_refused(request)
    if not settings.simkl_login_configured:  # pragma: no cover — cleared mid-flow
        return _notice(request, "Not available", NOT_CONFIGURED, status=503)

    try:
        token = await simkl_auth.exchange_code(
            settings.simkl_client_id, settings.simkl_client_secret, code,
            settings.public_base_url,
        )
        account = await simkl_auth.fetch_account(
            settings.simkl_client_id, token.get("access_token") or "",
        )
    except (httpx.HTTPError, simkl_transport.SimklError, simkl_auth.AccountLookupError) as exc:
        # Deliberately vague to the visitor and specific in the log: the
        # exception text can carry the request URL, and that carries the code.
        logger.warning("Simkl authorization exchange failed: %s", type(exc).__name__)
        return _notice(request, "Sign-in failed", UPSTREAM_FAILED, status=502)

    identity = auth.ProviderIdentity(
        provider=PROVIDER,
        # The numeric account id. A Simkl username can be changed by its owner
        # and re-registered by somebody else, who would inherit this link along
        # with it.
        provider_user_id=str(account["id"]),
        display_name=account.get("name"),
        avatar_url=account.get("avatar"),
        access_token=token.get("access_token"),
        # BOTH NULL, AND DELIBERATELY SO. Simkl issues no refresh token and the
        # grant it does issue stands until the user revokes the application, so
        # there is no expiry worth recording and nothing that could act on one.
        # Writing the exchange's nominal `expires_in` here would invent a
        # deadline that nothing can meet: the renewal path it implies does not
        # exist at Simkl. A 401 means link again.
        refresh_token=None,
        token_expires_at=None,
    )

    if handshake["purpose"] == "link":
        return await _finish_link(request, settings, identity, current)
    return await _finish_login(request, settings, identity, handshake)


async def _finish_link(request: Request, settings: Settings,
                       identity: auth.ProviderIdentity, current):
    """Render provider_login's link completion as a browser redirect."""
    outcome = await provider_login.complete_provider_link(
        identity=identity, current=current,
        already_linked=ALREADY_LINKED, key_unhealthy=KEY_UNHEALTHY,
    )
    if isinstance(outcome, provider_login.Refusal):
        return _refusal_notice(request, outcome, _LINK_NOTICES)
    response = RedirectResponse(outcome.redirect_target, status_code=303)
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _finish_login(request: Request, settings: Settings,
                        identity: auth.ProviderIdentity, handshake):
    """Render provider_login's sign-in completion as a browser redirect."""
    outcome = await provider_login.complete_provider_login(
        identity=identity, handshake=handshake, request=request, settings=settings,
        already_linked=ALREADY_LINKED,
    )
    if isinstance(outcome, provider_login.Refusal):
        return _refusal_notice(request, outcome, _LOGIN_NOTICES)
    response = RedirectResponse(outcome.redirect_target, status_code=303)
    # The only correct way to finish a sign-in: the session cookie going on and
    # the handshake cookie coming off are one act, not two.
    provider_login.attach_session(response, outcome, settings, request)
    return response


async def access_token_for_user(user_id: int) -> str | None:
    """This user's own Simkl access token, or None when they have not linked one.

    THE WHOLE OF IT. Trakt's counterpart carries an expiry check and a serialized
    refresh lease; Simkl issues no refresh token at all and its tokens are valid
    until the person revokes the app, so `token_expires_at` is NULL on every row
    this app writes. A branch on it could only ever take the "no expiry" path, so
    there is none — dead code that looks like a safety check is worse than the
    plain read, because the next reader has to work out that it never runs.

    A 401 later is what a revoked grant looks like, and the transport's message
    for it already says the link has to be made again.

    The token is stored sealed and is opened here, at the point of use. With no
    key set it passes through unchanged; a sealed value whose key is missing
    opens to None, so the row degrades to "no usable token" and the tracker takes
    its existing not-configured path rather than sending ciphertext to Simkl.
    """
    row = await db.fetch_one(
        "SELECT access_token FROM linked_identities WHERE user_id = ? AND provider = ?",
        (user_id, PROVIDER),
    )
    if row is None:
        return None
    return secrets_box.open_(row["access_token"])
