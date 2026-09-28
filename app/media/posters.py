"""Which picture a title has, and — for the two things that composite real bytes
— the tile of it on disk. No DB schema knowledge beyond calling
app/media/artwork.py for the URL registry.

TWO HALVES. `find_address` / `ensure_addresses` answer with an ORIGIN ADDRESS and
write nothing to disk: a page draws that through the image proxy, which is how
the rankings board, its details panel and the tracker's rows show pictures.
`ensure_poster` / `ensure_posters` / `cached_poster` are the TILES, 500x750
files the share card and a board's exported image composite — written when one
of those is being made, and only for what it draws. Both halves resolve through
the same registry in the same order, so an export shows the picture its board
did.

Mirrors app/media/logos.py's shape: disk cache, negative markers, best-effort
degradation. Never called on a render path — warming is explicit, and a render
resolves whatever is already known.

STORAGE, KEYED ON (media, tmdb, SOURCE). TMDB ids are namespaced per media type
— movie 550 and TV 550 are different titles — and the SOURCE is here because two
viewers with opposite artwork precedence want different pictures for the same
title. A key that could not tell them apart would be a per-viewer value reaching
shared storage, which is exactly what the calendar's own storage rules forbid.
    DATA_DIR/posters/<media>/<tmdb>.<source>.jpg       500x750, from the proxy
    DATA_DIR/posters/<media>/<tmdb>.s<N>.<source>.jpg  season N's own
    DATA_DIR/posters/<media>/<tmdb>.<source>.none      negative marker

A SEASON IS TRIED FOR ITS OWN ARTWORK FIRST — the season registry, which a
season list somebody already read filled (only Trakt publishes season artwork,
measured) — and falls back to its show's through the chain below. A season has
no lookup of its own.

THE PROXY DOES THE RESIZING, and that is why there is no Pillow pipeline here
any more. wsrv.nl is asked for the exact 500x750 canvas the compositor wants
(providers/base.POSTER_PARAMS), so this side does no decode, resample, pad or
re-encode — and because it is the same host the browser's own cards address, a
poster warmed here and one drawn there are one cached object rather than two
fetches of the same picture. What survives of the old pipeline is `_verify`: the
header check that keeps a decompression bomb from reaching the compositor.

RESOLUTION CHAIN, in order, each one falling through to the next on failure:
    1. disk hit, for the first source in the caller's order that has one
    2. every source in that order given up on (no calls)
    3. the show_posters registry, tried in the CALLER'S order — which is what
       makes a link preview draw the same picture as the page it previews
    4. THE TITLE'S ONE LOOKUP (`_look_up_address`), unless one already found
       nothing (artwork.record_miss): TMDB /tv or /movie -> poster_path -> w500
       if TMDB is configured and in the order, else a Trakt id-lookup by tmdb;
       an answer is recorded into the registry, and so is a miss
    5. negative marker against the order's first source
Stage 4 used to come FIRST, which spent a request to learn something a calendar
fill had already written down and decided the picture would be TMDB's whatever
the page was showing. It is the fallback now: it exists for titles nobody's
calendar has ever named, which is the ranker's imported ones.
A registry URL that fails is not deleted — its fail_count increments and
resolution falls through to the next source, because a dead URL should trigger
rediscovery, not a permanent hole.
"""
from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from pathlib import Path

import anyio.to_thread
from PIL import Image

from . import artwork
from . import tmdb as tmdb_client
from ..providers.trakt import calendar as trakt_calendar, transport as trakt_transport
from ..config import DATA_DIR
from ..perftrace import span
from ..providers.base import Media, POSTER_PARAMS, Source, proxied_image

logger = logging.getLogger(__name__)

POSTER_DIR = DATA_DIR / "posters"

# The canvas every tile is stored at. NOT USED TO RESIZE ANYTHING HERE any more
# — providers/base.POSTER_PARAMS is what actually asks for it, and this pair is
# what app/calendar/share_card.py's 2:3 tiles are drawn against. Kept as the
# app's statement of the size, so a change has one place to start.
POSTER_W, POSTER_H = 500, 750

