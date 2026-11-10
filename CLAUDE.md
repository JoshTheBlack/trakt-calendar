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

**A write transaction's statement count does not grow with what it writes.**
`db.transaction` holds SQLite's one write lock for as long as its body runs, and
the driver lets go of the GIL around every statement step and must win it back —
up to a switch interval (5 ms) each time while the event loop is busy. MEASURED on
a copy of dev: a calendar fill's store issued ~3,300 statements and held the lock
5.3 s under one month read, and the next writer gave up at `db.BUSY_TIMEOUT_MS`.
So a writer that writes many rows writes them SET-BASED — rows spelled as a JSON
array before BEGIN, one `INSERT … SELECT … FROM json_each(?)` per table
(`rows.json_rows`), a read inside answering in ONE aggregated row — and a read
that needs no lock (finding rows by an id no index reaches, deciding what a queue
owes) is made before the transaction. The title store's batch is
`identity.settle_many` (three reads, one write, a re-key's own repointing;
`settle` is a batch of one), fed by `metadata.listings`, `plan_records` and
`write_records`; a fill DECIDES ITS TITLES AND ITS EPISODE ROWS BEFORE THE LOCK —
`db.transaction(fn, prepare=...)` runs the reads and the deciding
(`metadata.plan_records` → `identity.SettlePlan`, then `episodes.plan_fill`) on
the same connection first, and again under the lock only if another connection
committed meanwhile (`PRAGMA data_version`), so the lock is held for the writes
alone. AND THE WRITES ARE ONLY WHAT WOULD CHANGE. Before the lock each writer
asks — with the very SET templates its upsert is spelled from, so the two cannot
disagree (`groups.changing`) — whether writing would change the stored row: a
title group once per ENTRY, its last listing's words (`rows.plan_listing_writes`;
an entry's listings in one answer carry the show's words), a spelling in
`service_ids`, a stub, a picture (`artwork.unseen`, an address seen again
re-stamped at most weekly), an episode row by its instants (`episodes.Row.
differs`, the moment an unchanged feed listing was stored set aside) — and sends
only those, so A REFILL THAT CHANGED NOTHING WRITES ITS COVERAGE ROW AND NOTHING
ELSE, and decides even that OUTSIDE THE WRITER QUEUE, on a plain read: a window
whose plan writes nothing (`Planned.empty`, `Fill.empty`) writes its coverage row
alone with `db.execute`, a turn of its own, and one that changes decides again in
its turn as every `prepare` does (`entries._store_window`) — `Book.spell`'s None
is "nothing changed", never "not spelled yet" (MEASURED on a copy of dev: a week
of All Episodes stored back held the lock 0.77–1.31 s; an unchanged refill, a
whole Simkl month's included, now holds it 0.000–0.001 s alone or under three
month reads; and through a restart's refills of weeks that had changed little, a
one-row write waited p95 0.25 s where deciding in the turn kept it 4.8 s;
`tests/calendar/test_write_statements.py`'s `ARefillWritesOnlyWhatChangedTests`).
A decision gone stale before its coverage row is written costs nothing the
queue's order did not already: a write landing in between is one the rows take
either way, and the coverage row says only that the service replied. A store's one DEBUG line names where its
hold went (`perftrace.Laps`: a prepare made again, merges, settles, listings,
episodes, coverage). A batch that re-keys a title plans the episode rows of the
titles it reshapes inside, once the merge is written — those titles' share of
the span, read in one aggregated row — and every other title's before
(`episodes.plan_fill`'s `titles` / `excluding`); the merge itself is rare —
a fill moves no entry (the title store's paragraph), so a fill's merges are
re-keys alone, thirteen in the development server's first 35 minutes on the
store — written row by row (`app/metadata/merge.py`), and outside the fixed
count as a re-key's repointing always was. Filing the whole span inside held the lock
2.98 s under a month's read where the store before held it 1.21 s; this holds it
1.31 s (MEASURED, a week of All Episodes re-keying five titles, on a copy of
dev). A payload-driven join says CROSS JOIN, and an UPDATE … FROM by rowid takes
the competing index away (`+source`): the planner otherwise walks the table with
the payload inside it. THE SAME GOES FOR A LOOP OF TRANSACTIONS: work fanned out
at once that writes as each piece lands takes a worker thread per write, and the
Simkl drain's 300 lookups doing that took all forty and left a month's page
waiting 29.7 s — so a pass looks up first and writes its batch once
(`calendar/enrich.py`'s `_record`; the title store's refresh pass,
`metadata/refresh.py`'s `drain`, files every answer through
`descriptions.store_many` — `plan` decides each answer as if the ones before it had
been written, `apply` writes them in a fixed count — and takes its entries off the
queue in one more). AND A BATCH IS WRITTEN IN BOUNDED TURNS, each a transaction
of that fixed count, planned in its own turn (`descriptions.turns`:
`TURN_ANSWERS` / `TURN_ROWS`; `rows.ENRICHMENT_TURN`, whose settle is planned
before its lock from the ids its update leaves, `rows._settlings_sql`): one turn
for a whole pass held the lock 1.3–1.6 s on the development server's restart
(316 and 257 answers) and 1.3–1.9 s for the Simkl drain's batch, and every other
write waited behind it. A turn's plan is made just before it and used only
while what it read reads the same (below) — a write between could leave it
filing rows under a key a re-key has retired. The commit
that crosses SQLite's 1,000-page WAL mark also runs its checkpoint, 0.07–0.33 s
(MEASURED), inside whichever writer's turn crosses it. `tests/support.py`'s `statements_per_transaction` holds
each writer to the same count for ten rows as for a hundred
(tests/calendar/test_write_statements.py, tests/metadata/test_write_statements.py,
tests/calendar/test_enrich.py's `APassWritesItsBatchOnceTests`), the retention
sweep included — which decides before its lock what is due, each read one
aggregated row, and takes no lock at all when nothing is (`entries.sweep`) —
and the response cache's sweep the same, deleting what is due in BOUNDED TURNS
of one statement each (`app/cache.py`'s `sweep`, SWEEP_TURN_ROWS /
SWEEP_TURN_BYTES; it scanned its whole table inside the lock every minute, up
to 3.54 s on dev at a start, and halving the table was one 2 s hold), and
`db.transaction` logs a WARNING naming its body whenever the lock was held
over `db.LOCK_HELD_WARN_SECONDS` (1 s), so the next long hold names itself.
WRITES TAKE TURNS: every `db.transaction`, `db.execute`, `db.executemany` and
`db.execute_gathered` waits in ONE in-process queue per event loop (`db._writer_queue`) and only then
prepares, takes the lock and writes. SQLite does not queue writers — a waiter
sleeps and retries, and a later writer can take the lock first — so on the
development server's first minute after a restart a refresh pass lost to three
week stores in a row and failed at `db.BUSY_TIMEOUT_MS`. A write that goes round
the helpers (a body handed to `db.run`) skips the queue and keeps that failure —
and commits while a store decides its rows in its turn, so the store decides
them again under the lock (the response cache's writes did: 0.17–0.70 s of lock
per store, MEASURED on a harness copy) — so a write belongs in `transaction` or
`execute`, `app/cache.py`'s included. A `prepare` RUNS IN THE TURN, so work that
reads nothing is done before it: the response cache compresses a value on a
worker thread before its turn (`cache.set` — compressing in its `prepare`, a
restart's slow cache writes held a viewer's mark 2.2 s, MEASURED with live
services). AND ANSWERS LANDING TOGETHER ARE WRITTEN TOGETHER: every answer that
arrives while the cache's write waits rides in that turn, one statement of up to
`cache.WRITE_TURN_ROWS` rows, its values taken as the turn starts
(`db.execute_gathered`), so the cache never has more than one turn in the queue —
as a turn each, a restart's first two minutes wrote 2,690 answers (mostly TMDB's
Season Premieres derivation and lookups), their turns adding up to 19.7 s against
the stores' 6.4 s, and an unmark waited 3.3 s. A STORE'S TURN IS ONE WINDOW'S
(`entries.store_span`): every other write waits behind a turn's deciding, and a
Simkl month decided in one turn kept a one-row write waiting 3.2–3.4 s. AND
EVERY WRITE SAYS HOW LONG IT WAITED FOR ITS TURN (`db._in_turn`): one `db.write`
line naming its body or its statement (never its parameters) with `queued` and
`turn`, DEBUG until slow, both put into its request's breakdown (`db.queue`,
`db.turn`) — a filter save on the development server's restart spent ten
seconds in the queue with nothing logged between two of its spans. A TURN IS
DECIDED BEFORE IT COMES, AND THE DECISION IS USED WHILE WHAT IT READ READS THE
SAME: a writer whose deciding is expensive hands its `prepare` to `db.decide`
(run on a worker thread outside the queue, every read it makes kept) and the
decision to `transaction(decided=)`, whose turn makes those reads again and uses
the decision only if every one returns the same rows — a decision is a function
of its inputs and its reads, so the same reads decide the same writes — and
otherwise gives the turn up, decides again outside the queue
(`db.REDECIDE_OUTSIDE` times) and only then decides in its turn. A guard that
asked only whether anything had been written never held (a restart writes every
second); this one is the rows. The calendar store (`entries._store_window`), the
description store's turns and the enrichment writer's turns use it. AND
CALENDAR STORES DECIDE ONE AT A TIME (`entries._deciding`, one lock per event
loop, never the writer queue): deciding is Python on threads sharing one
interpreter lock, so side by side was never faster, and stores of one burst
rewrite each other's rows — side by side, 11 of 15 decisions were stale by their
turn and three decided four times, the last in their turn. MEASURED on a copy
of dev's restart burst: the longest background turn 5.2–9.5 s → 1.4–1.55 s, a
viewer's write's wait 5–7.4 s → 0.04–0.69 s, a one-row write's p95 4.1–7.6 s →
0.46–0.51 s. A window's span is read in one aggregated row
(`episodes.read_span`), because a read stepping thousands of rows wins the
interpreter lock back at every step.

**The event loop runs no pass over a fill's records or a month's cards.** The
loop serves every request, so Python that walks thousands of things runs on a
worker thread (`anyio.to_thread.run_sync`, or `db.run` where it reads), and the
loop only awaits it: Simkl's file parsed, de-duped and made Records
(app/providers/simkl/calendar.py — a real October was 345 ms); a fill's floor,
trim, id bridge and de-dupe (`cache._kept`, `enrich.match_ids`), its stored
enrichment (`enrich.give_stored_enrichment`), its filing by window and digests
(`entries._file_by_window`), its listings and completed ids (`metadata.listings`,
`complete_records`), its picture sightings; a read's day trim and order
(`query.in_day_order`), its grouping, marks and counts (`cache._assembled`), a
page's filed month (`routes._filed_cards`) and its viewed titles. WHAT STAYS ON
THE LOOP is every in-process structure those feed — `_storing` /
`_stored_from`, `_note_changed`, `cache._pending`, the refill latches,
`routes._pages` and `_new_answers`, `refresh.viewed`'s queueing, the pacers —
so each is touched by one thread only, and the work moved touches none of them:
it is handed its own records and returns values. MEASURED, the restart burst:
the longest loop block 1.85–3.46 s → 0.30–0.34 s; what is left lines up with
full garbage collections (up to ~0.3 s) and the interpreter lock shared with
the threads. AND THE LOOP WRITES NO LOG LINE AND NO PICTURE FILE: a write or a
flush lets go of the interpreter lock and waits up to a switch interval to win
it back beside a busy thread, so every line goes onto a bounded queue and a
listener thread writes what is waiting a batch at a time, one write and one
flush per handler (`app/logging_setup.py`), and a picture's reads and writes run
on a worker thread (`app/media/user_images.py`). SO A FILE IS READ ON ONE THREAD
WHILE ANOTHER WRITES IT, and every file the app keeps — a picture, the saved
pictures' names, a poster tile, a rendered share card or export — is written
beside its name and moved over it (`app/files.py`'s `write_whole`); a
read-change-write of the names file holds one lock. MEASURED: dev's refresh pass
held its loop 2,061 ms writing 558 DEBUG lines; on a harness copy beside a busy
thread that burst was one 12.1 s block, and its per-row log calls 17.5 s → 53 ms
in all; a 558-span burst costs the logging thread 10.1 s → 27 ms beside a busy
thread (113 → 18 ms idle); a listener writing a line at a time managed 52 lines
a second beside one, and filled its queue — batched, 17,000.

**The calendar's reads run in a pool of worker processes; everything else in the
web process.** `app/read_pool.py` owns it. Threads share one interpreter lock, so
reads asked at once ran one after another: MEASURED on a copy of dev, six of a
filtered month's late days at once took 1.97–2.02 s each on threads and
1.01–1.11 s in a pool of four, and under a restart's refill burst a mark answered
in 2.0–2.4 s instead of 4.9–5.6 s. WHAT A WORKER RUNS IS A READ: a module-level
function of a connection and values (`read_pool.read(fn, ...)`), on the worker's
own READ-ONLY connection (`db.become_read_only`, `db.worker_read` — which refuses
to run in a process that has not become a reader, so the web process can never be
made one), handed values and returning values, pickled across. It touches no
in-process state — a page's filed month, `_new_answers`, the refill latches, the
pacers, the month-changed clocks, `refresh.viewed` all stay in the web process,
which is why this shape needed no latch moved; every write stays in the one
writer queue. A read whose callers pass their own functions (`query.titles`'
`keep` / `narrow`) stays on a thread. A MONTH NOBODY FILTERS IS READ IN PLACE
(`query._answered_in_place`): its answer is the whole calendar, and handing
20,277 cards back cost 0.86 s, more than the read; a filtered month's 1,470 cost
~60 ms. `READ_PROCESSES` sets how many workers (default: the cores less one, at
most `read_pool.DEFAULT_MOST`; 0 reads on threads, which the suite does —
tests/conftest.py — and tests/kernel/test_read_pool.py runs a real one). Workers
are SPAWNED on every platform (the web process has threads by then, and a fork
copies a held lock), started and warmed by the lifespan (`query.ready`), write no
log file, ignore Ctrl+C, are rebuilt by the next read when one dies, and LEAVE WHEN
THE WEB PROCESS DOES, however it ended (`read_pool._leave_with_the_web_process`,
waiting on the parent's sentinel): a web process stopped by force says nothing,
and every one of its workers ran on. A worker holds ~106 MB. A spawned worker
first imports its parent's main module, so a script that starts the app's
lifespan keeps its own top level out of it — run.py does everything from
`main()` — or every worker re-runs it: with run.py's steps at its top level,
each worker attached the log file the web process rotates and imported the whole
app (`tests/kernel/test_read_pool.py`'s `AWorkerWritesNoLogFileTests`).

**A slow request's log line accounts for its whole time.** `app/perftrace.py`
owns it: every SLOW request line ends with `unspanned=` — the part of the
request no span covers, nested and concurrent spans counted once — so a gap in
the instrumentation names itself instead of reading as a fast request. A route
that spends time between two spans is the next thing to span; every step
between a calendar read and its render has one (`calendar.view_state`,
`calendar.viewed`, `calendar.backlog`,
`calendar.file_month`, `calendar.day_chips`, `calendar.first_page`,
`query.span` / `query.titles` / `query.present`, `calendar.season_lines`,
`calendar.logos`, `calendar.card_pictures`, and every off-loop render's
`templating.drawn`). A span is DEBUG until slow
(`SLOW_SPAN_MS`) and spells its fields only when its line is printed: 2–3 µs at
INFO, ~30 µs at DEBUG to the file and console, which the log thread writes
(MEASURED; 203 µs when the logging thread wrote them itself, 18 ms beside a busy
thread). EVERY READ IS COUNTED TOO: `db.run`
TALLIES itself (`perftrace.tally` — one summed entry, `db.run=812ms×9`, each
read still counted toward what is covered, no line of its own), because beside
a restart's refills a read waits for a thread and the interpreter lock, and a
mark's eleven small reads were 2.6 s its line could only call unspanned. AND SO
IS THE MIDDLEWARE: a marker INNERMOST of the stack (`perftrace._StackCrossing`,
appended by `install`) times the way in (`asgi.in`), each body read through the
layers (`asgi.receive`) and the answer's way out (`asgi.out`) — every
`@app.middleware("http")` layer is a task and a stream, a turn of a busy loop per
hand-over, MEASURED at up to 1.18 s in and 1.11 s out on a live restart. A gap
of 50 ms or more says where it fell (`unspanned=…ms (longest …, after X, before
Y)`), which is what to span next; on the restart that closed these, the slow
requests left 4–36 ms unaccounted.

**Work a request starts behind itself starts once its answer has been sent.**
`app/after_response.py` owns it: `later(start)` holds a starter until the
response's last byte has gone (a pure ASGI middleware, outermost), and starts it
at once outside a request. Every starter of background work a request reaches
goes through it — a stale week's refill, a pending answer's follow-up, the
enrichment drain a fill asks for, the title store's refresh after a view, a
page's network-logo warm-up, a share page's poster warm, a lapsed library read,
TMDB's season-premieres worker — each checking again as it starts what it
checked before. Started during the page, they ran beside it: a filter save's
logo look-ups ran beside its 718-card render. The tracker's watchlist pass is
the one exception and says why: it rides on Starlette's background task, which
attaches only to an answer the route gave, so a request its access check refuses
starts nothing. `tests/kernel/test_after_response.py` holds every starter to it.
A WRITE THE ANSWER DOES NOT DEPEND ON goes the same way (`after_response.write`):
a session's slide and a month's is-new commit waited their turn in the writer
queue behind a restart's refills, up to 1.17 s, on pages that drew nothing from
either. A test client not entered as a context manager stops its loop when the
app returns, cancelling such a write mid-queue, so every test client is made over
`tests/support.py`'s `settling(app)`, which returns once they have landed.

**The session ROW is the authority on expiry, not the cookie.**
`app/auth/cookies.py` issues it HttpOnly, SameSite=Lax, Path=/, no Domain, and
Secure with the `__Host-` name unless the Secure policy says otherwise; its
Max-Age is the ABSOLUTE cap, never the sliding window (`app/auth/sessions.py`
owns both clocks). A cookie expiring at the sliding window would log out the
active user the sliding exists to keep signed in.

**An account keeps a way in, and what is left without one is ONE function's
answer.** `app/auth/identities.py`'s `ways_in` reads an account's ways in — its
password and each link a service SIGNS IN with (`revocation._LINKED`'s
`signs_in`: never TMDB's) — inside the caller's transaction, and `WaysIn.
leaves_none` says whether removing one leaves none. Every removal of a way in
refuses by it (`unlink_identity`, `LastLoginMethod`; an administrator may pass
`force` after a confirmation), and every control the account page draws for one
asks it too (`routes._unlinkable`), so the page offers a button exactly where
the removal is allowed. Both used to count the ways apart from revocation's
table; two counts of one rule is how a page comes to draw a button that only
refuses, or to hide one the rule allows (tests/auth/test_ways_in.py holds the
two to one answer).

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
MEANS belongs in the Trakt and Simkl ones; TMDB's `cached_get` makes only public
catalogue reads with the instance's token and so has no `private` flag at all — a
read made with a viewer's own TMDB token, and every write, goes through
`transport.send_private` instead, which shares the gate and the circuit and
caches nothing (`app/providers/tmdb/account.py` and `watchlist.py` are its
callers).
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
of THREE providers implements it (`app/providers/simkl/watchlist.py`,
`app/providers/trakt/watchlist.py`, which reads its list EVERY PAGE — Trakt's
watchlist pages at 100, and a read that stopped at one page once missed 46
shows — and takes a full list's 420 as REFUSED, never retried, and
`app/providers/tmdb/watchlist.py`, a plain list of shows read every page at 20
through v4 and written one title at a time through v3, each answer's
`status_code` the outcome — 12 is TMDB saying the viewer listed it meanwhile, so
it counts as theirs). A service may
answer success to a request that did nothing — Simkl answers any `/sync/` path
and answered a put of a title already listed with the same body as a real move —
so an outcome comes from the answer's body, a written title is WRITTEN only when
a read of the list afterwards shows it, and what the service held BEFORE is read
at the moment of the write and kept, because it is the only thing an undo can
restore. A write moves a title the viewer may have put somewhere on purpose, so
dropping never adds a title and an undo never takes what the viewer did
themselves — and a drop is TOLD what the tracker's own record says was there
before (`base.WatchlistDrop.status_before`: from a write the read-back
confirmed, None otherwise), which a list with no statuses to set a title back to
needs: TMDB's drop removes only a show the tracker put there and keeps any other
(KEPT). The tracker is the record: a failed write is logged and counted, and
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

**A TMDB link is a viewer's own grant: never a way in, never an operator setting,
and it changes only what asks about the viewer's account.** `app/auth/tmdb_routes.py`
owns the round trip (`app/auth/tmdb.py` its calls): a v4 request token the
instance asks for with `redirect_to` set to THIS instance's callback carrying the
handshake's state — TMDB sends the browser back to that address exactly — approved
at TMDB and exchanged once. The request token rides SEALED on the handshake row
(`handshakes.hold_on_handshake`), so the callback trusts nothing from the URL but
the state, and the state only beside its cookie and the session that started it.
WHAT NOTHING CAN TELL IS WHO APPROVED: TMDB binds an approval to the request token,
not to a browser, so whoever starts a link receives the grant of whichever TMDB
account approves its token — a person persuaded to approve someone else's approval
link links their TMDB account to that someone's account here (starting one needs an
account on this instance); TMDB's approval page naming this instance's application
is the only warning. ONE ACCOUNT OF A SERVICE PER ACCOUNT HERE, for every link:
`identities.link_provider_identity` refuses a different account of a service the
account already holds (`AnotherAccountHeld`, told to unlink first) and keeps
reconnect of the same one; TMDB is stricter (`tmdb_routes._holds_a_link`, the same
account too and before the exchange, since its grant has no clock to refresh).
EVERY REFUSED LINK OR SIGN-IN REVOKES THE TOKEN IT WAS HANDED, at the service,
unless a row of that service account or the instance itself holds that very token
or a row cannot be read (`provider_login.end_unkept_grant`) — stored nowhere, it
could never be revoked later.
The grant is stored like every link (`linked_identities`, provider `tmdb`, the
account's 24-character object id, the token sealed; `session_token` stays NULL —
v3 takes the v4 bearer, MEASURED). `app/auth/revocation.py`'s `_LINKED` is the one
table of linkable services and says which SIGN IN (`Linked.signs_in`): TMDB does
not, so an unlink counts only the ways in left (`identities.unlink_identity` —
counting every link let a TMDB row stand in for the last real one),
`login_with_provider_identity` refuses it, and an unlink — as Trakt's and Simkl's
do (the /me unlink route in `app/auth/routes.py`) — deletes the row and then revokes
the grant at TMDB, saying so beside the rows when TMDB could not be told. The grant reaches code only through the viewer's Settings
(`auth.viewer.settings_for_user`): `config.VIEWER_FIELDS` are never loaded from,
written to or offered by the operator's settings. TMDB's `is_configured` is "this
viewer holds a grant" (`Settings.tmdb_account_configured`) — the PRIVATE question —
while every catalogue path (the search's default, calendar sources, details) asks
`catalogue_is_configured` / `config.tmdb_configured`; TMDB has no sync port, so it
is no tracker source either. Linking therefore changes no search box, calendar
source, tracker source, access gate or sign-in (tests/auth/test_tmdb_link.py holds
each), and appears only through `providers.for_watchlist` and the ratings sources.
RATINGS ARE A CHOICE OF SERVICE: `app/ranker/sources.py`'s `_RATINGS` names every
registered Source (None: keeps no ratings — Simkl), a board offers one press per
linked service that keeps them, the press names its service, and an unknown name is
`UnknownRatingsService`, never read as Trakt. TMDB scores in half points and a
half rounds UP. A seed registers its titles through `metadata.ensure_many` in
bounded turns before its own transaction (`core.REGISTER_TURN_TITLES`), and so does
a film's WHOLE COLLECTION, added from that film's board panel
(`add_titles(..., in_turns=True)`): the request names the board row alone, and
which collection and which films are TMDB's word (`ranker_sources.film_collection`
from its stored description, `collection_items` from one cached `/collection/{id}`).

**TMDB is first in `Source`, and nothing it supplies outlives six months.**
`app/providers/base.py`'s `Source` declares TMDB first, and that ORDER is the
app's one order: who describes a title (the details panel and the tracker's
season totals, through `detail_source`), whose value a card shows for a viewer
with no stated order, whose artwork is tried first. ONE VIEWER'S STATEMENT
REORDERS A PART OF IT, never a part it was not asked about: the tracker's season
TOTALS have an order of their own (`SourcePrefs.totals_order`, stated in the
tracker's settings dialog, `app/distrakt/tracker_settings.py`), passed down as
`order` to `detail_source.choose` / `for_season` and nowhere else — so it changes
WHO IS ASKED for a total and nothing about a card, a panel or a picture, and a
frozen month keeps the totals it recorded. A caller that passes no order is asked
in the declared one, which is every details panel. TMDB keeps no watch history,
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
network logos all read that constant; the title store's groups declare it through
the registry (`Capabilities.retain_seconds`) and `rows.sweep_past_ceilings`
CLEARS the service's group by it — the row and every other service's group stay
— held or not. A CEILING IS ABOUT EVERY WORD AND A GROUP HAS ONE CLOCK, so for
such a service a group's `fetched_at` is its OLDEST word's: a write renews it only
when it rewrites everything the group holds (`rows.ceiling_clock` for an upsert,
`rows.kept_clock` for a write made from Python; `groups.WORDS` names the words,
and tests/metadata/test_retention.py fails on a column of the three tables that
neither it, `groups.NOT_WORDS` nor `groups.IDENTITY` names), and the heartbeat
re-reads a held group before its ceiling (`refresh.queue_ceiling`). A REFILL
WRITES ONLY WHAT WOULD CHANGE, AND A CEILING-BOUND CLOCK MOVING IS A CHANGE:
`rows.plan_listing_writes` counts `fetched_at` only for `ceiling_sources`, so a
TMDB group a re-read restates whole is re-stamped, while an unchanged Trakt or
Simkl group is not rewritten (its clock then says when its listing last CHANGED
it, which `sweep_unlisted` reads only once nothing lists it). A LISTING'S CLOCK
IS ITS WINDOW'S COVERAGE ROW: a feed's listing is kept while its service's
coverage row for its window replied inside the retention window, and goes with
the row (`entries.sweep`, `_due_sql`) — every listing a feed files lies in a
window the same fill covers — and a coverage row's `stored_at` is when its
service LAST REPLIED, a failure marking it answered = 0 and leaving the time, or
a service whose token stopped working would keep its last listings for as long
as anybody asked. A SEARCHED listing — a per-title lookup — goes by its own
`listed_at`. TMDB'S DESCRIPTION CARRIES FOUR WORDS NO LISTING STATES, each in
`groups.WORDS` and so under the same ceiling and clock: the title's `logo` (the
`images` append, narrowed to the instance's language alone — `detail._image_params`,
measured +23% on a show's cached answer where "lang,null" was +47%), a film's
`collection_json`, the `next_episode_json` snapshot and a season's own `videos_json`
(the season call's `videos` append). All ride on the calls a description already
makes, so the refresh budget is unchanged; changing an append changes the cached
answer's URL, so each title's next read is a full answer once. THE NEXT EPISODE IS
A SNAPSHOT, wrong the day after it airs: a panel draws it only while its day is
today or later (`panel._still_next`) and a description naming one falls due the
local midnight after it (`descriptions.title_stale_after`), so the next view
re-reads it. A group described before these words existed gains them when it is
next re-read — on a view, past its three-day clock — and nothing re-reads every
group to fill them.
The credential is the v4 read token,
sent only in the Authorization header (`config.tmdb_token_error` refuses the
short key, `tmdb_configured` reads a stored one as unset). NO SERVICE IS
ANOTHER'S FALLBACK: a per-service difference names each service explicitly —
through the registry (`Provider.title_url`, `Provider.episode_url`,
`DetailPort.fetch_season_summary`) or an explicit table with a test that every
registered Source has an entry — and an unknown name is an error, never quietly
read as one of the others.

**ONE TITLE STORE, AND ONE WAY IN.** `app/metadata/` owns what every service says
about a title — ONE ROW PER TITLE, PER SEASON AND PER EPISODE (`titles`,
`seasons`, `episodes`, named once in `tables.py`), every service's facts side by
side in a COLUMN GROUP of its own (`{source}_{column}`: the columns its package
declares, `Capabilities.store_columns`, spelled through `groups.py`), each group
with its own expiry — which of a service's own ids names which title
(`service_ids`), which title a set of ids is (`titles`' identity columns,
`title_aliases`; `identity.py`), and every column elsewhere that holds a title's
key (`holders.py`). A SERVICE HAS ONE GROUP PER TITLE, AND IT IS ONE ENTRY'S: a
service can hold several entries for one title (Simkl files a later season as a
title of its own); the group is the first to arrive — or, arriving together, the
one with the most listings, then the more complete, then the lowest id
(`rows._entry_rank`) — and it stays. Every other entry is a non-primary
`service_ids` row that still finds the title, and its words are kept nowhere, on
the title or on its seasons and episodes, because it numbers its own. A
service's own id has ONE SPELLING (`rows.own_id`: the id its ids state, a number
as decimal text), and every spelling a listing used finds it. Every path that
meets a title registers it there: `plan_records` then `write_records` for a
service's own listings (spelled before the caller's transaction by
`metadata.listings`, decided before its lock, written inside it), asking nobody
(a fill names hundreds of titles); `ensure` / `ensure_many` for a
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
titles, and its episodes stopped reaching the season the tracker held. THE STORE
FOLLOWS THE SAME OWN ID, BY THE SERVICE'S OWN ANSWER: a description — the
service's answer about the title itself — naming its entry under another title
than the one it is filed under, with no re-key behind it, moves the entry — its
group where it was the title's, its seasons' and episodes' words, its listings,
its ids — and the title it lands on keeps a group of its own (`merge.entry`,
from `descriptions.plan`, onto a title another entry of the service holds too).
A LISTING NEVER MOVES ONE (`metadata.plan_records`): one service's calendar
files disagree with each other — Simkl's October file names its entry 2777093 as
TMDB's 230757 and its November file as 276408, its own lookup the newer — and
every week filled from one moved the entry and every week from the other moved it
back, inside the fill's lock. A listing of a filed entry is filed under that
entry's title and the ids that named another title are taught to neither, its
group's ids included (`identity.Planner.named_apart` / `agreeing`); a listing
still re-keys its entry's title up the waterfall, and files an entry the store
has not met where its ids say. `tests/metadata/test_holders.py`
fails when the schema grows a table neither holder list classifies (a table the
store retired is `tables.RETIRED`'s), and SQL naming the store's tables lives in
`app/metadata/` or names them through `tables.py`'s constants.
READERS READ THE STORE, AND A FEATURE'S ROW KEEPS NO WORDS OF ITS OWN. A tracker
row's and a board row's name, network, year and counts are columns their own
SELECT adds (`app/metadata/labels.py`): each word from the first service in the
declared order that states it, the service HOLDING THE ROW'S SEASON naming it,
and a whole show's counts only from a description that vouches for its season
list. Nothing falls back to a copy — a frozen month's record is a snapshot, not a
copy, and keeps its words. A details panel answers from the store first
(`app/metadata/panel.py`), a tracker total and a board season row from the
season's row. Words a feature's record carries SEED the store only where no
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
names no network, status or certification) has every `enriched = 0` group queued
on the heartbeat (`refresh.queue_listed`, at most a minute's allowance waiting),
because a calendar month queues only groups already described; an answer that
says nothing is recorded as a failure, so the backoff holds it and five give it
up (`LISTED_GIVE_UP_AFTER`), and the calendar's "unenriched" count adds what is
still owed (`refresh.listings_owed`). Simkl's sparse listings are its own drain's
(app/calendar/enrich.py), Trakt's arrive complete. SO IS A CALENDAR TITLE NOBODY
ASKED A DESCRIBING SERVICE ABOUT: a service declaring
`Capabilities.describes_every_card` (TMDB) is queued a description of every title
on this and next month's calendars it has an id for and no group of
(`refresh.queue_cards`, from calendar/completion.py on the heartbeat, sharing that
one minute with the sparse listings — `_background_room`), because its group is
the title-only record a card is filtered by. A refresh that fails for a title
the service has no group of leaves a BARE group carrying the failure
(`descriptions.plan_failures`) where the service's own id names the title —
TMDB's — so it becomes an owed group like any sparse listing, backed off and given
up at five; a service whose own id names no title (Trakt, Simkl) records nothing.
A HOLDER KEEPS ITS TITLE'S GROUPS (`rows.sweep_unlisted` asks
`holders.held_keys`); only a service's ceiling clears them. A RE-KEY THAT FINDS A
FEATURE'S ROW UNDER BOTH KEYS keeps the MORE COMPLETE one — one rank per table,
`holders.more_complete` — re-keyed, its gaps filled from the other, the other
logged in full and a board row sent to its board's removal history. The store's
own rows MERGE (`merge.titles`): each service's group decided on its own — where
both titles have one they are two entries, ranked as above, the loser's words
logged — and no listing is lost: two services' listings of one episode are one
row, one service's two a listing and its repeat, and a listing naming no full
episode is re-decided against the merged title. THE CONVERSION AND THE WRITERS
ARE ONE RULE: migration 49 built this shape from the per-service tables, which it
kept whole and nothing reads (`tables.RETIRED`, dropped in a later release once a
production copy has been checked against them), and
tests/metadata/test_conversion_agreement.py holds its frozen fold and the live
writers to the same rows on the same input.

**The calendar cache stores the UNFILTERED window once per (endpoint, 7 days),
holding EVERY source's records as ROWS.** There is no stored window object: a
versioned compressed envelope with a `PAYLOAD_VERSION` used to live here, and it
went with the blob — `app/calendar/entries.py` now hands each source's records to
the title store, which writes the title's group and files each listing as an
INSTANT on its episode's row (`episodes`: a service's first instant in its own
columns, the rest in `repeats_json`, each with a bit per calendar it is on and
per calendar a search put it on; app/metadata/episodes.py) in the same
transaction — so a shape change is a migration rather than a version byte. The reasoning that version
carried still binds, and `app/calendar/cache.py` keeps it: a month spans five or
six windows, so nothing may leave the stored calendar half-converted or one
airing would draw merged in the span that had been refilled and split in the one
that had not, on the same page. Per-viewer filtering — and
per-viewer source selection — happen at READ time, so one viewer excluding a
genre or narrowing to one service does not poison what another sees from the
same rows (`app/calendar/cache.py` owns the envelope and the fill;
`app/calendar/query.py` owns the read; `app/calendar/resolve.py` composes what a
drawn card shows). A VIEWER'S SOURCE
CHOICE DECIDES WHICH LISTINGS A CARD IS DRAWN FROM, NEVER WHAT THEIR FILTERS JUDGE:
`query.judge` — the one implementation of every filter, the network
lists included — judges a title as every service the INSTANCE admits describes it,
in the viewer's order, and draws the card from the viewer's own services alone;
otherwise narrowing to TMDB took Simkl's "talk-show" with Simkl's listing and a
filtered title came back. EACH SERVICE'S ANSWER IS STORED OVER THE SPAN IT IS
AUTHORITATIVE FOR, and nowhere else (`cache.answer_span`, from the service's own
`Capabilities.reach`, never its name): Trakt's and TMDB's the window asked,
Simkl's every whole window of the calendar months its files cover — so one Simkl
month file fills, confirms and takes listings off its month's every week, and a
week straddling two months is covered only by an answer that read both files.
`entries.store_span` keeps each service's records inside its span, takes its
listings off there alone, and writes a coverage row for every window of it (a
service answering "unchanged" confirms the window asked, which is what its
validator speaks for). A WINDOW AN OVERLAPPING ANSWER OF THE SAME SERVICE HAS
JUST STORED IS NOT STORED AGAIN (`entries._stored_from`): a straddling week's
Simkl answer and an inner week's both speak for that month's inner weeks, and a
restart asks both — 15 stores for 8 windows — so a store claims each window for
what each service said (a digest of its listings, taken before the store) and an
answer within a minute saying exactly the same leaves it alone. The envelope
records who was ASKED as well as who ANSWERED, which is what lets a source
switched on later refill a window that legitimately has none of its records,
and what keeps "partial" meaning a source that failed rather than one nobody
asked. WHICH SERVICES A READ ASKS IS DECIDED SERVICE BY SERVICE from each one's
own coverage row (`cache._due`, `fill_sources` the one fill): a window nothing
has filled, or a service in play it never asked, is filled inline, those services
alone; a service whose reply lapsed, or whose failure is due a retry, is refilled
alone AFTER THE RESPONSE HAS BEEN SENT (`after_response.later`, whose middleware
wraps the whole app) — the window is served from what is stored at once; a fresh
service is not asked, and nor is one a refill already under way is asking about
the window, its own or a neighbour's whose answer spans it (`schedule_refill`'s
latch, by service and window), so the weeks one Simkl file renewed, which lapse
together, are refilled by one answer; and a refill, decided by a read and started
after its answer, re-reads the window's coverage as it starts and asks only what
is still lapsed (`cache._still_lapsed`) — a filter save's refills started after
its 44 s answer asked Trakt again for four weeks the shell's had renewed.
`refresh_months` asks only the services past its own tier.
A fill whose every service refused records the failure, so the failure clock,
not every read, decides the next try. ONE GAP IS NOT A FAILURE: a source still WORKING ITS ANSWER OUT
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
THE READ IS ONE MODULE, `app/calendar/query.py`, AND EVERY READER ASKS IT: the
month and a late day, the search's stored half and its season picker, the share
page and its preview card, the tracker's calendar import and a mark's redraw. Four
steps, each said once (its docstring): the rows a reader names, whole, with each
service's listings and the ids it files them under and none of the words
(`read_rows`); the cards, and each viewer's placement of them, in Python
(`cards_of`, `place`); THE JUDGING, one SQL statement for the whole read
(`judge`), over the few thousand title-and-services combinations a month's cards
share — each judged field the first service in the viewer's order that states it
(`_winner`), the operator's floor and then the viewer's specs through ONE predicate
builder (`_keeps`); and, for the cards a page DRAWS alone, the winning service per
field in one more statement and those rows' words (`present`), composed by
`resolve.compose`. A READ JUDGES EVERY CARD AND DRAWS ONLY THE CARDS IT SHOWS: the
shell asks the whole month for keys and days (`assemble_range(drawn=False)`) and
draws its inline days; a late day reads its own day; a mark redraws its title's
rows (`query.titles`). NOTHING OF A READ IS KEPT BETWEEN REQUESTS. A span memo was
here, holding a month per viewer key in process, and its key was where "filtered
at read" could quietly break — a key missing one thing a viewer decides hands one
reader somebody else's month; it went with this module, and comes back only for a
measured need, with that key's whole burden. THE READING RUNS IN A READ WORKER
PROCESS (`app/read_pool.py`; `query.span` and `query.present` hand their pure
halves, `placed_span` and `present_items`, to `read_pool.read`), the drawing on a
worker thread (`templating.drawn`), so the loop serves other requests while a
month is built and reads asked at once run side by side — below. MEASURED on a copy of dev, user 1's October All
Episodes, against the memo's read: the cold shell 3.45–4.66 s → 1.93–2.29 s, a late
day 0.88–0.95 s → 0.32–0.35 s, the longest loop block 0.49–1.67 s → 0.09–0.10 s;
the warm shell, which the memo served, 0.44–0.62 s → 1.75–2.14 s; every drawn
field of 52,794 cards identical across five calendars, two months, four service
orders, filters off and one service alone. ITS READS COME BACK IN ONE AGGREGATED
ROW (`db.aggregated`: the month's rows, its titles' identities, the judging's
verdicts — a row a step, each step winning the interpreter lock back, read a
month's 19,607 rows in 5.6–7.0 s beside a restart's refills, 0.14 s idle; one row
is one step, in the same order — but a REAL in 15 significant digits, so a read
whose rows are WRITTEN BACK names its REAL columns `exact`, as the episode rows a
merge rewrites do: `episodes._aggregated`), and every connection reads the file through a
memory map (`db.MMAP_BYTES`: the rows' SQL 235 → 126 ms). With a card one service
lists placed without asking the resolvers (`query.place`) and a row's instants
read by column names spelled once (`episodes.instants_of`), MEASURED on a copy
of dev against the tree before: the cold shell 2.0–2.2 s → 1.7–1.8 s, the warm
1.9–2.2 s → 1.5–1.7 s, a late day 0.33–0.35 s → 0.28–0.29 s, and the 60-view card
dump 74 → 49 s with every drawn field of 56,900 cards identical.

**One episode is ONE card, drawn on the day the viewer's first service gives
it.** Which listings are one card is decided twice, both times the same for every
viewer. WHICH EPISODE ROW A LISTING IS, AT WRITE (`app/metadata/episodes.py`): a
listing naming its episode is that episode's row (`S{s}E{e}`), a film its
title's `film` row, whoever lists it and however far apart; a show listing with
no full coordinate joins a numbered episode only when, on its own calendar and
UTC day, exactly ONE episode agrees with what it did state and its own service
has no listing of that episode there yet — otherwise it is a row of its own
(`D{day}…`) — and the halves it left out are kept (`unstated`), so the fold is
RE-DECIDED BOTH WAYS whenever that day's listings change (`Book.refold`). HOW A
ROW SPLITS INTO CARDS, AT READ (`query.cards_of`): a listing joins a card when it
is in the same calendar month as EVERY listing already in the card, or at most a
week from it across a month's edge (`may_join`, a date-ordered scan, one listing
per service per card), and then cards sharing no service are one card, each
joining the nearest earlier one — two services' dates for one episode, however
far apart, are one episode; only a service listing it twice draws it twice.
ONE LISTING PER SERVICE
IS ALSO WHAT STORAGE PROMISES: an instant a search or a tracker add wrote (its
calendar's `searched` bit) fills a gap in its service's feed and stands down the
moment that feed lists the same episode, at whatever instant — dropped at read
(`query._row_listings`), taken off by the fill (`episodes.plan_fill`), never
written beside a listed one (`store_loose_airings`). A date-only searched
instant and the feed's timed one were otherwise two listings of one service, and
the scan drew two cards. Which day
the card is drawn on is the viewer's: the first service in their order describes
it, DATE AND TIME INCLUDED, and a date-only one borrows a time only from an
instant that falls on its date somewhere on earth (`app/calendar/resolve.py`'s
`resolve_airing`, `falls_on`); the others' days are kept (`Record.listed_airings`)
and the card draws them where they differ (`Item.air_choices`). Because the scan's
units depend on every listing of an episode, A READ NEVER GROUPS PART OF ONE: a
span makes its windows current coverage-only (`ensure_window`) and reads each
episode row with a listing in it once, whole — every service's listings at any
date (`query.read_rows`) — and the search reads a title whole
(`query.titles`). And there is ONE PLACEMENT FUNCTION, `query.place`: the month,
a late day, a share page and its card, the tracker's calendar import, a mark's
redraw (all through `query.span` or `query.titles`), the
search's "where it airs", its season picker and the card its jump lands on
(`calendar/search.py`'s `_drawn`) all ask it, and none works a day out from one
listing. `tests/calendar/test_placement.py` holds the readers to one answer — a
day against its month, two viewers on one span, a month's edge, the search.

**A service may LIST a calendar it does not publish, by another service's word.**
`app/providers/base.py`'s `DrawnFrom` declares it (`Capabilities.drawn_from`):
Simkl's Season Finales are its All Episodes listings whose episode TMDB's season
list marks a finale or a mid-season finale. Nothing is fetched or stored for the
calendar: the read takes each service's listings by its own bit and keeps a
drawing service's only where its marker's word says so (`query._calendar_reads`,
`episodes.span_rowids(bits=, typed=)`); a read of the calendar makes the drawing
service's windows of the calendar it draws from current, for it alone
(`cache.drawn_sources`, `ensure_window(only=)`); the heartbeat queues the
marker's reads of the seasons those listings need (`refresh.queue_marks`, a
season the marker's own description lists, inside `_background_room`). A
service is OFFERED on a calendar by `providers.lists_calendar` — publishing it,
or drawing it where the marker's catalogue can be read — and ASKED by
`capabilities.answers` alone. On a calendar whose cards say which finale they are
(`Endpoint.marked`, Season Finales only) every listing carries its marker's mark —
a drawing service's by the service it names, a publishing one's by its own word
— and the card the first in the viewer's order that states one
(`Record.episode_mark`, the MID-SEASON FINALE tag). The month-changed check
watches the calendar's own windows, so a mark landing or the drawn calendar
refilling shows on the next load, not as the notice.

**A service that describes a title it does not list joins the card as a
TITLE-ONLY record, which is an opinion on facts and never a listing.**
`app/calendar/query.py` owns the rules — such a group speaks for a card it does
not list (`_speaks`) and is drawn as a record of its own (`_title_only_record`) —
and `resolve.compose` takes those records apart from the listings
(`describing`), so no choice that names a listing can reach one. A service
declaring `Capabilities.describes_every_card` (TMDB) has its group of the title's
row read with the listings' own groups — judged in the read's one statement, drawn
in its words read (`present_rows`), no call and no write — for every card of a
title it describes and does not list.
The record competes for every scalar field in the viewer's order and joins the
genres, keywords and show type (those two only where `facts_known`), its ids
add the service's greyed button — and it NEVER wins attribution, the airing or
the coordinate, so a TMDB-first viewer's card stays on the day a listing gave
(the Yellowjackets fixture holds it); it never makes a card alone, so a viewer
narrowed to TMDB sees only what TMDB lists; a viewer's source choice narrows it
as it narrows a listing; it is not among the services a card's combination
counts as listing it (`query._combo`); and the release narrowing and the
disguised-film prune never see it. It carries no airing, coordinate or release
schedule at all (`query._title_only_record`).
`tests/calendar/test_title_only.py` holds each rule on every reader, the search
picker's `_kept_by_filters` included.

**A card's `alternatives` are what its services REALLY disagree about.**
`resolve._distinct` decides it for every field and every service: text folded
(case, whitespace, no-break spaces), a runtime in whole minutes, a picture by
its exact address, and a status read through each service's
`Provider.status_means` into the one vocabulary (`base.STATUS_WORDS`, Trakt's
words) — a coarser word (Simkl's "ended", "tba") agrees with every word it
covers and steps aside for the finer one. Ratings are never folded
(`SIDE_BY_SIDE`): two services' scores are always drawn side by side. Where
values agree, `field_sources` lists the winner first, which is what
`Record.shown_source` names and the details panel is opened with.
`tests/calendar/test_equivalence.py` holds it; `tests/providers/
test_protocol_conformance.py` holds every service to an answer for its words.
A CARD IS DRAWN WITH ITS WINNERS AND COMPARES ON DEMAND: `_card.html` draws
each field's winning value and no flip or pair of chips; a card with
`alternatives` (or `air_choices`) carries ONE control on its poster that GETs
the card drawn whole (`expanded`) from `calendar_card`
(`/calendar/fragments/card`, references only — the card's store row
`Record.card`, its `airing_key`, the page's tokens, built by `_card_url`) and
swaps it in for itself — and with it EVERY OTHER CARD OF ITS TITLE on the month
that has something to compare, out of band by airing (`_compared_cards.html`, as
a mark redraws its title's cards), and the title is FILED AS COMPARED on the
page (`_FiledMonth.compared`), so a late day, a big day's rest and a mark's
redraw draw it whole too (`_card.html`'s `compared`); a reload compares nothing.
MEASURED, October All Episodes: every one of a filtered
month's 1,334 cards carried a flip and a pair of chips; this took 4.9 KB and 39
elements off each card's 18.2 KB / 134. The answer reads the title's own rows
like a mark (~30 ms); a card the store no longer draws answers 204. A surface
that hands the card no `card_expand` draws no control (the share page has a
card of its own, which never compares); `tests/calendar/test_card_provenance.py`
holds both drawings, `test_routes.py`'s `TheCardDrawnWholeTests` the route. THE
SAME GOES FOR A BADGE'S THREE FILTER BUTTONS: the badge's `<summary>` asks for
them `click once` from `/calendar/fragments/badge` (`_filter_offer.html`, the
same for everybody — the stored answer is the badge's mark, drawn with the card)
and they land after it, never with the card: 6.2 KB and 29.5 elements of every
card. AND A CARD'S TEMPLATES SEND NO WHITESPACE BETWEEN THEIR TAGS: they open
with `{#- tight -#}` (app/templating.py's `Dedent`, at compile time: each line's
indentation, and a line break between tags or statement tags — never beside an
expression's output), because every container in a card lays out by flex or grid
or positions what it holds; a template in ordinary inline flow may not opt in,
since a line break between two inline tags is a visible space there. MEASURED,
the same October: text nodes 84 -> 23 a card, the documents otherwise identical.
AND A CARD'S SEASON LINE IS DRAWN WITH IT, FROM THE STORE: `routes._season_lines`
reads every drawn card's season rows in one query (`panel.season_lines`, every
service's `episode_count` / `air_dates_json`) and decides each by
`detail_source.for_season`'s walk (`later_for_season`, one walk for both) — and
a service whose season the store does not hold DECIDES NOTHING: no line, and that
service's season queued behind the page (`refresh.Wanted.source`, which narrows a
view's queueing to the one service the page drew words from). It used to be a
request per card as it scrolled into view (4-13 s each beside a month read,
MEASURED on a copy of dev); a filtered October owes 227 seasons, all TMDB's.

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
THE READ PATH TAKES THOSE KEYWORDS AS ONE SET (`vocab.SPEC_KEYS`, checked by
`vocab.complete_specs`, which refuses a keyword it does not name), and THE
OPERATOR'S FLOOR IS THE SAME SHAPE (`vocab.floor_specs`, the one place a
Settings field becomes something a read applies; `vocab.FLOOR_FIELDS` lists
every field it reads). `query.judge` hands the floor and then the viewer's
specs to ONE predicate builder, `query._keeps`, in one statement, so a viewer can
only narrow what the floor left and the floor is never counted as their
filtering. Each dimension's treatment of a card nobody
has described is `_keeps`'s and is the instance owner's decision, not a
default: genres, countries, certifications, networks, language and missing data
show an undescribed card unjudged (`enriched`); keywords and show type wait for
the catalogue that states them (`Record.keywords is None` — TMDB alone, whether
it lists the card or only describes the title, below; a card TMDB has not
described is not judged by them); "only one service lists it" and "only a sparse
catalogue lists it and it names no network" are about who LISTS the card — a
service that only describes it is not counted — and exempt nothing. Only the
language floor is also applied at fill
(beside the country floor); the rest need the whole card. A group's keywords and
show type are an answer only where its `facts_known` says a
description stated them (`metadata/rows.py`'s `facts_values`) — an empty
keyword list is otherwise "never asked", which an include filter must not read
as "none".

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
empty card. THE OWNER MAY FEATURE UP TO TEN OF A MONTH'S PREMIERES ON IT
(`app/calendar/share_highlights.py`, chosen by a star on the tracker's rows), and
a feature is a GUARANTEE, NEVER A NARROWING: on a premieres card alone, every
featured title the view SHOWS takes a tile before the ranking fills the rest
(`share_routes.tile_candidates`), the grid stays in date order, the count stays
the page's, and a featured title the view filters, narrows or hides is not drawn —
the rule above, not a choice. With nothing featured the card is
`select_tile_items`' byte for byte. The page never changes. AND THE PICTURE'S
ADDRESS NAMES WHAT IT DRAWS: an unfurler keeps a picture by its address (Discord
kept a wrong one), so the og:image URL ends `&v=` a digest of the count and the
wanted tiles' titles, dates and premiere marks (`share_routes.card_digest`, after
the `p=` code), which the picture's route ignores; the file on disk is still
addressed by everything it draws (`share_card_cache.render_key`).

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
on — A FEW DOZEN BYTES, every picture a page draws carries one: a versioned
binary layout, each service and medium an explicit one-byte code
(`pictures._SOURCE_CODES`, every registered Source held to one by a test; a
service without one draws the static chain), base64url in ONE spelling, read
strictly; an image's request only — answers at once with the first address, in the
viewer's order and from what the registry holds NOW, that the token's chain of
hops has not tried (so two dead pictures never bounce a browser between them),
and confirms BEHIND the answer: a HEAD of the registry's address, never the
request's. Dead caps it (`artwork.cap`; the same address re-stated stays capped,
a new one starts fresh) and queues the WHOLE TITLE from every service at the
front of the refresh queue. Transient (5xx, 403, a timeout) is a STRIKE, counted
only a day or more after the last one (`artwork.strike`, the row's own
`fail_count` / `last_failed_at`, sooner leaving the clock where it was), and the
third (`pictures.TRANSIENT_STRIKES`, the registry's cap) is a death and heals —
a host answering 403 for a deleted file never says 404; alive forgets the strikes
(`artwork.clear_strikes`). Deduped per
address, bounded per minute, one heal per title per few hours, and every step
logged at INFO by the title's key. A share page keeps the static chain, and an
instance with no public base URL has no absolute address to report to.

**A network's badge is TMDB's logo through the image proxy, and one table row is
the whole record.** `app/media/logos.py` owns it. `network_logos` holds, per
network NAME, the TMDB network id, the logo path, whether the logo is light
(mean luminance of the very PNG the proxy hands the browser, which picks the
CSS frame — `.is-light` is a dark frame, never a second picture) and the clocks;
a page asks `logos.for_page` (signed in; it also warms what is due, behind the
response, one warm in flight, eight at a time) or `logos.known` (a share page,
which never asks anybody) ONCE for every badge on it — no badge costs a lookup or
a request of its own, and a network with no row, a recorded "no logo" or a row
past the ceiling is the 📡 with its name. A look-up that could not get an answer
(TMDB down, the proxy down, a show TMDB does not know) is NEVER recorded as "no
logo"; a show whose networks do not name the network is no evidence about it
(it used to fall back to the show's first network and drew BBC Two with Channel
4's logo), so up to `TITLES_TRIED` shows are asked. A row is TMDB's, so it is
rewritten whole, drawn only inside `MAX_CACHE_SECONDS` and swept by it, and renewed
monthly while it is on a page. Only the Discord emoji download composites bytes
(`logos.download_tile`, Pillow in a worker thread, kept nowhere); the old tiles in
`data/logos` are removed at start.

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
not return shapes. THE SEASON STRIP IS WHAT THE SERVICES LIST, NEVER A COUNT:
each answer states its season list (`seasons`, base.SeasonsAnswer's shape, kept
with the description — `descriptions.write_many` files it, `read` gives it back),
and `describe_panel` widens it with every other service's list the store or its
response cache holds, asking nobody; `details_panel.panel` draws those and the
season open. Counting up to the season open drew 2,026 tabs for a show Simkl
numbers by year. VIDEOS ARE THE ONE FIELD WHOSE VALUES ARE POOLED, and the
exception is deliberate: a missing or wrong default video is recoverable only
when the alternatives are on screen and say who listed them, and one field from
one service showed nothing for a title whose only video was a teaser. Each
answer's `videos` is that service's own list; `panel.describe_panel` (app/metadata/panel.py) —
what every surface calls for a whole panel, a season tab calling
`describe_season` — asks every other service the ids name for its list
(`DetailPort.fetch_videos`) beside the description, and
`app/providers/videos.py`'s pure `pool` orders them. The share page passes it the
BOUND ids, so a service asked for its list is asked about a title the link
shows. The panel plays them in ONE iframe that every choice in its popover menu
targets by name, so a pick replaces what was playing with no request; the one
line of script closes the menu afterwards. A SEASON'S OWN VIDEOS (only TMDB lists
any) lead the menu of a panel opened on that season (`videos.season_first`); a tab
pressed later draws its season's own in its pane, aimed at the same one player,
because a tab is fetched once and a menu it swapped would name the wrong season
when an earlier tab is pressed again. WHAT THE DESCRIBER SAYS ABOUT EPISODES IS
PRESENTED ONCE (`panel.presented`, which takes the answering service): the episode
airing next under the show's description, only while it is still ahead, and a
Finale / Mid-season finale mark on an episode by that service's OWN word
(`Provider.episode_mark` into `base.EPISODE_MARKS`; Trakt says "season_finale",
TMDB "finale" — one never read as the other). THE TITLE'S LOGO is the panel's
heading where the describer has one: out of band into the shell's `#detailsTitle`
through the image proxy (`base.LOGO_PARAMS`, `output=png` — TMDB serves some logos
as SVG, which the proxy rasterises, measured in Chromium and Firefox), alt the
title; a title with none keeps the text heading the opening click wrote. Nothing
of it is on a calendar card. ITS OUTBOUND BUTTONS AND A CARD'S ARE
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
new — filed IN PROCESS, bounded like the filed month (`_NEW_LOADS_MAX`,
`_NEW_KEYS_MAX`, half an hour), because only this process's late days read it and
written to the database it waited its turn before the shell could draw (3.2 s
behind a restart's refills, MEASURED); its chip is redrawn out of band (`_day_chip.html`); and its add-to-library
buttons arrive in their state. The page keeps no marks of its own — a mark posts
and its answer redraws every card for that title — so a day landing while a mark
is in flight is drawn from whichever reached the database first, and the other's
answer redraws it. THE SHELL FILES THE MONTH IT DREW (`_file_month`: each card's
day, title and every spelling a mark names it by, equal values one object per
page, in process) under a page token every mark and late day carries, and a mark
recounts the month's numbers and chips from it while drawing its title's own
rows — a mark that read the month again took 1.5 s. THE STORE IS BOUNDED BY WHAT
IT HOLDS (`_FILED_CARDS_MAX` cards in all, as well as `_PAGES_MAX` pages and
`_PAGE_SECONDS`, pruned whenever it is touched): an unfiltered month files
~18,000 cards, and 256 of those pages were 3.4 GB. A BIG DAY IS DRAWN IN PAGES
(app/calendar/day_pages.py, `DAY_PAGE_CARDS`) — the shell's inline day and a late
day alike (`_draw_day`, the one drawing of a day): its first page, then a
"Show the other N" control whose answer (`calendar_day_rest`, `_day_rest.html`)
is the rest of its cards in the control's place. THE FIRST PAGE IS A RANKING
(`day_pages.first_page`): the cards more of the viewer's own services list first
(`View.listed`), then the cards showing more of their title (`View.complete`, a
count of `query.COUNTED_FACTS` asked for a day over a page alone — `query.ranked`,
one statement over the combinations the judging reads, never the month's read),
then the day's order — and both pages are drawn in the day's order. PAGING, NEVER CAPPING: the
day's count, chip and is-new marks, the filed month and the month-changed check
are all the WHOLE day's — a day is read whole and drawn in part — and the rest
compares the day with what was filed exactly as a late day does. The control
carries ONE SIGNED TOKEN (app/signed_tokens.py, the sealing the picture report
token shares) naming the account, the day, WHICH PLACES IN THE DAY were drawn
and the page's references — the places, not the ranking again, because the
ranking reads descriptions the page's own view queues, and ranked again at the
press a card described meanwhile could be drawn twice or not at all; another
account's or an edited one draws nothing but a sentence. AND THE DAY THE PLACES
WERE COUNTED IN (`day_pages.cards_digest`, its cards' rows in order): the token
lives a day and the filed month hours, so a press on a day that has changed
since says the month changed by its own digest, whatever is still filed. A jump's card is drawn
wherever it ranks in its day. A DAY THAT ARRIVES AFTER ITS MONTH CHANGED draws the
store as it is now — the only thing it can draw: a card that went has no rows — and
when what it draws is not what the shell filed for that day, it says so for the
whole page, out of band (`_month_changed.html`). The page also asks once a minute
(`calendar_changed`): 204, reading nothing, until a store has rewritten a window of
the month since the page last knew it (`entries.changed_since`), and then the
month is read keys-only and compared card for card with what was filed, so only a
change this viewer would see raises the sentence. A timer and no filter
expression, which is what the CSP admits; htmx 4 stops a poll only by the element
being replaced. The add-to-library rule lives in one place per question:
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
has no season count and its own episode total. Those are the title store's
words for that season of the title (`labels.board_columns`), so
`core._redefine_row` — the one write of an existing row's season — changes the
season alone (and `_ARRIVING_FACTS` where an arrival knows them): there is no
copy on the row to rewrite, and none of the old definition's numbers to carry
across.
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
back carrying entries two months past its end. Trakt declares `Reach.WINDOW`: its
answer is authoritative for the window asked and nothing past it, so what lies
beyond is dropped before storage (`cache.in_span`, and again in
`entries.store_span`) and takes nothing off — it is some of those weeks'
entries, never all, and a listing filed in a window no coverage row speaks for
would never be read and would be taken off unseen by its own window's fill.

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
"already here". AN ENTRY THAT IS ONE SEASON OF ITS TITLE — Simkl files a later
anime season as a title of its own — is offered as that season alone, and which
season it is is the TITLE STORE'S (`seasons.{service}_id` / `{service}_named`,
`descriptions.named_season`): filed from the service's season list whenever one is
read (a refresh pass, a picker's lookup) and read back by the tracker's and the
board's pickers through `metadata.picked`, so an add re-reading a lookup the
response cache has let go still knows it. Ticked seasons post as one flat field each, `season:<n>`
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
filter the calendar applies, through the read's own judging (`query.described_kept`,
the described record standing in for its service's group in `query.judge`), and
BEFORE a stored season is offered as a link to its day. A season the calendar
ALREADY HOLDS is judged by the record it holds, through the calendar's read itself
(`query.titles`) with the viewer's source
choice (`calendar/search.py`'s `_drawn`), never by the describing service's
answer: the two can disagree on a filter (a stored record with no country under
a country filter), and a link judged by the description opens a day that draws
nothing. The jump names the card it lands on whether or not it wrote one. WHO
LISTS A SEASON'S CARD IS KNOWN ONLY ONCE IT IS WRITTEN, so the jump makes the week
it is filed in current first (`cache.ensure_window`, the fill the month's own load
would make) and judges the card as that month draws it; one the viewer's filters
hide — "only one service lists it" — is answered with the picker again, in its
own slot, the season refused and a sentence saying why, never a month with
nothing to land on (the jump form's `hx-target`; a navigation is a 204).

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
test reaching the network; `tests/support.py` owns the database, the client, the
base classes and the synchronous `load_settings` / `save_settings` a test calls
(the app's are coroutines over `db.run` / `db.transaction`, and nothing outside
app/db.py takes a connection itself — DriverIsolationTests). A new test file
should need no setup of its own. A test about what the calendar READS stores its
records and reads them back through the app's own read (`seed_records`,
`read_views` / `read_cards` / `read_kept`, `CardTestCase`), so the read under test
is the one every page uses and not a copy of its rules.
