"""Password hashing and verification.

The one security-load-bearing thing here is that every Argon2 call is offloaded
to a worker thread; the reason is written at hash_password.
"""
from __future__ import annotations

from dataclasses import dataclass

import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Argon2id at the library's own defaults. Those defaults track current guidance
# and move as the library is updated, which is the whole point of
# check_needs_rehash below — hand-tuning here would freeze this instance at
# whatever was reasonable on the day it was written.
_hasher = PasswordHasher()

# Verified against when the submitted username doesn't exist, so an unknown
# username costs the same ~50-200ms as a wrong password and login can't be used
# to enumerate accounts by timing. Built on first use rather than at import, so
# a process that never sees a login doesn't pay for it.
_dummy_hash: str | None = None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hasher.hash("timing-parity-placeholder")
    return _dummy_hash


async def hash_password(password: str) -> str:
    """Hash a password, off-thread.

    Argon2 is memory-hard and deliberately costs 50-200ms of CPU. Called inline
    from an async route it would stall the event loop for that whole time, which
    turns every login request into a denial-of-service lever. Every hash and
    verify in this module is offloaded for that reason.
    """
    return await anyio.to_thread.run_sync(_hasher.hash, password)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    # Set when the password was correct but its stored hash was made with
    # outdated parameters. The caller persists it; None means nothing to do.
    new_hash: str | None = None


async def verify_password(stored_hash: str | None, password: str) -> VerifyResult:
    """Verify a password off-thread, upgrading the stored hash when the hashing
    library's defaults have moved on since it was written.

    A missing or empty stored hash still burns a full verify against the dummy
    hash, so an account with no password set is indistinguishable by timing from
    an account whose password was simply wrong.
    """
    def _work() -> VerifyResult:
        candidate = stored_hash or _dummy()
        try:
            _hasher.verify(candidate, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return VerifyResult(False)
        if not stored_hash:
            return VerifyResult(False)
        try:
            if _hasher.check_needs_rehash(candidate):
                return VerifyResult(True, _hasher.hash(password))
        except InvalidHashError:  # pragma: no cover — verify() already accepted it
            pass
        return VerifyResult(True)

    return await anyio.to_thread.run_sync(_work)


async def burn_dummy_verify(password: str) -> None:
    """Spend a verify against the dummy hash. Call it when the username is
    unknown so that failure costs the same as a real one."""
    await verify_password(None, password)
