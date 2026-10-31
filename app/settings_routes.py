"""The admin Settings panel, and the Trakt app-wide token it manages.

Two halves that belong together because the second is what the first writes:

  /settings/fragments/panel  the panel itself, drawn with every value in it and
  /settings                  saved through config.apply_form, the one writer.
  /settings/arr-options      Sonarr/Radarr profiles and folders as <option>s.
  /settings/trakt/*          Trakt's OAuth device-code flow — the break-glass
                             path that seeds client_secret and refresh_token — a
                             manual refresh, and linking the token to an
                             administrator.

EVERY ANSWER HERE IS MARKUP. The panel used to be an empty form that a script
filled in from a JSON read, sent back as a JSON write, and decorated with hints
it composed itself; each of those was a second statement of something this
module or app/config.py already knew. The panel is now fetched every time it
opens — never drawn into a page — because each opening must re-read which
secrets are set, and every action answers with the part of it that changed.

A REFUSAL IS DRAWN AT 200. A non-200 is never swapped (static/js/boost.js's
`noSwap`), so a sentence answered with 400 would read as a press that did nothing.

The token renewal the heartbeat performs (maybe_refresh_trakt_token) lives here
too rather than in the bootstrap module, because it renews the token the device
flow issued. Trakt issues a NEW refresh_token on every refresh and the old one
stops working, so a renewal is stored through trakt_routes.store_rotated_tokens —
the app-wide copy and an administrator's linked identity can be one grant, and a
renewal written to only one of them leaves the other dead.

The per-user "Log in with Trakt" redirect flow is a different thing entirely and
lives in app/auth/trakt_routes.py; this module only ever touches the ONE app-wide
connection stored in settings.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import auth, authz, cache, config, db, route_params, secrets_box
from .auth import encryption_flow, encryption_routes
from .auth import routes as auth_routes, simkl as simkl_auth, trakt as trakt_auth, trakt_routes
from .auth import AuthLevel
from .calendar import resolve as calendar_resolve, vocab
from .config import SECRET_FIELDS, Settings, load_settings, save_settings
from .endpoints import endpoint_choices
from .integrations import arr, routes as integrations_routes
from .templating import templates
from .timezones import build_options as build_timezone_options

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

PANEL_TEMPLATE = "_settings_panel.html"
NOTICE_TEMPLATE = "_settings_notice.html"
ARR_TEMPLATE = "_settings_arr.html"
DEVICE_TEMPLATE = "_settings_device.html"
TOKEN_TEMPLATE = "_settings_token.html"
RECONNECT_TEMPLATE = "_settings_reconnect.html"

# The content floor's certification, show-type and missing-data rows are the
# Filters panel's chips and tick boxes, spelled and read back by
# app/calendar/vocab.py, so a floor spec is byte-identical to a spec a viewer's
# own filter would have written. Each Settings field carries the viewer's column
# name for exactly that reason (vocab.FLOOR_FIELDS).
FLOOR_CHIP_FIELDS = (vocab.SHOW_CERTIFICATIONS, vocab.MOVIE_CERTIFICATIONS,
                     vocab.TV_SHOW_TYPES, vocab.TV_MISSING, vocab.MOVIE_MISSING)

ARR_KINDS = ("sonarr", "radarr")

# One sentence for both places a credential write is refused while the at-rest key
# is unhealthy, because it is one refusal: encryption_flow.secret_writes_blocked.
_KEY_UNHEALTHY = (
    "Encryption is unhealthy, so saving a credential is blocked to avoid overwriting "
    "a value the correct key could still recover. Restore the original "
    f"{secrets_box.ENV_VAR}, or run the recovery reset first."
)


async def _apply_new_trakt_token(settings: Settings, token: dict) -> Settings:
    """Write the token pair a NEW authorization issued (the device-code flow)
    into `settings` and persist it. A REFRESH is not stored here: it replaces a
    refresh token that may be held in more than one place, so it goes through
    trakt_routes.store_rotated_tokens, which writes it everywhere it was."""
    settings.trakt_access_token = token["access_token"]
    settings.trakt_refresh_token = token.get("refresh_token", "")
    settings.trakt_token_expires_at = int(token.get("created_at", time.time())) + int(token.get("expires_in", 0))
    await save_settings(settings)
    return settings


async def maybe_refresh_trakt_token() -> None:
    """Refresh the Trakt access token once it has actually expired.

    Runs on every heartbeat tick (cheap — just a timestamp comparison until the
    token is actually due), so the token renews itself in the background
    without the user having to notice or intervene.
    """
    settings = await load_settings()
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
        # WHOSE TOKEN, AND TRAKT'S OWN REASON, so a failure says what to do: an
        # `invalid_grant` means the refresh token was retired and only "Authorize
        # with Trakt" in Settings brings it back.
        logger.warning("Trakt token auto-refresh failed for %s: %s",
                       await trakt_routes.holders_note(settings.trakt_refresh_token, app_wide=True),
                       trakt_routes.refusal_reason(exc))
        return
    # Stored wherever this grant is held — Settings AND the linked identity that
    # adopted it — or the copy not written keeps a refresh token Trakt retired.
    await trakt_routes.store_rotated_tokens(settings.trakt_refresh_token, token,
                                              identity_id=None)
    logger.info("Trakt token auto-refreshed (next expiry %s)", (await load_settings()).trakt_token_expires_at)


# ---------------------------------------------------------------------------
# what the panel says about the instance
# ---------------------------------------------------------------------------

def token_status(settings: Settings, now: float | None = None) -> str:
    """The sentence under the access token: when it runs out, or that it has.

    A DAY, IN UTC, spelled by the server. The browser used to compose this from the
    raw timestamp in its own locale, which made the one fact on the Trakt tab that
    the server holds the only one the server did not say.
    """
    expires = settings.trakt_token_expires_at
    if not expires:
        return ""
    when = datetime.fromtimestamp(expires, tz=timezone.utc)
    day = f"{when.day} {when:%B %Y}"
    if expires < (time.time() if now is None else now):
        return f"Token expired {day} — it refreshes automatically, or press “Refresh token now”."
    return f"Token valid until {day} (it refreshes automatically once it expires)."


async def _reconnect_notice(admin) -> bool:
    """Whether to ask this administrator to link the instance's Trakt token.

    DERIVED, not just read back: the stored flag records that first-run setup
    could not resolve the token, but what the notice actually asks for is a linked
    Trakt identity, and this caller either has one or does not. Trusting the flag
    alone left the prompt up after somebody linked by a route that forgot to clear
    it — a sticky "do this thing" that stayed after the thing was done.
    """
    return bool(await db.get_meta(auth_routes.TRAKT_RECONNECT_NOTICE, "")) and not (
        admin and admin.has_trakt_identity)


def _redirect_uri(module, settings: Settings) -> str:
    """The exact callback to register with a service, or "" before a base URL
    exists to build it from. Trakt and Simkl both compare it byte for byte, so
    the panel shows the value rather than describing how to build it."""
    return module.redirect_uri(settings.public_base_url) if settings.public_base_url else ""


def _server_facts(request: Request, settings: Settings) -> dict:
    """What the Server tab's hints are written from.

    `peer` is the IMMEDIATE peer — the reverse proxy on a real deployment — which
    is what `trusted_proxy_ips` has to cover, so the operator reads the answer off
    the screen instead of guessing their container network. Forwarded headers
    arriving from a peer that is NOT trusted is the misconfiguration worth naming:
    every user collapses onto one address for rate limiting and the session list.

    `browser_scheme` is the scheme the BROWSER is on, read from its own headers
    (auth.browser_scheme) — the same reading the save route refuses a self-locking
    cookie policy on, so the warning and the refusal cannot disagree. None when the
    browser did not say, and then no warning is drawn rather than a guessed one.
    """
    forwarded = any(h in request.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded"))
    return {
        "peer": (request.client.host if request.client else "") or "unknown",
        "forwarded_ignored": forwarded and not auth.peer_is_trusted_proxy(request, settings),
        "base_url_overridden": bool(config.public_base_url_override()),
        "browser_scheme": auth.browser_scheme(request),
    }


def _arr_choices(kind: str, settings: Settings, *, profiles: list[dict] | None = None,
                 folders: list[dict] | None = None, chosen_profile: object = None,
                 chosen_folder: object = None, message: str = "", ok: bool = False) -> dict:
    """One service's profile and folder <option>s, and the line under them.

    BEFORE ANYTHING IS LOADED the saved choice is the only option, so a panel that
    has not asked the service yet still shows — and saves — what is stored rather
    than a blank that would write a zero. AFTER, the service's own lists, with the
    choice the form was holding still selected where the service still offers it.
    """
    profile = str(chosen_profile if chosen_profile not in (None, "")
                  else getattr(settings, f"{kind}_quality_profile_id") or "")
    folder = str(chosen_folder if chosen_folder not in (None, "")
                 else getattr(settings, f"{kind}_root_folder") or "")
    if profiles is None:
        profiles = [{"id": profile, "name": f"Profile #{profile}"}] if profile not in ("", "0") else []
    if folders is None:
        folders = [{"path": folder}] if folder else []
    return {
        "kind": kind,
        "label": integrations_routes.LIBRARY_NAMES[kind],
        "profiles": [{"value": str(p["id"]), "label": p["name"], "selected": str(p["id"]) == profile}
                     for p in profiles],
        "folders": [{"value": f["path"], "label": f["path"], "selected": f["path"] == folder}
                    for f in folders],
        "message": message,
        "ok": ok,
    }


async def _trakt_context(settings: Settings, admin, *, linked: bool = False,
                         reconnect_error: str | None = None,
                         token_message: str = "", token_ok: bool = False) -> dict:
    """The Trakt tab's three live parts — the reconnect notice, the token line and
    the access token's own field — which every Trakt action may change at once."""
    return {
        "reconnect_shown": not linked and await _reconnect_notice(admin),
        "reconnect_error": reconnect_error,
        "token_status": token_status(settings),
        "token_message": token_message,
        "token_ok": token_ok,
        "secrets": config.secret_inputs(settings),
    }