# Checked on the image header, before Pillow decodes any pixel data — a
# decompression-bomb guard cheaper than letting Image.load() find out the hard
# way. No legitimate poster is anywhere near this large.
MAX_SOURCE_DIMENSION = 6000

# Read from the app's own media vocabulary rather than restated, so a third kind
# of title would not need this module to be remembered.
MEDIA_VALUES = tuple(Media)

# WHICH SERVICE'S ARTWORK TO PREFER, when the caller does not say. The app's own
# declared provider order first and TMDB behind it — the reverse of what this
# module used to do, and the reason is that the CARD AND ITS PREVIEW PICTURE
# MUST AGREE. The calendar draws `Record.poster`, which is whatever the source
# filling that calendar published; resolving the preview TMDB-first meant the
# link preview showed a different picture from the page it previewed, which for
# Halloween Wars was a different picture of a different-looking show. TMDB stays
# in the order because it is the only source the ranker's own imported titles
# have — they were never on anyone's calendar — but it is now the backstop
# rather than the default.
#
# TMDB IS NOT A `Source`, which is why this is not simply the enum: it is an
# artwork origin this app reads, never a calendar or tracker one, so it has no
# entry in the provider registry and has to be named here.
DEFAULT_ORDER = tuple(str(s) for s in Source) + ("tmdb",)

_FAN_OUT = 8


def _valid(media: str, tmdb: object) -> int | None:
    """tmdb as an int if (media, tmdb) is a well-formed pair, else None."""
    if media not in MEDIA_VALUES or not tmdb:
        return None
    try:
        return int(tmdb)
    except (TypeError, ValueError):
        return None


def _dir(media: str) -> Path:
    return POSTER_DIR / media


def _tile_path(media: str, tmdb: int, source: str, season: int | None = None) -> Path:
    """A season's own tile carries the season in its name ({tmdb}.s{N}.{source}),
    beside its show's ({tmdb}.{source}), so the two can never overwrite each
    other and a season with no artwork of its own is simply absent."""
    if season is not None:
        return _dir(media) / f"{tmdb}.s{season}.{source}.jpg"
    return _dir(media) / f"{tmdb}.{source}.jpg"


def _none_path(media: str, tmdb: int, source: str) -> Path:
    return _dir(media) / f"{tmdb}.{source}.none"


def order_for(preferred) -> tuple[str, ...]:
    """Whose artwork a viewer's pictures come from, most preferred first:
    `preferred` (their metadata preference over the app's sources —
    sources.prefs' source_order) with TMDB behind it.

    TMDB IS APPENDED RATHER THAN ORDERED, because it is not a calendar source
    and an account cannot have an opinion about it in those preferences. It is
    the backstop for a title no calendar named. One spelling of this, for the
    share card, the rankings board and the tracker, so the three cannot come to
    prefer different pictures of one title.
    """
    ordered = tuple(str(source) for source in preferred)
    return ordered if "tmdb" in ordered else ordered + ("tmdb",)


def cached_poster(media: str, tmdb: object, order: tuple[str, ...] = DEFAULT_ORDER,
                  season: int | None = None) -> Path | None:
    """The on-disk tile for (media, tmdb) under the first source in `order` that
    has one, else None — for a `season`, its own tile first and its show's after.

    SYNCHRONOUS AND STAT-ONLY, WHICH IS A CONSTRAINT AND NOT AN ACCIDENT. An
    export resolves every tile of its grid through this while building the
    render, so it may not touch the database — which is also why the source is
    resolved by trying the order on disk rather than by asking the registry who
    has a URL.
    """
    tid = _valid(media, tmdb)
    if tid is None:
        return None
    for which in ((season, None) if season is not None else (None,)):
        for source in order:
            path = _tile_path(media, tid, source, which)
            if path.exists():
                return path
    return None


