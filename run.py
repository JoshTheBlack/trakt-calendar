"""Dev runner — start the app under Hypercorn without Docker.

Usage:
    python run.py               # http://localhost:8000 with auto-reload
Environment:
    HOST (default 0.0.0.0), PORT (default 8000), RELOAD (default 1)
"""
import os

import hypercorn.asyncio
from hypercorn.config import Config

from app.main import app


def main() -> None:
    config = Config()
    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "8000")
    config.bind = [f"{host}:{port}"]
    config.use_reloader = os.environ.get("RELOAD", "1") == "1"
    config.accesslog = "-"
    print(f">> Trakt New Shows running at http://localhost:{port}  (Ctrl+C to stop)")
    import asyncio
    asyncio.run(hypercorn.asyncio.serve(app, config))


if __name__ == "__main__":
    main()