async def _panel_context(request: Request) -> dict:
    settings = await load_settings()
    admin = await auth.current_user(request)
    return {
        "request": request,
        "settings": settings,
        "numbers": config.number_inputs(settings),
        "server": _server_facts(request, settings),
        "trakt_redirect_uri": _redirect_uri(trakt_auth, settings),
        "simkl_redirect_uri": _redirect_uri(simkl_auth, settings),
        **await _trakt_context(settings, admin),
        "pairing": await _pairing(admin.user_id),
        "device_message": "",
        "device_ok": False,
        "authorized": False,
        "encryption": await encryption_routes.panel_state(),
        "encryption_reveal": None,
        "encryption_error": None,
        "timezone_groups": build_timezone_options(settings.timezone),
        "endpoints": endpoint_choices(),
        "floor_chips": {field: vocab.chips_for(field, getattr(settings, field))
                        for field in FLOOR_CHIP_FIELDS},
        # A tick box sends nothing when unticked, so a missing-data row says it
        # was drawn with the presence marker the Filters panel uses.
        "floor_presence": {field: vocab.presence_name(field) for field in vocab.TICK_FIELDS},
        # The services the no-network rule means, by their registry labels.
        "sparse_labels": calendar_resolve.sparse_labels(),
        "arr": {kind: _arr_choices(kind, settings) for kind in ARR_KINDS},
    }


