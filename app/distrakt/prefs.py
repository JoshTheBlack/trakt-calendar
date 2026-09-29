"""The per-user network -> emoji map the Discord posts render with.

This was app-wide in settings.json, which meant every tracker user shared one
map: importing a roster on any account registered its networks into the
operator's, and one person's emoji choices went out in everybody's Discord
posts. It is per-user for the same reason the roster and the watch history are.

THERE IS NO SEEDING. A new account starts with an empty map and the default
emoji. Inheriting the operator's map would be the same mistake in slower motion
— one person's choices arriving in another person's posts, just once instead of
continuously. The map travels with the tracker's own Backup export/restore
instead, which is how a user moves it between instances or accounts.

THE MAP HOLDS ONLY WHAT SOMEBODY CHOSE. A network with no entry posts as the
default, so an entry equal to the default says nothing an absent one does not —
and it is worse than nothing, because it stops following the default when the
default changes. Networks used to be filed here AS the default the moment a
roster named them, so a separate editor had something to list; the codes are
edited on the tracker's own rows now, which already name every network the
month has, so nothing is registered and every save drops what equals the
default.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .. import db

DEFAULT_EMOJI = ":tv:"

# How long a code may be. A custom Discord emoji written out in full,
# `<a:name:snowflake>`, is under sixty characters; the cap is only there so a
# field cannot be used to park a page of text in somebody's map.
MAX_CODE_LENGTH = 100
# A network name is the key, and it is only ever one the page drew.
MAX_NETWORK_LENGTH = 200


def _decode(row) -> tuple[dict, str]:
    """(network_emojis, default_emoji) out of a distrakt_prefs row, or the empty
    map and the app's default for an account with no row yet — deliberately,
    not as a fallback to anything app-wide."""
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


_SELECT = ("SELECT network_emojis_json, default_network_emoji FROM distrakt_prefs "
           "WHERE user_id = ?")
_UPSERT = ("INSERT INTO distrakt_prefs (user_id, network_emojis_json, default_network_emoji, updated_at) "
           "VALUES (?, ?, ?, ?) "
           "ON CONFLICT(user_id) DO UPDATE SET "
           "network_emojis_json = excluded.network_emojis_json, "
           "default_network_emoji = excluded.default_network_emoji, "
           "updated_at = excluded.updated_at")


def _without_defaults(emojis: dict, *defaults: str) -> dict:
    """The map with every entry that only restates a default taken out."""
    return {str(network): str(code) for network, code in emojis.items()
            if str(code) not in defaults}


def _write(conn: sqlite3.Connection, user_id: int, emojis: dict, default_emoji: str) -> None:
    conn.execute(_UPSERT, (user_id, json.dumps(emojis), default_emoji, db.now()))


async def get_emoji_prefs(user_id: int) -> tuple[dict, str]:
    """This user's (network_emojis, default_emoji)."""
    return _decode(await db.fetch_one(_SELECT, (user_id,)))


async def set_emoji_prefs(user_id: int, emojis: dict, default_emoji: str) -> None:
    """Replace this user's whole map — for a caller that holds all of it."""
    default_emoji = (default_emoji or DEFAULT_EMOJI).strip() or DEFAULT_EMOJI
    await db.execute(_UPSERT, (
        user_id, json.dumps({str(k): str(v) for k, v in (emojis or {}).items()}),
        default_emoji, db.now()))


def clean_code(value) -> str:
    """A code as it is stored: stripped and capped. No format check — a plain
    unicode emoji is as valid a code as `:name:` is."""
    return str(value or "").strip()[:MAX_CODE_LENGTH]


async def set_network_emoji(user_id: int, network: str, code: str) -> tuple[dict, str]:
    """Set ONE network's code and return the map as it now stands.

    ONE KEY, READ AND WRITTEN IN ONE TRANSACTION, not the whole map replaced. The
    codes are edited on the rows, one at a time, and two tabs open on the tracker
    each hold the map as it was when they were drawn — so a save that sent its
    whole copy back would undo whatever the other tab saved in between.

    AN EMPTY CODE, OR THE DEFAULT, REMOVES THE ENTRY: the network then posts as
    the default and follows it when it changes. Every other entry that equals
    the default goes in the same write (see the module's note).
    """
    network, code = network.strip(), clean_code(code)

    def _work(conn: sqlite3.Connection) -> tuple[dict, str]:
        emojis, default_emoji = _decode(conn.execute(_SELECT, (user_id,)).fetchone())
        emojis = {**emojis, network: code}
        emojis = _without_defaults(emojis, "", default_emoji)
        _write(conn, user_id, emojis, default_emoji)
        return emojis, default_emoji

    return await db.transaction(_work)


async def set_default_emoji(user_id: int, code: str) -> tuple[dict, str]:
    """Set the code a network with none of its own posts as, and return the map
    as it now stands. Empty means the app's own default.

    AN ENTRY EQUAL TO THE DEFAULT BEING REPLACED GOES TOO. It is drawn on the page
    as "no code of its own" — dimmed, like a network with no entry — so it has to
    behave like one and move with the default. Left in, the networks a roster
    once filed as the default would all keep the old code the moment somebody
    changed it.
    """
    code = clean_code(code) or DEFAULT_EMOJI

    def _work(conn: sqlite3.Connection) -> tuple[dict, str]:
        emojis, was = _decode(conn.execute(_SELECT, (user_id,)).fetchone())
        emojis = _without_defaults(emojis, "", was, code)
        _write(conn, user_id, emojis, code)
        return emojis, code

    return await db.transaction(_work)


def own_code(emojis: dict, default_emoji: str, network: str) -> str:
    """The network's own code, or "" where it posts as the default. An entry
    equal to the default counts as none — see the module's note."""
    code = str(emojis.get(network or "", ""))
    return "" if code == default_emoji else code


def network_ref(network: str) -> str:
    """A network, as something an out-of-band swap can address.

    A DIGEST RATHER THAN THE NAME, for the reason providers.base.Item.airing_key
    gives: it goes into an `hx-swap-oob` selector, which htmx splits at its first
    colon and only when it holds no space, and which a quote would end early —
    and network names carry spaces, quotes and "+" ("Paramount+ with Showtime").
    Sixteen hex characters cannot hold any of them. Not a secret and not stable
    across releases; it only has to be the same on both sides of one request.
    """
    return hashlib.blake2s((network or "").encode("utf-8"), digest_size=8).hexdigest()
