"""Linked provider identities — what a completed handshake produces.

`provider_user_id` is always the provider's immutable account handle (see
insert_linked_identity), and the UNIQUE (provider, provider_user_id) index is
what makes "this account is already known, sign its owner in" a single lookup.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import db, secrets_box
from ..config import Settings, load_settings
from ..media import user_images
from . import encryption_flow, invites, prefs, users


def insert_linked_identity(
    conn: db.Connection,
    *,
    user_id: int,
    provider: str,
    provider_user_id: str | int,
    display_name: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    token_expires_at: int | None = None,
    now: int | None = None,
) -> int:
    """SYNCHRONOUS.

    `provider_user_id` MUST be the provider's immutable, non-reassignable account
    handle — never a username, slug, or email, which can be changed by their
    owner, released, and re-registered by somebody else, who would then inherit
    this link. Per provider that is:

      Plex:  the numeric account id from /api/v2/user.
      Trakt: the account UUID from /users/settings. Trakt users have NO numeric
             id — `ids` on a user is `{"slug": ...}` — so the UUID is the whole
             of what is available (see trakt_auth.fetch_account).

    Stored as TEXT for exactly that reason: the two providers do not agree on a
    type, and the column has to hold whichever each one actually issues.
    """
    ts = db.now() if now is None else now
    # The token pair is sealed at rest when a key is configured (a pass-through
    # otherwise); it is opened again at the point it is used to call a provider, in
    # trakt_routes.py beside it. seal(None) stays None, so an identity that carries no
    # token (e.g. a Plex link) writes NULLs exactly as before.
    cur = conn.execute(
        "INSERT INTO linked_identities (user_id, provider, provider_user_id, display_name, "
        "access_token, refresh_token, token_expires_at, created_at, last_login_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, provider, str(provider_user_id), display_name,
         secrets_box.seal(access_token), secrets_box.seal(refresh_token),
         token_expires_at, ts, ts),
    )
    return int(cur.lastrowid)


@dataclass(frozen=True)
class ProviderIdentity:
    """One provider account, as a completed authorization describes it."""
    provider: str
    provider_user_id: str
    display_name: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: int | None = None
    # WHERE THE PROVIDER SAYS ITS PICTURE IS, AND NOTHING MORE. Carried on this
    # object because it arrives with the account lookup and is consumed by the
    # shared completion seam, but it is NEVER persisted: no column holds it, the
    # link and login writes below do not read it, and it is not part of what
    # makes this identity this identity. Treat it as third-party input all the
    # way — app/auth/provider_avatars.py decides whether it may be fetched.
    avatar_url: str | None = None


@dataclass(frozen=True)
class LoginOutcome:
    """What a provider authorization resolved to.

    `kind` is "login" (a known identity), "registered" (a new account), or
    "linked" (an additional identity on an account that was already signed in).
    """
    kind: str
    user_id: int
    calendar_approved: bool


class IdentityInUse(Exception):
    """The provider account is already linked to a DIFFERENT local account.

    Never resolved by moving the link. Silently reassigning it would mean
    whoever authorizes last owns the identity, which is a takeover primitive
    handed out for free.
    """


class RegistrationRefused(Exception):
    """A provider sign-in would have created an account, and may not.

    Either no usable invite travelled with the handshake, or the instance is
    not accepting registrations.
    """


class AccountUnavailable(Exception):
    """The identity resolved to an account that cannot be signed in to."""


class IdentityWritesBlocked(Exception):
    """A link/relink was refused because the encryption key is unhealthy.

    Linking an identity that already exists here calls _refresh_identity, which
    overwrites the row's stored tokens outright — exactly the same overwrite
    save_settings() already refuses for app-level secrets while the key is
    missing or wrong, and for the same reason: sealing is a pass-through
    without a working key, so the fresh tokens would land as plaintext over
    ciphertext the original key could still recover.
    """


class LastLoginMethod(Exception):
    """Unlinking would leave the account with no way to sign in at all."""


async def find_identity(provider: str, provider_user_id: str | int):
    return await db.fetch_one(
        "SELECT * FROM linked_identities WHERE provider = ? AND provider_user_id = ?",
        (provider, str(provider_user_id)),
    )


async def list_identities(user_id: int):
    return await db.fetch_all(
        "SELECT * FROM linked_identities WHERE user_id = ? ORDER BY provider", (user_id,),
    )


def _refresh_identity(
    conn: db.Connection, identity_id: int, identity: ProviderIdentity, ts: int,
) -> None:
    """SYNCHRONOUS. Write the newest token pair and display name onto an
    existing identity row.

    The display name is refreshed on every sign-in because it is only ever shown
    to the user, and a stale one on the account page is confusing; nothing keys
    off it, so refreshing it is free.
    """
    conn.execute(
        "UPDATE linked_identities SET display_name = ?, access_token = ?, refresh_token = ?, "
        "token_expires_at = ?, refreshing_until = NULL, last_login_at = ? WHERE id = ?",
        (identity.display_name, secrets_box.seal(identity.access_token),
         secrets_box.seal(identity.refresh_token), identity.token_expires_at, ts, identity_id),
    )


async def link_provider_identity(*, identity: ProviderIdentity, user_id: int) -> LoginOutcome:
    """Attach a provider account to the signed-in account, or refuse.

    Raises IdentityInUse when the provider account already belongs to someone
    else here. Re-linking one this account already holds is not an error — it
    just refreshes the stored token, which is what a user clicking "reconnect"
    is asking for — which is exactly why IdentityWritesBlocked is checked here
    first: that refresh overwrites the row's existing tokens unconditionally,
    so it is refused up front rather than partway through, the same guard
    save_settings() already applies to app-level secrets.
    """
    if encryption_flow.secret_writes_blocked():
        raise IdentityWritesBlocked()
    ts = db.now()

    def _work(conn: db.Connection) -> LoginOutcome:
        user = conn.execute(
            "SELECT calendar_approved, is_disabled FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if user is None or user["is_disabled"]:
            raise AccountUnavailable()
        existing = conn.execute(
            "SELECT * FROM linked_identities WHERE provider = ? AND provider_user_id = ?",
            (identity.provider, identity.provider_user_id),
        ).fetchone()
        if existing is not None:
            if int(existing["user_id"]) != user_id:
                raise IdentityInUse()
            _refresh_identity(conn, int(existing["id"]), identity, ts)
        else:
            insert_linked_identity(
                conn, user_id=user_id, provider=identity.provider,
                provider_user_id=identity.provider_user_id,
                display_name=identity.display_name, access_token=identity.access_token,
                refresh_token=identity.refresh_token,
                token_expires_at=identity.token_expires_at, now=ts,
            )
        return LoginOutcome("linked", user_id, bool(user["calendar_approved"]))

    return await db.transaction(_work)


async def login_with_provider_identity(
    *,
    identity: ProviderIdentity,
    invite_token: str | None = None,
    ip_address: str | None = None,
    settings: Settings | None = None,
) -> LoginOutcome:
    """Sign in with a provider account, registering one if it is unknown.

    A known identity signs its owner in. An unknown one is a REGISTRATION, and
    registration needs a usable invite unless the operator has opened the
    instance up — a provider sign-in proves only that somebody controls some
    account on that service, which is not a membership test for anything here.
    Without a usable invite this raises RegistrationRefused and NO account is
    created.

    The whole registration — account, preferences, identity, invite redemption —
    is one transaction, and the invite is re-read inside it because the quota
    check before it ran without the write lock held.
    """
    cfg = settings or load_settings()
    ts = db.now()

    existing = await find_identity(identity.provider, identity.provider_user_id)
    if existing is not None:
        user = await users.get_user(int(existing["user_id"]))
        if user is None or user["is_disabled"]:
            raise AccountUnavailable()

        def _sign_in(conn: db.Connection) -> LoginOutcome:
            _refresh_identity(conn, int(existing["id"]), identity, ts)
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (ts, user["id"]))
            return LoginOutcome("login", int(user["id"]), bool(user["calendar_approved"]))

        return await db.transaction(_sign_in)

    invite_required = not cfg.allow_open_registration
    token = (invite_token or "").strip()
    invite = await invites.find_invite_by_token(token) if token else None
    if invite_required and not invites.invite_is_usable(invite, ts):
        raise RegistrationRefused()

    def _register(conn: db.Connection) -> LoginOutcome:
        row = None
        if token:
            candidate = conn.execute(
                "SELECT * FROM invites WHERE token = ?", (token,),
            ).fetchone()
            usable = invites.invite_is_usable(candidate, ts)
            if invite_required and not usable:
                raise RegistrationRefused()
            # Under open registration a stale token doesn't block the
            # registration; it just doesn't grant anything either.
            row = candidate if usable else None
        # The invite's own grant, OR the instance-wide "approve new accounts
        # automatically" setting. Either is a deliberate administrator decision;
        # the setting just makes it once instead of per invite, and is what lets
        # open registration hand out a working account without one.
        grants_calendar = (
            bool(row["grants_calendar_on_accept"]) if row is not None else False
        ) or cfg.auto_approve_calendar
        grants_ranker = bool(row["grants_ranker_on_accept"]) if row is not None else False
        # No username and no password: this account's only credential is the
        # provider identity below. One can be added later from the account page.
        user_id = users.insert_user(
            conn, username=None, password_hash=None, calendar_approved=grants_calendar,
            distrakt_approved=False, ranker_approved=grants_ranker,
            timezone=cfg.timezone or None, now=ts,
        )
        prefs.insert_user_prefs(conn, user_id, cfg)
        insert_linked_identity(
            conn, user_id=user_id, provider=identity.provider,
            provider_user_id=identity.provider_user_id, display_name=identity.display_name,
            access_token=identity.access_token, refresh_token=identity.refresh_token,
            token_expires_at=identity.token_expires_at, now=ts,
        )
        if row is not None:
            invites.redeem_invite(conn, invite=row, user_id=user_id, ip_address=ip_address, now=ts)
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (ts, user_id))
        return LoginOutcome("registered", user_id, grants_calendar)

    try:
        return await db.transaction(_register)
    except db.IntegrityError as exc:
        # The UNIQUE (provider, provider_user_id) index: another request
        # registered this same provider account between the lookup above and
        # this insert. It belongs to that account now, not this one.
        raise IdentityInUse() from exc


async def unlink_identity(user_id: int, provider: str, *, force: bool = False) -> bool:
    """Remove a linked provider account. False when there was none to remove.

    Raises LastLoginMethod when this is the account's only remaining way in — an
    account with no password and no identities cannot be signed in to by anyone,
    including its owner, and there is no self-service recovery from that. Every
    caller except the admin screen leaves `force` at its default, so the
    self-service unlink endpoint keeps refusing exactly as before; the admin
    screen sets it only after showing the operator the same warning and asking
    them to confirm the orphan deliberately.
    """
    def _work(conn: db.Connection) -> bool:
        row = conn.execute(
            "SELECT id FROM linked_identities WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ).fetchone()
        if row is None:
            return False
        user = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        remaining = int(conn.execute(
            "SELECT COUNT(*) FROM linked_identities WHERE user_id = ? AND id != ?",
            (user_id, row["id"]),
        ).fetchone()[0])
        if remaining == 0 and not (user and user["password_hash"]) and not force:
            raise LastLoginMethod()
        conn.execute("DELETE FROM linked_identities WHERE id = ?", (row["id"],))
        return True

    removed = await db.transaction(_work)
    if removed:
        # THE SERVICE'S PICTURE GOES WITH THE LINK. An account no longer
        # connected to a service should not keep serving that service's copy of
        # its face, and the slot is the only place that copy lives. Deliberately
        # AFTER the transaction commits: a file delete cannot be rolled back, so
        # doing it inside would leave the picture gone on a transaction that
        # later failed. If this half does not happen the row is still gone and
        # the next link simply overwrites the slot, which is why it is not worth
        # a compensating step.
        user_images.delete_provider_avatar(user_id, provider)
    return removed


# ---------------------------------------------------------------------------
# token refresh serialization
# ---------------------------------------------------------------------------
# Providers issue a NEW refresh token every time one is spent and invalidate the
# old one, so two requests refreshing the same identity at the same moment both
# succeed against the provider and then overwrite each other — leaving the row
# holding a refresh token that was already replaced, and the user silently
# logged out of the integration. `refreshing_until` is a lease over the row that
# lets exactly one of them proceed.

REFRESH_LEASE_SECONDS = 60


async def claim_identity_refresh(
    identity_id: int, *, now: int | None = None, lease_seconds: int = REFRESH_LEASE_SECONDS,
) -> bool:
    """Take the refresh lease on an identity. False means somebody else has it.

    One conditional UPDATE, which SQLite runs as its own transaction, so the
    check and the claim cannot be interleaved. The lease expires on its own so
    that a process which dies mid-refresh doesn't wedge the row forever.
    """
    ts = db.now() if now is None else now
    result = await db.execute(
        "UPDATE linked_identities SET refreshing_until = ? "
        "WHERE id = ? AND (refreshing_until IS NULL OR refreshing_until <= ?)",
        (ts + lease_seconds, identity_id, ts),
    )
    return result.rowcount > 0


async def release_identity_refresh(identity_id: int) -> None:
    """Drop the lease without writing a token — for a refresh that failed."""
    await db.execute(
        "UPDATE linked_identities SET refreshing_until = NULL WHERE id = ?", (identity_id,),
    )


async def store_identity_tokens(
    identity_id: int,
    *,
    access_token: str | None,
    refresh_token: str | None,
    token_expires_at: int | None,
) -> None:
    """Persist a renewed token pair and release the refresh lease. The pair is
    sealed at rest when a key is configured, matching the other identity writers."""
    await db.execute(
        "UPDATE linked_identities SET access_token = ?, refresh_token = ?, "
        "token_expires_at = ?, refreshing_until = NULL WHERE id = ?",
        (secrets_box.seal(access_token), secrets_box.seal(refresh_token),
         token_expires_at, identity_id),
    )
