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
exceptions — config.py reaching the provider registry, and reaching auth's
`encryption_flow` to refuse writing a secret while the at-rest key is unhealthy.
Both are function-local, both are declared DEFERRED, and hoisting either to
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
then has one answer per module. THERE ARE NOW TWO IMPLEMENTATIONS OF THIS RULE,
one per provider transport (`app/providers/trakt/transport.py` and
`app/providers/simkl/transport.py`), and they are two because the services
differ in what a request carries, not because the rule does. A change to what
`private` MEANS belongs in both; `tests/providers/test_protocol_conformance.py`
is what holds the two packages to one shape.

**The calendar cache stores the UNFILTERED window once per (endpoint, 7 days),
holding EVERY source's records as ROWS.** There is no stored window object: a
versioned compressed envelope with a `PAYLOAD_VERSION` used to live here, and it
went with the blob — `app/calendar/entries.py` now writes each source's records
into `calendar_titles`, `calendar_airings` and `calendar_episodes`, so a shape
change is a migration rather than a version byte. The reasoning that version
carried still binds, and `app/calendar/cache.py` keeps it: a month spans five or
six windows, so nothing may leave the stored calendar half-converted or one
airing would draw merged in the span that had been refilled and split in the one
that had not, on the same page. Per-viewer filtering — and
per-viewer source selection — happen at READ time, so one viewer excluding a
genre or narrowing to one service does not poison what another sees from the
same rows (`app/calendar/cache.py` owns the envelope and the read path;
`app/calendar/resolve.py` owns which source answers each field). The envelope
records who was ASKED as well as who ANSWERED, which is what lets a source
switched on later refill a window that legitimately has none of its records,
and what keeps "partial" meaning a source that failed rather than one nobody
asked. Two things are decided BEFORE storage rather than at read: the
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
`(media, tmdb, SOURCE)` and resolved in the OWNER's own `field_order("poster")`,
which is the identical question the calendar asks (`app/media/posters.py`). The
card is one of the TWO public paths that may make an outbound call (the details
panel, below, is the other): it warms poster ARTWORK, bounded to what the grid
draws and to a wall clock, for titles the calendar cache already holds. It never
fetches the CALENDAR (`allow_fetch=False`), so an empty cached month stays an
empty card.

**There is one details panel, and one way of opening it.** `templates/_details.html`
draws what a title is, rendered through `app/details_panel.py` — which names the
context's keys once — for the calendar, the tracker, the share page and the
rankings board; no surface builds that markup in script, and `_details_modal.html`
is the one shell around it. Which service describes a title is one question on
all four, answered by `app/providers/detail_source.py` (in the providers package,
not the calendar, because four features ask it). Because one renderer draws every
answer, `DetailPort`'s answer has ONE KEY SET whoever gives it, and
`tests/providers/test_protocol_conformance.py`'s
`EveryDetailPortAnswersTheSameKeysTests` asserts it — a Protocol checks methods,
not return shapes. The browser's half is one delegated listener in
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
in `app/calendar/routes.py` renders it through the shell's own partials: whether a
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
The whole first-party surface is **702 code lines across 15 .js files, 25 lines
of inline `<script>` and 10 inline `on*=` handlers** — 694 when the audit ended
(the rankings board's multi-select then came in three lines under it, and keeping
a page of its pool from drawing a title twice added eleven), from 4,024
lines when the audit began, and from 1,213 / 26 / 30 at
its last conversion. The WATCH TRACKER now loads no script of its own at all. Those three numbers are the point of this paragraph: they
are what somebody adding a file is adding to, and a number that has drifted is a
signal to go and ask why.

`app/assets.py`'s `PAGE_SCRIPTS` owns which scripts exist, so adding one is a
visible act (`tests/kernel/test_page_head.py` fails a render reaching for a
script the list does not track). `tests/kernel/test_script_reasons.py` owns the
CLAIM: every .js file and every inline `<script>` must OPEN with prose saying
what it could not have been, naming at least two of markup, a stylesheet rule, a
template and a route. A label is not a reason and the test says so.

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
held is drawn rather than swept on afterwards.
Moving a title between tiers is one button per destination ON THE ROW, posting to the
same route a bulk move already used — a menu shared by the whole board is what
made "which row opened it" a question the browser had to answer.
A saved view preference is `hx-post` plus `hx-json-enc` on the control
itself, answered with the page's own `#pageMeta` out of band — which is how a
class reaches <body>, the one element a swap can never replace. And a title
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
  - a POST whose answer is a file to save;
  - a payload that is not a form's fields — the board's nested ordered tree;
  - loading a page's own scripts after a boosted navigation, which is the one
    thing a body swap cannot do for itself;
  - reading something the server is deliberately not told: the device's timezone,
    and whether this browser has found the tracker.

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
.venv/Scripts/python.exe -m pytest -q          # full suite, ~8.5 minutes
.venv/Scripts/python.exe -m pytest -q tests/calendar/test_cache.py
```

**WHICH RUN A COMMIT NEEDS DEPENDS ON WHERE IT LANDS.** A commit on a BRANCH
needs the targeted files — the ones covering what changed, plus whatever the
change reaches into. A commit on MAIN, and any merge back into main, needs the
FULL SUITE. The full run is ~8.5 minutes (8m36s, measured 2026-09-23), which is
cheap once at the point work becomes everybody's and expensive at every step of
getting there; a branch is
where the iterating happens, so the targeted run is what keeps it honest without
making a commit something to put off.

Run targeted files while iterating.
`tests/` mirrors `app/`, so where a test lives says which part it covers.
`tests/encryption/` is named for a concern instead of a package because the
at-rest key path it covers end to end genuinely spans two: the kernel's
`app/secrets_box.py` and auth's encryption modules.
`tests/conftest.py` owns the data directory and an autouse guard that fails any
test reaching the network; `tests/support.py` owns the database, the client and
the base classes. A new test file should need no setup of its own.
