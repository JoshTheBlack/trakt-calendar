"""The setup every test file used to build for itself.

conftest.py owns the things that must happen before collection (the data
directory, the import path, the outbound-network guard). This module owns the
things a test file asks for BY NAME: where the repo is on disk, the origin the
app is configured for, a database nothing else is using, and the two base
classes most of the suite's unittest classes were each re-declaring.

WHY NOT ALL FIXTURES: most of this suite is unittest.TestCase, and a pytest
fixture cannot be requested from one. Rewriting nine hundred assertions into
pytest style to reach the fixtures would be a far larger change than the
duplication it removes, and the base classes below give the same three lines of
setup one home either way. New tests can use conftest's fixtures instead; both
run in the same process against the same data directory.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import shutil
import unittest
from html import unescape
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The one temp directory conftest created before anything imported app.config,
# which is therefore the directory app.config.DATA_DIR is bound to for the whole
# process. Test databases go in it; a file's own mkdtemp would be a second
# directory the app never looks at.
TMP = Path(os.environ["TRAKT_DATA_DIR"])

# The base URL the app is configured for in tests. It is https because the
# session cookie is Secure by default, and it is sent as Origin because the
# app's cross-site rules refuse a mutating request without one.
ORIGIN = "https://testserver"

_db_counter = itertools.count(1)

# The Argon2 hasher the app builds for itself, captured by conftest before it
# swaps in a cheap one for the duration of the run. Everything in the suite
# hashes with the cheap one; this is here so that the COST of the real one stays
# assertable, since it is a security property and nothing else exercises it now.
PRODUCTION_HASHER = None


def calendar_records(entries, endpoint):
    """Raw calendar entries, as the Records a source hands the cache.

    The calendar cache stores each source's NORMALIZED record, so a test that
    wants "these shows air in this window" has to say it in records. Fixtures
    stay written as the payload a real source returns — that is what makes them
    checkable against a captured response — and this is the one place they are
    turned into what the cache actually sees.
    """
    from app.providers.trakt import calendar as trakt_calendar
    return [r for r in (trakt_calendar.to_record(e, endpoint.media) for e in entries)
            if r is not None]


def window_fetch(entries):
    """A stand-in for app.calendar.cache.fetch_window_records.

    `entries` is either the raw entries every window should answer with, or a
    callable (endpoint, start) -> raw entries for a test that wants one window to
    differ from another. The answer names EVERY SOURCE THAT COULD HAVE ANSWERED
    THIS ENDPOINT as having replied — `trakt` always, `simkl` for the four
    endpoints its calendar covers, and `tmdb` for its three when the test's
    Settings hold a TMDB read token (it is in play only then) — because that is
    what a real fill against the registered sources returns, and because the
    caller records who it ASKED independently: a stub answering for fewer
    sources than are in play stores a window that was asked-but-silent, and the
    read path then marks the span partial. A test exercising a genuinely partial
    fetch (one source refused) wants that, and builds its own stub instead of
    this one.

    Patching HERE rather than at the transport keeps the floor filter and the
    window trim out of the way, which is what a test about the read path wants;
    a test about the fill itself patches the client instead and gets both.

    Every record it hands back is attributed to `trakt`, which is what makes a
    span read the same whichever sources answered; a test about per-viewer source
    selection needs records from two services and builds them itself.
    """
    async def fetch(endpoint, settings, start, *, covered=()):
        from app.providers.simkl import _SimklProvider
        from app.providers.tmdb import _TmdbProvider

        rows = entries(endpoint, start) if callable(entries) else entries
        sources = ["trakt"]
        if _SimklProvider.capabilities.answers(endpoint.key):
            sources.append("simkl")
        if (_TmdbProvider.capabilities.answers(endpoint.key)
                and getattr(settings, "tmdb_configured", False)):
            sources.append("tmdb")
        # (records, answered, unchanged) — the stub never reports a source as
        # unchanged, because a test that wants that case is testing the
        # conditional GET and builds its own stub for it.
        return calendar_records(rows, endpoint), sources, []
    return fetch


def new_db_path(prefix: str = "test") -> Path:
    """Point app.db at a database no other test is using, and return its path.

    Counted rather than named after the caller: two classes that picked the same
    name would share a file, and a test that left a row behind would then fail
    its neighbour instead of itself.
    """
    from app import db

    path = TMP / f"{prefix}-{next(_db_counter)}.db"
    db.set_db_path(path)
    return path


_template_path: Path | None = None


def _schema_template() -> Path:
    """A migrated but empty database, built once, to be COPIED per test.

    Migrating from nothing costs ~60ms, and almost every test in this suite
    wants a fresh database, so the suite was replaying the same forty-odd
    migration steps well over a thousand times to arrive at the identical empty
    schema. Copying the finished file instead costs about a millisecond.

    This is safe only because the migrations are a pure function of the code:
    they build schema and seed no rows that vary by clock, environment or
    randomness. A migration that inserted a timestamp would be frozen at
    whenever the template was built — if one is ever added, it belongs after the
    copy, not inside it.

    Checkpointed and closed before it is used as a source, because the app runs
    SQLite in WAL mode: the schema lives in a sibling -wal file until then, and
    copying the main file alone would hand every test a database that had never
    been migrated.
    """
    global _template_path
    if _template_path is None:
        from app import db

        path = TMP / "schema-template.db"
        db.set_db_path(path)
        asyncio.run(db.migrate())
        asyncio.run(db.run(lambda conn: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")))
        db.close_thread_connection()
        _template_path = path
    return _template_path


def migrated_db(prefix: str = "test") -> Path:
    """new_db_path plus the migrations, for a synchronous test."""
    template = _schema_template()
    path = new_db_path(prefix)
    shutil.copyfile(template, path)
    return path


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """A freshly migrated database per test, for async tests with no HTTP."""

    async def asyncSetUp(self):
        self.db_path = migrated_db(type(self).__name__.lower())

    async def asyncTearDown(self):
        from app import db

        db.close_thread_connection()


PASSWORD = "hunter2hunter2"


class AppTestCase(unittest.TestCase):
    """A migrated database, saved settings and a TestClient, per test.

    The client speaks https because the session cookie is Secure by default and
    a client honouring that will not send it back over plain http, and it sends
    an Origin header because the app refuses a mutating request without one.
    Both were re-explained in a comment in most of the copies this replaces.

    Override `make_settings` to change what is saved — what the copies actually
    differed on was whether public_base_url was set.
    """

    def make_settings(self):
        from app.config import Settings

        # AN INSTANCE WITH NOTHING SET UP, AND THAT NOW HAS TO BE SAID OUT LOUD
        # FOR THE ONE SOURCE THAT NEEDS NOTHING. Simkl's calendar is public CDN
        # files, so it is a usable calendar source on a Settings with every
        # credential blank — which is correct for the app and wrong for a shared
        # fixture: it would have every page render in this suite reach for a real
        # CDN, and conftest's network guard is what would tell you, one file at a
        # time. A test that wants that source switches it back on.
        return Settings(simkl_public_calendar_enabled=False)

    def setUp(self):
        from app.config import save_settings
        from app.main import app

        self.db_path = migrated_db(type(self).__name__.lower())
        save_settings(self.make_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def tearDown(self):
        from app import db

        self.client.close()
        db.close_thread_connection()

    def make_user(self, username: str, password: str = PASSWORD, **flags) -> int:
        """An account with whatever grants the test needs."""
        from app import auth
        from app.config import Settings

        return asyncio.run(auth.create_user(
            username=username, password=password, settings=Settings(), **flags))

    def link_identity(self, user_id: int, provider: str, provider_user_id: int,
                      access_token: str | None = None) -> None:
        """Give an account a linked provider identity, as a real handshake would.

        Written straight to the table rather than through a flow: the flows have
        their own tests, and everything that gates on "has this person linked
        Trakt?" only needs the row to exist.

        A SIMKL TOKEN IS NORMALIZED TO LOOK LIKE A V2 ONE, unless it is already
        empty (a test deliberately asking for "linked, but no usable token") or
        already carries the real prefix. simkl_routes.access_token_for_user
        reads a token's local SHAPE to tell a v2 grant from a legacy v1 one it
        must never send (the app keeps no V1 registration to send it with) — see app/auth/simkl.py's
        V2_ACCESS_TOKEN_PREFIX — and every caller here is asking for an
        ordinarily-working link, not testing that specific legacy-token path
        (tests/auth/test_simkl_login.py's LegacyTokenTests does that directly,
        against auth.insert_linked_identity, bypassing this helper).
        """
        from app import auth, db
        from app.auth import simkl as simkl_auth

        if (provider == "simkl" and access_token
                and not access_token.startswith(simkl_auth.V2_ACCESS_TOKEN_PREFIX)):
            access_token = simkl_auth.V2_ACCESS_TOKEN_PREFIX + access_token

        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider=provider,
            provider_user_id=provider_user_id, access_token=access_token)))

    def sign_in_as(self, user_id: int) -> str:
        """Put a real session cookie on the client, skipping the login form.

        Does NOT clear the jar first: the session cookie replaces itself by
        name, and some tests are about what a handshake cookie set earlier does
        once a session exists. A test that wants a clean jar says so itself.
        """
        from app import auth

        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)
        return session_id


# ---------------------------------------------------------------------------
# The tracker's month, read the two ways there are to read one.
# ---------------------------------------------------------------------------
class RewatchPrompt:
    """The re-watch question, read back out of the markup it is now drawn as.

    IT USED TO BE A `needs_decision` OBJECT the browser turned into a dialog, and
    every fact in it is still here — the question is drawn open over a month left
    exactly as it was, so the whole answer is out of band and this reads it.
    `asked` is the one thing every caller checks: whether the add stopped to ask
    rather than writing something.

    THE DAYS COME BACK AS THEY ARE SPELLED FOR A READER, because that is what the
    server now sends: `completed_on` was an ISO day the browser formatted, and
    the formatting moved with the markup.
    """

    _OPEN = re.compile(r'<dialog id="rewatchModal"[^>]*data-open-modal')
    _FINISHED = re.compile(r"Your history says you finished (.*?) season (\d+)\s*"
                           r"on (.*?)\.</p>", re.S)
    _FROM = re.compile(r'id="rewatchFrom"[^>]*value="([^"]*)"')
    _PRESET = re.compile(r'data-fill-value="([^"]*)"')
    # A preset carries its DAY and no longer its spelling: pressing one fills the
    # field, and the field asks the server what the button should say.
    _ASKS_LABEL = re.compile(r'id="rewatchFrom"[^>]*hx-get="([^"]*)"', re.S)
    _SUBMIT = re.compile(r'id="rewatchSubmit".*?>(.*?)</button>', re.S)
    _EPISODE = re.compile(r"<li>S\d+E(\d+)\s*<span class=\"rewatch-when\">([^<]*)</span>")
    _POST = re.compile(r'hx-post="([^"]*)"')

    def __init__(self, html: str):
        self.html = html

    @property
    def asked(self) -> bool:
        return bool(self._OPEN.search(self.html))

    @property
    def label_source(self) -> str:
        """Where the submit button's wording comes from as the field is typed
        into. Empty when the field asks nobody, which is the shape this replaced:
        a date formatter in the browser."""
        found = self._ASKS_LABEL.search(self.html)
        return found.group(1) if found else ""

    @property
    def title(self) -> str:
        found = self._FINISHED.search(self.html)
        return " ".join(found.group(1).split()) if found else ""

    @property
    def season(self) -> int | None:
        found = self._FINISHED.search(self.html)
        return int(found.group(2)) if found else None

    @property
    def finished_on(self) -> str:
        """The day the previous run ended, as the reader is shown it."""
        found = self._FINISHED.search(self.html)
        return " ".join(found.group(3).split()) if found else ""

    @property
    def suggested_from(self) -> str:
        """The day a fresh run is offered from — the date field's own value."""
        found = self._FROM.search(self.html)
        return found.group(1) if found else ""

    @property
    def presets(self) -> list[str]:
        """The two days either side of the restart, or [] where nothing
        restarted and the plainer question is asked."""
        return self._PRESET.findall(self.html)

    @property
    def episodes(self) -> list[tuple[str, str]]:
        """(episode number, the day it was watched) for what has been seen since
        the run turned back."""
        return self._EPISODE.findall(self.html)

    @property
    def submit_label(self) -> str:
        """What the button that starts a fresh run SAYS as the question opens.

        The field arrives pre-filled, so a label that named no day — or named
        one only after somebody touched the field — was the prompt offering one
        answer and announcing another."""
        found = self._SUBMIT.search(self.html)
        return " ".join(found.group(1).split()) if found else ""

    @property
    def resubmit(self) -> str:
        """Where answering re-posts. Both buttons send the same address: the
        route that asked IS the add, and it now has the answer it was missing."""
        found = self._POST.findall(self.html)
        return found[0] if found else ""


