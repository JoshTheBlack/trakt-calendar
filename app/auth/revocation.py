"""Ending a sign-in service's grant when somebody's link to it is removed.

ONE TABLE OF THE SERVICES A PERSON CAN SIGN IN WITH, and what unlinking each one
does at that service. The account page's own unlink and an administrator's both
read it, so "which providers can be unlinked" and "which of them revoke" have one
answer. They used to be two lists in two routes — the account page knew three
services and the admin page two, so an administrator could not remove somebody's
Simkl link at all — with an if per service name deciding the revocation.

THESE ARE LOGIN PROVIDERS, NOT CATALOGUE SOURCES, and the set is deliberately
not the provider registry: Plex is a way in and describes no title, and a
catalogue service is not a way in. A name missing from the table is REFUSED
(`is_login_provider` False, `revoker` raising), never treated as one of the
others.
"""
from __future__ import annotations

from types import ModuleType

# provider -> the module in this package holding its `stored_access_token` and
# `revoke_token_value`, or None where there is nothing to revoke.
#   trakt  revokes through Trakt's /oauth/revoke.
#   simkl  revokes through Simkl's v2 revocation; its route module reads a
#          legacy-shaped stored token as nothing to revoke.
#   plex   is invalidated by the person on plex.tv — Plex publishes no
#          revocation endpoint — so unlinking is local-only.
_REVOKER_MODULES: dict[str, str | None] = {
    "trakt": "trakt_routes",
    "simkl": "simkl_routes",
    "plex": None,
}

LOGIN_PROVIDERS = tuple(_REVOKER_MODULES)


def is_login_provider(provider: str) -> bool:
    return provider in _REVOKER_MODULES


def revoker(provider: str) -> ModuleType | None:
    """The route module that stores and revokes `provider`'s token, or None for
    a provider with nothing to revoke. Raises ValueError for a name that is not a
    login provider: the routes check first, so reaching here with one is a bug.

    IMPORTED WHEN ASKED, because those route modules read message constants out
    of app/auth/routes.py, which imports this one."""
    if provider not in _REVOKER_MODULES:
        raise ValueError(f"{provider!r} is not a provider anybody signs in with")
    name = _REVOKER_MODULES[provider]
    if name is None:
        return None
    from importlib import import_module
    return import_module(f"{__package__}.{name}")


async def stored_token(provider: str, user_id: int) -> str | None:
    """Read BEFORE an unlink — the token lives on the row the unlink deletes."""
    module = revoker(provider)
    return await module.stored_access_token(user_id) if module else None


async def revoke(provider: str, token: str | None) -> str | None:
    """Spent only after an unlink actually happened. The warning to show when the
    service could not be told, or None."""
    module = revoker(provider)
    return await module.revoke_token_value(token) if module else None
