"""FastAPI application — served under Hypercorn.

Bootstrap only: logging, the app object and its middleware, the router includes,
the startup/shutdown lifespan, the background heartbeat, and the error page
handler. Every route lives in one of the app/*_routes.py modules, grouped by the
thing it serves rather than by having grown up here.

Two routes stay: /healthz, which is about the PROCESS rather than any feature,
and /api/changelog, which is the release history of the app as a whole and
belongs to no page family.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from . import auth
from . import authz
from . import cache
from . import changelog
from . import clock
from . import config
from . import chrome
from . import db
from . import http_pool
from . import perftrace
from . import secrets_box
from . import settings_routes
from .auth import admin_routes
from .auth import encryption_flow
from .auth import encryption_routes
from .auth import plex as plex_auth
from .auth import plex_routes
from .auth import routes as auth_routes
from .auth import secrets_backfill
from .auth import simkl_routes
from .auth import trakt_routes
from .calendar import cache as calendar_cache
from .calendar import enrich as calendar_enrich
from .calendar import routes as calendar_routes
from .calendar import share_card_cache
from .calendar import share_routes
from .distrakt import routes as distrakt_routes
from .integrations import routes as integrations_routes
from .media import artwork, posters
from .ranker import routes as ranker_routes
from .sources import routes as sources_routes
from .auth import AuthLevel
from .config import load_settings
from .templating import templates

logger = logging.getLogger(__name__)

# Configured here (not only in run.py) so `hypercorn app.main:app` — what the
# Docker image's CMD runs directly, bypassing run.py entirely — gets the same
# app.* diagnostics and Trakt-call tracing as the dev runner, instead of
# Python's silent WARNING-only default. LOG_LEVEL controls the app's own
# loggers (including "app.perf", which every outbound Trakt call logs a line
# to at DEBUG — see app/providers/trakt/, app/calendar/cache.py, app/auth/trakt.py);
# third-party libraries stay at WARNING regardless, since their own DEBUG
# output is rarely what anyone actually wants. basicConfig() only attaches a
# handler if the root logger doesn't already have one, so when run.py has
# already called it (the dev path) this is a no-op and run.py's config wins;
# in Docker, where nothing else calls it, this is the only config that fires.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
# HYPERCORN'S ACCESS LOG CANNOT BE TURNED OFF FROM HERE, and trying is the
# obvious wrong answer. Hypercorn builds its Logger AFTER importing this module
# and calls setLevel on "hypercorn.access" itself, from config.loglevel (default
# INFO) — so any level set here is overwritten seconds later. It also attaches its
# own handler and sets propagate=False, which is why those lines carry a different
# format from everything above.
# The only thing that decides it is whether the server is given an access log
# target at all: unset, Hypercorn creates no access logger and the whole path is
# dead. So the switch lives at the invocation — run.py's config.accesslog (opt in
# with ACCESS_LOG=1) and the Dockerfile's CMD, which must not pass
# --access-logfile. Both are commented there; this note exists so the next person
# to try it from Python finds out here rather than from a deploy.

BASE_DIR = Path(__file__).resolve().parent
HEARTBEAT_SECONDS = 60


async def _warn_on_key_state(health: str) -> None:
    """One loud, actionable startup line for each unhealthy key state.

    The missing-key warning is worded as strongly as it is on purpose: the sealed
    values are intact and come back the instant the key is restored, but a well-meaning
    re-link or credential re-save overwrites them for good, so the one thing the
    operator must NOT do is exactly the thing that looks like the fix."""
    if health == encryption_flow.KEY_MISMATCH:
        logger.error(
            "ENCRYPTION_KEY does not match the stored secrets — the app is gated to the "
            "admin recovery screen. Restore the original key to get everything back, or "
            "run the recovery reset to discard the unrecoverable values.",
        )
        return
    if health == encryption_flow.KEY_MISSING:
        logger.error(
            "ENCRYPTION_KEY is not set but stored secrets are sealed. They are UNREADABLE "
            "but INTACT — restore ENCRYPTION_KEY to get them back. Do NOT re-link Trakt or "
            "re-save API keys while the key is missing: that overwrites the encrypted "
            "values and loses them for good.",
        )
        return
    warning = secrets_box.plaintext_storage_warning(await secrets_backfill.unsealed_present())
    if warning:
        logger.warning(warning)


async def _sweep_auth_rows() -> None:
    """Delete expired sessions, abandoned OAuth/PIN handshakes, and login/
    registration attempt rows old enough that no rate limiter still needs them.

    All three expire by a stored timestamp rather than by any self-expiring
    token, so without this sweep their rows would accumulate forever. Cheap
    indexed deletes.
    """
    now = db.now()
    await auth.sweep_expired_sessions(now)
    await db.execute("DELETE FROM auth_handshakes WHERE expires_at <= ?", (now,))
    await auth.sweep_login_attempts(now)
    # Age out expired cache windows and hold the shared blob table under its
    # size cap, evicting least-recently-stored first.
    settings = load_settings()
    await cache.sweep(now, settings.api_cache_max_bytes)
    # Same idea, one table over: a Simkl enrichment row past its retention
    # window is not urgent to keep — the next read that resolves to that
    # title just queues it again (see app/calendar/enrich.py).
    await calendar_enrich.sweep(now)
    # Drop poster-URL sightings past their retention window, and hold the
    # on-disk poster tile cache under its own size cap, oldest file first. The
    # tile sweep is filesystem walking, not a DB call, so it goes through a
    # worker thread rather than the event loop.
    await artwork.sweep(now)
    await anyio.to_thread.run_sync(posters.sweep, settings.poster_cache_max_bytes)
    # And the rendered share cards, which age out at 90 days and are held under
    # their own byte budget behind that. Same worker-thread reason as the posters
    # above: it is a directory walk, and it is deliberately here rather than on
    # the request path that writes the files.
    await anyio.to_thread.run_sync(share_card_cache.sweep, settings.share_card_cache_max_bytes)


async def _heartbeat_tick() -> None:
    """One pass of the background maintenance work, each job isolated from the
    others.

    The except is deliberately broad and deliberately silent, which is the one
    place in the app that is the right call: this loop is the only thing renewing
    the Trakt token and the only thing sweeping expired rows, so ANY escaping
    exception — a transient database lock, an unreachable Sonarr, a bug in one of
    the four — would end all four for the lifetime of the process, and nothing
    would say so until somebody noticed their session had stopped expiring. Each
    job is idempotent and runs again in a minute, so skipping one tick of it
    costs nothing.
    """
    for job in (
        integrations_routes.refresh_integration_health,
        settings_routes.maybe_refresh_trakt_token,
        _sweep_auth_rows,
        lambda: calendar_cache.prewarm_calendar_cache(load_settings()),
        # run_drain, not drain directly — the same coalescing latch a fill
        # uses (app/calendar/enrich.py's schedule_drain), so a heartbeat tick
        # landing while a fill-triggered pass is already running folds into
        # it rather than starting a second, concurrent pass of its own.
        lambda: calendar_enrich.run_drain(load_settings()),
    ):
        try:
            await job()
        except Exception:
            pass


async def _heartbeat_loop() -> None:
    while True:
        await _heartbeat_tick()
        await asyncio.sleep(HEARTBEAT_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema first — everything after this point may touch the database.
    await db.init()
    # The detail-lookup cache moved into the api_cache table; drop the old
    # data/cache/*.json directory now that the schema that replaces it is in place.
    cache.discard_legacy_dir()
    # Generated once and persisted: this UUID names the INSTALLATION to Plex,
    # not any particular user, and every PIN request needs it.
    await plex_auth.ensure_client_identifier()
    # Loud, once, at boot: a route nobody declared is being refused to every
    # caller, and the operator should hear about it here rather than from a user.
    authz.log_undeclared_routes(_app)
    # Also loud and once, and for the same reason: PUBLIC_BASE_URL beats the saved
    # base URL, so an operator who left it set would otherwise see a value save in
    # Settings and never take effect. Silent unless the two actually differ.
    config.warn_if_public_base_url_overridden()
    # And loudest of the three: an instance running on a faked date must say so
    # every single time it boots, because nothing else about it looks unusual.
    clock.warn_if_overridden()
    # Turn encryption on non-interactively when the escape-hatch env var is set with
    # a valid key, then derive the key-health once so the request gate can steer an
    # administrator to recovery on a wrong key BEFORE any ordinary load hits a sealed
    # secret and raises. Both happen before the integration health check below, which
    # reads real secrets and would raise on a wrong key.
    try:
        # Broad on purpose: this is an optional convenience, and every way it can
        # fail (a malformed key, an unreadable secrets row, a half-written enable)
        # is one the app can still start without — it just starts unencrypted, and
        # the warning below says which state it is in.
        await encryption_flow.run_env_escape_hatch()
    except Exception:
        logger.warning("Non-interactive encryption enable failed at startup", exc_info=True)
    health = await encryption_flow.refresh_health()
    await _warn_on_key_state(health)
    if health != encryption_flow.KEY_MISMATCH:
        await integrations_routes.refresh_integration_health()
    task = asyncio.create_task(_heartbeat_loop())
    # Observes the loop it runs on and is depended on by nothing — see
    # perftrace.watch_event_loop for what its one log line is able to say that
    # no per-request timing can.
    watchdog = asyncio.create_task(perftrace.watch_event_loop())
    try:
        yield
    finally:
        watchdog.cancel()
        task.cancel()
        # Every pooled client at once, rather than naming each service here — a
        # list in the composition root is one a new provider would silently fall
        # out of, leaving its connections open at shutdown.
        await http_pool.aclose_all()


# Every load carries the page's scripts/style.css/fonts plus (mostly) whatever
# cache-busting token was minted at the last deploy; a short max-age still saves a full refetch
# within a session without risking the "forgot to register the file" staleness a
# long/immutable one would cause. ETags (StaticFiles' own default) still catch a change within
# that window.
_STATIC_CACHE_HEADERS = {"Cache-Control": "max-age=600"}

# Fonts are the one exception, and it is safe for a reason that does not hold for
# anything else under /static: a vendored woff2 cannot change without its FILENAME
# changing, because the name carries the version (inter-v20-latin-400). So there is
# no "forgot to bust it" staleness to protect against — a new font is a new
# URL. At 600s a viewer who opens the calendar twice a day re-downloads ~86 KB both
# times and watches the text re-flow from the fallback face on each; a year makes
# that a true-cold-load-only event.
_FONT_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


class _CachedStaticFiles(StaticFiles):
    def file_response(self, full_path, *args, **kwargs) -> Response:
        response = super().file_response(full_path, *args, **kwargs)
        headers = (_FONT_CACHE_HEADERS if Path(full_path).parent.name == "fonts"
                   else _STATIC_CACHE_HEADERS)
        response.headers.update(headers)
        return response


# The interactive API docs are off: they are a complete, unauthenticated
# inventory of every endpoint in the app, and nothing here is a public API that
# anyone consumes from a schema.
app = FastAPI(title=chrome.PRODUCT_NAME, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
# Added before authz.install() below, so it nests INSIDE the authz middleware
# stack (Starlette's registration order is reversed — see authz.install's own
# docstring) and compresses the actual route responses, including the
# multi-megabyte calendar HTML. authz's own short-circuit responses (redirects,
# 403s) are tiny, so shipping those uncompressed costs nothing.
app.add_middleware(GZipMiddleware)
app.mount("/static", _CachedStaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(auth_routes.router)
app.include_router(trakt_routes.router)
app.include_router(simkl_routes.router)
app.include_router(plex_routes.router)
app.include_router(admin_routes.router)
app.include_router(encryption_routes.router)
app.include_router(share_routes.router)
app.include_router(ranker_routes.router)
app.include_router(calendar_routes.router)
app.include_router(distrakt_routes.router)
app.include_router(sources_routes.router)
app.include_router(settings_routes.router)
app.include_router(integrations_routes.router)

# The two routes below are registered through this, which requires an access
# level and refuses to register one without it.
guard = authz.Guard(app)
# Styles, scripts, images, and the easter egg's audio. Nothing here is derived
# from anyone's data.
authz.declare_mount(app, "/static", AuthLevel.PUBLIC)
authz.install(app)
# LAST, and therefore outermost: what it reports has to be the time the client
# actually waited, which includes the gates authz just installed and the gzip of
# the response below them.
perftrace.install(app)


# The status codes that get a rendered page rather than Starlette's bare
# {"detail": "Not Found"} JSON. Deliberately short: a wrong URL and a refused one
# are what a person typing in the address bar actually hits. Everything else
# keeps the default, because an unexpected status is worth seeing raw.
#
# The third field is the template. A mistyped address gets the intermission
# page — a dead end is the one place there is room to be charming. A 403 keeps
# the plain card: somebody who has just been told no is not in the mood, and the
# two pages stay deliberately similar in what they SAY (see below). Swapping
# error_lobby.html back to error.html here is the whole rollback.
_NOT_FOUND_PAGE = (
    "Not found", "There's nothing at this address. It may have moved, or the link may have been mistyped.", "error_lobby.html",
)
_ERROR_PAGES: dict[int, tuple[str, str, str]] = {
    404: _NOT_FOUND_PAGE,
    405: _NOT_FOUND_PAGE,  # why: see handle_http_exception's docstring
    403: ("Not allowed", "Your account can't open this. If that seems wrong, ask an administrator to check your access.", "error.html"),
}


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """A themed page for a browser, the unchanged JSON for everything else.

    Split on Accept rather than on the path: /api/... is not the only thing a
    script calls, and a page is only ever useful to something that renders one.
    A 405 is folded into "not found" on purpose — telling a stranger that an
    address exists but takes a different method is an inventory of the app.
    """
    page = _ERROR_PAGES.get(exc.status_code)
    if page is None or "text/html" not in (request.headers.get("accept") or "").lower():
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
        return JSONResponse({"ok": False, **detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    title, message, template = page
    return templates.TemplateResponse(
        request, template,
        # No user to gate a header with — these pages have none — but the version
        # and build labels come from the one function that knows them, same as
        # every other page.
        {"status": exc.status_code, "title": title, "message": message,
         **chrome.page_context(None)},
        status_code=exc.status_code,
    )


# Deliberately reachable by anyone: a container orchestrator's liveness probe
# carries no session, and the response says nothing about the instance.
@guard.get("/healthz", AuthLevel.PUBLIC)
async def healthz():
    return {"ok": True}


# SESSION rather than PUBLIC: release notes are not sensitive, but they are a
# feature inventory and a version history, and handing that to an unauthenticated
# visitor is a small gift to somebody probing the instance. Any signed-in account
# may read them, approved for anything or not — the menu entry sits on every page
# and gating it further would make the header a different shape per account.
@guard.get("/api/changelog", AuthLevel.SESSION)
async def api_changelog(request: Request):
    """The modal's body, as an HTML fragment. Fetched once on first open."""
    return templates.TemplateResponse(
        request, "_changelog.html", {"releases": changelog.releases()})
