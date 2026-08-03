"""Everything the admin screen does to another account.

Kept here rather than in the route module so the business rules — the
last-admin guard chief among them — have exactly one implementation regardless
of how many HTTP routes end up calling them.
"""
from __future__ import annotations

import anyio.to_thread

from .. import db
from ..media import user_images
from . import identities, users


class UserNotFound(Exception):
    """The target account does not exist."""


class LastAdmin(Exception):
    """The instance's last remaining administrator cannot be demoted, disabled,
    or deleted — there would be no account left able to run the admin screen at
    all, including to reverse the mistake."""


class CannotDeleteSelf(Exception):
    """An administrator cannot delete their own account.

    Demoting it and deleting it from another admin's account works; this just
    rules out the one-click way to lock yourself out of your own instance.
    """


def _admin_count(conn: db.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0])


async def list_users_overview() -> list[dict]:
    """One row per account, shaped for the admin list: a display name (the
    username, or a linked identity's display name when there is no username),
    every linked provider, the three approval/disabled flags, and both
    activity timestamps.

    Two queries rather than one GROUP_CONCAT join, because an account can hold
    up to two linked identities and the "no username" fallback needs a
    specific one's display name, not a flattened string of both.
    """
    user_rows = await db.fetch_all("SELECT * FROM users ORDER BY created_at ASC")
    identity_rows = await db.fetch_all(
        "SELECT user_id, provider, display_name FROM linked_identities ORDER BY provider"
    )
    by_user: dict[int, list[dict]] = {}
    for row in identity_rows:
        by_user.setdefault(int(row["user_id"]), []).append(
            {"provider": row["provider"], "display_name": row["display_name"]}
        )
    sessions = await db.fetch_all(
        "SELECT user_id, MAX(last_seen_at) AS last_seen_at FROM sessions GROUP BY user_id"
    )
    last_seen = {int(row["user_id"]): row["last_seen_at"] for row in sessions}

    overview = []
    for u in user_rows:
        uid = int(u["id"])
        idents = by_user.get(uid, [])
        # DELIBERATELY NOT users.display_name. Three different things are called
        # a display name around here, and this is the third: the admin screen's
        # unambiguous LABEL for an account, which is also what an admin must type
        # back to confirm a deletion. The chosen display name is free text and
        # not unique, so two accounts could offer the identical confirmation
        # string — this stays on the username, which cannot.
        display = u["username"] or next((i["display_name"] for i in idents if i["display_name"]), None) or f"user #{uid}"
        overview.append({
            "id": uid,
            "username": u["username"],
            # The account's chosen name, or None. Separate from the label above.
            "chosen_name": u["display_name"],
            "display_name": display,
            "providers": [i["provider"] for i in idents],
            "is_admin": bool(u["is_admin"]),
            "is_bootstrap": bool(u["is_bootstrap"]),
            "calendar_approved": bool(u["calendar_approved"]),
            "distrakt_approved": bool(u["distrakt_approved"]),
            "ranker_approved": bool(u["ranker_approved"]),
            "is_disabled": bool(u["is_disabled"]),
            "created_at": u["created_at"],
            "last_login_at": u["last_login_at"],
            "last_session_at": last_seen.get(uid),
        })
    return overview


async def display_name_for(user_id: int) -> str | None:
    """The same display name list_users_overview() computes, for one account —
    what an admin must type back to confirm deleting it. None when the account
    doesn't exist.

    Ignores users.display_name for the reason given there: a chosen name is free
    text and not unique, so it cannot be a safe confirmation string.
    """
    user = await users.get_user(user_id)
    if user is None:
        return None
    if user["username"]:
        return user["username"]
    for row in await identities.list_identities(user_id):
        if row["display_name"]:
            return row["display_name"]
    return f"user #{user_id}"


async def set_calendar_approved(user_id: int, approved: bool) -> None:
    await db.execute(
        "UPDATE users SET calendar_approved = ?, updated_at = ? WHERE id = ?",
        (int(approved), db.now(), user_id),
    )


async def set_distrakt_approved(user_id: int, approved: bool) -> None:
    await db.execute(
        "UPDATE users SET distrakt_approved = ?, updated_at = ? WHERE id = ?",
        (int(approved), db.now(), user_id),
    )


async def set_ranker_approved(user_id: int, approved: bool) -> None:
    await db.execute(
        "UPDATE users SET ranker_approved = ?, updated_at = ? WHERE id = ?",
        (int(approved), db.now(), user_id),
    )


async def set_admin(user_id: int, is_admin: bool) -> None:
    """Promote or demote. Raises LastAdmin rather than demoting the instance's
    only administrator."""
    def _work(conn: db.Connection) -> None:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound()
        if not is_admin and row["is_admin"] and _admin_count(conn) <= 1:
            raise LastAdmin()
        conn.execute(
            "UPDATE users SET is_admin = ?, updated_at = ? WHERE id = ?",
            (int(is_admin), db.now(), user_id),
        )

    await db.transaction(_work)


async def set_disabled(user_id: int, disabled: bool) -> None:
    """Disable or re-enable an account.

    Disabling deletes every session that account holds, on top of the flag
    itself — a disabled account that stayed signed in everywhere it already was
    would not actually be disabled. Raises LastAdmin rather than disabling the
    instance's only administrator.
    """
    def _work(conn: db.Connection) -> None:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound()
        if disabled and row["is_admin"] and _admin_count(conn) <= 1:
            raise LastAdmin()
        conn.execute(
            "UPDATE users SET is_disabled = ?, updated_at = ? WHERE id = ?",
            (int(disabled), db.now(), user_id),
        )
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    await db.transaction(_work)


