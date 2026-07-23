"""Convert already-stored plaintext secrets to their sealed form in place.

When at-rest encryption is turned on for an instance that has been running without
it, the credentials in app_secrets and the per-user tokens in linked_identities are
sitting in the clear. This walks both stores once and re-writes every not-yet-sealed
value as `enc:v1:` ciphertext under the configured key, leaving the rest of the app
— which reads through seal()/open_() either way — untouched.

Two properties matter and are both load-bearing:
  - Idempotent. An already-sealed value (it starts with the `enc:v1:` prefix) is
    skipped, so a second run touches nothing and re-running after a partial failure
    just finishes the job. It is also the same routine the "encrypt now" opt-in in
    Settings runs, so there is one conversion path, not two.
  - Never destructive. It only ever turns plaintext INTO ciphertext; it never
    decrypts, blanks, or rewrites a value that is already sealed (including one
    sealed under a different key). With no key configured it is a no-op, because
    sealing without a key would only rewrite plaintext as plaintext.

The whole walk runs in one transaction, so a crash midway leaves every row either
its original plaintext or fully sealed, never half-encrypted.
"""
from __future__ import annotations

import logging

from . import db, secrets_box

logger = logging.getLogger(__name__)


def _needs_sealing(value: str | None) -> bool:
    """A stored value that should be converted: present, and not already sealed.

    NULL means the column holds no token, and an `enc:v1:`-prefixed value is
    already sealed (possibly under another key) — neither is touched."""
    return value is not None and not value.startswith(secrets_box.PREFIX)


async def seal_plaintext_in_place() -> dict[str, int]:
    """Seal every plaintext secret and per-user token under the configured key.

    Returns a per-store count of how many values were converted. A no-op — and an
    all-zero count — when no key is configured or everything is already sealed, so
    it is safe to call unconditionally and safe to call again.
    """
    if not secrets_box.is_enabled():
        # No key: sealing is a pass-through, so there is nothing to convert. Return
        # rather than rewrite every row with an identical plaintext value.
        return {"app_secrets": 0, "identity_tokens": 0}

    def _work(conn: db.Connection) -> dict[str, int]:
        sealed_secrets = 0
        for row in conn.execute("SELECT name, value FROM app_secrets").fetchall():
            if not _needs_sealing(row["value"]):
                continue
            conn.execute(
                "UPDATE app_secrets SET value = ? WHERE name = ?",
                (secrets_box.seal(row["value"]), row["name"]),
            )
            sealed_secrets += 1

        sealed_tokens = 0
        for row in conn.execute(
            "SELECT id, access_token, refresh_token FROM linked_identities"
        ).fetchall():
            updates: dict[str, str] = {}
            for column in ("access_token", "refresh_token"):
                if _needs_sealing(row[column]):
                    updates[column] = secrets_box.seal(row[column])
            if not updates:
                continue
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE linked_identities SET {assignments} WHERE id = ?",
                (*updates.values(), row["id"]),
            )
            sealed_tokens += 1

        return {"app_secrets": sealed_secrets, "identity_tokens": sealed_tokens}

    result = await db.transaction(_work)
    if result["app_secrets"] or result["identity_tokens"]:
        logger.info(
            "Sealed %d stored secret(s) and %d identity token pair(s) at rest.",
            result["app_secrets"], result["identity_tokens"],
        )
    return result
