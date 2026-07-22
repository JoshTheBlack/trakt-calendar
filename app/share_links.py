"""Public share-link settings: the per-user token/username/slug publishing
state behind /s/, /u/, /c/, and the owner-chosen defaults a share request falls
back to before the app-wide default.

A row is created lazily — the first time a signed-in user opens the Share
panel — rather than for every account up front, since most accounts will never
touch it. `enabled_token` starts on so a freshly-opened panel already has one
working link; the human-readable forms are opt-in (a user whose username here
differs from what they go by elsewhere may not want it handed out).

This module owns storage and the cross-namespace validation rule. Resolving a
public request to a page is app/share_routes.py's job.
"""
from __future__ import annotations

import json
import secrets

from . import auth, db

PREFERRED_KINDS = ("token", "username", "slug")

# kind -> the column that gates whether that form of the link answers at all.
_ENABLED_COLUMN = {"token": "enabled_token", "username": "enabled_username", "slug": "enabled_slug"}

_SELECT = "SELECT * FROM share_links WHERE user_id = ?"


async def get(user_id: int):
    return await db.fetch_one(_SELECT, (user_id,))


def _insert_row(conn: db.Connection, user_id: int, now: int) -> None:
    """Seed the owner-default view columns from this user's CURRENT prefs and
    timezone at the moment the row is created — the same seed-then-diverge
    pattern user_prefs itself uses against settings.json. After this, the
    owner defaults are their own copy: app/main.py's prefs/timezone writes
    keep them in sync going forward (see their docstrings), but nothing here
    re-reads user_prefs on every share request."""
    prefs = conn.execute(
        "SELECT endpoint, card_style, day_packing, hide_not_watching, network_filter_json "
        "FROM user_prefs WHERE user_id = ?", (user_id,),
    ).fetchone()
    account = conn.execute("SELECT timezone FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.execute(
        "INSERT INTO share_links (user_id, token, preferred_kind, enabled_token, "
        "enabled_username, enabled_slug, created_at, token_rotated_at, "
        "endpoint, card_style, day_packing, hide_not_watching, network_filter_json, timezone) "
        "VALUES (?, ?, 'token', 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, secrets.token_urlsafe(32), now, now,
            prefs["endpoint"] if prefs else None,
            prefs["card_style"] if prefs else None,
            prefs["day_packing"] if prefs else None,
            int(bool(prefs["hide_not_watching"])) if prefs else 0,
            prefs["network_filter_json"] if prefs else "[]",
            account["timezone"] if account else None,
        ),
    )


async def get_or_create(user_id: int):
    """This user's share settings, creating a token-only row the first time
    anything asks. The read-then-maybe-insert happens inside one transaction so
    two concurrent first opens (two tabs) converge on one row rather than
    colliding on the UNIQUE user_id constraint."""
    row = await get(user_id)
    if row is not None:
        return row

    def _work(conn: db.Connection):
        existing = conn.execute(_SELECT, (user_id,)).fetchone()
        if existing is None:
            _insert_row(conn, user_id, db.now())
        return conn.execute(_SELECT, (user_id,)).fetchone()

    return await db.transaction(_work)


async def rotate_token(user_id: int) -> str:
    """Issue a fresh /s/<token>, immediately invalidating the old one."""
    await get_or_create(user_id)
    new_token = secrets.token_urlsafe(32)
    await db.execute(
        "UPDATE share_links SET token = ?, token_rotated_at = ? WHERE user_id = ?",
        (new_token, db.now(), user_id),
    )
    return new_token


async def set_enabled(user_id: int, kind: str, enabled: bool) -> None:
    if kind not in _ENABLED_COLUMN:
        raise ValueError(f"Unknown share kind: {kind!r}")
    await get_or_create(user_id)
    column = _ENABLED_COLUMN[kind]
    await db.execute(f"UPDATE share_links SET {column} = ? WHERE user_id = ?", (int(enabled), user_id))


async def set_preferred_kind(user_id: int, kind: str) -> None:
    if kind not in PREFERRED_KINDS:
        raise ValueError(f"Unknown share kind: {kind!r}")
    await get_or_create(user_id)
    await db.execute("UPDATE share_links SET preferred_kind = ? WHERE user_id = ?", (kind, user_id))


