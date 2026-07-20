# Trakt New Shows — Python Rebuild: Planning Document

## Goal
Port the existing single-file PHP app (`trakt_new_shows_fixed.php`) to a Python ASGI
application served by **Hypercorn**, keeping the same dark gold/crimson aesthetic and
"posters grouped by day" design language, while adding front-end configuration,
selectable Trakt endpoints, watch-status filtering, richer tiles, and a details modal.

## Requirements → Approach

| # | Requirement | Approach |
|---|-------------|----------|
| A | Rebuild in Python w/ Hypercorn | **FastAPI** (ASGI) + **Jinja2** server-rendered templates + **httpx** async Trakt client, run under **Hypercorn**. FastAPI chosen over Flask/Quart for typed request handling + built-in JSON API, while keeping server-side HTML rendering to preserve the existing look. |
| B | Docker image built by GitHub Actions on push | `Dockerfile` (python:3.12-slim, hypercorn entrypoint) + `.github/workflows/docker-build.yml` building & pushing to GHCR on push to `main`. |
| C | All config settable from the front end (incl. Trakt API values) | Config persisted to `data/settings.json` (git-ignored). A **⚙️ Settings modal** (GET/POST `/api/settings`) edits Trakt Client ID / Access Token, timezone, genres, countries, network filter, endpoint, pagination. No code edits or `config.php` needed. |
| D | Switch between Trakt endpoints | `app/endpoints.py` defines a registry of calendar endpoints (new shows, season premieres, finales, all shows airing, movies). An endpoint dropdown in the header drives the `?endpoint=` param; the Trakt client + response normalizer adapt per type. |
| E | Filter out / show "not watching" items | Existing per-item watch toggle retained (server-side state). A header toggle **"Hide not-watching / Show all"** filters the grid client-side; default is a saved setting. |
| F | Richer tile details | Add language, day-of-week, runtime, episode label (SxxEyy), air date. *(Last-episode-of-season date needs an extra season call — wired in phase 2.)* |
| G | Click tile → details modal (cast, episode list) | Phase 2: `/api/show/{id}` endpoint calling Trakt `people` + `seasons/{n}?extended=full,episodes`; rendered into a modal. Scaffolding added now, data wired later. |

## Architecture

```
app/
  main.py         FastAPI app, routes, page rendering
  config.py       Settings model + load/save (data/settings.json)
  trakt.py        Async Trakt API client (httpx) + response normalizer
  endpoints.py    Calendar endpoint registry (the "switchable" endpoints)
  state.py        Per-month/endpoint watch-state persistence (data/state_*.json)
  templates/
    index.html    Full page (hero, grid, settings modal, details modal shell)
  static/
    css/style.css Ported design tokens + layout
    js/app.js     State sync, watch toggle, hide filter, settings modal, endpoint switch
    images/       Logos/icons copied from ./images
data/             git-ignored runtime data (settings.json, state_*.json)
run.py            Dev runner: `python run.py` → hypercorn with reload
requirements.txt  Pinned deps
Dockerfile / .dockerignore / .github/workflows/docker-build.yml
```

### Data flow
1. Browser hits `GET /?year=&month=&endpoint=`.
2. `main` loads settings, builds a Trakt request via `endpoints.py`, fetches with `trakt.py`.
3. Response is normalized to a uniform `item` shape and grouped by local day.
4. Jinja renders the grid; `app.js` loads/saves watch-state via `/api/state` and applies filters.

### Endpoints exposed
- `GET /` — main page
- `GET /api/state`, `POST /api/state` — watch-state per (endpoint, year, month)
- `GET /api/settings`, `POST /api/settings` — front-end configuration
- `GET /api/show/{trakt_id}` — details modal payload *(phase 2)*
- `GET /healthz` — container health probe

## Local development (no Docker)
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py            # http://localhost:8000  (hypercorn --reload)
```

## Phasing
- **Phase 1 ✅ done: A–E** + the low-cost parts of F (language, day, runtime, episode label).
- **Phase 2 ✅ done: F (season last-episode date) + G (details modal)** — per-show Trakt
  calls (`/api/tile`, `/api/details`) backed by a TTL disk cache (`app/cache.py`).
  Tiles lazily fetch their current-season summary (episode count, latest/next air date)
  via `IntersectionObserver`; clicking a tile opens a modal with full overview, cast
  (headshots + characters), and the season's episode list with air dates.

## Notes / decisions
- Secrets stay in `data/settings.json` (git-ignored), not committed. Kept the old
  `config.php` path unused; the front-end settings supersede it.
- State JSON schema is preserved (`notWatching`, `history`, `lastCount`, `lastShowIds`)
  so behavior matches the PHP version, but keyed per endpoint as well as per month.
- Timezone handling mirrors the PHP fix: filter/group by **local** air date to avoid
  month-boundary bleed.
