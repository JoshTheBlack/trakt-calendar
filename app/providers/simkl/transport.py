"""Getting a real answer out of Simkl, and nothing else.

Two pooled clients, the request headers, the pacing and retry rules Simkl's
published quota asks for, and the disk-cached GET every data call routes
through. Like the Trakt transport beside it, this module deliberately does NOT
interpret what an answer MEANS — a calendar file, a catalog record and a watch
history are all just parsed JSON here.

WHY TWO POOLS RATHER THAN ONE. Simkl publishes two different regimes and a
single pool could only honour the stricter of them. The Cloudflare-cached paths
(the CDN calendar files, the catalog and episode lookups) explicitly allow
parallel requests; everything under /sync/ and /users/ explicitly does not, and
POST there is capped at one request per second. Sizing one pool for the second
would make a month's worth of catalog lookups take minutes; sizing it for the
first would break the rule that matters. So the two regimes are two pools, and
which one a call belongs to is stated at the call rather than guessed here.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from urllib.parse import parse_qsl, urlencode

import httpx

from ... import cache
from ... import changelog
from ... import http_pool
from ... import perftrace
from ...config import Settings
from ..base import SourceUnavailable

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

API_BASE = "https://api.simkl.com"

# Sent on every request. Simkl's docs ask for an application name and version
# alongside the client id so an instance misbehaving can be identified and told
# about it rather than silently blocked; the User-Agent is the same courtesy for
# anything reading raw logs.
#
# TWO NAMES, BECAUSE THIS APP IS TWO THINGS IN SIMKL'S LOGS. The tracker reads
# ONE PERSON'S watch history with that person's token; the calendar and the
# catalogue lookups behind it read PUBLIC data with the instance's own client id.
# When Simkl asks which half of an instance is leaning on them, those are
# genuinely different answers, and a single name would lose the distinction at
# exactly the moment it matters. The split is the same one that runs through this
# whole package — see `cached_get`'s `private` flag, which asks the identical
# question about the RESPONSE.
APP_NAME_TRACKER = "distrakkt"
APP_NAME_CALENDAR = "distrakkl"

# What a caller that did not say gets: the public half's name. Deliberately the
# safer default of the two — the catalogue reads are shared by both halves, so a
# call that has not stated which one it belongs to is being made on behalf of
# something public more often than not, and mislabelling a public read as the
# tracker would put the tracker's name on traffic no viewer's token was spent on.
DEFAULT_APP_NAME = APP_NAME_CALENDAR

# The version reported when the changelog cannot be read at all. Not a second
# statement of the version — `app_version` prefers the real one and this only
# stands in when there is none to prefer.
APP_VERSION_FALLBACK = "0"

USER_AGENT = f"{APP_NAME_CALENDAR}-py/{APP_VERSION_FALLBACK}"


def app_version() -> str:
    """The running version, for Simkl's `app-version` parameter.

    READ FROM THE CHANGELOG rather than restated here, because the app already
    has one place that answers "what version is this" and a constant beside it
    would be a second copy to keep in step — one that goes stale silently, since
    nothing renders it. `changelog.current_version` answers "" when the file
    cannot be parsed, which is what the fallback is for.
    """
    return changelog.current_version() or APP_VERSION_FALLBACK


class SimklError(SourceUnavailable):
    """Simkl could not answer. Every caller that writes `except SimklError` is
    saying "Simkl could not answer", not "the transport layer raised".

    Its base is the app-wide "a source could not answer" contract, the same one
    TraktError derives from, so a caller reading BOTH sources can degrade
    whichever failed without an except clause per service."""


class SimklRateLimitError(SimklError):
    """Simkl returned 429 and send's retry/backoff budget was exhausted.

    "You went too fast just now" — a condition about THIS burst of requests,
    which clears on its own in seconds. A distinct type for the same reason
    TraktRateLimitError is one: a caller can degrade the one affected title or
    the one affected month deliberately, instead of treating a temporary
    slow-down as a hard failure of the source.
    """


class SimklBlockedError(SimklError):
    """Simkl returned 412 — this instance's client id is throttle-blocked.

    A DIFFERENT FAILURE FROM 429 AND IT MUST NOT BE RETRIED THE SAME WAY. A 429
    is about one burst; a 412 is Simkl saying the whole application is currently
    refused, for every user on this box at once. Retrying into it makes the
    block worse rather than clearing it, so it opens the circuit breaker below
    and is raised again, without a request, until the cooldown passes.
    """


# The statuses that are about WHO ASKED rather than about WHAT WAS ASKED FOR.
# Simkl answers 401 `user_token_failed` for a token it will not accept and 403
# for one it accepts but will not honour, and neither is a property of the path
# that happened to be called first: the same token on any other path gets the
# same answer. A caller that tolerates one call failing must NOT tolerate one of
# these, because "this list could not be read" and "nothing this token asks for
# can be read" are different facts with opposite consequences — and Simkl issues
# no refresh token (a grant is revoked, a password is reset, a token expires), so
# this is an ordinary expected state rather than an exotic one.
CREDENTIAL_STATUSES = (401, 403)


def is_credential_failure(error: SimklError) -> bool:
    """True when this failure says the CREDENTIAL is not usable.

    Lives here because the statuses are Simkl's, and reading them is what this
    module is for; a caller asks the question rather than comparing numbers of
    its own, so the answer has one place to change.
    """
    return error.status in CREDENTIAL_STATUSES


def api_headers(settings: Settings, *, private: bool = False) -> dict:
    """Simkl request headers.

    THE BEARER GOES ONLY ON A PRIVATE READ, and that is a caching decision as
    much as a correctness one. `private` asks the same question `cached_get`
    asks — does this answer depend on WHOSE token asked — and a public catalogue
    lookup does not.

    SENDING IT ANYWAY COSTS THE EDGE CACHE, measured 2026-08-21 against the live
    service: `GET /tv/1687953` answers `cf-cache-status: BYPASS` with the
    Authorization header and `MISS` then `HIT` without it. Cloudflare will still
    SERVE an entry somebody else's traffic warmed — Chuck answers HIT either way
    — but it will not STORE one for an authenticated request. So every cold title
    this app looked up went to the origin and left nothing behind for the next
    caller, which is exactly the traffic Simkl's docs allow parallel requests for
    ON THE GROUNDS THAT IT IS EDGE-CACHED. The header made that untrue.

    A token is still only sent when there IS one: the calendar and catalogue
    halves are unauthenticated by design, and an empty bearer turns a public
    lookup into a rejected one.
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    token = (settings.simkl_access_token or "").strip()
    if private and token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# The query parameters that say WHO IS ASKING rather than WHAT IS BEING ASKED