def is_negative(media: str, tmdb: object, order: tuple[str, ...] = DEFAULT_ORDER) -> bool:
    """Whether EVERY source in `order` has been given up on.

    All rather than any, because the negative marker is per source and the point
    of an order is to fall through: one source having no artwork for a title says
    nothing about the next, and treating it as the answer would strand a poster
    the second source is holding.
    """
    tid = _valid(media, tmdb)
    if tid is None:
        return False
    return all(_none_path(media, tid, source).exists() for source in order)


def _verify(raw: bytes) -> bytes | None:
    """The downloaded bytes back, or None if they are not a picture this app
    should keep. NO RE-ENCODING — the proxy was asked for the exact canvas
    (POSTER_PARAMS), so there is nothing left to resize, pad or convert.

    IT STILL DECODES THE HEADER, AND THAT IS THE POINT OF KEEPING A FUNCTION
    HERE AT ALL. This is the only place bytes fetched from somebody else's host
    are opened before app/calendar/share_card.py composites them with Pillow,
    and the dimension check below is a decompression-bomb guard: a hostile image
    declares a huge canvas in a few bytes of header and is only expensive once
    something calls load(). Dropping the resize would have dropped that check
    with it, moving the first decode to the compositor where nothing is
    checking.

    `Image.open` READS THE HEADER AND NOT THE PIXELS, so this stays cheap enough
    to keep on the download path; `load()` is deliberately not called. Run
    through anyio.to_thread.run_sync anyway, since Pillow is synchronous and the
    header parse is still somebody else's data being parsed.
    """
    try:
        img = Image.open(BytesIO(raw))
        if img.width > MAX_SOURCE_DIMENSION or img.height > MAX_SOURCE_DIMENSION:
            logger.warning("poster declares %dx%d, refusing it", img.width, img.height)
            return None
    except Exception as exc:
        logger.warning("Pillow could not open poster (%d bytes): %s", len(raw or b""), exc)
        return None
    return raw


async def _try_source(url: str) -> bytes | None:
    """Fetch one candidate URL THROUGH THE IMAGE PROXY and keep what comes back.

    THE PROXY DOES THE RESIZING NOW, which is most of why this module no longer
    carries a Pillow pipeline: asking wsrv.nl for 500x750 costs this app one
    request either way and saves it a decode, a resample, a pad and a re-encode
    per poster. It is also the same host every card in the browser already
    addresses, so a poster warmed here and a poster drawn there are one cached
    object rather than two fetches of the same picture.

    None on ANY failure — network, non-200, or a body that will not open — so
    the caller falls through to the next source in the chain.
    """
    raw = await tmdb_client.download(proxied_image(url, POSTER_PARAMS))
    if raw is None:
        return None
    return await anyio.to_thread.run_sync(_verify, raw)


async def _fresh_provider_lookup(settings, media: str, tmdb: int) -> str | None:
    """A live Trakt id-lookup by tmdb id, cached like any other Trakt call
    through cached_get. Tried only once TMDB and the registry have both come
    up empty. Whatever URL this finds is recorded through app/media/artwork.py so the
    next poster this cold doesn't pay for the lookup twice."""
    media_type = "show" if media == "show" else "movie"
    results = await trakt_transport.cached_get(
        trakt_transport.shared_client(), settings, f"search/tmdb/{tmdb}",
        {"type": media_type, "extended": "full,images"},
        # A failure RAISES rather than reading as an empty answer: "Trakt knows
        # no picture for this" is remembered for good (artwork.record_miss), and
        # an unreachable Trakt must not be taken for it.
        raise_errors=True,
    )
    for entry in results if isinstance(results, list) else []:
        obj = entry.get(media_type) or {}
        url = trakt_calendar.poster(obj)
        if url:
            await artwork.record_poster_url(media, tmdb, "trakt", url)
            return url
    return None


