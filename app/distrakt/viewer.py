"""The tracker, acting as one person.

Everything the tracker reads — a progress record, a play count, a history feed —
is somebody's private viewing, so every call it makes has to authenticate as
THEM rather than as the operator whose tokens sit in settings.json. This module
is the one place that builds the settings object which does that.

IT IS ITS OWN MODULE BECAUSE TWO PACKAGES NEED IT. It was a private helper in
routes.py while the tracker's own pages were the only surface drawing a person's
viewing; the calendar's details panel now draws the same episode ticks, and a
second builder of this object would be a second place for "which credential does
this call carry" to be answered — which is the one question in the tracker where
a wrong answer hands somebody else's viewing to the wrong reader.
"""
from __future__ import annotations

import asyncio
import dataclasses

from ..auth import simkl_routes, trakt_routes
from ..config import load_settings


async def settings_for_user(user_id: int):
    """The app-wide settings with EVERY source's credential swapped for
    `user_id`'s own.

    The tracker reads one person's private watch history — their progress, their
    plays, their movies — so every call it makes has to authenticate as THEM. The
    tokens in settings.json belong to the operator and would hand every user the
    operator's viewing instead of their own. Everything else on the object (the
    network emoji map, the TMDB key, the genre/country strings) is genuinely
    app-wide and is carried through untouched.

    BOTH TOKENS RIDE ON THE ONE SETTINGS OBJECT rather than each source being
    handed a credential of its own. Each port already reads its own field off
    `settings` — that is what `is_configured` asks about — so a per-source
    credential object would be a second mechanism for something this one already
    expresses, and the SyncPort protocol would have to grow an argument for it.
    A user with no Simkl identity gets an empty Simkl token, `simkl_configured`
    goes false, and that port is simply never asked; the same has always been
    true of Trakt.

    The refresh token is cleared as well: nothing downstream refreshes, and
    leaving the operator's beside somebody else's access token would be a pairing
    that means nothing. (There is no Simkl counterpart to clear — Simkl issues no
    refresh token.) The access level guarantees SOME linked identity that can
    read a history, but a row can still hold an empty token, in which case that
    source's `*_configured` goes false and the handlers take their existing "not
    configured" path.

    The network->emoji map is per-user too, but it is NOT on this object: it was
    removed from Settings entirely when it stopped being app-wide, so the
    renderers take it as an argument (see routes._distrakt_month_payload). Keeping
    it off `settings` is deliberate — there is no longer an app-wide value for a
    caller to reach for by mistake.
    """
    trakt_token, simkl_token = await asyncio.gather(
        trakt_routes.access_token_for_user(user_id),
        simkl_routes.access_token_for_user(user_id),
    )
    return dataclasses.replace(
        load_settings(), trakt_access_token=trakt_token or "", trakt_refresh_token="",
        simkl_access_token=simkl_token or "",
    )