# FOR. `api_params` is what puts them on a request and `cache_key` is what leaves
# them off the address the answer is filed under; both read this one tuple,
# because "does this parameter describe the question" is a single fact and a
# second copy of it is exactly how the client id came to be part of the cache key
# in the first place.
#
# THE APP NAME IS IN HERE FOR A REASON THAT BIT ONCE ALREADY. Two halves of this
# app now identify themselves differently (APP_NAME_TRACKER / APP_NAME_CALENDAR)
# and both ask for the same titles, so a key that included the name would file
# one public answer under two addresses — the same defect the client id caused,
# in a new spelling.
CALLER_PARAMS = ("client_id", "app-name", "app-version")


def api_params(settings: Settings, params: dict | None = None, *,
               app: str = DEFAULT_APP_NAME) -> dict:
    """`params` with the three things Simkl asks every request to carry: this
    instance's client id, and the name and version of the app making the call.

    ALL THREE ARE QUERY PARAMETERS, which is Simkl's own spelling of them
    ("appended to every request URL"). Trakt sends the equivalent facts in
    headers; the difference is Simkl's and every request has to honour it,
    because a cold origin request without a client id answers 412
    client_id_failed. This app previously sent the name and version as HEADERS,
    which is not where the docs put them.

    `app` NAMES WHICH HALF IS CALLING — see APP_NAME_TRACKER / APP_NAME_CALENDAR
    for why there are two of them and why the public one is the default.

    NONE OF THE THREE IS PART OF THE CACHE KEY, and that is `cache_key`'s
    business — see there for why a public catalogue answer does not belong to the
    caller that happened to fetch it. That matters more now than it did with the
    client id alone: two halves of this app ask for the same title, and keying on
    the name would file one answer twice.
    """
    merged = dict(params or {})
    merged["client_id"] = settings.simkl_client_id
    merged["app-name"] = app
    merged["app-version"] = app_version()
    return merged


def cache_key(path: str, params: dict | None = None) -> str:
    """The address a cached answer for `path` is filed under.

    THE CREDENTIAL IS NOT IN IT. A cached answer is a PUBLIC catalogue response —
    a title, an episode list, a search — and measured against the live service,
    `GET /tv/{id}?extended=full` returns the same 23 fields with no client id, an
    empty one, a bogus one and the real one. The id identifies the APPLICATION for
    rate limiting and attribution; it does not select the content. Keying on it
    made every stored answer the property of the credential that fetched it, so
    rotating or clearing the client id stranded thousands of rows describing
    titles whose content never depended on it — which is why a modal could not
    fall back to what this instance was already holding.
    A RESPONSE THAT GENUINELY DEPENDS ON WHO ASKED IS NOT CACHED AT ALL. That is
    `private=True` in `cached_get`, and it is the case the old rule was reaching
    for: it cannot occur on the path this key governs, because such a response is
    never written here.

    Same URL shape as the request so the key stays legible next to a log line and
    cannot collide with the other transport's, minus the credential. The
    remaining parameters are SORTED, so two callers spelling the same question in
    a different order address one entry rather than two.
    """
    describing = sorted((name, value) for name, value in (params or {}).items()
                        if name not in CALLER_PARAMS)
    return f"{API_BASE}/{path}?{urlencode(describing)}"


# ---------------------------------------------------------------------------
# The two pools, and the POST pacer that sits beside them because it answers the
# same question: how hard may this app lean on this service.
# ---------------------------------------------------------------------------

# THE CLOUDFLARE-CACHED HALF: the CDN calendar files and the catalog / episode
# lookups. Simkl's docs name these as the endpoints where parallel requests are
# allowed, because they are served from the edge rather than the origin. Sized
# for throughput — a month's window can reference several hundred distinct
# titles — while staying under the 10 GET/second ceiling that applies to every
# path regardless of caching.
#
# THIS POOL DOES NOT FOLLOW REDIRECTS OF ITS OWN ACCORD, even though GET
# /tv/{id} 302s to GET /anime/{id} for a real fraction of anime titles and that
# target is where the answer lives. `send` follows the hop instead, after
# classifying it — see redirect_pool below for why a pool chosen before the
# request cannot be the pool the answer is fetched under.
CATALOG_POOL = http_pool.Pool("simkl", max_connections=8, timeout=30, concurrency=6)

# EVERYTHING UNDER /sync/ AND /users/. The docs mandate SEQUENTIAL requests off
# the cached paths, so this pool admits exactly one request at a time. Two
# connections rather than one only so a keep-alive that has gone stale does not
# stall the next call behind it.
SYNC_POOL = http_pool.Pool("simkl-sync", max_connections=2, timeout=30, concurrency=1)

