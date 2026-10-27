"""Poster URL bookkeeping against the `show_posters` table. No downloads, no
Pillow — this module only knows about rows, never bytes.

Durable record of every poster URL the app has seen, so a lookup already paid
for is never paid for twice. GLOBAL rather than per user: most accounts on an
instance watch overlapping titles, so one shared record serves everyone and the
table does not grow with the user count.

MEDIA NAMESPACING. TMDB ids are namespaced per media type — movie 550 and TV
550 are different titles — so every row, lookup and cache key here is keyed on
the PAIR (media, tmdb), never tmdb alone.

A SEASON'S OWN ARTWORK is `season_posters`, the same bookkeeping with the season
in its key (migration 44 says why it is a table of its own). A season with none
falls back to its show's, and `pick` is the one place that order is decided.

A TITLE NOTHING HAS A PICTURE FOR IS REMEMBERED TOO, as a `MISSING` row in
show_posters (`record_miss`): an empty address with its fail_count already at
the cap, so every reader that skips a failing source skips it as well, and a real
sighting of any source — a calendar fill naming the title — is found ahead of it
without anybody having to delete it. Without it, a title with no poster anywhere
would cost its one lookup again on every page that drew it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .. import db
from ..providers.tmdb import transport as tmdb_transport

# Rows are tiny and are the fallback for exactly the moment a primary source
# fails; pruning them re-spends API calls already paid for. Three years.
POSTER_URL_RETENTION_SECONDS = 3 * 365 * 24 * 3600

# EXCEPT A ROW TMDB SUPPLIED, which may not be kept past TMDB's own ceiling: its
# API terms forbid caching "any information obtained through or from TMDB" for
# longer than six months, and an address TMDB answered with is exactly that. The
# number is the TMDB client's (`MAX_CACHE_SECONDS`), not restated here, and the
# clock is `last_seen_at` like every other row's: a fresh answer from TMDB is
# re-obtaining the address, which restarts it.
TMDB_SOURCE = "tmdb"

# A registry URL that has failed this many times running is skipped in favour
# of trying a fresh provider lookup instead of retrying a source that keeps
# not working.
MAX_FAIL_COUNT = 3

# The `source` a known miss is filed under. Not a service: no sighting ever
# arrives under it, so nothing but `record_miss` writes it and nothing that reads
# addresses can mistake it for one (its url is empty and its fail_count at the
# cap). A later feature that lets somebody choose or upload a picture is a
# different record — a choice, not a sighting — and belongs ahead of `pick`'s
# order, not in this table's sources.
MISSING = "missing"


# Which table a sighting is filed in, by whether it names a season, and the
# columns that identify one row of it. Constants, never input, which is what
# makes naming them in SQL safe.
_SHOW = ("show_posters", ("media", "tmdb", "source"))
_SEASON = ("season_posters", ("media", "tmdb", "season", "source"))


def _upsert_one(conn: db.Connection, media: str, tmdb: int, source: str, url: str, ts: int,
                season: int | None = None, fail_count: int = 0) -> None:
    table, columns = _SHOW if season is None else _SEASON
    key = (media, tmdb, source) if season is None else (media, tmdb, season, source)
    match = " AND ".join(f"{column} = ?" for column in columns)
    row = conn.execute(f"SELECT url FROM {table} WHERE {match}", key).fetchone()
    if row is None:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}, url, first_seen_at, last_seen_at, "
            f"fail_count) VALUES ({', '.join('?' for _ in columns)}, ?, ?, ?, ?)",
            (*key, url, ts, ts, fail_count),
        )
    elif row["url"] == url:
        conn.execute(f"UPDATE {table} SET last_seen_at = ? WHERE {match}", (ts, *key))
    else:
        # Upstream re-pointing artwork is normal; the new URL deserves a fresh
        # chance rather than inheriting the old one's failure history.
        conn.execute(
            f"UPDATE {table} SET url = ?, last_seen_at = ?, fail_count = ?, "
            f"last_failed_at = NULL WHERE {match}",
            (url, ts, fail_count, *key),
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


async def urls_for(media: str, tmdb: int) -> dict[str, str]:
    """{source: url} for every registry row worth trying for (media, tmdb).

    THE CALLER SUPPLIES THE ORDER, which is why this returns the map rather than
    a winner: a caller resolving artwork for one VIEWER has an opinion, and it is
    not this module's business to hold it. Rows past MAX_FAIL_COUNT are left
    out — a source that keeps not working is not a candidate until a fresh
    sighting replaces it.
    """
    rows = await db.fetch_all(
        "SELECT source, url FROM show_posters "
        "WHERE media = ? AND tmdb = ? AND fail_count < ?",
        (media, tmdb, MAX_FAIL_COUNT),
    )
    return {str(row["source"]): str(row["url"]) for row in rows}


async def record_season_poster_urls(sightings) -> None:
    """The season registry's record_poster_urls: `sightings` is an iterable of
    (media, tmdb, season, source, url), one transaction for all of them, and
    best-effort for the same reason."""
    rows = [(m, t, n, s, u) for m, t, n, s, u in sightings if m and t and n is not None and u]
    if not rows:
        return
    ts = db.now()

    def _work(conn: db.Connection) -> None:
        for media, tmdb, season, source, url in rows:
            _upsert_one(conn, media, tmdb, source, url, ts, season=season)

    try:
        await db.transaction(_work)
    except db.DatabaseError:
        pass


async def record_season_failure(media: str, tmdb: int, season: int, source: str) -> None:
    """record_failure, for one season's own artwork."""
    await db.execute(
        "UPDATE season_posters SET fail_count = fail_count + 1, last_failed_at = ? "
        "WHERE media = ? AND tmdb = ? AND season = ? AND source = ?",
        (db.now(), media, tmdb, season, source),
    )


