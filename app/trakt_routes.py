"""Log in with Trakt — the redirect authorization flow.

Three entry points and one exit:

  GET /auth/trakt/start     public. Begins a SIGN-IN (or, with an invite, a
                            registration) and sends the browser to Trakt.
  GET /auth/trakt/link      signed in. Begins a LINK of a Trakt account onto the
                            account already in session.
  GET /auth/trakt/callback  public. Where Trakt sends the browser back.

The callback is the sensitive one, and everything it does before touching Trakt
is about proving the request belongs to the visitor who started the flow. It is
a top-level GET navigation, which SameSite=Lax deliberately sends cookies on, so
without that proof an attacker could hand a signed-in victim a callback URL
carrying the ATTACKER's authorization code — and the victim's account would end
up permanently linked to the attacker's Trakt identity. The handshake row and
its cookie, both handled in app.auth, are what make that impossible; this module
refuses with one generic message the moment either fails, and never falls back
to treating an unrecognized callback as an ordinary sign-in.

The administrator's device-code flow in Settings is unaffected and stays as the
break-glass path for the app-wide connection: it needs no redirect URI, so it
still works when the registered one is wrong or the public base URL is unset.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, authz, db, secrets_box, trakt_auth
from .auth import AuthLevel
from .auth_routes import INVALID_CREDENTIALS, INVALID_INVITE, TRAKT_RECONNECT_NOTICE
from .config import Settings, load_settings

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

PROVIDER = "trakt"

# Shown when the operator hasn't finished setting the integration up. Distinct
# from every refusal below because it is addressed to the operator, describes a
# state they can fix, and reveals nothing about any account.
NOT_CONFIGURED = (
    "Signing in with Trakt isn't set up on this instance. An administrator needs "
    "to add the Trakt client ID, client secret, and public base URL in Settings."
)

# One message for a Trakt account that belongs to somebody else here. The link
# is refused rather than moved: whoever authorized last must not be able to take
# an identity away from the account holding it.
ALREADY_LINKED = (
    "That Trakt account is already linked to another user on this instance."
)

UPSTREAM_FAILED = (
    "Trakt could not complete the sign-in. Please try again in a moment."
)

TOO_MANY_STARTS = (
    "Too many sign-in attempts from this address. Try again in a few minutes."
)


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


async def _begin(
    request: Request,
    *,
    purpose: str,
    session_id: str | None = None,
    invite_token: str | None = None,
):
    """Create a handshake and redirect the browser to Trakt's approval screen.

    Throttled per address on the same counter Plex's start route uses, so an
    unauthenticated caller cannot mint handshake rows in a loop — or get a second
    budget by alternating between the two providers.
    """
    settings = load_settings()
    if await auth.handshake_start_limited(request, settings):
        return _notice(request, "Too many attempts", TOO_MANY_STARTS, status=429)
    if not settings.trakt_login_configured:
        return _notice(request, "Not available", NOT_CONFIGURED, status=503)
    state = await auth.create_handshake(
        provider=PROVIDER, purpose=purpose, session_id=session_id,
        invite_token=invite_token or None,
    )
    response = RedirectResponse(
        trakt_auth.authorize_url(settings.trakt_client_id, settings.public_base_url, state),
        status_code=303,
    )
    # Pins the handshake to this browser as well as to this row, so a callback
    # completed in a different browser than the one that left is refused.
    auth.set_handshake_cookie(response, state, settings, request)
    return response


@guard.get("/auth/trakt/start", AuthLevel.PUBLIC)
async def trakt_start(request: Request):
    """Begin a sign-in. An `invite` query parameter is carried in the handshake
    row so that a first-time Trakt user can register — it travels server-side
    precisely so that nothing in the browser can substitute a different one
    part-way through."""
    return await _begin(
        request, purpose="login",
        invite_token=(request.query_params.get("invite") or "").strip(),
    )


@guard.get("/auth/trakt/link", AuthLevel.SESSION)
async def trakt_link(request: Request):
    """Begin linking a Trakt account to the account already signed in.

    Signed-in only, and bound to this exact session: linking from a logged-out
    page is what would let a callback attach an identity to whoever happened to
    be signed in when it arrived.
    """
    user = await auth.require_session(request)
    return await _begin(request, purpose="link", session_id=user.session_id)


@guard.get("/auth/trakt/callback", AuthLevel.PUBLIC)
async def trakt_callback(request: Request):
    """Where Trakt returns the browser after the user approves or declines.

    Order matters here. The handshake is validated and consumed BEFORE the
    authorization code is exchanged, so a replayed or forged callback costs one
    database lookup and never reaches Trakt at all.
    """
    settings = load_settings()
    state = request.query_params.get("state")

    if request.query_params.get("error"):
        # The user pressed "deny" on Trakt's screen, or Trakt refused. Nothing
        # was authorized, so there is nothing to undo.
        return _notice(request, "Sign-in cancelled",
                       "You didn't authorize this app on Trakt. Nothing has changed.",
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
    if not settings.trakt_login_configured:  # pragma: no cover — cleared mid-flow
        return _notice(request, "Not available", NOT_CONFIGURED, status=503)

    try:
        token = await trakt_auth.exchange_code(
            settings.trakt_client_id, settings.trakt_client_secret, code,
            settings.public_base_url,
        )
        account = await trakt_auth.fetch_account(
            settings.trakt_client_id, token.get("access_token") or "",
        )
    except (httpx.HTTPError, trakt_auth.AccountLookupError) as exc:
        # Deliberately vague to the visitor and specific in the log: the
        # exception text can carry the request URL, and that carries the code.
        logger.warning("Trakt authorization exchange failed: %s", type(exc).__name__)
        return _notice(request, "Sign-in failed", UPSTREAM_FAILED, status=502)

    identity = auth.ProviderIdentity(
        provider=PROVIDER,
        # The account UUID — Trakt exposes no numeric user id at all. A username
        # or slug can be released by its owner and re-registered by somebody
        # else, who would inherit this link along with it.
        provider_user_id=str(account["id"]),
        display_name=account.get("name"),
        access_token=token.get("access_token"),
        refresh_token=token.get("refresh_token") or None,
        token_expires_at=_expires_at(token),
    )

    if handshake["purpose"] == "link":
        return await _finish_link(request, settings, identity, current)
    return await _finish_login(request, settings, identity, handshake)


def _expires_at(token: dict) -> int | None:
    """When the access token stops working, as a unix timestamp."""
    expires_in = token.get("expires_in")
    if not expires_in:
        return None
    return int(token.get("created_at") or time.time()) + int(expires_in)


async def _finish_link(request: Request, settings: Settings,
                       identity: auth.ProviderIdentity, current):
    if current is None:  # pragma: no cover — consume_handshake already required it
        return _handshake_refused(request)
    try:
        await auth.link_provider_identity(identity=identity, user_id=current.user_id)
    except auth.IdentityInUse:
        return _notice(request, "Already linked", ALREADY_LINKED,
                       status=409, back="/me", back_label="Back to your account")
    except auth.AccountUnavailable:
        return _notice(request, "Sign-in failed", auth.HANDSHAKE_REJECTED, status=403)
    await _clear_reconnect_notice(current.is_admin)
    response = RedirectResponse("/me", status_code=303)
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _finish_login(request: Request, settings: Settings,
                        identity: auth.ProviderIdentity, handshake):
    ip = auth.client_ip(request, settings)
    token = handshake["invite_token"]
    # Only a REGISTRATION is throttled. An ordinary sign-in with an already
    # known identity is no more expensive than one with a password, and
    # throttling it would lock out a household behind one address.
    if await auth.find_identity(PROVIDER, identity.provider_user_id) is None:
        if await _registration_rate_limited(ip, token):
            return _notice(request, "Too many attempts",
                           "Too many sign-up attempts from this address. Try again later.",
                           status=429)
    try:
        outcome = await auth.login_with_provider_identity(
            identity=identity, invite_token=token, ip_address=ip, settings=settings,
        )
    except auth.RegistrationRefused:
        # An unknown Trakt account with no usable invite. NO account is created,
        # and every unusable-invite cause renders the same page.
        await _record_registration_attempt(ip, token, False)
        return _notice(request, "Invite required", INVALID_INVITE, status=403)
    except auth.IdentityInUse:  # pragma: no cover — needs a concurrent registration
        return _notice(request, "Already linked", ALREADY_LINKED, status=409)
    except auth.AccountUnavailable:
        # A disabled account, reported exactly like a failed password sign-in so
        # that a provider callback is not an oracle for account state.
        return _notice(request, "Sign-in failed", INVALID_CREDENTIALS, status=403)

    if outcome.kind == "registered":
        await _record_registration_attempt(ip, token, True)
    else:
        await _clear_reconnect_notice(await _is_admin(outcome.user_id))

    session_id = await auth.create_session(
        outcome.user_id, user_agent=request.headers.get("user-agent"), ip_address=ip,
    )
    response = RedirectResponse("/" if outcome.calendar_approved else "/me", status_code=303)
    auth.set_session_cookie(response, session_id, settings, request)
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _registration_rate_limited(ip: str, token: str | None) -> bool:
    """The same two per-address volume limits registration with a password uses,
    so a script cycling Trakt accounts or invite tokens through this path is
    throttled exactly as one cycling them through the form is."""
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


async def _is_admin(user_id: int) -> bool:
    user = await auth.get_user(user_id)
    return bool(user and user["is_admin"])


async def _clear_reconnect_notice(is_admin: bool) -> None:
    """Take down the "reconnect your Trakt account" prompt in Settings.

    That notice is raised at first-run setup when the Trakt token already in
    settings.json could not be resolved to an account id, and an administrator
    completing this flow is exactly the reconnection it was asking for. A
    non-admin linking their own account is not, so it stays up for them.
    """
    if is_admin:
        await db.set_meta(TRAKT_RECONNECT_NOTICE, "")


async def adopt_app_token(user_id: int, settings: Settings) -> tuple[bool, str | None]:
    """Link settings.json's app-wide Trakt token to `user_id` as a personal
    identity. Returns (linked, why_it_did_not).

    First-run setup does this too, but it can only try once, and it fails
    whenever Trakt is unreachable or the stored token has already expired —
    leaving the operator with the reconnect notice up and no way to clear it,
    because the device-code flow in Settings renews the app-wide token without
    ever touching `linked_identities`. Running the same adoption after a
    successful device authorization is what closes that loop: the token is
    known-good at that exact moment, so the account lookup behind it is as
    likely to succeed as it will ever be.

    The reason is RETURNED, not only logged. Every failure here leaves the same
    unexplained notice on the Settings screen, and its causes want different
    actions — authorize again, or free the Trakt account from the other login on
    this instance. Sending the operator to a log file to find out which is what
    made a clearable notice look permanent.

    Best effort by design — the caller has already saved a working token and must
    not fail on this.
    """
    token = (settings.trakt_access_token or "").strip()
    client_id = (settings.trakt_client_id or "").strip()
    if not (token and client_id):
        return False, "No Trakt Client ID and access token are saved yet."
    try:
        account = await trakt_auth.fetch_account(client_id, token)
    except trakt_auth.AccountLookupError as exc:
        logger.warning("Could not adopt the app-wide Trakt token: %s", exc)
        return False, (
            f"Trakt would not say which account this token belongs to ({exc}) — "
            "authorize again to get a fresh one."
        )
    try:
        await auth.link_provider_identity(
            identity=auth.ProviderIdentity(
                provider=PROVIDER,
                provider_user_id=str(account["id"]),
                display_name=account.get("name"),
                access_token=token,
                refresh_token=settings.trakt_refresh_token or None,
                token_expires_at=settings.trakt_token_expires_at or None,
            ),
            user_id=user_id,
        )
    except auth.IdentityInUse:
        # Somebody else already holds this Trakt account here. Never moved —
        # and the notice stays up, because this login is still unlinked.
        logger.warning("The app-wide Trakt account is already linked to another user.")
        return False, (
            "The Trakt account this token belongs to is already linked to "
            f"{await _identity_owner_label(str(account['id']))} on this instance. "
            "Unlink it there first, then try again."
        )
    except auth.AccountUnavailable:
        return False, "This login can't be linked to right now."
    await db.set_meta(TRAKT_RECONNECT_NOTICE, "")
    return True, None


async def _identity_owner_label(provider_user_id: str) -> str:
    """Which local login already holds this Trakt account, named well enough to
    act on. Falls back to a bare description when the row has no username, which
    an account created through Plex or Trakt legitimately may not."""
    row = await db.fetch_one(
        "SELECT u.username AS username FROM linked_identities li "
        "JOIN users u ON u.id = li.user_id "
        "WHERE li.provider = ? AND li.provider_user_id = ?",
        (PROVIDER, provider_user_id),
    )
    username = (row["username"] if row else None) or ""
    return f'the "{username}" account' if username else "another account here"


REVOKE_FAILED_NOTICE = (
    "Unlinked here, but this instance could not tell Trakt to forget the "
    "authorization. Remove it yourself under Settings > Connected apps on trakt.tv."
)


async def stored_access_token(user_id: int) -> str | None:
    """`user_id`'s Trakt token exactly as stored, with no refresh attempted.

    Read this BEFORE unlinking — the token lives on the row an unlink deletes,
    so afterwards there is nothing left to revoke.
    """
    row = await db.fetch_one(
        "SELECT access_token FROM linked_identities WHERE user_id = ? AND provider = ?",
        (user_id, PROVIDER),
    )
    # Opened at this point of use — the row stores it sealed — so the revoke call
    # posts the real token to Trakt rather than `enc:v1:...`.
    token = secrets_box.open_(row["access_token"]) if row else None
    return token or None


async def revoke_token_value(token: str | None, settings: Settings | None = None) -> str | None:
    """Invalidate a Trakt token at Trakt's end. Returns None on success (or when
    there was nothing to revoke), else a notice to show the user.

    Call this only once the unlink has actually gone through. A failure here is
    never allowed to undo it: the local row going away is what stops this
    instance using the token, and rolling an unlink back because a third party
    was unreachable would leave the user stuck with a link they asked to remove.
    """
    cfg = settings or load_settings()
    if not token:
        return None
    if not (cfg.trakt_client_id and cfg.trakt_client_secret):
        # Revocation is authenticated with the app's own credentials. Without
        # them there is no call to make, and saying so is more useful than
        # silently doing nothing.
        return REVOKE_FAILED_NOTICE
    try:
        await trakt_auth.revoke_token(cfg.trakt_client_id, cfg.trakt_client_secret, token)
    except httpx.HTTPError as exc:
        logger.warning("Trakt token revocation failed for a linked account: %s", exc)
        return REVOKE_FAILED_NOTICE
    return None


async def access_token_for_user(user_id: int, settings: Settings | None = None) -> str | None:
    """A currently-valid Trakt access token for `user_id`, or None.

    This is what reads a user's own Trakt data on their behalf, rather than the
    app-wide token in settings.json — that one belongs to the operator and says
    nothing about anybody else's watch history.

    Renewal is serialized through the identity row's refresh lease, so two
    concurrent requests can't both spend the same single-use refresh token and
    invalidate each other. The request that loses the race falls back to the
    token already stored, which is either still valid or about to be replaced by
    the request that won.
    """
    cfg = settings or load_settings()
    row = await db.fetch_one(
        "SELECT * FROM linked_identities WHERE user_id = ? AND provider = ?",
        (user_id, PROVIDER),
    )
    if row is None:
        return None
    # Both tokens are stored sealed; open them here, at the point they are used.
    # With no key set they pass through unchanged; a sealed value whose key is
    # missing opens to None, so the row degrades to "no usable token" (fail open)
    # and — because the refresh guard below then short-circuits — nothing is
    # written back over the intact ciphertext. A wrong key raises out of open_().
    access_token = secrets_box.open_(row["access_token"])
    expires_at = row["token_expires_at"]
    if not expires_at or time.time() < int(expires_at) - 60:
        return access_token
    refresh_token = secrets_box.open_(row["refresh_token"])
    if not (refresh_token and cfg.trakt_client_id and cfg.trakt_client_secret):
        return access_token
    if not await auth.claim_identity_refresh(int(row["id"])):
        return access_token
    try:
        token = await trakt_auth.refresh_access_token(
            cfg.trakt_client_id, cfg.trakt_client_secret, refresh_token,
        )
    except httpx.HTTPError as exc:
        logger.warning("Trakt token refresh failed for a linked account: %s", exc)
        await auth.release_identity_refresh(int(row["id"]))
        return access_token
    await auth.store_identity_tokens(
        int(row["id"]),
        access_token=token.get("access_token"),
        refresh_token=token.get("refresh_token") or None,
        token_expires_at=_expires_at(token),
    )
    return token.get("access_token")