# THE CALENDAR CDN — data.simkl.in, NOT api.simkl.com. A third pool rather than
# reusing CATALOG_POOL: that pool is sized for the API host's catalog and
# episode lookups, which carry a client_id and are subject to this instance's
# 412 block; the CDN files take neither, are edge-cached with parallel requests
# explicitly allowed, and can run several megabytes each. A slow calendar file
# holding every connection open would starve a catalog lookup that has nothing
# to do with it, which is exactly the coupling the two-pool split above exists
# to avoid — so the calendar files get their own budget rather than sharing
# either existing one. Timeout is longer than the other pools': the largest
# archive months measured over 7 MB.
CDN_POOL = http_pool.Pool("simkl-cdn", max_connections=6, timeout=45, concurrency=4)

# A SEMAPHORE IS NOT ENOUGH FOR THE POST CAP, which is why this exists at all.
# `concurrency=1` bounds how many requests are IN FLIGHT, not how fast they
# LEAVE: two POSTs issued back to back both go out inside the same second and
# the second one is over the published 1 POST/second limit. So a POST is held
# until at least this long after the previous one finished. The margin over a
# flat second is deliberate — the cap is enforced on Simkl's clock, not ours,
# and a request that leaves 999ms after the last one by our reckoning is a
# coin flip by theirs.
POST_MIN_INTERVAL = 1.05

# AND THE CATALOGUE GETS ARE PACED TOO, which they were not, and the gap is what
# took a fresh deployment off Simkl entirely.
#
# WHAT HAPPENED, because the reasoning that left these unpaced was not silly and
# should not be repeated. Simkl's docs name the catalogue paths as parallel-safe,
# so this pool was sized for throughput (concurrency=6) and left to run: a
# semaphore bounds what is IN FLIGHT, and "parallel allowed" was read as "rate
# does not apply here". A settled instance never tested that reading, because its
# enrichment table is full and the drain trickles. A FRESH one owes a lookup for
# every Simkl title in every cached window and fires DRAIN_BATCH_SIZE of them per
# heartbeat, six at a time, as fast as they come back — measured at 23.5 requests
# per second sequentially, and higher than that in parallel. Simkl answered 412,
# which is an instance-wide refusal, and the app then correctly stopped calling
# Simkl at all for fifteen minutes: no enrichment, no detail modals, and no
# signing in or linking a Simkl account, on a deployment whose credentials were
# perfectly good. Verified from the other side, from inside that container: one
# hand-made request to the same URL answered 200 while the app was being refused.
#
# 8 PER SECOND, UNDER THE 10 THE DOCS NAME. Pacing to the published ceiling is
# what the ceiling is for; the margin is because the cap is enforced on Simkl's
# clock rather than ours, the same reasoning POST_MIN_INTERVAL carries. A batch
# of 300 then costs ~38s of a 60-second heartbeat and a backlog still clears in
# minutes, while the only interactive caller — a detail modal, one or two
# lookups, usually cached — waits at worst a beat behind whatever the drain has
# already queued.
CATALOG_MIN_INTERVAL = 0.125

# The monotonic instant the next catalogue GET may leave. Module state for the
# same reason _post_ready_at is: the budget belongs to the instance, not to a
# caller, and a second copy of this number is a second way to spend it.
_catalog_ready_at = 0.0

# How long after a refusal the catalogue stays paced. An hour is chosen against
# the only number Simkl gave us — the block itself lasts fifteen minutes — so the
# instance spends a while being polite after one clears rather than sprinting
# straight back into whatever caused it. Nothing sets this on a healthy instance,
# so nothing pays for it.
PACE_AFTER_BLOCK_SECONDS = 3600.0
_pace_until = 0.0

# The monotonic instant the next POST may leave. Module state rather than
# per-pool state because the cap is per client id: it is one budget however many
# callers there are, and a second copy of this number would let two of them
# spend it twice. Monotonic seconds, so it survives the suite's fresh loop per
# test in a way an asyncio primitive bound at import would not.
_post_ready_at = 0.0

# 412 is an INSTANCE-WIDE refusal, so the response to it is instance-wide too:
# stop calling Simkl entirely until this deadline passes. Fifteen minutes is a
# starting point chosen to be long enough that a blocked client id is not being
# hammered while blocked, and short enough that an instance recovers without an
# operator noticing anything.
BLOCK_COOLDOWN_SECONDS = 900.0
_blocked_until = 0.0
# WHOSE block it is. See `blocked_seconds_remaining`.
_blocked_client_id = ""


def catalog_client() -> httpx.AsyncClient:
    """The pooled client for the Cloudflare-cached half. Callers must NOT close
    it — the pool owns its lifetime and http_pool.aclose_all closes it at
    shutdown.

    Kept as a named function rather than collapsed into CATALOG_POOL.client() so
    there is one stable seam the test suite can hand a recording double.
    """
    return CATALOG_POOL.client()


def sync_client() -> httpx.AsyncClient:
    """The pooled client for /sync/ and /users/. Same ownership rule as
    catalog_client, and deliberately a different client: sharing one would let a
    bulk catalog read hold every connection while a personal read waits."""
    return SYNC_POOL.client()


def cdn_client() -> httpx.AsyncClient:
    """The pooled client for the calendar CDN (data.simkl.in). Same ownership
    rule as catalog_client — see CDN_POOL for why this is its own pool rather
    than sharing the API host's."""
    return CDN_POOL.client()


def client_for(pool: http_pool.Pool) -> httpx.AsyncClient:
    """The client that belongs with `pool`.

    Exists because a redirect is the one case where the code, rather than the
    call site, has to pick a pool — and `send` must then reach that pool's
    client without the caller's help. It goes through the three named accessors
    above rather than calling `pool.client()` directly so a test double handed
    to `catalog_client` is still the client a followed redirect lands on;
    bypassing them would turn a recorded hop into a real network call.
    """
    if pool is CATALOG_POOL:
        return catalog_client()
    if pool is SYNC_POOL:
        return sync_client()
    if pool is CDN_POOL:
        return cdn_client()
    return pool.client()


