# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## 🏷️ [1.1.7] - 2026-08-01

### Fixed
- 🗓️ **A mistyped month in a backfill is now refused instead of quietly becoming a different one.** Asking to fill in `2026-7` — the month unpadded — didn't fail; it threw away what you typed, fell back to the offered range, and went and surveyed January through last month instead. You'd get a believable-looking result covering eight months when you'd asked for two, with nothing anywhere saying your dates had been discarded. A month that isn't written `YYYY-MM` now says so and does nothing. Leaving a box empty still means "use the range you offered me", which is the only case a default belonged in.
- 🎬 **The backfill stops giving film advice on runs that found no films.** Finishing a backfill always ended with a line telling you to go and switch a setting to Movies — whether or not it had written a single film, and naming a control on another screen by a label that screen was free to change. It now mentions films only when it wrote some, and points at the Rankings page rather than at a field on it.
- 🔑 **Hand-editing `data/settings.json` no longer wipes the instance's saved credentials.** Adding a setting to that file — the only way back into an instance nobody can sign in to — quietly emptied the Trakt client secret, the Trakt access token and every integration API key the next time anybody used the app. Anything left out of that file is now left exactly as it was, and a credential is only cleared by clearing it in Settings. If it's the public base URL that has locked you out — whether it was never set or is set to somewhere you aren't browsing from, which refuses every sign-in as coming from another origin — you no longer need to touch the file at all: set `PUBLIC_BASE_URL` in the environment and restart, and it takes precedence over the saved one so you can sign in and save the real one in Settings. It says so while it's in force, in the startup log and beside the field itself, so a variable you forgot can't quietly outrank what you save; remove it and restart when you're done.
- 📚 **Adding a title from the details popup now marks the calendar tile behind it straight away.** Requesting a title through Sonarr, Radarr or Seerr from inside an open popup added it successfully but left the tile showing a plain Add button until the page was refreshed. It now shows the mark the moment the request succeeds, whichever of the three you used. (Administrators only — nobody else sees those buttons.)

### Under the hood
- 🗂️ **The code behind the app is filed by what it does.** Each part of the app — the calendar, the boards, sign-in, pictures — now lives together in one place instead of being spread across a long flat list of files, and which parts are allowed to depend on which is written down and checked automatically. Nothing about using the app changes; it just makes the next change easier to make and harder to get wrong.

### 🥚
- It now lets a month's second notice hold only what belongs to it — one that hasn't begun says just what starts in it, one that's over keeps what it settled and lets go of whatever was still open on its last day, and anything you'd already turned away is simply absent rather than listed as given up on. Turn something away later, once the month is under way, and it still says so. The first notice is untouched by any of this: in every month, behind you or ahead, it announces everything that began that month, whatever each of those things has since become. The month you're in is unchanged.
- It no longer builds a month the calendar hasn't reached out of the current one's contents, nor quietly closes off the months in between. Only the month under way and the one immediately after it get filled in.
- It now asks which month you want rather than taking whichever one you arrived from, and gathers nothing until you've said — every way in asks, the page you came from included. A month it can't speak for yet is shown but can't be chosen; one it already holds always can.
- A month that hasn't begun waits to be asked. It opens empty, saying so, until you tell it to gather — and what it then takes is only what begins in that month, with nothing carried over from the months before it. The month under way still fills itself in the moment you open it, and a month further ahead than it can see says that instead, in the same words it uses if you ask anyway.

## 🏷️ [1.1.6] - 2026-07-31

### Look and feel
- 🍿 **A mistyped address now gets an intermission.** A wrong link used to land on a plain card; it now opens on a cinema curtain with a marching line of concession-stand snacks and the lobby jingle waiting behind a play button. It says exactly what the old page said about where you are and how to get back — a dead end is just the one place with room to be charming about it.

### What's new
- 🖼️ **A shared link now previews the month it points at.** Pasting a share link into Discord or Slack used to show the same picture every time, whichever calendar and whichever month it led to. It now unfurls into a card for that month — the month and year, how much is on it, and the titles leading it with their posters and air dates, series premieres marked. It shows exactly what the page behind the link shows, so the number on the picture is the number you'd see if you opened it.
- 📜 **The changelog is readable from inside the app.** The ☰ menu has a *What's new* entry on every page, opening this file rendered properly — one collapsible section per release, newest already open. It is the same `CHANGELOG.md` that lives in the repository, rendered on the way past rather than copied, so what you read here cannot drift from what was actually written.

