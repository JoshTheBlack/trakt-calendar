"""The admin-facing lifecycle for at-rest encryption: turning it on, verifying the
key survived a restart, and recovering when the key is lost or replaced.

The crypto itself lives in app/secrets_box (seal/open) and the bulk conversion in
app/secrets_backfill (plaintext rows -> sealed). This module owns the operator's
decisions and the health of the key over time, both recorded in app_meta:

  - `encryption_phase` — where an instance is in the enable flow, one of PHASE_*
    below. It advances on admin choice (offer -> chose to enable -> key verified in
    env -> encrypted), and OPTED_OUT is the deliberate "not now". It is NOT the
    schema-migration version and NOT an app_secrets row: a control flag is neither a
    credential nor sealable, and parking it beside the sealed secrets would force a
    skip-this-row exception into the seal-everything backfill.

  - `encryption_canary` — a known constant sealed under the key encryption was
    turned on with. One O(1) check at startup tells the three key states apart
    without touching a real secret: it decrypts -> the key is right (HEALTHY);
    it is present but a key is set and it will not decrypt -> the WRONG key is in
    the environment (KEY_MISMATCH); it is present but no key is set at all -> the
    key was removed (KEY_MISSING, a read-only degraded state that fails open).
    Without the canary those states would only surface by hitting a real secret
    mid-request and 500ing.

KEY_MISMATCH is the one that cannot be lived with: every value sealed under the old
key raises on decrypt, so load_settings() itself raises and ordinary request paths
would 500 one by one. The health check is derived once at startup so the app can
gate an administrator into the recovery screen BEFORE any of those loads run,
rather than after they have each failed. Two ways out from there: put the original
key back (nothing is written — the canary simply decrypts again), or accept the
loss and run destructive_reset(), which blanks only the values the current key
cannot open, keeps the identity rows so their owners fail open to "re-link", and
re-seals the canary under the current key.

While the key is unhealthy, secret writes are refused (see secret_writes_blocked):
sealing a freshly typed API key while no key is configured would write plaintext
over ciphertext the original key could still recover, turning a recoverable outage
into real loss. Non-secret settings are never sealed and stay writable.
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet

from . import db, secrets_backfill, secrets_box

logger = logging.getLogger(__name__)

# app_meta keys.
PHASE_KEY = "encryption_phase"
CANARY_KEY = "encryption_canary"

# Enable-flow states, persisted under PHASE_KEY. A plain string rather than an enum
# so it stores and compares as-is in app_meta.
PHASE_NONE = "none"                       # never offered / not yet decided
PHASE_PENDING_KEY = "pending_key"         # chose to enable; key must be set in env + a restart
PHASE_PENDING_ENCRYPT = "pending_encrypt" # key verified in env; awaiting the confirm to seal
PHASE_ENCRYPTED = "encrypted"             # secrets are sealed at rest
PHASE_OPTED_OUT = "opted_out"             # deliberately left plaintext; re-offerable from Settings

# Derived key-health states. Never stored — computed from the canary at startup and
# after any operation that could change it (encrypting, the recovery reset).
HEALTHY = "healthy"          # no encryption, or the canary decrypts under the current key
KEY_MISSING = "key_missing"  # canary sealed but no key in env: degraded, fails open, recoverable
KEY_MISMATCH = "key_mismatch"  # canary sealed and a key IS set but does not decrypt it: wrong key

# What the canary holds. Its plaintext is not a secret — only whether it round-trips
# under the current key matters — so a fixed, human-legible constant is fine.
CANARY_PLAINTEXT = "at-rest-encryption-key-health-canary"

# Set with a valid ENCRYPTION_KEY to run the seal-in-place conversion at startup
# with no browser flow — the same conversion the Settings opt-in button calls. It
# exists so the test suite and local dev can turn encryption on non-interactively;
# once it is on, the phase lives in the DB and a restart with the key present just
# works with no modal, so this is a convenience, not the primary path.
ENV_ESCAPE_HATCH = "ENCRYPT_SECRETS"

# Cached derived health, refreshed by refresh_health(). Defaults to HEALTHY so an
# instance that never enabled encryption (and never calls refresh_health) is never
# accidentally treated as unhealthy. Readable synchronously by the write guard.
_health: str = HEALTHY


# ---------------------------------------------------------------------------
# phase
# ---------------------------------------------------------------------------

async def get_phase() -> str:
    """The stored enable-flow phase, defaulting to NONE when nothing has been
    recorded yet (a freshly consolidated instance that has not been offered
    encryption)."""
    return await db.get_meta(PHASE_KEY, PHASE_NONE) or PHASE_NONE


async def set_phase(phase: str) -> None:
    await db.set_meta(PHASE_KEY, phase)


# ---------------------------------------------------------------------------
# key-health canary
# ---------------------------------------------------------------------------

def _derive_health(canary_stored: str | None) -> str:
    """Classify the key state from the stored canary and the current environment.

    No canary (or a legacy-unsealed one) means encryption was never turned on, so
    the instance is HEALTHY by definition. A sealed canary with no key is the
    removed-key degraded state (KEY_MISSING); one the configured key cannot open is
    the wrong/replacement key (KEY_MISMATCH)."""
    if canary_stored is None or not canary_stored.startswith(secrets_box.PREFIX):
        return HEALTHY
    if not secrets_box.is_enabled():
        return KEY_MISSING
    try:
        opened = secrets_box.open_(canary_stored)
    except secrets_box.SealedButWrongKey:
        return KEY_MISMATCH
    # A key that decrypts the canary to anything other than the constant it sealed
    # is not our key either — treat it as a mismatch rather than trusting it.
    return HEALTHY if opened == CANARY_PLAINTEXT else KEY_MISMATCH


async def refresh_health() -> str:
    """Recompute and cache the derived key health from the stored canary.

    Called at startup and after any operation that reseals the canary or changes the
    key situation, so the synchronous secret_writes_blocked() and the request gate
    read a value that reflects reality."""
    global _health
    _health = _derive_health(await db.get_meta(CANARY_KEY))
    return _health


def health() -> str:
    """The last derived key health. Synchronous by design: the request gate and the
    write guard both need it without awaiting a query on every call."""
    return _health


def secret_writes_blocked() -> bool:
    """Whether a secret may NOT be written right now.

    True while the key is missing or wrong: in both states a fresh seal would either
    write plaintext over recoverable ciphertext (no key) or pile a second key's
    ciphertext onto values the app already cannot read (wrong key). Non-secret
    settings are unaffected — they are never sealed."""
    return _health in (KEY_MISSING, KEY_MISMATCH)


def _seal_canary(conn: db.Connection) -> None:
    """Seal the health canary under the current key and store it, inside the caller's
    transaction. A no-op-shaped write when no key is set would store plaintext, so
    this is only ever called on a path that has already confirmed a key."""
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CANARY_KEY, secrets_box.seal(CANARY_PLAINTEXT)),
    )


# ---------------------------------------------------------------------------
# enabling
# ---------------------------------------------------------------------------

def generate_key() -> str:
    """A fresh Fernet key as a string, for the reveal-once path. Never stored by the
    app — the admin copies it into their environment."""
    return Fernet.generate_key().decode("ascii")


async def begin_enable(generate: bool) -> dict:
    """Start turning encryption on.

    When a valid key is ALREADY in the environment there is nothing to save and no
    restart to wait for, so this jumps straight to the awaiting-confirm phase and
    reports restart_required False. Otherwise it records that a key is expected and
    reports it back — with a freshly generated one to reveal when `generate` is set,
    or None when the admin is bringing their own — so the caller can show the
    save-to-env-and-restart instructions."""
    if secrets_box.is_enabled():
        await set_phase(PHASE_PENDING_ENCRYPT)
        return {"restart_required": False, "key": None}
    await set_phase(PHASE_PENDING_KEY)
    return {"restart_required": True, "key": generate_key() if generate else None}


async def verify_key() -> bool:
    """After a restart, check whether the expected key actually arrived in the
    environment. On success advance to awaiting-confirm and report True; on failure
    leave the phase alone so the caller can re-show the instructions (and the still-
    available opt-out) — nothing has been encrypted, so this is safe to retry."""
    if not secrets_box.is_enabled():
        return False
    await set_phase(PHASE_PENDING_ENCRYPT)
    return True


async def encrypt_now() -> dict:
    """Seal every plaintext secret and token in place, stamp the canary, and mark the
    instance encrypted. Requires a valid key; refuses loudly otherwise so a missing
    key cannot be mistaken for 'nothing to do'. Idempotent — the backfill skips
    already-sealed rows — so it is safe to call again after a partial run."""
    if not secrets_box.is_enabled():
        raise RuntimeError(
            "Cannot encrypt: no valid key is configured. Set ENCRYPTION_KEY in the "
            "environment first."
        )
    counts = await secrets_backfill.seal_plaintext_in_place()

    def _finish(conn: db.Connection) -> None:
        # The canary records which key these secrets now belong to and the phase
        # records that the conversion happened, both in one transaction so a crash
        # cannot leave the instance marked encrypted without a canary to check it
        # against. The backfill ran its own transaction above; a crash between the
        # two just means a re-run re-seals the already-sealed rows as a no-op and
        # finishes here.
        _seal_canary(conn)
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PHASE_KEY, PHASE_ENCRYPTED),
        )

    await db.transaction(_finish)
    await refresh_health()
    logger.info(
        "At-rest encryption enabled: sealed %d stored secret(s) and %d identity "
        "token pair(s).", counts["app_secrets"], counts["identity_tokens"],
    )
    return {"phase": PHASE_ENCRYPTED, "counts": counts}


async def opt_out() -> None:
    """Record that the operator chose to leave secrets in plaintext for now. The
    Settings opt-in re-offers it later; nothing is encrypted and nothing is lost."""
    await set_phase(PHASE_OPTED_OUT)


async def run_env_escape_hatch() -> bool:
    """When the escape-hatch env var is set alongside a valid key, encrypt at startup
    with no browser flow. Returns whether it ran. Skipped when already encrypted (the
    backfill would be a no-op anyway) or when there is no key to seal under."""
    if not os.environ.get(ENV_ESCAPE_HATCH, "").strip():
        return False
    if not secrets_box.is_enabled():
        return False
    if await get_phase() == PHASE_ENCRYPTED:
        return False
    await encrypt_now()
    logger.info(
        "%s was set with a valid key: encrypted at rest without the consent flow.",
        ENV_ESCAPE_HATCH,
    )
    return True


# ---------------------------------------------------------------------------
# lost-key recovery
# ---------------------------------------------------------------------------

async def destructive_reset() -> dict:
    """Discard the values the CURRENT key cannot open, and make the instance healthy
    under that key again. The deliberate, admin-confirmed way out of a lost original
    key — and the only path allowed to blank a row on a failed decrypt.

    Every sealed value is tried under the current key: the ones that open are left
    untouched (they belong to this key already), and the unrecoverable ones are
    cleared. An app_secret is cleared by removing its row (an unset secret has no
    row); an identity token is cleared to NULL with the row KEPT, so the link
    survives and fails open to a clean 're-link' instead of leaving canary-tripping
    ciphertext behind. The canary is re-sealed under the current key and the phase
    returns to encrypted, so the next check reads HEALTHY. The operator then re-enters
    API keys and users re-link, all sealing under the new key."""

    def _work(conn: db.Connection) -> dict:
        blanked_secrets = 0
        for row in conn.execute("SELECT name, value FROM app_secrets").fetchall():
            value = row["value"]
            if value is None or not value.startswith(secrets_box.PREFIX):
                continue
            try:
                secrets_box.open_(value)
            except secrets_box.SealedButWrongKey:
                conn.execute("DELETE FROM app_secrets WHERE name = ?", (row["name"],))
                blanked_secrets += 1

        blanked_tokens = 0
        for row in conn.execute(
            "SELECT id, access_token, refresh_token FROM linked_identities"
        ).fetchall():
            updates: dict[str, None] = {}
            for column in ("access_token", "refresh_token"):
                value = row[column]
                if value is None or not value.startswith(secrets_box.PREFIX):
                    continue
                try:
                    secrets_box.open_(value)
                except secrets_box.SealedButWrongKey:
                    updates[column] = None
            if not updates:
                continue
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE linked_identities SET {assignments} WHERE id = ?",
                (*updates.values(), row["id"]),
            )
            blanked_tokens += 1

        # Re-key the instance to the current key and clear the mismatch.
        _seal_canary(conn)
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PHASE_KEY, PHASE_ENCRYPTED),
        )
        return {"app_secrets": blanked_secrets, "identity_tokens": blanked_tokens}

    result = await db.transaction(_work)
    await refresh_health()
    logger.warning(
        "Encrypted-secrets reset: cleared %d unrecoverable stored secret(s) and %d "
        "identity token pair(s), and re-keyed to the current ENCRYPTION_KEY. Affected "
        "API keys must be re-entered and affected users must re-link.",
        result["app_secrets"], result["identity_tokens"],
    )
    return result