async def season_urls_for(media: str, tmdb: int, season: int) -> dict[str, str]:
    """urls_for, for one season's own artwork."""
    rows = await db.fetch_all(
        "SELECT source, url FROM season_posters "
        "WHERE media = ? AND tmdb = ? AND season = ? AND fail_count < ?",
        (media, tmdb, season, MAX_FAIL_COUNT),
    )
    return {str(row["source"]): str(row["url"]) for row in rows}


async def record_miss(media: str, tmdb: int) -> None:
    """Remember that a title's one lookup found no picture — see the module
    docstring for why a row, and why this shape of one."""
    if not media or not tmdb:
        return

    def _work(conn: db.Connection) -> None:
        _upsert_one(conn, media, tmdb, MISSING, "", db.now(), fail_count=MAX_FAIL_COUNT)

    try:
        await db.transaction(_work)
    except db.DatabaseError:
        pass


@dataclass
class Known:
    """What the registry holds for a batch of titles, read in one go (`known`):
    each title's addresses by source, each season's, and which titles were
    looked up and found to have none."""
    shows: dict[tuple[str, int], dict[str, str]] = field(default_factory=dict)
    seasons: dict[tuple[str, int, int], dict[str, str]] = field(default_factory=dict)
    missed: set[tuple[str, int]] = field(default_factory=set)
    # The addresses left out for failing: what a reader merging in another copy
    # of a service's address (the title store's own row of it) must leave out,
    # or a picture the registry has given up on would be drawn again from the
    # copy.
    capped: set[str] = field(default_factory=set)

    def address(self, media: str, tmdb: int, season: int | None,
                order: Iterable[str]) -> str:
        """The picture for one title (or season), in `order`: see `pick`."""
        found = self.addresses(media, tmdb, season, order)
        return found[0] if found else ""

    def addresses(self, media: str, tmdb: int, season: int | None,
                  order: Iterable[str]) -> list[str]:
        """That picture and every one behind it, in `order`: see `ranked`."""
        return ranked(self.shows.get((media, tmdb), {}),
                      self.seasons.get((media, tmdb, season), {}) if season is not None else {},
                      order)


