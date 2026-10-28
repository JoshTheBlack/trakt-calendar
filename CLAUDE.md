# CLAUDE.md

Self-hosted FastAPI app: a Trakt-backed TV/movie calendar, a watch tracker, and
a ranking board. One process, SQLite, server-rendered Jinja + HTMX. README.md
covers install and operation; this file covers the things a change can silently
break.

## Invariants — each one lives somewhere; go read that before changing it

Every entry names its home. An invariant with no stated owner is how three
modules quietly stopped restating one and a survey read that as a hole.

**Which package may import which is declared, and the declaration is checked.**
`tests/kernel/test_layering.py` owns it. `app/` is a kernel — config, db, cache,
templating, endpoints and the rest, flat at the root — plus feature packages
(`calendar/`, `distrakt/`, `ranker/`, `media/`, `integrations/`, `providers/`),
with `auth/` as a LAYER rather than a peer: every feature may depend on it and it
depends on no feature. The kernel imports no feature, with two declared
exceptions — the provider registry (config.py asking which sources are
configured, templating.py's `source_look` reading a service's label), and auth's
`encryption_flow` to refuse writing a secret while the at-rest key is unhealthy.
All are function-local, both edges are declared DEFERRED, and hoisting one to
module level fails the test: that placement is the only reason they do not close
an import cycle at load time. Every cross-package import must appear in that file's `DECLARED_EDGES`
with a sentence saying why; an undeclared edge fails and the failure names the
file, the line and the target. A declaration nothing exercises fails too, so the
table cannot rot into a blanket permission nobody has read since. Three modules
sit at the root without being kernel and are named there with the reason:
`main.py` (the composition root, exempt as an importer because registering every
feature's router is what it is for), `authz.py` (auth tier) and
`settings_routes.py`.

**Mutating requests must be `application/json`.** Enforced app-wide, for every
route at once, by `request_shape_guard` in `app/authz.py` — before routing, and
by exact type match. It is CSRF defence-in-depth behind the session cookie's
`SameSite=Lax`: a cross-origin HTML form can only send form-encoded or
text/plain, so the check makes that POST unreachable. Do not re-check it in a
route, and do not call `await request.json()` in one — `authz.json_body()` is
the shared guard (413 over the size cap, 400 malformed, 400 non-object), and
`authz.error()` is the refusal shape the front end reads.

**The Content-Security-Policy is strict on script AND style, and it is enforced.**
`app/csp.py` owns it; `content_security_policy` in `app/main.py` sets it on every
response. Neither script-src nor style-src carries `'unsafe-inline'` or
`'unsafe-eval'`, so four things a template may not do: an `on*=` attribute (a
`data-` attribute and a listener instead — see the JavaScript section), a
`style=""` attribute (a per-render value travels as a `data-` attribute that
style.css reads with typed `attr()`, measured in Chromium, Firefox and WebKit),
an htmx expression it builds with the Function constructor (an `hx-trigger`
filter, `hx-on`, a `js:` value), and an inline `<script>` or `<style>` outside
`csp.INLINE_BLOCKS` — the two there are admitted by hashes derived from what the
templates RENDER, once per start, so there is no second copy of their text.
`tests/kernel/test_inline_script.py` fails on all four, because a browser only
logs one console line and a button silently does nothing. img-src admits one
remote host, the image proxy: a blocked picture is a read that skipped
`proxied_image`, fixed there, never by widening the header. There is no
report-only header and no reporting endpoint.

**The session ROW is the authority on expiry, not the cookie.**
`app/auth/cookies.py` issues it HttpOnly, SameSite=Lax, Path=/, no Domain, and
Secure with the `__Host-` name unless the Secure policy says otherwise; its
Max-Age is the ABSOLUTE cap, never the sliding window (`app/auth/sessions.py`
owns both clocks). A cookie expiring at the sliding window would log out the
active user the sliding exists to keep signed in.

**Stored secrets are `enc:v1:`-prefixed ciphertext when encryption is on**, and
plain otherwise, so sealed and legacy rows coexist in one column and a later
scheme can be told apart (`app/secrets_box.py`). Ciphertext must never reach a
provider: a sealed value with no key configured reads as unset and the app
degrades.

**The HTTP response cache is URL-keyed and GLOBAL.** A response that depended on
whose token asked must never be written to it — that is what `private=True` on
`cached_get` is for, and why a provider package separates public per-title
lookups (`detail.py`) from per-user reads (`sync.py`): "does this module cache?"
then has one answer per module. THERE ARE NOW THREE PROVIDER TRANSPORTS
(`app/providers/trakt/transport.py`, `app/providers/simkl/transport.py` and
`app/providers/tmdb/transport.py`), and they differ because the services differ
in what a request carries, not because the rule does. A change to what `private`
MEANS belongs in the Trakt and Simkl ones; TMDB's makes only public catalogue
reads with the instance's token and so has no `private` flag at all — a read
made with a viewer's own TMDB token must not ride its `cached_get`.
`tests/providers/test_protocol_conformance.py` is what holds the packages to one
shape. EVERY TRAKT READ IS PACED IN ONE OF TWO KINDS OF BUCKET, because Trakt
counts two quotas — 500 GETs per five minutes for the application (client id
only) and 500 for each person (their bearer), MEASURED: it publishes 1,000, and
refused every read for five minutes at ~495 — so `transport.APP_GET_PACER` and a
bucket per bearer digest, each at `GET_LIMIT` (450). A Trakt request that skips
`transport.send` skips both.

**A write to a person's own list at a service is read from its BODY and
confirmed by READING THE LIST BACK, and it never blocks the tracker.**
`WatchlistPort` in `app/providers/base.py` owns the rule and states it; each
provider implements it (`app/providers/simkl/watchlist.py` and
`app/providers/trakt/watchlist.py`, which reads its list EVERY PAGE — Trakt's
watchlist pages at 100, and a read that stopped at one page once missed 46
shows — and takes a full list's 420 as REFUSED, never retried). A service may
answer success to a request that did nothing — Simkl answers any `/sync/` path
and answered a put of a title already listed with the same body as a real move —
so an outcome comes from the answer's body, a written title is WRITTEN only when
a read of the list afterwards shows it, and what the service held BEFORE is read
at the moment of the write and kept, because it is the only thing an undo can
restore. A write moves a title the viewer may have put somewhere on purpose, so
dropping never adds a title and an undo never takes what the viewer did
themselves. The tracker is the record: a failed write is logged and counted, and
the tracker action that caused it goes through.

**What the tracker owes those lists is ONE module's answer, and the tracker never
waits for it.** `app/distrakt/watchlist_writes.py` owns both. No route says "write
this show": every pass compares which shows are on the tracker now (`on_tracker`
— the viewer's list and this and later months' premieres, less a season already
settled in them; never a completed record, a past month or a film) with what
`watchlist_writes` rows say was done, and writes the difference — so a new way
onto the tracker needs nothing, and a pass lost to a restart is made up by the
next. Passes run one at a time per viewer, which with the table deciding what is
owed is what stops two requests writing one show twice. The trigger is a
dependency of the tracker's ROUTER (`_owe_watchlist_pass` in
`app/distrakt/routes.py`), so a route cannot be written without it
(`tests/distrakt/test_watchlist_hook.py` holds every route to it); it runs after
the answer has gone and only starts the pass as a task of its own, so no request
— this one or the next on the connection — waits for a service. The only
explicit hooks are the viewer's own departures, Abandon and ✕, which say so
BEFORE changing anything (`expect`) and are acted on only once their request has
finished, because a pass already running could otherwise read the tracker
before the removal and decide it was not the last season. A show that leaves by
any other road is forgotten, so its return is an arrival. Neither table travels
in a tracker backup, and `app/distrakt/backup.py` says why.

