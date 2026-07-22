"""Per-user calendar state: the "not watching" marks and the change-detection
fields, replacing app/state.py's shared per-(endpoint,year,month) JSON files.

The two halves are keyed differently ON PURPOSE.

"Not watching" is a fact about a SHOW and lives in not_watching_shows, keyed by
(user, item_id) alone. Marking a series premiere means you are not watching that
show — so its episodes stop appearing on All Episodes, its next season premiere
arrives already marked, and none of it comes back next month. Keying the mark by
the view it happened to be made in made the toggle mean "hide this cell", which
is not what it says.

Change detection ("N new since you last looked") is genuinely per view, because
it is about one month of one endpoint's list, so calendar_view_state keeps its
(user, endpoint, year, month) key.

Both are rows rather than documents, which is what turns a single toggle into a
delta — an INSERT or DELETE of one item_id — instead of the whole-array
read-modify-write that loses updates when a user has two tabs open.
"""
from __future__ import annotations

import json
import logging
import re
import shutil

from . import db
from .config import DATA_DIR
from .endpoints import ENDPOINTS

logger = logging.getLogger(__name__)

# The same slug-safe transform app/state.py used to build its filenames, so a
# state_*.json name can be mapped back to its endpoint key on import.
_SAFE = re.compile(r"[^a-z0-9]+")


def _safe_endpoint(key: str) -> str:
    return _SAFE.sub("_", key.lower()).strip("_")


# safe filename fragment -> endpoint key, e.g. "shows_new" -> "shows/new".
_SAFE_TO_ENDPOINT = {_safe_endpoint(key): key for key in ENDPOINTS}


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

async def not_watching_list(user_id: int) -> list[str]:
    """Every show this user has marked not-watching, oldest mark first.

    Not scoped to an endpoint or a month: a mark applies wherever that show turns
    up. The item ids are the calendar card's data-id, which the normalizer builds
    from the SHOW's ids on every show endpoint, so one list filters all of them.
    """
    rows = await db.fetch_all(
        "SELECT item_id FROM not_watching_shows WHERE user_id = ? "
        "ORDER BY created_at, item_id",
        (user_id,),
    )
    return [r["item_id"] for r in rows]


async def not_watching_ids(user_id: int) -> set[str]:
    """not_watching_list as a set, for the callers that only ever ask "is this
    one of them?"."""
    return set(await not_watching_list(user_id))


async def load_state(user_id: int, endpoint: str, year: int, month: int) -> dict:
    """What the calendar page needs on load, in the shape app/state.load_state
    returned so the front end is unchanged.

    `notWatching` is the user's whole global set; the change-detection fields are
    read for this one (endpoint, year, month).
    """
    not_watching = await not_watching_list(user_id)
    row = await db.fetch_one(
        "SELECT last_count, last_show_ids_json, history_json FROM calendar_view_state "
        "WHERE user_id = ? AND endpoint = ? AND year = ? AND month = ?",
        (user_id, endpoint, int(year), int(month)),
    )
    return {
        "notWatching": not_watching,
        "history": json.loads(row["history_json"]) if row and row["history_json"] else [],
        "lastCount": row["last_count"] if row else None,
        "lastShowIds": json.loads(row["last_show_ids_json"]) if row and row["last_show_ids_json"] else None,
    }


# ---------------------------------------------------------------------------
# writes — deltas (a single toggle) and whole-document (the drop-in for POST)
# ---------------------------------------------------------------------------

async def set_not_watching(user_id: int, item_id: str, not_watching: bool) -> None:
    """Mark or unmark one show for this user, everywhere. A delta, so two open
    tabs cannot lose each other's marks the way a whole-array save did."""
    if not_watching:
        await db.execute(
            "INSERT INTO not_watching_shows (user_id, item_id, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, item_id) DO NOTHING",
            (user_id, str(item_id), db.now()),
        )
    else:
        await db.execute(
            "DELETE FROM not_watching_shows WHERE user_id = ? AND item_id = ?",
            (user_id, str(item_id)),
        )


async def set_view_state(user_id: int, endpoint: str, year: int, month: int, *,
                         last_count: int | None, last_show_ids: list | None,
                         history: list | None = None) -> None:
    """Write the change-detection fields for one (endpoint, year, month). When
    history is None the stored history is left as it was, so the "N new since you
    last looked" write does not have to re-send the whole history each time."""
    def _work(conn: db.Connection) -> None:
        if history is None:
            row = conn.execute(
                "SELECT history_json FROM calendar_view_state "
                "WHERE user_id = ? AND endpoint = ? AND year = ? AND month = ?",
                (user_id, endpoint, int(year), int(month)),
            ).fetchone()
            history_json = row["history_json"] if row else None
        else:
            history_json = json.dumps(list(history))
        conn.execute(
            "INSERT INTO calendar_view_state "
            "(user_id, endpoint, year, month, last_count, last_show_ids_json, history_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, endpoint, year, month) DO UPDATE SET "
            "last_count = excluded.last_count, last_show_ids_json = excluded.last_show_ids_json, "
            "history_json = excluded.history_json, updated_at = excluded.updated_at",
            (
                user_id, endpoint, int(year), int(month),
                None if last_count is None else int(last_count),
                None if last_show_ids is None else json.dumps(list(last_show_ids)),
                history_json, db.now(),
            ),
        )

    await db.transaction(_work)