def pick(show: Mapping[str, str], season: Mapping[str, str], order: Iterable[str]) -> str:
    """THE ONE ANSWER TO "WHICH PICTURE": the season's own artwork from the first
    source in `order` that has one, else the show's the same way, else "".

    `order` is the VIEWER's (sources.prefs' source_order —
    app/media/posters.py order_for), which is what makes a picture on one
    surface the picture on the next. Pure, so a later way of choosing a picture
    by hand has one place to stand in front of. It is `ranked`'s first answer.
    """
    found = ranked(show, season, order)
    return found[0] if found else ""


def ranked(show: Mapping[str, str], season: Mapping[str, str],
           order: Iterable[str]) -> list[str]:
    """Every address `pick` would consider, in the order it considers them — the
    season's own in `order`, then the show's — each once.

    WHAT STANDS BEHIND THE PICTURE A PAGE DRAWS. A recorded address can die at
    its host and nothing tells the server: Trakt replaced Coyote vs. Acme's
    poster, the address recorded for it began answering 404, and the tracker drew
    an empty slot while Simkl's still loaded. A page draws the first of these with
    the rest as the image proxy's fallbacks (providers.base.proxied_image), so the
    next one is what the viewer sees — the picture they would have been shown had
    the dead one never been recorded.
    """
    order = tuple(order)
    out: list[str] = []
    for urls in (season, show):
        for source in order:
            url = urls.get(source)
            if url and url not in out:
                out.append(url)
    return out


async def known(refs: Iterable[tuple[str, int, int | None]]) -> Known:
    """Everything the registry holds for `refs` — (media, tmdb, season or None)
    — in two reads however many titles there are, for a page that draws a
    picture per row and must not read per row. Failing addresses are left out
    as `urls_for` leaves them out."""
    refs = {(str(m), int(t), s) for m, t, s in refs if m and t}
    out = Known()
    tmdbs = sorted({t for _, t, _ in refs})
    if not tmdbs:
        return out
    marks = ", ".join("?" for _ in tmdbs)
    for row in await db.fetch_all(
            f"SELECT media, tmdb, source, url, fail_count FROM show_posters "
            f"WHERE tmdb IN ({marks})", tmdbs):
        pair = (str(row["media"]), int(row["tmdb"]))
        if row["source"] == MISSING:
            out.missed.add(pair)
        elif row["fail_count"] < MAX_FAIL_COUNT:
            out.shows.setdefault(pair, {})[str(row["source"])] = str(row["url"])
        elif row["url"]:
            out.capped.add(str(row["url"]))
    if any(s is not None for _, _, s in refs):
        for row in await db.fetch_all(
                f"SELECT media, tmdb, season, source, url, fail_count FROM season_posters "
                f"WHERE tmdb IN ({marks})", tmdbs):
            if row["fail_count"] >= MAX_FAIL_COUNT:
                out.capped.add(str(row["url"]))
                continue
            out.seasons.setdefault(
                (str(row["media"]), int(row["tmdb"]), int(row["season"])),
                {})[str(row["source"])] = str(row["url"])
    return out


async def sweep(now: int | None = None) -> int:
    """Delete rows older than the retention window, from both registries.
    Returns how many were removed. Age is judged by last_seen_at, not
    first_seen_at, so a poster that is still being observed regularly never
    ages out just because it was first recorded long ago. A known miss ages out
    the same way, which is when its title gets one more lookup. A row TMDB
    supplied goes at TMDB's six-month ceiling instead (TMDB_SOURCE)."""
    ts = db.now() if now is None else now
    cutoff = ts - POSTER_URL_RETENTION_SECONDS
    tmdb_cutoff = ts - tmdb_transport.MAX_CACHE_SECONDS
    removed = 0
    for table in ("show_posters", "season_posters"):
        result = await db.execute(
            f"DELETE FROM {table} WHERE last_seen_at <= ? "
            f"OR (source = ? AND last_seen_at <= ?)",
            (cutoff, TMDB_SOURCE, tmdb_cutoff))
        removed += result.rowcount
    return removed
