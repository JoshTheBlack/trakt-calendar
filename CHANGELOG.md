# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## 🏷️ [1.1.0] - 2026-07-22

### Accounts & access
- 👥 The app is now **multi-user with sign-in**. First run walks you through creating the admin account and adopts your existing Trakt connection and watching/not-watching data onto it — nothing to migrate by hand.
- 🔐 Sign in with a **username and password**, **Plex**, or **Trakt** — all three can be linked to one account from your profile page.
- 🎟️ **Registration is invite-only** by default. Admins issue invite links with an optional label, expiry, and use limit; an invite normally grants calendar access on the spot, and every unusable invite (expired, revoked, used up, never existed) shows the same page.
- ✅ Access is granted **per account and per area** — calendar access and the hidden area are separate, deliberate grants.
- 🛡️ **Every route now declares who may call it**, and anything undeclared is refused. This closes the big one: `GET /api/settings` used to hand the Trakt token, Trakt client secret, TMDB key, and every *arr API key to anyone who asked. Credentials are now write-only — the Settings screen shows which are set, never what they are.
- 🔒 A run of wrong passwords locks **that account**, not everyone. The per-address limit sits far above anything one person fumbling a password produces, so a household — or everyone behind a reverse proxy — can't be locked out by one neighbour's typos. Lockouts expire on their own and are written to the log, since the sign-in page deliberately can't say why it refused.
- 🧯 Hardened against cross-site requests: changes must be JSON and same-origin, sessions are server-side and revocable, sign-in is rate-limited per username and per address, and the interactive API docs are switched off.

### Admin interface
- ⚙️ **Settings is organised into tabs** — Server, Trakt, Calendar, Integrations — on a wider panel, instead of one long scroll. It's still one form and one Save, and the "reconnect your Trakt account" prompt sits above the tabs so it can't hide behind the one you're not on.
- 🧑‍✈️ A new **/admin** screen: list accounts with their linked providers and last activity, approve or revoke each kind of access, promote/demote admins, reset passwords, disable or delete accounts, revoke individual sessions or sign someone out everywhere, and manage invites and their redemptions.
- 🧹 Two separate destructive actions — **wipe data** (reversible; keeps the account and its links) and **delete account** (full, typed confirmation, retires the username and share links so nobody inherits them).

### Your calendar, your view
- 🗓️ Card style, day packing, hide-not-watching, **timezone**, and your watching/not-watching marks are now **per account** — two people looking at the same month see the same shows with their own marks and their own layout.
- 🌍 All times are stored in UTC and rendered in **your** timezone, with a picker in the header and a one-click "use my device timezone". No silent auto-detection: month and day boundaries shift with the zone, so the change is yours to make.
- ⚡ Calendar data is fetched once per week-long window and **shared across everyone**, refreshed on a short TTL, with genre/country/network/language filtering applied per viewer instead of per request to Trakt. Fewer API calls, faster loads, and the per-show detail cache moved into the same store.
- 🔎 **A Filters button in the header, for everyone.** Genre, country, and network filters moved out of the admin Settings screen into a panel any signed-in account can open, and the button lights up as **🔎 Filtered** whenever something is being held back — so a month that looks short says why. They were previously admin-only *and* wrote the app-wide seed, so they changed nothing about the admin's own calendar and nobody else had any way to filter at all. One cached month, filtered per person.
- 🌍 **New accounts now start with no filters.** The old default quietly excluded nine genres and allowed only 35 countries, which looked like the calendar simply not carrying those shows. Nothing is filtered until you say so. Existing accounts keep whatever they already had, and can clear it in the new panel.
- 👯 **Fixed duplicate cards.** Trakt returns more than the week it's asked for — sometimes two months more — so neighbouring weeks overlapped and the same episode was drawn twice (July 2026 had 207 doubled cards on All Episodes). Each week now keeps only its own days, and the page drops repeats on the way out, so existing installs are fixed without clearing any cache.
- 🔁 Marking something not-watching now sends just that one change, so two open tabs can no longer overwrite each other.
- 🙈 **"Not watching" now means the whole show, everywhere.** Turning off a series or season premiere also takes its episodes off All Episodes, keeps it off next month, and hides it on your shared page — one decision instead of one per calendar per month. Turning it back on brings it back everywhere too, and every mark you already had is carried over. On All Episodes, hiding one episode folds away the show's other episodes **as you click**, with no reload.

### Network logos
- 🖼️ Network badges on cards now show the **real network logo** instead of a text label — looked up per show from TMDB and rendered as a rounded tile, falling back to the 📡 network name when no logo is available.
- 🗝️ New **TMDB API key** setting. Logos are processed once and cached on disk, with a regenerate action to rebuild them.