def restore_backup(client, document: dict, ack: str = "REPLACE MY DATA"):
    """POST a tracker export back, the way the page does.

    THE DOCUMENT IS ONE FIELD OF A FORM. Reading the chosen file is the one thing
    only the browser can do — app/authz.py's shape guard admits application/json
    alone — so the text arrives in a hidden input beside the typed
    acknowledgement, and everything else about the restore is markup. Two callers
    drive it, and each writing that shape out itself is two places to fix.
    """
    return client.post("/api/distrakt/restore",
                       json={"payload": json.dumps(document), "ack": ack})


_IMPORT_PICK = re.compile(r'name="(pick:[^"]+)"')


def import_everything_offered(client, year: int, month: int):
    """Press Import and keep every box ticked, the way the dialog opens: read the
    offer the page would draw, then post every row of it. Returns the answer.

    A MONTH IS NOT FILLED BY OPENING IT, and the import files only what is picked,
    so a test that means "import the month" has to say which — and "all of them"
    is what the dialog's default states."""
    offer = client.get(f"/distrakt/fragments/import?year={year}&month={month}")
    picks = {unescape(name): "1" for name in _IMPORT_PICK.findall(offer.text)}
    return client.post(f"/api/distrakt/import?year={year}&month={month}", json=picks)


async def import_offered(user_id: int, month_key: str, settings) -> int:
    """`calendar_import.import_premieres` with every offered title chosen — the
    store-level twin of `import_everything_offered`."""
    from app.distrakt import calendar_import, store
    doc = await store.load_month(user_id, month_key)
    if doc is None:
        return 0
    offer = await calendar_import.import_candidates(user_id, doc, settings)
    return await calendar_import.import_premieres(
        user_id, month_key, settings, {calendar_import.candidate_ref(r) for r in offer.offered})