**TMDB is first in `Source`, and nothing it supplies outlives six months.**
`app/providers/base.py`'s `Source` declares TMDB first, and that ORDER is the
app's one order: who describes a title (the details panel and the tracker's
season totals, through `detail_source`), whose value a card shows for a viewer
with no stated order, whose artwork is tried first. TMDB keeps no watch history,
so it is never a tracker source (`providers.tracker_sources`), and a tracker
preference may not name it (`app/sources/prefs.py`). It IS a calendar source for
Series, Season and Movie Premieres (`app/providers/tmdb/calendar.py`; finales
and All Episodes are not its to list), every date a DAY, so a TMDB-first
viewer's card sits on TMDB's day and borrows a time only from a service whose
instant falls on it — through the same join and placement as every other
service, with nothing TMDB-specific in either. A film is dated by its discover
entry's own (primary) release date and trimmed to the window, which drops a
later regional release (digital after theatrical): one day per film, measured
equal to its first regional release for 1,037 of 1,042 films. TMDB's API terms forbid
caching anything obtained from it for longer than six months: the ceiling is
`app/providers/tmdb/transport.py`'s `MAX_CACHE_SECONDS`, response-cache rows are
written with `CACHE_RETAIN_SECONDS` so the sweep's grace lands on it, and the
poster registry's TMDB rows (`app/media/artwork.py`), the poster tiles and the
network logos all read that constant; the title store's rows declare it through
the registry (`Capabilities.retain_seconds`) and `rows.sweep_past_ceilings`
deletes by it, held or not. A CEILING IS ABOUT EVERY WORD AND A ROW HAS ONE
CLOCK, so for such a service a store row's `fetched_at` is its OLDEST word's: a
write renews it only when it rewrites everything the row holds
(`rows.ceiling_clock` for an upsert, `rows.kept_clock` for a write made from
Python; `rows.WORDS` names the words, and tests/metadata/test_retention.py fails
on a column of the three tables that neither it nor `rows.NOT_WORDS` names), and
the heartbeat re-reads a held row before its ceiling (`refresh.queue_ceiling`).
The credential is the v4 read token,
sent only in the Authorization header (`config.tmdb_token_error` refuses the
short key, `tmdb_configured` reads a stored one as unset). NO SERVICE IS
ANOTHER'S FALLBACK: a per-service difference names each service explicitly —
through the registry (`Provider.title_url`, `Provider.episode_url`,
`DetailPort.fetch_season_summary`) or an explicit table with a test that every
registered Source has an entry — and an unknown name is an error, never quietly
read as one of the others.

