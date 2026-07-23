"""Route authorization: the declaration mechanism, deny-by-default enforcement,
and the request-shape rules that protect mutating endpoints from cross-site
submission.

EVERY ROUTE DECLARES EXACTLY ONE ACCESS LEVEL, at the point it is registered:

    guard = Guard(app)            # or Guard(some_router)

    @guard.get("/api/settings", AuthLevel.ADMIN)
    async def get_settings(...): ...

    @guard.post("/api/state", AuthLevel.CALENDAR_APPROVED)
    async def post_state(...): ...

The level is a required positional argument, so a route that forgets one raises
at import time instead of quietly shipping open. Declaring does two things:
attaches the FastAPI dependency that enforces the level, and records the level on
the endpoint function, which is what the startup audit and the middleware below
read back.

DENY-BY-DEFAULT, in three layers, deliberately overlapping:

  1. The registrar cannot be called without a level.
  2. A request matching a route that carries no declaration is refused with 403
     before the handler runs — so a route added later with a bare `@app.get` is
     closed rather than open.
  3. Startup logs an ERROR naming every undeclared route, and a test fails on
     any. The loud version, for the case where somebody notices the log before a
     user notices the 403.

Only layer 1 is convenient; the other two exist because "someone adds a route and
forgets to gate it" is the failure this whole mechanism is built around.

THE MIDDLEWARE ALSO CARRIES two rules that apply to every mutating request:

  - JSON bodies only. A form-encoded cross-origin POST is a CORS "simple
    request": the browser sends it with cookies and with no preflight, so an
    endpoint that accepts one is defended by the cookie's SameSite attribute
    alone. Rejecting anything but application/json removes that whole shape.
  - Same-origin only. If the request carries an Origin it must match this
    instance's own origin; if it carries none, Sec-Fetch-Site must say the
    request came from this site or from a user typing it. This is defence in
    depth behind SameSite=Lax, and it is what a hostile SIBLING SUBDOMAIN (which
    SameSite considers same-site) runs into.

Both are enforced here rather than per-route so that a new endpoint is covered
the moment it exists. Non-browser clients (curl, scripts) must therefore send
`Content-Type: application/json` and either an `Origin` header or
`Sec-Fetch-Site: same-origin`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Match

from . import auth, db, encryption_flow
from .auth import DEPENDENCY_FOR_LEVEL, AuthError, AuthLevel
from .config import Settings, load_settings

logger = logging.getLogger(__name__)

# Stamped onto endpoint functions (and onto Mount objects, which have no
# endpoint) by declare(). Read back by the audit and the middleware.
LEVEL_ATTR = "tns_auth_level"

# The methods that can change state, and so the ones the body-shape and origin
# rules apply to. HEAD and OPTIONS are absent for the same reason GET is.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Reachable before any account exists. Everything else is refused until the
# instance has been set up, because until then there is nobody who could be
# authorized and the operator's only useful destination is the setup form.
# The sign-in page is here because it sends a visitor to setup itself, with a
# redirect rather than a refusal.
FIRST_RUN_PATHS = ("/onboarding", "/healthz", "/login")
FIRST_RUN_PREFIXES = ("/static/",)


# ---------------------------------------------------------------------------
# declaring
# ---------------------------------------------------------------------------

def declare(func: Callable, level: AuthLevel) -> Callable:
    """Record `level` on an endpoint function and return it.

    Re-declaring the same function at a second path is fine as long as the level
    matches; declaring two different levels for one function is a mistake that
    would leave the audit unable to say which applies, so it raises.
    """
    existing = getattr(func, LEVEL_ATTR, None)
    if existing is not None and existing != level:
        raise RuntimeError(
            f"{getattr(func, '__qualname__', func)} is already declared "
            f"{existing.value}; it cannot also be {level.value}. Split it into two "
            "handlers rather than sharing one across access levels."
        )
    setattr(func, LEVEL_ATTR, level)
    return func


class Guard:
    """Registers routes on a FastAPI app or APIRouter, requiring a level.

    A thin wrapper: every keyword FastAPI's own decorators take is passed
    straight through, and the returned route object is FastAPI's. The only
    additions are the mandatory level, the dependency it implies, and the
    declaration the audit reads.
    """

    def __init__(self, target: FastAPI | APIRouter):
        self._target = target

    def get(self, path: str, level: AuthLevel, **kwargs):
        return self._register("get", path, level, kwargs)

    def post(self, path: str, level: AuthLevel, **kwargs):
        return self._register("post", path, level, kwargs)

    def put(self, path: str, level: AuthLevel, **kwargs):
        return self._register("put", path, level, kwargs)

    def patch(self, path: str, level: AuthLevel, **kwargs):
        return self._register("patch", path, level, kwargs)

    def delete(self, path: str, level: AuthLevel, **kwargs):
        return self._register("delete", path, level, kwargs)

    def _register(self, method: str, path: str, level: AuthLevel, kwargs: dict):
        if not isinstance(level, AuthLevel):
            raise TypeError(f"{method.upper()} {path}: pass an AuthLevel, not {level!r}.")
        dependency = DEPENDENCY_FOR_LEVEL[level]
        if dependency is not None:
            # Copied rather than appended to, so a caller-supplied list isn't
            # mutated behind their back.
            kwargs["dependencies"] = [*kwargs.get("dependencies", []), Depends(dependency)]
        decorate = getattr(self._target, method)(path, **kwargs)

        def wrapper(func: Callable) -> Callable:
            declare(func, level)
            decorate(func)
            return func

        return wrapper


def declare_mount(app: FastAPI, path: str, level: AuthLevel) -> None:
    """Declare a mounted sub-application (the static file server).

    Mounts have no endpoint function to stamp, so the declaration goes on the
    Mount itself. Without this the audit would report `/static` as undeclared,
    which is correct — it just isn't declarable through the registrar.
    """
    for route in app.routes:
        if getattr(route, "path", None) == path:
            setattr(route, LEVEL_ATTR, level)
            return
    raise LookupError(f"Nothing is mounted at {path}.")


def route_level(route: BaseRoute) -> AuthLevel | None:
    """The level declared for a route, or None if it has no declaration."""
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        return getattr(endpoint, LEVEL_ATTR, None)
    return getattr(route, LEVEL_ATTR, None)


def _child_routes(route: BaseRoute) -> list[BaseRoute]:
    """The routes nested inside a container route, if it is one.

    An included router is a single entry in `app.routes` that stands in for all
    of its routes, and a mount is an entry standing in for a whole sub-app. Both
    have to be opened up, or the audit would see one opaque object where it needs
    to see six endpoints. A mount whose app has no routes of its own (the static
    file server) is a leaf and comes back empty.
    """
    children = getattr(route, "routes", None)
    if children:
        return list(children)
    children = getattr(getattr(route, "original_router", None), "routes", None)
    return list(children) if children else []


def iter_routes(app: FastAPI) -> Iterator[BaseRoute]:
    """Every route in the app, with included routers and mounts flattened."""
    def walk(routes: Iterable[BaseRoute]) -> Iterator[BaseRoute]:
        for route in routes:
            children = _child_routes(route)
            if children:
                yield from walk(children)
            else:
                yield route

    return walk(app.routes)


def _route_label(route: BaseRoute) -> str:
    methods = sorted(getattr(route, "methods", None) or ["MOUNT"])
    return f"{','.join(methods)} {getattr(route, 'path', route)}"


def undeclared_routes(app: FastAPI) -> list[str]:
    """Every registered route with no declared level, as "METHOD /path" labels.

    Sorted so the startup log and a failing test read the same way twice.
    """
    return sorted(_route_label(r) for r in iter_routes(app) if route_level(r) is None)


def log_undeclared_routes(app: FastAPI) -> list[str]:
    """Log an ERROR naming every undeclared route. Called at startup."""
    missing = undeclared_routes(app)
    if missing:
        logger.error(
            "%d route(s) have no declared access level and are being DENIED to "
            "every caller: %s. Register them through authz.Guard with an "
            "AuthLevel.",
            len(missing), ", ".join(missing),
        )
    return missing


def routes_by_level(app: FastAPI) -> dict[str, list[str]]:
    """The whole matrix, for a quick look at what is gated how."""
    matrix: dict[str, list[str]] = {}
    for route in iter_routes(app):
        level = route_level(route)
        matrix.setdefault(level.value if level else "UNDECLARED", []).append(_route_label(route))
    return {key: sorted(value) for key, value in sorted(matrix.items())}


# ---------------------------------------------------------------------------
# origins
# ---------------------------------------------------------------------------

def _configured_origin(settings: Settings) -> str | None:
    """This instance's origin as the operator configured it, if they have.

    The configured base URL is authoritative when present, because it does not
    depend on a request header. The provider-login work is what introduces that
    setting; until an instance has one, the check falls back to the request's own
    Host, which is still sound against the attack being defended: a browser sets
    Host to the site it is talking to and Origin to the page that asked, so a
    hostile page posting here sends our Host and its own Origin.
    """
    base = (getattr(settings, "public_base_url", "") or "").strip()
    if not base:
        return None
    parts = urlsplit(base)
    if not (parts.scheme and parts.netloc):
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def acceptable_origins(request: Request, settings: Settings) -> set[str]:
    """Every origin this instance will accept a mutating request from.

    A configured `public_base_url` is authoritative and yields exactly one, which
    is the tightest this check gets and the reason to set it.

    Without one, the HOST comes from the request and the SCHEME cannot be
    established at all: behind a TLS-terminating proxy this app is served plain
    HTTP whether or not the browser is on HTTPS, and `X-Forwarded-Proto` is only
    believed when the peer is a configured trusted proxy — which on a fresh
    install it is not. Insisting on a scheme we cannot observe rejected every
    mutating request on a brand-new proxied deployment, INCLUDING onboarding,
    which made the instance impossible to set up: the setting that would fix it
    lives behind the login that could not be created.

    So an unconfigured instance accepts either scheme for its OWN host. The host
    comparison is what actually refuses a hostile origin; the scheme half adds
    nothing against a cross-site attacker (who controls neither) and everything
    against the operator.
    """
    configured = _configured_origin(settings)
    if configured:
        return {configured}
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return set()
    if auth.request_is_https(request, settings):
        return {f"https://{host}"}
    return {f"http://{host}", f"https://{host}"}


def expected_origin(request: Request, settings: Settings) -> str | None:
    """The single origin this instance considers canonical, or None.

    Kept for callers that want one value to show or log. The actual admission
    decision uses acceptable_origins(), which may be broader — see there.
    """
    configured = _configured_origin(settings)
    if configured:
        return configured
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return None
    scheme = "https" if auth.request_is_https(request, settings) else "http"
    return f"{scheme}://{host}"


def cross_origin_reason(request: Request, settings: Settings) -> str | None:
    """None when the request may proceed, otherwise why it may not."""
    origin = (request.headers.get("origin") or "").strip().lower()
    if origin:
        if origin not in acceptable_origins(request, settings):
            return "This request came from another origin."
        return None
    # No Origin header: fall back to the browser's own account of where the
    # request came from. `same-origin` is our own page; `none` is a user typing
    # the address or using a bookmark. `cross-site` and `same-site` are not
    # good enough — a sibling subdomain counts as same-site.
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site in ("same-origin", "none"):
        return None
    return "This request did not come from this site."


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------

def _wants_html(request: Request) -> bool:
    """Whether this looks like a browser navigation rather than a fetch() call.

    `fetch()` sends `Accept: */*` unless told otherwise; a navigation asks for
    text/html. That is the difference between "redirect them somewhere useful"
    and "return a status the calling script can act on".
    """
    return "text/html" in (request.headers.get("accept") or "").lower()


def _deny(status: int, reason: str, message: str) -> Response:
    return JSONResponse({"ok": False, "reason": reason, "error": message}, status_code=status)


def _match(routes: Iterable[BaseRoute], scope: dict) -> BaseRoute | None:
    for route in routes:
        match, child_scope = route.matches(scope)
        if match != Match.FULL:
            continue
        children = _child_routes(route)
        if not children:
            return route
        # Descend with whatever the container rewrote (a mount strips its own
        # prefix from the path). A container that matched but whose children
        # don't is not something this can judge, so keep looking.
        if (deeper := _match(children, {**scope, **child_scope})) is not None:
            return deeper
    return None


def _matched_route(app: FastAPI, request: Request) -> BaseRoute | None:
    """The route this request would be dispatched to, or None.

    Resolved here rather than in a dependency because a route with no
    declaration has no dependency to run — the whole point is to catch the route
    somebody forgot about.
    """
    return _match(app.routes, request.scope)


def _install_request_shape_guard(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_shape_guard(request: Request, call_next):
        if request.method not in MUTATING_METHODS:
            return await call_next(request)
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return _deny(415, "json_only", "This endpoint accepts application/json only.")
        # Origin admission needs only the configured base URL and the request Host,
        # never a credential, so secrets are left sealed. That keeps this guard from
        # raising on a mutating request while a secret is sealed under a wrong key —
        # the recovery-screen POST has to get through here to fix exactly that.
        settings = load_settings(open_secrets=False)
        if reason := cross_origin_reason(request, settings):
            return _deny(403, "cross_origin", reason)
        return await call_next(request)


def _install_first_run_gate(app: FastAPI) -> None:
    @app.middleware("http")
    async def first_run_gate(request: Request, call_next):
        if await _any_users_exist():
            return await call_next(request)
        path = request.url.path
        if path in FIRST_RUN_PATHS or path.startswith(FIRST_RUN_PREFIXES):
            return await call_next(request)
        if _wants_html(request):
            return RedirectResponse("/onboarding", status_code=303)
        return _deny(401, "setup_required", "This instance has not been set up yet.")


# Reachable while the encryption key is wrong, when everything else is gated to the
# recovery screen: the recovery page and its API, sign-in (so an admin whose session
# lapsed can get back in), the health probe, and static assets. The recovery page
# resolves the viewer itself, so it is reachable signed-out to send them to /login.
KEY_MISMATCH_PATHS = ("/healthz", "/login", "/logout")
KEY_MISMATCH_PREFIXES = ("/static/", "/admin/encryption/", "/api/admin/encryption/")


def _install_key_mismatch_gate(app: FastAPI) -> None:
    @app.middleware("http")
    async def key_mismatch_gate(request: Request, call_next):
        # Ordinary loads decrypt stored secrets, which RAISE while a secret is sealed
        # under a key the current one cannot open — so once that state is derived at
        # startup, every request but the recovery ones is steered here instead of
        # letting each load 500 in turn. A browser is redirected to the screen; a
        # script gets a status it can act on.
        if encryption_flow.health() != encryption_flow.KEY_MISMATCH:
            return await call_next(request)
        path = request.url.path
        if path in KEY_MISMATCH_PATHS or path.startswith(KEY_MISMATCH_PREFIXES):
            return await call_next(request)
        if _wants_html(request):
            from .encryption_routes import RECOVERY_PATH
            return RedirectResponse(RECOVERY_PATH, status_code=303)
        return _deny(
            503, "key_mismatch",
            "The encryption key does not match the stored secrets. An administrator "
            "must resolve this from the recovery screen.",
        )


def _install_deny_by_default(app: FastAPI) -> None:
    @app.middleware("http")
    async def deny_undeclared_routes(request: Request, call_next):
        route = _matched_route(app, request)
        if route is not None and route_level(route) is None:
            logger.error(
                "Refusing %s: the route has no declared access level. Register it "
                "through authz.Guard with an AuthLevel.", _route_label(route),
            )
            return _deny(403, "undeclared_route", "This endpoint is not available.")
        return await call_next(request)


# Once an instance has accounts it can never go back to having none for the life
# of the process, so the check is worth doing exactly once. Keyed by database
# path so tests that point the app at a fresh file re-check rather than
# inheriting the previous case's answer.
_users_exist_for: Path | None = None


async def _any_users_exist() -> bool:
    global _users_exist_for
    path = db.db_path()
    if _users_exist_for == path:
        return True
    if await auth.any_users_exist():
        _users_exist_for = path
        return True
    return False


def _install_auth_error_handler(app: FastAPI) -> None:
    @app.exception_handler(AuthError)
    async def handle_auth_error(request: Request, exc: AuthError) -> Response:
        """Turn a refusal into a redirect for a browser and a status for a script.

        The reason the dependencies carry is what makes this possible without
        re-deriving why the request was refused: not signed in means the sign-in
        page, anything else means the account page, which is where approval state
        is shown.
        """
        if _wants_html(request):
            return RedirectResponse(
                "/login" if exc.reason == "login_required" else "/me", status_code=303,
            )
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
        return JSONResponse({"ok": False, **detail}, status_code=exc.status_code)


def install(app: FastAPI) -> None:
    """Wire the middleware and the refusal handler onto the app.

    Registration order is reversed by Starlette — the last middleware added is
    the outermost — so this reads bottom-up. The intended order a request meets
    them in is:

      1. key-mismatch: while a stored secret is sealed under a key that cannot open
         it, every request but sign-in and the recovery screen is steered to
         recovery. First, so it runs before any middleware that decrypts a secret.
      2. request shape: a mutating request must be JSON and same-origin, so a
         malformed cross-site submission is rejected on its shape alone, before
         anything looks at cookies or the database.
      3. first-run: until an account exists, everything but setup is refused.
      4. deny-by-default: a route with no declaration never reaches its handler.
    """
    _install_deny_by_default(app)
    _install_first_run_gate(app)
    _install_request_shape_guard(app)
    _install_key_mismatch_gate(app)
    _install_auth_error_handler(app)
