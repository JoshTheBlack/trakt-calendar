<p align="center">
  <img src="images/distrakkl-mark.svg" width="150" alt="Distrakkl">
</p>

<p align="center">
  <img src="images/distrakkl-wordmark.svg" width="440" alt="Distrakkl">
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

## Features

- 📅 Browse premieres for any month, grouped by day
- 📡 **Switchable endpoints** — new shows, season premieres, season finales, all episodes, or movies
- 🔀 **Several sources, one calendar** — Trakt, optionally [Simkl](#simkl--an-optional-second-source), and [TMDB](#tmdb--the-first-describer) for series, season and movie premieres. A title several list is one card; which services show is a tick-box in **🎚️ Filters**, and whose description wins is an order on your **Account** page. Simkl needs no credentials to fill a calendar
- 🔍 **Find a title and jump to it** — search the calendar you're on as you type, or press Enter to also search TMDB (and any service you've linked) for titles your calendar hasn't loaded yet; opening one goes to where it airs
- 🖼️ Rich poster tiles — rating, runtime, network and episode badges, plus language, country and a current-season summary
- 🔍 **Details modal** — overview, every video both services list in one player, and each season's description, cast and episodes with your watched ones marked, plus a link out to each service
- ✅ Mark shows **watching / not watching** — saved to your account, with a one-click filter to hide the ones you're not watching
- 📥 **Add to Sonarr / Radarr / Seerr** — one click per title. Buttons appear only when configured, arrive already marked when the title is in that library, and grey out with a reason when they can't be used. "Add all" adds everything you're watching that month, after confirming
- 🎛️ **Layout options** — poster-on-top or poster-beside cards, stacked or packed days, and a poster-only wall
- 🔗 **Read-only share links** — a shared calendar opens each title's details and shows your watched marks (you can switch that off). Pasted into Discord, Slack or iMessage, a link unfurls into a picture of the month, drawn only from what's already cached so a stranger's link never spends your Trakt budget
- 🏆 **Rankings boards** — tier-rank shows, films or single seasons by dragging, up to 2,000 titles a board. Add by search, from what you've finished, or from your Trakt ratings; move several at once, restore removals for 30 days, and export an image, Markdown or a backup
- 📈 Tracks premiere counts over time and shows the change since your last visit
- ⚙️ **Everything configured from the UI** — no config files to edit

## Quick start (local)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
python run.py            # -> http://localhost:8000
```

The first visit redirects to **/onboarding**, where you create the administrator account.
The session cookie is set up from that browser, so plain HTTP and HTTPS both work with no
configuration, and an existing `data/settings.json` (Trakt token included) is adopted, so an
upgraded instance keeps its data.

Then click **⚙️ Settings** and paste your Trakt **Client ID** and **Access Token** (create
a free API app at [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications)).
That's it — pick a month and browse. Simkl is optional; see
[Simkl — an optional second source](#simkl--an-optional-second-source).

> `run.py` starts the app under Hypercorn with auto-reload. Override `HOST`, `PORT`, or
> set `RELOAD=0` via environment variables.

## Run with Docker

A container image is built and pushed to GHCR on every push to `main`
(see [`.github/workflows/docker-build.yml`](.github/workflows/docker-build.yml)). Mount a
volume so your settings and watch-state persist:

```bash
docker run -p 8000:8000 -v trakt-data:/data ghcr.io/<owner>/trakt-new-shows:latest
```

You can also seed credentials by setting `TRAKT_CLIENT_ID` and `TRAKT_ACCESS_TOKEN` on
first run.

### Keeping a log file

The app logs to the console only. Set `LOG_FILE=1` to also write `logs/app.log` in the data
directory (`/data/logs/app.log` in the container), or any other value for a path of your own.
It rolls over at 10 MB and keeps five old files, so it never passes about 60 MB. `LOG_LEVEL`
(default `INFO`) sets how much is written to both.

## Serving over HTTPS

Three settings decide whether the app behaves correctly behind a reverse proxy. All three
have safe production defaults, so a normal HTTPS deployment needs only `public_base_url`.

### `cookie_secure` — set for you at onboarding

Whether the session cookie carries the `Secure` flag and the `__Host-` name prefix.
**Onboarding sets it from the browser that created the admin account**, so you normally
never touch it. To override, edit `data/settings.json` and restart:

| Value | Use it when |
|---|---|
| `always` | Anything reached over HTTPS — Traefik, nginx, Caddy, Cloudflare, a tunnel. |
| `never` | You genuinely serve over plain HTTP: local development, or a LAN-only instance with no TLS. |
| `auto` | You want live scheme detection. Requires `trusted_proxy_ips` to be correct, or it degrades to `never`. |

It is file-only because a wrong value would log you out of the screen that fixes it.

> **If it is ever wrong**, sign-in succeeds and the very next request is anonymous, looping
> back to `/login`. Both `/login` and Settings detect the mismatch and say so.

### `public_base_url` — required for provider login, share links and self-healing posters

The origin people reach this instance on, e.g. `https://shows.example.com` — no path, no
trailing slash. Every absolute URL the app generates is built from it, never from the `Host`
header, and Trakt and Simkl compare the redirect URI byte for byte. Required for "Log in with
Trakt" and public share links; setting it also narrows the same-origin check on mutating
requests to that one origin.

