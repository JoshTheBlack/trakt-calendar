"""At-rest encryption for the instance's stored secrets.

Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package: authenticated,
versioned, and not hand-rolled. The key lives ONLY in the ENCRYPTION_KEY
environment variable and is never written to disk by this app — that off-disk
placement is the whole point, since a key sitting next to the database file would
leak with it in a backup or snapshot.

Two storage-facing operations, seal() and open_(), sit between the rest of the app
and the DB. A sealed value is tagged `enc:v1:` so that:
  - sealed and legacy-plaintext rows can coexist in the same column with no
    flag-day cutover, and
  - the scheme is version-stamped, leaving room for a future v2. Fernet supports
    MultiFernet for key rotation (decrypt with old + new keys, encrypt with the new
    one); a future `enc:v2:` prefix plus this same seal/open indirection is where
    rotation would slot in, so the next person is not left guessing.

Enabled vs. not: with no key in the environment, seal() and open_() are
pass-throughs and every value is stored and served as plaintext, exactly as before
encryption existed. That is a deliberate opt-out state — distinct from a key that
IS set but malformed, which raises InvalidKeyError so startup fails fast with an
actionable message instead of silently degrading to plaintext.

open_() degradation rules — a correctness point, not a nicety:
  None                    -> None
  not `enc:v1:`-prefixed    -> returned unchanged (a legacy plaintext row)
  sealed, but NO key set    -> the key was removed. Treated as UNSET (returns None)
                              rather than returning ciphertext, so the app fails
                              OPEN — a provider is never handed `enc:v1:...` — and
                              the operator restores the key to get the value back.
                              Read-only: the ciphertext in the DB is untouched and
                              returns the moment the key is back.
  sealed, but WRONG key     -> raises SealedButWrongKey (loud). A rotated or
                              mistyped key must never be mistaken for "unset" or
                              silently return garbage.
The split between "no key at all" (fail open) and "a key that does not decrypt"
(fail loud) is what makes the fail-open promise safe: an operator who removed the
key recovers by putting it back, while one who replaced it with the wrong key is
stopped loudly instead of losing data to a value that reads as blank.

Empty string vs None are kept distinct throughout: "" is a real (if empty) stored
value and seals/opens to "", never collapsing to None.
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENV_VAR = "ENCRYPTION_KEY"

# Marks a value as sealed by this module and stamps the scheme version. Present so
# sealed and plaintext rows coexist and a future scheme can be told apart.
PREFIX = "enc:v1:"


class SecretsBoxError(Exception):
    """Base for the errors this module raises."""


class InvalidKeyError(SecretsBoxError):
    """ENCRYPTION_KEY is set but is not a usable Fernet key.

    Raised eagerly so a mistyped or truncated key stops the app at startup with a
    clear message, rather than being mistaken for the no-key opt-out and quietly
    storing everything in the clear."""


class SealedButWrongKey(SecretsBoxError):
    """A value is sealed but the configured key does not decrypt it.

    Signals a rotated or mistyped key, which must fail loudly: returning it as
    "unset" would let a re-save overwrite a value the correct key could still
    recover."""


# Cached across calls: the Fernet instance when a valid key is configured, None
# when the env var is absent/blank (the opt-out state). `_loaded` distinguishes
# "not read yet" from "read, and there is no key", so a genuine no-key state is
# cached instead of re-parsing the environment on every seal/open.
_fernet: Fernet | None = None
_loaded: bool = False


def reset_cache() -> None:
    """Forget the cached key so the next call re-reads the environment.

    The running app reads ENCRYPTION_KEY once at process start and never changes
    it, so this exists for tests (and any code that mutates the env in-process)
    to pick up a new value without a restart."""
    global _fernet, _loaded
    _fernet = None
    _loaded = False


def _instance() -> Fernet | None:
    """The configured Fernet, or None when no key is set.

    Reads ENCRYPTION_KEY once and caches the result. A present-but-malformed key
    raises InvalidKeyError; a blank/absent one is the valid opt-out and caches as
    None."""
    global _fernet, _loaded
    if _loaded:
        return _fernet
    raw = os.environ.get(ENV_VAR, "")
    candidate = raw.strip()
    if not candidate:
        _fernet = None
    else:
        try:
            _fernet = Fernet(candidate)
        except (ValueError, TypeError) as exc:
            # Distinct from "no key": the operator meant to enable encryption but
            # the value can't be used, so refuse to run rather than silently fall
            # back to plaintext under a key they think is protecting them.
            raise InvalidKeyError(
                f"{ENV_VAR} is set but is not a valid Fernet key. Generate one with "
                f"`python -c \"from cryptography.fernet import Fernet; "
                f"print(Fernet.generate_key().decode())\"` and set it as {ENV_VAR}, "
                f"or unset {ENV_VAR} to leave secrets stored as plaintext."
            ) from exc
    _loaded = True
    return _fernet


def is_enabled() -> bool:
    """True when a valid key is configured and secrets will be sealed at rest."""
    return _instance() is not None


def key_is_valid(candidate: str | bytes) -> bool:
    """Whether `candidate` is a usable Fernet key.

    Used to VERIFY a pasted or env-provided key before anything is encrypted with
    it, so a bad key is caught up front instead of after it has sealed real data."""
    if isinstance(candidate, bytes):
        candidate = candidate.decode("utf-8", "ignore")
    candidate = (candidate or "").strip()
    if not candidate:
        return False
    try:
        Fernet(candidate)
    except (ValueError, TypeError):
        return False
    return True


def seal(plaintext: str | None) -> str | None:
    """Encrypt `plaintext` for storage, or pass it through when no key is set.

    None stays None. With no key configured the value is returned unchanged, so
    the DB simply holds plaintext exactly as it did before encryption existed.
    Otherwise returns `enc:v1:` + the Fernet token, which open_() reverses. An
    empty string is a real value and seals to a token, not to None."""
    if plaintext is None:
        return None
    fernet = _instance()
    if fernet is None:
        return plaintext
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return PREFIX + token


def open_(stored: str | None) -> str | None:
    """Decrypt a stored value, honoring the coexistence and fail-open/loud rules.

    See the module docstring for the full case table. In short: None -> None;
    an unprefixed value is legacy plaintext and returned as-is; a sealed value with
    no key configured returns None (fail open, so ciphertext never reaches a
    provider and the value comes back when the key is restored); a sealed value the
    configured key cannot decrypt raises SealedButWrongKey (fail loud)."""
    if stored is None:
        return None
    if not stored.startswith(PREFIX):
        return stored
    fernet = _instance()
    if fernet is None:
        # Sealed, but the key is gone. Do NOT return the ciphertext; report the
        # value as unset so the app degrades to "re-link / re-enter" instead of
        # shipping `enc:v1:...` to a provider. The row is left intact and reads
        # correctly again the moment ENCRYPTION_KEY is restored.
        return None
    token = stored[len(PREFIX):].encode("ascii")
    try:
        return fernet.decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        # A key is set but it does not open this value: it is the wrong/rotated
        # key. Fail loud — treating it as "unset" would invite a re-save that
        # overwrites a value the original key could still recover.
        raise SealedButWrongKey(
            "A stored secret is encrypted with a different key than the one "
            f"currently in {ENV_VAR}. Restore the original key, or run the "
            "encrypted-secrets reset to discard the unrecoverable values."
        ) from exc


def plaintext_storage_warning(unsealed_present: bool) -> str | None:
    """The one-line warning to surface at startup when something is stored in
    the clear right now.

    Takes the ANSWER (is any row actually unsealed?), not is_enabled(), because
    a key can be configured without every row being sealed under it yet — the
    seal-in-place backfill runs on the admin's confirmation, not the instant a
    key lands in the environment. Gating this on is_enabled() alone would
    silence the warning the moment a key is set, even though the existing rows
    are still plaintext until that backfill actually runs — exactly the window
    this warning exists to cover. The caller checks the real rows for the
    `enc:v1:` prefix, so this module still needs no DB import."""
    if unsealed_present:
        return (
            f"Secrets are stored UNENCRYPTED in the database. Set {ENV_VAR} to a "
            "Fernet key and enable at-rest encryption to protect them in backups "
            "and snapshots."
        )
    return None
