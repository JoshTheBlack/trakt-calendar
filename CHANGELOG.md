# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## 🏷️ [1.0.0] - 2026-07-20

First release — a self-hosted Python app for browsing new TV/movie premieres by month, powered by the Trakt API.

### Core
- 🐍 Python (FastAPI) app served by Hypercorn (`app/`, `run.py`); runs from the terminal or the provided Docker image.
- ⚙️ All configuration — including Trakt API credentials — is set from an in-app **Settings** panel, saved to `data/settings.json` (no config files to edit).
- 📡 Switchable Trakt calendar endpoints: new shows, season premieres, season finales, all episodes, and movie premieres.
- 🗓️ Month/year picker is the landing page — opening the app (no month in the URL) shows a selector, then takes you to that month's calendar. Also reachable via the month title in the header.
- 🧭 Timezone picker: a grouped dropdown of canonical IANA zones with current (DST-aware) UTC offsets.

### Browsing & details
- 🖼️ Rich poster tiles — rating, runtime, network, and episode (SxxEyy) badges, plus language, country, day-of-week, and a lazily-loaded current-season summary (episode count, latest / next air date).
- 🔍 Details modal on click — full overview, an embedded trailer, cast (headshots + characters), and the season's episode list with air dates. Per-show lookups are cached on disk with a configurable TTL (`app/cache.py`).
- ✅ Mark shows **watching / not watching** (persisted server-side, shared across devices) with a one-click filter to hide not-watching items and premiere-count history/deltas.

### Layout
- 🎛️ Persisted, header-switchable **Card style** — *poster on top*, *poster beside*, or *poster only* — and **Day packing** (stacked bands / days packed beside each other). *Poster beside* and *poster only* (on hover) render an identical fixed-size card whose height is locked to the poster, so long descriptions scroll rather than stretch it. In *poster only*, hovering rebuilds that card as an attached panel and pushes neighbors aside (flipping left near the screen edge); clicking opens the details modal.

### Sonarr / Radarr / Seerr
- 📥 One-click **add to Sonarr** (show, by TVDB), **Radarr** (movie, by TMDB), or **request on Seerr** (by TMDB), with each app's official logo, on every card and in the details modal.
- 📚 An **"Add all"** header button adds every watching item on the current month to the endpoint's service (each endpoint is TV-only or movie-only), with a toast per title.
- 💚 Items **already in a service's library** are detected (each library fetched once and cached) and shown with a green ✓ "already in…" state.
- ❤️ Buttons render only when a service is configured, and a background heartbeat (every 60s + on save) disables them when the instance is unreachable. Sonarr/Radarr settings include quality profile + root folder (loadable before saving); Seerr needs only URL + API key.

### Ops
- 🐳 Dockerfile + GitHub Actions workflow to build and push an image to GHCR on push.
- 🔄 Static assets are cache-busted per deploy so style/script changes appear without a hard refresh.
