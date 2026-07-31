"""Finished share cards on disk: how one is addressed, and what bounds how many
are kept.

CONTENT-ADDRESSED, AND THAT IS THE ENTIRE INVALIDATION STORY. The key hashes
everything that can change a pixel — the renderer's version, whose calendar it
is, the month, the count, the exact titles drawn, the dates and premiere marks
drawn under them, and the exact poster files behind them. A month that gained a
show hashes differently, so it renders; a
refresh that changed nothing hashes the same, so it does not. There is no
invalidation table, no per-view bookkeeping, and no case anyone can forget,
because the picture's inputs ARE its address.
Note what is deliberately NOT in the key: the time the calendar data was last
refreshed. That moves several times a day whether or not a single title changed,
so keying on it would mint a new address — and burn a render — for a month whose
picture is identical. The content is the signal; the clock is not.
Note also what the key cannot see: a change that does not reach the drawn card,
such as a title's artwork changing upstream, keeps the cached image until
something else moves. For a link preview that is the right trade.

ITS OWN DIRECTORY, not one of the caches this app already has. The shared blob
table stores JSON through zlib, which a JPEG gains nothing from and would have to
be base64'd into; and an account's generated-images directory is capped by FILE
COUNT for the ranker's exports, so dropping cards in there would let a crawler
quietly evict pictures a user made.

RETENTION IS BY AGE FIRST, WITH A BYTE BUDGET AS THE BACKSTOP, swept on the
heartbeat rather than on write — a directory walk on the request path would be a
cost paid by the very requests this cache exists to make cheap. Bytes rather
than a file count because that is the unit every other cache in this app is
bounded in, and it is the unit the question is actually asked in: how much disk
may this feature use.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from . import share_card
from .config import DATA_DIR

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "share_cards"

# How long a rendered card may sit unused before the sweep drops it. Long,
# because the file is addressed by its CONTENT: an old file is either still
# exactly right (and re-serving it is free) or already unreachable (and this is
# the only thing that will ever remove it). This is NOT a staleness window — see
# the module docstring.
RETENTION_DAYS = 90
RETENTION_SECONDS = RETENTION_DAYS * 24 * 60 * 60

# Half a sha256, which is the same trade ranker_export.render_key makes: far more
# than enough to make a collision between two cards on one instance a
# non-event, short enough to read in a directory listing.
KEY_LENGTH = 32


def render_key(card: share_card.Card, *, owner_id: int, month: int) -> str:
    """An address for exactly this picture.

    Everything that reaches the canvas goes in, deliberately: an input the key
    omits is an input that can change while the old image keeps being handed
    back. The renderer's own version is in here for the same reason — without it
    a layout change would keep serving yesterday's geometry to everyone who
    unfurled a link before it, and unfurlers cache hard.

    THE VIEW OPTIONS THEMSELVES ARE NOT IN THE KEY, and that is not an omission:
    the calendar view, the timezone, the marks filter and the network filter are
    already fully reflected in the titles and the count they produced. Hashing
    both the cause and the effect would only make the key longer, and if two
    different views really do produce the identical picture they SHOULD share one
    file.

    Poster files are identified by their media directory and filename — which is
    to say by the (media, tmdb) pair they are filed under — rather than by their
    absolute path, so moving the data directory does not invalidate every card
    on the instance.
    """
    payload = {
        "renderer": share_card.RENDERER_VERSION,
        # By id, never by name: the card deliberately draws no name at all, so
        # there is no name to key on. The id is what says whose month this is.
        "owner": owner_id,
        "year": card.year,
        "month": month,
        "month_label": card.month_label,
        "count": card.count,
        # EVERYTHING A TILE DRAWS, not just its picture: the title, the poster,
        # the air date beneath it and whether it is marked as a series premiere.
        # The last two are the easiest terms in this whole key to forget, and
        # forgetting one is invisible — the card keeps rendering, it just keeps
        # rendering the OLD date, or the old marking, for as long as the file
        # survives. A tile that starts or stops being a premiere changes the
        # picture, so it changes the address.
        "tiles": [[tile.title, tile.poster.parent.name, tile.poster.name,
                   tile.date_label, tile.is_premiere]
                  for tile in card.tiles],
        # The avatar is drawn, so changing it changes a pixel. Hashed rather than
        # embedded because the key is a hash of a small JSON document and an
        # image does not belong inside one.
        "avatar": hashlib.sha256(card.avatar).hexdigest() if card.avatar else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:KEY_LENGTH]


def cache_path(key: str) -> Path:
    """Where the card with this key lives.

    One flat directory, and the only thing that ever becomes a path component is
    the server-generated hex above — no username, no slug, no query value. That
    is what makes the "can a crafted identifier reach the filesystem" question
    have no surface to ask about rather than a check to get right.
    """
    return CACHE_DIR / f"{key}.jpg"


def cached_render(path: Path) -> bytes | None:
    """SYNCHRONOUS. A previously rendered card, or None.

    A cache that will not read is a cache MISS, never an error: the render that
    would replace it is the same render that produced it.
    """
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


def store_render(path: Path, payload: bytes) -> None:
    """SYNCHRONOUS. Keep a finished card.

    Best-effort throughout: failing to cache an image the caller is already
    holding is not a reason to fail the request it is about to answer. Nothing is
    trimmed here — the heartbeat owns that, see `sweep`.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except OSError:
        logger.warning("Could not cache a share card at %s", path, exc_info=True)


def sweep(max_bytes: int, *, now: float | None = None) -> int:
    """SYNCHRONOUS. Drop cards past the retention window, then evict oldest-first
    until the directory is back under `max_bytes`. Returns how many files went.

    Age is the rule that fires in practice and the budget is the runaway guard,
    which is why they run in that order. Pure filesystem walking, so the caller
    runs it in a worker thread rather than on the event loop — see app/main.py.
    """
    if not CACHE_DIR.exists() or max_bytes < 0:
        return 0
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS

    entries: list[tuple[float, int, Path]] = []
    total = 0
    removed = 0
    for path in CACHE_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size

    if total <= max_bytes:
        return removed
    entries.sort(key=lambda entry: entry[0])
    for _mtime, size, path in entries:
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    return removed
