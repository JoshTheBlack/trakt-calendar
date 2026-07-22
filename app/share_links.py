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
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import auth, db
from .endpoints import ENDPOINTS

PREFERRED_KINDS = ("token", "username", "slug")

# The view vocabulary a public share page understands, kept here rather than in
# app/share_routes.py so the link BUILDER and the page's own param resolver
# cannot drift apart — a value this module will happily put in a URL that the
# page then rejects would be silently ignored at the far end.
CARD_STYLES = ("vertical", "horizontal", "poster")
DAY_PACKINGS = ("stacked", "packed")

# Param name -> what counts as a usable value. `year`/`month` are navigation
# rather than display and are deliberately absent: a link pinned to one month
# would go stale the moment that month passed.
LINK_VIEW_PARAMS = ("endpoint", "card", "packing", "hidenw", "tz")

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
    # All three forms answer from the start. Which one the panel shows is a
    # separate question (preferred_kind), and the username/slug forms resolve to
    # nothing until there is a username/slug to resolve, so enabling them up front
    # publishes nothing that did not already exist.
    conn.execute(
        "INSERT INTO share_links (user_id, token, preferred_kind, enabled_token, "
        "enabled_username, enabled_slug, created_at, token_rotated_at, "
        "endpoint, card_style, day_packing, hide_not_watching, network_filter_json, timezone) "
        "VALUES (?, ?, 'token', 1, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
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


async def set_active_kind(user_id: int, kind: str) -> None:
    """Record which link form the Share panel hands out, and make sure all three
    still answer.

    The dropdown is PRESENTATION ONLY. Every form a user has published keeps
    working regardless of which one they last looked at — a link already given to
    somebody must not break because its owner switched the panel to a different
    one. That is why this enables rather than swapping: the alternative silently
    revokes URLs that are already out in the world.

    set_enabled remains the way to actually take a form out of service.
    """
    if kind not in PREFERRED_KINDS:
        raise ValueError(f"Unknown share kind: {kind!r}")
    await get_or_create(user_id)
    await db.execute(
        "UPDATE share_links SET preferred_kind = ?, enabled_token = 1, "
        "enabled_username = 1, enabled_slug = 1 WHERE user_id = ?",
        (kind, user_id),
    )


async def set_preferred_kind(user_id: int, kind: str) -> None:
    if kind not in PREFERRED_KINDS:
        raise ValueError(f"Unknown share kind: {kind!r}")
    await get_or_create(user_id)
    await db.execute("UPDATE share_links SET preferred_kind = ? WHERE user_id = ?", (kind, user_id))


async def set_post_link(user_id: int, *, kind: str | None = ..., endpoint: str | None = ...) -> None:
    """Choose what the tracker's announcement post embeds: which link form, and
    which calendar view it opens on.

    Each argument is skipped entirely when not passed, and an explicit None means
    "go back to following the share panel" (the preferred kind) or "leave the
    link bare" (no view in the query string). `...` rather than None as the
    sentinel is what makes those two cases distinguishable.
    """
    if kind is not ... and kind is not None and kind not in PREFERRED_KINDS:
        raise ValueError(f"Unknown share kind: {kind!r}")
    if endpoint is not ... and endpoint is not None and endpoint not in ENDPOINTS:
        raise ValueError(f"Unknown endpoint: {endpoint!r}")
    await get_or_create(user_id)
    columns: list[str] = []
    params: list = []
    if kind is not ...:
        columns.append("post_link_kind = ?")
        params.append(kind)
    if endpoint is not ...:
        columns.append("post_link_endpoint = ?")
        params.append(endpoint)
    if not columns:
        return
    params.append(user_id)
    await db.execute(f"UPDATE share_links SET {', '.join(columns)} WHERE user_id = ?", tuple(params))


def link_view_error(view: dict) -> str | None:
    """None when `view` is a usable set of share-link view params, else why not.

    Whitelisted value by value rather than passed through, because these end up
    in a URL handed to other people: an unrecognized key or value would be
    dropped silently by the page that receives it, leaving a link that quietly
    does not do what its author set it to do.
    """
    if not isinstance(view, dict):
        return "Expected an object of view options."
    for key, value in view.items():
        if key not in LINK_VIEW_PARAMS:
            return f"Unknown view option: {key}."
        text = str(value)
        if key == "endpoint" and text not in ENDPOINTS:
            return "Unknown calendar view."
        if key == "card" and text not in CARD_STYLES:
            return "Unknown card style."
        if key == "packing" and text not in DAY_PACKINGS:
            return "Unknown day packing."
        if key == "hidenw" and text not in ("0", "1"):
            return "Hide-not-watching must be 0 or 1."
        if key == "tz":
            try:
                ZoneInfo(text)
            except (ZoneInfoNotFoundError, ValueError):
                return "Unknown timezone."
    return None


def link_view(row) -> dict | None:
    """The stored view params for this row's generated link, or None for "hand
    out a bare link and let the page resolve the owner's own defaults"."""
    if row is None:
        return None
    try:
        stored = json.loads(row["link_view_json"] or "null")
    except (TypeError, ValueError):
        return None
    return stored if isinstance(stored, dict) and stored else None


async def set_link_view(user_id: int, view: dict | None) -> str | None:
    """Store (or clear, with None) the view params the generated link carries.

    This writes ONLY the link. The owner's own calendar preferences and the
    share page's fallback defaults are untouched — customizing a link someone
    else will open must not change how the owner's private calendar renders.
    Returns an error string, having written nothing, when the view is unusable.
    """
    if view is not None:
        if err := link_view_error(view):
            return err
        view = {key: str(view[key]) for key in LINK_VIEW_PARAMS if key in view}
    await get_or_create(user_id)
    payload = json.dumps(view) if view else None
    await db.execute("UPDATE share_links SET link_view_json = ? WHERE user_id = ?", (payload, user_id))
    return None


def _form_path(row, kind: str, username: str | None) -> str | None:
    """The path segment for one of the three link forms, or None when that form
    is switched off or has nothing to resolve to."""
    name = (username or "").strip()
    if kind == "token":
        return f"/s/{row['token']}" if row["enabled_token"] else None
    if kind == "username":
        return f"/u/{name}" if row["enabled_username"] and name else None
    if kind == "slug":
        return f"/c/{row['custom_slug']}" if row["enabled_slug"] and row["custom_slug"] else None
    return None


def build_url(row, username: str | None, base_url: str, kind: str,
              params: dict | None = None) -> str | None:
    """One share URL: origin + the chosen form's path + optional view params.

    The origin is only ever the operator's configured one, never the incoming
    request's, so a spoofed Host header cannot make this app publish somebody
    else's address as the place to find its calendar.
    """
    base = (base_url or "").rstrip("/")
    if row is None or not base:
        return None
    path = _form_path(row, kind, username)
    if path is None:
        return None
    query = urlencode({k: params[k] for k in LINK_VIEW_PARAMS if params and k in params})
    return f"{base}{path}?{query}" if query else f"{base}{path}"


def share_urls(row, username: str | None, base_url: str) -> dict[str, str | None]:
    """The three public URLs for a share row, bare — no view params. Each is None
    when it cannot be handed out: the form is disabled, has nothing to resolve
    to, or the instance has no configured public base URL."""
    return {kind: build_url(row, username, base_url, kind) for kind in PREFERRED_KINDS}


def generated_urls(row, username: str | None, base_url: str) -> dict[str, str | None]:
    """The three URLs as the Share panel hands them out — carrying whatever view
    params the owner set on the link, which is the only thing those params
    affect."""
    view = link_view(row)
    return {kind: build_url(row, username, base_url, kind, view) for kind in PREFERRED_KINDS}


def post_link_url(row, username: str | None, base_url: str) -> str | None:
    """The share URL to embed in the tracker's announcement post, or None when
    this account has no publishable link at all.

    Falls back down the chain post_link_kind -> preferred_kind -> any enabled
    form, because a stored choice can stop resolving after the fact (the user
    disables that form, or clears the slug it pointed at) and silently dropping
    the link from every future post is a worse answer than publishing the one
    that still works.
    """
    urls = share_urls(row, username, base_url)
    candidates = [row["post_link_kind"], row["preferred_kind"], *PREFERRED_KINDS] if row is not None else []
    for kind in candidates:
        if kind and urls.get(kind):
            return urls[kind]
    return None


def post_link_with_view(row, username: str | None, base_url: str) -> str | None:
    """post_link_url plus the owner's chosen calendar view as a query param, so
    an announcement can point at the premieres list while the owner's own share
    page defaults to something else."""
    urls = share_urls(row, username, base_url)
    kind = next(
        (k for k in ([row["post_link_kind"], row["preferred_kind"], *PREFERRED_KINDS] if row is not None else [])
         if k and urls.get(k)),
        None,
    )
    if kind is None:
        return None
    endpoint = row["post_link_endpoint"]
    params = {"endpoint": endpoint} if endpoint and endpoint in ENDPOINTS else None
    return build_url(row, username, base_url, kind, params)


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
    # A slug this same account retired is theirs to take back — switching away
    # and back must not permanently cost someone their own name. It stays blocked
    # for everybody else, which is the point of retiring it.
    retired = await db.fetch_one(
        "SELECT user_id FROM retired_identifiers WHERE kind = 'slug' AND value = ?", (candidate,),
    )
    if retired is not None and not (
        exclude_user_id is not None
        and retired["user_id"] is not None
        and int(retired["user_id"]) == exclude_user_id
    ):
        return "That slug is taken."
    row = await db.fetch_one("SELECT user_id FROM share_links WHERE custom_slug = ?", (candidate,))
    if row is not None and (exclude_user_id is None or int(row["user_id"]) != exclude_user_id):
        return "That slug is taken."
    return None


def _retire_slug(conn: db.Connection, user_id: int, slug: str | None) -> None:
    """SYNCHRONOUS. Block a slug this account is giving up from being claimed by
    anybody else.

    Changing a slug silently frees the old one, and `/c/<old-slug>` links are
    already out in the world by the time anyone changes it — so without this the
    next person to claim that name inherits an audience. Recorded WITH the
    account that gave it up, which is what lets the same owner take it back
    (slug_error) while it stays blocked for everyone else.
    """
    if not slug:
        return
    conn.execute(
        "INSERT OR REPLACE INTO retired_identifiers (kind, value, retired_at, user_id) "
        "VALUES ('slug', ?, ?, ?)",
        (slug, db.now(), user_id),
    )


async def set_custom_slug(user_id: int, slug: str | None) -> str | None:
    """Set or clear the user's custom slug. Returns an error string (making no
    change) when `slug` is unusable, otherwise None.

    The slug being replaced is retired, so a name whose links are already shared
    cannot be picked up by somebody else. Clearing the slug also disables the slug
    form: an enabled form with no slug set would have nowhere to resolve.
    """
    await get_or_create(user_id)
    candidate = (slug or "").strip().lower()

    def _work(conn: db.Connection) -> None:
        previous = conn.execute(
            "SELECT custom_slug FROM share_links WHERE user_id = ?", (user_id,),
        ).fetchone()
        old = previous["custom_slug"] if previous else None
        if old and old.lower() != candidate:
            _retire_slug(conn, user_id, old)
        if candidate:
            # Taking a name back that this account itself retired: drop the block
            # rather than leaving a row that contradicts the live slug.
            conn.execute(
                "DELETE FROM retired_identifiers WHERE kind = 'slug' AND value = ? AND user_id = ?",
                (candidate, user_id),
            )
            conn.execute(
                "UPDATE share_links SET custom_slug = ? WHERE user_id = ?", (candidate, user_id),
            )
        else:
            conn.execute(
                "UPDATE share_links SET custom_slug = NULL, enabled_slug = 0 WHERE user_id = ?",
                (user_id,),
            )

    if candidate:
        if err := await slug_error(candidate, exclude_user_id=user_id):
            return err
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
    """Resolve /s/<token>. The index lookup finds the row; the compare_digest
    below repeats the equality in constant time, so the one comparison this app
    makes against a secret is not a byte-at-a-time one (§4.1). Usernames and
    slugs are public identifiers and need no such treatment."""
    if not token:
        return None
    row = await db.fetch_one(
        _RESOLVE_SELECT.format(condition="sl.token = ? AND sl.enabled_token = 1"), (token,),
    )
    if row is None or not secrets.compare_digest(str(row["token"]), token):
        return None
    return row


async def resolve_by_username(username: str):
    candidate = (username or "").strip().lower()
    if not candidate:
        return None
    return await db.fetch_one(
        _RESOLVE_SELECT.format(condition="u.username = ? AND sl.enabled_username = 1"), (candidate,),
    )


async def resolve_by_slug(slug: str):
    """Resolve /c/<slug>, including slugs this owner has since moved on from.

    A retired slug KEEPS WORKING for the account that gave it up. Retiring exists
    so nobody ELSE can claim a name whose links are already circulating — and
    404ing those links would be the same harm the retirement was meant to prevent,
    just inflicted by us instead of by a stranger. So an old `/c/` link follows its
    owner rather than dying.

    A retired row whose `user_id` is NULL — a deleted account, or one written
    before the column existed — resolves to nothing and stays blocked. There is
    no owner left to follow.
    """
    candidate = (slug or "").strip().lower()
    if not candidate:
        return None
    row = await db.fetch_one(
        _RESOLVE_SELECT.format(condition="sl.custom_slug = ? AND sl.enabled_slug = 1"), (candidate,),
    )
    if row is not None:
        return row
    return await db.fetch_one(
        _RESOLVE_SELECT.format(
            condition=(
                "sl.enabled_slug = 1 AND sl.user_id = ("
                "  SELECT user_id FROM retired_identifiers"
                "   WHERE kind = 'slug' AND value = ? AND user_id IS NOT NULL)"
            ),
        ),
        (candidate,),
    )
