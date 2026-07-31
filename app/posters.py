"""Poster tiles on disk (TMDB + Pillow). Fetch, normalize, cache — no DB schema
knowledge beyond calling app/artwork.py for the URL registry.

Mirrors app/logos.py's shape: disk cache, negative markers, best-effort
degradation. Never called on a render path — warming is explicit (ensure_poster
/ ensure_posters), and a render resolves whatever is already on disk.

STORAGE, MEDIA-NAMESPACED. TMDB ids are namespaced per media type — movie 550
and TV 550 are different titles — so every path here is keyed on the PAIR:
    DATA_DIR/posters/<media>/<tmdb>.jpg    normalized 500x750, JPEG q88
    DATA_DIR/posters/<media>/<tmdb>.none   negative marker

RESOLUTION CHAIN, in order, each one falling through to the next on failure:
    1. disk hit
    2. negative marker (give up, no calls)
    3. TMDB /tv or /movie -> poster_path -> w500 (natively 500x750: the common
       path never resamples)
    4. the best surviving show_posters registry row
    5. a fresh Trakt id-lookup by tmdb, recorded into the registry for next time
    6. negative marker
A registry URL that fails is not deleted — its fail_count increments and
resolution falls through to the next stage, because a dead URL should trigger
rediscovery, not a permanent hole.
"""
from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path

import anyio.to_thread
from PIL import Image

from . import artwork
from . import tmdb as tmdb_client
from .providers.trakt import calendar as trakt_calendar, transport as trakt_transport
from .config import DATA_DIR
from .perftrace import span
from .providers.base import Media

logger = logging.getLogger(__name__)

POSTER_DIR = DATA_DIR / "posters"

# Matches TMDB's w500, which is natively this size — see the module docstring.
POSTER_W, POSTER_H = 500, 750
JPEG_QUALITY = 88

# Checked on the image header, before Pillow decodes any pixel data — a
# decompression-bomb guard cheaper than letting Image.load() find out the hard
# way. No legitimate poster is anywhere near this large.
MAX_SOURCE_DIMENSION = 6000

# Read from the app's own media vocabulary rather than restated, so a third kind
# of title would not need this module to be remembered.
MEDIA_VALUES = tuple(Media)

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


def _tile_path(media: str, tmdb: int) -> Path:
    return _dir(media) / f"{tmdb}.jpg"


def _none_path(media: str, tmdb: int) -> Path:
    return _dir(media) / f"{tmdb}.none"


def cached_poster(media: str, tmdb: object) -> Path | None:
    """The on-disk tile for (media, tmdb) if already generated, else None."""
    tid = _valid(media, tmdb)
    if tid is None:
        return None
    p = _tile_path(media, tid)
    return p if p.exists() else None


def is_negative(media: str, tmdb: object) -> bool:
    tid = _valid(media, tmdb)
    return tid is not None and _none_path(media, tid).exists()


