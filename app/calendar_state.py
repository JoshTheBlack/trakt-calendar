"""Per-user calendar state: the "not watching" marks and the change-detection
fields, replacing app/state.py's shared per-(endpoint,year,month) JSON files with
rows keyed additionally by user.

app/state.py wrote one JSON document per (endpoint, year, month) shared by
everyone. Here each such document becomes rows in calendar_not_watching (one per
marked item) plus one row in calendar_view_state (the last_count / last_show_ids
/ history a viewer's change detection needs). Rows rather than a document is what
turns a single toggle into a delta — an INSERT or DELETE of one item_id — instead
of the whole-array read-modify-write that loses updates when a user has two tabs
open.

This module provides the storage functions only. The calendar route keeps using
app/state.py until it is switched over to load_state / save_state / the delta
helpers here.
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

# Endpoints whose main-calendar not-watching decisions the distrakt roster is
# built from. Kept here so the roster reader below and its future per-user caller
# agree on the source set.
ROSTER_ENDPOINTS = ("shows/new", "shows/premieres")

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

async def not_watching_list(user_id: int, endpoint: str, year: int, month: int) -> list[str]:
    """This user's not-watching item ids for one (endpoint, year, month), oldest
    mark first."""
    rows = await db.fetch_all(
        "SELECT item_id FROM calendar_not_watching "
        "WHERE user_id = ? AND endpoint = ? AND year = ? AND month = ? "
        "ORDER BY created_at, item_id",
        (user_id, endpoint, int(year), int(month)),
    )
    return [r["item_id"] for r in rows]


async def load_state(user_id: int, endpoint: str, year: int, month: int) -> dict:
    """The whole state for one (endpoint, year, month), in the exact shape
    app/state.load_state returned, so the route can swap to this with no change
    to what it hands the front end."""
    not_watching = await not_watching_list(user_id, endpoint, year, month)
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


async def not_watching_ids(user_id: int, year: int, month: int) -> set[str]:
    """Union of this user's main-calendar not-watching ids across the roster's
    source endpoints (shows/new + shows/premieres) for one year/month — the
    per-user replacement for the shared distrakt roster read."""
    ids: set[str] = set()
    for endpoint in ROSTER_ENDPOINTS:
        ids.update(await not_watching_list(user_id, endpoint, year, month))
    return ids


# ---------------------------------------------------------------------------
# writes — deltas (a single toggle) and whole-document (the drop-in for POST)
# ---------------------------------------------------------------------------

async def set_not_watching(user_id: int, endpoint: str, year: int, month: int,
                           item_id: str, not_watching: bool) -> None:
    """Toggle one item. A delta, so two open tabs cannot lose each other's marks
    the way a whole-array save did."""
    if not_watching:
        await db.execute(
            "INSERT INTO calendar_not_watching "
            "(user_id, endpoint, year, month, item_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, endpoint, year, month, item_id) DO NOTHING",
            (user_id, endpoint, int(year), int(month), str(item_id), db.now()),
        )
    else:
        await db.execute(
            "DELETE FROM calendar_not_watching "
            "WHERE user_id = ? AND endpoint = ? AND year = ? AND month = ? AND item_id = ?",
            (user_id, endpoint, int(year), int(month), str(item_id)),
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
    """Replace the whole state for one (endpoint, year, month), mirroring
    app/state.save_state's whole-document semantics — a drop-in for the current
    POST handler. The delta helpers above are the better shape for a single
    toggle; this exists so the route can be switched over without also being
    reshaped in the same step.
    """
    not_watching = [str(x) for x in (payload.get("notWatching") or [])]
    history = list(payload.get("history") or [])
    last_count = payload.get("lastCount")
    last_show_ids = payload.get("lastShowIds")

    def _work(conn: db.Connection) -> None:
        now = db.now()
        conn.execute(
            "DELETE FROM calendar_not_watching "
            "WHERE user_id = ? AND endpoint = ? AND year = ? AND month = ?",
            (user_id, endpoint, int(year), int(month)),
        )
        for item_id in not_watching:
            conn.execute(
                "INSERT INTO calendar_not_watching "
                "(user_id, endpoint, year, month, item_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, endpoint, year, month, item_id) DO NOTHING",
                (user_id, endpoint, int(year), int(month), item_id, now),
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
            for item_id in (doc.get("notWatching") or []):
                conn.execute(
                    "INSERT INTO calendar_not_watching "
                    "(user_id, endpoint, year, month, item_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, endpoint, year, month, item_id) DO NOTHING",
                    (user_id, endpoint, year, month, str(item_id), now),
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
