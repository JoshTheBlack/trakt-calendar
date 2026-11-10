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
    settings.json and seeds the new account's view preferences and timezone
    from settings.json, so the calendar looks exactly as it did.

  - JSON-only request bodies. A form-encoded POST is a CORS "simple request"
    that browsers send with no preflight, so accepting one would leave these
    endpoints defended by SameSite alone. The rule itself is enforced for every
    mutating route at once, in app/authz.py's request_shape_guard, rather than
    here — this note says what the constraint MEANS for these pages: every form
    here posts itself through htmx's json-enc, never as a native form post, and
    none may be added.

EVERY FORM ON THESE PAGES IS ANSWERED WITH MARKUP. A refusal is a sentence drawn
where the form shows it, at 200, because a non-200 is never swapped
(static/js/boost.js's `noSwap`) — so the sentence a person needs would otherwise be
a press that did nothing. A success that changes which page somebody should be on
is `204` with `HX-Redirect`: a redirect STATUS would be followed by the request and
the page it found swapped into the form.

Registration is gated by an invite unless the operator has turned that off,
and every sign-in / registration failure is throttled per username and per IP
independently, using the same generic response and timing class as an
ordinary failure — see the login and register handlers for why.
"""
from __future__ import annotations

import dataclasses
import logging
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

# `auth` is the package this module lives in, reached through the parent so the
# routes read the public surface app/auth/__init__.py exposes rather than
# knowing which submodule each name is defined in.
from .. import auth, authz, chrome, clock, db, providers, route_params, secrets_box
from ..config import Settings, load_settings, save_settings
from ..media import user_images
from ..templating import templates
# Bound as `trakt_auth` because inside app/auth/ a bare `trakt` would read as the
# Trakt SOURCE (app/providers/trakt/); this is the login flow.
from . import passkeys, provider_avatars, revocation
from . import trakt as trakt_auth
from .levels import AuthLevel

logger = logging.getLogger(__name__)

router = APIRouter()
# Every route here declares its access level through the same registrar the rest
# of the app uses, so the startup audit and the deny-by-default middleware see
# them exactly as they see the routes defined on the app itself.
guard = authz.Guard(router)

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


def refused(request: Request, message: str) -> Response:
    """A form's refusal, as the sentence its error line shows.

    One template for every form on the signed-out pages and the account page, so
    what a refusal IS — escaped text, nothing else — is stated once. The form
    names where it lands (`hx-target`), which keeps this knowing nothing about any
    page's layout."""
    return templates.TemplateResponse(request, "_form_refusal.html", {
        "request": request, "message": message})


def go_to(target: str) -> Response:
    """Send the browser to `target` as a real navigation — see the module
    docstring for why this is a header and not a redirect status."""
    return Response(status_code=204, headers={"HX-Redirect": target})


async def _signed_in(request: Request, user_id: int, calendar_approved: bool,
                     settings: Settings, ip: str) -> Response:
    """THE END OF EVERY SIGN-IN THIS MODULE MAKES — first-run setup, registration,
    a password and a passkey: the account's last sign-in stamped, a session, and
    the browser sent to the calendar, or to its own account page while it waits
    on approval. Said once, so the four cannot come to differ in what a session
    is issued with — the sign-in hint included (cookies.set_session_cookie)."""
    await auth.mark_logged_in(user_id)
    session_id = await auth.create_session(
        user_id, user_agent=request.headers.get("user-agent"), ip_address=ip)
    response = go_to("/" if calendar_approved else "/me")
    auth.set_session_cookie(response, session_id, settings, request,
                            leads_with_passkey=await passkeys.holds_any(user_id))
    return response


def _source_prefs():
    """app/sources/prefs, imported at the point of use rather than at the top.

    THE PLACEMENT IS THE WHOLE REASON THIS IS A FUNCTION. auth is a LAYER every
    feature may depend on, and that only stays true while it names none of them
    at module level; a feature package imported at the top of this file would
    make the layer a peer.

    Reached at all because the account page is where the two ORDERS are stated —
    which linked tracker decides a season is finished, and whose description of a
    title you read. Both are stored with the other per-account source preferences
    rather than in a second place that answers "which service leads".
    """
    from ..sources import prefs
    return prefs


# ---------------------------------------------------------------------------
# first-run setup
# ---------------------------------------------------------------------------

@guard.get("/onboarding", AuthLevel.PUBLIC)
async def onboarding_page(request: Request):
    if await auth.any_users_exist():
        return RedirectResponse("/login", status_code=303)
    settings = await load_settings()
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
    data = await authz.json_body(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    confirm = str(data.get("password_confirm") or "")

    if err := auth.identifier_error(username):
        return refused(request, err)
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return refused(request, f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
    if password != confirm:
        return refused(request, "The two passwords don't match.")
    if await auth.any_users_exist():
        return refused(request, ALREADY_SET_UP)

    settings = await load_settings()
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
            distrakt_approved=True, ranker_approved=True,
            timezone=settings.timezone or None,
        )
        # Seeded from settings.json so an upgraded instance's calendar renders
        # exactly as it did before there were accounts — filters included, which
        # is why this is the one call site that asks for them. Every account made
        # after this one starts unfiltered (see insert_user_prefs).
        auth.insert_user_prefs(conn, user_id, settings, seed_filters=True)
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
        return refused(request, ALREADY_SET_UP)
    except db.IntegrityError:
        # The unique index on the bootstrap flag caught a simultaneous request
        # that got past the count check. Same answer either way.
        return refused(request, ALREADY_SET_UP)

    if settings.trakt_access_token.strip() and not trakt_identity:
        await db.set_meta(TRAKT_RECONNECT_NOTICE, "1")
    elif trakt_identity:
        await db.set_meta(TRAKT_RECONNECT_NOTICE, "")

    # BEFORE the session below, so the very first cookie this instance issues
    # already carries the right policy.
    settings = await _adopt_cookie_policy(settings, request)
    return await _signed_in(request, user_id, True, settings, auth.client_ip(request, settings))


# Said the same way whether the count check or the unique index caught it.
ALREADY_SET_UP = "This instance has already been set up."


class _AlreadySetUp(Exception):
    """Raised inside the setup transaction to roll it back."""


class _UsernameTaken(Exception):
    """Raised inside the registration transaction to roll it back."""


class _InvalidInvite(Exception):
    """Raised inside the registration transaction to roll it back."""


async def _adopt_cookie_policy(settings, request: Request):
    """Pin the session-cookie Secure policy to what this browser actually used,
    and persist it. Returns the settings to issue the first cookie with.

    `cookie_secure` ships as "always", which is correct for every HTTPS
    deployment and wrong for a plain-HTTP one — there the browser silently
    discards the cookie, so sign-in reports success and the very next request is
    anonymous. Startup cannot tell those apart, because behind a TLS-terminating
    proxy this app is served over plain HTTP either way.

    Onboarding can. It is the one moment a real browser talks to an instance that
    has no policy yet, and the request carries the browser's own scheme in its
    Origin header (see auth.browser_scheme for why that beats X-Forwarded-Proto
    here). So the answer is resolved from evidence rather than asked as a
    question the operator may not know applies to them. It stays editable in
    data/settings.json afterwards, and /login says so if the two ever diverge.
    """
    detected = auth.detect_cookie_secure(request)
    if detected == (settings.cookie_secure or "").strip().lower():
        return settings
    settings.cookie_secure = detected
    await save_settings(settings)
    logger.info(
        "Setup: session cookies set to %r — this browser reached the instance over %s. "
        "Change `cookie_secure` in data/settings.json if that is wrong.",
        detected, auth.browser_scheme(request) or "an unreported scheme",
    )
    return settings


async def _fetch_trakt_identity(settings) -> dict | None:
    """Look up the Trakt account UUID for the token already in settings.json.

    settings.json holds a token but no account handle, and an identity row must
    be keyed on the immutable UUID rather than a username or slug. Returns
    None on any failure at all — an expired token, a revoked app registration, no
    network, or a response with no account UUID in it. Setup then creates
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


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def _public_base(settings) -> str:
    return (settings.public_base_url or "").rstrip("/")


def _og_context(settings, path: str) -> dict:
    """Open Graph tags for link unfurlers (Discord/Slack/etc.), built only from
    the configured public_base_url — never the request Host — since an
    unauthenticated crawler resolves relative paths unreliably and a spoofed
    Host header must not become the advertised origin. Absent a configured base
    there's nothing safe to advertise, so both tags come back empty and the
    templates fall back to a bare text preview.

    THE STATIC BANNER HERE IS NOT AN OVERSIGHT. A shared calendar's preview is a
    generated picture of the month it points at (app/calendar/share_routes.py); a
    registration form has no calendar behind it to render, so it advertises the
    app's own banner. Two different facts that happen to name one file today.

    THE DIMENSIONS TRAVEL WITH THE PICTURE, which they did not before. The og()
    macro emits og:image:width/height only when a route supplies them, and without
    them Slack and Discord size a card by guessing — a `summary_large_image` claim
    with no dimensions renders as a thumbnail often enough to be worth closing.
    The card is drawn at exactly the 1200x630 those unfurlers want, so the numbers
    are stated rather than left to be worked out."""
    base = _public_base(settings)
    if not base:
        return {"og_url": None, "og_image": None}
    return {
        "og_url": f"{base}{path}",
        "og_image": f"{base}/static/images/distrakkl-social-card-1200x630.png",
        "og_image_w": 1200,
        "og_image_h": 630,
    }


@guard.get("/register", AuthLevel.PUBLIC)
async def register_page(request: Request):
    """The registration form, or the "invalid invite" page when one is required
    and missing/unusable. Reachable only once an instance has been set up — an
    invite implies an admin already exists to have issued it, so this route is
    not part of the first-run path the way /login and /onboarding are."""
    settings = await load_settings()
    token = (request.query_params.get("invite") or "").strip()
    if not settings.allow_open_registration:
        invite = await auth.find_invite_by_token(token) if token else None
        if not auth.invite_is_usable(invite):
            return templates.TemplateResponse(request, "auth_invite_invalid.html", {
                "request": request,
                **_og_context(settings, "/register"),
            })
    return templates.TemplateResponse(request, "auth_register.html", {
        "request": request,
        "invite_token": token,
        # The invite rides along to the provider flow in the handshake row, not
        # in a cookie or the redirect URL, so registering that way is gated
        # exactly as tightly as registering with a password.
        "trakt_login_configured": settings.trakt_login_configured,
        "simkl_login_configured": settings.simkl_login_configured,
        **_og_context(settings, f"/register?invite={quote(token)}" if token else "/register"),
    })


@guard.post("/register", AuthLevel.PUBLIC)
async def register(request: Request):
    """Create an account, gated by an invite unless the operator turned that
    off (settings.allow_open_registration).

    Every rejection before the account exists — a bad invite, a taken
    username, a validation failure — is recorded as a failed attempt against
    this IP, the same as a login failure, so a script trying tokens or
    usernames in a loop eventually gets throttled. A taken username is still
    revealed in the response — usernames are already public (they appear in
    share URLs and Discord posts), so confirming one is registered leaks
    nothing new, and the alternative (a generic error) would just make normal
    "oops, taken" collisions confusing to fix. An unusable invite never is —
    invite tokens are secret, so every cause of a bad one looks identical from
    here.
    """
    settings = await load_settings()
    ip = auth.client_ip(request, settings)
    data = await authz.json_body(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    confirm = str(data.get("password_confirm") or "")
    token = str(data.get("invite") or request.query_params.get("invite") or "").strip()
    invite_required = not settings.allow_open_registration

    if await auth.registration_rate_limited(ip, token):
        return refused(request, "Too many attempts from this address. Try again later.")

    async def _fail(message: str) -> Response:
        await auth.record_registration_attempt(ip, token, False)
        return refused(request, message)

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
        # The invite's own grant, OR the instance-wide "approve new accounts
        # automatically" setting — the same rule auth.login_with_provider_identity
        # applies when it registers, so both doors behave alike.
        grants_calendar = (
            bool(row["grants_calendar_on_accept"]) if row else False
        ) or settings.auto_approve_calendar
        grants_ranker = bool(row["grants_ranker_on_accept"]) if row else False
        user_id = auth.insert_user(
            conn, username=username_lower, password_hash=password_hash,
            calendar_approved=grants_calendar, distrakt_approved=False,
            ranker_approved=grants_ranker, timezone=settings.timezone or None,
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

    await auth.record_registration_attempt(ip, token, True)
    return await _signed_in(request, user_id, calendar_approved, settings, ip)


# ---------------------------------------------------------------------------
# sign in / sign out
# ---------------------------------------------------------------------------

async def _settings_even_if_sealed() -> tuple[Settings, bool]:
    """Settings for a page that has to render while the at-rest key may be wrong,
    and whether it is. `open_secrets=False` on the second attempt, so the fields
    that come from app_secrets read as unset rather than raising.

    THE ONE PAGE THAT MUST NOT 500 ON A KEY MISMATCH IS THIS ONE. Every other route
    is steered to the recovery screen by authz.key_mismatch_gate, and /login is
    deliberately exempt so an administrator whose session has lapsed can get back in
    and fix it — but the recovery screen sends a signed-out viewer here, so a /login
    that cannot render is a closed loop with no way into the instance at all.
    Loading secrets is the only part of the page that can fail that way: the sign-in
    form itself needs the globals and the recovery fields and never a credential.

    Not conditioned on encryption_flow.health(), which is what the gate asks. That
    would be a second copy of the condition, in a route, that has to stay in step
    with the middleware's — and the direct question here is simply whether the
    secrets could be opened.
    """
    try:
        return await load_settings(), False
    except secrets_box.SealedButWrongKey:
        # Deliberately not logged: the gate already reports this state at startup
        # and the page is about to say so in the one place a person will see it.
        return await load_settings(open_secrets=False), True


@guard.get("/login", AuthLevel.PUBLIC)
async def login_page(request: Request):
    if not await auth.any_users_exist():
        return RedirectResponse("/onboarding", status_code=303)
    settings, key_mismatch = await _settings_even_if_sealed()
    relying_party = passkeys.relying_party(settings)
    response = templates.TemplateResponse(request, "auth_login.html", {
        "request": request,
        # Sign in with Trakt cannot work while a secret is sealed under the wrong
        # key — the code exchange authenticates with the client secret, which is one
        # of the values that will not open — so the button is off for the same
        # reason it is off when the credential was never set. Password sign-in is
        # the way in, and it is the way to the recovery screen.
        "key_mismatch": key_mismatch,
        "trakt_login_configured": settings.trakt_login_configured,
        # Same story one provider along: the Simkl code exchange authenticates
        # with a client secret that will not open under the wrong key, so the
        # button is off exactly when Trakt's is and for the same reason.
        "simkl_login_configured": settings.simkl_login_configured,
        # PASSKEYS ARE OFFERED where the public base URL is one a browser makes
        # them for (passkeys.relying_party), and the page LEADS with one where this
        # browser last signed in to an account holding one (the sign-in hint,
        # app/auth/cookies.py) — ignored where none is offered, so a changed setting
        # never leaves a page leading with a control that is not there.
        "passkeys_offered": relying_party is not None,
        "passkey_origin": relying_party.origin if relying_party else "",
        "lead_with_passkey": (relying_party is not None
                              and auth.leads_with_passkey(request, settings)),
        **_og_context(settings, "/login"),
    })
    # The page asks, once it has loaded, whether this cookie came back — see
    # login_cookie_check below.
    auth.set_cookie_probe(response, settings, request)
    return response


@guard.get("/login/cookie-check", AuthLevel.PUBLIC)
async def login_cookie_check(request: Request):
    """Whether the cookie a successful sign-in issues will survive in this browser.

    If the policy makes it `Secure` and the browser is on plain http://, the cookie
    is dropped on arrival and sign-in silently does nothing — which otherwise looks
    exactly like a wrong password. Onboarding resolves the policy correctly on its
    own, so a warning here means the deployment moved or settings.json was edited
    by hand.

    ASKED BY THE PAGE, NOT DRAWN WITH IT, because the answer is what the browser
    did with the cookie the page's own response offered, and only a request made
    after that response can carry it back (app/auth/cookies.py says why this beats
    reading the protocol in the browser). Reachable while the at-rest key is wrong,
    like /login itself: an administrator locked out of recovery by a cookie the
    browser refuses is exactly who needs this sentence."""
    settings = await load_settings(open_secrets=False)
    return templates.TemplateResponse(request, "_cookie_warning.html", {
        "request": request,
        "dropped": auth.session_cookie_would_be_dropped(request, settings),
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

    Failed attempts are counted per username AND per IP independently, so one
    attacker can't lock out every account by spraying one IP, and one victim
    IP can't be used to spray many usernames without tripping its own limit
    first.

    A LOCKED-OUT ATTEMPT IS NOT COUNTED. It still burns a full dummy verify, so
    it costs the same and reveals nothing, but recording it would let a retry
    loop keep pushing fresh failures into the trailing window and hold the
    lockout open forever — which matters most for the per-IP counter, where
    every user behind one reverse proxy shares a key.
    """
    data = await authz.json_body(request)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    # Sign-in reads only the cookie policy and the proxy list, never a credential,
    # so it loads without decrypting secrets — an admin must be able to sign in and
    # reach recovery even while a stored secret is sealed under the wrong key.
    settings = await load_settings(open_secrets=False)
    ip = auth.client_ip(request, settings)
    username_key = username.lower()

    locked = None
    if username_key and await auth.check_lockout(
        "username", username_key,
        max_attempts=auth.LOGIN_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS,
    ):
        locked = "username"
    elif await auth.check_lockout(
        "ip", ip,
        max_attempts=auth.LOGIN_IP_MAX_ATTEMPTS, window_seconds=auth.LOGIN_WINDOW_SECONDS,
    ):
        locked = "address"

    if locked:
        # Logged because the response deliberately cannot say this. Without a line
        # here a lockout is indistinguishable from a wrong password to the
        # operator too, which turns a working rate limiter into an unexplained
        # "login is broken" — the reason this log line exists.
        logger.warning(
            "Sign-in refused: this %s is temporarily locked out after %d failed "
            "attempts in %d minutes. It clears itself; attempts made while locked "
            "are not counted.",
            locked, auth.LOGIN_MAX_ATTEMPTS if locked == "username" else auth.LOGIN_IP_MAX_ATTEMPTS,
            auth.LOGIN_WINDOW_SECONDS // 60,
        )
        await auth.burn_dummy_verify(password)
        return refused(request, INVALID_CREDENTIALS)

    user = await auth.find_user_by_username(username) if username else None
    if user is None:
        await auth.burn_dummy_verify(password)
        if username_key:
            await auth.record_attempt("username", username_key, False)
        await auth.record_attempt("ip", ip, False)
        return refused(request, INVALID_CREDENTIALS)

    result = await auth.verify_password(user["password_hash"], password)
    if not result.ok or user["is_disabled"]:
        await auth.record_attempt("username", username_key, False)
        await auth.record_attempt("ip", ip, False)
        return refused(request, INVALID_CREDENTIALS)
    if result.new_hash:
        # The hashing library's defaults have moved on since this hash was
        # written; upgrade it now that we have the plaintext to do it with.
        await auth.update_password_hash(int(user["id"]), result.new_hash)

    # Both counters, not just the username's. The per-IP key is shared by every
    # user behind a reverse proxy, so leaving other people's failures on it after
    # somebody has just proved they own an account means one more mistake from
    # anyone locks out the whole instance.
    await auth.clear_attempts("username", username_key)
    await auth.clear_attempts("ip", ip)
    return await _signed_in(request, int(user["id"]), bool(user["calendar_approved"]),
                            settings, ip)


@guard.post("/logout", AuthLevel.PUBLIC)
async def logout(request: Request):
    """End the session and send the browser to the sign-in page.

    THE SESSION ROW IS WHAT IS INVALIDATED, not the cookie: `revoke_session` is a
    hard delete, so a copy of the cookie kept anywhere else stops working at the
    same moment (app/auth/sessions.py). Clearing the cookie is the tidy-up, not
    the mechanism.

    ANSWERED WITH `HX-Redirect` RATHER THAN A REDIRECT STATUS. The control is a
    button that posts itself, and htmx FOLLOWS a 303 and swaps what it finds — so
    a redirect status would put the sign-in page inside the menu instead of
    navigating to it. The header says "go here", which is the one thing a page
    being signed out of actually wants. 204 because there is nothing to swap.
    """
    # Signing someone out is a state change, so it is held to the same JSON-only
    # rule as every other mutating endpoint. The body itself is ignored.
    await authz.json_body(request)
    settings = await load_settings(open_secrets=False)
    session_id = auth.read_session_cookie(request, settings)
    if session_id:
        await auth.revoke_session(session_id)
    response = Response(status_code=204, headers={"HX-Redirect": "/login"})
    auth.clear_session_cookie(response, settings, request)
    return response


@guard.get("/me", AuthLevel.SESSION)
async def me_page(request: Request):
    """The account page: approval state, timezone, linked identities, and sign
    out. Doubles as the awaiting-approval page — a user without
    calendar_approved lands here from /login and stays here whenever they try
    to reach a gated route."""
    user = await auth.require_session(request)
    return templates.TemplateResponse(
        request, "auth_me.html", await _account_context(request, user.session_id))


# ---------------------------------------------------------------------------
# the account page's sections
# ---------------------------------------------------------------------------
# EVERY CONTROL ON THE ACCOUNT PAGE POSTS ITSELF AND IS ANSWERED WITH ITS SECTION,
# drawn from the same context the page is. The page used to be a 309-line script
# over a dozen JSON routes: each answer either reloaded the page or patched a few
# elements by hand — a heading here, a badge's class there, arrow buttons rebuilt
# with insertAdjacentHTML — which is a second statement of what the page draws,
# and one that went stale whenever the template moved on.
#
# ONE CONTEXT, BUILT ONCE PER ANSWER. A section is a slice of the page, so it reads
# the page's own dictionary rather than a narrower one of its own: a section and
# the page it sits in cannot disagree about, say, whether unlinking is allowed.
# Built AFTER the write, from a fresh reading of the session, so what an answer
# draws is what is stored.
#
# A REFUSAL IS A SENTENCE IN THE SECTION, at 200. A non-200 is never swapped, so a
# statused refusal would be a press that did nothing.

ACCOUNT_SECTIONS = {
    "avatar": "_account_avatar.html",
    "names": "_account_names.html",
    "identities": "_account_identities.html",
    "metadata_order": "_account_order.html",
    "sharing": "_account_sharing.html",
    "passkeys": "_account_passkeys.html",
    "credentials": "_account_credentials.html",
}


async def _account_context(request: Request, session_id: str, **notes) -> dict:
    """Everything the account page draws, for the page and for every section.

    `notes` are the sentences an action leaves in its section — `<section>_message`
    and, where a success is worth saying, `<section>_ok`."""
    user = await auth.validate_session(session_id)
    if user is None:
        # The session this answer was for has just been ended — a password change
        # replaces it, and the caller passes the new one. Anything else reaching
        # here is a session revoked mid-request, which is a sign-in problem.
        raise auth.AuthError(401, "login_required", "Sign in to continue.")
    settings = await load_settings()
    identities = await auth.list_identities(user.user_id)
    linked = {row["provider"]: row["display_name"] for row in identities}
    # WHICH LINKED SERVICES NEED RECONNECTING, drawn fresh each time this page
    # is asked for rather than stored as a flag — a row's true state is
    # whatever access_token_for_user answers right now. Simkl is the one
    # source this applies to today: a legacy v1-shaped token is never sent
    # (this app moved to Simkl's AUTH V2 in one cutover and keeps no V1
    # registration to send it with) and reads as "reconnect needed" exactly like a
    # refresh that failed (simkl_routes.access_token_for_user), and both cases
    # are indistinguishable from here, on purpose — see the template's wording.
    # NEUTRAL WORDING, DELIBERATELY: a 401 can also mean the person themselves
    # revoked this app on simkl.com, so this never claims a specific cause it
    # has not verified, only that reconnecting is needed.
    from . import simkl_routes
    needs_reconnect = set()
    if "simkl" in linked and await simkl_routes.access_token_for_user(user.user_id) is None:
        needs_reconnect.add("simkl")
    account = await auth.get_user(user.user_id)
    orders = await _account_orders(user.user_id, settings)
    prefs = await auth.get_user_prefs(user.user_id)
    pictures = await user_images.account_pictures(user.user_id)
    ways = await auth.account_ways_in(user.user_id)
    relying_party = passkeys.relying_party(settings)
    held = await passkeys.for_account(user.user_id)
    zone = clock.first_zone(user.timezone, settings.timezone)
    return {
        "request": request,
        # is_admin, calendar_available, ranker_available, version, build
        # for the shared header.
        **chrome.page_context(user),
        # This page gates the tracker's menu item server-side rather than leaving
        # it to CSS — it must not mention the tracker at all to an account without
        # the grant. Same two conditions the tracker's own access level enforces.
        "distrakt_available": bool(user and user.distrakt_approved and user.has_tracker_identity),
        "user": user,
        "linked": linked,
        "needs_reconnect": needs_reconnect,
        # WHETHER THIS ACCOUNT'S SHARE LINKS PUBLISH ITS WATCH HISTORY. It is on
        # this page rather than in the Share panel because it is an account
        # decision and not a per-link view option: switching it off has to reach
        # links that were handed out months ago, which is exactly what somebody
        # wants when they change their mind about publishing it at all.
        "share_watch_history": bool(prefs["share_watch_history"]),
        # The saved-picture library, drawn with the page rather than fetched by it
        # — the same context every one of its own actions answers with.
        "images": await user_images.describe_images(user.user_id),
        "cap": user_images.MAX_IMAGES_PER_USER,
        "name_cap": user_images.MAX_IMAGE_NAME,
        # THE SERVICES THAT COULD SUPPLY A WATCH HISTORY AND ARE NOT LINKED, by
        # their own labels, for the notice that tells somebody with the tracker
        # grant why it has nothing to read. Asked of the registry rather than
        # written out, because more than one source can answer now and a sentence
        # naming Trakt alone became wrong the moment a second one could: it told a
        # viewer signed in with the other service to go and link a service they had
        # deliberately not linked. An empty list means somebody is already linked
        # and the notice does not appear at all.
        # IN DECLARED ORDER, not alphabetical: `registered()` iterates the Source
        # declaration, which is the app's one statement of which service comes
        # first, and the sentence should not disagree with the rows below it.
        "tracker_services_missing": [
            provider.label for source, provider in providers.registered().items()
            if str(source) in providers.tracker_sources() and str(source) not in linked],
        "orders": {"metadata_order": orders.metadata},
        "trakt_login_configured": settings.trakt_login_configured,
        "simkl_login_configured": settings.simkl_login_configured,
        "tmdb_link_configured": settings.tmdb_link_configured,
        # WHICH LINKS AN UNLINK IS OFFERED FOR. Without a password an account's
        # links it can sign in with are its only way in, so the last of those may
        # not be removed — showing a button that always refuses would be worse
        # than showing none. A link nobody signs in with (TMDB's) is never a way
        # in, so it can always go, and never counts as the one left
        # (app/auth/revocation.py, identities.unlink_identity).
        "can_unlink": _unlinkable(identities, ways),
        # Drives whether the password form asks for a current password, and
        # whether the "a password is your way back in" nudge is shown.
        "has_password": bool(account and account["password_hash"]),
        "min_password_length": auth.MIN_PASSWORD_LENGTH,
        # PASSKEYS: offered only where the public base URL is one a browser makes
        # passkeys for (passkeys.relying_party), and drawn for that origin alone —
        # the page cannot know the address it was reached by, so static/js/
        # passkey.js reveals the controls where it is that origin. Remove is drawn
        # for a passkey whose removal leaves a way in, by the answer the removal
        # itself refuses by (identities.WaysIn).
        "passkey_origin": relying_party.origin if relying_party else "",
        "passkeys": held,
        "passkey_removable": frozenset(key.id for key in held
                                       if not ways.leaves_none(passkey=key.id)),
        "passkey_suggested": passkeys.suggested_name(request.headers.get("user-agent")),
        "passkey_most": passkeys.MAX_PER_ACCOUNT,
        "passkey_name_max": passkeys.NAME_MAX,
        "passkey_day": lambda timestamp: _day(timestamp, zone),
        "display_name_max": auth.DISPLAY_NAME_MAX,
        "has_avatar": pictures.has_avatar,
        # THE PICTURE'S OWN VERSION, SO A RELOAD CANNOT SHOW THE OLD ONE. These
        # images are served `private, max-age=86400`, which is right for a file
        # that rarely changes and wrong for the moment it does: the URL is
        # identical either side of a change, so a browser that has one cached
        # goes on drawing it for a day. Carrying the file's mtime in the template
        # fixes it for every path at once — upload, adopt, refresh — rather than
        # each call site remembering.
        "avatar_version": pictures.avatar_version,
        # WHICH SERVICES HAVE GIVEN US A PICTURE, and which one (if any) the
        # account is currently wearing. The second is derived by comparing bytes
        # rather than stored, for the reason user_images.adopt_avatar_source
        # gives: the avatar is a COPY, so there is no "primary provider" field
        # and the honest answer is whether the two files match.
        "avatar_sources": [
            {"source": picture.source,
             "label": ("Uploaded" if picture.source == user_images.UPLOAD_SOURCE
                       else picture.source.title()),
             # Refresh means "ask them again", which only a service can answer.
             "refreshable": picture.source != user_images.UPLOAD_SOURCE,
             "adopted": picture.adopted,
             "version": picture.version}
            for picture in pictures.sources
        ],
        # WHICH SERVICES COULD BE ASKED, as opposed to which have already
        # answered. An account linked before pictures were ever fetched has a
        # connection and no slot, and without this it would have no way to get
        # one but to disconnect and reconnect — which is a destructive action
        # standing in for "fetch that, please".
        "avatar_fetchable": [
            name for name in user_images.PROVIDER_SLOTS if name in linked
        ],
        **notes,
    }


def _day(timestamp, zone) -> str:
    """A stored moment as the account page says a day: "Nov 10, 2026"."""
    moment = clock.moment(timestamp, zone)
    return f"{moment:%b} {moment.day}, {moment.year}" if moment else ""


def _unlinkable(identities, ways: auth.WaysIn) -> frozenset[str]:
    """The linked services the account page offers Unlink for: every link whose
    removal leaves the account a way in — `WaysIn.leaves_none`, the very answer
    identities.unlink_identity refuses by."""
    return frozenset(row["provider"] for row in identities
                     if not ways.leaves_none(link=row["provider"]))


@dataclasses.dataclass(frozen=True)
class _Orders:
    """The "whose description is read" list the page offers, as it shows it."""
    prefs: object
    metadata: list[dict]


async def _account_orders(user_id: int, settings: Settings) -> _Orders:
    """Whose description of a title is read, in the order it is currently asked in.

    WHICH LINKED TRACKER DECIDES, WHOSE STORED NUMBERS STILL COUNT AND WHERE A
    SEASON'S EPISODE TOTAL IS ASKED OF ARE NOT HERE: they are about the tracker and
    sit in the tracker's own settings dialog (app/distrakt/tracker_settings.py).
    This page keeps what is about the account.

    WHOSE DESCRIPTION IS READ: every source that can describe titles for this
    instance, linked or not. A calendar is read with the instance's own credentials
    or with none at all, so every source this instance can fill a calendar from
    describes titles for every account — asked through the registry's own function,
    so an operator switching one off empties this control by the same rule that
    empties the calendar. Empty when fewer than two can describe, which is what
    hides the control: there is no order to state when one service answers.

    ORDERED THROUGH THE FUNCTION THAT ACTS ON IT (prefs.source_order) rather than
    by reading the stored list here. A screen with its own reading of a preference
    could disagree with the code acting on it, and a viewer would have no way to
    tell which was lying. The same list is what a move reorders
    (move_account_order), so a press moves what the viewer saw.
    """
    source_prefs = await _source_prefs().load(user_id)
    # AND EVERY SOURCE THAT DESCRIBES TITLES WITHOUT PUBLISHING A CALENDAR, when
    # this instance can ask it (TMDB): its answers are the pictures and the
    # details a viewer reads, so an order that left it out would be an order the
    # viewer could not put it in. A calendar source switched off by the operator
    # still drops out, by the calendar's own rule above.
    calendars = {str(p.source) for p in providers.calendar_sources(settings=settings)}
    describing = [str(source) for source, provider in providers.registered().items()
                  if str(source) in calendars
                  or (not provider.capabilities.endpoints and provider.detail_port is not None
                      and provider.detail_port.catalogue_configured(settings))]

    def entries(names: list[str]) -> list[dict]:
        return [{"source": name, "label": providers.registered()[name].label} for name in names]

    return _Orders(
        prefs=source_prefs,
        metadata=entries(source_prefs.source_order(describing)) if len(describing) > 1 else [],
    )


async def _section(request: Request, session_id: str, section: str, **notes) -> Response:
    """One section of the account page, drawn from the page's own context."""
    context = await _account_context(request, session_id, **notes)
    context["section"] = section
    return templates.TemplateResponse(request, ACCOUNT_SECTIONS[section], context)


async def _card(request: Request, session_id: str, **notes) -> Response:
    """The whole account card, for an action that changes what several sections
    offer — unlinking a service reaches the order, the tracker notice and whether
    unlinking is still allowed at all."""
    return templates.TemplateResponse(
        request, "_account_card.html", await _account_context(request, session_id, **notes))


@guard.post("/me/identities/unlink", AuthLevel.SESSION)
async def unlink_identity(request: Request):
    """Detach a linked provider account from the signed-in account.

    Refused when it would leave the account with no way to sign in at all —
    there is no self-service recovery from that, and an administrator has to
    undo it by hand.

    Answered with the whole card: see _card for why one section is not enough.
    """
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    provider = str(data.get("provider") or "").strip().lower()
    if not revocation.is_linkable(provider):
        return await _card(request, user.session_id, link_message="Unknown provider.")

    # Read before the unlink (the token lives on the row it deletes), spent only
    # after one actually happened — an unlink that gets refused below must not
    # leave the account linked to a token this app just killed. Which services
    # revoke at all, and how, is app/auth/revocation.py's table.
    stored_token = await revocation.stored_token(provider, user.user_id)
    try:
        removed = await auth.unlink_identity(user.user_id, provider)
    except auth.LastLoginMethod:
        return await _card(request, user.session_id, link_message=(
            "That's the only way you can sign in. Link another account first, or "
            "ask an administrator."))
    if not removed:
        return await _card(request, user.session_id, link_message="That account isn't linked.")
    # The unlink succeeded either way; a warning is about the part only the user
    # can finish, on the provider's own site, so it is said beside the rows.
    warning = await revocation.revoke(provider, stored_token)
    return await _card(request, user.session_id, link_message=warning or "",
                       link_ok=not warning)


@guard.post("/me/order/{kind}", AuthLevel.SESSION)
async def move_account_order(kind: str, request: Request):
    """Move one service a place up or down in the "whose description is read" list,
    and answer with that list.

    A MOVE, NOT THE WHOLE LIST FROM THE BROWSER. The page used to reorder its own
    DOM and post back what it read out of it; the server now takes the list it
    drew (_account_orders), moves the named service, and saves the result — so the
    order a press starts from is the stored one, not whatever another tab's page
    was left showing.

    THE ORDER IS STORED WHOLE rather than as "promote this one", because that is
    what it means: with a third service registered, "Simkl first" says nothing
    about the other two. `source_prefs.save` is what refuses a name this app has
    never heard of and one named twice.

    A REORDERING AND NEVER A FILTER: whether a service appears on this account's
    calendar at all is stated in the calendar's own filters panel. The tracker's
    own lists are moved from the tracker's settings dialog, not here.
    """
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    if kind != "metadata_order":
        return await _section(request, user.session_id, "metadata_order",
                              order_message="There is no such list.", order_kind="metadata_order")
    orders = await _account_orders(user.user_id, await load_settings())
    names = [entry["source"] for entry in orders.metadata]
    source = str(data.get("source") or "")
    step = {"up": -1, "down": 1}.get(str(data.get("direction") or ""))
    if source not in names or step is None or not 0 <= names.index(source) + step < len(names):
        return await _section(request, user.session_id, kind, order_kind=kind,
                              order_message="That service cannot move that way.")
    at = names.index(source)
    names[at], names[at + step] = names[at + step], names[at]
    try:
        await _source_prefs().save(dataclasses.replace(orders.prefs, metadata_order=names))
    except ValueError as exc:
        return await _section(request, user.session_id, kind, order_kind=kind,
                              order_message=str(exc))
    return await _section(request, user.session_id, kind, order_kind=kind,
                          order_moved=f"{source}-{data.get('direction')}")


@guard.post("/me/share-watch-history", AuthLevel.SESSION)
async def set_share_watch_history(request: Request):
    """Turn the watch marks on this account's shared calendars on or off.

    ACCOUNT-WIDE AND RETROACTIVE, which is the whole reason it is not beside the
    other share options. Every one of those is a view option a link may override
    and a visitor's query string may name; this one decides whether a viewing
    history is published at all, and an answer a URL could change would not be an
    answer. Stored on `user_prefs` (migration 42), read by the share page on the
    request it is already reading the owner's filters on, so turning it off takes
    effect on every link that account has ever handed out, immediately.

    NO PER-LINK OVERRIDE IS OFFERED and that is deliberate rather than unfinished:
    two switches would mean a link that still published the history after the
    account said not to, which is the failure this exists to make impossible.

    ONLY THE BOX'S OWN TWO SPELLINGS ARE ACCEPTED — its hidden "0" and its "1" —
    so a value that merely happens to read as truthy cannot decide whether a
    viewing history is published. SAID ON SUCCESS AS WELL, because this writes
    something the viewer cannot see the effect of from here.
    """
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    value = route_params.last_value(data.get("enabled"))
    if value not in ("0", "1"):
        return await _section(request, user.session_id, "sharing",
                              sharing_message="That has to be on or off.")
    enabled = value == "1"
    await auth.update_user_prefs(user.user_id, share_watch_history=enabled)
    return await _section(request, user.session_id, "sharing", sharing_ok=True, sharing_message=(
        "Shared calendars will show your watched episodes." if enabled
        else "Shared calendars will no longer show your watched episodes."))


@guard.post("/me/username", AuthLevel.SESSION)
async def set_own_username(request: Request):
    """Claim a username for an account that has none.

    An account created through Plex or Trakt has no username and no password, so
    without this it depends on an administrator for both. Claiming is one-way:
    CHANGING an existing username is deliberately not offered here, because a
    username is a public identifier — it is what /u/<name> share links are built
    from — and handing it over silently would break every link already shared and
    free the old name for somebody else to claim. That path stays with an
    administrator, who has the retired-identifier machinery to do it safely.

    Answered with the credentials section, and the heading and the names beside it
    out of band — the heading falls back to a username, and the names row shows it.
    """
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    account = await auth.get_user(user.user_id)
    if account and account["username"]:
        return await _section(request, user.session_id, "credentials", credentials_message=(
            "You already have a username. An administrator can change it."))
    username = str(data.get("username") or "").strip().lower()
    if err := await auth.username_availability_error(username):
        return await _section(request, user.session_id, "credentials", credentials_message=err)
    try:
        await auth.admin_set_username(user.user_id, username)
    except db.IntegrityError:
        # Lost a race between the availability check and this write; the UNIQUE
        # column is the real arbiter, exactly as registration treats it.
        return await _section(request, user.session_id, "credentials",
                              credentials_message="That username is taken.")
    return await _section(request, user.session_id, "credentials", names_changed=True,
                          credentials_ok=True, credentials_message="Username saved.")


@guard.post("/me/display-name", AuthLevel.SESSION)
async def set_own_display_name(request: Request):
    """Set or clear the signed-in account's display name.

    Freely changeable, unlike the username above — and that difference is the
    reason the two are separate columns. A username is a public identifier that
    /u/<name> share links are built from, so changing one breaks links and frees
    a name for somebody else to claim; a display name is a label nothing keys
    off, so changing it costs nothing and needs no administrator.

    Sending an empty value clears it, and the account goes back to being shown
    by its username. The answer is drawn from what was STORED, whitespace
    collapsed, so what is on screen is what was actually saved.
    """
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    raw = data.get("display_name")
    if err := auth.display_name_error(raw):
        return await _section(request, user.session_id, "names", names_message=err)
    await auth.set_display_name(user.user_id, raw)
    return await _section(request, user.session_id, "names", names_changed=True)


async def _current_password_refusal(ip: str, stored_hash: str, given) -> str | None:
    """Why `given` does not prove the account's current password, or None when
    it does — for every control that asks for it before acting.

    The check is a guess-checking oracle, so it is throttled on the same shared
    per-address counter a sign-in is — at the same tolerant threshold, since this
    is reached by someone who is ALREADY signed in and locking them out over a
    neighbour's typos is pure harm. A wrong guess counts; the caller clears the
    counter once it has acted."""
    if await auth.check_lockout("ip", ip, max_attempts=auth.LOGIN_IP_MAX_ATTEMPTS,
                                window_seconds=auth.LOGIN_WINDOW_SECONDS):
        return "Too many attempts. Try again later."
    if not (await auth.verify_password(stored_hash, str(given or ""))).ok:
        await auth.record_attempt("ip", ip, False)
        return "That isn't your current password."
    return None


@guard.post("/me/password", AuthLevel.SESSION)
async def set_own_password(request: Request):
    """Set or change the signed-in account's password.

    Changing an existing password requires the current one. Setting a FIRST
    password does not, because there is nothing to prove — the live session is
    the only credential such an account has, and requiring one would make the
    feature unreachable for exactly the accounts that need it.

    Setting a password revokes every session the account holds, so a
    password-change after a compromise actually evicts the other party. The
    caller is then re-issued a fresh session, since signing the person out of the
    tab they just used would read as a failure — and the section is drawn from
    that new session, the only one left.

    PASSKEYS OUTLIVE IT. Setting a password revokes sessions, not passkeys, and a
    password change is what somebody does after a compromise — so where the
    account holds any, the sentence says they still sign in and to remove one it
    does not recognise. A FIRST password also redraws the passkeys section out of
    band: adding one asks for the password, so its button comes on now.
    """
    user = await auth.require_session(request)
    settings = await load_settings()
    ip = auth.client_ip(request, settings)
    data = await authz.json_body(request)
    account = await auth.get_user(user.user_id)
    stored_hash = account["password_hash"] if account else None
    if stored_hash:
        refusal = await _current_password_refusal(ip, stored_hash, data.get("current_password"))
        if refusal:
            return await _section(request, user.session_id, "credentials",
                                  credentials_message=refusal)
    elif await auth.check_lockout("ip", ip, max_attempts=auth.LOGIN_IP_MAX_ATTEMPTS,
                                  window_seconds=auth.LOGIN_WINDOW_SECONDS):
        return await _section(request, user.session_id, "credentials",
                              credentials_message="Too many attempts. Try again later.")

    password = str(data.get("password") or "")
    if password != str(data.get("password_confirm") or ""):
        return await _section(request, user.session_id, "credentials",
                              credentials_message="The two passwords don't match.")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return await _section(request, user.session_id, "credentials", credentials_message=(
            f"Use at least {auth.MIN_PASSWORD_LENGTH} characters."))

    await auth.clear_attempts("ip", ip)
    await auth.set_password(user.user_id, password)
    session_id = await auth.create_session(
        user.user_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=ip,
    )
    holds_passkeys = await passkeys.holds_any(user.user_id)
    saved = "Password saved. Every other session on this account has been signed out."
    if holds_passkeys:
        saved += (" Your passkeys still sign you in — remove any you don't recognise "
                  "under Passkeys.")
    response = await _section(request, session_id, "credentials", credentials_ok=True,
                              credentials_message=saved, passkeys_oob=not stored_hash)
    auth.set_session_cookie(response, session_id, settings, request,
                            leads_with_passkey=holds_passkeys)
    return response


# ---------------------------------------------------------------------------
# passkeys
# ---------------------------------------------------------------------------
# A PASSKEY IS ONE MORE WAY INTO AN ACCOUNT THAT ALREADY EXISTS (app/auth/
# passkeys.py says what one is and where its challenge lives). Both ceremonies
# are a handshake made BEFORE the press, so the press is the gesture the browser's
# prompt needs: a browser may refuse to open it outside the click that asked for
# it, and a click does not survive a round trip — so signing in, the button
# arrives from a POST the page makes once it has loaded (never from the GET of
# the page, so a visitor without script mints nothing), and adding, "Add a
# passkey" is answered with a "Create passkey" button carrying the options.
# static/js/passkey.js calls the browser's credential API and posts what it
# answered; every sentence is drawn here.

def _passkey_sentence(error: str) -> str:
    """What the page says when the BROWSER did not use a passkey — the person
    closed the prompt, it timed out, or the device refused. Its error's name is
    all passkey.js sends; nothing about the account is in it."""
    if error == "InvalidStateError":
        return "This device already holds a passkey for this account."
    return "No passkey was used. Try again."


async def _passkey_button(request: Request, settings: Settings,
                          relying_party: passkeys.RelyingParty, *, lead: bool,
                          message: str = "") -> Response:
    """The "Sign in with a passkey" button, on a fresh handshake pinned to this
    browser, with the sentence an attempt before it ended in."""
    state = await passkeys.start("login")
    response = templates.TemplateResponse(request, "_passkey_signin.html", {
        "request": request, "options": passkeys.sign_in_options(relying_party, state),
        "origin": relying_party.origin, "lead": lead, "message": message})
    passkeys.set_attempt_cookie(response, state, settings, request)
    return response


@guard.post("/login/passkey/offer", AuthLevel.PUBLIC)
async def offer_passkey_sign_in(request: Request):
    """The sign-in page's passkey button, asked for once the page has loaded.

    THROTTLED LIKE EVERY SIGN-IN START (`handshake_start_limited`, one budget per
    address across every service), because each answer is a handshake row. When
    throttled no button is drawn — and a page that leads with a passkey opens its
    password fold instead, with a sentence, since there is nothing to lead with.

    Loads settings without opening secrets, as /login does: nothing here needs
    one, and it must work while the at-rest key is wrong."""
    await authz.json_body(request)
    settings = await load_settings(open_secrets=False)
    relying_party = passkeys.relying_party(settings)
    if relying_party is None:
        return Response(status_code=204)
    lead = auth.leads_with_passkey(request, settings)
    if await auth.handshake_start_limited(request, settings):
        return templates.TemplateResponse(request, "_passkey_signin.html", {
            "request": request, "throttled": True, "lead": lead})
    return await _passkey_button(request, settings, relying_party, lead=lead)


@guard.post("/login/passkey", AuthLevel.PUBLIC)
async def sign_in_with_passkey(request: Request):
    """Sign in with the passkey the browser answered with.

    EVERY REFUSAL IS ONE SENTENCE (passkeys.REFUSED) — an unknown or removed
    passkey, another account's, a bad signature, a disabled account and a locked
    out address alike — and answers with a fresh button, so a refusal is never an
    oracle for whether a passkey is known here. Failures count on the per-address
    sign-in counter; a locked-out address is refused WITHOUT being counted, for
    the reason `login` gives; success clears it. A press the browser itself
    refused is not a failure and is not counted.

    Then the end every sign-in shares (`_signed_in`)."""
    data = await authz.json_body(request)
    settings = await load_settings(open_secrets=False)
    relying_party = passkeys.relying_party(settings)
    if relying_party is None:
        return refused(request, passkeys.REFUSED)
    lead = auth.leads_with_passkey(request, settings)
    ip = auth.client_ip(request, settings)
    sentence = passkeys.REFUSED + (" You can use your password below." if lead else "")

    if await auth.check_lockout("ip", ip, max_attempts=auth.LOGIN_IP_MAX_ATTEMPTS,
                                window_seconds=auth.LOGIN_WINDOW_SECONDS):
        logger.warning("Passkey sign-in refused: this address is temporarily locked out "
                       "after failed sign-in attempts. Attempts made while locked are not "
                       "counted.")
        return await _passkey_button(request, settings, relying_party, lead=lead,
                                     message=sentence)
    error = str(data.get("error") or "")
    try:
        state = await passkeys.consume(request, settings, purpose="login")
    except passkeys.Refused:
        if error:
            return await _passkey_button(request, settings, relying_party, lead=lead,
                                         message=_passkey_sentence(error))
        await auth.record_attempt("ip", ip, False)
        return await _passkey_button(request, settings, relying_party, lead=lead,
                                     message=sentence)
    if error:
        return await _passkey_button(request, settings, relying_party, lead=lead,
                                     message=_passkey_sentence(error))
    try:
        user_id = await passkeys.finish_sign_in(relying_party, state,
                                                str(data.get("credential") or ""))
    except passkeys.Refused:
        await auth.record_attempt("ip", ip, False)
        return await _passkey_button(request, settings, relying_party, lead=lead,
                                     message=sentence)
    user = await auth.get_user(user_id)
    await auth.clear_attempts("ip", ip)
    response = await _signed_in(request, user_id, bool(user["calendar_approved"]),
                                settings, ip)
    passkeys.clear_attempt_cookie(response, settings, request)
    return response


@guard.post("/me/passkeys/begin", AuthLevel.SESSION)
async def begin_adding_passkey(request: Request):
    """"Add a passkey": the account's current password and a name, answered with
    the "Create passkey" button that carries the options.

    THE PASSWORD COMES FIRST, because a stolen session could otherwise add the
    thief's passkey — and it would outlive the password change meant to evict
    them, since setting a password revokes sessions and not passkeys. It is
    checked exactly as /me/password checks it (`_current_password_refusal`), and
    only a password that verifies mints the handshake, so the challenge itself
    is the proof and the finish asks nothing more. An account with no password
    is drawn the control disabled with a line saying so; a post from it anyway
    is refused here."""
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    settings = await load_settings()
    relying_party = passkeys.relying_party(settings)
    if relying_party is None:
        return await _section(request, user.session_id, "passkeys", passkeys_message=(
            "Passkeys need an https public base URL."))
    account = await auth.get_user(user.user_id)
    if not (account and account["password_hash"]):
        return await _section(request, user.session_id, "passkeys", passkeys_message=(
            "Set a password first — adding a passkey asks for it."))
    ip = auth.client_ip(request, settings)
    refusal = await _current_password_refusal(ip, account["password_hash"],
                                              data.get("current_password"))
    if refusal:
        return await _section(request, user.session_id, "passkeys", passkeys_message=refusal,
                              passkey_name=passkeys.clean_name(data.get("name")))
    await auth.clear_attempts("ip", ip)
    if len(await passkeys.for_account(user.user_id)) >= passkeys.MAX_PER_ACCOUNT:
        return await _section(request, user.session_id, "passkeys", passkeys_message=(
            f"An account can hold at most {passkeys.MAX_PER_ACCOUNT} passkeys. "
            "Remove one first."))
    return await _passkey_create(request, settings, relying_party, user,
                                 passkeys.clean_name(data.get("name")))


async def _passkey_create(request: Request, settings: Settings,
                          relying_party: passkeys.RelyingParty, user, name: str,
                          message: str = "") -> Response:
    """The passkeys section with the "Create passkey" button, on a fresh link
    handshake bound to this session and pinned to this browser."""
    state = await passkeys.start("link", session_id=user.session_id)
    options = await passkeys.creation_options(relying_party, state,
                                              await auth.get_user(user.user_id))
    response = await _section(request, user.session_id, "passkeys", passkey_create=options,
                              passkey_name=name, passkeys_message=message)
    passkeys.set_attempt_cookie(response, state, settings, request)
    return response


@guard.post("/me/passkeys", AuthLevel.SESSION)
async def add_passkey(request: Request):
    """Store the passkey the browser made, against the challenge "Add a passkey"
    minted.

    A handshake this session did not start, or one already used or expired, is
    a refusal back to the first step: the password has to be proven again. One
    that was this session's own but where the browser made nothing — the prompt
    closed — answers with a fresh "Create passkey" button, since the password was
    proven for it. Adding one sets this browser's sign-in hint: its sign-in page
    now leads with a passkey."""
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    settings = await load_settings()
    relying_party = passkeys.relying_party(settings)
    name = passkeys.clean_name(data.get("name"))
    if relying_party is None:
        return await _section(request, user.session_id, "passkeys", passkeys_message=(
            "Passkeys need an https public base URL."))
    try:
        state = await passkeys.consume(request, settings, purpose="link",
                                       session_id=user.session_id)
    except passkeys.Refused:
        return await _section(request, user.session_id, "passkeys", passkey_name=name,
                              passkeys_message="That took too long. Start again.")
    if error := str(data.get("error") or ""):
        return await _passkey_create(request, settings, relying_party, user, name,
                                     message=_passkey_sentence(error))
    try:
        await passkeys.finish_adding(relying_party, state, user.user_id,
                                     str(data.get("credential") or ""), name)
    except passkeys.Refused as exc:
        return await _section(request, user.session_id, "passkeys", passkey_name=name,
                              passkeys_message=str(exc))
    response = await _section(request, user.session_id, "passkeys", passkeys_ok=True,
                              passkeys_message=f"Passkey “{name}” added.")
    passkeys.clear_attempt_cookie(response, settings, request)
    auth.set_sign_in_hint(response, True, settings, request)
    return response


def _passkey_id(data: dict) -> int | None:
    try:
        return int(str(data.get("passkey") or ""))
    except ValueError:
        return None


@guard.post("/me/passkeys/rename", AuthLevel.SESSION)
async def rename_passkey(request: Request):
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    passkey_id = _passkey_id(data)
    if passkey_id is None or not await passkeys.rename(user.user_id, passkey_id,
                                                       data.get("name")):
        return await _section(request, user.session_id, "passkeys",
                              passkeys_message="That passkey isn't on this account.")
    return await _section(request, user.session_id, "passkeys", passkeys_ok=True,
                          passkeys_message="Passkey renamed.")


@guard.post("/me/passkeys/remove", AuthLevel.SESSION)
async def remove_passkey(request: Request):
    """Remove one of the account's passkeys — refused when it is the account's
    last way in (identities.WaysIn), which the page never offers. Removing the
    account's LAST passkey clears this browser's sign-in hint, so its sign-in
    page goes back to leading with the password."""
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    passkey_id = _passkey_id(data)
    try:
        removed = passkey_id is not None and await passkeys.remove(user.user_id, passkey_id)
    except auth.LastLoginMethod:
        return await _section(request, user.session_id, "passkeys", passkeys_message=(
            "That's the only way you can sign in. Set a password or link another account "
            "first."))
    if not removed:
        return await _section(request, user.session_id, "passkeys",
                              passkeys_message="That passkey isn't on this account.")
    response = await _section(request, user.session_id, "passkeys", passkeys_ok=True,
                              passkeys_message="Passkey removed. It may still be listed on "
                                               "your device; it no longer signs in here.")
    if not await passkeys.holds_any(user.user_id):
        auth.set_sign_in_hint(response, False, await load_settings(), request)
    return response


# ---------------------------------------------------------------------------
# avatar & saved images
# ---------------------------------------------------------------------------
# `private`: these images are per-account, so a shared cache in front of the
# app has no business holding a copy.
_PRIVATE_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}


@guard.post("/me/avatar", AuthLevel.SESSION)
async def upload_avatar(request: Request):
    """Replace this account's avatar with an uploaded picture.

    The file arrives as a base64 field of a JSON body, read by static/js/ui.js's
    `data-file-into` — the same reader the saved-picture library and both restores
    use, because the shape guard admits application/json alone."""
    user = await auth.require_session(request)
    # An image is the one body whose ceiling this route knows, so it says so
    # here: the refusal then reads as the image rule at every size, rather than
    # switching to the app-wide guard's wording once the file grew past it.
    data = await authz.json_body(request, user_images.MAX_UPLOAD_BODY_BYTES,
                                 user_images.TOO_LARGE_MESSAGE)
    try:
        await user_images.save_avatar(user.user_id, str(data.get("image_b64") or ""))
    except user_images.ValidationError as exc:
        return await _section(request, user.session_id, "avatar", avatar_message=str(exc))
    return await _section(request, user.session_id, "avatar")


@guard.post("/me/avatar/remove", AuthLevel.SESSION)
async def remove_avatar(request: Request):
    user = await auth.require_session(request)
    await authz.json_body(request)
    await user_images.delete_avatar(user.user_id)
    return await _section(request, user.session_id, "avatar")


@guard.get("/api/me/avatar", AuthLevel.SESSION)
async def get_avatar(request: Request):
    """The signed-in account's own avatar. `size` resizes the 512x512 master on
    the fly — see user_images.py for why that beats caching a second file per
    size."""
    user = await auth.require_session(request)
    path = user_images.avatar_path(user.user_id)
    if not path.exists():
        return Response(status_code=404)
    size_param = request.query_params.get("size")
    if size_param is None:
        return FileResponse(path, media_type="image/webp", headers=_PRIVATE_CACHE_HEADERS)
    try:
        size = int(size_param)
    except ValueError:
        return authz.error("`size` must be a whole number.")
    if not 16 <= size <= user_images.MASTER_SIZE:
        return authz.error(f"`size` must be between 16 and {user_images.MASTER_SIZE}.")
    resized = await user_images.resized(path, size)
    if resized is None:
        return Response(status_code=404)
    return Response(content=resized, media_type="image/webp", headers=_PRIVATE_CACHE_HEADERS)


# ---------------------------------------------------------------------------
# the provider picture palette
# ---------------------------------------------------------------------------
# A slot is one connected service's copy of that account's picture. The routes
# below never take a provider name into a path themselves —
# user_images.provider_avatar_path refuses anything outside its closed set, and
# these check membership first so the refusal is a sentence rather than a 500.


def _known_source(value: object) -> str | None:
    """A name from a request, or None. Membership of the closed set is what keeps
    it from becoming a path component."""
    name = str(value or "").strip().lower()
    return name if name in user_images.AVATAR_SOURCES else None


def _known_slot(value: object) -> str | None:
    """A CONNECTED SERVICE only. Refresh is the one action that means "ask them
    again", and the account's own upload has nobody to ask."""
    name = str(value or "").strip().lower()
    return name if name in user_images.PROVIDER_SLOTS else None


@guard.get("/api/me/avatar/source/{source}", AuthLevel.SESSION)
async def get_avatar_source(request: Request, source: str):
    """One source's picture — the upload, or a service's — for the picker."""
    user = await auth.require_session(request)
    name = _known_source(source)
    if name is None:
        return Response(status_code=404)
    path = user_images.avatar_source_path(user.user_id, name)
    if not path.exists():
        return Response(status_code=404)
    size_param = request.query_params.get("size")
    if size_param is None:
        return FileResponse(path, media_type="image/webp", headers=_PRIVATE_CACHE_HEADERS)
    try:
        size = int(size_param)
    except ValueError:
        return authz.error("`size` must be a whole number.")
    if not 16 <= size <= user_images.MASTER_SIZE:
        return authz.error(f"`size` must be between 16 and {user_images.MASTER_SIZE}.")
    resized = await user_images.resized(path, size)
    if resized is None:
        return Response(status_code=404)
    return Response(content=resized, media_type="image/webp", headers=_PRIVATE_CACHE_HEADERS)


@guard.post("/me/avatar/use", AuthLevel.SESSION)
async def use_avatar_source(request: Request):
    """Make one source's picture this account's avatar.

    `only_if_missing=False`: this is a person choosing, and a choice overrides
    whatever is there. That is the whole difference from the automatic seeding
    at sign-in, which never overwrites — see provider_login.seed_provider_avatar.
    """
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    name = _known_source(data.get("source"))
    if name is None:
        return await _section(request, user.session_id, "avatar", avatar_message="Unknown picture.")
    if not await user_images.adopt_avatar_source(user.user_id, name, only_if_missing=False):
        return await _section(request, user.session_id, "avatar",
                              avatar_message="There is no saved picture of that kind.")
    return await _section(request, user.session_id, "avatar")


@guard.post("/me/avatar/refresh", AuthLevel.SESSION)
async def refresh_provider_avatar(request: Request):
    """Ask the service for its current picture again and refill the slot.

    THE ONLY WAY A SLOT IS EVER RE-FETCHED, which is what makes "no refetch on an
    ordinary sign-in" affordable rather than a limitation: a picture that almost
    never changes is not worth an outbound fetch on every login, and a person who
    has just changed theirs has somewhere to press. The identities list's "Get
    picture" is this same request, pressed beside a service rather than a picture.

    THE AVATAR IS NOT TOUCHED unless it was already a copy of this slot. Somebody
    refreshing a service's picture means "update that picture", not "make it my
    avatar" — but if they were already wearing it, leaving the old copy on would
    make the refresh look like it did nothing.
    """
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    name = _known_slot(data.get("source"))
    if name is None:
        return await _section(request, user.session_id, "avatar", avatar_message="Unknown service.")
    was_adopted = await user_images.avatar_source_is_adopted(user.user_id, name)
    url = await provider_avatars.current_url(user.user_id, name)
    raw = await provider_avatars.fetch(name, url)
    if raw is None:
        return await _section(request, user.session_id, "avatar",
                              avatar_message="That service did not give us a picture to use.")
    try:
        await user_images.save_provider_avatar(user.user_id, name, raw)
    except user_images.ValidationError as exc:
        return await _section(request, user.session_id, "avatar", avatar_message=str(exc))
    if was_adopted:
        await user_images.adopt_avatar_source(user.user_id, name, only_if_missing=False)
    return await _section(request, user.session_id, "avatar")


@guard.post("/me/avatar/forget", AuthLevel.SESSION)
async def remove_avatar_source(request: Request):
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    name = _known_source(data.get("source"))
    if name is None:
        return await _section(request, user.session_id, "avatar", avatar_message="Unknown picture.")
    await user_images.delete_avatar_source(user.user_id, name)
    return await _section(request, user.session_id, "avatar")


# ---------------------------------------------------------------------------
# the saved pictures a poster grid can carry at its top
# ---------------------------------------------------------------------------
# THE LIBRARY IS MARKUP THE SERVER DRAWS. It was a client renderer over a JSON
# list: a card built per image, a rename, a delete, an upload, and a re-fetch of
# the whole list after each — with the cap, the name length and the positional
# default name all restated in the browser. Every action posts itself now and is
# answered with the list, which is the unit that is always right (a cleared name
# falls back to a position, and both an upload and a delete change how many are
# left).
#
# TWO SURFACES SHOW THESE PICTURES and each draws its own markup: this list, and
# the rankings board's export picker, which chooses between them. What they share
# is the one place the bytes are validated and stored — user_images.add_image.

async def _image_library(request: Request, user_id: int, message: str = "") -> Response:
    """This account's saved pictures, as the account page draws them."""
    return templates.TemplateResponse(request, "_user_images.html", {
        "request": request,
        "images": await user_images.describe_images(user_id),
        "cap": user_images.MAX_IMAGES_PER_USER,
        "name_cap": user_images.MAX_IMAGE_NAME,
        "message": message,
    })


@guard.post("/me/images", AuthLevel.SESSION)
async def upload_saved_image(request: Request):
    """Add a saved image, capped at user_images.MAX_IMAGES_PER_USER per account."""
    user = await auth.require_session(request)
    # Same known ceiling as the avatar route above, for the same reason.
    data = await authz.json_body(request, user_images.MAX_UPLOAD_BODY_BYTES,
                                 user_images.TOO_LARGE_MESSAGE)
    message = ""
    try:
        await user_images.add_image(
            user.user_id, str(data.get("image_b64") or ""), str(data.get("name") or ""))
    except user_images.TooManyImages:
        message = (f"You can save up to {user_images.MAX_IMAGES_PER_USER} images. "
                   "Remove one first.")
    except user_images.ValidationError as exc:
        message = str(exc)
    return await _image_library(request, user.user_id, message)


@guard.post("/me/images/{image_uid}/name", AuthLevel.SESSION)
async def rename_saved_image(image_uid: str, request: Request):
    """Name or rename one saved image. An empty name clears it, and the image
    falls back to its positional default rather than being left blank."""
    user = await auth.require_session(request)
    data = await authz.json_body(request)
    message = ""
    try:
        name = user_images.clean_name(data.get("name"))
        if not await user_images.set_image_name(user.user_id, image_uid, name):
            message = "No such image."
    except user_images.ValidationError as exc:
        message = str(exc)
    return await _image_library(request, user.user_id, message)


@guard.post("/me/images/{image_uid}/delete", AuthLevel.SESSION)
async def remove_saved_image(image_uid: str, request: Request):
    user = await auth.require_session(request)
    await authz.json_body(request)
    message = "" if await user_images.delete_image(user.user_id, image_uid) else "No such image."
    return await _image_library(request, user.user_id, message)


@guard.get("/api/me/images/{image_uid}", AuthLevel.SESSION)
async def get_saved_image(image_uid: str, request: Request):
    """One saved image's bytes.

    THE UID IS CHECKED AGAINST WHAT THIS ACCOUNT ACTUALLY HAS before it becomes a
    path segment. It is server-issued at upload, but it arrives back here from a
    client, and a membership test answers both questions at once: that the shape
    is one we produced, and that this caller owns it.
    """
    user = await auth.require_session(request)
    if image_uid not in user_images.list_images(user.user_id):
        return Response(status_code=404)
    return FileResponse(user_images.image_path(user.user_id, image_uid),
                        media_type="image/webp", headers=_PRIVATE_CACHE_HEADERS)