async def _look_up_address(settings, media: str, tmdb: int,
                           order: tuple[str, ...]) -> tuple[str, str] | None:
    """A TITLE'S ONE LOOKUP: (source, address) of a poster nothing had recorded,
    recorded now for every later reader — or None, and then the title is
    recorded as a miss (artwork.record_miss) so nothing asks again.

    Only the services that answer by the tmdb id already in hand: TMDB's own
    record where TMDB is configured and in `order`, then Trakt's search by tmdb
    id. The caller has already checked the registry and the miss.
    """
    if "tmdb" in order and getattr(settings, "tmdb_configured", False):
        path = f"/tv/{tmdb}" if media == "show" else f"/movie/{tmdb}"
        data = await tmdb_client.get_json(settings, path, "posters.tmdb_detail")
        poster_path = (data or {}).get("poster_path")
        if poster_path:
            fresh = f"{tmdb_client.IMG}/w500{poster_path}"
            await artwork.record_poster_url(media, tmdb, "tmdb", fresh)
            return "tmdb", fresh
    try:
        fresh = await _fresh_provider_lookup(settings, media, tmdb)
    except Exception as exc:
        # A lookup that FAILED is not a lookup that found nothing: no miss is
        # recorded, so the next reader may try again.
        logger.warning("poster[%s/%s]: lookup failed: %s", media, tmdb, exc)
        return None
    if fresh:
        return "trakt", fresh
    await artwork.record_miss(media, tmdb)
    return None


async def find_address(settings, media: str, tmdb: object, order: tuple[str, ...],
                       season: int | None = None) -> str:
    """THE ORIGIN ADDRESS of the picture a viewer should see for one title or
    season, or "" — no bytes, nothing on disk. What a page draws through the
    image proxy (providers.base.proxied_image).

    What is recorded first (artwork.pick over the season's registry and the
    show's, in `order`); a title recorded as having none is "" with no call; and
    only a title nobody has recorded anything for costs its one lookup
    (`_look_up_address`), whose answer or miss is recorded for every later load.
    """
    tid = _valid(media, tmdb)
    if tid is None:
        return ""
    known = await artwork.known([(media, tid, season)])
    address = known.address(media, tid, season, order)
    if address or (media, tid) in known.missed:
        return address
    found = await _look_up_address(settings, media, tid, order)
    return found[1] if found else ""


async def ensure_addresses(settings, refs, order: tuple[str, ...]) -> int:
    """Best-effort: give every (media, tmdb) in `refs` that nothing has recorded
    its one lookup, fanned out under the semaphore ensure_posters uses. Returns
    how many were looked up. One registry read for the whole batch, so a page
    whose titles are all known costs exactly that and no request."""
    pairs = {(media, tid) for media, tmdb in refs if (tid := _valid(media, tmdb))}
    if not pairs:
        return 0
    known = await artwork.known((media, tid, None) for media, tid in pairs)
    want = [pair for pair in pairs if pair not in known.shows and pair not in known.missed]
    if not want:
        return 0
    sem = asyncio.Semaphore(_FAN_OUT)

    async def _one(pair):
        async with sem:
            return await _look_up_address(settings, pair[0], pair[1], order)

    with span("posters.ensure_addresses", n=len(want)):
        await asyncio.gather(*(_one(pair) for pair in want), return_exceptions=True)
    return len(want)


