"""Ending a linked service's grant when somebody's link to it is removed — and
which linked services are a way to sign in.

ONE TABLE OF THE SERVICES A PERSON CAN LINK TO THEIR ACCOUNT, what unlinking each
one does at that service, and whether it is a WAY IN. The account page's own
unlink and an administrator's both read it, so "which services can be unlinked",
"which of them revoke" and "which of them an account can sign in with" have one
answer. They used to be lists in two routes — the account page knew three
services and the admin page two, so an administrator could not remove somebody's
Simkl link at all — with an if per service name deciding the revocation.

THESE ARE NOT THE PROVIDER REGISTRY'S SOURCES. Plex is a way in and describes no
title; TMDB is a catalogue whose link is NOT a way in — linking it lets the
tracker write a person's TMDB watchlist and a board seed from their TMDB ratings,
and nobody signs in with it. A name missing from the table is REFUSED
(`is_linkable` False, `revoker` raising), never treated as one of the others.

WHY "A WAY IN" IS STATED HERE: an account with no password keeps a way back in
only through a link it can sign in with, so the last such link may not be removed
(identities.unlink_identity) — and a link that is not one must never count as
the last, or an account holding only a TMDB link beside its Trakt one could have
the Trakt one removed and be locked out.
"""
from __future__ import annotations

from types import ModuleType
from typing import NamedTuple


class Linked(NamedTuple):
    """One linkable service: the module in this package holding its
    `stored_access_token` and `revoke_token_value` (None where there is nothing
    to revoke), and whether linking it is a way to sign in."""
    revoker: str | None
    signs_in: bool


#   trakt  revokes through Trakt's /oauth/revoke.
#   simkl  revokes through Simkl's v2 revocation; its route module reads a
#          legacy-shaped stored token as nothing to revoke.
#   plex   is invalidated by the person on plex.tv — Plex publishes no
#          revocation endpoint — so unlinking is local-only.
#   tmdb   revokes through TMDB's DELETE /4/auth/access_token. Not a way in.
_LINKED: dict[str, Linked] = {
    "trakt": Linked("trakt_routes", signs_in=True),
    "simkl": Linked("simkl_routes", signs_in=True),
    "plex": Linked(None, signs_in=True),
    "tmdb": Linked("tmdb_routes", signs_in=False),
}

# Every service a person can link, and the ones among them an account can sign
# in with — in the table's order, which is the order the admin screen offers them.
LINKABLE = tuple(_LINKED)
LOGIN_PROVIDERS = tuple(name for name, linked in _LINKED.items() if linked.signs_in)


def is_linkable(provider: str) -> bool:
    return provider in _LINKED


def is_login_provider(provider: str) -> bool:
    """Whether a link to `provider` is a way to sign in. False for a name the
    table does not hold, as for one it holds that is not a way in."""
    linked = _LINKED.get(provider)
    return linked is not None and linked.signs_in


def revoker(provider: str) -> ModuleType | None:
    """The route module that stores and revokes `provider`'s token, or None for
    a provider with nothing to revoke. Raises ValueError for a name that is not
    linkable: the routes check first, so reaching here with one is a bug.

    IMPORTED WHEN ASKED, because those route modules read message constants out
    of app/auth/routes.py, which imports this one."""
    if provider not in _LINKED:
        raise ValueError(f"{provider!r} is not a service anybody links")
    name = _LINKED[provider].revoker
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
