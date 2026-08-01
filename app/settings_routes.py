"""The admin Settings screen's API, and the Trakt app-wide token it manages.

Two halves that belong together because the second is what the first writes:

  /api/settings              read back the instance's configuration (never a
                             credential) and save a partial update to it.
  /api/auth/device/*         Trakt's OAuth device-code flow — the break-glass
  /api/auth/refresh          path that seeds client_secret and refresh_token,
  /api/auth/trakt/adopt      and links the resulting token to an administrator.

The token renewal the heartbeat performs (maybe_refresh_trakt_token) lives here
too rather than in the bootstrap module, because it writes exactly the same
fields the device flow does and for the same reason — Trakt issues a NEW
refresh_token on every refresh and the old one stops working, so there must be
one place that knows how to store a token pair.

The per-user "Log in with Trakt" redirect flow is a different thing entirely and
lives in app/auth/trakt_routes.py; this module only ever touches the ONE app-wide
connection stored in settings.json.
"""
from __future__ import annotations

import logging
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import auth, authz, db
from .auth import encryption_flow, encryption_routes
from .auth import routes as auth_routes, trakt as trakt_auth, trakt_routes
from .integrations import routes as integrations_routes
from .auth import AuthLevel
from .config import (
    SECRET_FIELDS,
    Settings,
    apply_update,
    load_settings,
    public_base_url_error,
    save_settings,
)

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)


async def _apply_new_trakt_token(settings: Settings, token: dict) -> Settings:
    """Write a fresh access/refresh token pair (from device-auth or a refresh
    call) into `settings` and persist it. Trakt issues a NEW refresh_token on
    every refresh — the old one stops working, so it must always be saved."""
    settings.trakt_access_token = token["access_token"]
    settings.trakt_refresh_token = token.get("refresh_token", "")
    settings.trakt_token_expires_at = int(token.get("created_at", time.time())) + int(token.get("expires_in", 0))
    save_settings(settings)
    return settings


async def maybe_refresh_trakt_token() -> None:
    """Refresh the Trakt access token once it has actually expired.

    Runs on every heartbeat tick (cheap — just a timestamp comparison until the
    token is actually due), so the token renews itself in the background
    without the user having to notice or intervene.
    """
    settings = load_settings()
    if not (settings.trakt_client_id and settings.trakt_client_secret and settings.trakt_refresh_token):
        return
    if not settings.trakt_token_expires_at or time.time() < settings.trakt_token_expires_at:
        return
    try:
        token = await trakt_auth.refresh_access_token(
            settings.trakt_client_id, settings.trakt_client_secret, settings.trakt_refresh_token,
        )
    except (httpx.HTTPError, trakt_auth.TraktRateLimitError) as exc:
        # A rate-limited refresh is not an httpx error; catch it here too so the
        # background renewal just skips this cycle and tries again next tick rather
        # than letting the exception escape the heartbeat.
        logger.warning("Trakt token auto-refresh failed: %s", exc)
        return
    await _apply_new_trakt_token(settings, token)
    logger.info("Trakt token auto-refreshed (next expiry %s)", settings.trakt_token_expires_at)


@guard.get("/api/settings", AuthLevel.ADMIN)
async def get_settings(request: Request):
    """Configuration for the Settings screen, WITHOUT any credential in it.

    Credentials are write-only over this API: the response carries a flag per
    secret saying whether one is stored, never the value. This route used to hand
    the Trakt access token, the Trakt client secret, the TMDB key, and every
    Sonarr/Radarr/Seerr API key to whoever asked for it.
    """
    settings = load_settings()
    peer = (request.client.host if request.client else "") or ""
    admin = await auth.current_user(request)
    return JSONResponse({
        **settings.redacted(),
        # What `trusted_proxy_ips` has to cover, shown beside that field so the
        # operator can read the answer off the screen instead of guessing their
        # container network. This is the IMMEDIATE peer — the reverse proxy on a
        # real deployment — not the forwarded client address.
        "detected_peer_ip": peer,
        # Whether forwarded headers are actually arriving AND being honored. The
        # two disagreeing is the misconfiguration worth surfacing: headers
        # present but the peer untrusted means every user is collapsed onto one
        # address for rate limiting and the session list.
        "forwarded_headers_present": any(
            h in request.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded")
        ),
        "peer_is_trusted_proxy": auth.peer_is_trusted_proxy(request, settings),
        # Raised at first-run setup when the Trakt token already in settings.json
        # could not be resolved to an account, so the Settings screen can prompt
        # the administrator to reconnect.
        #
        # DERIVED, not just read back: the stored flag records that setup failed,
        # but what the notice actually asks for is a linked Trakt identity, and
        # this caller either has one or does not. Trusting the flag alone left the
        # prompt up after somebody linked by a route that forgot to clear it —
        # a sticky "do this thing" that stayed after the thing was done.
        "trakt_reconnect_notice": bool(
            await db.get_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "")
        ) and not (admin and admin.has_trakt_identity),
        # Whether the per-user "Log in with Trakt" button can be offered at all.
        "trakt_login_configured": settings.trakt_login_configured,
        "trakt_redirect_uri": (
            trakt_auth.redirect_uri(settings.public_base_url)
            if settings.public_base_url else ""
        ),
    })