# ---------------------------------------------------------------------------
# REDIRECTS: CLASSIFY THE TARGET, THEN PICK THE POOL.
# ---------------------------------------------------------------------------
#
# WHY THIS IS NOT `follow_redirects=True` ON A POOL, WHICH IS WHAT IT USED TO
# BE. A pool is a BUDGET, and the pool is chosen before the request leaves.
# Letting httpx follow a hop inside that request means the answer is fetched
# under a budget decided when nobody yet knew where it would land — the
# ordering is backwards. Today the only hop this app sees is same-host and
# lands inside the parallel-safe family, so nothing was actually wrong; it was
# right by observation rather than by construction, and the difference shows up
# the first time Simkl moves a path.
#
# WHAT IT WOULD COST TO BE WRONG, measured rather than imagined. Simkl allows
# PARALLEL requests only on its Cloudflare-cached endpoints (the trending and
# calendar data files, GET /movies/{id}, GET /tv/{id}, GET /anime/{id},
# GET /tv/episodes/{id}, GET /anime/episodes/{id}); every other path is capped
# at 10 GET/second and 1 POST/second. Benchmarked against the live endpoint,
# the calendar enrichment drain's SEQUENTIAL rate alone was 23.5 requests per
# second — already more than double the ceiling that applies off a cached path.
# So a redirect that quietly carried this traffic somewhere uncached would
# breach the published limit with no constant having changed and nothing in the
# code noticing.
#
# AND TWO CONCRETE LEAKS A CROSS-HOST HOP WOULD OPEN, which is why "refuse
# another host" is the rule rather than a fussy default:
#   - Simkl's 302 Location carries the client id as a QUERY PARAMETER
#     (`/anime/{id}?client_id=<...>`). Following it puts this instance's client
#     id into a URL rather than only into a header, and a cross-host Location
#     would hand that id to whatever host it named.
#   - httpx strips `Authorization` on a cross-origin redirect but does NOT
#     strip custom headers. Simkl's documented alternative credential is the
#     custom `simkl-api-key` header, and this app already sends custom
#     `app-name` / `app-version` headers on every call, so a blindly followed
#     cross-host hop forwards headers httpx will not protect.
#
# THE CLASSIFICATION LIVES HERE, BESIDE THE POOLS, because "which endpoints may
# run in parallel" is the same fact the pool declarations above are built out
# of, and stating it twice is how the two would drift apart. Every Simkl call
# in the app goes through `send`, and `send` is the only caller of this — so
# there is one implementation and no call site can opt out of it.

# 304 is deliberately absent: the calendar CDN answers 304 to a conditional GET
# and that is an answer, not a hop.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# ONE HOP. That is what was measured — /tv/{id} to /anime/{id}, once — and an
# unbounded chain is a way to spend a whole drain's budget on a single title.
# A longer chain is refused loudly rather than walked.
MAX_REDIRECT_HOPS = 1

API_HOST = "api.simkl.com"
CDN_HOST = "data.simkl.in"

# The api.simkl.com paths Simkl's rate-limit documentation names as
# parallel-safe: cached at the edge by id, carrying no per-user state. Written
# as anchored patterns rather than prefixes so `/tv/{id}` cannot be read as
# permission for everything under `/tv/` — `/tv/episodes/{id}` is listed on its
# own precisely because the family is a list of endpoints, not a subtree.
_PARALLEL_SAFE_API_PATHS = (
    re.compile(r"^/movies/[^/]+/?$"),
    re.compile(r"^/tv/[^/]+/?$"),
    re.compile(r"^/anime/[^/]+/?$"),
    re.compile(r"^/tv/episodes/[^/]+/?$"),
    re.compile(r"^/anime/episodes/[^/]+/?$"),
)


def redirect_pool(origin_url: str, target_url: str) -> http_pool.Pool | None:
    """The pool a redirect TARGET deserves, or None when it must not be followed.

    Three answers, and each one is a different fact about the target:

      CATALOG_POOL / CDN_POOL   the target is in the family Simkl declares
                                parallel-safe, so the throughput budget that
                                fetched the origin is the correct budget for
                                the answer too.
      SYNC_POOL                 the target is a Simkl path OUTSIDE that family.
                                The request is still made — the data is real —
                                but under the bounded budget the 10 GET/second
                                ceiling asks for, which is what SYNC_POOL is.
      None                      another host. Refused: see the header and
                                client-id leaks written above this function.

    `origin_url` is what makes the host check meaningful — the target must be
    the host we were already talking to, not merely a host this app happens to
    know the name of.
    """
    origin = httpx.URL(origin_url)
    target = httpx.URL(target_url)
    if target.host != origin.host:
        return None
    if target.host == CDN_HOST:
        # Everything on the calendar CDN is a static data file, edge-cached and
        # explicitly parallel-safe, so there is no sub-classification to make
        # here the way there is on the API host.
        return CDN_POOL
    if target.host != API_HOST:
        # A Simkl host this module does not know the regime for. Refusing beats
        # guessing a budget: the whole point of classifying is not to run
        # traffic under a limit nobody checked.
        return None
    return (CATALOG_POOL if any(pattern.match(target.path)
                                for pattern in _PARALLEL_SAFE_API_PATHS)
            else SYNC_POOL)


# ---------------------------------------------------------------------------
# The circuit breaker.
# ---------------------------------------------------------------------------

def _client_id_of(url: str) -> str:
    """The client id a request carries, or "" when it names none."""
    query = url.split("?", 1)[1] if "?" in url else ""
    for name, value in parse_qsl(query):
        if name == "client_id":
            return value
    return ""