async def save_state(user_id: int, endpoint: str, year: int, month: int, payload: dict) -> None:
    """Write a whole state document for one (endpoint, year, month).

    The not-watching marks in it are ADDED to the user's global set rather than
    replacing it, because the payload only ever describes one view: a document
    listing July's Series Premieres says nothing about a show marked in August,
    and treating its absence as an unmark would delete marks the sender never
    saw. Unmarking is set_not_watching's job, which names the one show it means.
    """
    not_watching = [str(x) for x in (payload.get("notWatching") or [])]
    history = list(payload.get("history") or [])
    last_count = payload.get("lastCount")
    last_show_ids = payload.get("lastShowIds")

    def _work(conn: db.Connection) -> None:
        now = db.now()
        for item_id in not_watching:
            conn.execute(
                "INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id, item_id) DO NOTHING",
                (user_id, item_id, now),
            )
        conn.execute(
            "INSERT INTO calendar_view_state "
            "(user_id, endpoint, year, month, last_count, last_show_ids_json, history_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, endpoint, year, month) DO UPDATE SET "
            "last_count = excluded.last_count, last_show_ids_json = excluded.last_show_ids_json, "
            "history_json = excluded.history_json, updated_at = excluded.updated_at",
            (
                user_id, endpoint, int(year), int(month),
                None if last_count is None else int(last_count),
                None if last_show_ids is None else json.dumps(list(last_show_ids)),
                json.dumps(history), now,
            ),
        )

    await db.transaction(_work)


# ---------------------------------------------------------------------------
# legacy import
# ---------------------------------------------------------------------------

LEGACY_BACKUP_DIR = DATA_DIR / "legacy_state_backup"


def _parse_state_filename(name: str) -> tuple[str, int, int] | None:
    """Map a `state_<safe>_<year>_<month>.json` filename to (endpoint, year,
    month), or None when it isn't one of ours."""
    if not (name.startswith("state_") and name.endswith(".json")):
        return None
    stem = name[len("state_"):-len(".json")]
    parts = stem.rsplit("_", 2)
    if len(parts) != 3:
        return None
    safe, year_s, month_s = parts
    endpoint = _SAFE_TO_ENDPOINT.get(safe)
    if endpoint is None:
        return None
    try:
        return endpoint, int(year_s), int(month_s)
    except ValueError:
        return None


async def import_legacy_state(user_id: int) -> int:
    """Import the pre-accounts data/state_*.json files onto `user_id`,
    idempotently. Returns the number of files imported.

    Before reading anything, every state_*.json is copied verbatim into
    data/legacy_state_backup/ as a debugging safety net: if a first onboard
    imports the wrong thing, the operator can inspect the raw originals and retry.
    The backup is remade on every run so a retry stays idempotent, and the
    originals are NOT deleted here. data/legacy_state_backup/ is PERMANENT — the
    later step that removes the live state_*.json once the route swap is proven
    must never delete the backup directory.
    """
    files = sorted(DATA_DIR.glob("state_*.json"))
    if not files:
        return 0

    LEGACY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for path in files:
        try:
            shutil.copy2(path, LEGACY_BACKUP_DIR / path.name)  # overwrite on re-run
        except OSError:
            logger.warning("Could not back up legacy state file %s", path)

    parsed: list[tuple[str, int, int, dict]] = []
    for path in files:
        meta = _parse_state_filename(path.name)
        if meta is None:
            logger.warning("Skipping unrecognized legacy state file %s", path.name)
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read legacy state file %s", path.name)
            continue
        if isinstance(doc, dict):
            endpoint, year, month = meta
            parsed.append((endpoint, year, month, doc))

    if not parsed:
        return 0

    def _work(conn: db.Connection) -> None:
        now = db.now()
        for endpoint, year, month, doc in parsed:
            # Every file's marks land in the one global set — which is the
            # widening this import is now carrying out for free: a show the
            # operator turned off in one month's Series Premieres is a show they
            # are not watching, full stop.
            for item_id in (doc.get("notWatching") or []):
                conn.execute(
                    "INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(user_id, item_id) DO NOTHING",
                    (user_id, str(item_id), now),
                )
            last_count = doc.get("lastCount")
            last_show_ids = doc.get("lastShowIds")
            history = doc.get("history") or []
            conn.execute(
                "INSERT INTO calendar_view_state "
                "(user_id, endpoint, year, month, last_count, last_show_ids_json, history_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, endpoint, year, month) DO UPDATE SET "
                "last_count = excluded.last_count, last_show_ids_json = excluded.last_show_ids_json, "
                "history_json = excluded.history_json, updated_at = excluded.updated_at",
                (
                    user_id, endpoint, year, month,
                    None if last_count is None else int(last_count),
                    None if last_show_ids is None else json.dumps(list(last_show_ids)),
                    json.dumps(list(history)), now,
                ),
            )

    await db.transaction(_work)
    logger.info("Imported %d legacy calendar state file(s) onto user %s", len(parsed), user_id)
    return len(parsed)
