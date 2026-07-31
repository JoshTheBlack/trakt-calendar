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
# The APP reads this env var, not the server: config.py seeds the admin-editable
# `trusted_proxy_ips` setting from it on first run, and app/auth.py does all the
# X-Forwarded-For parsing itself off the raw connection peer. Hypercorn is left
# out of it deliberately — it only rewrites the client via an opt-in ProxyFix
# middleware this app does not use, so it always hands the app the true peer.
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

# Not documentation in the image — the app SERVES this. app/changelog.py reads it
# from the project root (BASE_DIR.parent, which is /app here) and renders it into
# the "What's new" modal, so there is only ever one copy of the release notes.
# Dropping this line does not fail the build or the healthcheck; the modal just
# comes up empty.
COPY CHANGELOG.md .

# Runtime data (settings.json, state_*.json) lives on a volume so it persists.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
# READS THE BODY BEFORE EXITING, and that is not tidiness. Checking .status and
# exiting leaves the response unread and the socket torn down by process exit,
# often before the server has finished writing its eleven bytes — and Hypercorn
# logs a stream that closes early as a SECOND access line with "- -" where the
# status should be. So every health check billed two lines, one of them looking
# like a failure, and the "client hung up early" signal that line actually
# carries became worthless for spotting the requests where it means something.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz'); r.read(); r.close(); sys.exit(0 if r.status==200 else 1)"

# Exec form: no shell, so hypercorn is PID 1 and receives the container's stop
# signal directly. No --forwarded-allow-ips — Hypercorn has no such option (it
# was an unrecognized-argument crash on start), and the app reads TRUSTED_PROXY_IPS
# itself (see the ENV note above), so the server needs no proxy configuration.
#
# NO --access-logfile, AND THAT FLAG IS THE ONLY THING THAT DECIDES IT. Passing it
# made Hypercorn log a line per static file, per poll and per health check, which
# is what the app.perf slow-request warnings then had to be found inside — while
# the dev runner logged none of it, because run.py leaves config.accesslog unset
# unless ACCESS_LOG=1. Setting the "hypercorn.access" level from Python does NOT
# work: Hypercorn builds that logger after importing the app and overwrites the
# level from its own config (see the note in app/main.py). Unset here means the
# access logger is never created at all.
# Nothing diagnostic is lost — app/perftrace.py times every request, names the
# slow ones with their phases broken down, and logs them all at DEBUG. To get the
# raw log back for an investigation, append --access-logfile - to this command.
CMD ["hypercorn", "app.main:app", "--bind", "0.0.0.0:8000"]