### Changed
- 🏆 **A board's tiers are ordered by their priority.** They used to sit in whatever order they were created in, with no way to move one afterwards — so a tier added later stayed at the bottom no matter what it was for. The board now reads top to bottom in priority order, the same order the ranking and its exports were already built in, and changing a tier's priority in its settings moves it straight away. Tiers sharing a priority stay where they are relative to each other.
- 📡 **Networks can be left out as well as picked.** The Networks box, on both Settings and Filters, took a list of networks to keep and nothing else. Put a `-` in front of a name — `-Apple TV` — and it now excludes that one instead, which is how genres, countries and certifications have always worked. Names are still matched exactly as Trakt spells them, capitals and all: `tvN` and `TVN` really are two different networks.
- 📆 **The month picker has one address, and it's the app's front page.** The month and year at the top of a calendar used to send you to a second address showing the same picker, and the picker's own year arrows kept you on that one. Everything now uses `/` — the address you'd type or bookmark. An old `/pick` link no longer resolves; go to `/` instead.

### Fixed
- 🎨 **The month picker keeps its styling when you arrive from the calendar.** Clicking the month and year at the top of a calendar dropped you onto an unstyled page, while loading the same address fresh looked right. It now looks the same however you get to it.
- 🖼️ **A too-big picture always says it's the picture that's too big.** Uploading an image past a certain size stopped naming the limit and fell back to "that request is too large", which tells you nothing you can do something about. Any image that's refused for its size now says so in the same words, with the size it has to come under.
- 🪜 **A tier you haven't opened yet no longer disturbs a drag into the one below it.** On a board big enough that its tiers arrive closed, dragging a title into a tier could set the row it was displacing shuffling aside over and over — until you'd opened the tier above it once, after which the same drag was smooth. Tiers that haven't been opened now stay out of the way.
- 🖱️ **Dragging in a long ranker pool no longer stutters, and nothing disappears.** On a pool big enough to load in pages, dragging a title out and back could set the row it was displacing shuffling over and over, and the title being dragged would vanish on drop until you reloaded. It was the next page of the pool arriving in the middle of the drag; it now arrives without disturbing what you're holding.
- 📚 **Days further down the calendar show their library marks straight away.** The Sonarr, Radarr and Seerr buttons on the first few days correctly showed what you already had; the days that load as you scroll to them arrived unmarked and stayed that way for up to a minute. They now say what they know the moment the day appears. (Administrators only — nobody else sees those buttons.)
- 🔢 **A greyed-out day chip comes back when you show that day again.** Opening the calendar with *Hiding* switched on greyed out the jump-to chips for days whose every show you'd marked not-watching — and switching to *Showing all* left them greyed and unclickable until you reloaded the page. They now become destinations again the moment the day has something on it.

### Under the hood
- 🧹 **A large rearranging of the code behind the app**, into smaller pieces that each do one thing, with the tests reorganized to match. Nothing about using the app changes — this is groundwork, so that what comes next is quicker to build and safer to change.
- 📦 **Requests that send data now have one size limit.** Every endpoint that accepts a body reads it through the same check instead of each deciding for itself, so an oversized or malformed one is refused the same way everywhere and says so. The limit sits comfortably above the largest thing the app legitimately sends, so uploads and backups are unaffected.

### 🥚
- 🤝 Two parts of it were reading the same record and quietly disagreeing about it, and the one you could see was the one with it wrong. They agree again — nothing was lost, and nothing needed fetching afresh.

## 🏷️ [1.1.5] - 2026-07-28

### Sharing
- 🔗 **Share links are short again.** A link carrying every option used to run to a hundred characters of query string with two of them percent-encoded; the same link is now `…/c/testing?p=13F11010E2A7`. Opening it puts the ordinary address back in the address bar straight away, so what a visitor bookmarks, edits or re-shares is still the plain readable URL — and every long link handed out before this keeps working exactly as it did.
- 📅 **Share one particular month.** "How the link opens" now has an **Opens on** setting: leave it as it was and the link lands on whichever month it's opened in, or pin it to a month and a year — hand round August and it opens on August, in September as much as in July. Whoever has the link can still page to any other month from there, and pinning changes only the link, never your own calendar.

### 🥚
- 🗓️ It can now be told about the months before it was paying attention, working them out from what you'd already watched. It shows you what it found before it keeps any of it, and what turns up counts towards the boards.
- ➕ Something it had no way of knowing about can be put into an old month by hand — films included, now that films can be added at all — and anything that shouldn't have made it onto an old month can be taken back off.
- 🎬 Films you watched are actually shown on the month, rather than only appearing tucked inside the text it writes for you.
- ✅ It now minds *when* you got to the end of something, not just whether you did — so each month shows what you actually finished during it, rather than everything you'd ever finished.
- 🔗 The link it writes points at the month it's talking about.
- ✕ Taking something off a list now sticks.

