"""The per-user network -> emoji map the Discord posts render with.

This was app-wide in settings.json, which meant every tracker user shared one
map: importing a roster on any account registered its networks into the
operator's, and one person's emoji choices went out in everybody's Discord
posts. It is per-user for the same reason the roster and the watch history are.

THERE IS NO SEEDING. A new account starts with an empty map and the default
emoji, and fills it in as its own roster registers networks. Inheriting the
operator's map would be the same mistake in slower motion — one person's choices
arriving in another person's posts, just once instead of continuously. The map
travels with the tracker's own Backup export/restore instead, which is how a user
moves it between instances or accounts.
"""
from __future__ import annotations

import json

from .. import db

DEFAULT_EMOJI = ":tv:"


async def get_emoji_prefs(user_id: int) -> tuple[dict, str]:
    """This user's (network_emojis, default_emoji).

    An account with no row yet gets an empty map — deliberately, not as a
    fallback to anything app-wide.
    """
    row = await db.fetch_one(
        "SELECT network_emojis_json, default_network_emoji FROM distrakt_prefs "
        "WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return {}, DEFAULT_EMOJI
    try:
        emojis = json.loads(row["network_emojis_json"] or "{}")
    except ValueError:
        emojis = {}
    return (
        emojis if isinstance(emojis, dict) else {},
        row["default_network_emoji"] or DEFAULT_EMOJI,
    )


async def set_emoji_prefs(user_id: int, emojis: dict, default_emoji: str) -> None:
    """Replace this user's whole map. The editor sends every row it has, so a
    partial merge would make deleting an entry impossible."""
    await db.execute(
        "INSERT INTO distrakt_prefs (user_id, network_emojis_json, default_network_emoji, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "network_emojis_json = excluded.network_emojis_json, "
        "default_network_emoji = excluded.default_network_emoji, "
        "updated_at = excluded.updated_at",
        (user_id, json.dumps({str(k): str(v) for k, v in (emojis or {}).items()}),
         (default_emoji or DEFAULT_EMOJI).strip() or DEFAULT_EMOJI, db.now()),
    )


async def register_networks(user_id: int, networks) -> dict:
    """Add any unmapped network to THIS user's map with the default emoji, so it
    shows up in their editor ready to customize. Returns the resulting map."""
    emojis, default_emoji = await get_emoji_prefs(user_id)
    changed = False
    for net in networks:
        net = (net or "").strip()
        if net and net not in emojis:
            emojis[net] = default_emoji
            changed = True
    if changed:
        await set_emoji_prefs(user_id, emojis, default_emoji)
    return emojis
