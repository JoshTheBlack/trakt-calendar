<p align="center">
  <img src="images/tvbanner.png" width="280" alt="Trakt New Shows banner">
</p>

<p align="center">
  <img src="images/title-banner.svg" width="440" alt="Trakt New Shows">
</p>

<p align="center">
  A self-hosted web app that shows you every new TV show premiering in a given month —
  something Trakt's official site stopped offering after its
  <a href="https://forums.trakt.tv/t/new-trakt-feedback/84794/" target="_blank">V3 redesign</a>.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-e8b545.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/Python-3.11%2B-4fa3e0.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/served%20by-Hypercorn-e0384d.svg" alt="Hypercorn">
</p>

---

## Why this exists

Trakt's V3 redesign removed the ability to simply browse "what new shows are premiering
this month." This app brings that back: pick a month, and see every premiere grouped by
day, with posters, ratings, languages, networks, air dates, and more — then click any
title for cast, an embedded trailer, and the full episode list.

## Features

- 📅 Browse premieres for any month/year, grouped by day
- 📡 **Switchable endpoints** — new shows, season premieres, season finales, all episodes, or movies
- 🖼️ Rich poster tiles — rating, runtime, network, and episode (SxxEyy) badges, plus language, country, day-of-week, and a lazily-loaded current-season summary (episode count, latest / next air date)
- 🔍 **Details modal** on click — full overview, an embedded trailer, cast (headshots + characters), and the season's episode list with air dates
- ✅ Mark shows **watching / not watching** — saved server-side, so it follows you across devices — plus a one-click filter to hide the ones you're not watching
- 📥 **Add to Sonarr / Radarr / Seerr** — one click to send a show to Sonarr, a movie to Radarr, or request either on Seerr (Overseerr/Jellyseerr); buttons show each app's logo, appear only when configured, and auto-disable if the instance is unreachable (background heartbeat). An "Add all" button bulk-adds a whole month.
- 🎛️ **Layout options** — poster-on-top or poster-beside cards, days stacked or packed beside each other, and a poster-only wall with hover-to-expand details
- 📈 Tracks premiere counts over time and shows the change since your last visit
- ⚙️ **Everything configured from the UI** — Trakt credentials, timezone, filters, and layout all live in an in-app Settings panel; no config files to edit

## Quick start (local)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
python run.py            # -> http://localhost:8000
```

Open the site, click **⚙️ Settings**, and paste your Trakt **Client ID** and **Access
Token** (create a free API app at [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications)).
That's it — pick a month and browse.

> `run.py` starts the app under Hypercorn with auto-reload. Override `HOST`, `PORT`, or
> set `RELOAD=0` via environment variables.

## Run with Docker

A container image is built and pushed to GHCR automatically on every push to `main`
(see [`.github/workflows/docker-build.yml`](.github/workflows/docker-build.yml)). Mount a
volume so your settings and watch-state persist:

```bash
docker run -p 8000:8000 -v trakt-data:/data ghcr.io/<owner>/trakt-new-shows:latest
```

You can also seed credentials without touching the UI by setting `TRAKT_CLIENT_ID` and
`TRAKT_ACCESS_TOKEN` environment variables on first run.

## Configuration

All configuration is done in the **⚙️ Settings** panel and saved to
`data/settings.json` (git-ignored). Available options:

| Setting | What it does |
|---|---|
| **Trakt Client ID / Access Token** | Your Trakt API credentials |
| **Timezone** | Air times are converted to this zone (grouped IANA dropdown) |
| **Default endpoint** | Which calendar to show by default |
| **Genres / Countries / Networks** | Filter which premieres appear |
| **Pagination limit** | Max items fetched per request |
| **Detail cache (minutes)** | How long cast/episode/trailer lookups are cached (`0` disables) |
| **Sonarr / Radarr** | Instance URL, API key, quality profile, and root folder for the add-to-library buttons (click "Load profiles & folders" to populate the dropdowns) |
| **Seerr** | Instance URL + API key to enable the request button (works with the Overseerr/Jellyseerr lineage) |

The endpoint, layout, and hide-not-watching controls also live in the header for quick
switching, and every choice persists.

> **Advanced filtering (Trakt VIP):** Trakt gates calendar filtering by genre, country,
> and network behind a [VIP subscription](https://trakt.tv/vip/filtering). If your account
> isn't VIP these filters may be ignored by the API; the unfiltered calendar works on any
> account.

## Requirements

- Python 3.11+ (3.12 recommended)
- A free [Trakt API](https://trakt.tv/oauth/applications) application (Client ID + Access Token)

## Project layout

```
app/
  main.py         FastAPI app + routes
  config.py       Settings model + persistence
  trakt.py        Async Trakt client + response normalizer
  endpoints.py    Calendar endpoint registry
  timezones.py    Curated IANA timezone list
  state.py        Per-month watch-state persistence
  cache.py        TTL disk cache for detail lookups
  templates/      Jinja2 templates
  static/         CSS, JS, images
run.py            Dev runner (Hypercorn)
```

## License

Released under the [MIT License](LICENSE).