## 🏷️ [1.1.4] - 2026-07-28

### Rankings
- 🏆 **A new Rankings page**, at `/rankings`. Build boards of shows and movies — "Top Movies 2026", "Top Reality 2026" — and arrange them into your own tiers. A board keeps its own pool of candidates alongside the tiers you've sorted, so you can gather far more than you'll finish with and curate down. Deleting a tier hands its titles back to the pool rather than throwing them away.
- ✅ **Rankings is its own access grant**, separate from everything else. Administrators approve or revoke it per account from the admin screen, and invites carry a "Grants rankings on accept" checkbox — ticked by default — so an invited account arrives ready to use it. Invites issued before this release grant nothing new.
- 🔎 **Search for shows and movies to add**, either one, from the same box. Results show the poster, the year and the network or runtime, so two things with the same name can still be told apart. Adding the same title twice does nothing rather than duplicating it, and a title you've already sorted into a tier stays where you put it.
- 🖱️ **Drag titles from the pool into a tier, between tiers, and up and down within one** — with touch supported, so it works on a phone as well as a mouse. The pool stays pinned in view as you scroll down the tiers, so dragging something into your bottom tier doesn't mean scrolling the pool off the screen first. Every move saves itself a moment later, with a quiet "Saved" in the header; if you have the same board open in two tabs, the second one to save is told rather than quietly overwriting the first.
- ⌨️ **Everything dragging can do is also a button.** Each row carries move up, move down, and move-to-tier controls, so the ranking is reachable without a pointer at all.
- ☑️ **Select a run of titles in the pool and move them into a tier together** — shift-click for a range, ctrl- or cmd-click to pick individually. Curating a couple of hundred candidates one drag at a time is a chore; this isn't.
- 🎯 **Drag a search result straight into a tier**, skipping the pool entirely.
- 🪄 **"Apply template"** drops in a ready-made S/A/B/C/D/F set with sensible priorities and colours, so a new board isn't a blank page. On a board that already has tiers it adds only the ones missing.
- ⭐ **Seed a board from your own ratings.** If you've linked a Trakt account, one click turns your scores into a starting arrangement — 10s in the top tier, 9s below them, and so on — creating whichever tiers it needs. It shows you the counts before it changes anything, it never moves a title you've placed yourself, and it's a starting point rather than a sync: run it again later and it only picks up what you've rated since. The option simply isn't there if you haven't linked an account.
- ↩️ **Undo for the two things you can't drag back**: taking a title off a board, and deleting a tier. Both offer an Undo in the toast that follows them.
- 🖼️ **Turn a ranking into a poster grid image** you can post anywhere. Choose how many titles (up to 100), how many columns, full size or half, and WebP, JPEG or PNG. Your picture and a title line sit across the top in type sized to read from across a room, each poster carries its rank underneath tinted in its tier's colour, and titles underneath are optional — as is a podium row that gives the top three the space they earned. A title whose artwork is missing gets a placeholder tile rather than a hole. Re-exporting something you've already made hands back the same file instantly.
- 👀 **The export dialog previews what you'll get, live**, re-rendering as you change the options and showing the exact pixel size beside them. **Click the preview to zoom in** and move the pointer to look around it — handy for checking the header and your picture, which are a small part of a tall grid. The finished file's size is reported too, with a nudge toward the smaller formats if it's over Discord's 10 MB limit.
- 📏 **A grid too big for the format you picked is caught while you're choosing it**, with the size it came to, that format's ceiling, and what to change — WebP in particular cannot go beyond 16,383 pixels, which 100 titles in three columns comfortably exceeds.
- 📋 **Export the same ranking as Markdown** for pasting into Discord or anywhere else, respecting the same scope and top-X as the image, with headings when it spans several tiers.
- 🚀 **Big boards stay quick.** The pool loads a page at a time as you scroll it, and on a board with a lot already sorted the tiers arrive closed and fill in the first time you open one — so a thousand-title board doesn't cost a thousand posters before you can see anything. If any one part of the page can't be loaded it says so in place with a Retry, rather than taking the board down with it.
- 🔀 **Switching between boards doesn't reload the page**, and the back button still works. Boards are grouped by year in the switcher.
- 🗄️ **Back up your boards and restore them later**, from the Backup entry in the header menu. One file holds every board, every tier and everything in them, and it restores into a rebuilt database intact. Restoring replaces what's there, so it asks you to type the confirmation out. Pictures aren't part of it; they belong to your account rather than to a board, and the dialog says so.
- 🖼️ Poster artwork is cached on the server under a configurable size cap (Settings → Calendar), so tiles load instantly.