def blocked_seconds_remaining(client_id: str | None = None) -> float:
    """How long the breaker stays open, or 0.0 when Simkl may be called.

    PUBLIC because a caller about to spend a whole batch needs to ask before it
    starts, not discover it one failure at a time: app/calendar/enrich.py's
    drain reads this so a blocked pass costs nothing and — the part that
    matters — records nothing against the titles it would have looked up.

    A BLOCK BELONGS TO THE CLIENT ID THAT EARNED IT, which is why `client_id` is
    worth passing. Simkl counts its limits per `client_id` (and per access token
    for authenticated calls) and answers 412 `client_id_failed` for both an
    invalid id AND an active throttle block — so a DIFFERENT id is a different
    bucket, and the old one's cooldown says nothing about it. Without this an
    operator who corrected a mistyped client id still had to restart the app to
    get it working, because the breaker outlived the credential that opened it.

    NOT A WAY TO ROTATE OUT OF A BLOCK. Simkl extends a block on repeated
    overage and suspends an id for sustained abuse; what this serves is an
    operator FIXING a credential, where local state about the previous one has
    simply stopped applying.

    A caller that does not say which id it is asking about gets the cautious
    answer — still blocked — because "I did not say" must not read as "I am
    somebody else".
    """
    if client_id is not None and str(client_id) != _blocked_client_id:
        return 0.0
    return max(0.0, _blocked_until - _time.monotonic())


def _open_breaker(path: str, client_id: str = "") -> None:
    global _blocked_until, _pace_until, _blocked_client_id
    _blocked_until = _time.monotonic() + BLOCK_COOLDOWN_SECONDS
    # Recorded so the block can be told apart from one belonging to a credential
    # this instance no longer uses — see blocked_seconds_remaining.
    _blocked_client_id = str(client_id or "")
    # The refusal is also what turns pacing ON — see _pace_catalog. It stays on
    # past the block itself, because the moment the block lifts is exactly when
    # a full-speed drain would go straight back at whatever earned it.
    _pace_until = _blocked_until + PACE_AFTER_BLOCK_SECONDS
    # WARNING, NOT DEBUG. This is a fact about the instance's relationship with
    # Simkl — every user on this box has just lost the source for a quarter of
    # an hour — and not a timing detail. The same reasoning the 429 line below
    # carries, one step more serious.
    #
    # AND IT NO LONGER BLAMES THE CLIENT ID, because measurement says that is
    # not what a 412 means here. Against the live endpoint, GET /tv/{id}
    # answered 200 with a valid client id, with a plainly invalid one, and with
    # no client_id parameter at all — the parallel-safe catalogue paths do not
    # authenticate on it. A message naming the credential sent an operator (and
    # the agent helping them) hunting through settings while the real trigger
    # was request RATE. Say what is known — Simkl refused this call — and let
    # the reader look at what the instance was doing.
    logger.warning(
        "Simkl refused this call: %s (HTTP 412). Every Simkl call is refused "
        "locally for the next %.0fs rather than retried, because retrying into "
        "a block extends it. A 412 here is not about the client id — it is "
        "Simkl declining to serve this instance for a while, usually after too "
        "many requests too quickly.", path, BLOCK_COOLDOWN_SECONDS)


def _close_breaker() -> None:
    """Let Simkl be called again immediately. Nothing in the app calls this —
    the deadline is what normally closes the breaker — but a test that has just
    opened it must be able to leave the module as it found it.

    Clears the pacing window too, since `_open_breaker` sets both: a test that
    left pacing armed would slow an unrelated one and look like a hang."""
    global _blocked_until, _pace_until, _blocked_client_id
    _blocked_until = 0.0
    _pace_until = 0.0
    _blocked_client_id = ""


# ---------------------------------------------------------------------------
# The one low-level sender every Simkl call routes through.
# ---------------------------------------------------------------------------

