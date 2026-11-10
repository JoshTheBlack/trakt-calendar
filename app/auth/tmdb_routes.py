"""Connect TMDB — a LINK of a person's TMDB account onto the account they are
signed in to, through TMDB's v4 user authorization. NEVER A SIGN-IN.

Two routes, and what TMDB does between them is app/auth/tmdb.py's docstring:

  GET /auth/tmdb/link      signed in. Asks TMDB for a request token whose
                           approval sends the browser back here, and sends the
                           browser to TMDB's approval page.
  GET /auth/tmdb/callback  where TMDB sends the browser back.

WHAT A LINK IS FOR: the tracker writes the viewer's shows to their TMDB watchlist
(app/providers/tmdb/watchlist.py, through providers.for_watchlist) and a rankings
board can be seeded from their TMDB ratings. It is not a way in — there is no
start route, the callback finishes only a `link` handshake, and
app/auth/revocation.py names TMDB as a service nobody signs in with, so it never
counts as an account's way back in when another link is removed.

THE CALLBACK PROVES IT BELONGS TO THE BROWSER AND SESSION THAT STARTED IT,
exactly as the Trakt and Simkl ones do (app/auth/handshakes.py says why): the
state is checked against the handshake cookie and consumed once, bound to the
session that asked, before anything reaches TMDB. Two things are particular to
TMDB, and both follow from what was measured:
  - THE STATE RIDES INSIDE redirect_to. TMDB sends the browser to the address it
    was given exactly, appending nothing, so the address this app gives it
    carries the state — and is built from the configured public base URL and
    nothing in the request, so an approval can only ever come back here.
  - THE REQUEST TOKEN NEVER COMES BACK FROM THE BROWSER. It is kept, sealed, on
    the handshake row (the slot the code-exchange flows keep their PKCE verifier
    in: the one server-side value the callback needs to finish), and the callback
    trades THAT one. A callback cannot hand over a request token somebody else
    approved and so link their TMDB account to this one.

WHAT NOTHING HERE CAN TELL: WHO APPROVED. TMDB binds an approval to the REQUEST
TOKEN, not to a browser or a person — whoever opens the approval address while
signed in to TMDB and presses Approve grants their own account to whoever started
the link. So the person who starts a link receives the grant of whichever TMDB
account approves its token: somebody persuaded to approve someone else's approval
link would link THEIR TMDB account to that someone's account here (starting one
needs an account on this instance). TMDB's approval page names this instance's
TMDB application, which is the only warning anybody gets; the exchange answers
with the approving account's id and nothing that says who pressed the button.

THE GRANT IS SEALED AT REST like every token (linked_identities.access_token,
enc:v1: when a key is set), opened only where it is sent, and never logged,
cached or put in an address. TMDB issues no refresh token and its grant has no
clock: it lasts until revoked, which an unlink does (`revoke_token_value`).
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import auth, authz, db, secrets_box
from ..config import Settings, load_settings
from ..templating import templates
from . import provider_login
from . import tmdb as tmdb_auth
from .levels import AuthLevel

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# ONE constant, for the handshake row, the identity row and the unlink — a second
# spelling would not fail at the database (the provider column is an open set of
# names), it would mint an identity nothing else can find.
PROVIDER = "tmdb"

NOT_CONFIGURED = (
    "Connecting TMDB isn't set up on this instance. An administrator needs to add "
    "the TMDB API Read Access Token and the public base URL in Settings."
)
ALREADY_LINKED = "That TMDB account is already linked to another user on this instance."
ALREADY_CONNECTED = (
    "TMDB is already connected to your account. Unlink it first to connect a "
    "different TMDB account."
)
UPSTREAM_FAILED = "TMDB could not complete the connection. Please try again in a moment."
NOT_APPROVED = "TMDB didn't approve the connection, so nothing has changed."
TOO_MANY_STARTS = "Too many attempts from this address. Try again in a few minutes."
KEY_UNHEALTHY = (
    "This instance's stored secrets are encrypted, but the key is currently "
    "missing or wrong, so connecting is refused rather than writing a fresh token "
    "in the clear. An administrator needs to restore ENCRYPTION_KEY (see "
    "Settings) before connecting can continue."
)
REVOKE_FAILED_NOTICE = (
    "Unlinked here, but this instance could not tell TMDB to forget the "
    "connection. Remove it yourself under Settings > Sessions on themoviedb.org."
)

_BACK_TO_ACCOUNT = {"back": "/me", "back_label": "Back to your account"}

# The title of the dead-end page each shared refusal is drawn as. A link starts
# and ends on the account page, so every one of them goes "back" there.
_LINK_TITLES = {
    provider_login.NO_SESSION: "Link not valid",
    provider_login.IDENTITY_IN_USE: "Already linked",
    provider_login.ANOTHER_ACCOUNT_HELD: "Already connected",
    provider_login.ACCOUNT_UNAVAILABLE: "Link failed",
    provider_login.KEY_UNHEALTHY: "Encryption needs attention",
}


async def _notice(request: Request, title: str, message: str, *, status: int = 400):
    """A dead-end page for a navigation that cannot continue, which also ends the
    flow's handshake cookie — leaving it would pair a dead state with whatever
    comes next."""
    response = templates.TemplateResponse(request, "auth_notice.html", {
        "request": request, "title": title, "message": message, **_BACK_TO_ACCOUNT,
    }, status_code=status)
    auth.clear_handshake_cookie(response, await load_settings(), request)
    return response


@guard.get("/auth/tmdb/link", AuthLevel.SESSION)
async def tmdb_link(request: Request):
    """Begin linking a TMDB account to the account signed in, bound to this
    exact session. Throttled per address on the counter every provider's start
    shares, so a caller cannot mint handshakes and request tokens in a loop."""
    user = await auth.require_session(request)
    settings = await load_settings()
    if await auth.handshake_start_limited(request, settings):
        return await _notice(request, "Too many attempts", TOO_MANY_STARTS, status=429)
    if not settings.tmdb_link_configured:
        return await _notice(request, "Not available", NOT_CONFIGURED, status=503)
    if await _holds_a_link(user.user_id):
        return await _notice(request, "Already connected", ALREADY_CONNECTED, status=409)
    # The row is made FIRST, so its state can ride in the address TMDB is given;
    # the request token is then kept on it (`hold_on_handshake`).
    state = await auth.create_handshake(provider=PROVIDER, purpose="link",
                                        session_id=user.session_id)
    try:
        token = await tmdb_auth.request_token(
            settings, tmdb_auth.redirect_to(settings.public_base_url, state))
    except tmdb_auth.TmdbAuthError as exc:
        logger.warning("TMDB link could not start: %s", exc)
        return await _notice(request, "Link failed", UPSTREAM_FAILED, status=502)
    await auth.hold_on_handshake(state, token)
    response = RedirectResponse(tmdb_auth.approve_url(token), status_code=303)
    auth.set_handshake_cookie(response, state, settings, request)
    return response


@guard.get("/auth/tmdb/callback", AuthLevel.PUBLIC)
async def tmdb_callback(request: Request):
    """Where TMDB returns the browser. PUBLIC like every provider callback, so a
    browser arriving signed out is told the link is not valid rather than sent to
    a sign-in page — and it IS refused: the handshake is bound to the session
    that started it.

    THE HANDSHAKE IS VALIDATED AND CONSUMED BEFORE TMDB IS ASKED ANYTHING, so a
    forged or replayed callback costs one database read and never reaches TMDB.
    """
    settings = await load_settings()
    state = request.query_params.get("state")
    if not auth.handshake_cookie_matches(request, settings, state):
        return await _notice(request, "Link not valid", auth.HANDSHAKE_REJECTED)
    current = await auth.current_user(request)
    try:
        handshake = await auth.consume_handshake(
            state, provider=PROVIDER, session_id=current.session_id if current else None)
    except auth.HandshakeError:
        return await _notice(request, "Link not valid", auth.HANDSHAKE_REJECTED)
    if handshake["purpose"] != "link":  # pragma: no cover — no route makes another kind
        return await _notice(request, "Link not valid", auth.HANDSHAKE_REJECTED)
    if not settings.tmdb_link_configured:  # pragma: no cover — cleared mid-flow
        return await _notice(request, "Not available", NOT_CONFIGURED, status=503)
    request_token = auth.held_on_handshake(handshake)
    if not request_token:
        return await _notice(request, "Link not valid", auth.HANDSHAKE_REJECTED)
    # Asked again here, BEFORE the exchange, because two tabs can each start a
    # link while neither has finished: refused now, the approved request token is
    # never traded and no grant exists to be left standing.
    if await _holds_a_link(current.user_id):
        return await _notice(request, "Already connected", ALREADY_CONNECTED, status=409)
    try:
        grant = await tmdb_auth.exchange(settings, request_token)
    except tmdb_auth.TmdbAuthError as exc:
        # Declining on TMDB's page and TMDB failing look the same from here: the
        # exchange is refused. Either way nothing was granted.
        logger.info("TMDB link not completed: %s", exc)
        return await _notice(request, "Not connected", NOT_APPROVED)
    try:
        account = await tmdb_auth.fetch_account(grant.access_token)
    except tmdb_auth.TmdbAuthError as exc:
        logger.warning("TMDB link: the account read failed: %s", exc)
        await _revoke_quietly(settings, grant.access_token)
        return await _notice(request, "Link failed", UPSTREAM_FAILED, status=502)
    identity = auth.ProviderIdentity(
        provider=PROVIDER,
        # TMDB's account object id: permanent, and what its account reads are
        # addressed by. Never the username, which its owner can change.
        provider_user_id=grant.account_id,
        display_name=account.username or None,
        access_token=grant.access_token,
    )
    outcome = await provider_login.complete_provider_link(
        identity=identity, current=current, already_linked=ALREADY_LINKED,
        holds_another=ALREADY_CONNECTED, key_unhealthy=KEY_UNHEALTHY)
    if isinstance(outcome, provider_login.Refusal):
        # The grant was minted for a link that did not happen, and the refusal
        # has ended it at TMDB already (provider_login.end_unkept_grant).
        return await _notice(request, _LINK_TITLES[outcome.kind], outcome.message,
                             status=outcome.status)
    response = RedirectResponse(outcome.redirect_target, status_code=303)
    auth.clear_handshake_cookie(response, settings, request)
    return response


async def _holds_a_link(user_id: int) -> bool:
    """Whether `user_id` already has a TMDB link — of ANY TMDB account.

    ONE TMDB LINK PER ACCOUNT, refused rather than replaced — and STRICTER than
    the rule every service's link is held to at the one write
    (auth.link_provider_identity refuses a DIFFERENT account of a service the
    account already holds, auth.AnotherAccountHeld). There, re-linking the SAME
    account is a "reconnect" that refreshes a token with a clock; a TMDB grant
    has no clock, so there is nothing to refresh: re-linking would only overwrite
    a grant nothing here then revokes, leaving a session standing at TMDB. So
    TMDB refuses the same account too, and asks BEFORE the exchange, because a
    grant minted for a link that is then refused has to be revoked again. Two
    rules, not one: the shared one still stands behind this, for a second TMDB
    link racing the first past both of these checks.
    """
    row = await db.fetch_one(
        "SELECT 1 FROM linked_identities WHERE user_id = ? AND provider = ?",
        (user_id, PROVIDER))
    return row is not None


async def _revoke_quietly(settings: Settings, token: str) -> None:
    try:
        await tmdb_auth.revoke(settings, token)
    except tmdb_auth.TmdbAuthError as exc:
        logger.warning("TMDB link: an unused grant could not be revoked: %s", exc)


class Grant(NamedTuple):
    """A viewer's own TMDB grant, opened for sending."""
    access_token: str
    account_id: str