It is also how a poster that won't load comes back to this instance: the image service the
pages draw pictures through sends the browser to `<public base URL>/api/posters/missed`,
which answers with the next service's poster and checks whether the first is really gone —
and if it is, has every service describe the title again, so its current poster is picked up.
Without a public base URL a failing poster still falls back to the next one, but nothing is
checked or looked up again.

**Locked out** (unset, or wrong)? Set the `PUBLIC_BASE_URL` environment variable and restart.
It overrides the saved value — the startup log and the Settings screen both say so while it
does — so save the right value in Settings, then remove the variable and restart. An unusable
value is logged and ignored.

```bash
docker run -p 8000:8000 -v trakt-data:/data \
  -e PUBLIC_BASE_URL=https://shows.example.com \
  ghcr.io/<owner>/trakt-new-shows:latest
```

### `trusted_proxy_ips` — required for correct rate limiting

Comma-separated CIDRs whose `X-Forwarded-For` this app believes. The default `127.0.0.1/32`
is **wrong behind Docker**: every user collapses onto the proxy's IP, so login rate limiting
applies to the whole instance at once. Set it to your proxy's address or subnet — the
Settings screen shows the address each request arrives from and warns when forwarded headers
are being ignored. Use the `TRUSTED_PROXY_IPS` environment variable: Hypercorn reads it at
start, and it seeds the Settings value on first run.

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

### `/api/posters/missed` — leave it reachable without signing in

That address is public on purpose: it is reached by the browser following the image
service's redirect, and Firefox sends no session cookie on that hop. It acts only on a
signed, short-lived token the page was drawn with, and refuses any request that isn't an
image loading. If a single-sign-on proxy sits in front of the app, let this one path through,
or failing posters will draw an empty frame instead of the next service's.

### Content-Security-Policy — the app sends its own

Every response carries a strict `Content-Security-Policy` (only the app's own scripts and
styles, pictures only through its image service, YouTube's no-cookie player as the one
embed). **Don't add another one at your proxy**: a browser enforces every policy it
receives, so a second one can block parts of the app. After deploying, check that a page
arrives with exactly one `Content-Security-Policy` header.

### `TRAKT_FAKE_TODAY` — a development-only clock

Set it to an ISO date (`2026-09-01`) and the app believes that is today, so the ███████'s
month rollover can be watched happening. **Never set it on a real instance** — it would
freeze or reopen months against your real data. It is read from the environment only, and
an instance running with it logs a warning naming the date on every boot.

## Configuration

