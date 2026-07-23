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

The first visit redirects to **/onboarding**, where you create the administrator account.
Two things happen there automatically, so plain-HTTP development and HTTPS deployments
both work with no configuration:

- The **session cookie policy** is resolved from the browser that submitted the form —
  plain HTTP gets a cookie that works, HTTPS gets a `Secure` one. See
  [Serving over HTTPS](#serving-over-https) if you need to override it.
- The account **adopts whatever is already in `data/settings.json`**, including an
  existing Trakt token, so an instance upgraded from a pre-accounts version keeps its
  data and its calendar looks exactly as it did.

Then click **⚙️ Settings** and paste your Trakt **Client ID** and **Access Token** (create
a free API app at [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications)).
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

## Serving over HTTPS

Three settings decide whether the app behaves correctly behind a reverse proxy. All three
have safe production defaults, so a normal HTTPS deployment needs only `public_base_url`.

### `cookie_secure` — set for you at onboarding

Whether the session cookie carries the `Secure` flag and the `__Host-` name prefix.
**Onboarding resolves this from the browser that created the admin account**, so you
normally never touch it.

It is decided there rather than at startup because startup genuinely cannot tell: behind a
TLS-terminating proxy this app is served over plain HTTP whether or not the browser is on
HTTPS. The onboarding request can tell, because the browser's `Origin` header carries the
scheme *it* used — and unlike `X-Forwarded-Proto`, that does not depend on
`trusted_proxy_ips` already being correct.

To override, edit `data/settings.json` and restart:

| Value | Use it when |
|---|---|
| `always` | Anything reached over HTTPS — Traefik, nginx, Caddy, Cloudflare, a tunnel. |
| `never` | You genuinely serve over plain HTTP: local development, or a LAN-only instance with no TLS. |
| `auto` | You want live scheme detection. Requires `trusted_proxy_ips` to be correct, or it degrades to `never`. |

It stays file-only, not a Settings field, because a wrong value logs you out of the screen
that would fix it.

> **If this is ever wrong**, it fails in one specific way: sign-in returns success and the
> very next request is anonymous, so the browser loops back to `/login`. Both `/login` and
> the Settings panel detect the mismatch and say so, rather than leaving it looking like a
> wrong password — but that is the symptom to recognize.

### `public_base_url` — required for provider login and share links

The origin people reach this instance on, e.g. `https://shows.example.com` — origin only,
no path and no trailing slash. **Every** absolute URL the app generates is built from it and
never from the `Host` header, which makes the app immune to Host-header injection and
removes any dependency on proxy configuration for URL correctness. Trakt also compares the
`redirect_uri` byte for byte against what you registered, so a header-derived value would
break sign-in. Required before "Log in with Trakt" and public share links can work.

Setting it also tightens the same-origin check on mutating requests to exactly one origin.
Until it is set, an instance accepts either scheme for its own `Host` — the host match is
what refuses a hostile origin, and the scheme cannot be observed through a TLS-terminating
proxy that hasn't been declared in `trusted_proxy_ips` yet. Set it once you're deployed.

### `trusted_proxy_ips` — required for correct rate limiting

Comma-separated CIDRs whose `X-Forwarded-For` this app believes. Default `127.0.0.1/32`,
which is **wrong behind Docker**: Traefik connects from a container address, so the header
is ignored and every user collapses onto the proxy's IP. Per-IP login rate limiting then
applies to the whole instance at once, and the admin session list shows one address for
everybody.

Set it to your proxy's address or subnet. The Settings screen shows the address the current
request arrived from, and warns when forwarded headers are arriving but being ignored — so
read the value off the screen rather than guessing your Docker subnet.

It is configured in two places deliberately. Hypercorn reads `TRUSTED_PROXY_IPS` at process
start for its own `--forwarded-allow-ips` and cannot be reconfigured from the running app;
the app's own copy is the `trusted_proxy_ips` setting, seeded from that env var on first run
and editable in Settings thereafter. Set the env var and the setting follows.

```bash
docker run -p 8000:8000 -v trakt-data:/data \
  -e TRUSTED_PROXY_IPS=172.18.0.0/16 \
  ghcr.io/<owner>/trakt-new-shows:latest
```

<details>
<summary>Traefik labels</summary>

```yaml
services:
  trakt-new-shows:
    image: ghcr.io/<owner>/trakt-new-shows:latest
    volumes:
      - trakt-data:/data
    environment:
      # The Docker network Traefik reaches this container on.
      TRUSTED_PROXY_IPS: 172.18.0.0/16
    labels:
      - traefik.enable=true
      - traefik.http.routers.shows.rule=Host(`shows.example.com`)
      - traefik.http.routers.shows.entrypoints=websecure
      - traefik.http.routers.shows.tls.certresolver=letsencrypt
      - traefik.http.services.shows.loadbalancer.server.port=8000
```

Then set `public_base_url` to `https://shows.example.com` in Settings and leave
`cookie_secure` at `always`.
</details>

## Configuration

Configuration is done in the **⚙️ Settings** panel. Almost everything lives in the SQLite
database (`data/app.db`) — credentials in an encryptable secrets store, everything else in
a plain settings store — so `data/settings.json` (git-ignored) is no longer the source of
truth for most of it. See [At-rest encryption](#at-rest-encryption-of-stored-secrets) below
for why it's split that way and what's still in the file.

| Setting | What it does |
|---|---|
| **Trakt Client ID / Access Token** | Your Trakt API credentials |
| **Timezone** | Air times are converted to this zone (grouped IANA dropdown) |
| **Default endpoint** | Which calendar to show by default |
| **Pagination limit** | Max items fetched per request |
| **Detail cache (minutes)** | How long cast/episode/trailer lookups are cached (`0` disables) |
| **Sonarr / Radarr** | Instance URL, API key, quality profile, and root folder for the add-to-library buttons (click "Load profiles & folders" to populate the dropdowns) |
| **Seerr** | Instance URL + API key to enable the request button (works with the Overseerr/Jellyseerr lineage) |
| **Public base URL** | The origin this instance is reached on — see [Serving over HTTPS](#serving-over-https) |
| **Trusted proxy addresses** | Whose `X-Forwarded-For` to believe — see [Serving over HTTPS](#serving-over-https) |
| **Genres / Countries / Networks** (Calendar section) | The instance's **content floor** — see below. Not the same thing as your own 🔎 Filters. |

Two settings are deliberately file-only, because a wrong value in the UI could lock the
operator out of the UI that would fix it. Edit `data/settings.json` and restart:
`cookie_secure` (set for you at onboarding — see above) and `allow_open_registration`
(default `false`; when `true`, anyone can register without an invite). Nothing else lives
in that file — see [At-rest encryption](#at-rest-encryption-of-stored-secrets).

The endpoint, layout, and hide-not-watching controls also live in the header for quick
switching, and every choice persists. Marking something "not watching" is a decision about
the **show**, not about the card you clicked: it applies on every calendar view, in every
month, and on your public share page, until you turn it back on.

**🔎 Filters** in the header narrows your calendar by genre, country, and network. The
button reads **🔎 Filtered** and stays lit while anything is being held back. Filters
belong to your account, not to the instance — two people read the same cached month and
each see it filtered their own way. Genres and countries are comma-separated, and a
leading `-` excludes instead of including (`drama, -reality`); networks are matched exactly
as Trakt spells them. **New accounts start with nothing filtered.**

> Trakt gates its own calendar filtering behind a [VIP subscription](https://trakt.tv/vip/filtering),
> but this app filters the cached response itself rather than asking Trakt to, so the
> Filters panel works on any account.

### The content floor — Settings > Calendar > Genres / Countries / Networks

These three fields look like the per-account 🔎 Filters above, but they are not: they are
instance-wide, and they filter **before** the shared calendar cache is ever populated, not
at read time. A show excluded here never enters the cache at all — no per-account filter
can bring it back, because there's nothing left to filter.

**This removes content for every user of the instance**, on top of whatever each person
sets in their own Filters. Legitimate uses: trimming genres/countries nobody on the
instance will ever watch (a smaller, cheaper cache), or keeping certain content off a
shared/family instance. Leave them blank — the default — to filter nothing at this layer.
The Settings screen repeats this warning next to the fields; read it before setting them on
an instance other people use.

## At-rest encryption of stored secrets

By default, credentials (Trakt tokens, Sonarr/Radarr/Seerr/TMDB API keys, and every linked
account's Trakt token) are stored in the database as plaintext — the same trust boundary
this app has always had. Turning on encryption seals them with a key that lives **only** in
your environment, never in the database file, so a leaked `app.db` (a backup, a volume
snapshot, a stray file read) does not hand over live credentials.

**What it defends against:** the database file leaving the host. **What it does not defend
against:** full host compromise — if an attacker can read the process environment, they get
the key too. That's inherent to any single-host design that doesn't rely on an external
KMS/Vault, which would be overkill here.

### Enabling it

From **⚙️ Settings**, the encryption panel walks through two stages:

1. **Get a key.** Generate one in the UI, or bring your own with:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   **The key is shown once.** Copy it into your environment as `ENCRYPTION_KEY` — an `.env`
   file, `docker run -e ENCRYPTION_KEY=...`, or a compose `environment:` block — and restart
   the app. Nothing is encrypted yet at this point.
2. **Verify and encrypt.** After the restart, come back to Settings and confirm the key
   survived the reboot. Once verified, encrypting is one click: every stored secret and
   every linked account's Trakt token is sealed in place.

You can opt out at either step, and turn encryption on later from the same panel — opting
out doesn't lose anything, it just leaves things as plaintext until you change your mind.

> **⚠️ Losing `ENCRYPTION_KEY` after enabling makes every sealed secret and linked token
> unrecoverable.** The app fails open, not silently broken: it treats sealed values it can't
> read as unset rather than crashing, so it stays usable, but the Trakt/Sonarr/Radarr/Seerr
> credentials have to be re-entered and every user has to re-link Trakt. **Do not re-save
> credentials or re-link while the key is missing** — that overwrites the still-recoverable
> ciphertext with a fresh plaintext value sealed under nothing. If a *different* key ends up
> in the environment (not merely missing), the app detects the mismatch at startup and
> routes an administrator to a dedicated recovery screen instead of failing request by
> request, with two options: restore the original key (nothing is touched), or run a typed,
> confirmed reset that blanks only the unrecoverable values, keeps linked accounts so they
> can simply re-link, and reseals under the new key. **Back up `ENCRYPTION_KEY` somewhere
> durable outside the app the moment you save it** — a password manager or secrets vault,
> not a note next to the database backup.

Non-interactive environments (CI, scripted deployments) can skip the UI entirely by setting
both `ENCRYPTION_KEY` and `ENCRYPT_SECRETS=1`; the app seals everything in place at startup,
the same conversion the Settings button runs.

## Requirements

- Python 3.11+ (3.12 recommended)
- A free [Trakt API](https://trakt.tv/oauth/applications) application (Client ID + Access Token)

## Project layout

```
app/
  main.py           FastAPI app + calendar/tracker routes
  config.py         Settings model + persistence (secrets/globals in data/app.db, two
                     recovery fields in data/settings.json)
  db.py             SQLite connection policy + schema migrations (data/app.db)
  secrets_box.py    At-rest encryption primitive (seal/open, key from ENCRYPTION_KEY)
  secrets_backfill.py  Seal-in-place conversion shared by the enable flow and Settings
  encryption_flow.py   Enable/verify/encrypt lifecycle, key-health canary, lost-key recovery
  encryption_routes.py Admin encryption endpoints + the recovery screen
  auth.py           Passwords, sessions, identities, invites, access levels
  authz.py          Route authorization: declare-or-denied, CSRF/origin rules
  auth_routes.py    Onboarding, register, sign in/out, account page
  admin_routes.py   Admin screen: accounts, invites, retired identifiers
  trakt_routes.py   "Log in with Trakt" (OAuth redirect flow)
  plex_routes.py    "Log in with Plex" (PIN flow)
  share_routes.py   Public read-only calendars (/s/, /u/, /c/)
  share_links.py    Share-link settings + URL building
  calendar_cache.py Global UTC window cache + the read path over it
  calendar_state.py Per-user not-watching marks (show-level, all views) + change detection
  trakt.py          Async Trakt client + response normalizer
  endpoints.py      Calendar endpoint registry
  timezones.py      Curated IANA timezone list
  cache.py          TTL blob cache for detail lookups (shares api_cache)
  templates/        Jinja2 templates
  static/           CSS, JS, images
run.py              Dev runner (Hypercorn)
```

## License

Released under the [MIT License](LICENSE).
