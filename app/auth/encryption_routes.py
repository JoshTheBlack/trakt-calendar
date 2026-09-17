"""The HTTP surface for the at-rest-encryption lifecycle: the admin consent flow,
the later opt-in, and the lost-key recovery screen.

The decisions and the crypto live in encryption_flow.py beside it; this module is
the thin routing over them, the same shape app/auth/admin_routes.py is over
app/auth. Two things here are load-bearing and must survive any restyling:

  - The recovery page is deliberately PUBLIC and self-gating rather than declared
    ADMIN. When the key is wrong the whole app is gated to this one screen, and an
    ADMIN dependency here would bounce a signed-in non-admin to /me — which is also
    gated back to here — into a redirect loop. So it resolves the viewer itself:
    anonymous -> sign in, non-admin -> a plain 'ask an administrator' page, admin ->
    the two recovery doors. Its two ACTIONS stay ADMIN, because each is a request a
    control on that page makes, answered in place, which a loop cannot form around.

  - The reset is destructive and reuses the type-to-confirm pattern the admin
    account-delete uses: the caller must echo an exact phrase back, checked
    server-side, before anything is blanked.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

# `auth` is this module's OWN package, read through the package object one level
# up: auth.current_user is the surface app/auth/__init__.py exists to present, and
# a route has no business knowing which submodule happens to define it.
from .. import auth, authz, chrome, db, route_params, secrets_box
from ..templating import templates
from . import encryption_flow, secrets_backfill
from .levels import AuthLevel

logger = logging.getLogger(__name__)

router = APIRouter()
guard = authz.Guard(router)

# Where the request gate sends a browser while the key is wrong, and the one screen
# it may reach. Kept here next to the routes that answer it.
RECOVERY_PATH = "/admin/encryption/recovery"

# The exact phrase the destructive reset requires, echoed back by the admin — the
# same deliberate-friction pattern as typing an account's name to delete it.
RESET_CONFIRM_PHRASE = "reset encrypted secrets"


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


PANEL_TEMPLATE = "_encryption_panel.html"


async def panel_state() -> dict:
    """The current lifecycle state the Settings panel's encryption section is
    drawn from.

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

# EVERY ACTION ANSWERS WITH THE SECTION REDRAWN FROM THE FLOW'S OWN STATE. The
# panel used to be a state machine in the browser over the four fields this module
# reports, restating encryption_flow's rules in a second language — that `phase`
# is a one-way ratchet, that a missing key outranks everything — and drawing its
# sentences with innerHTML. The template reads `panel_state()` instead, so there
# is one statement of what each state means and it is next to the state.
#
# A REFUSAL IS DRAWN, AT 200. A non-200 is never swapped (static/js/boost.js's
# `noSwap`), so a sentence answered with 409 would be a press that did nothing.

async def _panel(request: Request, *, reveal: dict | None = None,
                 error: str | None = None):
    return templates.TemplateResponse(request, PANEL_TEMPLATE, {
        "request": request,
        "encryption": await panel_state(),
        "encryption_reveal": reveal,
        "encryption_error": error,
    })


@guard.post("/admin/encryption/enable", AuthLevel.ADMIN)
async def encryption_enable(request: Request):
    """Begin turning encryption on, or — when a valid key is already in the
    environment — move straight to the confirm step with no restart.

    A GENERATED KEY IS IN THIS ANSWER AND NOWHERE ELSE. It is never stored, and no
    later answer can draw it again: the admin copies it into their own environment
    from this one response, which is what "revealed once" has always meant."""
    data = await authz.json_body(request)
    result = await encryption_flow.begin_enable(generate=route_params.ticked(data.get("generate")))
    return await _panel(request, reveal=result if result["restart_required"] else None)


@guard.post("/admin/encryption/verify", AuthLevel.ADMIN)
async def encryption_verify(request: Request):
    """After the restart, check whether the expected key actually arrived. Advances
    to the confirm step on success; on failure the section says so beside the
    instructions and the still-available opt-out, and nothing has changed."""
    if await encryption_flow.verify_key():
        return await _panel(request)
    return await _panel(request, error=(
        f"No valid key found in the environment yet. Set {secrets_box.ENV_VAR} and "
        "restart, then check again."))


