"""Login, registration and handshake throttling.

One table backs three independent limiters — login, registration, invite
redemption — each keyed by its own key_type so their counts never mix. A
lockout is a COUNT over a trailing window, recomputed on every check, rather
than a stored "locked until" timestamp that could drift out of sync with the
attempts it is supposed to summarize.
"""
from __future__ import annotations

from fastapi import Request

from .. import db
from ..config import Settings, load_settings
from . import cookies

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
# The per-address counter is a DIFFERENT job from the per-username one and needs a
# different threshold. Per-username at 5 is the precise defence: it protects one
# account from being guessed at. Per-address exists only to stop one attacker
# spraying MANY usernames from one place — and it is shared by everybody behind
# that address, which on a home instance is every user, and behind a reverse proxy
# is the entire internet-facing side of the app.
#
# At 5 it did the wrong thing spectacularly: five wrong passwords on ONE account
# locked out EVERY account from that address, administrator included, with the
# generic "invalid username or password" and nothing in the log. The address
# limit has to sit far above anything one person fumbling a password produces,
# because the cost of tripping it is borne by people who did nothing.
LOGIN_IP_MAX_ATTEMPTS = 25
REGISTER_MAX_ATTEMPTS = 10
REGISTER_WINDOW_SECONDS = 60 * 60
INVITE_MAX_ATTEMPTS = 10
INVITE_WINDOW_SECONDS = 60 * 60
# The provider sign-in START routes. They are unauthenticated GETs that write a
# handshake row and — for Plex — call plex.tv before anybody has proved anything,
# so they are the one pre-auth path that costs this instance an outbound request.
# Generous, because a person retrying a flaky popup must never hit it: 30 in ten
# minutes is far more than any human does and far less than a script wants.
HANDSHAKE_MAX_ATTEMPTS = 30
HANDSHAKE_WINDOW_SECONDS = 10 * 60
# Old enough that no limiter above still needs the row, so one sweep interval
# covers all three (plus the share-page limiter built on the same table).
ATTEMPT_RETENTION_SECONDS = 24 * 60 * 60


async def record_attempt(key_type: str, key_value: str, succeeded: bool, now: int | None = None) -> None:
    ts = db.now() if now is None else now
    await db.execute(
        "INSERT INTO login_attempts (key_type, key_value, attempted_at, succeeded) VALUES (?, ?, ?, ?)",
        (key_type, key_value, ts, int(succeeded)),
    )


async def clear_attempts(key_type: str, key_value: str) -> None:
    """Drop every recorded attempt for this key. Called on a successful login so
    a string of earlier failures can't combine with one later mistyped password
    to lock out someone who just proved they own the account."""
    await db.execute(
        "DELETE FROM login_attempts WHERE key_type = ? AND key_value = ?", (key_type, key_value),
    )


async def is_locked_out(
    key_type: str, key_value: str, *, max_attempts: int, window_seconds: int, now: int | None = None,
) -> bool:
    """Whether `max_attempts` FAILURES have landed for this key within the
    trailing `window_seconds`. Failures only, so a burst of wrong passwords
    followed by the right one doesn't count against whoever just succeeded.

    A pure read. check_lockout() is what the sign-in paths call — it also clears
    a lockout that has served its time.
    """
    ts = db.now() if now is None else now
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
        "AND succeeded = 0 AND attempted_at > ?",
        (key_type, key_value, ts - window_seconds), default=0,
    )
    return int(count) >= max_attempts


async def check_lockout(
    key_type: str, key_value: str, *, max_attempts: int, window_seconds: int, now: int | None = None,
) -> bool:
    """Whether this key is locked out right now, RESETTING the counter when a
    lockout has expired.

    Without the reset, a lockout does not really end after `window_seconds`: the
    failures that caused it age out one at a time, so the count sits at
    max_attempts-1 and the very next mistake re-locks the key immediately. That
    is a lockout that quietly becomes permanent for anyone still using the
    account. Once a key has served a full window without reaching the threshold
    again, its history is dropped and it starts from zero.

    The caller must NOT record an attempt when this returns True — see the
    sign-in handlers. Counting attempts made while locked out lets a retry loop
    keep refilling the window and hold the lockout open indefinitely, which is
    denial of service against the account holder rather than protection for them.
    """
    ts = db.now() if now is None else now
    cutoff = ts - window_seconds

    def _work(conn: db.Connection) -> bool:
        recent = int(conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
            "AND succeeded = 0 AND attempted_at > ?",
            (key_type, key_value, cutoff),
        ).fetchone()[0])
        if recent >= max_attempts:
            return True
        # Not locked now, but there is history. If it ever reached the threshold
        # it was a lockout that has since lapsed, so wipe the slate rather than
        # leaving a primed counter behind.
        total = int(conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? "
            "AND succeeded = 0",
            (key_type, key_value),
        ).fetchone()[0])
        if total >= max_attempts:
            conn.execute(
                "DELETE FROM login_attempts WHERE key_type = ? AND key_value = ?",
                (key_type, key_value),
            )
        return False

    return await db.transaction(_work)


