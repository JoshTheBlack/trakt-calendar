"""Poster URL bookkeeping against the `show_posters` table. No downloads, no
Pillow — this module only knows about rows, never bytes.

Durable record of every poster URL the app has seen, so a lookup already paid
for is never paid for twice. GLOBAL rather than per user: most accounts on an
instance watch overlapping titles, so one shared record serves everyone and the
table does not grow with the user count.

MEDIA NAMESPACING. TMDB ids are namespaced per media type — movie 550 and TV
550 are different titles — so every row, lookup and cache key here is keyed on
the PAIR (media, tmdb), never tmdb alone.
"""
from __future__ import annotations

from .. import db

# Rows are tiny and are the fallback for exactly the moment a primary source
# fails; pruning them re-spends API calls already paid for. Three years.
POSTER_URL_RETENTION_SECONDS = 3 * 365 * 24 * 3600

# Preference order when picking a fallback URL from the registry: TMDB is the
# artwork source itself, so its own URL is trusted over a hotlinked one from
# elsewhere.
SOURCE_PREFERENCE = ("tmdb", "trakt")

# A registry URL that has failed this many times running is skipped in favour
# of trying a fresh provider lookup instead of retrying a source that keeps
# not working.
MAX_FAIL_COUNT = 3


def _upsert_one(conn: db.Connection, media: str, tmdb: int, source: str, url: str, ts: int) -> None:
    row = conn.execute(
        "SELECT url FROM show_posters WHERE media = ? AND tmdb = ? AND source = ?",
        (media, tmdb, source),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO show_posters "
            "(media, tmdb, source, url, first_seen_at, last_seen_at, fail_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (media, tmdb, source, url, ts, ts),
        )
    elif row["url"] == url:
        conn.execute(
            "UPDATE show_posters SET last_seen_at = ? "
            "WHERE media = ? AND tmdb = ? AND source = ?",
            (ts, media, tmdb, source),
        )
    else:
        # Upstream re-pointing artwork is normal; the new URL deserves a fresh
        # chance rather than inheriting the old one's failure history.
        conn.execute(
            "UPDATE show_posters SET url = ?, last_seen_at = ?, fail_count = 0, "
            "last_failed_at = NULL WHERE media = ? AND tmdb = ? AND source = ?",
            (url, ts, media, tmdb, source),
        )


async def record_poster_url(media: str, tmdb: int, source: str, url: str) -> None:
    """Upsert a single sighting of `url` for (media, tmdb, source). Best-effort:
    a write failure here must never fail the request that observed the URL,
    exactly as app/cache.py treats its own writes."""
    if not media or not tmdb or not url:
        return
    await record_poster_urls([(media, tmdb, source, url)])


async def record_poster_urls(sightings) -> None:
    """Batch form of record_poster_url for a page of calendar entries observed
    in one fetch, so a window with dozens of items costs one transaction rather
    than one per title. `sightings` is an iterable of (media, tmdb, source, url).
    Best-effort, same as the single-row form."""
    rows = [(m, t, s, u) for m, t, s, u in sightings if m and t and u]
    if not rows:
        return
    ts = db.now()

    def _work(conn: db.Connection) -> None:
        for media, tmdb, source, url in rows:
            _upsert_one(conn, media, tmdb, source, url, ts)

    try:
        await db.transaction(_work)
    except db.DatabaseError:
        pass


async def record_failure(media: str, tmdb: int, source: str) -> None:
    """A registry URL turned out to be dead (404, timeout, non-image body).
    Increments fail_count and stamps last_failed_at rather than deleting the
    row — the URL might come back, and a fresh sighting always resets this."""
    ts = db.now()
    await db.execute(
        "UPDATE show_posters SET fail_count = fail_count + 1, last_failed_at = ? "
        "WHERE media = ? AND tmdb = ? AND source = ?",
        (ts, media, tmdb, source),
    )


async def best_url(media: str, tmdb: int) -> tuple[str, str] | None:
    """The best (source, url) registry row for (media, tmdb), or None if there
    isn't one worth trying. Prefers 'tmdb' over 'trakt' over any other source,
    and skips a source whose fail_count has crossed MAX_FAIL_COUNT until a
    fresh sighting replaces it. The source is returned alongside the url so a
    caller whose download fails can call record_failure on the right row."""
    rows = await db.fetch_all(
        "SELECT source, url FROM show_posters "
        "WHERE media = ? AND tmdb = ? AND fail_count < ?",
        (media, tmdb, MAX_FAIL_COUNT),
    )
    if not rows:
        return None
    by_source = {row["source"]: row["url"] for row in rows}
    for source in SOURCE_PREFERENCE:
        if source in by_source:
            return source, by_source[source]
    # An unrecognized future provider: any surviving row beats none at all.
    return rows[0]["source"], rows[0]["url"]


async def sweep(now: int | None = None) -> int:
    """Delete rows older than the retention window. Returns how many were
    removed. Age is judged by last_seen_at, not first_seen_at, so a poster that
    is still being observed regularly never ages out just because it was first
    recorded long ago."""
    ts = db.now() if now is None else now
    cutoff = ts - POSTER_URL_RETENTION_SECONDS
    result = await db.execute("DELETE FROM show_posters WHERE last_seen_at <= ?", (cutoff,))
    return result.rowcount