Configuration is done in the **⚙️ Settings** panel and stored in `data/app.db` — credentials
in an encryptable secrets store (see [At-rest encryption](#at-rest-encryption-of-stored-secrets)).

| Setting | What it does |
|---|---|
| **Trakt Client ID / Access Token** | Your Trakt API credentials |
| **Simkl Client ID / Secret** | Optional — see [Simkl](#simkl--an-optional-second-source) for what each feature needs. Each person links their own account |
| **Include Simkl results on the calendar** | Instance-wide, on by default. Untick it and Simkl leaves the calendar at once, cached months included |
| **TMDB API Read Access Token** | Optional — see [TMDB](#tmdb--the-first-describer). The long token, not the short API key |
| **TMDB language / region** | The language TMDB describes titles in (default `en-US`), and the country whose film releases count (default `US`) |
| **Timezone** | Air times are converted to this zone |
| **Default endpoint** | Which calendar to show by default |
| **Pagination limit** | Max items fetched per request |
| **Detail cache (minutes)** | How long cast/episode/video lookups are cached (`0` disables) |
| **Sonarr / Radarr** | Instance URL, API key, quality profile and root folder ("Load profiles & folders" fills the dropdowns) |
| **Seerr** | Instance URL + API key for the request button (Overseerr/Jellyseerr) |
| **Public base URL** | The origin this instance is reached on — see [Serving over HTTPS](#serving-over-https) |
| **Trusted proxy addresses** | Whose `X-Forwarded-For` to believe — see [Serving over HTTPS](#serving-over-https) |
| **Genres / Countries / Certifications / Networks** (Calendar section) | The instance's **content floor** — see below. Not the same thing as your own 🎚️ Filters. |

Two settings are file-only, because a wrong value in the UI could lock the operator out of
the UI that would fix it. Edit `data/settings.json` and restart: `cookie_secure` (see above)
and `allow_open_registration` (default `false`; when `true`, anyone can register without an
invite).

The endpoint, layout and hide-not-watching controls also live in the header. Marking
something "not watching" applies to the **show** — every view, every month, and your share
page — until you turn it back on.

**🎚️ Filters** in the header narrows your calendar by genre, country, language,
certification, network, show type and keyword, and by who lists a title. Filters belong to
your account, so two people reading the same cached month each see it their own way; new
accounts start unfiltered. The button reads **🎚️ Filtered** while anything is held back. The
**📺 TV** and **🎬 Movies** tabs hold separate answers — excluding a genre from your shows
leaves your films alone:

| | 📺 TV | 🎬 Movies |
|---|---|---|
| Services | which services fill your show calendars | which fill your film calendar |
| Genres | ✅ | ✅ (separate answer) |
| Countries — where a title was *made* | ✅ | ✅ (separate answer) |
| Languages — the language it was made in | ✅ | ✅ (separate answer) |
| Networks | ✅ | — (a film has no network) |
| Certifications | US TV Parental Guidelines | MPA film ratings |
| Show types — TMDB's scripted, reality, talk show… | ✅ | — |
| Keywords — TMDB's, typed one at a time | ✅ | ✅ (separate answer) |
| Missing data — no country, language or poster, or two of them | ✅ | ✅ (separate answer) |
| Hide titles only one service lists | ✅ | ✅ (separate answer) |
| Hide shows only TMDB lists that name no network | ✅ | — |
| Release markets and formats — where a film is being *shown* | — | ✅ |

Press a chip once to keep **only** that value, again to **filter it out**, again to clear it.
Anything the chips don't name can be typed in below them. Networks match **exactly** as the
service spells them: `tvN` and `TVN` are two different broadcasters. A keyword is typed one
per box, because some hold a comma (`san francisco, california`); most new titles have no
keywords at all, so filtering one out is the usual use. At least one service has to stay on.

**A title nothing has described yet is shown until it is**, rather than hidden for a fact
nobody has looked up — Simkl's calendar arrives without countries or languages, TMDB's
without networks. Keywords and show types are TMDB's, so they wait for TMDB's own
description, and a title TMDB doesn't list isn't judged by them. The two exceptions are
about who lists a title, which is known at once: **only one service lists it**, and **only
TMDB lists it with no network**, which hides a brand-new TMDB-only listing until TMDB says
where it airs (usually within minutes).

**The switch at the top turns every filter off without clearing any of them** — for "show
me everything for a bit". Your **share links keep filtering** while it is off.

You can also filter from a card: press its genre, country, language, network or
certification and choose *filter out* or *only this*.

> Trakt gates its own calendar filtering behind a [VIP subscription](https://trakt.tv/vip/filtering);
> this app filters the cached response itself, so Filters work on any account.

### The content floor — Settings > Calendar > Genres / Countries / Certifications / Networks

These look like the per-account 🎚️ Filters, but they are **instance-wide** and are applied
before the shared calendar cache is filled: a title excluded here reaches nobody, whatever
their own Filters say. Use it to trim what nobody on the instance watches, or to keep
content off a family instance (Certifications uses the US TV Parental Guidelines and MPA
film ratings). Blank — the default — filters nothing. A change applies at once, months
already cached included, and undoing it brings those titles straight back.

The floor also carries the newer dimensions: **Languages** and **Keywords** (one floor for
TV and films; keywords one per line), **Show types**, **missing data** per medium, **hide
titles only one service lists** per medium, and **hide shows only TMDB lists that name no
network**. Languages is applied before caching like the fields above; the rest judge a whole
card — who lists it, what TMDB says of it — so they apply as each calendar is read, ahead of
every viewer's own filters. A viewer can only narrow further: a switch or missing-data rule
the floor already applies shows in their 🎚️ Filters ticked and fixed.

## TMDB — the first describer

With a TMDB token in Settings, TMDB is the first service asked about any title that
carries a TMDB id — which is nearly every title Trakt or Simkl lists. It describes the
title in the details panel, gives the tracker its season totals, supplies posters and
network logos, and adds its videos to the panel's menu. Trakt and Simkl answer what TMDB
doesn't know, and a season TMDB has no episodes of goes to the next service that does.
Anyone who set their own service order on their **Account** page keeps it; TMDB comes after
the services they named.

TMDB keeps no watch history, so it never backs the tracker.

**TMDB also lists three calendars**, beside Trakt and Simkl: **Series Premieres** (shows
whose first episode airs that day), **Movie Premieres** (films with a limited, theatrical or
digital release in the instance's TMDB region, on the day of their first release) and
**Season Premieres**. TMDB publishes no season-premieres list, so the app works one out for
each week — the shows on air that week that had nothing on air in the four weeks before,
each checked against TMDB's own season dates. That takes a few hundred requests a week, so
it runs in the background: the first time a week is opened, Trakt's and Simkl's cards show
at once and TMDB's join them within a minute, and the stats box counts the **weeks being
worked out**. Season Finales and All Episodes stay Trakt's and Simkl's.

TMDB gives dates, not times. A card TMDB describes — every card, for anyone whose service
order starts with TMDB, which is the default — sits on TMDB's day and shows a time only when
another service's airing falls on that same day; when the services disagree, the card shows
each one's date side by side. TMDB lists far more than the other two (about five times the
series premieres in a typical month, many of them regional or web series): switch TMDB off
for TV or films in 🎚️ Filters, or trim it with **hide titles only one service lists**, **hide
shows only TMDB lists that name no network**, languages, show types or missing data. A
listing arrives without its network, certification, keywords or show type and is looked up
in the background, so for its first few minutes a TMDB-only title is shown even where those
filters would hide it — except by the no-network rule, which hides it until it is.

**Titles are matched across services in the background**, every minute. This month's and
next month's premieres and films that only TMDB lists are looked up on Trakt by their TMDB
id, and where Trakt has the title its own date joins the card; titles Trakt or Simkl list
without a TMDB id are looked up on TMDB, and merge with TMDB's own card where it has one.
Each answer stands for a week. Trakt allows an app 500 reads every five minutes, shared with
the pages you're waiting on, so the lookups spend at most 50 Trakt requests a minute, and
only while the whole app has sent fewer than 350 in the last five minutes — the pages always
keep the other 150. A card costs about three requests, so that's around fifteen cards a
minute, and a month's backlog takes about two hours. Simkl isn't asked in the background, because its lookup needs a person's own account;
opening a card's details asks it then.

**A card shows a button for every service that knows its title.** The services whose
calendars the card is drawn from are in colour; one that knows the title but doesn't list
this airing — or that your calendar leaves out — is greyed until you hover it. The details
panel offers every service that knows the title, wherever it is opened.

**Every search box searches TMDB** when you press Enter, and all three behave alike: the
calendar's Find a title, the tracker's Add show and Add film, and the rankings board.
Twenty results at a time, with **More from TMDB** for the next twenty. Under the results,
**Also search Trakt**, **Also search Simkl** and **Also search Simkl Anime** appear for each
service *you* have linked; each adds that catalogue's results below the rest, searched with
your own account, and then offers its own **More from …**. (Simkl keeps anime apart from its
TV catalogue, and one search does not reach both.) Typing still lists only titles this
instance already holds, and costs nothing.

**Opening a show gives the same season checklist everywhere**: each season with its episode
count, the suggested one marked, the ones you already have greyed. The tracker can add
several seasons at once, and so can a rankings board, whose checklist also offers the
**Whole show** (dragging a result into a tier adds the whole title). On the calendar, seasons your filters hide say so, seasons already
on your calendar link to their day, and **Go** opens the month of the season you ticked,
on its card when the calendar draws one. A title found on TMDB is looked up on Trakt and Simkl by its
TMDB id when it's added, so the tracker can read your history for it. A show neither
lists can still be added; its row is marked **untracked**, because nothing will count what
you watch of it. A calendar season TMDB found is added as TMDB's own card on a calendar TMDB
lists (a series or season premiere, a film's release); on Finales or All Episodes it opens
the month it should be in.

Get the token at [themoviedb.org → Settings → API](https://www.themoviedb.org/settings/api):
copy the **API Read Access Token** (the long one). The short "API Key" is refused —
it travels in the address of every request, where a credential ends up in logs. Nothing
TMDB answers is kept longer than six months, which is TMDB's own limit; titles on your
tracker or a rankings board are re-read shortly before then, so they never lose their names.

Without a token, TMDB steps aside and the other services describe titles as before. The
search boxes say TMDB isn't configured rather than searching something else; anyone who
has linked Trakt or Simkl can still add that service to a search.

## Simkl — an optional second source

Simkl fills the calendar alongside Trakt, can sign people in, and can read a linked
account's watch history. **None of it is required**, and each capability is gated
separately:

| What you want | What it needs |
|---|---|
| Simkl titles on the calendar | **Nothing.** The calendar comes from public CDN files that take no id and no token |
| Details for a Simkl-only title (overview, episodes, videos) | **Client ID** — Simkl's per-title endpoints take it as a query parameter |
| **Log in with Simkl** / linking a Simkl account | **Client ID + Secret + Public base URL** |
| Reading a linked account's own Simkl history, searching Simkl's catalogue | Each person's **own** Simkl link |

Create an application at [simkl.com/settings/developer](https://simkl.com/settings/developer),
choosing **"Server apps & services"** — Simkl's OAuth v2 needs its own registration, and a v1
application's credentials will not work. The **redirect URI must match byte for byte**:

```
<your public base URL>/auth/simkl/callback
```

Settings ▸ **Simkl** shows the exact string once a public base URL is saved; copy it from
there, then paste the client id and secret back into the same tab. Each person then links
their own account from their **Account** page, and the link refreshes itself.

### What Simkl's calendar does and does not cover

- It reaches roughly **three years back and three months forward**; outside that, the app
  says the source does not go there rather than drawing an empty month.
- It has **no season-finale listing**, so the Season Finales calendar is Trakt's alone.
- Its records arrive without genres, countries, certifications or networks. The app looks
  those up afterwards and shows how many titles are still waiting; until then a title is
  shown rather than filtered out.
- Simkl lists anime films on its anime feed; the app files them onto the movies calendar.

## At-rest encryption of stored secrets

By default, credentials (Trakt tokens, Sonarr/Radarr/Seerr API keys, the TMDB token, and every linked
account's Trakt token) are stored in the database as plaintext. Turning on encryption seals
them with a key that lives **only** in your environment, so a leaked `app.db` (a backup, a
volume snapshot) does not hand over live credentials. It does not defend against full host
compromise — whoever can read the environment has the key too.

### Enabling it

From **⚙️ Settings**, the encryption panel walks through two stages:

1. **Get a key.** Generate one in the UI, or bring your own with:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   **The key is shown once.** Put it in your environment as `ENCRYPTION_KEY` (an `.env`
   file, `docker run -e`, or a compose `environment:` block) and restart.
2. **Verify and encrypt.** After the restart, confirm in Settings that the key survived;
   then one click seals every stored secret and linked token in place.

You can opt out at either step and turn it on later from the same panel.

> **⚠️ Losing `ENCRYPTION_KEY` after enabling makes every sealed secret unrecoverable.** The
> app keeps running and treats them as unset, so credentials must be re-entered and every
> user must re-link Trakt. **Don't re-save credentials while the key is missing** — that
> overwrites ciphertext you could still recover. If a *different* key turns up, an
> administrator is sent to a recovery screen: restore the original key, or run a confirmed
> reset that blanks only the lost values. **Back the key up somewhere durable outside the
> app** — a password manager, not a note beside the database backup.

Non-interactive deployments can set both `ENCRYPTION_KEY` and `ENCRYPT_SECRETS=1`; the app
then seals everything at startup.

### Setting `ENCRYPTION_KEY` for local (non-Docker) runs

`python run.py` loads a `.env` file from the project root before anything reads the
environment:

```bash
cp .env.example .env
# then edit .env and set ENCRYPTION_KEY=<your key>
```

`.env` is gitignored, a real environment variable wins over it, and the Docker image never
reads it.

## Requirements

- Python 3.11+ (3.12 recommended), whose built-in `sqlite3` links SQLite **3.35 or newer** —
  true of the Docker image and of any current Python build; only an old distribution's system
  Python (Ubuntu 20.04 ships 3.31) falls short
- A free [Trakt API](https://trakt.tv/oauth/applications) application (Client ID + Access Token)
- Optionally, a [Simkl](https://simkl.com/settings/developer) application — see
  [Simkl](#simkl--an-optional-second-source) for which parts need one and which need nothing

## Project layout

A shared kernel flat at the root of `app/`, plus one package per feature. Which
package may import which is not a convention but a test —
`tests/kernel/test_layering.py` declares every permitted edge with its reason and
fails on an undeclared one.

```
app/
  main.py           App construction, lifespan/heartbeat, error pages. The
                     composition root: it registers every feature's router
  ── the kernel: no feature, so every feature may depend on it ──
  config.py         Settings model + persistence (secrets/globals in data/app.db, two
                     recovery fields in data/settings.json)
  db.py             SQLite connection policy + schema migrations (data/app.db)
  cache.py          TTL blob cache for detail lookups (shares api_cache)
  secrets_box.py    At-rest encryption primitive (seal/open, key from ENCRYPTION_KEY)
  http_pool.py      One pooled httpx client per outbound service
  chrome.py         The page context every route merges in (nav flags, version)
  assets.py         The cache-busted asset registry each page's <head> renders from
  templating.py     The one Jinja environment
  route_params.py   The query coercions the route modules share
  endpoints.py      Calendar endpoint registry
  changelog.py      CHANGELOG.md, parsed once, for the in-app modal
  perftrace.py      Where a request's time went
  timezones.py      Curated IANA timezone list
  csp.py            The Content-Security-Policy, and the hashes of the two inline blocks
  details_panel.py  The one details panel every surface draws
  youtube.py        Which YouTube video a service's value names, if any
  logging_setup.py  Console logging, plus the optional LOG_FILE
  ── at the root without being kernel ──
  authz.py          Route authorization: declare-or-denied, CSRF/origin rules,
                     the shared JSON body guard. Auth tier, not kernel
  settings_routes.py   Settings and the Trakt device-authorization flow
  ── auth: a layer every feature may depend on, not a peer feature ──
  auth/             passwords, sessions, cookies, identities, handshakes, invites,
                     lockout, prefs, users, levels, admin
    routes.py         Onboarding, register, sign in/out, account page
    admin_routes.py   Admin screen: accounts, invites, retired identifiers
    provider_login.py The sign-in/register/link completion every provider shares
    provider_avatars.py  Seeding an account's picture from the service it signed in with
    trakt.py          The Trakt OAuth flows and refresh-token renewal
    trakt_routes.py   "Log in with Trakt" (OAuth redirect flow)
    simkl.py          The Simkl OAuth v2 flow, with PKCE, refresh and revoke
    simkl_routes.py   "Log in with Simkl"
    plex.py           The Plex PIN flow
    plex_routes.py    "Log in with Plex" (PIN flow)
    encryption_flow.py   Enable/verify/encrypt lifecycle, key-health canary, recovery
    encryption_routes.py Admin encryption endpoints + the recovery screen
    secrets_backfill.py  Seal-in-place conversion shared by the enable flow and Settings
  ── the features ──
  calendar/
    routes.py         The calendar, the month picker, and the tile/detail endpoints
    cache.py          Global UTC window cache + the read path over it
    resolve.py        Which source answers each field of a card two services describe
    enrich.py         Filling in what a source does not publish (genres, network, …)
    filter.py         The read-time genre/country/certification/network filter
    state.py          Per-user not-watching marks (all views) + change detection
    share_routes.py   Public read-only calendars (/s/, /u/, /c/)
    share_links.py    Share-link settings + URL building
    share_code.py     The compact ?p= code a share link is handed out as
    share_card.py     The 1200x630 picture a share link unfurls into
    share_card_cache.py  Rendered cards on disk (data/share_cards), keyed by content
  metadata/         The title store every feature reads: what each service says
                     about a title, which title a set of ids is, and the one way in
    __init__.py       ensure / store_records — registering a title, and the one
                       place the app asks a service for its id by shared id
    identity.py       A title's key, its aliases, and how a key moves up
    holders.py        Every column holding a title's key, moved together on a re-key
    rows.py           Each service's rows for a title and its episodes, and how
                       long each may be kept
    descriptions.py   A service's description of a title, filed and read back
    panel.py          The details panel's answer, from the store first
    labels.py         What a tracker or board row is called and counts, read with it
    refresh.py        Re-reading what a page found stale, behind the page
    pictures.py       A picture that won't load: the next one, and the title healed
    picture_routes.py The public address such a picture reports to
    typed_search.py   The stored titles an add box lists as you type
    backfill.py       Registering, once, everything stored before the store existed,
                       and the words the tracker's and boards' rows carried
  ████████/         The █████ ███████: store, live counts, month rollover, backup,
                     per-user prefs, and the calendar premiere import
    routes.py         The ███████ page and its API
    backfill.py       Filling in never-███████ months from █████ ███████
    █████_███████.py  The incremental per-user █████ cache the ███████ reads
    ███████_fmt.py    Which bucket a show is in, and the two ███████ posts
    prune.py          A hand-run pass over stored months (python -m)
  ranker/
    core.py           The board data layer: tiers, entries, ordering, and what a
                       board keeps of the titles and tiers taken off it
    routes.py         The rankings page and its board API
    item_keys.py      What a board row is called: a title, or one season of it
    seasons.py        What a season row is called
    sources.py        Where rankable titles come from
    imports.py        Seeding a board from what the viewer has finished
    exports.py        Turning a board into the thing a user takes away
    grid_builder.py   The exported poster grid: pixels, and nothing else
  media/
    posters.py        Which picture a title has, and the tiles on disk that the
                       share card and a board export composite
    logos.py          Network logo tiles
    artwork.py        Poster URL bookkeeping (show_posters, season_posters, a
                       title known to have none); no downloads
    user_images.py    Per-user avatar and grid-header images
    imaging.py        Font, text-in-a-box and circular-mask primitives
  integrations/     The three services a user points at their OWN server
    routes.py         Sonarr / Radarr / Seerr: status, options, add-to-library
    arr.py            Sonarr / Radarr
    seer.py           Overseerr / Jellyseerr
  sources/          Per-account source preferences; no routes of its own
    prefs.py          The stored preference and the vocabulary it is written in
  providers/        The calendar-source seam: the Record, capabilities, the registry
    base.py           What a source produces and what every source must implement
    detail_source.py  Which service a details panel asks about a given title
    title_search.py   The one title search the calendar, boards and add dialogs share
    videos.py         Every service's videos for a title, pooled into one ordered list
    tmdb/             transport, normalize, detail, search (posters and logos fetch through it)
    trakt/            transport, calendar, detail, sync, releases, watchlist
    simkl/            transport, calendar, detail, sync, titles, watchlist
  templates/        Jinja2 templates
  static/           CSS (one bundled stylesheet), JS (one directory per page), images
run.py              Dev runner (Hypercorn)
tests/              Mirrors app/ — kernel, providers, calendar, metadata, ████████,
                     ranker, auth, media, integrations — plus encryption/, which is named
                     for a concern because the at-rest key path spans two packages
```

## License

Released under the [MIT License](LICENSE).