### Accounts
- 🏷️ **Pick a display name, separate from your username.** Your username is still what you sign in with — lowercase, and the thing share links are built from — but what people see can have capitals and spaces. Set it on your account page, where it's the heading and sits next to your username so it's clear which is which. It's used by default on ranker exports, and the export dialog still lets you type something else for a one-off. Leave it empty to go back to being shown by your username.
- 🖼️ **Upload a profile picture** from your account page. It's centred, cropped to a square and re-encoded on upload, so an odd aspect ratio or leftover photo metadata never makes it into what's stored. Re-uploading replaces it; removing it is one click.
- 🏞️ **Save up to five header images** for the top of an exported grid, as an alternative to your profile picture. Your account page lists them with their names editable in place, a Remove for each, and how many slots are used; the export dialog shows the one you've picked beside its name.

### Admin interface
- 🚪 **Open registration is a setting now** (Settings → Server) rather than something reachable only by hand-editing a file. Choose invite-only or open sign-up, and the screen warns you in place about what you're turning on.
- ✅ **"New accounts" is its own separate setting**, deciding whether a new account gets calendar access immediately or waits for an administrator. Deliberately independent of open registration — who may create an account and who may see the data are two different decisions, and keeping them apart allows the useful middle setting of open sign-up with each account still reviewed. It applies to invited sign-ups too. Rankings and everything else stay manual grants either way.

### Look and feel
- 🧭 **One header, identical on every page.** Logo on the left, whatever that page needs in the middle, and the ☰ menu on the right — with the same entries in the same order everywhere, instead of links that moved around or went missing depending on where you were. The links that take you between pages live in the menu, so the bar no longer changes width from page to page.
- 📱 **Posters are no longer cropped on a phone held upright** in the "Poster beside" card style. The poster took its height from the description next to it, so a long description squeezed the artwork; it keeps its proper shape now.

### Internals
- 🧹 Rankings search is rate-limited through the same storage every other limit in the app uses, so the allowance is tracked correctly no matter how the server is deployed.
- 🧹 Deleting an account sweeps up the images it uploaded, rather than leaving files behind with no row to point at them.
- 🧹 The site header is built from a single shared template rather than six hand-maintained copies, which is what had let them drift apart.

### 🥚
- What it has been quietly keeping track of can be handed over to the new boards in one go — everything, or just the year you'd rather look at. It goes by when you finished with something, not when it first turned up.

## 🏷️ [1.1.3] - 2026-07-25

### Admin interface
- 🎬 **Certification filtering** (US TV Parental Guidelines for shows, MPA ratings for movies), set with a click-to-cycle chip picker — no free text, since both rating systems are small and fixed. It shows up both as an instance-wide floor (Settings → Calendar) and as your own 🔎 Filters, the same two-layer setup genre/country already has.

### Reliability
- 🚦 **Trakt rate-limit handling.** Every Trakt call now reads Trakt's own "slow down" signal and backs off and retries automatically, instead of a rate limit silently turning into "this show has no data" on screen with no error and no retry. If retries still run out, an already-loaded view degrades to what it last knew with a note that a refresh will try again, rather than showing wrong numbers or a server error.

### Accounts & access
- 🔗 **Login, registration, and invite links now unfurl properly when pasted into Discord/Slack/etc.** — they show this app's real preview image and link instead of a blank or generic one.

### Calendar performance
- ⚡ **The calendar shows up immediately instead of after the whole month is ready.** The header, the month's totals, the jump-to strip and the first few days arrive in one fast response; every later day is already there as itself — its date, its heading, and the right amount of space held open — and fills in with its cards as you reach it. A day you never scroll to is never fetched at all, so opening a month to see what's on this week costs a fraction of what it used to. Days that arrive late are indistinguishable from the ones that came with the page: the same layout, your filters and hidden shows already applied, and ✨ NEW marked correctly.
- 🧭 **Changing month or calendar no longer reloads the page.** The arrows and the calendar picker swap just the content, so the header, your scroll position and everything already loaded stay put, and the back button still works.
- 🩹 **One day failing is now a gap, not the end of the month.** If Trakt can't be reached for part of a month, that day says so and offers a Retry that reloads just that day. Previously one failure took out every remaining day at once.
- ⚠️ **A month that only partly loaded says so**, with a banner, instead of quietly looking like a quiet month.
- 🔥 **Optional background pre-warming** (Settings → Calendar). Off by default; when on, the app quietly fetches the next couple of months in the background so the first visit of the day is already warm.
- 🎨 **No more flash of unstyled page on a hard refresh.** The stylesheet was queued behind the fonts, so the page could paint before any of it applied. It's requested first now, the dark background is declared up front so the moment before paint isn't a white flash, and two small things in the header — the logo and an occasional extra link — no longer arrive late and shove the bar sideways. Fonts are also cached properly now, so the text stops re-flowing on every visit.