**ONE TITLE STORE, AND ONE WAY IN.** `app/metadata/` owns what every service says
about a title (`title_metadata`, `episode_metadata` — a row per service, each with
its own expiry; `rows.py`), which title a set of ids is (`titles`,
`title_aliases`; `identity.py`), and every column elsewhere that holds a title's
key (`holders.py`). Every path that meets a title registers it there:
`store_records` for a service's own listings, inside the caller's transaction and
asking nobody (a fill names hundreds of titles); `ensure` / `ensure_many` for a
title met by its ids — a search pick, a tracker arrival, a board add, an import, a
restore — which, handed a viewer's settings, ask each service the title has no id
from ONCE by shared id. THAT IS THE ONE PLACE THE APP ASKS BY SHARED ID
(`providers.for_shared_id_lookup`; each port keeps its own day-long clock). A
caller spending ONE service's allowance narrows it to that service
(`metadata.ask(..., asking=)`): the background completion
(`app/calendar/completion.py`, on the heartbeat) asks Trakt about this and next
month's premiere and film cards only TMDB lists, paced by the Trakt REQUESTS a
pass sends (`TRAKT_READS_PER_MINUTE`, counted by `http_pool.tally`), and TMDB
about titles with no TMDB id, each answer remembered a week under the key a
panel's open remembers its own by (`search.asked_key`). Simkl is never asked
there: its lookup needs a viewer's token. A KEY
ONLY MOVES UP the waterfall, once, everywhere: when a title learns a higher id,
`identity.settle` re-keys it, keeps the old key as an alias one hop long, and
`holders.repoint` moves every row holding the old key in the same transaction.
Two titles in the same id space never merge, and a lower id joins a higher title
only when exactly ONE title holds it — fifteen pairs of TMDB titles share a lower
id on the development copy. A READER THAT KEYS FROM ONE SERVICE'S IDS COMPLETES
THEM FROM THE STORE FIRST (`identity.complete`): the calendar's read, a history
play, a library read, a search row drawn as a card — or a show one service knows
only by its tvdb id splits from its tmdb self. A READER THAT ONLY ADDRESSES A
SERVICE — an outbound button — reads EVERY id the store knows, the services' own
included (`identity.every_id`; a calendar listing's `Record.known_ids`), and those
never enter the ids a card is keyed by: a service's own id is that service's to
state. ONE THING OUTRANKS THAT for the
watch tracker: a service's play or library entry lands on the tracked title
holding THAT SERVICE'S OWN id, when exactly one does
(`watch_history._tracked_by_own_id`), because a service re-points its shared ids
and never its own — Simkl moved The Great British Bake Off between TMDB's two
titles, and its episodes stopped reaching the season the tracker held. `tests/metadata/test_holders.py`
fails when the schema grows a table neither holder list classifies, and SQL
naming the store's tables lives in `app/metadata/` or names them through its
constants (`rows.TITLES`, `rows.EPISODES`, `identity.TABLE`).
READERS READ THE STORE, AND A FEATURE'S ROW KEEPS NO WORDS OF ITS OWN. A tracker
row's and a board row's name, network, year and counts are columns their own
SELECT adds (`app/metadata/labels.py`): each word from the first service in the
declared order that states it, the service HOLDING THE ROW'S SEASON naming it,
and a whole show's counts only from a description that vouches for its season
list. Nothing falls back to a copy — a frozen month's record is a snapshot, not a
copy, and keeps its words. A details panel answers from the store first
(`app/metadata/panel.py`), a tracker total and a board season row from
`season_metadata`. Words a feature's record carries SEED the store only where no
service states them (`descriptions.seed_words`), and give way to the service's.
EVERY SERVICE'S ROWS — TMDB'S TOO — STAY CURRENT BY BEING VIEWED: a details panel,
a tracker's or board's rows and a calendar month name what they drew to
`refresh.viewed`, which queues what is due (a description past its clock; a
missing one only where the page draws its words) behind the page, within the
trigger's budget, and a pass re-reads it past the response cache at each
service's per-minute allowance (`refresh.PER_MINUTE` — a budget per pass was a
budget per reload, and spent the pages' Trakt quota). A PUBLIC READ NEVER QUEUES.
A SPARSE LISTING IS DESCRIBED BY THE SAME QUEUE, NOT BY BEING VIEWED: a service
that declares `Capabilities.describes_sparse_listings` (TMDB — a discover entry
names no network, status or certification) has every `enriched = 0` row queued
on the heartbeat (`refresh.queue_listed`, at most a minute's allowance waiting),
because a calendar month queues only rows already described; an answer that
says nothing is recorded as a failure, so the backoff holds it and five give it
up (`LISTED_GIVE_UP_AFTER`), and the calendar's "unenriched" count adds what is
still owed (`refresh.listings_owed`). Simkl's sparse listings are its own drain's
(app/calendar/enrich.py), Trakt's arrive complete.
A HOLDER KEEPS ITS TITLE'S ROWS (`rows.sweep_unlisted` asks
`holders.held_keys`); only a service's ceiling takes them. A RE-KEY THAT FINDS A
ROW UNDER BOTH KEYS keeps the MORE COMPLETE one — one rank per table,
`holders.more_complete` — re-keyed, its gaps filled from the other, the other
logged in full and a board row sent to its board's removal history.

**The calendar cache stores the UNFILTERED window once per (endpoint, 7 days),
holding EVERY source's records as ROWS.** There is no stored window object: a
versioned compressed envelope with a `PAYLOAD_VERSION` used to live here, and it
went with the blob — `app/calendar/entries.py` now writes each source's airings
into `calendar_airings`, handing the records to the title store, which writes
the title and episode rows (`title_metadata`, `episode_metadata`) in the same
transaction — so a shape change is a migration rather than a version byte. The reasoning that version
carried still binds, and `app/calendar/cache.py` keeps it: a month spans five or
six windows, so nothing may leave the stored calendar half-converted or one
airing would draw merged in the span that had been refilled and split in the one
that had not, on the same page. Per-viewer filtering — and
per-viewer source selection — happen at READ time, so one viewer excluding a
genre or narrowing to one service does not poison what another sees from the
same rows (`app/calendar/cache.py` owns the envelope and the read path;
`app/calendar/resolve.py` owns which source answers each field). A VIEWER'S SOURCE
CHOICE DECIDES WHICH LISTINGS A CARD IS DRAWN FROM, NEVER WHAT THEIR FILTERS JUDGE:
`cache.narrow_for_viewer` — the one implementation of every filter, the network
lists included — judges a title as every service the INSTANCE admits describes it,
in the viewer's order, and draws the card from the viewer's own services alone;
otherwise narrowing to TMDB took Simkl's "talk-show" with Simkl's listing and a
filtered title came back. The envelope
records who was ASKED as well as who ANSWERED, which is what lets a source
switched on later refill a window that legitimately has none of its records,
and what keeps "partial" meaning a source that failed rather than one nobody
asked. ONE GAP IS NOT A FAILURE: a source still WORKING ITS ANSWER OUT
(`base.SourcePending`, raised at once with a `ready` awaitable) is stored asked
and not answered, remembered in process (`cache._pending`), and its share alone
is filled once `ready` finishes (`cache.fill_source`, started only AFTER the
window's own store, or the store would mark the answer missing again) — no
"couldn't be loaded" banner, no failure retry over the top of it, and the stats
box counts the weeks (`cache.pending_windows`). TMDB's Season Premieres are the
case: TMDB publishes no such list, so `app/providers/tmdb/premieres.py` derives
each week (shows on air less those on air in the four weeks before, each
confirmed by `/tv/{id}`) — a few hundred requests, tens of seconds cold, and
THEREFORE NEVER ON A REQUEST: one span at a time on a worker of its own, each
week's aired set kept in api_cache for the four weeks after it to reuse, a stale
derivation served while it is renewed behind the answer. A share page reads with
`allow_fetch=False` and so never asks, queues or derives. Two things are decided BEFORE storage rather than at read: the
instance-wide content floor and the instance-wide "include Simkl results"
switch, both operator decisions taken once for every viewer, and both also
applied at read so unticking is immediate over windows already stored. A
PER-VIEWER preference reaching a fill is the bug this shape exists to prevent
(`tests/calendar/test_resolve.py`'s `TwoViewersOneWindowTests`).
THE READ PATH HOLDS A MEMO, AND ITS KEY IS WHERE "FILTERED AT READ" IS NOW KEPT
OR BROKEN. `_assembled_span` in `app/calendar/cache.py` keeps the half of a span
that does not depend on the viewer's marks, in process, served while a rebuild
runs behind the reader and retired when its endpoint's rows are written — so a
span built for one viewer is served to any later request whose key matches. That
key is `_span_memo_key`'s, and its docstring says why each part is there: every
per-viewer narrowing by name, `repr(prefs)`, the operator's floor and
`instance_sources`, and `db.generation()`. Anything a viewer or `settings` decides
on the read path belongs in it, and a key missing one does not fail loudly — it
hands one reader somebody else's month. `tests/calendar/test_span_memo.py` holds
the list against the fields a read actually uses.

**One episode is ONE card, drawn on the day the viewer's first service gives
it.** Which listings are one card is `app/calendar/cache.py`'s: every service's
listing of one episode — `(title_key, season, episode)`, a film's title alone —
joins when it is in the same calendar month as EVERY listing already in the card,
or at most a week from it across a month's edge (`may_join`, `join_listings`, a
date-ordered scan, one listing per service per card); a show listing with no
full coordinate still folds into an episode only on its own day (`match_keys`).
That is viewer-independent and computed at READ — no key is stored. ONE LISTING
PER SERVICE IS ALSO WHAT STORAGE PROMISES: an airing a search or a tracker add
wrote (`from_search`) fills a gap in its service's feed and stands down the
moment that feed lists the same episode, at whatever instant — dropped at read
(`entries._in_fetch_order`), deleted by the fill (`store_span`), never written
over a listed one (`store_loose_airings`). The airing key includes the instant,
so a date-only searched row and the feed's timed one were two listings of one
service, and the scan drew two cards. Which day
the card is drawn on is the viewer's: the first service in their order describes
it, DATE AND TIME INCLUDED, and a date-only one borrows a time only from an
instant that falls on its date somewhere on earth (`app/calendar/resolve.py`'s
`_resolve_airing`, `falls_on`); the others' days are kept (`Record.listed_airings`)
and the card draws them where they differ (`Item.air_choices`). Because the scan's
units depend on every listing of an episode, A READ NEVER GROUPS PART OF ONE: a
span makes its windows current coverage-only (`ensure_window`) and reads its
airings once, with every other stored listing of each episode they name at any
date (`entries.read_span_whole` — the per-title query shape is the measured one;
a plain join picked a 39-second plan), and the search reads a title whole
(`entries.read_groups`). And there is ONE PLACEMENT FUNCTION, `cache.placed`
(resolution, render, both network lists): the month, a late day, a share page and
its card, the tracker's calendar import (all through `assemble_range`), the
search's "where it airs", its season picker and the card its jump lands on
(`calendar/search.py`'s `_drawn`) all ask it, and none works a day out from one
listing. `tests/calendar/test_placement.py` holds the readers to one answer — a
day against its month, two viewers on one span, a month's edge, the search.

**A viewer's filters are per MEDIUM, and one function turns a medium into
columns.** `app/calendar/vocab.py` owns it: `TV_FIELDS`/`MOVIE_FIELDS` say which
`user_prefs` column belongs to which kind of title, and `active_specs` is what
every read path calls to get the keywords the filter takes. There are four such
paths — the calendar, a day fragment, the search (which narrows PER ENDPOINT
inside its loop, because a search across every calendar spans both media) and a
share link — and none of them may name a column itself; four copies of "which
genres apply to a film" is four places to get it wrong. `filters_paused` is
checked in that same function, which is why it takes `honour_pause` with no
default: a viewer's own read passes True, and the share path passes False so a
paused session never publishes a wider calendar than the owner's links promised.
The stash stores nothing aside and restores nothing — the specs stay exactly
where they are, so turning filters back on cannot have lost one.

**The filters panel is markup the server built, and its field names are one
module's format.** A chip posts `chip:<pref column>:<token>` with a value of
`""`, `include` or `exclude` — one field per token, one flat string per field —
spelled by `vocab.field_name` and read back by `vocab.specs_from_form`, which
assembles specs by calling `filter.py`'s own `merge_token` rather than joining
strings. So NOTHING IN THE BROWSER KNOWS WHAT A SPEC IS: not the leading `-`,
not which dimensions fold case, not that networks are a list. The flat-string
shape is also deliberate — mutating requests are `application/json` only, and
the htmx json-encoding extensions this app is heading towards serialize a form
as flat string name/value pairs with no dependable array convention, so a
payload that is already flat needs no server change the day the submit handler
becomes two attributes. A dimension the payload does not mention is LEFT ALONE
and one mentioned with an empty value is CLEARED; that distinction is what makes
a per-tab save safe, and `tests/calendar/test_vocab.py` holds the round trip
between rendering a chip and reading it back.

**A source's calendar records may arrive incomplete, and `simkl_titles` is the
one place that completes them.** The schema is `app/db.py`'s like every other
table's, but what goes into it, the read-time overlay and the background drain
are all `app/calendar/enrich.py`'s; Simkl's CDN calendar carries no
genres, network, country, certification, runtime, status or overview, so its
Records land with `enriched=False` and are filled in afterwards. Nothing on the
fill path may do that lookup inline — a busy window names hundreds of titles
against a 10 GET/second ceiling — and a record the overlay cannot answer for is
SHOWN rather than filtered out, because a filter that hides what it has not
learned yet reads as "nothing matched" instead of "not known yet".

**A share link's preview picture renders the SAME view its page does.** Both
resolve it through `resolve_view` and read the month through `_read_month`, both
in `app/calendar/share_routes.py`, so a card can never advertise a count the page
does not show — there is no override, and a title the page shows is a title the
card counts. THE SAME PICTURES, TOO, and that half went unheld for a while
because this paragraph was written about counts and read as being about the
view: the page draws `Record.poster` — whatever service filled that calendar —
while artwork resolved TMDB-first regardless, so a link could advertise a
different picture of a different-looking show. Poster tiles are now keyed on
`(media, tmdb, SOURCE)` and resolved in the OWNER's own metadata order
(`posters.order_for`), which is the identical question the calendar asks
(`app/media/posters.py`). The
card is one of the TWO public paths that may make an outbound call (the details
panel, below, is the other): it warms poster ARTWORK, bounded to what the grid
draws and to a wall clock, for titles the calendar cache already holds. It never
fetches the CALENDAR (`allow_fetch=False`), so an empty cached month stays an
empty card.

**A page draws a picture's ADDRESS; a tile on disk is for compositing only, and
a title costs at most one lookup, ever.** `app/media/posters.py` owns both halves
and `app/media/artwork.py` the bookkeeping under them. The rankings board, its
details panel and the tracker's rows draw an origin address through the image
proxy (`posters.find_address`, `artwork.known` for a whole page) — the board's
row `<img>` asks `/api/rankings/poster` by board and key and is REDIRECTED there,
nothing written. Tiles (`ensure_poster`/`ensure_posters`) are fetched only by
what composites real bytes — the share card, and a board's export at export
time (`_warm_tiles` in `app/ranker/routes.py`). Which picture is ONE function,
`artwork.pick`: a season's own artwork (`season_posters`, filed from season
lists already read — only Trakt publishes it, measured) before its show's, each
in the viewer's order; a later "choose or upload a poster" feature stands in
front of it rather than beside it. WHAT STANDS BEHIND IT is the same order
whole (`artwork.ranked`): a page draws the first address with the rest as the
image proxy's fallbacks (`proxied_image(..., fallbacks=)`, wsrv.nl's `default=`,
each itself proxied so the redirect stays on the one host img-src admits),
because a recorded address can die at its host and nothing tells the server —
Trakt replaced Coyote vs. Acme's poster and the tracker drew an empty slot.
A title nothing has recorded a picture for
gets ONE lookup (`posters._look_up_address`, joined by any concurrent asker) and
its answer is recorded — and so is a miss (`artwork.record_miss`, a
`show_posters` row with source `missing`, an empty url and fail_count at the
cap, which every address reader already skips). A lookup that FAILS is not a
miss. Anything that decides "settled" for a cached render — the share card, an
export — treats a recorded miss as settled; the per-source negative marker
alone cannot say so, since it is written against an order's first source.
A TITLE WITH NO TMDB ID has its pictures in `title_posters`, under the title
store's key, with show_posters' columns and meanings, read through the same
`artwork.known` and `ranked` — so `pick` stays the one answer — and filed by the
store as it learns them (`artwork.file_sighting`, called from a description or a
listing). A DEAD PICTURE REPORTS ITSELF AND THE TITLE HEALS
(`app/metadata/pictures.py`): a signed-in surface draws its first address with
THIS APP'S report address as the image proxy's default (`pictures.drawn`; a
card through `CardPictures`), replacing the static fallback chain, and
`/api/posters/missed` — PUBLIC, because Firefox sends no cookie on the proxy's
redirect; a short-lived token signed with a key derived from ENCRYPTION_KEY
(`secrets_box.derived_key`, never the sealing key itself) decides what it acts
on; an image's request only — answers at once with the first address, in the
viewer's order and from what the registry holds NOW, that the token's chain of
hops has not tried (so two dead pictures never bounce a browser between them),
and confirms BEHIND the answer: a HEAD of the registry's address, never the
request's. Dead caps it (`artwork.cap`; the same address re-stated stays capped,
a new one starts fresh) and queues the WHOLE TITLE from every service at the
front of the refresh queue; transient notes when and nothing else. Deduped per
address, bounded per minute, one heal per title per few hours, and every step
logged at INFO by the title's key. A share page keeps the static chain, and an
instance with no public base URL has no absolute address to report to.