def tracker_month(user_id: int, year: int, month: int, *, fresh: bool = False) -> dict:
    """The payload a month is computed into, called directly.

    THERE IS NO LONGER A ROUTE THAT HANDS THIS BACK. The tracker's page reads a
    RENDERED month (GET /distrakt/fragments/month) and every mutation answers
    with the same markup, so the JSON that used to be both the page's state and
    a test's window into a month is gone — it was state only because the rows
    were assembled in the browser.

    WHAT THAT SPLITS IS WHICH QUESTION A TEST IS ASKING, and the split is worth
    having. A test about what a month CONTAINS — which bucket a season landed
    in, what its counts say, which service could not be read — asks this, the
    function that works it out, with no HTTP and no markup in the way. A test
    about what a VIEWER is told asks the fragment and reads its markup, the way
    every details-panel test already does.
    """
    from app.auth import viewer
    from app.distrakt import routes as distrakt_routes

    async def _run() -> dict:
        settings = await viewer.settings_for_user(user_id)
        payload, _status = await distrakt_routes._distrakt_month_payload(
            user_id, year, month, settings, force_fresh=fresh)
        return payload

    return asyncio.run(_run())


class TrackerMonth:
    """A rendered month fragment, read back out of its own markup.

    The same bargain DetailsPanel makes one panel down: what a viewer is told is
    now markup, so a test that is about what the page SAYS reads the page.
    """

    def __init__(self, html: str):
        self.html = html

    @property
    def headings(self) -> list[str]:
        """Every section head and weekday head, in the order they are drawn."""
        return re.findall(r'<div class="distrakt-(?:bucket|weekday)-head">([^<]*)</div>',
                          self.html)

    @property
    def titles(self) -> list[str]:
        """Every row's title, in the order the page draws them."""
        return re.findall(r'<span class="tt">([^<]*)</span>', self.html)

    def section(self, label: str) -> list[str]:
        """The titles drawn under one heading, up to the next heading."""
        parts = re.split(r'<div class="distrakt-(?:bucket|weekday)-head">([^<]*)</div>',
                         self.html)
        for index in range(1, len(parts), 2):
            if parts[index] == label:
                return re.findall(r'<span class="tt">([^<]*)</span>', parts[index + 1])
        return []

    @property
    def notice(self) -> str:
        """What the degraded-month banner says, or "" when it is hidden."""
        found = re.search(r'<div id="distraktNotice"[^>]*>(.*?)</div>', self.html, re.S)
        return unescape((found.group(1) if found else "").strip())

    @property
    def banner(self) -> str:
        """What the read-only banner says, or "" when the month is editable."""
        found = re.search(r'<div class="distrakt-frozen-note">(.*?)</div>', self.html, re.S)
        return unescape((found.group(1) if found else "").strip())

    @property
    def toast(self) -> str:
        """The line this answer flashed, or ""."""
        found = re.search(r'<div class="toast [^"]*"[^>]*>(.*?)</div>', self.html, re.S)
        return unescape((found.group(1) if found else "").strip())

    @property
    def asks(self) -> list[str]:
        """The text of each question drawn above the list."""
        return [unescape(re.sub(r"<[^>]+>", "", block)).strip()
                for block in re.findall(
                    r'<span class="distrakt-unknown-text">(.*?)</span>', self.html, re.S)]

    def posts_to(self) -> list[str]:
        """Every address this fragment's controls post to."""
        return re.findall(r'hx-post="([^"]*)"', self.html)