def _notice(request: Request, message: str, *, recovery: bool = False):
    """A refusal of the whole save, drawn beside the Save button."""
    return templates.TemplateResponse(request, NOTICE_TEMPLATE, {
        "request": request, "settings_notice": message,
        "recovery_url": encryption_routes.RECOVERY_PATH if recovery else None,
    })


@guard.get("/settings/fragments/panel", AuthLevel.ADMIN)
async def settings_panel(request: Request):
    """The whole Settings overlay, already open, with every value drawn in.

    NO CREDENTIAL IS EVER DRAWN — not masked, not its length. A credential field
    says whether one is stored and offers to clear it; what is typed replaces it
    and an empty field leaves it alone (config.apply_form)."""
    return templates.TemplateResponse(request, PANEL_TEMPLATE, await _panel_context(request))


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


@guard.post("/settings", AuthLevel.ADMIN)
async def save_settings_form(request: Request):
    """Save the Settings form, then reload the page it was opened from.

    THE TYPES ARE config.apply_form'S, and nothing here coerces a field. What this
    route adds is only what needs the REQUEST: the certification chips turned into
    the spec vocab.py spells, the cookie policy judged against the browser's own
    scheme, and the refusal to write a credential while the at-rest key is
    unhealthy — asked of what the save would actually CHANGE, so a form that
    touches no secret still saves its ordinary settings.

    A RELOAD, NOT A REDRAW: timezone, default endpoint and hide-not-watching change
    the page behind the panel, which is what reloading after a save was always for.
    """
    data = await authz.json_body(request)
    current = await load_settings()
    try:
        settings = config.apply_form(current, {**data, **vocab.specs_from_form(data)})
    except config.SettingsFormError as exc:
        return _notice(request, str(exc))
    changes_secret = any(getattr(settings, name) != getattr(current, name) for name in SECRET_FIELDS)
    if changes_secret and encryption_flow.secret_writes_blocked():
        return _notice(request, _KEY_UNHEALTHY, recovery=True)
    if "cookie_secure" in data and (error := _cookie_secure_error(settings, request)):
        return _notice(request, error)
    # The one save path allowed to remove a credential. apply_form has already
    # resolved what the administrator meant — an empty field keeps the stored
    # value, a ticked clear empties it — so a blank secret on this object is a
    # decision rather than a gap, which is what save_settings otherwise
    # (correctly) refuses to assume.
    await save_settings(settings, clear_unset_secrets=True)
    # Re-check Sonarr/Radarr/Seerr immediately so buttons reflect the new config
    # right away, and invalidate the library cache so the next fetch re-pulls with
    # the new credentials rather than serving the stale cache until it expires.
    await integrations_routes.refresh_integration_health()
    integrations_routes.invalidate_library_cache()
    # 204 rather than an empty 200: htmx acts on this header before it decides
    # anything about swapping — the same answer the rankings board gives with
    # HX-Location.
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@guard.post("/settings/arr-options", AuthLevel.ADMIN)
async def arr_options(request: Request):
    """One service's quality profiles and root folders, drawn as the <select>s.

    EACH HALF FALLS BACK ON ITS OWN. The form sends the URL as typed and the API
    key as typed — which is usually nothing, because a stored key is never drawn
    back — and a blank one is filled from what is saved. It used to fall back
    only when EITHER was blank, and then for both: a new URL typed beside a saved
    key was silently replaced by the old URL, and the options loaded were the old
    server's."""
    data = await authz.json_body(request)
    kind = route_params.last_value(data.get("kind"))
    if kind not in ARR_KINDS:
        return _notice(request, "Unknown service.")
    settings = await load_settings()
    saved_url, saved_key = arr.credentials(kind, settings)
    url = str(route_params.last_value(data.get(f"{kind}_url")) or "").strip() or saved_url
    key = str(route_params.last_value(data.get(f"{kind}_api_key")) or "").strip() or saved_key
    chosen = {"chosen_profile": route_params.last_value(data.get(f"{kind}_quality_profile_id")),
              "chosen_folder": route_params.last_value(data.get(f"{kind}_root_folder"))}
    label = integrations_routes.LIBRARY_NAMES[kind]
    if not (url and key):
        return _arr(request, _arr_choices(
            kind, settings, **chosen, message=f"Enter the {label} URL and API key first."))
    try:
        options = await arr.fetch_options(kind, url, key)
    # The four things an unreachable or wrong-service instance actually raises:
    # a transport failure, a body that is not JSON, and — when the URL turns out
    # to be some other web app answering 200 with JSON of its own — a profile
    # list whose entries have neither the keys nor the shape Sonarr's do.
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return _arr(request, _arr_choices(
            kind, settings, **chosen,
            message=f"Couldn't reach {label} at {url} — check the URL and API key."))
    return _arr(request, _arr_choices(
        kind, settings, profiles=options["profiles"], folders=options["folders"], **chosen,
        message=f"{label} options loaded.", ok=True))