**There is one details panel, and one way of opening it.** `templates/_details.html`
draws what a title is, rendered through `app/details_panel.py` — which names the
context's keys once — for the calendar, the tracker, the share page and the
rankings board; no surface builds that markup in script, and `_details_modal.html`
is the one shell around it. Which service describes a title is one question on
all four, answered by `app/providers/detail_source.py` (in the providers package,
not the calendar, because four features ask it). ONE SEASON of a title has one
refinement, and it lives there too (`for_season`): a service whose answer holds
nothing of that season hands it to the next service the title names that does —
ids join two catalogues' SHOWS, and their seasons need not line up (Simkl's
anthology "Monster" carries the ids of Trakt's one-season "DAHMER – Monster").
It is decided after the first answer, so the common case costs nothing, and a
failure is never read as an empty season. The same handover applies to a whole
title (`describe_season` with no season): an answer that says nothing about the
title — an id the first service does not know — goes to the next service the
title names, which is also what keeps a share page drawing what an earlier
describer had cached. Because one renderer draws every
answer, `DetailPort`'s answer has ONE KEY SET whoever gives it, and
`tests/providers/test_protocol_conformance.py`'s
`EveryDetailPortAnswersTheSameKeysTests` asserts it — a Protocol checks methods,
not return shapes. VIDEOS ARE THE ONE FIELD POOLED ACROSS SERVICES, and the
exception is deliberate: a missing or wrong default video is recoverable only
when the alternatives are on screen and say who listed them, and one field from
one service showed nothing for a title whose only video was a teaser. Each
answer's `videos` is that service's own list; `detail_source.describe_panel` —
what every surface calls for a whole panel, a season tab calling
`describe_season` — asks every other service the ids name for its list
(`DetailPort.fetch_videos`) beside the description, and
`app/providers/videos.py`'s pure `pool` orders them. The share page passes it the
BOUND ids, so a service asked for its list is asked about a title the link
shows. The panel plays them in ONE iframe that every choice in its popover menu
targets by name, so a pick replaces what was playing with no request; the one
line of script closes the menu afterwards. ITS OUTBOUND BUTTONS AND A CARD'S ARE
ONE WALK OVER THE REGISTRY (`detail_source.service_links`), over every id the store
knows: a card draws in colour the services it was drawn from and greys (`.unlisted`)
one that only knows the title; a panel draws them all. The browser's half is one delegated listener in
`static/js/ui.js`: an element carrying `data-details-open` is an opener, and a
control INSIDE an opener acts for itself rather than also opening the panel.