async def slug_error(slug: str, *, exclude_user_id: int | None = None) -> str | None:
    """None when `slug` is usable as a custom share slug right now, otherwise
    why not.

    Composes auth.identifier_error's format/reserved rules with the
    cross-namespace check a slug and a username must both pass: neither may
    equal an existing (or retired) instance of the other, and a slug may not
    already belong to a different user. Not authoritative under concurrency —
    the UNIQUE COLLATE NOCASE column is the final backstop, same as
    username_availability_error's relationship to the users table.
    """
    if err := auth.identifier_error(slug):
        return err
    candidate = slug.strip().lower()
    if await auth.find_user_by_username(candidate):
        return "That name is already someone's username."
    if await auth.identifier_is_retired("username", candidate):
        return "That name is already someone's username."
    if await auth.identifier_is_retired("slug", candidate):
        return "That slug is taken."
    row = await db.fetch_one("SELECT user_id FROM share_links WHERE custom_slug = ?", (candidate,))
    if row is not None and (exclude_user_id is None or int(row["user_id"]) != exclude_user_id):
        return "That slug is taken."
    return None


async def set_custom_slug(user_id: int, slug: str | None) -> str | None:
    """Set or clear the user's custom slug. Returns an error string (making no
    change) when `slug` is unusable, otherwise None.

    Clearing the slug also disables the slug form: an enabled form with no
    slug set would have nowhere to resolve.
    """
    await get_or_create(user_id)
    candidate = (slug or "").strip().lower()
    if not candidate:
        await db.execute(
            "UPDATE share_links SET custom_slug = NULL, enabled_slug = 0 WHERE user_id = ?",
            (user_id,),
        )
        return None
    if err := await slug_error(candidate, exclude_user_id=user_id):
        return err

    def _work(conn: db.Connection) -> None:
        conn.execute("UPDATE share_links SET custom_slug = ? WHERE user_id = ?", (candidate, user_id))

    try:
        await db.transaction(_work)
    except db.IntegrityError:
        # Lost a race with another save between the check above and this write.
        return "That slug is taken."
    return None


_OWNER_DEFAULT_FIELDS = frozenset({
    "endpoint", "card_style", "day_packing", "hide_not_watching", "network_filter", "timezone",
})


async def update_owner_defaults(user_id: int, **fields) -> None:
    """Partial update of the owner defaults a share request falls back to when
    a query param doesn't override them (§7.3's second tier). Mirrors
    auth.update_user_prefs; unknown keys and None values are ignored."""
    await get_or_create(user_id)
    updates = {k: v for k, v in fields.items() if k in _OWNER_DEFAULT_FIELDS and v is not None}
    if not updates:
        return
    columns: list[str] = []
    params: list = []
    for key, value in updates.items():
        if key == "hide_not_watching":
            columns.append("hide_not_watching = ?")
            params.append(int(bool(value)))
        elif key == "network_filter":
            columns.append("network_filter_json = ?")
            params.append(json.dumps(list(value)))
        else:
            columns.append(f"{key} = ?")
            params.append(value)
    params.append(user_id)
    await db.execute(f"UPDATE share_links SET {', '.join(columns)} WHERE user_id = ?", tuple(params))


# ---------------------------------------------------------------------------
# public resolution — the only reads app/share_routes.py needs
# ---------------------------------------------------------------------------
# Each resolver is a single query joining the owning user, so a disabled
# account's link resolves to nothing in the same lookup rather than a second
# check the caller could forget. A miss here and a miss for any other reason
# render the identical 404 (app/share_routes.py's job, not this module's).

_RESOLVE_SELECT = (
    "SELECT sl.*, u.username AS owner_username, u.timezone AS owner_account_timezone "
    "FROM share_links sl JOIN users u ON u.id = sl.user_id "
    "WHERE {condition} AND u.is_disabled = 0"
)


async def resolve_by_token(token: str):
    if not token:
        return None
    return await db.fetch_one(_RESOLVE_SELECT.format(condition="sl.token = ? AND sl.enabled_token = 1"), (token,))


async def resolve_by_username(username: str):
    candidate = (username or "").strip().lower()
    if not candidate:
        return None
    return await db.fetch_one(
        _RESOLVE_SELECT.format(condition="u.username = ? AND sl.enabled_username = 1"), (candidate,),
    )


async def resolve_by_slug(slug: str):
    candidate = (slug or "").strip().lower()
    if not candidate:
        return None
    return await db.fetch_one(
        _RESOLVE_SELECT.format(condition="sl.custom_slug = ? AND sl.enabled_slug = 1"), (candidate,),
    )
