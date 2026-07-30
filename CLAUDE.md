# CLAUDE.md

Self-hosted FastAPI app: a Trakt-backed TV/movie calendar, a watch tracker, and
a ranking board. One process, SQLite, server-rendered Jinja + HTMX. README.md
covers install and operation; this file covers the things a change can silently
break.

## Invariants — each one lives somewhere; go read that before changing it

Every entry names its home. An invariant with no stated owner is how three
modules quietly stopped restating one and a survey read that as a hole.

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
`app/providers/trakt/transport.py`'s `cached_get` is for, and why a provider
package separates public per-title lookups (`detail.py`) from per-user reads
(`sync.py`): "does this module cache?" then has one answer per module.

**The calendar cache stores the UNFILTERED window once per (endpoint, 7 days).**
Per-viewer filtering happens at READ time, so one viewer excluding a genre does
not poison what another sees from the same rows (`app/calendar_cache.py`). The
one thing filtered BEFORE storage is the instance-wide content floor, which is
deliberate and documented there.

**Trakt does not honour the `days` bound it is given** — a 7-day window has come
back carrying entries two months past its end. Windows are trimmed before
storage; without the trim a month read shows the same airing several times.

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
.venv/Scripts/python.exe -m pytest -q          # full suite, ~4 minutes
.venv/Scripts/python.exe -m pytest -q tests/calendar/test_cache.py
```

Run targeted files while iterating and the full suite before committing.
`tests/` mirrors `app/`, so where a test lives says which part it covers.
`tests/conftest.py` owns the data directory and an autouse guard that fails any
test reaching the network; `tests/support.py` owns the database, the client and
the base classes. A new test file should need no setup of its own.