_COOKIE_SECURE_MODES = ("always", "auto", "never")


def _cookie_secure_error(settings, request: Request) -> str | None:
    """Reject a cookie_secure change that is invalid or self-locking.

    The lockout: "always" makes the session cookie Secure, and a browser on plain
    http:// silently discards a Secure cookie — so the operator's next request
    arrives with no session and they can't get back to this screen to undo it,
    which is exactly why this used to be hand-edited only.

    Judged on the BROWSER's scheme (Origin/Referer), never the request's own,
    because behind a TLS-terminating proxy the app sees http while the browser is
    on https — that is the case "always" exists for and must stay allowed. The
    browser scheme also does not depend on trusted_proxy_ips being right, so the
    guard holds on a fresh instance whose proxy list is still the default. A save
    from this screen always carries an Origin (mutating + same-origin), so a
    missing scheme here means we genuinely can't tell, and we allow it.
    """
    mode = (settings.cookie_secure or "").strip().lower()
    if mode not in _COOKIE_SECURE_MODES:
        return f"Session cookie security must be one of: {', '.join(_COOKIE_SECURE_MODES)}."
    settings.cookie_secure = mode  # normalize what gets saved
    if mode == "always" and auth.browser_scheme(request) == "http":
        return (
            "You're viewing this over http://, so a Secure session cookie would be "
            "discarded by your browser and lock you out. Serve this over https:// "
            "(a reverse proxy in front counts), or choose Auto or Never."
        )
    return None


@guard.post("/api/settings", AuthLevel.ADMIN)
async def post_settings(request: Request):
    """Save a partial settings update.

    A secret that is absent or blank keeps its stored value, and an explicit null
    clears it — see config.apply_update. That is what lets the Settings screen
    render its credential inputs empty (it cannot read them back) without the
    first save wiping every credential the instance has.
    """
    data = await authz.json_body(request)
    # A credential save while the key is missing or wrong would seal a fresh value
    # over ciphertext the original key could still recover. Refuse it loudly and send
    # the admin to recovery, rather than let it silently overwrite. A save that only
    # touches non-secret settings is fine — those are never sealed — so this checks
    # for a real secret change (a new value or an explicit clear), not a blank field.
    changes_secret = any(
        name in data and (data[name] is None or str(data[name]).strip())
        for name in SECRET_FIELDS
    )
    if changes_secret and encryption_flow.secret_writes_blocked():
        return JSONResponse({
            "ok": False,
            "reason": "key_unhealthy",
            "error": (
                "Encryption is unhealthy, so saving a credential is blocked to avoid "
                "overwriting a value the correct key could still recover. Restore the "
                "original ENCRYPTION_KEY, or run the recovery reset first."
            ),
            "recovery_url": encryption_routes.RECOVERY_PATH,
        }, status_code=409)
    settings = apply_update(load_settings(), data)
    # Rejected on save rather than on use: a base URL with a path or a trailing
    # slash builds a redirect URI that no longer matches the one registered on
    # the Trakt application, and Trakt compares the two exactly — so the failure
    # would otherwise surface much later as an unreadable error mid-sign-in.
    if err := public_base_url_error(settings.public_base_url):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if "cookie_secure" in data and (err := _cookie_secure_error(settings, request)):
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    save_settings(settings)
    # Re-check Sonarr/Radarr/Seerr immediately so buttons reflect the new config right away,
    # and invalidate the library cache so the next fetch re-pulls with the new credentials
    # (rather than serving the stale/empty cache until the TTL expires or a restart).
    await integrations_routes.refresh_integration_health()
    integrations_routes.invalidate_library_cache()
    return JSONResponse({"ok": True, "settings": settings.redacted()})


@guard.post("/api/auth/device/start", AuthLevel.ADMIN)
async def auth_device_start(request: Request):
    """Begin Trakt's OAuth device-code flow. Accepts an in-progress (unsaved)
    client_id from the Settings form, falling back to the saved one — same
    pattern as /api/integrations/options for Sonarr/Radarr."""
    data = await authz.json_body(request)
    settings = load_settings()
    client_id = (data.get("client_id") or "").strip() or settings.trakt_client_id
    if not client_id:
        return JSONResponse({"ok": False, "error": "Enter a Trakt Client ID first."}, status_code=400)
    try:
        code = await trakt_auth.request_device_code(client_id)
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "error": f"Could not start device authorization: {exc}"}, status_code=502)
    return JSONResponse({"ok": True, **code})