**A public details request may cost an outbound call only for a title its link
already shows.** `_describe_shared_title` in `app/calendar/share_routes.py` owns
that promise and does the lookup itself, so there is no way to obtain permission
and then ask about something else: it resolves the view through the same
`resolve_view`, `_read_month` and `_visible_items` the page and the card use,
narrows the request's ids to those the visible airings carry — medium and season
included, since each is its own upstream call — BEFORE `detail_source.choose`, and
takes the picture and the outbound links from the month's airing, never from the
request. "Nothing describes this" and "this link does not show that" are one
refusal, so a stranger cannot probe what the owner's calendar contains. It is not
a flag a caller passes; a second public details route calls that function.

**A calendar day that arrives late is drawn whole by the server.** `calendar_day`
in `app/calendar/routes.py` renders it through the shell's own partials, from the
same placement the month made (the invariant above — a card the month draws on
another day is not drawn here), and: whether a
card is new comes from the SHELL's answer, filed once per load and named by a
token on the day's own address (`_remember_new_marks` / `_new_marks`), because
re-diffing from a fragment would read the shell's own commit and find nothing
new; its chip is redrawn out of band (`_day_chip.html`); and its add-to-library
buttons arrive in their state. The page keeps no marks of its own — a mark posts
and its answer redraws every card for that title — so a day landing while a mark
is in flight is drawn from whichever reached the database first, and the other's
answer redraws it. The add-to-library rule lives in one place per question:
`_library` (`app/calendar/routes.py`) for WHO gets the buttons, `LibraryView`
(`app/integrations/routes.py`) for what state each is in, `add_title` beside it
for the add itself.