def _arr(request: Request, choices: dict):
    return templates.TemplateResponse(request, ARR_TEMPLATE, {"request": request, "arr_service": choices})


# ---------------------------------------------------------------------------
# Trakt's device-code flow, held on the server
# ---------------------------------------------------------------------------
#
# THE PAGE HOLDS NOTHING. It used to keep the device code AND the client secret
# typed into the form in script memory and re-send both on every poll. The pairing
# now lives in the shared cache's key/value store (app/cache.py), one entry per
# administrator, and the panel's pairing block polls itself with nothing in its
# body: the route reads the pairing back. No migration, because a pairing is a
# ten-minute handshake rather than a record.
#
# IT EXPIRES WITH THE CODE. The entry is written with the code's own `expires_in`
# as its lifetime, and a read past `expires_at` deletes it and answers as though
# nothing were waiting. It is deleted outright the moment the flow ends any other
# way — approved, denied, cancelled — because the sweep's grace period would
# otherwise keep a finished handshake for months.
#
# ITS SECRETS ARE SEALED. The device code and a client secret typed for this flow
# are credentials, so they are stored through secrets_box.seal exactly as
# app_secrets are: `enc:v1:` ciphertext while encryption is on, and never read
# back into markup. A client secret left blank is not stored at all — the saved
# one is read at poll time.


