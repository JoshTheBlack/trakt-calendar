FROM python:3.12-slim

# Build metadata (passed by CI); surfaced in the app footer.
ARG APP_BUILD=dev
ARG APP_COMMIT=

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRAKT_DATA_DIR=/data \
    APP_BUILD=$APP_BUILD \
    APP_COMMIT=$APP_COMMIT

# Which peers' X-Forwarded-For is believed. Override this with the reverse
# proxy's address or subnet (e.g. -e TRUSTED_PROXY_IPS=172.18.0.0/16): behind
# Traefik the default is WRONG, and the failure is silent — forwarded headers get
# ignored, so every user collapses onto the proxy's address and per-IP login rate
# limiting becomes instance-wide.
#
# Used in two places on purpose. Hypercorn reads it at process start (see CMD)
# and cannot be reconfigured from the running app; the app's own copy is the
# `trusted_proxy_ips` setting, which this seeds on first run and the admin
# Settings screen edits thereafter.
ENV TRUSTED_PROXY_IPS=127.0.0.1/32

WORKDIR /app

# Native runtime libs: libcairo2 for cairosvg (SVG-only network logos) and
# libjpeg/zlib for Pillow. Cleaned up in the same layer to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcairo2 libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runtime data (settings.json, state_*.json) lives on a volume so it persists.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# Shell form so $TRUSTED_PROXY_IPS expands — hypercorn needs the value at start,
# and exec form would pass the literal string. `exec` keeps hypercorn as PID 1 so
# it still receives the container's stop signal directly.
CMD exec hypercorn app.main:app --bind 0.0.0.0:8000 --access-logfile - \
    --forwarded-allow-ips "$TRUSTED_PROXY_IPS"
