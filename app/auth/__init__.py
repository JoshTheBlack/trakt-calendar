"""Auth core — password hashing, sessions, cookies, client IP, and the FastAPI
dependencies that express the app's five authorization levels.

This package owns the primitives; the routes attach the dependencies at their own
definitions (see app/authz.py and routes.py), and the provider login flows —
trakt.py and plex.py, driven by trakt_routes.py and plex_routes.py — build on
`linked_identities` through the helpers here.

Three things in here are security-load-bearing and are explained where they are
defined rather than here: the off-thread Argon2id hashing (passwords.py), the
two-clock session lifetime (sessions.py), and the cookie Secure policy
(cookies.py).

THIS MODULE IS THE PACKAGE'S PUBLIC SURFACE FOR THE AUTH CORE. Every core name a
caller outside app/auth/ uses is re-exported below, so `from . import auth`
followed by `auth.current_user(...)` reads the same as it did when this was one
file, and which submodule a name lives in stays an internal detail that can
change without touching a call site.

THE ROUTE MODULES AND THE TWO PROVIDER CLIENTS ARE NOT RE-EXPORTED, deliberately.
A router object and a login flow are things to mount and to run, not facts a
caller reads as `auth.X`, so main.py and the Settings screen import the module
they want on purpose (`from .auth import routes as auth_routes`,
`from .auth import trakt as trakt_auth`). Adding them here would make every
router reachable by two paths and put a second name on each one to keep in step.

Submodules call ACROSS each other through the module object — `sessions.
validate_session(...)`, never a name imported at module load. A name bound at
import time is bound to whichever copy existed then, so patching the defining
module in a test would silently miss the copy the caller holds, and the test
would pass while exercising the real thing.
"""
from __future__ import annotations

from .admin import (CannotDeleteSelf, LastAdmin, UserNotFound, WIPE_DATA_TABLES,
                    admin_set_username, delete_user, display_name_for, list_retired_identifiers,
                    list_sessions, list_users_overview, release_retired_identifier,
                    set_admin, set_calendar_approved, set_disabled, set_distrakt_approved,
                    set_ranker_approved, wipe_user_data)
from .cookies import (COOKIE_NAME, COOKIE_NAME_SECURE, browser_scheme, clear_session_cookie,
                      client_ip, detect_cookie_secure, parse_trusted_networks,
                      peer_is_trusted_proxy, read_session_cookie, request_is_https,
                      session_cookie_name, set_session_cookie, use_secure_cookie)
from .handshakes import (HANDSHAKE_COOKIE, HANDSHAKE_COOKIE_SECURE, HANDSHAKE_REJECTED,
                         HANDSHAKE_TTL_SECONDS, HandshakeError, clear_handshake_cookie,
                         consume_handshake, create_handshake, handshake_cookie_matches,
                         peek_handshake, read_handshake_cookie, set_handshake_cookie)
from .identities import (REFRESH_LEASE_SECONDS, AccountUnavailable, IdentityInUse,
                         IdentityWritesBlocked, LastLoginMethod, LoginOutcome, ProviderIdentity,
                         RegistrationRefused, claim_identity_refresh, find_identity,
                         insert_linked_identity, link_provider_identity, list_identities,
                         login_with_provider_identity, release_identity_refresh,
                         store_identity_tokens, unlink_identity)
from .invites import (create_invite, find_invite_by_token, invite_is_usable,
                      list_invite_redemptions, list_invites, redeem_invite, revoke_invite)
from .levels import (DEPENDENCY_FOR_LEVEL, AuthError, AuthLevel, current_user, require_admin,
                     require_calendar, require_distrakt, require_ranker, require_session)
from .lockout import (ATTEMPT_RETENTION_SECONDS, HANDSHAKE_MAX_ATTEMPTS, HANDSHAKE_WINDOW_SECONDS,
                      INVITE_MAX_ATTEMPTS, INVITE_WINDOW_SECONDS, LOGIN_IP_MAX_ATTEMPTS,
                      LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS, REGISTER_MAX_ATTEMPTS,
                      REGISTER_WINDOW_SECONDS, check_lockout, clear_attempts, cooldown_remaining,
                      handshake_start_limited, is_locked_out, rate_limited, record_attempt,
                      record_registration_attempt, registration_rate_limited,
                      sweep_login_attempts)