async def grant_for_user(user_id: int) -> Grant | None:
    """`user_id`'s TMDB grant, opened, or None when they have not linked TMDB (or
    the at-rest key cannot open what is stored, which reads as no grant rather
    than sending `enc:v1:...` to TMDB). No refresh: TMDB issues none, and a grant
    it has revoked answers 401 to whatever is sent with it."""
    row = await db.fetch_one(
        "SELECT provider_user_id, access_token FROM linked_identities "
        "WHERE user_id = ? AND provider = ?", (user_id, PROVIDER))
    token = secrets_box.open_(row["access_token"]) if row else None
    if not token:
        return None
    return Grant(token, str(row["provider_user_id"]))


async def stored_access_token(user_id: int) -> str | None:
    """`user_id`'s TMDB token as stored, opened — read BEFORE an unlink, since
    the token lives on the row the unlink deletes."""
    grant = await grant_for_user(user_id)
    return grant.access_token if grant else None


async def revoke_token_value(token: str | None, settings: Settings | None = None) -> str | None:
    """End a TMDB grant at TMDB's end. None on success (or with nothing to
    revoke), else the notice to show the person.

    CALLED ONLY ONCE THE UNLINK HAS GONE THROUGH, and a failure never undoes it —
    as for Trakt and Simkl: the row going away is what stops this instance using
    the token, and rolling the unlink back because TMDB was unreachable would
    leave the person stuck with a link they asked to remove.
    """
    if not token:
        return None
    cfg = settings or await load_settings()
    if not cfg.tmdb_configured:
        # Revocation is authenticated with the instance's read token; without it
        # there is no call to make, and saying so beats doing nothing silently.
        return REVOKE_FAILED_NOTICE
    try:
        await tmdb_auth.revoke(cfg, token)
    except tmdb_auth.TmdbAuthError as exc:
        logger.warning("TMDB grant revocation failed for a linked account: %s", exc)
        return REVOKE_FAILED_NOTICE
    return None