async def ensure_poster(settings, media: str, tmdb: object,
                        order: tuple[str, ...] = DEFAULT_ORDER,
                        season: int | None = None) -> Path | None:
    """The tile for (media, tmdb) under `order`, generating it if needed. None
    (and a negative marker) when nothing worked — a missing poster degrades one
    tile, it never fails the caller.

    A `season` is tried for ITS OWN ARTWORK FIRST — a tile already on disk, else
    an address the season registry holds — and falls back to its show's tile
    through everything below. There is no season lookup and no season marker:
    the only season artwork there is arrives on a season list somebody already
    read (app/media/artwork.py's season_posters), so a season with nothing
    recorded costs one database read and nothing else.

    ONE LOOP, MOST-PREFERRED SOURCE FIRST, and each source gets the same two
    chances in turn: the tile it already has, or the tile it can be made to
    produce. Written this way rather than as "any tile on disk, else resolve"
    because that reading has a defect the source-keyed cache exists to prevent —
    a viewer who prefers Simkl would be handed the Trakt tile somebody else's
    warm happened to store first, and never resolve the picture their own page
    is showing. Preferring a source has to mean fetching it, not merely
    preferring it among what is already lying around.

    THE CHEAP CHECKS COME FIRST WITHIN EACH SOURCE, so the common case — a tile
    already on disk under the first choice — costs one stat and no database read
    at all. `urls_for` is read once, lazily, and only when some source in the
    order still has to be tried.

    THE MARKER IS WRITTEN AGAINST THE FIRST SOURCE IN THE ORDER, not against
    every source tried: enough to stop this order retrying, while leaving a
    viewer who prefers a different service free to try the one they prefer.
    """
    tid = _valid(media, tmdb)
    if tid is None:
        return None

    if season is not None:
        for source in order:
            tile = _tile_path(media, tid, source, season)
            if tile.exists():
                return tile
        own = await artwork.season_urls_for(media, tid, season)
        for source in order:
            if not own.get(source):
                continue
            raw = await _try_source(own[source])
            if raw is not None:
                return _store(media, tid, source, raw, season)
            await artwork.record_season_failure(media, tid, season, source)

    # EVERY SOURCE GIVEN UP ON MEANS NO CALLS AT ALL, checked before the loop
    # rather than inside it. Falling out of the loop with every source marked
    # would still reach the search at the bottom, which is the one stage that is
    # not per source — so a title nothing can describe would pay for a Trakt
    # search on every warm, for ever.
    if is_negative(media, tid, order):
        logger.info("poster[%s/%s]: negative-cached (skipping resolution)", media, tid)
        return None

    known: dict[str, str] | None = None
    with span("posters.generate", media=media, tmdb=tid):
        for source in order:
            tile = _tile_path(media, tid, source)
            if tile.exists():
                return tile
            if _none_path(media, tid, source).exists():
                continue

            if known is None:
                known = await artwork.urls_for(media, tid)
            url = known.get(source)
            if url:
                raw = await _try_source(url)
                if raw is not None:
                    return _store(media, tid, source, raw)
                # Dead URL: rediscovery, not a permanent hole. Fall through to
                # the next source rather than giving up on this poster.
                await artwork.record_failure(media, tid, source)
                continue

        # NOTHING RECORDED WORKED, so pay for the title's one lookup — unless it
        # has been paid for already and found nothing (artwork.record_miss).
        # After the registry rather than ahead of it, which is the reverse of
        # what this module used to do: asking first spent a request to learn
        # something a calendar fill had already written down, and decided the
        # picture would be TMDB's whatever the page was showing.
        if (media, tid) not in (await artwork.known([(media, tid, None)])).missed:
            found = await _look_up_address(settings, media, tid, order)
            if found:
                raw = await _try_source(found[1])
                if raw is not None:
                    return _store(media, tid, found[0], raw)

        logger.info("poster[%s/%s]: nothing resolved -> negative marker", media, tid)
        _dir(media).mkdir(parents=True, exist_ok=True)
        _none_path(media, tid, order[0]).write_text("", encoding="utf-8")
        return None


def _store(media: str, tmdb: int, source: str, raw: bytes, season: int | None = None) -> Path:
    """Write one verified tile and answer where it went."""
    _dir(media).mkdir(parents=True, exist_ok=True)
    tile = _tile_path(media, tmdb, source, season)
    tile.write_bytes(raw)
    logger.info("poster[%s/%s]: GENERATED from %s -> %s", media, tmdb, source, tile.name)
    return tile


