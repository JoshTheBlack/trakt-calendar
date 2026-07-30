"""Invites — the gate on registration.

A Plex or Trakt login only proves control of some account on that service —
neither is a membership check against anything this instance cares about — so
without a gate anyone on the internet could auto-register and sit in the
approval queue forever. An invite is that gate.
"""
from __future__ import annotations

import secrets

from .. import db


async def create_invite(
    *,
    created_by: int,
    label: str | None = None,
    expires_at: int | None = None,
    max_uses: int | None = None,
    grants_calendar_on_accept: bool = True,
    grants_ranker_on_accept: bool = True,
) -> dict:
    """Mint a new invite token. Returns {"id", "token"}.

    Both grants default to True: issuing an invite is already a deliberate act
    of trust, so making the invitee then wait in the approval queue is friction
    with no added safety. The ranker qualifies for the same treatment as the
    calendar because it exposes nobody's private data — its optional import of
    finished titles is separately gated, so granting the ranker can never hand
    over a watch history. There is deliberately no distrakt equivalent, for
    exactly that reason: that grant is always a separate, manual step taken
    after the account exists.
    """
    token = secrets.token_urlsafe(32)
    ts = db.now()
    await db.execute(
        "INSERT INTO invites (token, label, created_by, created_at, expires_at, max_uses, "
        "used_count, revoked, grants_calendar_on_accept, grants_ranker_on_accept) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
        (token, label or None, created_by, ts, expires_at, max_uses,
         int(grants_calendar_on_accept), int(grants_ranker_on_accept)),
    )
    invite_id = await db.fetch_value("SELECT id FROM invites WHERE token = ?", (token,))
    return {"id": int(invite_id), "token": token}


async def find_invite_by_token(token: str):
    return await db.fetch_one("SELECT * FROM invites WHERE token = ?", (token,))


def invite_is_usable(invite, now: int | None = None) -> bool:
    """Whether an invite row can still be redeemed right now.

    Takes the row rather than a token so a caller who already fetched it inside
    a transaction — to re-check quota against a concurrent redemption — doesn't
    pay for a second lookup. A missing row (None) reads as unusable, so a
    find_invite_by_token() result can be passed straight through with no
    separate None check.
    """
    if invite is None or invite["revoked"]:
        return False
    ts = db.now() if now is None else now
    if invite["expires_at"] is not None and ts >= int(invite["expires_at"]):
        return False
    if invite["max_uses"] is not None and int(invite["used_count"]) >= int(invite["max_uses"]):
        return False
    return True


def redeem_invite(
    conn: db.Connection,
    *,
    invite,
    user_id: int,
    ip_address: str | None = None,
    now: int | None = None,
) -> None:
    """SYNCHRONOUS. Increments the invite's use count and records who redeemed
    it. Call inside the same transaction that creates the account, against a
    row read inside that same transaction — the caller's earlier
    invite_is_usable() check ran before the transaction opened, and quota may
    have moved since."""
    ts = db.now() if now is None else now
    conn.execute("UPDATE invites SET used_count = used_count + 1 WHERE id = ?", (invite["id"],))
    conn.execute(
        "INSERT INTO invite_redemptions (invite_id, user_id, redeemed_at, ip_address) "
        "VALUES (?, ?, ?, ?)",
        (invite["id"], user_id, ts, ip_address),
    )


async def revoke_invite(invite_id: int) -> bool:
    result = await db.execute("UPDATE invites SET revoked = 1 WHERE id = ?", (invite_id,))
    return result.rowcount > 0


async def list_invites():
    """Newest first, with each invite's redemption count alongside it — enough
    for an admin listing without a second query per row."""
    return await db.fetch_all(
        "SELECT i.*, "
        "(SELECT COUNT(*) FROM invite_redemptions r WHERE r.invite_id = i.id) AS redemption_count "
        "FROM invites i ORDER BY i.created_at DESC"
    )


async def list_invite_redemptions(invite_id: int):
    """Who has redeemed a given invite, newest first — for the admin invites
    screen. LEFT JOIN because the redeeming user could since have been deleted;
    the redemption row still records that it happened."""
    return await db.fetch_all(
        "SELECT r.*, u.username FROM invite_redemptions r "
        "LEFT JOIN users u ON u.id = r.user_id WHERE r.invite_id = ? ORDER BY r.redeemed_at DESC",
        (invite_id,),
    )