async def admin_set_username(user_id: int, username: str) -> None:
    """Give an OAuth-only account a username, so it can also be given a
    password. An account created purely by a provider sign-in has neither —
    there is nothing for set_password() to attach a username-based login to
    until this has run once."""
    await db.execute(
        "UPDATE users SET username = ?, updated_at = ? WHERE id = ?",
        (username.strip().lower(), db.now(), user_id),
    )


async def list_sessions(user_id: int):
    return await db.fetch_all(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC", (user_id,),
    )


# Per-user tables cleared by wipe_user_data(), beyond the account row itself.
# Each entry is (table_name, user_id_column); a later table that holds per-user
# data appends its own entry here rather than teaching wipe_user_data a new
# special case.
WIPE_DATA_TABLES: tuple[tuple[str, str], ...] = (
    ("not_watching_shows", "user_id"),
    ("calendar_view_state", "user_id"),
    ("distrakt_month_records", "user_id"),
    ("distrakt_user_seasons", "user_id"),
    ("distrakt_prompt_dismissals", "user_id"),
    ("distrakt_months", "user_id"),
    ("distrakt_watch_state", "user_id"),
    ("distrakt_show_progress", "user_id"),
    ("distrakt_movie_watches", "user_id"),
    ("distrakt_prefs", "user_id"),
)


async def wipe_user_data(user_id: int) -> None:
    """Clear a user's calendar and distrakt data, disable the account, and log
    it out everywhere — while keeping the account itself, its linked
    identities, its username/slug, and its share links untouched.

    This is the reversible "start this person over" action: re-enabling the
    account afterwards is all it takes to undo it, and nothing is retired.
    delete_user() is the separate, permanent action for when that is not what
    is wanted.
    """
    def _work(conn: db.Connection) -> None:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound()
        for table, column in WIPE_DATA_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (user_id,))
        conn.execute(
            "UPDATE users SET is_disabled = 1, updated_at = ? WHERE id = ?", (db.now(), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    await db.transaction(_work)


async def delete_user(user_id: int, *, actor_user_id: int) -> None:
    """Permanently delete an account and everything under it, in one
    transaction.

    Every foreign key that references users(id) is ON DELETE CASCADE except
    invites.created_by, which is SET NULL so that deleting the admin who
    issued an invite doesn't revoke it out from under someone mid-redemption —
    so this single DELETE fans out to sessions, linked_identities,
    auth_handshakes, invite_redemptions, and share_links with no row left
    behind in any of them. The account's username, custom share slug, and
    share token are all recorded in retired_identifiers so a new registration
    can't silently inherit a `/u/<username>`, `/c/<slug>`, or `/s/<token>` link
    that was already shared. The account's avatar and any saved images are
    removed from disk after the row commits, since they live under the user's
    id rather than in a cascading table. Raises CannotDeleteSelf or LastAdmin
    rather than performing either.
    """
    if user_id == actor_user_id:
        raise CannotDeleteSelf()

    def _work(conn: db.Connection) -> None:
        row = conn.execute(
            "SELECT username, is_admin FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if row is None:
            raise UserNotFound()
        if row["is_admin"] and _admin_count(conn) <= 1:
            raise LastAdmin()
        ts = db.now()
        if row["username"]:
            conn.execute(
                "INSERT OR IGNORE INTO retired_identifiers (kind, value, retired_at) "
                "VALUES ('username', ?, ?)",
                (row["username"], ts),
            )
        share = conn.execute(
            "SELECT custom_slug, token FROM share_links WHERE user_id = ?", (user_id,),
        ).fetchone()
        if share is not None:
            if share["custom_slug"]:
                conn.execute(
                    "INSERT OR IGNORE INTO retired_identifiers (kind, value, retired_at) "
                    "VALUES ('slug', ?, ?)",
                    (share["custom_slug"], ts),
                )
            conn.execute(
                "INSERT OR IGNORE INTO retired_identifiers (kind, value, retired_at) "
                "VALUES ('token', ?, ?)",
                (share["token"], ts),
            )
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    await db.transaction(_work)
    # The row is gone; the per-user upload directory (avatar, saved images) has
    # no row of its own to cascade from, so it needs its own sweep or it would
    # survive the account forever. Filesystem work, so it goes to a worker
    # thread; best-effort, since a stray leftover file must not make an
    # otherwise-successful deletion look like it failed.
    await anyio.to_thread.run_sync(user_images.delete_user_data, user_id)


async def list_retired_identifiers():
    return await db.fetch_all("SELECT * FROM retired_identifiers ORDER BY retired_at DESC")


async def release_retired_identifier(kind: str, value: str) -> bool:
    """Delete a retired-identifier block, making the name claimable again.

    Tokens are never releasable: they are random with no legitimate reason to
    reissue a specific one, unlike a username or slug someone might want back.
    """
    if kind == "token":
        raise ValueError("Share tokens cannot be released.")
    result = await db.execute(
        "DELETE FROM retired_identifiers WHERE kind = ? AND value = ?", (kind, value),
    )
    return result.rowcount > 0
