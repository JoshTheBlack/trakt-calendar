FROM python:3.12-slim

# Build metadata (passed by CI); surfaced in the app footer.
ARG APP_BUILD=dev
ARG APP_COMMIT=

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRAKT_DATA_DIR=/data \
    APP_BUILD=$APP_BUILD \
    APP_COMMIT=$APP_COMMIT

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

CMD ["hypercorn", "app.main:app", "--bind", "0.0.0.0:8000", "--access-logfile", "-"]