# Bounded two ways, whichever is hit first: a small attempt count AND a
# wall-clock budget, so a viewer-initiated read returns an answer in a bounded
# window rather than hanging behind a large Retry-After. The budget is ELAPSED
# time — request time plus any backoff sleep — not cumulative sleep alone.
_SEND_MAX_ATTEMPTS = 3
_SEND_MAX_ELAPSED = 30.0


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Retry-After as a positive float, or None when it is missing, non-numeric,
    negative, or absurd. None means "no usable wait" — the caller falls back to
    its exponential step rather than sleeping on a value it cannot trust. An
    HTTP-date form simply fails the float parse and takes the same fallback."""
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return None
    if secs <= 0 or secs > 86400:
        return None
    return secs


async def _pace_post() -> None:
    """Hold until this POST is allowed to leave under the 1/second cap."""
    wait = _post_ready_at - _time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)


def _post_sent() -> None:
    """Record that a POST has just been issued, so the next one waits."""
    global _post_ready_at
    _post_ready_at = _time.monotonic() + POST_MIN_INTERVAL


async def _pace_catalog() -> None:
    """Hold until this catalogue GET is allowed to leave — but only if Simkl has
    actually refused this instance recently. Normally this returns instantly.

    PACING EVERY CALL WAS THE WRONG TRADE, and it was made on an unproven theory.
    The observed facts, once both instances were watched: a settled instance
    fires six hundred of these in seconds and Simkl answers every one, and a 412
    could not be reproduced from any machine under any credential shape — valid
    id, invalid id, empty id, absent id, junk bearer, all 200. What pacing every
    call bought was a sevenfold slowdown of the drain (8/second against the ~60
    the pool's concurrency gave) in exchange for a guess.

    WHAT REMAINS IS THE PART THAT IS NOT A GUESS: a 412 did happen, in
    production, and it costs fifteen minutes of no Simkl at all. So the app now
    runs at full speed until Simkl says otherwise, and paces only in the window
    after a refusal, where the one thing we know for certain is that going
    straight back to full rate risks tripping it again. Nothing to tune, and a
    healthy instance never touches this path.
    """
    if _time.monotonic() >= _pace_until:
        return
    await _claim_catalog_slot()


async def _claim_catalog_slot() -> None:
    """Hold until this catalogue GET is allowed to leave under CATALOG_MIN_INTERVAL.

    A TICKET, NOT A CHECK-THEN-SLEEP, and the difference is the whole reason this
    reads differently from `_pace_post` above. That one serves a pool of
    concurrency 1, where the caller waiting IS the only caller. This pool admits
    six at once, and six coroutines that each read the deadline, decide to sleep,
    and then leave together would pace nothing at all — they would agree on the
    same instant and burst exactly as before.

    So the slot is CLAIMED before any await: the deadline is advanced
    synchronously, which under asyncio cannot interleave, and each caller then
    sleeps until the instant it claimed. Concurrency still overlaps the requests;
    what is spaced is when each one LEAVES, which is what a rate limit measures.
    """
    global _catalog_ready_at
    now = _time.monotonic()
    leave_at = max(now, _catalog_ready_at)
    _catalog_ready_at = leave_at + CATALOG_MIN_INTERVAL
    wait = leave_at - now
    if wait > 0:
        await asyncio.sleep(wait)


async def send(client: httpx.AsyncClient, method: str, url: str, *,
               pool: http_pool.Pool, headers: dict | None = None,
               json=None, timeout: float | None = None) -> httpx.Response:
    """Issue one Simkl request under that pool's rules, and return its response.

    `pool` IS NOT OPTIONAL AND MUST BE THE POOL `client` CAME FROM. The two
    travel together because they are two halves of one budget: the client owns
    the connections and the pool owns the gate that decides how many requests
    may be in flight through them. Passing one regime's client with the other's
    pool would honour neither rule, so the pairing is stated at every call site
    instead of being defaulted here.

    Returns the httpx.Response for any status this function does not act on, and
    lets network errors propagate unchanged — reading a 401 or an empty body is
    the caller's job. Three statuses are acted on:

      412  the client id is blocked instance-wide. Opens the breaker and raises
           SimklBlockedError. NEVER retried: see SimklBlockedError.
      429  backs off — a numeric Retry-After wins over the exponential schedule
           (1s, 2s, 4s) — and raises SimklRateLimitError once the attempt count
           or the wall-clock budget is spent, rather than a fabricated response.
      3xx  re-issued at the target, on the pool `redirect_pool` says that target
           deserves — which may not be the pool this call started on, and may be
           a refusal. This is the ONLY place in the app that follows a Simkl
           redirect; no pool is allowed to do it on its own.

    A POST is additionally paced to one per second (see POST_MIN_INTERVAL).
    """
    hops = 0
    while True:
        resp = await _send_once(client, method, url, pool=pool, headers=headers,
                                json=json, timeout=timeout)
        if resp.status_code not in REDIRECT_STATUSES:
            return resp
        location = resp.headers.get("location")
        if not location:
            # A redirect with nowhere to go is not a hop, it is a malformed
            # answer. Handed back as-is so the caller reads it as the non-200 it
            # is, rather than being turned into an exception here.
            logger.warning("Simkl answered HTTP %s for %s with no Location header",
                           resp.status_code, url)
            return resp
        if method.upper() != "GET":
            # Only a GET is safe to replay at a new URL without deciding what
            # happens to its BODY — and the only Simkl POSTs this app makes are
            # under /sync/, which does not redirect. Returned untouched rather
            # than guessed at.
            logger.warning("Simkl redirected a %s of %s to %s; not following — only "
                           "GET redirects are replayed", method.upper(), url, location)
            return resp
        target = str(httpx.URL(url).join(location))
        hops += 1
        if hops > MAX_REDIRECT_HOPS:
            raise SimklError(
                f"Simkl redirected {url} more than {MAX_REDIRECT_HOPS} time(s); "
                f"refusing to keep following at {target}.", resp.status_code)
        next_pool = redirect_pool(url, target)
        if next_pool is None:
            # LOUD, AND WITHOUT THE TARGET'S CREDENTIALS. The refusal is the
            # security property, so it must be visible in the log rather than
            # showing up as a title that mysteriously never enriches.
            logger.warning(
                "Simkl redirected %s off its own host to %s — refusing to follow, "
                "because this app's custom headers and its client id would travel "
                "with the request.", url, target)
            raise SimklError(
                f"Simkl redirected {url} to another host; not following.",
                resp.status_code)
        if next_pool is not pool:
            # The budget changed, so the connections change with it — the two
            # are halves of one thing (see this function's own docstring).
            logger.info("Simkl redirected %s to %s; re-issuing on the %s pool",
                        url, target, next_pool.name)
            client = client_for(next_pool)
            pool = next_pool
        # Headers carry over unchanged, and that is safe for exactly one
        # reason: redirect_pool has already established the target is the same
        # host. It is not a general permission.
        url = target


async def _send_once(client: httpx.AsyncClient, method: str, url: str, *,
                     pool: http_pool.Pool, headers: dict | None = None,
                     json=None, timeout: float | None = None) -> httpx.Response:
    """One request to one URL under one pool's gate, with the 412 breaker and
    the 429 retry loop — everything that is about THIS request and not about
    where its answer might point.

    Split out of `send` so the redirect walk above runs OUTSIDE any pool's gate.
    Holding one pool's semaphore while waiting for another's is how two pools
    that redirect into each other would deadlock, and the split makes that
    impossible rather than merely unlikely.
    """
    path = url.split("?", 1)[0].replace(API_BASE, "") or url
    # Read off the URL rather than taken as an argument: every Simkl request
    # carries the client id as a query parameter (api_params), so the request
    # itself already says whose block would apply to it.
    asked_with = _client_id_of(url)
    remaining_block = blocked_seconds_remaining(asked_with)
    if remaining_block > 0:
        # Refused HERE, before the gate and before any socket: the whole point
        # of the breaker is that this request never reaches Simkl.
        raise SimklBlockedError(
            f"Simkl has blocked this instance's client id; not calling {path} for "
            f"another {remaining_block:.0f}s.", 412)
    method_up = method.upper()
    start = _time.monotonic()
    # The gate is held across the request AND the backoff sleep, not just the
    # request: during a 429 storm this makes callers wait their turn instead of
    # every coroutine re-firing the instant a slot frees and tripping the limit
    # again.
    async with pool.gate():
        attempt = 0
        while True:
            attempt += 1
            remaining = _SEND_MAX_ELAPSED - (_time.monotonic() - start)
            if remaining <= 0:
                raise SimklRateLimitError(
                    f"Simkl rate limit not cleared within {_SEND_MAX_ELAPSED:.0f}s for {path}.", 429)
            attempt_timeout = remaining if timeout is None else min(timeout, remaining)
            if method_up == "GET":
                # Paced only on the API host's catalogue pool. The CDN pool is
                # deliberately exempt: those files are static, edge-served, and
                # carry no client id, so they are not spending the budget this
                # paces — and a month's calendar fill would crawl for no reason.
                if pool is CATALOG_POOL:
                    await _pace_catalog()
                resp = await client.get(url, headers=headers, timeout=attempt_timeout)
            elif method_up == "POST":
                await _pace_post()
                try:
                    resp = await client.post(url, headers=headers, json=json,
                                             timeout=attempt_timeout)
                finally:
                    # Recorded even when the request failed. The cap counts
                    # requests Simkl received, and a call that timed out on our
                    # side may well have arrived on theirs.
                    _post_sent()
            else:
                resp = await client.request(method, url, headers=headers, json=json,
                                            timeout=attempt_timeout)
            if resp.status_code == 412:
                _open_breaker(path, asked_with)
                raise SimklBlockedError(
                    f"Simkl refused this instance's client id on {path} (HTTP 412).", 412)
            if resp.status_code != 429:
                return resp
            if attempt >= _SEND_MAX_ATTEMPTS:
                raise SimklRateLimitError(
                    f"Simkl still rate-limiting after {attempt} attempt(s) for {path}.", 429)
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = float(2 ** (attempt - 1))  # 1s, 2s, 4s — one storm's worth
            # Don't begin a sleep that would carry elapsed past the budget: stop
            # and raise now rather than sleeping most of the way in and raising
            # anyway.
            if (_time.monotonic() - start) + wait > _SEND_MAX_ELAPSED:
                logger.warning("Simkl rate-limited %s with a %.0fs Retry-After, over the "
                               "%.0fs budget — giving up on this call", path, wait, _SEND_MAX_ELAPSED)
                raise SimklRateLimitError(
                    f"Simkl Retry-After would exceed the {_SEND_MAX_ELAPSED:.0f}s budget for {path}.", 429)
            # WARNING, NOT DEBUG, AND ALWAYS. Being rate-limited is the
            # difference between "that read was slow" and "we were told to slow
            # down", and those have opposite fixes. Rare by construction — the
            # pools exist to keep it from happening — so a run of these lines is
            # itself the signal that a pool is sized wrong.
            logger.warning("Simkl rate-limited %s — attempt %d, waiting %.1fs before retry",
                           path, attempt, wait)
            await asyncio.sleep(wait)


async def _fetch_json(client: httpx.AsyncClient, settings: Settings, url: str, path: str,
                      pool: http_pool.Pool, fresh: bool, raise_errors: bool,
                      private: bool = False):
    """One GET, reduced to "(the parsed body or None, how many pages there are)".
    No caching.

    Split out of cached_get so that function is only the CACHE POLICY and this
    one is only the call and what its answer means. The two change for different
    reasons: a new caching mode touches the policy alone, and a change in how
    Simkl reports a failure touches this alone.

    THE PAGE COUNT COMES BACK BESIDE THE BODY because it is not IN the body:
    Simkl states it in the `X-Pagination-Page-Count` response header, and a
    caller assembling a paginated answer cannot ask for it afterwards — by then
    the response is gone. 1 when the header is absent or unreadable, which is
    every unpaginated endpoint and is the answer that stops a loop after one
    pass.
    """
    t0 = _time.perf_counter()
    try:
        resp = await send(client, "GET", url, pool=pool,
                          headers=api_headers(settings, private=private))
    except httpx.HTTPError as exc:
        # A transport failure means we never got a real answer. Unlike a 404 or
        # an empty list that is NOT "Simkl says there is nothing here", so it
        # must never collapse into the None callers read as an empty result.
        # Always raise, regardless of raise_errors. (SimklRateLimitError and
        # SimklBlockedError are not httpx errors and propagate on their own, for
        # the same reason — their callers degrade them deliberately.)
        logger.warning("Simkl GET %s failed: %s", path, exc)
        raise SimklError(f"Could not reach Simkl: {exc}") from exc
    try:
        pages = max(1, int(resp.headers.get("x-pagination-page-count") or 1))
    except (TypeError, ValueError):
        pages = 1
    _perf.debug("netGET    %s -> %s  %.0fms%s%s", path, resp.status_code,
                (_time.perf_counter() - t0) * 1000.0,
                " (fresh)" if fresh else " (miss)", perftrace.activity_tag())
    if resp.status_code != 200:
        logger.warning("Simkl GET %s -> HTTP %s: %s", path, resp.status_code, resp.text[:200])
        if raise_errors:
            if resp.status_code == 401:
                raise SimklError(
                    "Simkl rejected the credentials (401). Simkl issues no refresh "
                    "token, so the link has to be made again.", 401)
            raise SimklError(f"Simkl API returned HTTP {resp.status_code}.", resp.status_code)
        return None, pages
    try:
        return resp.json(), pages
    except ValueError:
        logger.warning("Simkl GET %s -> unreadable JSON body", path)
        if raise_errors:
            raise SimklError("Simkl API returned an unreadable response.")
        return None, pages


# How many pages one paginated read may walk. Simkl caps `page` at 20 server
# side, so this is that cap rather than a policy of ours — a query that would
# need more has already returned five hundred results and the viewer is going to
# refine it rather than scroll.
MAX_PAGES = 20


async def cached_paged_get(
    client: httpx.AsyncClient,
    settings: Settings,
    path: str,
    params: dict | None = None,
    *,
    pool: http_pool.Pool,
    ttl_seconds: int | None = None,
    raise_errors: bool = False,
    app: str = DEFAULT_APP_NAME,
    max_pages: int = MAX_PAGES,
) -> list:
    """Every page of a paginated GET, joined into one list and cached as ONE
    ANSWER.

    THE PAGINATION IS INVISIBLE TO EVERY CALLER AND TO THE CACHE, which is the
    whole design. What gets stored is "the results for this query", not "page one
    of this query" — so a cache hit returns the complete list and nothing
    downstream has to know how many requests it took to build, or re-derive that
    from headers it no longer has. Caching page one alone would be worse than not
    caching: a hit would silently serve a truncated answer with nothing to say it
    was short.

    `page` IS THEREFORE NOT PART OF THE KEY. `params` names the question — the
    query text, the size of a page — and the key is built from it before any page
    is asked for, so every page of one search writes into one entry.

    SEQUENTIALLY, and for search that is a rule rather than a preference: Simkl
    permits parallel requests only against the edge-cached endpoints, and
    `/search/*` answers `cf-cache-status: DYNAMIC`. Walking pages in parallel is
    the shape Simkl names as a reason a client id is suspended.

    A PAGE THAT FAILS ENDS THE WALK rather than failing what came before it,
    unless `raise_errors` says the caller would rather know. Partial results are
    the honest answer to "the first two pages arrived and the third did not", and
    they are what the viewer would have seen had the query been narrower.
    """
    key = cache_key(path, params)
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_minutes * 60
    cached = await cache.get(key, ttl)
    if cached is not None:
        _perf.debug("cacheHIT  %s", path)
        return cached if isinstance(cached, list) else []
    out: list = []
    page = 1
    while page <= max_pages:
        query = {**(params or {}), "page": str(page)}
        url = f"{API_BASE}/{path}?{urlencode(api_params(settings, query, app=app))}"
        data, pages = await _fetch_json(client, settings, url, path, pool,
                                        fresh=False, raise_errors=raise_errors)
        if not isinstance(data, list):
            break
        out.extend(data)
        if page >= min(pages, max_pages):
            break
        page += 1
    await cache.set(key, out)
    return out


async def cached_get(
    client: httpx.AsyncClient,
    settings: Settings,
    path: str,
    params: dict | None = None,
    *,
    pool: http_pool.Pool,
    ttl_seconds: int | None = None,
    fresh: bool = False,
    raise_errors: bool = False,
    private: bool = False,
    cache_only: bool = False,
    app: str = DEFAULT_APP_NAME,
):
    """GET a Simkl path (with disk caching keyed by the path and the parameters
    that describe the question — see `cache_key`). Returns parsed JSON or None.

    `private=True` MEANS THE RESPONSE DEPENDS ON WHOSE TOKEN ASKED — a watch
    history, a library bucket, an activity beacon. The cache is keyed by the
    request and shared by the whole instance, and Simkl carries the token in a
    HEADER, so every user's /sync/ request asks the IDENTICAL question: a
    response written to the cache without this flag would be served back to the
    wrong person. Every call under /sync/ and /users/ passes it. Nothing that
    reads personal data may reach this function without it — which is also why
    leaving the client id out of the key is safe: a response that depended on the
    caller is never written here at all.

    POST RESPONSES ARE NEVER CACHED AT ALL, which is why this function is GET
    only: /sync/watched is a POST whose meaning is in the request BODY, and a
    URL key cannot express that. THE FREE-TEXT SEARCH ENDPOINTS ARE NOT IN
    THAT GROUP — measured live, `GET /search/tv|anime|movie` all answer 200
    with results; the POSTs this area of Simkl's API actually reserves are
    `/search/file` (identify one video file) and `/search/random`, which this
    app has no use for. Search rides this function like any other catalogue
    GET (see app/providers/simkl/search.py).

    `ttl_seconds` overrides the default detail TTL; `fresh=True` skips the cache
    read but still refreshes it; `raise_errors=True` raises SimklError instead of
    returning None, for callers where a swallowed 401 would look identical to a
    genuine empty result.

    `cache_only=True` NEVER makes a network call: it returns the cached value —
    even past its TTL — or None. This is what lets a public page reuse data the
    owner's own views already fetched without a stranger being able to make this
    instance spend its Simkl budget on demand.
    """
    # TWO ADDRESSES, DELIBERATELY. The request carries the client id because
    # Simkl will not answer a cold one without it; the cached copy is filed
    # without it because the content behind it does not vary by credential. See
    # `cache_key`.
    url = f"{API_BASE}/{path}?{urlencode(api_params(settings, params, app=app))}"
    key = cache_key(path, params)
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_minutes * 60
    if not fresh and not private:
        cached = await cache.get(key, ttl)
        if cached is not None:
            _perf.debug("cacheHIT  %s", path)
            return cached
    if cache_only:
        # Stale beats blank here: this caller can never trigger a refresh to fix
        # a hard miss anyway.
        return await cache.get_stale(key)
    data, _pages = await _fetch_json(client, settings, url, path, pool,
                                     fresh=fresh, raise_errors=raise_errors, private=private)
    if data is None:
        # None is how a swallowed failure comes back, and it is also what a
        # literal `null` body would parse to. Neither is worth storing: the read
        # above treats a cached None as a MISS, so such a row could never be
        # served as a hit anyway.
        return None
    if not private:
        await cache.set(key, data)
    return data