@guard.post("/api/auth/device/poll", AuthLevel.ADMIN)
async def auth_device_poll(request: Request):
    """Check whether the user has approved the device code yet. On success,
    persists client_id/client_secret + the new token pair to settings.json so
    the background auto-refresh (heartbeat) can pick it up without the user
    separately clicking "Save & reload" on the main Settings form."""
    data = await authz.json_body(request)
    settings = load_settings()
    client_id = (data.get("client_id") or "").strip() or settings.trakt_client_id
    client_secret = (data.get("client_secret") or "").strip() or settings.trakt_client_secret
    device_code = data.get("device_code")
    if not (client_id and client_secret and device_code):
        return JSONResponse({"ok": False, "error": "Missing client_id, client_secret, or device_code."}, status_code=400)
    try:
        token = await trakt_auth.poll_device_token(client_id, client_secret, device_code)
    except trakt_auth.DevicePending:
        return JSONResponse({"ok": True, "status": "pending"})
    except trakt_auth.DeviceSlowDown:
        return JSONResponse({"ok": True, "status": "slow_down"})
    except trakt_auth.DeviceExpired as exc:
        return JSONResponse({"ok": False, "status": "expired", "error": str(exc)}, status_code=410)
    except trakt_auth.DeviceDenied as exc:
        return JSONResponse({"ok": False, "status": "denied", "error": str(exc)}, status_code=409)
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "status": "error", "error": f"Trakt error: {exc}"}, status_code=502)

    settings.trakt_client_id = client_id
    settings.trakt_client_secret = client_secret
    settings = await _apply_new_trakt_token(settings, token)
    await integrations_routes.refresh_integration_health()
    # The token is known-good right now, so this is the best moment there will
    # ever be to resolve it to an account and link it to the administrator who
    # just authorized it. Without this the app-wide token renews while
    # `linked_identities` stays empty — which leaves the "reconnect your Trakt
    # account" notice up with nothing in the UI able to clear it, and leaves the
    # tracker refusing this account for want of a linked identity.
    admin = await auth.current_user(request)
    linked, link_error = await trakt_routes.adopt_app_token(admin.user_id, settings)
    # The token itself is not echoed back. It is already saved, so sending it to
    # the browser would put a Trakt bearer token in page memory for no purpose.
    return JSONResponse({
        "ok": True,
        "status": "authorized",
        "expires_at": settings.trakt_token_expires_at,
        # Lets the Settings screen take the reconnect notice down without a reload.
        "trakt_linked": linked,
        # And, when it can't, say so on the spot. A successful authorization that
        # silently failed to link is the exact state that leaves the reconnect
        # notice up looking like it ignored what was just done.
        "trakt_link_error": link_error,
    })


@guard.post("/api/auth/trakt/adopt", AuthLevel.ADMIN)
async def auth_trakt_adopt(request: Request):
    """Retry linking the saved app-wide Trakt token to the calling administrator.

    The reconnect notice asks for exactly this, and until now the only thing that
    performed it was a fresh device authorization — so an adoption that failed
    for a reason re-authorizing does not fix (the Trakt account already belonging
    to another login here) left the notice up no matter how many times the
    operator re-ran the flow. This is the same operation on its own, with the
    reason reported.
    """
    admin = await auth.current_user(request)
    linked, link_error = await trakt_routes.adopt_app_token(admin.user_id, load_settings())
    if not linked:
        return JSONResponse({"ok": False, "error": link_error}, status_code=409)
    return JSONResponse({"ok": True})


@guard.post("/api/auth/refresh", AuthLevel.ADMIN)
async def auth_refresh():
    """Manual "refresh now" button — uses whatever is already saved (the
    device-auth flow is what actually seeds client_secret/refresh_token)."""
    settings = load_settings()
    if not (settings.trakt_client_id and settings.trakt_client_secret and settings.trakt_refresh_token):
        return JSONResponse({"ok": False, "error": "Not authorized yet — use 'Authorize with Trakt' first."}, status_code=400)
    try:
        token = await trakt_auth.refresh_access_token(
            settings.trakt_client_id, settings.trakt_client_secret, settings.trakt_refresh_token,
        )
    except (httpx.HTTPError, trakt_auth.TraktRateLimitError) as exc:
        return JSONResponse({"ok": False, "error": f"Refresh failed: {exc}"}, status_code=502)
    settings = await _apply_new_trakt_token(settings, token)
    return JSONResponse({"ok": True, "expires_at": settings.trakt_token_expires_at})