from .passwords import VerifyResult, burn_dummy_verify, hash_password, verify_password
from .prefs import get_user_prefs, insert_user_prefs, set_user_timezone, update_user_prefs
from .sessions import (SESSION_ABSOLUTE_SECONDS, SESSION_REFRESH_INTERVAL,
                       SESSION_SLIDING_SECONDS, CurrentUser, create_session, revoke_session,
                       revoke_user_session, revoke_user_sessions, sweep_expired_sessions,
                       validate_session)
from .users import (DISPLAY_NAME_MAX, IDENTIFIER_RE, MIN_PASSWORD_LENGTH, RESERVED_IDENTIFIERS,
                    any_users_exist, create_user, display_name_error, find_user_by_username,
                    get_user, identifier_error, identifier_is_retired, insert_user,
                    mark_logged_in, normalize_display_name, set_display_name, set_password,
                    update_password_hash, user_count, username_availability_error)

__all__ = [
    "ATTEMPT_RETENTION_SECONDS", "AccountUnavailable", "AuthError", "AuthLevel",
    "COOKIE_NAME", "COOKIE_NAME_SECURE", "CannotDeleteSelf", "CurrentUser",
    "DEPENDENCY_FOR_LEVEL", "DISPLAY_NAME_MAX",
    "HANDSHAKE_COOKIE", "HANDSHAKE_COOKIE_SECURE", "HANDSHAKE_MAX_ATTEMPTS",
    "HANDSHAKE_REJECTED", "HANDSHAKE_TTL_SECONDS", "HANDSHAKE_WINDOW_SECONDS",
    "HandshakeError", "IDENTIFIER_RE", "INVITE_MAX_ATTEMPTS", "INVITE_WINDOW_SECONDS",
    "IdentityInUse", "IdentityWritesBlocked", "LOGIN_IP_MAX_ATTEMPTS", "LOGIN_MAX_ATTEMPTS",
    "LOGIN_WINDOW_SECONDS", "LastAdmin", "LastLoginMethod", "LoginOutcome",
    "MIN_PASSWORD_LENGTH", "ProviderIdentity", "REFRESH_LEASE_SECONDS",
    "REGISTER_MAX_ATTEMPTS", "REGISTER_WINDOW_SECONDS", "RESERVED_IDENTIFIERS",
    "RegistrationRefused", "SESSION_ABSOLUTE_SECONDS", "SESSION_REFRESH_INTERVAL",
    "SESSION_SLIDING_SECONDS", "UserNotFound", "VerifyResult", "WIPE_DATA_TABLES",
    "admin_set_username", "any_users_exist", "browser_scheme", "burn_dummy_verify",
    "check_lockout", "claim_identity_refresh", "clear_attempts", "clear_handshake_cookie",
    "clear_session_cookie", "client_ip", "consume_handshake", "cooldown_remaining",
    "create_handshake", "create_invite", "create_session", "create_user", "current_user",
    "delete_user", "detect_cookie_secure", "display_name_error", "display_name_for",
    "find_identity", "find_invite_by_token", "find_user_by_username", "get_user",
    "get_user_prefs", "handshake_cookie_matches", "handshake_start_limited", "hash_password",
    "identifier_error", "identifier_is_retired", "insert_linked_identity", "insert_user",
    "insert_user_prefs", "invite_is_usable", "is_locked_out", "link_provider_identity",
    "list_identities", "list_invite_redemptions", "list_invites", "list_retired_identifiers",
    "list_sessions", "list_users_overview", "login_with_provider_identity", "mark_logged_in",
    "normalize_display_name", "parse_trusted_networks", "peek_handshake",
    "peer_is_trusted_proxy", "rate_limited", "read_handshake_cookie", "read_session_cookie",
    "record_attempt", "record_registration_attempt", "redeem_invite",
    "registration_rate_limited", "release_identity_refresh",
    "release_retired_identifier",
    "request_is_https", "require_admin", "require_calendar", "require_distrakt",
    "require_ranker", "require_session", "revoke_invite", "revoke_session",
    "revoke_user_session", "revoke_user_sessions", "session_cookie_name", "set_admin",
    "set_calendar_approved", "set_disabled", "set_display_name", "set_distrakt_approved",
    "set_handshake_cookie", "set_password", "set_ranker_approved", "set_session_cookie",
    "set_user_timezone", "store_identity_tokens", "sweep_expired_sessions",
    "sweep_login_attempts", "unlink_identity", "update_password_hash", "update_user_prefs",
    "user_count", "username_availability_error", "validate_session", "verify_password",
    "wipe_user_data",
]
