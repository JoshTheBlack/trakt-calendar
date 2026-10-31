"""The app's settings, acting as one signed-in person.

Everything read about one person — a progress record, a play count, a history
feed, and now a catalogue search Simkl answers only with that person's own token
— has to authenticate as THEM rather than as the instance. This module is the
one place that builds the settings object which does that.

IT LIVES IN auth/ BECAUSE EVERY FEATURE NEEDS IT AND IT NEEDS ONLY auth. It
reads nothing but this package's own token functions, and it started life in the
tracker only because the tracker was the first surface drawing a person's
viewing. The calendar's details panel, its search and its card completion ask
the same question now, and so will the rankings board's search; each of them
reaching into the tracker for it made the tracker a dependency of features that
have nothing to do with it. auth is the layer every feature may depend on, and a
second builder of this object would be a second place for "which credential does
this call carry" to be answered — the one question where a wrong answer hands
somebody else's viewing to the wrong reader.
"""
from __future__ import annotations

import asyncio
import dataclasses

from ..config import load_settings
from . import simkl_routes, trakt_routes


async def settings_for_user(user_id: int):
    """The app-wide settings with EVERY source's credential swapped for
    `user_id`'s own.

    A caller reading one person's private watch history — their progress, their
    plays, their movies — has to authenticate as THEM. The tokens an operator
    once stored belong to the operator and would hand every user the operator's
    viewing instead of their own. Everything else on the object (the TMDB key,
    the genre/country strings, each service's client id) is genuinely app-wide
    and is carried through untouched.

    BOTH TOKENS RIDE ON THE ONE SETTINGS OBJECT rather than each source being
    handed a credential of its own. Each port already reads its own field off
    `settings` — that is what `is_configured` asks about — so a per-source
    credential object would be a second mechanism for something this one already
    expresses, and the SyncPort protocol would have to grow an argument for it.
    A user with no Simkl identity gets an empty Simkl token, `simkl_configured`
    goes false, and that port is simply never asked; the same has always been
    true of Trakt.

    Trakt's refresh token is cleared as well: nothing downstream on this object
    refreshes with it (that happens once, up front, inside
    trakt_routes.access_token_for_user — this function only ever hands out the
    resulting access token), and leaving the operator's beside somebody else's
    access token would be a pairing that means nothing. Simkl's refresh token
    never reaches this object at all for the same reason: simkl_routes'
    equivalent already refreshed and returned only the access token, and
    Settings has no `simkl_refresh_token` field for it to occupy. An access
    level can guarantee SOME linked identity, but a row can still hold an empty
    token, in which case that source's `*_configured` goes false and the caller
    takes its existing "not configured" path.

    The tracker's network->emoji map is per-user too, but it is NOT on this
    object: it was removed from Settings entirely when it stopped being
    app-wide, so the tracker's renderers take it as an argument. Keeping it off
    `settings` is deliberate — there is no longer an app-wide value for a caller
    to reach for by mistake.
    """
    trakt_token, simkl_token = await asyncio.gather(
        trakt_routes.access_token_for_user(user_id),
        simkl_routes.access_token_for_user(user_id),
    )
    return dataclasses.replace(
        await load_settings(), trakt_access_token=trakt_token or "", trakt_refresh_token="",
        simkl_access_token=simkl_token or "",
    )
