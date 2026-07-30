"""The FastAPI dependencies that express the app's five authorization levels.

Routes attach these at their own definitions (see app/authz.py, which resolves a
declared level to the dependency that enforces it).
"""
from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from fastapi import HTTPException, Request

from ..config import load_settings
from . import cookies, sessions
from .sessions import CurrentUser


class AuthLevel(str, Enum):
    """The authorization levels a route can require.

    Every route declares exactly one. PUBLIC is a declaration too, not an
    absence of one, so that a route which simply forgot to say can be told apart
    from a route that means it.
    """
    PUBLIC = "public"
    SESSION = "session"
    CALENDAR_APPROVED = "calendar_approved"
    DISTRAKT_APPROVED = "distrakt_approved"
    RANKER_APPROVED = "ranker_approved"
    ADMIN = "admin"


class AuthError(HTTPException):
    """401/403 carrying a machine-readable `reason`, so an HTML route can turn a
    refusal into the right redirect without re-deriving why it was refused."""

    def __init__(self, status_code: int, reason: str, message: str):
        super().__init__(status_code=status_code, detail={"reason": reason, "error": message})
        self.reason = reason


_NO_USER = object()


async def current_user(request: Request) -> CurrentUser | None:
    """The signed-in user, or None. Never raises — for public routes that render
    differently when somebody is signed in.

    Cached on `request.state`, so several dependencies on one route cost one
    query between them rather than one each.
    """
    cached = getattr(request.state, "auth_user", _NO_USER)
    if cached is not _NO_USER:
        return cached  # type: ignore[return-value]
    # Only the cookie policy is needed to read the session, never a credential, so
    # this skips decrypting the stored secrets. That keeps sign-in and the admin
    # dependency working even when a stored secret is sealed under a key the current
    # one cannot open — the state whose only fix is the admin recovery screen.
    settings = load_settings(open_secrets=False)
    user = await sessions.validate_session(cookies.read_session_cookie(request, settings))
    request.state.auth_user = user
    return user


async def require_session(request: Request) -> CurrentUser:
    """Signed in, whatever their approval state."""
    user = await current_user(request)
    if user is None:
        raise AuthError(401, "login_required", "Sign in to continue.")
    return user


async def require_calendar(request: Request) -> CurrentUser:
    """Signed in and approved for calendar access."""
    user = await require_session(request)
    if not user.calendar_approved:
        raise AuthError(403, "awaiting_approval", "Your account is awaiting admin approval.")
    return user


async def require_distrakt(request: Request) -> CurrentUser:
    """Signed in, approved for distrakt, and linked to Trakt.

    The Trakt link is not decoration: distrakt reads the requesting user's own
    watch history using their own token, so an account that only ever signed in
    with a password or with Plex has nothing for it to read.
    """
    user = await require_session(request)
    if not user.distrakt_approved:
        raise AuthError(403, "distrakt_not_approved", "distrakt access not yet approved.")
    if not user.has_trakt_identity:
        raise AuthError(403, "trakt_link_required", "Link your Trakt account to use distrakt.")
    return user


async def require_ranker(request: Request) -> CurrentUser:
    """Signed in and approved for the ranker.

    Deliberately only the grant, with no provider requirement attached. The
    ranker's baseline way of adding a title is a search run with the instance's
    own credential, so an account that has never linked anything is a complete
    one here — asking for a link would refuse accounts the feature works fine
    for.
    """
    user = await require_session(request)
    if not user.ranker_approved:
        raise AuthError(403, "ranker_not_approved", "Rankings access not yet approved.")
    return user


async def require_admin(request: Request) -> CurrentUser:
    """Signed in and an administrator."""
    user = await require_session(request)
    if not user.is_admin:
        raise AuthError(403, "admin_required", "Administrator access required.")
    return user


# Resolves a declared level to the dependency that enforces it. PUBLIC maps to
# None: a public route runs with no dependency at all and calls current_user()
# itself if it wants to know who is looking.
DEPENDENCY_FOR_LEVEL: dict[AuthLevel, Callable | None] = {
    AuthLevel.PUBLIC: None,
    AuthLevel.SESSION: require_session,
    AuthLevel.CALENDAR_APPROVED: require_calendar,
    AuthLevel.DISTRAKT_APPROVED: require_distrakt,
    AuthLevel.RANKER_APPROVED: require_ranker,
    AuthLevel.ADMIN: require_admin,
}