**A rankings board row is a title OR one season of it, and its key says which.**
`app/ranker/item_keys.py` owns the one spelling and the one parser:
`show:tmdb:1234` is the whole show — exactly the shared ItemKey's flat form, so
every key already stored (a removal, a deleted tier's `items_json`, a backup, a
page open in another tab) still names the row it always did — and
`show:tmdb:1234:s2` is its season 2. A show and any of its seasons sit on one
board as separate rows (`tier_items.season`, unique over `IFNULL(season, -1)`),
so a lookup that leaves the season out does not fail: it names the wrong row, or
none. That is why `app/ranker/core.py` matches every key with the unique index's
own expression (`_key_match`), and why no module in `app/ranker/` may call the
shared `parse_item_key` or `item_key` — the shared parser reads
`show:tmdb:1234:s2` as the id `1234:s2` and quietly matches nothing.
`tests/ranker/test_item_keys.py` holds the second rule; `tests/ranker/test_seasons.py`
walks a show and its season through every operation that names a row. The
scripts on the board treat a key as an opaque string and must go on doing so.
WHAT A ROW IS CALLED AND COUNTS follows what it is. `app/ranker/seasons.py`'s
`row_label` is the one spelling of its name ("Show — Season 3", or the season's
own name, `tier_items.season_name`) for the row, its details heading, the
removal history and both exports (`exports.RankedTitle.label`, which the text
and the render cache's key read, and whose two halves the image captions on two
lines — the show's name, then `RankedTitle.subtitle`); and a row's counts
describe the row — a season
has no season count and its own episode total. `core._redefine_row` REWRITES
those (`_DEFINING_FACTS`) whenever a row's season changes, from the season list
already read (`seasons.season_facts`) or blank, never carrying the old
definition's numbers across.
A BOARD BACKUP says which it is in fields rather than a key: version 2
(`core.EXPORT_SCHEMA`) lists `season` and `season_name` on every title and
refuses a title that leaves either out, because in a file that can hold seasons
an absent season is lost identity, not "the whole show"; a version-1 file — every
title the whole show — restores exactly as it always did. Why each version reads
what it reads is written at `EXPORT_SCHEMA`; a change to what a title carries is
a new version there, with the older ones still read.

**Migrations are append-only, and migration 19 is the one recorded exception.**
`app/db.py` owns the rule; this is the note it points at. 19 shipped classifying an
unfrozen month's rows by comparing their month against the CLOCK, which turned a
whole month's roster into month-less viewer records and destroyed the only record
of which month they sat on. It was corrected in place rather than superseded
because a corrective migration could not have helped anyone: 19 ends by dropping
the old roster table, so by the time a 20 could run there was nothing left to fix.
Migration 20 exists only to give an instance that applied the original 19 the
held-rows table, so the drain pass finds it empty rather than absent. Do not treat
this as precedent — the reason it was allowed is that the alternative repaired
nothing, and that is a rare shape.

**Trakt does not honour the `days` bound it is given** — a 7-day window has come
back carrying entries two months past its end. Windows are trimmed before
storage; without the trim a month read shows the same airing several times.

**JavaScript is for what ONLY JavaScript can do, and the floor is MEASURED.**
The whole first-party surface is **712 code lines across 15 .js files, 27 lines
of inline `<script>` and NO inline `on*=` handlers** — the inline figure is
every non-blank line inside the two blocks `csp.INLINE_BLOCKS` names
(`_nav_head.html`'s 3, `error_lobby.html`'s 24), counted that way so the next
person reproduces it the same way — 694 when the audit ended,
702 after the rankings board's multi-select, 676 once every page extended one
base template and the per-page script loader went, and 703 when the
Content-Security-Policy moved the last ten handlers out of markup into listeners
in the files that own them, and 706 when the rankings board moved onto the shared
title search and a dragged result began inserting its row under the key the
server answers with, and 708 when a dialog the server opens stopped being opened
again by every later swap once it had been closed (static/js/ui.js takes
`data-open-modal` off as it acts on it), and 712 when choosing a video from the
details panel's menu began closing the menu (static/js/ui.js) — counted as the non-blank, non-comment
lines of those 15 files, the vendored `htmx-json-enc.js` not among them; from 4,024 lines when the audit began, and from
1,213 / 26 / 30 at its last conversion. The WATCH TRACKER has no script of its
own at all. Those three numbers are the point of this paragraph: they are what
somebody adding a file is adding to, and a number that has drifted is a signal
to go and ask why.

`app/assets.py` owns which scripts exist, in two lists — `PUBLIC_SCRIPTS` and
`APP_SCRIPTS`, the second beginning with the first — so adding one is a visible
act (`url()` fails a render reaching for a script neither list tracks).
`tests/kernel/test_script_reasons.py` owns the CLAIM: every .js file and every
inline `<script>` must OPEN with prose saying what it could not have been, naming
at least two of markup, a stylesheet rule, a template and a route. A label is not
a reason and the test says so.

**Every page extends `templates/_base.html`, and which script list it loads is
tied to whether it boosts.** A boosted link keeps the `<head>` the session
started with, so a page reached by a swap has only the scripts its session's
first page loaded. Every signed-in page loads `APP_SCRIPTS`, so a swap between
them needs nothing fetched; the pages a stranger lands on set `public = true`,
which loads the short `PUBLIC_SCRIPTS` AND turns boosting off on `<body>` — both
live on things a swap never replaces, so a session that started public reaches
the app only by a full load. Making a page public without the other half, or
boosting out of one, lands a page INERT with nothing on screen to say why.
`tests/kernel/test_page_head.py`'s `PublicPageTests` names the public pages.

BEFORE WRITING A LINE OF IT, ANSWER WHY MARKUP, A STYLESHEET RULE, A JINJA
TEMPLATE OR A ROUTE CANNOT DO THIS. The question is not rhetorical — every item
below was believed to be script until somebody asked. A season picker is a radio
group and three CSS rules. How wide a day is and whether hiding empties it are
`:has()` and `:nth-child(k of …)`. Every overlay in the app is a modal
`<dialog closedby="any">` opened by a `command="show-modal"` invoker, which is
where its Escape, its backdrop click, its focus trap and its focus return come
from. Both header menus are popovers, which is where light dismiss comes from.
Where a menu OPENS is `position-anchor: auto` plus `anchor()`. Flipping a card
between the services that described it is a radio per service, each paired with
its own value, and `input:checked + .flip-value`. Marking a show not-watching is
a button posting itself, answered with every card for that title redrawn out of
band. Adding a title a search found is the RESULT posting itself, answered with the page
and that same search redrawn from the cache its own request filled — so a second
title from one search costs no second search, and whether a result is already
held is drawn rather than swept on afterwards — and a held one refusing the
click is `pointer-events: none`, not an event filter on its trigger. Adding
several SEASONS of it at once is a checklist UNDER the row — the row is the
`<summary>` of its own `<details>` and asks for it `click once`, one checkbox per
season (the board's leading with the whole show), one json-enc post naming the
result by reference — the summary's sibling and never inside it, because
attribute inheritance would hand the row's request to the form.
Moving a title between tiers is one button per destination ON THE ROW, posting to the
same route a bulk move already used — a menu shared by the whole board is what
made "which row opened it" a question the browser had to answer. Setting which
season a show row is has the same shape beside it: a button ON THE ROW saying
what the row is ("All", "S3"), with its own empty popover that the button asks
the season list into `click once`, and one button per season posting itself, a
season the board already holds being `disabled` rather than refused afterwards.
The request is on the button and never on an ancestor of the list's form,
because htmx's attribute inheritance is on (static/js/boost.js) and a trigger on
an ancestor becomes the trigger of the form inside it.
A saved view preference is `hx-post` plus `hx-json-enc` on the control
itself, answered with the page's own `#pageMeta` out of band — which is how a
class reaches <body>, the one element a swap can never replace. A value edited
where it is drawn — a tracker row's emoji code — is a FIELD DRESSED AS TEXT,
saved on `change` and answered out of band to EVERY copy of it by a digest
selector: nothing swaps text for a field, and `change` alone is what makes
Enter save once, a click away save once, and a click in and out send nothing
(measured in both engines; templates/_distrakt_rows.html's `emoji`). And a title
scrolling because it is too wide for its cell is CONTAINER QUERY UNITS: `100cqw`
is the cell's own width and `100%` in a transform is the title's, so the
difference IS the overflow and nothing has to measure it — which is what emptied
the watch tracker of script entirely.

WHAT GENUINELY QUALIFIES, and this list is what actually survived the audit
rather than what was hoped for:
  - a stream of pointer events — a drag, and the preview pan;
  - measuring what the browser has actually LAID OUT — and the only survivor is
    the header's real height, which wraps when narrow;
  - reading a local file into a request body, because the shape guard above
    admits only `application/json`;
  - the clipboard;
  - input with no declarative form: a modifier-click range, a ten-key sequence,
    a run of taps, and a button that has to write a value into another field;
  - putting a `<dialog>` into the TOP layer when the server delivered it with
    nobody pressing anything — the `open` attribute makes a NON-modal one and
    there is no attribute for the other kind;
  - a window opened INSIDE a click and aimed afterwards (the Plex popup);
  - closing a popover when a LINK inside it is followed — a link cannot close
    one, and the button that can loads a frame only through a form, which
    `form-action 'self'` refuses for YouTube (the details panel's video menu);
  - a POST whose answer is a file to save;
  - a payload that is not a form's fields — the board's nested ordered tree;
  - telling a page it has ARRIVED after a boosted navigation, which fires no
    DOMContentLoaded — `page-arrived`, announced by static/js/boost.js;
  - switching a button on once htmx has wired its form, because before then
    the form would be sent the browser's own way (the sign-in form);
  - reading something the server is deliberately not told: the device's timezone,
    and whether this browser has found the tracker;
  - noticing that an image did not load — no selector matches a broken image,
    and the server cannot know before a browser asks (the ranker's posters).

WHERE A SURVIVOR LIVES IS DECIDED TOO: in a FILE, never in markup. The policy
runs no event-handler attribute, so a control that needs script carries a
`data-` attribute and the file that owns the function listens for it on
`document` — a listener bound to the element would be lost to the next swap.
`static/js/ui.js`'s details-panel opener is the pattern. The one listener that
cannot rely on being there in time is an `error` one: an attribute was attached
as the element was parsed, a deferred file is not, so ranker/rows.js also sweeps
for images that failed before it ran — MEASURED, Chromium failed every forced
404 on the board before the file had loaded.

EVERYTHING ELSE IS THE SERVER'S. The reason is not taste: markup and stylesheet
rules are right the moment anything changes them and cannot drift from the data,
while a second copy of a fact in the browser is a copy that goes stale silently
— three of this app's own copies did, and none of them looked wrong from any one
page. The audit's own largest removals were all of that shape: a month's per-show
counts held in the page so the tiles could be moved by arithmetic, a serialized
copy of every alternative value on every card, and a map of marks the page had
made but not yet saved.

A BROWSER CLAIM IS NOT A BROWSER MEASUREMENT. Two of this audit's findings were
features that ARE supported and silently did nothing as written: a popover's
implicit anchor does not resolve unless `position-anchor: auto` is spelled out,
and `closedby="any"` dismisses nothing on a dialog that fills the viewport,
because there is no outside left to press. Both read as unsupported. Drive it in
both browsers before believing either answer.

THE SAME GOES FOR htmx's REQUEST ORDERING, and a search box is where it bites.
Two measured facts about the vendored htmx decide how a box that lists as you
type and asks the services on Enter must be wired: a DEBOUNCED trigger's timer
cannot be cancelled by anything else, so a debounced typed request fires after
an immediate Enter and lands over its answer; and `hx-sync`'s `replace` loses
track of the request that did the replacing once the aborted one settles, so the
NEXT request aborts nothing and stale answers arrive late — and on the calendar
it aborted the services' request outright, 64 of 64 type-then-Enter trials in
each engine. What held in both is no debounce and `queue last` on the results
target, shared by the box, the form and every control inside the results that
asks for them again. THAT RULE HAS ONE HOME, `templates/_title_search.html`:
its `box` macro is every title search's box (the calendar's, the tracker's two
and the rankings board's), its `row` macro every result row, with the surface filling the ACTION
SLOT (the element and what pressing it does), and its `queue`/`sync` are the one
spelling of the policy for anything else that re-asks. A new search box is a
call to that macro, not a copy of its attributes. The box brings its CLEAR
BUTTON with it — a `type="reset"`, which empties the box with no script, asking
the typed route for an empty query in the same queue — and that is why a box
that must survive a whole-page answer (the rankings board's) is `hx-preserve`d
rather than redrawn with a `value`: a drawn value is also what a reset restores.
Its `widen` macro is what every box draws under a services' answer — one
button per CATALOGUE, "Also search <service>" until it is added and "More
from <service>" after, and the default catalogue not being configured — each
control a GET of the surface's live route in the same queue.

**ONE SEASON PICKER, AND ONE RULE FOR WHAT IT OFFERS.** `templates/_season_checklist.html`
is the checklist the tracker's add, the rankings board's add and the calendar's jump
all draw — a surface decides only what submitting it does (always a json-enc post;
the calendar's to `/calendar/jump` WRITES the season's airing and is answered with
`HX-Redirect` to the card) and whether a season is ticked or ONE PRESS
(`one_press`: the calendar's, where a season it draws is a link to its card and
any other a submit button carrying its own `season:<n>`, since only one place can
be gone to) — and `app/providers/season_choices.py`
decides what it offers, greys, refuses and suggests, with each surface's own words for
"already here". Ticked seasons post as one flat field each, `season:<n>`
(`season_choices.ticked`). A tracker add of several seasons asks the re-watch question
per season, stopping at the first that needs it and re-posting it and the rest with
`answering` naming which season the answer is about. The calendar's catalogue half
lists titles like every box — the calendar's own medium, both only across every
calendar — and DESCRIBES NOTHING. On the calendar, in the tracker's show box and
on the rankings board the picker UNFOLDS UNDER ITS ROW: the row is the `<summary>` of its own
`<details>`, asking `click once` for the picker into the `.search-pick` slot
inside it, so a new search takes it down with the list and no slot beside the
list needs resetting out of band. The calendar's picker is where a title is
described, and the viewer's filters refuse a season with the reason — EVERY
filter the calendar applies, through `calendar/cache.py`'s `narrow_for_viewer`,
the one implementation `visible_records` also calls, and BEFORE a stored season
is offered as a link to its day. A season the calendar ALREADY HOLDS is judged
by the record it holds, through `visible_records` itself with the viewer's source
choice (`calendar/search.py`'s `_drawn`), never by the describing service's
answer: the two can disagree on a filter (a stored record with no country under
a country filter), and a link judged by the description opens a day that draws
nothing. The jump names the card it lands on whether or not it wrote one.

**WHO A SEARCH ASKS IS THE REGISTRY'S, AND THE PAGE NAMES IT BY REFERENCE.**
`providers.for_search` owns it: the catalogue `searched_by_default` (TMDB — a
`Capabilities` flag each package declares, never a name in the registry), plus
any other searchable service the VIEWER has linked (`is_configured` on their own
Settings) when the request ADDS it. A default with no credential is named for
the box to say so; NOTHING IS SEARCHED IN ITS PLACE, and a name the viewer may
not add raises `UnknownSearchService` rather than being read as the default.
`title_search.SearchRef` is the one spelling of which search a page drew —
`also` (catalogue keys: `trakt`, `simkl`, `simkl-anime`) and `pages` (a count
per catalogue, `tmdb:2`), never results — and every result
row and widening control sends it back, so `resolve_hit` re-reads EXACTLY the
list the row was numbered in: same services, same pages, from the cache. AN
ADDED SERVICE'S TITLES COME BELOW THE DEFAULT'S on every box — the merge's own
order, which the calendar keeps across both media (`calendar/search.py`'s
`_default_first`) — because no common relevance exists to rank across services
by, and rows shuffled above the ones being read would be confusing. EVERY
CATALOGUE ANSWERS `base.SEARCH_PAGE_SIZE` A PAGE (TMDB's size; Trakt and Simkl
are asked for it), and a port may hold more than one catalogue
(`SearchPort.catalogues` — Simkl's TV and its anime, which one query does not
reach together).
`title_search.search` is the one search every box makes. THE LOOKUP BY SHARED ID
IS A DIFFERENT QUESTION with its own function, `providers.for_shared_id_lookup`
— every service that answers an exact lookup, whichever box found the title —
because that lookup is how a title TMDB found learns its Trakt and Simkl ids;
the title store asks it (`metadata.ensure`) for an arrival, a board add and the
calendar's card completion, never the search list. And a
service WRITES an airing only onto a calendar it publishes (`_publishes` in
`app/calendar/search.py`, asked by `fill_one_gap` and the stored-season
completion), so a catalogue with no calendar describes a search result and the
jump opens the month.

## Conventions for code you add or change

**No references to planning documents in shipped code** — not in comments,
docstrings, log messages, test names, or commit messages. No "Step 3", no "phase
2", no "chat B owns this". Planning files are gitignored and get deleted, and a
step number is a pointer to a reason: when the pointer dies the reason is gone.
Write the reason inline. Pointing at durable things — module paths, function
names, table columns, RFCs, an external API's behaviour — is encouraged.

**A leading underscore means non-public, and it has to be true.** Production
code outside the defining module does not use `_name`, and there is no exception
for siblings in the same package. Tests are not outsiders — a test may reach a
private helper directly. When something genuinely is package-internal, put it in
a module whose NAME carries the underscore and give the names inside it ordinary
ones, the way the standard library does (`_collections_abc`,
`importlib._bootstrap`).

**Call across a package through the module object** — `transport.send(...)`, not
`from .transport import send`. A name bound at import time is a second reference
that patching the owning module cannot reach, which turns a test double into a
real network call that passes for the wrong reason.

**An invariant every call site must uphold cannot live in one call site's
docstring.** A rule that holds app-wide needs ONE implementation everything
calls, with the reason written where that implementation lives. A docstring
claiming "every route does X", written inside one of several near-copies, is a
claim nobody can check while the copies drift out from under it. This matters
most for security rules, where the drift is silent.

**One source of truth per fact; one reason to change per unit.** The test for
duplication is shared MEANING, not similar-looking text — two functions that
resemble each other but change for different reasons are not duplication, and
merging them couples things that wanted to move apart. I/O at the edges, pure
transformation in the middle.

**Explain WHY in prose.** The house style is comment-dense with the reasoning
written out, and that part is worth matching.

## Testing

```
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # once: pytest + pytest-xdist
.venv/Scripts/python.exe -m pytest -q          # full suite, ~2.5 minutes across every CPU
.venv/Scripts/python.exe -m pytest -q tests/calendar/test_cache.py
.venv/Scripts/python.exe -m pytest -q -n 0 tests/...   # one process, for a debugger or print()
```

**THE SUITE RUNS IN PARALLEL BY DEFAULT** (`-n auto` in pytest.ini, one
pytest-xdist worker per CPU), and that is safe only because every worker is its
own process with its OWN data directory — tests/conftest.py makes one per worker
rather than letting them inherit the controller's, which is what eight workers
sharing one settings.json looked like (141 failures). A test that writes
somewhere other than the data directory or its own database is a test that will
race its neighbours.

**WHICH RUN A COMMIT NEEDS DEPENDS ON WHERE IT LANDS.** A commit on a BRANCH
needs the targeted files — the ones covering what changed, plus whatever the
change reaches into. A commit on MAIN, and any merge back into main, needs the
FULL SUITE. The full run is ~2.5 minutes (2m19s across eight workers, measured
2026-09-30; it was 10m47s in one process before the suite went parallel and the
trust store stopped being re-read — see app/http_pool.py), which is cheap once
at the point work becomes everybody's; a branch is where the iterating happens,
so the targeted run is what keeps it honest without making a commit something to
put off.

Run targeted files while iterating.
`tests/` mirrors `app/`, so where a test lives says which part it covers.
`tests/encryption/` is named for a concern instead of a package because the
at-rest key path it covers end to end genuinely spans two: the kernel's
`app/secrets_box.py` and auth's encryption modules.
`tests/conftest.py` owns the data directory and an autouse guard that fails any
test reaching the network; `tests/support.py` owns the database, the client and
the base classes. A new test file should need no setup of its own.
