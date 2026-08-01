"""Seerr (Overseerr / Jellyseerr lineage) request integration.

Requests are made by TMDB id for both shows and movies via the /api/v1/request
endpoint, authenticated with an X-Api-Key header.
"""
from __future__ import annotations

import json

import anyio.to_thread
import httpx

from .. import http_pool
from ..config import Settings
from ..perftrace import span

# How many media records to ask for per page. Seerr's default page size is 20 and
# this endpoint has no "give me everything" form, so the only lever on how many
# ROUND TRIPS a full read costs is this number: a library of ~3500 took 36
# sequential requests at 100, each one a full round trip to a box this app does
# not control. Raised, not removed — the loop below still honours whatever the
# server actually returns, so an instance that caps `take` lower simply paginates
# more and nothing breaks.
PAGE_SIZE = 500

# SEER'S OWN POOL — same reasoning as app/integrations/arr.py: this module built a fresh
# client per call, and building one loads the system trust store inline on the
# event loop. The library read below is the worst offender because it PAGINATES,
# so a large Seerr was paying that cost once and then holding a connection
# through an unbounded number of round trips.
POOL = http_pool.Pool("seer", max_connections=4, timeout=20)


def _base(settings: Settings) -> tuple[str, str]:
    return settings.seer_url.strip().rstrip("/"), settings.seer_api_key.strip()


def is_configured(settings: Settings) -> bool:
    url, key = _base(settings)
    return bool(url and key)


async def check_health(settings: Settings) -> dict:
    if not is_configured(settings):
        return {"configured": False, "reachable": False}
    url, key = _base(settings)
    try:
        with span("seer.health"):
            resp = await POOL.client().get(
                f"{url}/api/v1/status", headers={"X-Api-Key": key}, timeout=8)
        return {"configured": True, "reachable": resp.status_code == 200}
    except httpx.HTTPError:
        return {"configured": True, "reachable": False}


def _page(raw: bytes) -> tuple[list, int, int]:
    """SYNCHRONOUS. One page of /api/v1/media as (ids, records, library total).

    ON A WORKER THREAD, like app/integrations/arr.py's equivalent and for the same reason:
    parsing the page is CPU, and at PAGE_SIZE records a page it is enough of it to
    stall every other request if it runs on the event loop.

    The RECORD COUNT is returned rather than derived from the ids, because a
    record carrying no tmdbId still advances the cursor — counting ids would leave
    `skip` short and re-request the same page forever.
    """
    data = json.loads(raw)
    results = data.get("results", [])
    ids = [m["tmdbId"] for m in results if m.get("tmdbId")]
    return ids, len(results), (data.get("pageInfo") or {}).get("results", 0)


class LibraryUnavailable(Exception):
    """Seerr could not be read — as distinct from having nothing in it.

    Mirrors app/integrations/arr.py's type of the same name, for the same reason: a caller
    that cannot tell a failed read from an empty library will cache the failure
    as truth and quietly stop marking anything as already-requested. Separate
    types rather than one shared one, because "which service is down" is what the
    caller needs in order to keep the OTHER two services' answers.
    """


async def library_ids(settings: Settings) -> list:
    """All TMDB ids already known to Seerr (requested or available), paginated.

    Raises LibraryUnavailable when the answer is unknown rather than empty.
    PARTIAL IS ALSO UNKNOWN: a page failing midway used to return the ids
    gathered so far, which reads downstream as a complete, shorter library — so
    it raises too, and the caller keeps what it already had.
    """
    if not is_configured(settings):
        return []
    url, key = _base(settings)
    headers = {"X-Api-Key": key}
    ids: list = []
    skip = 0
    pages = 0
    client = POOL.client()
    try:
        with span("seer.library") as sp:
            while True:
                resp = await client.get(
                    f"{url}/api/v1/media",
                    params={"take": PAGE_SIZE, "skip": skip}, headers=headers)
                if resp.status_code != 200:
                    raise LibraryUnavailable(f"Seerr returned HTTP {resp.status_code}")
                page_ids, count, total = await anyio.to_thread.run_sync(_page, resp.content)
                ids.extend(page_ids)
                pages += 1
                skip += count
                if not count or skip >= total or skip > 10000:  # safety cap
                    break
            sp.set(ids=len(ids), pages=pages)
    except (httpx.HTTPError, ValueError) as exc:
        raise LibraryUnavailable(f"Seerr could not be read: {exc}") from exc
    return ids


async def add_media(settings: Settings, media: str, tmdb, title: str) -> dict:
    """Create a Seerr request. Shows request all seasons; both use the TMDB id."""
    url, key = _base(settings)
    if not tmdb:
        thing = "movie" if media == "movie" else "show"
        return {"ok": False, "error": f"This {thing} has no TMDB id, so Seerr can't request it."}
    payload = {"mediaType": "movie" if media == "movie" else "tv", "mediaId": int(tmdb)}
    if media != "movie":
        payload["seasons"] = "all"
    try:
        with span("seer.request"):
            resp = await POOL.client().post(
                f"{url}/api/v1/request",
                json=payload,
                headers={"X-Api-Key": key, "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not reach Seerr: {exc}"}

    if resp.status_code in (200, 201):
        return {"ok": True, "message": f"Requested {title} on Seerr."}
    if resp.status_code == 409:  # already exists / requested
        return {"ok": True, "message": f"{title} is already on Seerr."}
    try:
        body = resp.json()
        msg = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    return {"ok": False, "error": msg or f"HTTP {resp.status_code}"}
