"""What happens after a provider says who somebody is — the part that is the
same whichever provider said it.

Plex answers a polling XHR in JSON and Trakt answers a browser callback in HTML,
so for a long time each carried its own copy of this sequence. The copies were
near-identical and drifted the way copies do; the policy in them is
security-critical, and a rule that has to hold at every completion cannot live in
one completion's function body. So the sequence lives here, once, and each route
module renders the answer in its own medium.

THE SEQUENCE, and why each step is where it is:

    resolve the client IP
    -> throttle, but ONLY if this identity is unknown. An unknown identity means
       a REGISTRATION; a known one is an ordinary sign-in, no more expensive than
       one with a password, and throttling those would lock out a household
       behind a single address.
    -> auth.login_with_provider_identity
    -> the four refusals, in this order, with these meanings
    -> record the registration attempt, success or failure, so the throttle above
       counts what actually happened
    -> auth.create_session
    -> redirect by outcome.calendar_approved

THE FOUR REFUSALS ARE NOT INTERCHANGEABLE and the order they are caught in is
part of the policy:

    THROTTLED             too many registrations from this address lately. 429.
    REGISTRATION_REFUSED  an unknown provider account with no usable invite. NO
                          account is created, and every unusable-invite cause
                          renders one message — which one it was is not something
                          an anonymous caller gets to learn. 403.
    IDENTITY_IN_USE       the provider account belongs to somebody else here.
                          Refused, never moved: silently reassigning it would
                          mean whoever authorizes last owns the identity. 409.
    ACCOUNT_UNAVAILABLE   the identity resolved to a DISABLED account, and it is
                          reported EXACTLY as a wrong password is — same message,
                          same status. A provider completion that said "this
                          account exists but is disabled" would be an oracle for
                          account state to anyone who can authorize at that
                          provider, which is anyone. 403.

WHAT STAYS IN THE ROUTE MODULES, and why it is not a failure to abstract it:

  - The MEDIUM. A JSONResponse and a rendered notice page are not the same thing
    and pretending otherwise would put HTML in here.
  - The two messages that NAME THE PROVIDER ("That Trakt account is already
    linked...") and the wording of the encryption refusal. Those are the honest
    per-provider difference, so they are passed in rather than guessed at from a
    provider string.
  - Trakt's reconnect-notice housekeeping, which is about the app-wide Trakt
    TOKEN and has no analogue anywhere else.

The `*_start` / `*_link` entry points are deliberately NOT here. They are three
lines each around a `_begin` that is wholly provider-specific — Plex mints a PIN
and returns JSON, Trakt builds an authorize URL and 303s — so the resemblance is
in the adapter and not in the substance.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import Response

from .. import auth
from ..config import Settings
from .routes import INVALID_CREDENTIALS, INVALID_INVITE

# The refusal names a route matches on to choose a title, a back link, or an
# extra JSON field. They are stable strings rather than an enum because that is
# all a caller does with them, and because the message and status that go with
# each already travel on the refusal itself.
THROTTLED = "throttled"
REGISTRATION_REFUSED = "registration_refused"
IDENTITY_IN_USE = "identity_in_use"
ACCOUNT_UNAVAILABLE = "account_unavailable"
NO_SESSION = "no_session"
KEY_UNHEALTHY = "key_unhealthy"

TOO_MANY_REGISTRATIONS = "Too many sign-up attempts from this address. Try again later."


@dataclass(frozen=True)
class Refusal:
    """A completion that may not proceed, and everything both media need to say
    so: which refusal it was, the text to show, and the status to send.

    Returned rather than raised. Each of these is an ordinary, expected answer to
    an ordinary request — an invite that ran out, an account somebody disabled —
    and a route has to render every one of them, so making three of the four
    exceptional would only mean catching them all again at the call site.
    """
    kind: str
    message: str
    status: int


@dataclass(frozen=True)
class LoginResult:
    """A completed sign-in. The route sets the cookies and answers.

    `registered` distinguishes a brand-new account from a returning one. It is
    on here because the caller cannot re-derive it — by the time it holds this,
    the identity exists either way.
    """
    user_id: int
    session_id: str
    redirect_target: str
    registered: bool


@dataclass(frozen=True)
class LinkResult:
    """A provider account successfully attached to the account in session."""
    user_id: int
    redirect_target: str


async def complete_provider_login(
    *,
    identity: auth.ProviderIdentity,
    handshake,
    request: Request,
    settings: Settings,
    already_linked: str,
) -> LoginResult | Refusal:
    """Sign in (or register) with a provider identity a completed handshake
    produced. See this module's docstring for the sequence and the refusals.

    `already_linked` is the message for an identity that belongs to another
    account here; it names the provider, which is the one thing this function
    cannot say for itself.
    """
    ip = auth.client_ip(request, settings)
    token = handshake["invite_token"]
    # Only a REGISTRATION is throttled — see the module docstring. `find_identity`
    # is what tells the two apart, and it runs before the throttle so a returning
    # user never spends any of that budget.
    if await auth.find_identity(identity.provider, identity.provider_user_id) is None:
        if await auth.registration_rate_limited(ip, token):
            return Refusal(THROTTLED, TOO_MANY_REGISTRATIONS, 429)
    try:
        outcome = await auth.login_with_provider_identity(
            identity=identity, invite_token=token, ip_address=ip, settings=settings,
        )
    except auth.RegistrationRefused:
        # An unknown provider account with no usable invite. The failed attempt
        # is recorded so the throttle above sees it.
        await auth.record_registration_attempt(ip, token, False)
        return Refusal(REGISTRATION_REFUSED, INVALID_INVITE, 403)
    except auth.IdentityInUse:  # pragma: no cover — needs a concurrent registration
        return Refusal(IDENTITY_IN_USE, already_linked, 409)
    except auth.AccountUnavailable:
        # A disabled account, reported exactly like a failed password sign-in so
        # that a provider completion is not an oracle for account state.
        return Refusal(ACCOUNT_UNAVAILABLE, INVALID_CREDENTIALS, 403)

    if outcome.kind == "registered":
        await auth.record_registration_attempt(ip, token, True)

    session_id = await auth.create_session(
        outcome.user_id, user_agent=request.headers.get("user-agent"), ip_address=ip,
    )
    return LoginResult(
        user_id=outcome.user_id,
        session_id=session_id,
        # An account still waiting on approval has nothing to see on the calendar,
        # so it lands on its own account page instead of on an empty month.
        redirect_target="/" if outcome.calendar_approved else "/me",
        registered=outcome.kind == "registered",
    )


async def complete_provider_link(
    *,
    identity: auth.ProviderIdentity,
    current,
    already_linked: str,
    key_unhealthy: str,
) -> LinkResult | Refusal:
    """Attach a provider identity to the account already in session.

    The same shape one layer smaller: no registration, so no throttle and no
    invite, but the same refusals with the same meanings. There is one extra —
    IdentityWritesBlocked — because linking WRITES a token pair, and a link is
    the only one of the two paths that can be asked to overwrite tokens that are
    already sealed at rest.

    `current` is the signed-in user. None means the handshake was consumed
    without one, which the consumption itself already required, so it is refused
    here rather than trusted.
    """
    if current is None:  # pragma: no cover — consume_handshake already required it
        return Refusal(NO_SESSION, auth.HANDSHAKE_REJECTED, 400)
    try:
        await auth.link_provider_identity(identity=identity, user_id=current.user_id)
    except auth.IdentityInUse:
        return Refusal(IDENTITY_IN_USE, already_linked, 409)
    except auth.AccountUnavailable:
        return Refusal(ACCOUNT_UNAVAILABLE, auth.HANDSHAKE_REJECTED, 403)
    except auth.IdentityWritesBlocked:
        return Refusal(KEY_UNHEALTHY, key_unhealthy, 409)
    return LinkResult(user_id=current.user_id, redirect_target="/me")


def attach_session(
    response: Response, result: LoginResult, settings: Settings, request: Request,
) -> None:
    """Put the new session on the response and take the handshake cookie off it.

    ONE function for the pair because they belong together: the handshake has
    done its job the moment a session exists, and a completion that set the
    session and left the handshake cookie behind would pair a spent state value
    with whatever the browser does next. Every refusal path clears it too — the
    route modules do that in their own notice/error helpers, because a refusal
    has no session to set and would otherwise be calling this for half of it.
    """
    auth.set_session_cookie(response, result.session_id, settings, request)
    auth.clear_handshake_cookie(response, settings, request)
