"""The HTTP surface for the at-rest-encryption lifecycle: the admin consent flow,
the later opt-in, and the lost-key recovery screen.

The decisions and the crypto live in app/encryption_flow; this module is the thin
routing over them, the same shape app/admin_routes is over app/auth. Two things here
are load-bearing and must survive any restyling:

  - The recovery page is deliberately PUBLIC and self-gating rather than declared
    ADMIN. When the key is wrong the whole app is gated to this one screen, and an
    ADMIN dependency here would bounce a signed-in non-admin to /me — which is also
    gated back to here — into a redirect loop. So it resolves the viewer itself:
    anonymous -> sign in, non-admin -> a plain 'ask an administrator' page, admin ->
    the two recovery doors. The destructive reset endpoint stays ADMIN, because it
    is a fetch() call that returns a status a loop cannot form around.

  - The reset is destructive and reuses the type-to-confirm pattern the admin
    account-delete uses: the caller must echo an exact phrase back, checked
    server-side, before anything is blanked.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import assets, auth, authz, db, encryption_flow, secrets_backfill, secrets_box
from .auth import AuthLevel

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

# Where the request gate sends a browser while the key is wrong, and the one screen
# it may reach. Kept here next to the routes that answer it.
RECOVERY_PATH = "/admin/encryption/recovery"

# The exact phrase the destructive reset requires, echoed back by the admin — the
# same deliberate-friction pattern as typing an account's name to delete it.
RESET_CONFIRM_PHRASE = "reset encrypted secrets"


def _error(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


async def _json_body(request: Request) -> dict:
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        raise HTTPException(status_code=415, detail="Send application/json.")
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed JSON body.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return data


async def _secrets_present() -> bool:
    """Whether there is anything to protect yet: a stored app secret or a per-user
    token. Encryption on an instance with neither would seal nothing, so the consent
    UI can say so instead of offering an empty gesture."""
    has_secret = await db.fetch_value("SELECT 1 FROM app_secrets LIMIT 1")
    has_token = await db.fetch_value(
        "SELECT 1 FROM linked_identities "
        "WHERE access_token IS NOT NULL OR refresh_token IS NOT NULL LIMIT 1"
    )
    return bool(has_secret or has_token)


async def _status_payload() -> dict:
    """The current lifecycle state for the Settings panel and the recovery screen.

    `key_present` vs `key_valid`: the environment can hold a value that is not a
    usable Fernet key, which the flow has to tell apart from no key at all so it can
    say 'the key you set is malformed' rather than 'set a key'."""
    raw_key = os.environ.get(secrets_box.ENV_VAR, "").strip()
    return {
        "phase": await encryption_flow.get_phase(),
        "health": encryption_flow.health(),
        "key_present": bool(raw_key),
        "key_valid": secrets_box.is_enabled(),
        "secrets_present": await _secrets_present(),
        # True when some row is genuinely plaintext right now, independent of
        # `phase` — a row can be written while the key was missing (a relink
        # with no key configured, say) and stay unsealed even though the phase
        # already reads `encrypted` from an earlier backfill. Lets the Settings
        # panel re-offer the encrypt action instead of a phase flag that, once
        # set, never gets revisited.
        "needs_reseal": await secrets_backfill.unsealed_present(),
        "env_var": secrets_box.ENV_VAR,
    }


# ---------------------------------------------------------------------------
# status + consent flow (admin)
# ---------------------------------------------------------------------------

@guard.get("/api/admin/encryption", AuthLevel.ADMIN)
async def encryption_status():
    return JSONResponse({"ok": True, **await _status_payload()})


@guard.post("/api/admin/encryption/enable", AuthLevel.ADMIN)
async def encryption_enable(request: Request):
    """Begin turning encryption on, or — when a valid key is already in the
    environment — move straight to the confirm step with no restart. A generated key
    is revealed exactly once here and never stored; the admin copies it into their
    environment."""
    data = await _json_body(request)
    result = await encryption_flow.begin_enable(generate=bool(data.get("generate")))
    return JSONResponse({"ok": True, "phase": await encryption_flow.get_phase(), **result})


@guard.post("/api/admin/encryption/verify", AuthLevel.ADMIN)
async def encryption_verify():
    """After the restart, report whether the expected key actually arrived. Advances
    to the confirm step on success; leaves everything as-is on failure so the modal
    can re-show the instructions and the still-available opt-out."""
    detected = await encryption_flow.verify_key()
    return JSONResponse({
        "ok": True, "detected": detected, "phase": await encryption_flow.get_phase(),
    })


@guard.post("/api/admin/encryption/encrypt", AuthLevel.ADMIN)
async def encryption_encrypt():
    """Seal every plaintext secret and token in place and mark the instance
    encrypted. Refused without a valid key so a missing one cannot be mistaken for
    'nothing to do'."""
    if not secrets_box.is_enabled():
        return _error(
            "No valid encryption key is set in the environment yet. Set "
            f"{secrets_box.ENV_VAR} and restart before encrypting.", 409,
        )
    result = await encryption_flow.encrypt_now()
    return JSONResponse({"ok": True, **result})


@guard.post("/api/admin/encryption/opt-out", AuthLevel.ADMIN)
async def encryption_opt_out():
    """Leave secrets in plaintext for now; the Settings opt-in re-offers it later."""
    await encryption_flow.opt_out()
    return JSONResponse({"ok": True, "phase": await encryption_flow.get_phase()})


# ---------------------------------------------------------------------------
# lost-key recovery
# ---------------------------------------------------------------------------

@guard.get("/admin/encryption/recovery", AuthLevel.PUBLIC)
async def recovery_page(request: Request):
    """The lost-key recovery screen. PUBLIC and self-gating on purpose — see the
    module docstring. Handles both unhealthy states, not just the one the request
    gate forces every visitor into:

      KEY_MISMATCH — a key is set but wrong. The whole app is gated here already.
      KEY_MISSING  — no key at all. The app stays usable elsewhere (this state
        fails open by design), so nothing forces an admin here; the page is
        still useful when they find their own way to it, most likely because
        Settings told them to, or because the original key is gone for good and
        restoring it isn't an option. Offers a freshly generated key for that
        case — setting it and restarting turns this into an ordinary
        KEY_MISMATCH, which the door-2 reset below already knows how to clean
        up.

    When the key is healthy there is nothing to recover, so it steps aside to
    the calendar."""
    health = encryption_flow.health()
    if health not in (encryption_flow.KEY_MISMATCH, encryption_flow.KEY_MISSING):
        return RedirectResponse("/", status_code=303)
    user = await auth.current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    context = {
        "request": request,
        "is_admin": user.is_admin,
        "health": health,
        "key_missing": health == encryption_flow.KEY_MISSING,
        "env_var": secrets_box.ENV_VAR,
        "confirm_phrase": RESET_CONFIRM_PHRASE,
        "asset_v": assets.ASSET_VERSION,
    }
    return templates.TemplateResponse(request, "encryption_recovery.html", context)


@guard.post("/api/admin/encryption/recovery/generate-key", AuthLevel.ADMIN)
async def recovery_generate_key():
    """A freshly generated key for an admin who has lost the original entirely.

    Stateless and side-effect-free — same generate_key() the enable flow's
    reveal-once step uses — so it is safe to call more than once; nothing is
    written until the admin saves it to their own environment and restarts.
    That restart is what turns KEY_MISSING into an ordinary KEY_MISMATCH (the
    new key does not decrypt the old canary), at which point the reset below
    is how the admin actually discards what the lost key made unrecoverable.
    """
    return JSONResponse({"ok": True, "key": encryption_flow.generate_key()})


@guard.post("/api/admin/encryption/recovery/reset", AuthLevel.ADMIN)
async def recovery_reset(request: Request):
    """The typed-confirmation destructive reset — the recovery path taken when the
    original key is truly gone (the other path is simply restoring it, which needs no
    endpoint). Only meaningful while the key is actually wrong; refused otherwise so
    it can never blank a healthy instance's values by mistake."""
    if encryption_flow.health() != encryption_flow.KEY_MISMATCH:
        return _error(
            "There is nothing to reset — the current key matches the stored secrets.",
            409,
        )
    data = await _json_body(request)
    typed = str(data.get("confirm") or "").strip().lower()
    if typed != RESET_CONFIRM_PHRASE:
        return _error(f'Type "{RESET_CONFIRM_PHRASE}" exactly to confirm the reset.')
    result = await encryption_flow.destructive_reset()
    return JSONResponse({"ok": True, **result})