# ---------------------------------------------------------------------------
# The details panel, read back out of its own markup.
# ---------------------------------------------------------------------------
class DetailsPanel:
    """What a rendered details fragment (templates/_details.html) says.

    THE ROUTES THAT DRAW IT ANSWER WITH THE PANEL RATHER THAN WITH A PAYLOAD, so
    a test asserting what a viewer is told has to read the markup. Three test
    files need the same handful of readings — which episodes carry a tick, whose
    tick it is, where it points — and a copy of these patterns in each of them
    would be three things to fix the day a class name changes.

    Deliberately not an HTML parser: what is asserted here is the panel's own
    class names, which ARE the contract between the template and the stylesheet,
    so a reading that stopped matching is a change worth noticing.
    """

    _ROW = re.compile(r'<div class="ep-row(?P<flags>[^"]*)">(?P<body>.*?)</div>', re.S)
    _NUM = re.compile(r'<span class="ep-num">E(\d+)</span>')
    _TICK = re.compile(
        r'<(?:a|span) class="ep-check svc-(?P<source>[a-z]+)(?P<flags>[^"]*)"'
        r'(?P<attrs>[^>]*)>', re.S)
    _HREF = re.compile(r'href="(?P<url>[^"]*)"')

    def __init__(self, html: str):
        self.html = html

    def _rows(self):
        for match in self._ROW.finditer(self.html):
            number = self._NUM.search(match.group("body"))
            if number:
                yield int(number.group(1)), match.group("flags"), match.group("body")

    @property
    def episodes(self) -> list[int]:
        return [number for number, _flags, _body in self._rows()]

    @property
    def watched(self) -> list[int]:
        return [number for number, flags, _body in self._rows() if "watched" in flags]

    @property
    def by_source(self) -> dict[str, list[int]]:
        """{service: the episodes its tick is drawn FILLED on}, which is the
        rendered form of what each service recorded."""
        found: dict[str, list[int]] = {}
        for number, _flags, body in self._rows():
            for tick in self._TICK.finditer(body):
                if "unseen" not in tick.group("flags"):
                    found.setdefault(tick.group("source"), []).append(number)
        return found

    @property
    def unasked(self) -> list[str]:
        """The services whose ticks are drawn as NOT ASKED — no id for this
        title — rather than as an answer."""
        return sorted({tick.group("source") for tick in self._TICK.finditer(self.html)
                       if "unasked" in tick.group("flags")})

    @property
    def services(self) -> list[str]:
        """Every service a tick is drawn for, filled or hollow."""
        return sorted({tick.group("source") for tick in self._TICK.finditer(self.html)})

    @property
    def tick_links(self) -> dict[str, str]:
        """{service: where its first tick points}, omitting a service whose ticks
        are drawn as plain text because the title carries no id for it."""
        found: dict[str, str] = {}
        for tick in self._TICK.finditer(self.html):
            url = self._HREF.search(tick.group("attrs"))
            if url and tick.group("source") not in found:
                # Unescaped, because what is being asserted is the ADDRESS a
                # press goes to and not how an attribute spells an ampersand.
                found[tick.group("source")] = unescape(url.group("url"))
        return found

    _TAB = re.compile(
        r'<input type="radio" name="detailsSeason" class="ep-season-radio"\s*'
        r'id="epSeason-(?P<number>\d+)"(?P<attrs>[^>]*)>', re.S)
    _TAB_GET = re.compile(r'hx-get="(?P<url>[^"]*)"')

    @property
    def season_tabs(self) -> list[int]:
        """The seasons the picker offers, in the order it draws them. Empty where
        there is nothing to pick between — one season draws no strip."""
        return [int(tab.group("number")) for tab in self._TAB.finditer(self.html)]

    @property
    def open_tab(self) -> int | None:
        """The season whose rows came with the panel."""
        for tab in self._TAB.finditer(self.html):
            if "checked" in tab.group("attrs"):
                return int(tab.group("number"))
        return None

    @property
    def tab_urls(self) -> dict[int, str]:
        """{season: where its tab fetches its rows}, omitting the open one — that
        tab carries no request at all, because its rows are already here."""
        found: dict[int, str] = {}
        for tab in self._TAB.finditer(self.html):
            url = self._TAB_GET.search(tab.group("attrs"))
            if url:
                # Unescaped, because what is asserted is the ADDRESS a press goes
                # to and not how an attribute spells an ampersand.
                found[int(tab.group("number"))] = unescape(url.group("url"))
        return found