def _fit_pad(img: Image.Image, w: int, h: int) -> Image.Image:
    """Fit `img` inside (w, h) preserving aspect ratio, then pad centered onto a
    (w, h) canvas. PAD rather than stretch: a wrong-aspect poster from a
    fallback source must never come out visibly distorted."""
    ratio = min(w / img.width, h / img.height)
    new_size = (max(1, round(img.width * ratio)), max(1, round(img.height * ratio)))
    resized = img.resize(new_size, Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(resized, ((w - new_size[0]) // 2, (h - new_size[1]) // 2))
    return canvas


def _normalize(raw: bytes) -> bytes | None:
    """Raw downloaded bytes -> a normalized 500x750 JPEG q88, or None if Pillow
    can't make sense of them. Downloaded bytes are untrusted and this is the
    only place they get decoded; always run through anyio.to_thread.run_sync so
    the decode never lands on the event loop."""
    try:
        img = Image.open(BytesIO(raw))
        if img.width > MAX_SOURCE_DIMENSION or img.height > MAX_SOURCE_DIMENSION:
            return None
        img.load()
        img = img.convert("RGB")
    except Exception as exc:
        logger.warning("Pillow could not open poster (%d bytes): %s", len(raw or b""), exc)
        return None
    if img.size != (POSTER_W, POSTER_H):
        img = _fit_pad(img, POSTER_W, POSTER_H)
    buf = BytesIO()
    with span("posters.encode"):
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


async def _try_source(url: str) -> bytes | None:
    """Download + normalize one candidate URL. None on ANY failure — network,
    non-200, or a body Pillow can't decode — so the caller can fall through to
    the next source in the chain."""
    raw = await tmdb_client.download(url)
    if raw is None:
        return None
    return await anyio.to_thread.run_sync(_normalize, raw)


async def _fresh_provider_lookup(settings, media: str, tmdb: int) -> str | None:
    """A live Trakt id-lookup by tmdb id, cached like any other Trakt call
    through cached_get. Tried only once TMDB and the registry have both come
    up empty. Whatever URL this finds is recorded through app/artwork.py so the
    next poster this cold doesn't pay for the lookup twice."""
    media_type = "show" if media == "show" else "movie"
    results = await trakt_transport.cached_get(
        trakt_transport.shared_client(), settings, f"search/tmdb/{tmdb}",
        {"type": media_type, "extended": "full,images"},
    )
    for entry in results if isinstance(results, list) else []:
        obj = entry.get(media_type) or {}
        url = trakt_calendar.poster(obj)
        if url:
            await artwork.record_poster_url(media, tmdb, "trakt", url)
            return url
    return None


async def _resolve(settings, media: str, tmdb: int) -> bytes | None:
    """The resolution chain (stages 3-5); stages 1/2/6 are ensure_poster's."""
    if getattr(settings, "tmdb_configured", False):
        path = f"/tv/{tmdb}" if media == "show" else f"/movie/{tmdb}"
        data = await tmdb_client.get_json(settings, path, "posters.tmdb_detail")
        poster_path = (data or {}).get("poster_path")
        if poster_path:
            url = f"{tmdb_client.IMG}/w500{poster_path}"
            normalized = await _try_source(url)
            if normalized is not None:
                await artwork.record_poster_url(media, tmdb, "tmdb", url)
                return normalized

    best = await artwork.best_url(media, tmdb)
    if best is not None:
        source, url = best
        normalized = await _try_source(url)
        if normalized is not None:
            return normalized
        # Dead: rediscovery, not a permanent hole. Fall through rather than
        # giving up on this poster entirely.
        await artwork.record_failure(media, tmdb, source)

    fresh_url = await _fresh_provider_lookup(settings, media, tmdb)
    if fresh_url:
        normalized = await _try_source(fresh_url)
        if normalized is not None:
            return normalized

    return None


async def ensure_poster(settings, media: str, tmdb: object) -> Path | None:
    """Return the cached tile for (media, tmdb), generating it via the
    resolution chain if needed. None (and a negative marker) when nothing
    worked — a missing poster degrades one tile, it never fails the caller."""
    tid = _valid(media, tmdb)
    if tid is None:
        return None
    tile = _tile_path(media, tid)
    if tile.exists():
        return tile
    if _none_path(media, tid).exists():
        logger.info("poster[%s/%s]: negative-cached (skipping resolution)", media, tid)
        return None

    with span("posters.generate", media=media, tmdb=tid):
        normalized = await _resolve(settings, media, tid)
        _dir(media).mkdir(parents=True, exist_ok=True)
        if normalized is None:
            logger.info("poster[%s/%s]: nothing resolved -> negative marker", media, tid)
            _none_path(media, tid).write_text("", encoding="utf-8")
            return None
        tile.write_bytes(normalized)
        logger.info("poster[%s/%s]: GENERATED -> %s", media, tid, tile.name)
    return tile


async def ensure_posters(settings, refs) -> int:
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
    want: set[tuple[str, int]] = set()
    with span("posters.disk_scan", refs=len(refs)) as sp:
        for media, tmdb in refs:
            tid = _valid(media, tmdb)
            if tid is None:
                continue
            pair = (media, tid)
            if _tile_path(*pair).exists() or _none_path(*pair).exists():
                continue
            want.add(pair)
        sp.set(missing=len(want))
    if not want:
        return 0

    sem = asyncio.Semaphore(_FAN_OUT)

    async def _one(pair: tuple[str, int]) -> Path | None:
        async with sem:
            return await ensure_poster(settings, pair[0], pair[1])

    with span("posters.ensure_posters", n=len(want)):
        results = await asyncio.gather(*(_one(pair) for pair in want), return_exceptions=True)
    generated = 0
    for pair, result in zip(want, results):
        if isinstance(result, Exception):
            logger.warning("poster pre-warm failed for %s/%s: %s", pair[0], pair[1], result)
        elif result is not None:
            generated += 1
    return generated


def sweep(max_bytes: int) -> int:
    """LRU-evict cached poster tiles (oldest file mtime first) until the total
    is back under max_bytes. Pure filesystem walking, so it runs on the same
    heartbeat as app/cache.py's sweep but through a worker thread (the caller's
    job — see app/main.py) rather than the event loop.
    """
    if not POSTER_DIR.exists() or max_bytes < 0:
        return 0
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in POSTER_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    if total <= max_bytes:
        return 0
    entries.sort(key=lambda e: e[0])
    removed = 0
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