async def ensure_posters(settings, refs, order: tuple[str, ...] = DEFAULT_ORDER) -> int:
    """Best-effort pre-generation of the poster tiles a set of (media, tmdb)
    pairs needs. Returns how many were newly generated.

    Mirrors logos.ensure_logos: dedupe, skip anything already cached or
    negative-marked (a single Path.exists() each), fan the rest out under a
    semaphore. `refs` is any iterable of (media, tmdb). A board can hold up to
    1000 items, so callers are expected to pass a bounded subset — the current
    board's visible pool page plus its tiered items — never a whole library.
    """
    # Two stat calls per ref, on the caller's thread — which is the event loop.
    # Free on a local disk and emphatically not free on a mounted volume, so it is
    # measured rather than assumed: a slow scan here delays every request on the
    # instance before a single poster has been fetched.
    # Materialized because `refs` is documented as any iterable and the span below
    # reports how many there were; a generator would be counted by consuming it.
    refs = list(refs)
    want: set[tuple[str, int, int | None]] = set()
    with span("posters.disk_scan", refs=len(refs)) as sp:
        for ref in refs:
            # (media, tmdb), or (media, tmdb, season) for a season's own artwork.
            media, tmdb, season = (*ref, None)[:3]
            tid = _valid(media, tmdb)
            if tid is None:
                continue
            # THE FIRST SOURCE'S TILE, NOT ANY TILE. A hit further down the
            # order is a picture this viewer would accept but has not asked
            # for, and skipping on it would leave them permanently on somebody
            # else's preference — see ensure_poster, which is what upgrades it.
            # The cost of being wrong here is one database read per title, which
            # is what ensure_poster does before deciding there is nothing to do.
            if _tile_path(media, tid, order[0], season).exists():
                continue
            if season is None and is_negative(media, tid, order):
                continue
            want.add((media, tid, season))
        sp.set(missing=len(want))
    if not want:
        return 0

    sem = asyncio.Semaphore(_FAN_OUT)

    async def _one(ref: tuple[str, int, int | None]) -> Path | None:
        async with sem:
            return await ensure_poster(settings, ref[0], ref[1], order, season=ref[2])

    with span("posters.ensure_posters", n=len(want)):
        results = await asyncio.gather(*(_one(pair) for pair in want), return_exceptions=True)
    generated = 0
    for pair, result in zip(want, results):
        if isinstance(result, Exception):
            logger.warning("poster pre-warm failed for %s/%s: %s", pair[0], pair[1], result)
        elif result is not None:
            generated += 1
    return generated


def sweep(max_bytes: int, now: float | None = None) -> int:
    """Reclaim cached poster tiles: anything past its age ceiling first, then
    LRU by file mtime until the total is back under max_bytes.

    TWO RULES, AND THE AGE ONE IS NOT NEGOTIABLE. TMDB's terms cap how long
    anything obtained from them may be kept (tmdb.MAX_CACHE_SECONDS), and a
    size cap does not satisfy that: an instance under its budget would keep a
    tile for ever, which is exactly the case the terms are about. The byte cap
    is this app's own housekeeping and runs after.

    THE AGE IS THE FILE's, and a tile is written once and never touched again,
    so its mtime is when it was obtained. Ageing one out is not a loss — the
    next request for it resolves it again, which is also how a poster that has
    changed upstream is eventually noticed.

    Pure filesystem walking, so it runs on the same heartbeat as app/cache.py's
    sweep but through a worker thread (the caller's job — see app/main.py)
    rather than the event loop.
    """
    if not POSTER_DIR.exists() or max_bytes < 0:
        return 0
    cutoff = (time.time() if now is None else now) - tmdb_client.MAX_CACHE_SECONDS
    entries: list[tuple[float, int, Path]] = []
    total = 0
    removed = 0
    for path in POSTER_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime <= cutoff:
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
    entries.sort(key=lambda e: e[0])
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