async def rate_limited(
    key_type: str, key_value: str, *, max_attempts: int, window_seconds: int, now: int | None = None,
) -> bool:
    """Whether `max_attempts` requests — successful or not — have landed for
    this key within the trailing `window_seconds`. For registration and invite
    redemption, which throttle request volume rather than failures."""
    ts = db.now() if now is None else now
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM login_attempts WHERE key_type = ? AND key_value = ? AND attempted_at > ?",
        (key_type, key_value, ts - window_seconds), default=0,
    )
    return int(count) >= max_attempts


async def cooldown_remaining(
    key_type: str, key_value: str, *, window_seconds: int, now: int | None = None,
) -> int:
    """Seconds left before this key may act again, or 0 when it may act now.

    READS WITHOUT RECORDING, which is the whole point. A limiter that records the
    refusal it just issued restarts its own window on every rejected attempt, so
    somebody clicking a button every few seconds is never let through — the wait
    resets instead of counting down. A caller pairs this with `record_attempt`
    at the moment work actually starts.
    """
    ts = db.now() if now is None else now
    last = await db.fetch_value(
        "SELECT MAX(attempted_at) FROM login_attempts WHERE key_type = ? AND key_value = ?",
        (key_type, key_value),
    )
    if last is None:
        return 0
    return max(0, int(last) + window_seconds - ts)


async def handshake_start_limited(request: Request, settings: Settings | None = None) -> bool:
    """Whether this address has started too many provider sign-ins, recording
    this one either way.

    Shared by both providers' start routes so one address cannot get a fresh
    budget by alternating between them. Volume-only, like the registration and
    share-page limiters: there is no notion of a "failed" start.
    """
    cfg = settings or load_settings()
    ip = cookies.client_ip(request, cfg)
    limited = await rate_limited(
        "handshake_ip", ip,
        max_attempts=HANDSHAKE_MAX_ATTEMPTS, window_seconds=HANDSHAKE_WINDOW_SECONDS,
    )
    await record_attempt("handshake_ip", ip, True)
    return limited


async def registration_rate_limited(ip: str, invite_token: str | None) -> bool:
    """Whether this address has tried to create an account too many times.

    TWO limits, because there are two things worth throttling and they have
    different budgets: attempts to register at all, and attempts against an
    invite. The second only applies when a token was actually presented — an
    invite-less sign-up should not consume the invite budget.

    ONE DEFINITION, because it is one rule. This is asked identically by
    registration with a password and by every provider sign-in that can create an
    account, and it was four copies of these six lines across three route modules
    before it lived here — which meant a provider added later would have been a
    fifth, and a change to either budget would have had to find all of them. It
    belongs beside record_attempt and rate_limited, which it is built from, and
    it names no provider, so a new one gets it by calling it.

    VOLUME, NOT FAILURES: pair it with record_registration_attempt at the moment
    a registration is actually decided, the same way handshake_start_limited and
    the share-page limiter work.
    """
    if await rate_limited("register_ip", ip, max_attempts=REGISTER_MAX_ATTEMPTS,
                          window_seconds=REGISTER_WINDOW_SECONDS):
        return True
    return bool(invite_token) and await rate_limited(
        "invite_ip", ip, max_attempts=INVITE_MAX_ATTEMPTS,
        window_seconds=INVITE_WINDOW_SECONDS,
    )


async def record_registration_attempt(ip: str, invite_token: str | None, succeeded: bool) -> None:
    """Record one registration attempt against the same two keys
    registration_rate_limited reads, so the pair cannot drift apart."""
    await record_attempt("register_ip", ip, succeeded)
    if invite_token:
        await record_attempt("invite_ip", ip, succeeded)


async def sweep_login_attempts(now: int | None = None) -> int:
    """Delete attempt rows old enough that no limiter still consults them. Run
    from the heartbeat loop alongside the session sweep."""
    ts = db.now() if now is None else now
    result = await db.execute(
        "DELETE FROM login_attempts WHERE attempted_at <= ?", (ts - ATTEMPT_RETENTION_SECONDS,),
    )
    return result.rowcount