### Internals
- 🧹 Cleaned up leftover internal-planning references in source comments and docstrings across the project (comment-only, no behavior change).
- ⏱️ Added debug-level timing spans around the calendar's month fetch and HTML render, so the server-side "time to first byte" for the calendar page can be measured on the `app.perf` channel ahead of upcoming performance work.

### 🥚
- When it gathers what it needs for the month, it now follows the same familiar path as everything else—quietly leaving behind whatever you've already turned away.
- It's learned to pace itself on a big list instead of asking for everything at once — and if it ever gets told to slow down, one entry sits out with a note to try again rather than quietly showing zero.
- For those who've found it, the way back in no longer turns up a moment after the rest — it's simply there as the page draws, instead of appearing late and nudging its neighbours aside.

## 🏷️ [1.1.2] - 2026-07-23

### Security
- 🔐 **Optional at-rest encryption for stored secrets.** Turn it on from Settings: generate or bring your own key, save it to your environment (never written to disk by this app), confirm it survived a restart, then encrypt every stored credential and linked account's Trakt token in one step. A key that's merely missing degrades gracefully — the app keeps running and treats the sealed values as unset rather than crashing, and refuses to link or relink a provider account rather than write a fresh token in the clear over one that could still be recovered — while a *wrong* key routes an administrator to a dedicated recovery screen. That screen now also covers a lost-and-never-replaced key, offering to generate a brand new one on the spot. See the README for the full walkthrough and the loud warning about what losing the key costs you.
- 🗄️ **Configuration consolidated into the database.** Everything that used to live in `data/settings.json` — credentials, Sonarr/Radarr/Seerr settings, timezone, layout, the works — now lives in `data/app.db`, giving the instance one backup-and-restore unit instead of two. The file shrinks down to the two settings that have to stay hand-editable for lockout recovery (`cookie_secure`, `allow_open_registration`); an existing instance migrates on its first boot with nothing to do by hand.

### Admin interface
- 📅 **The instance-wide genre/country/network filter is back in Settings → Calendar**, with a clear label: this pre-filters the *shared* calendar cache before anyone's own 🔎 Filters ever see it, so it removes content for every user of the instance, not just whoever set it. It had quietly stopped being reachable from the UI even though it was still doing exactly that.
- 🖱️ **No more browser confirm() popups.** Every "are you sure" (deleting, unlinking, disabling, resetting) now shows inline under the button you clicked instead of a native dialog.

## 🏷️ [1.1.1] - 2026-07-22

### Fixes
- 🐳 **The Docker image starts again.** The container passed `--forwarded-allow-ips` to Hypercorn, which has no such option, so it crashed on boot with "unrecognized arguments". The app reads `TRUSTED_PROXY_IPS` itself and does its own forwarded-header parsing, so the flag was never needed — it's gone.
- ▶️ **Trailer embeds play behind a reverse proxy again.** The YouTube embed needs to send its origin to verify the page, and a `Referrer-Policy` in front of the app (e.g. from Traefik) was stripping it, so the player showed an error instead. The trailer iframe now sets its own `referrerpolicy` so it works regardless of the proxy's policy.

### Admin interface
- 🍪 **Session cookie security is now set in Settings → Server**, instead of only by hand-editing `data/settings.json`. Choose Always (the default, correct for any HTTPS deployment including behind a reverse proxy), Auto (detect per request), or Never (plain-HTTP LAN only). Picking "Always" while you're actually on `http://` is refused with an explanation rather than silently locking you out.

### Public sharing
- 🪟 **Clicking a card on a shared calendar now opens the full details** — overview, trailer, cast, and episode list — the same modal the calendar page shows. It's served entirely from what your own views already cached, so a shared page still makes **zero Trakt calls**: shows you've opened yourself show everything, and any others fill in as you browse them.

### 🥚
- Its decorations now fill themselves in on the things you pinned up by hand before it knew how to make them, instead of only on what it gathered itself.
- Its first notice holds the whole month again — a thing stays listed there once it has begun, and only drops off if it moves out of the month entirely.

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