def _pairing_key(user_id: int) -> str:
    return f"settings:trakt-device-pairing:{user_id}"


@dataclasses.dataclass(frozen=True)
class Pairing:
    """A device code waiting to be approved, as the routes use it (opened)."""
    user_code: str
    verification_url: str
    interval: int
    expires_at: int
    client_id: str
    device_code: str
    client_secret: str  # "" when the saved client secret is the one to use

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))


# Where a pairing code is entered when Trakt does not say. Also what is drawn in
# place of a verification address that is not https: it is rendered as a link an
# administrator presses, and a `javascript:` or plain-http address from an answer
# is not something this page should offer to open.
_TRAKT_ACTIVATE = "https://trakt.tv/activate"


async def _remember_pairing(user_id: int, code: dict, client_id: str, typed_secret: str) -> None:
    expires_in = max(int(code.get("expires_in") or 600), 1)
    verification_url = str(code.get("verification_url") or "")
    pairing = Pairing(
        user_code=str(code.get("user_code") or ""),
        verification_url=verification_url if verification_url.startswith("https://") else _TRAKT_ACTIVATE,
        # Never faster than Trakt asks, and never faster than five seconds.
        interval=max(int(code.get("interval") or 5), 5),
        expires_at=int(time.time()) + expires_in,
        client_id=client_id,
        device_code=str(code["device_code"]),
        client_secret=typed_secret,
    )
    stored = dataclasses.asdict(pairing)
    stored["device_code"] = secrets_box.seal(pairing.device_code)
    stored["client_secret"] = secrets_box.seal(typed_secret) if typed_secret else ""
    await cache.set(_pairing_key(user_id), stored, retain_seconds=expires_in)


async def _forget_pairing(user_id: int) -> None:
    await cache.delete(_pairing_key(user_id))