### Interface
- 🎯 Rebuilt the header as a **compact sticky bar** — endpoint / cards / days controls collapse to icon pills with tooltips, and the item count and generated time merge into a single meta line.
- 🧭 **The same header on every page** — calendar, month picker, account, and admin. Account, Settings, Admin and Sign out collapse into one menu, so an administrator's bar fits on a single line again.
- 📅 The month picker is a tidy **4×3 grid** and carries the header too, instead of being a dead end.
- 🙈 **Hiding not-watching** is smarter: days where every item is hidden collapse entirely, and packed layout now sizes each day's columns to only the visible cards.
- 🚧 A **mistyped address now gets a real page** instead of a line of raw JSON, and it says the same thing whether the address never existed or simply isn't yours to open. Scripts still get JSON.
- 🧹 Removed the storage/sync panel — saves are silent on success and only raise a toast if persistence actually fails.

### Public sharing
- 🔗 Publish your calendar as a **read-only public page** — as an unguessable link, as `/u/your-name`, or as your own custom `/c/slug`. Enable any combination, pick which one gets generated, and rotate the private link at any time.
- 🎛️ Choose **how the link opens**: hand it out reflecting your current display, or pin the calendar, card style, day packing, timezone, and hide-not-watching into the link itself. Either way your own calendar is untouched — the options are written into the URL, not into your settings.
- 👀 Visitors get their own view controls on a shared page (endpoint, cards, days, timezone, hide-not-watching). They're plain links, so no sign-in and no saved state — and the URL they end up on is shareable too.
- ♻️ **Changing your custom name doesn't break links you've already shared** — the old `/c/name` keeps opening your calendar, and nobody else can claim that name afterwards. Every link form you've published stays live; the picker only chooses which one you're handed to copy.
- 🚫 Public pages make **zero API calls**, ever. They serve what is already cached (with a "data as of" line) rather than spending your rate limit for a stranger, and a bad link always 404s the same way regardless of why.

### Your account
- 🪪 Set your own **username and password** from the account page. An account created through Plex or Trakt starts with neither, and a password means you can still get in if you ever lose access to the linked service. Changing a password signs out every other session but keeps you signed in where you are.
- 🔌 **Unlinking Plex or Trakt now revokes the authorization** at the provider instead of leaving it sitting in your connected-apps list. If that can't be reached, the unlink still happens and you're told to finish it there.

### Connecting to Trakt
- 🔑 **Authorize with Trakt** from the Settings panel — a device-code flow pairs the app on trakt.tv instead of pasting an access token by hand. Adds a **Trakt Client Secret** field alongside the Client ID.
- ↻ Access tokens now **refresh automatically** (with a manual *Refresh token now* button), and Settings shows the current token's expiry status.
- 🔑 **Authorizing with Trakt shows the pairing code properly** — its own field with a Copy button and a button that opens trakt.tv, instead of a code bolded inside a sentence. The Authorize button is held down while a code is live, because pressing it again quietly issued a *new* code and invalidated the one you had just copied.
- 🔗 When a Trakt authorization succeeds but can't be attached to your login, Settings now **says why** and offers a one-click retry, instead of leaving the same prompt up with no explanation.
- 🙈 Fixed the "reconnect your Trakt account" notice never going away: Settings panels marked hidden were being drawn anyway, so the notice ignored the app's answer entirely and showed for every administrator whether or not it applied. The same fault was quietly showing the redirect-URI and cookie-policy panels to everyone.

### Ops
- 🍪 **First-run setup works out the session-cookie policy for you** from the browser that sets the instance up, so a plain-HTTP LAN install and an HTTPS deployment both work with no configuration. If it's ever wrong, the sign-in page and Settings say so instead of leaving you looping back to the login form.
- 🚶 **Trusted proxy addresses** are editable in Settings, which shows the address your requests are actually arriving from and warns when forwarded headers are being ignored — the misconfiguration that makes every user look like one IP. The Docker image passes the same value through to the server.
- 🎚️ Calendar cache lifetime and the total cache size cap are now editable in Settings instead of only in the config file.
- 🔇 The per-request access log is **off by default** (set `ACCESS_LOG=1` to bring it back); app diagnostics log at INFO while third-party libraries are quieted to WARNING.
- 📦 Added Pillow + cairosvg for logo rendering; the Docker image now installs `libcairo2`, `libjpeg`, and `zlib` to match.
- 🧪 Added an offline test suite — 625 tests, no credentials or network required.

### 🥚
- There's something hidden in here now. No hints — you'll know it when you find it.
- It makes a noise.
- It now checks whether it has anywhere to take you. If it doesn't, it just makes the noise.
- Whatever it is you found, you can now take a copy of it home, and put it back.
- Its decorations are yours alone now, rather than shared with everyone else who found it. They travel in the copy you take home.
- Clicking a row opens the full details, with the ones you've already seen ticked off — and each tick takes you to it.

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
