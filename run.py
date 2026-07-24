"""Dev runner — start the app under Hypercorn without Docker.

Usage:
    python run.py               # http://localhost:8000 with auto-reload
Environment:
    HOST (default 0.0.0.0), PORT (default 8000), RELOAD (default 1)
    LOG_LEVEL (default INFO) — set to DEBUG to see every outbound Trakt API
        call (the "app.perf" logger's netGET/netPOST/cacheHIT lines from
        app/trakt.py, app/calendar_cache.py, and app/trakt_auth.py), plus
        per-request timing spans elsewhere in the app. Also honored by the
        Docker image (app/main.py applies the same setting either way).
"""
import logging
import os

# Loads a gitignored .env file in the project root, if present, before anything
# reads os.environ — in particular ENCRYPTION_KEY, which app/secrets_box.py
# caches from the environment on first use. Docker never runs this file (its
# CMD invokes hypercorn directly), so a real environment variable is still the
# only way in there; this is purely a non-Docker dev convenience. A variable
# already set in the actual environment wins over the .env file either way.
from dotenv import load_dotenv
load_dotenv()

import hypercorn.asyncio
from hypercorn.config import Config

# Quiet third-party libs (WARNING) but surface our own app.* diagnostics at
# LOG_LEVEL (default INFO; set DEBUG for per-Trakt-call tracing — see the
# module docstring). Fires before `from app.main import app` below, which
# reruns the identical setup (see app/main.py) — a no-op here since
# basicConfig() only attaches a handler once, so this call is the one that
# actually wins for the dev runner.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
# Quiet hypercorn's per-request access log (the "GET /api/... 200" lines) — set to
# DEBUG to bring them back. App diagnostics above are unaffected by this one.
logging.getLogger("hypercorn.access").setLevel(logging.WARNING)

from app.main import app


def main() -> None:
    config = Config()
    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "8000")
    config.bind = [f"{host}:{port}"]
    config.use_reloader = os.environ.get("RELOAD", "1") == "1"
    # Per-request access log ("GET /... 200" lines, incl. static 304s) is OFF by
    # default — set ACCESS_LOG=1 to enable. App INFO diagnostics are unaffected.
    config.accesslog = "-" if os.environ.get("ACCESS_LOG") else None
    print(f">> Trakt New Shows running at http://localhost:{port}  (Ctrl+C to stop)")
    import asyncio
    asyncio.run(hypercorn.asyncio.serve(app, config))


if __name__ == "__main__":
    main()