async def _pairing(user_id: int) -> Pairing | None:
    """The pairing waiting for this administrator, or None.

    Anything that cannot be a live pairing is forgotten on the way past: an
    expired code, an entry that no longer reads as one, and a sealed value the
    configured key cannot open — the flow simply has to be started again, which
    costs a code rather than a credential."""
    stored = await cache.get_stale(_pairing_key(user_id))
    if not isinstance(stored, dict):
        return None
    try:
        pairing = Pairing(**{
            **stored,
            "device_code": secrets_box.open_(stored["device_code"]) or "",
            "client_secret": secrets_box.open_(stored["client_secret"]) or "",
        })
    except (TypeError, KeyError, secrets_box.SealedButWrongKey):
        pairing = None
    if pairing is None or not pairing.device_code or pairing.seconds_left <= 0:
        await _forget_pairing(user_id)
        return None
    return pairing


async def _device(request: Request, admin, *, pairing: Pairing | None = None,
                  message: str = "", ok: bool = False, authorized: bool = False,
                  linked: bool = False, link_error: str | None = None):
    """The pairing block: waiting on a code (and polling itself), or not.

    AN AUTHORIZATION CHANGES THREE OTHER PARTS OF THE TAB, and they ride the same
    answer out of band: the token line, the access token's field (now stored) and
    the reconnect notice, which a successful link takes down."""
    settings = await load_settings()
    return templates.TemplateResponse(request, DEVICE_TEMPLATE, {
        "request": request,
        "pairing": pairing,
        "device_message": message,
        "device_ok": ok,
        "authorized": authorized,
        **await _trakt_context(settings, admin, linked=linked, reconnect_error=link_error),
    })


@guard.post("/settings/trakt/device/start", AuthLevel.ADMIN)
async def device_start(request: Request):
    """Ask Trakt for a pairing code and hold it server-side.

    Takes the client id and secret as typed into the panel, each falling back to
    the saved one, so an operator can authorize before saving. REFUSED WHILE THE
    AT-REST KEY IS UNHEALTHY: the token this flow ends by storing is a credential,
    and save_settings would silently leave it unwritten — an authorization that
    appears to work and stores nothing."""
    data = await authz.json_body(request)
    admin = await auth.current_user(request)
    settings = await load_settings()
    client_id = str(route_params.last_value(data.get("trakt_client_id")) or "").strip() \
        or settings.trakt_client_id
    typed_secret = str(route_params.last_value(data.get("trakt_client_secret")) or "").strip()
    if not client_id:
        return await _device(request, admin, message="Enter your Trakt Client ID first.")
    if not (typed_secret or settings.trakt_client_secret):
        return await _device(request, admin, message="Enter your Trakt Client Secret first.")
    if encryption_flow.secret_writes_blocked():
        return await _device(request, admin, message=_KEY_UNHEALTHY)
    try:
        code = await trakt_auth.request_device_code(client_id)
        await _remember_pairing(admin.user_id, code, client_id, typed_secret)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return await _device(request, admin, message=f"Could not start device authorization: {exc}")
    return await _device(request, admin, pairing=await _pairing(admin.user_id))