@guard.post("/admin/encryption/encrypt", AuthLevel.ADMIN)
async def encryption_encrypt(request: Request):
    """Seal every plaintext secret and token in place and mark the instance
    encrypted. Refused without a valid key so a missing one cannot be mistaken for
    'nothing to do'."""
    if not secrets_box.is_enabled():
        return await _panel(request, error=(
            "No valid encryption key is set in the environment yet. Set "
            f"{secrets_box.ENV_VAR} and restart before encrypting."))
    await encryption_flow.encrypt_now()
    return await _panel(request)


@guard.post("/admin/encryption/opt-out", AuthLevel.ADMIN)
async def encryption_opt_out(request: Request):
    """Leave secrets in plaintext for now; the section re-offers it later."""
    await encryption_flow.opt_out()
    return await _panel(request)


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
        # Only is_admin is actually read by this page — it has no shared header
        # to gate — but page_context is the one place the header flags live, so
        # this reads from there rather than deciding is_admin by hand.
        **chrome.page_context(user),
        "health": health,
        "key_missing": health == encryption_flow.KEY_MISSING,
        "env_var": secrets_box.ENV_VAR,
        "confirm_phrase": RESET_CONFIRM_PHRASE,
    }
    return templates.TemplateResponse(request, "encryption_recovery.html", context)


@guard.post("/admin/encryption/recovery/generate-key", AuthLevel.ADMIN)
async def recovery_generate_key(request: Request):
    """A freshly generated key for an admin who has lost the original entirely.

    Stateless and side-effect-free — same generate_key() the enable flow's
    reveal-once step uses — so it is safe to call more than once; nothing is
    written until the admin saves it to their own environment and restarts.
    That restart is what turns KEY_MISSING into an ordinary KEY_MISMATCH (the
    new key does not decrypt the old canary), at which point the reset below
    is how the admin actually discards what the lost key made unrecoverable.

    SHOWN ONCE, AND THAT IS WHAT THE MARKUP GUARANTEES. The key is in this answer
    and nowhere else: it is never stored, the page draws no key when it loads, and
    pressing again answers a DIFFERENT key rather than this one again — so nothing
    can put a key back on screen once it has left it.
    """
    await authz.json_body(request)
    response = _recovery_answer(request, "_recovery_key.html",
                                key=encryption_flow.generate_key(),
                                env_var=secrets_box.ENV_VAR)
    response.headers["Cache-Control"] = "no-store"
    return response


@guard.post("/admin/encryption/recovery/reset", AuthLevel.ADMIN)
async def recovery_reset(request: Request):
    """The typed-confirmation destructive reset — the recovery path taken when the
    original key is truly gone (the other path is simply restoring it, which needs no
    endpoint). Only meaningful while the key is actually wrong; refused otherwise so
    it can never blank a healthy instance's values by mistake.

    A refusal is drawn beside the form; a completed reset navigates to the calendar,
    which is reachable again the moment the unreadable values are gone."""
    data = await authz.json_body(request)
    if encryption_flow.health() != encryption_flow.KEY_MISMATCH:
        return _recovery_answer(request, "_form_refusal.html", message=NOTHING_TO_RESET)
    typed = str(data.get("confirm") or "").strip().lower()
    if typed != RESET_CONFIRM_PHRASE:
        return _recovery_answer(request, "_form_refusal.html", message=(
            f'Type "{RESET_CONFIRM_PHRASE}" exactly to confirm the reset. Nothing was changed.'))
    await encryption_flow.destructive_reset()
    return Response(status_code=204, headers={"HX-Redirect": "/"})


NOTHING_TO_RESET = "There is nothing to reset — the current key matches the stored secrets."


def _recovery_answer(request: Request, template: str, **context):
    return templates.TemplateResponse(request, template, {"request": request, **context})