@guard.post("/settings/trakt/device/poll", AuthLevel.ADMIN)
async def device_poll(request: Request):
    """Ask Trakt whether the waiting code has been approved yet.

    THE ANSWER DECIDES WHETHER THE PAGE ASKS AGAIN. A pairing still waiting is
    drawn with its poller; every other answer is drawn without one, which is the
    whole of how polling stops. A transport failure keeps waiting — a hiccup is not
    a refusal — while anything Trakt actually answered with ends the flow."""
    admin = await auth.current_user(request)
    pairing = await _pairing(admin.user_id)
    if pairing is None:
        return await _device(request, admin, message=(
            "That code is no longer waiting — start again if you still need to authorize."))
    settings = await load_settings()
    client_secret = pairing.client_secret or settings.trakt_client_secret
    try:
        token = await trakt_auth.poll_device_token(pairing.client_id, client_secret, pairing.device_code)
    except (trakt_auth.DevicePending, trakt_auth.DeviceSlowDown):
        return await _device(request, admin, pairing=pairing)
    except (trakt_auth.DeviceExpired, trakt_auth.DeviceDenied) as exc:
        await _forget_pairing(admin.user_id)
        return await _device(request, admin, message=str(exc))
    except httpx.TransportError:
        return await _device(request, admin, pairing=pairing,
                             message="Trakt did not answer — still waiting.")
    except httpx.HTTPError as exc:
        await _forget_pairing(admin.user_id)
        return await _device(request, admin, message=f"Trakt error: {exc}")

    await _forget_pairing(admin.user_id)
    settings.trakt_client_id = pairing.client_id
    settings.trakt_client_secret = client_secret
    settings = await _apply_new_trakt_token(settings, token)
    await integrations_routes.refresh_integration_health()
    # The token is known-good right now, so this is the best moment there will
    # ever be to resolve it to an account and link it to the administrator who
    # just authorized it. Without this the app-wide token renews while
    # `linked_identities` stays empty — which leaves the "reconnect your Trakt
    # account" notice up with nothing able to clear it, and leaves the tracker
    # refusing this account for want of a linked identity.
    linked, link_error = await trakt_routes.adopt_app_token(admin.user_id, settings)
    if linked:
        message = "✅ Authorized, and linked to your account."
    elif link_error:
        # Said on the spot: an authorization that "worked" yet left the notice up
        # is the state that reads as the app ignoring what was just done.
        message = f"✅ Authorized, but not linked to your login: {link_error}"
    else:
        message = "✅ Authorized! The access token has been saved."
    return await _device(request, admin, message=message, ok=True, authorized=True,
                         linked=linked, link_error=link_error)


@guard.post("/settings/trakt/device/cancel", AuthLevel.ADMIN)
async def device_cancel(request: Request):
    """Abandon the waiting code. Nothing is asked of Trakt: the code simply stops
    being polled and expires on its own side."""
    admin = await auth.current_user(request)
    await _forget_pairing(admin.user_id)
    return await _device(request, admin, message="Authorization cancelled.")


@guard.post("/settings/trakt/adopt", AuthLevel.ADMIN)
async def trakt_adopt(request: Request):
    """Retry linking the saved app-wide Trakt token to the calling administrator.

    The reconnect notice asks for exactly this, and until it existed the only
    thing that performed it was a fresh device authorization — so an adoption that
    failed for a reason re-authorizing does not fix (the Trakt account already
    belonging to another login here) left the notice up no matter how many times
    the operator re-ran the flow. This is the same operation on its own, with the
    reason drawn where the notice is.
    """
    admin = await auth.current_user(request)
    linked, link_error = await trakt_routes.adopt_app_token(admin.user_id, await load_settings())
    return templates.TemplateResponse(request, RECONNECT_TEMPLATE, {
        "request": request,
        "reconnect_shown": not linked,
        "reconnect_error": None if linked else link_error,
    })


@guard.post("/settings/trakt/refresh", AuthLevel.ADMIN)
async def trakt_refresh(request: Request):
    """The manual "refresh now" button — uses whatever is already saved (the
    device-auth flow is what actually seeds client_secret/refresh_token)."""
    settings = await load_settings()
    if not (settings.trakt_client_id and settings.trakt_client_secret and settings.trakt_refresh_token):
        return _token(request, settings,
                      "Not authorized yet — use “Authorize with Trakt” first.", ok=False)
    try:
        token = await trakt_auth.refresh_access_token(
            settings.trakt_client_id, settings.trakt_client_secret, settings.trakt_refresh_token,
        )
    except (httpx.HTTPError, trakt_auth.TraktRateLimitError) as exc:
        return _token(request, settings,
                      f"Refresh failed ({trakt_routes.refusal_reason(exc)}) — use “Authorize "
                      "with Trakt” to get a fresh token.", ok=False)
    await trakt_routes.store_rotated_tokens(settings.trakt_refresh_token, token,
                                              identity_id=None)
    return _token(request, await load_settings(), "Trakt token refreshed.", ok=True)


def _token(request: Request, settings: Settings, message: str, *, ok: bool):
    return templates.TemplateResponse(request, TOKEN_TEMPLATE, {
        "request": request, "token_status": token_status(settings),
        "token_message": message, "token_ok": ok,
    })
