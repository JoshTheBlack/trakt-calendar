"""One account's source preferences, read and written.

Backs the `source_prefs` table. Five facts live here:

  - CALENDAR SOURCE and TRACKER SOURCE: which services each half of the app
    asks. Separately, because they are separate decisions — somebody can
    reasonably want every service's calendar and only one service's idea of what
    they have watched. The two halves also read `auto` differently, and
    `admits_calendar` below is where that is written down.
  - PER-ENDPOINT CALENDAR SOURCE: the same choice again, narrowed to one
    calendar. `endpoint_sources` below says why one account-wide value is not
    enough.
  - PRECEDENCE: when two services fill the same field with different values,
    whose value the viewer sees. Resolved at READ over already-cached data, so
    changing it is instant and invalidates nothing. `field_order` is the whole
    of what this module decides about it; what the FIELDS are is
    app/calendar/resolve.py's vocabulary, and deliberately not restated here.
  - TRACKER PRIORITY: which LINKED tracker decides, when several answer for one
    season. Separate from PRECEDENCE beside it because they settle different
    arguments — precedence picks whose description of a title a viewer reads,
    this one picks whose COUNT the bucket rule acts on and a frozen month keeps.
    `tracker_order` is the whole of it, and it is a reordering of the services
    that already answer rather than a selection among them.

AN ACCOUNT WITH NO ROW HAS NO OPINION, and `load` returns the defaults for one
rather than creating anything. That is what keeps this free for the overwhelming
majority of accounts, which have linked one service and will never open the
screen: no row is written until somebody states something.

I/O IS THE TWO VERBS AT THE BOTTOM. Everything above them is a pure function of
values the caller already holds, so "does this preference admit Simkl?" can be
answered in a test without a database.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from .. import db
from ..providers.base import Source

# "Whatever there is to ask", stated by nobody. THE DEFAULT, and what it comes
# out to differs by half: for the TRACKER it follows the links, so linking a
# second service starts reading it without anybody being asked to state
# anything and unlinking one quietly stops; for the CALENDAR it is every source
# the instance can fill from, because no link is spent reading one. See
# `admits` and `admits_calendar`.
#
# AUTO IS THE ONE SELECTION THAT GROWS. Registering a third service widens what
# `auto` comes out to, and that is correct precisely because nobody stated it:
# it has always meant "whatever there is", never "these two".
AUTO = "auto"

# A STATED SET OF SERVICES is spelled by naming them, joined by SEPARATOR:
# "trakt", "simkl", "trakt+simkl". A single name is simply a one-element set, so
# the values this column has always held are already in the new spelling and
# nothing had to be rewritten to introduce it.
#
# THE SET NEVER GROWS, and that is the difference from `auto`. Somebody who named
# the services they wanted has not agreed to a service that did not exist when
# they chose, so registering a third leaves their calendar exactly as they left
# it. The two shapes together are the whole vocabulary: `auto` for "whatever
# there is, now and later", a named set for "these, and only these".
SEPARATOR = "+"

# THE LEGACY SPELLING OF ONE PARTICULAR SET, kept because rows in the field carry
# it. It reads as exactly the two services that existed when it could be written
# — never as "all", which would hand a third service to somebody who chose from a
# menu of two. Nothing writes it any more; naming the services is how a set is
# stated now, and this exists so that an account that chose `both` before that
# was possible keeps the choice it actually made.
BOTH = "both"
LEGACY_BOTH = frozenset({"trakt", "simkl"})

# The service names come from Source rather than being spelled again here, so a
# service the app does not know about cannot be stored as a preference for it.
SOURCE_NAMES = frozenset(str(s) for s in Source)

# The selections that are a single word. A named SET of two or more services is
# also valid and is not enumerable, so `is_selection` rather than membership here
# is what a caller validating user input should ask.
SELECTIONS = frozenset({AUTO, BOTH, *SOURCE_NAMES})

DEFAULT_SELECTION = AUTO


def named_sources(selection: str) -> frozenset[str] | None:
    """The services `selection` names, or None for "do not narrow at all".

    None is `auto` and only `auto` — the answer that depends on something this
    function does not have (who is linked, what the instance can fill from), so
    it is deliberately handed back rather than guessed at. Both callers below
    resolve it, differently, and that difference is the two halves of the app.

    ANYTHING UNPARSEABLE READS AS None, i.e. as the default. A row written by a
    newer version of the app naming a service this one has never heard of must
    not stop a page rendering; the widest answer is the safe one to degrade to,
    because it is what an account that has stated nothing already gets.
    """
    text = str(selection or "")
    if text == BOTH:
        return LEGACY_BOTH
    named = {part for part in text.split(SEPARATOR) if part}
    if named and named <= SOURCE_NAMES:
        return frozenset(named)
    return None


def is_selection(value) -> bool:
    """Whether `value` is something this module can store and act on."""
    text = str(value or "")
    return text == AUTO or named_sources(text) is not None


def canonical_selection(value: str) -> str:
    """`value` respelled in declared source order, so that "simkl+trakt" and
    "trakt+simkl" are one stored value rather than two that behave identically.

    Left alone for `auto` and for the legacy `both`, neither of which is a list.
    """
    named = named_sources(value)
    if str(value) in (AUTO, BOTH) or named is None:
        return str(value)
    return SEPARATOR.join(str(s) for s in Source if str(s) in named)


def admits(selection: str, source: Source | str, linked) -> bool:
    """Whether `selection` says to ask `source`, given the services `linked`.

    THE TRACKER'S PREDICATE. Reading one person's viewing history means asking a
    service for THEIR data with THEIR token, so under `auto` — "follow the
    links" — a service this account has no identity for has nothing to be asked
    for and is not asked. `admits_tracker` is the only caller; the calendar
    answers a different question, and `admits_calendar` below says why.

    `linked` is the set of services this account actually has an identity for.
    It is passed in rather than looked up because who is linked is auth's fact,
    not this module's, and reading it here would put a query behind what is
    otherwise a comparison.
    """
    name = str(source)
    named = named_sources(selection)
    if named is None:
        return name in {str(s) for s in linked}
    return name in named


@dataclass(frozen=True)
class SourcePrefs:
    """One account's whole row, or the defaults when it has none.

    Frozen, and `save` takes a whole one: the fields are read together on every
    path that wants any of them, and a partial write verb would need a "leave
    this alone" sentinel for each. `dataclasses.replace` is how a caller changes
    one.
    """
    user_id: int
    calendar_source: str = DEFAULT_SELECTION
    tracker_source: str = DEFAULT_SELECTION
    precedence: dict = field(default_factory=dict)
    # {endpoint key: selection}. See `calendar_selection`.
    endpoint_sources: dict = field(default_factory=dict)
    # Tracker service names, most trusted first. See `tracker_order`, which is
    # the whole of what it does. Empty means "no opinion" and leaves the app's
    # declared order standing, which is what every account had before this
    # existed — it is NOT a claim that no service decides.
    tracker_priority: list = field(default_factory=list)
    # Tracker service names whose STORED numbers this account no longer counts.
    # See `counts_tracker`. Empty means "count everything", which is what every
    # account had before this existed.
    tracker_retired: list = field(default_factory=list)

    def calendar_selection(self, endpoint=None) -> str:
        """Which services answer for `endpoint`, falling back to the
        account-wide `calendar_source` when this account has said nothing about
        that particular calendar.

        WHY ONE ACCOUNT-WIDE VALUE IS NOT ENOUGH, measured rather than supposed:
        on one real August, Simkl contributed 1773 movie records against Trakt's
        46, because Simkl's movie calendar is a global release calendar and
        Trakt's is a curated one. The same account's SHOW calendar is where Simkl
        adds coverage that is plainly worth having. Those are opposite answers
        about one service, and a single selection can only give one of them — so
        somebody would be choosing between an unreadable movies page and losing
        Simkl's shows entirely.

        THE OVERRIDE IS PER CALENDAR, NOT PER VIEW. It is keyed on the endpoint
        key (`app/endpoints.py`), which is also what a stored window is keyed on,
        so "which services answer for movies" is a question with one answer
        wherever it is asked. `endpoint=None` means the question was asked
        without one and gets the account-wide value, which is what every caller
        that predates this got.

        PRECEDENCE IS DELIBERATELY NOT PER ENDPOINT. The reason this one is comes
        from two calendars having genuinely different shapes; whose spelling of a
        title wins does not change between them, and an override nobody needs is
        a screen control nobody can explain.
        """
        if endpoint is None:
            return self.calendar_source
        stated = (self.endpoint_sources or {}).get(str(endpoint))
        if isinstance(stated, str) and is_selection(stated):
            return stated
        return self.calendar_source

    def admits_calendar(self, source: Source | str, endpoint=None) -> bool:
        """Whether this account's calendar reads `source`, on `endpoint`.

        IT TAKES NO `linked`, AND THAT IS THE WHOLE DIVERGENCE FROM `admits`.
        A calendar is fetched with the INSTANCE's credentials or with none at
        all — Trakt's windows go out under this instance's client id and secret,
        and one source's calendar files are static public JSON needing nothing —
        so no viewer's identity is spent reading one, and there is no credential
        for a link to supply. Gating on links would make a signed-in account see
        LESS than an anonymous visitor to a share link on the same instance,
        which is backwards, and would take Trakt's calendar away from somebody
        whose only link happens to be to the other service.

        So `auto` here means "every source this INSTANCE can fill from", not
        "every source this account has linked". A STATED selection — the services
        named — is still exactly what it says and is honoured whatever is linked;
        this only ever widens the default.
        """
        named = named_sources(self.calendar_selection(endpoint))
        return True if named is None else str(source) in named

    def admits_tracker(self, source: Source | str, linked) -> bool:
        return admits(self.tracker_source, source, linked)

    def field_order(self, field_name: str, sources) -> list[str]:
        """`sources` reordered so the one this account wants for `field_name`
        comes first — THE WHOLE OF WHAT A PRECEDENCE PREFERENCE DOES.

        It is a REORDERING and never a filter, so a preference can only decide
        which of several answers is shown, never remove the only one there is. A
        viewer who prefers a service that did not describe this title still sees
        the title.

        The document is `{"default": <source>, "fields": {<field>: <source>}}`:
        the per-field entry leads, then the account's default, then whatever
        order the caller handed in, which is the app's declared source order.
        Anything unrecognized — a field this version does not have, a service it
        has never heard of, a document that is not shaped like this at all — is
        simply not found in `sources` and falls out, leaving the declared order.
        That is the same degrade-to-the-default rule `_stored_selection` follows
        and for the same reason: a row written by a newer version of the app must
        not stop an older one rendering a page.

        WHAT A FIELD IS is app/calendar/resolve.py's vocabulary, not this
        module's, and it is not restated here — a second list of field names
        would be a second thing to keep in step with `Record`.
        """
        names = [str(s) for s in sources]
        document = self.precedence if isinstance(self.precedence, dict) else {}
        fields = document.get("fields")
        preferred = []
        stated = (fields or {}).get(field_name) if isinstance(fields, dict) else None
        for candidate in (stated, document.get("default")):
            if isinstance(candidate, str) and candidate in names and candidate not in preferred:
                preferred.append(candidate)
        return preferred + [name for name in names if name not in preferred]

    def tracker_order(self, sources) -> list[str]:
        """`sources` reordered so the tracker this account trusts most comes
        first — WHICH LINKED SERVICE DECIDES when several answer for one season.

        THE SAME REORDERING RULE AS `field_order`, AND FOR THE SAME REASON: it
        can only decide which of several answers leads, never remove the only
        answer there is. A season only one service knows about is still that
        service's number whatever this says, so an account that names Trakt first
        does not lose the seasons only Simkl holds. That is what makes one
        preference work across a roster of mixed rows — some from both services,
        some from either alone — without a rule per row.

        `sources` IS ALREADY THE SET THAT ANSWERS FOR THIS ACCOUNT — the linked,
        admitted, credentialled ones (watch_history.tracker_sources) — so a
        service named here but not linked is simply absent from `names` and
        cannot decide anything. THAT IS THE WHOLE FIX for a service deciding from
        a number it left behind when its link lapsed: unlinking removes it from
        the candidates, so the next one down decides, and re-linking hands it
        back.

        A name this version does not recognise falls out on the way past, and an
        account that has stated nothing gets `sources` back untouched — the
        caller's own order, which is the app's declared one. Same degrade rule as
        everything else here, for the same reason: a row written by a newer
        version must not stop an older one rendering a page.
        """
        names = [str(source) for source in sources]
        stated = self.tracker_priority if isinstance(self.tracker_priority, list) else []
        preferred = []
        for candidate in stated:
            if isinstance(candidate, str) and candidate in names and candidate not in preferred:
                preferred.append(candidate)
        return preferred + [name for name in names if name not in preferred]


    def counts_tracker(self, source: Source | str) -> bool:
        """Whether this account still counts what `source` reported.

        THE EXIT FROM A STATE THAT HAD NONE. Unlinking a service stops it being
        ASKED, which already worked; it could not stop the numbers it had already
        contributed from counting. Those sit in the watch state, per source, and
        every row that ever had one goes on rendering it — correctly flagged as
        belonging to a service nobody asked, and with no way to ever stop. An
        account that has genuinely migrated then reads as permanently degraded
        instead of as a healthy single-service account. This is how it says so.

        IT REMOVES AN ANSWER EVEN WHEN IT IS THE ONLY ONE, which is the one place
        this deliberately parts company with `tracker_order` above. That one can
        only decide which of several answers LEADS, because reordering a single
        answer is meaningless. This one is a statement that a service's numbers
        are not to be used at all — and a title only the retired service ever knew
        is precisely the row carrying the stalest number of the lot, so exempting
        it would leave the migration half-done and unexplainable. The row says
        "retired, not counted" rather than going quiet, so a count that drops to
        nothing is visible and reversible rather than mysterious.

        IT IS ABOUT STORED NUMBERS, NOT ABOUT ASKING. A retired service that is
        still linked is still read — this says what to do with the answer, not
        whether to fetch it — so re-counting it later needs nothing refetched.
        """
        stated = self.tracker_retired if isinstance(self.tracker_retired, list) else []
        return str(source) not in {str(name) for name in stated}

    def retired_trackers(self, sources=None) -> frozenset[str]:
        """The retired names, optionally narrowed to `sources`.

        A frozenset because every caller asks "is this one in it" per row, and
        because the order of a set of exclusions means nothing — unlike
        `tracker_priority`, where the order IS the statement.
        """
        stated = self.tracker_retired if isinstance(self.tracker_retired, list) else []
        names = {str(name) for name in stated if isinstance(name, str)}
        if sources is None:
            return frozenset(names)
        return frozenset(names & {str(source) for source in sources})


def _stored_tracker_priority(document) -> list[str]:
    """The stated tracker order read back, or an empty list.

    Empty on anything unreadable, which reads as "no opinion" and leaves the
    declared order standing — the same answer an account that never opened the
    screen gets, and nothing here that cannot be restated by opening it again.
    Entries are kept as written rather than filtered against the registry:
    `tracker_order` already ignores a name it cannot place, and dropping an
    unknown service HERE would quietly forget a preference belonging to a
    provider this version happens not to have registered.
    """
    if not document:
        return []
    try:
        parsed = json.loads(document)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(name) for name in parsed if isinstance(name, str)]


def _tracker_priority(value) -> list[str]:
    """A supplied tracker order, validated on the way IN.

    REFUSED RATHER THAN COERCED, exactly as `_selection` is and for the same
    reason: an order naming a service this app has never heard of is a bug in
    the caller, and silently dropping it would hide the screen sending the wrong
    name. Duplicates are refused too — an order that names one service twice has
    no single meaning.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"tracker_order must be a list of service names, not {value!r}")
    names = [str(name) for name in value]
    unknown = [name for name in names if name not in SOURCE_NAMES]
    if unknown:
        raise ValueError(
            f"tracker_order names {', '.join(sorted(unknown))}, which is not among "
            f"{', '.join(sorted(SOURCE_NAMES))}")
    if len(set(names)) != len(names):
        raise ValueError(f"tracker_order names a service more than once: {names}")
    return names


def _tracker_retired(value) -> list[str]:
    """The retired-tracker list, validated on the way IN.

    Same rules as `_tracker_priority` above and for the same reasons — an unknown
    service name is a bug in the caller rather than something to swallow — with
    duplicates TOLERATED rather than refused, because this is a set of exclusions
    and naming one twice says exactly what naming it once says. Order carries no
    meaning here either, so it is stored sorted and reads back the same whatever
    order the screen sent.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"tracker_retired must be a list of service names, not {value!r}")
    names = {str(name) for name in value}
    unknown = [name for name in names if name not in SOURCE_NAMES]
    if unknown:
        raise ValueError(
            f"tracker_retired names {', '.join(sorted(unknown))}, which is not "
            f"among {', '.join(sorted(SOURCE_NAMES))}")
    return sorted(names)


def _selection(value, column: str) -> str:
    """A stored or supplied selection, validated.

    REFUSED RATHER THAN COERCED on the way IN — a preference nobody can satisfy
    is a bug in the caller and silently rewriting it to 'auto' would hide it. On
    the way OUT of the database an unknown value falls back to the default
    instead, because a row written by a newer version of the app must not stop an
    older one from rendering a page.
    """
    text = str(value or "")
    if not is_selection(text):
        raise ValueError(
            f"{column} must be {AUTO} or services named from "
            f"{', '.join(sorted(SOURCE_NAMES))}, not {text!r}")
    return canonical_selection(text)


def _stored_selection(value) -> str:
    text = str(value or "")
    return text if is_selection(text) else DEFAULT_SELECTION


def _stored_endpoint_sources(document) -> dict:
    """The per-endpoint overrides read back, with anything unusable dropped.

    Dropped rather than defaulted per entry: an endpoint whose override this
    version cannot read falls back to the account-wide selection, which is what
    an account that never stated one already gets.
    """
    parsed = _stored_precedence(document)
    return {str(key): value for key, value in parsed.items()
            if isinstance(value, str) and is_selection(value)}


def _stored_precedence(document) -> dict:
    """The precedence map read back, or an empty one.

    Empty on anything unreadable rather than raising: with no map every field
    falls to its seeded default, which is exactly what an account that has never
    opened the screen already gets. There is nothing here that cannot be restated
    by opening it again.
    """
    if not document:
        return {}
    try:
        parsed = json.loads(document)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def load(user_id: int) -> SourcePrefs:
    """This account's preferences, or the defaults if it has stated none."""
    row = await db.fetch_one(
        "SELECT calendar_source, tracker_source, precedence_json, endpoint_sources_json, "
        "tracker_order_json, tracker_retired_json FROM source_prefs WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return SourcePrefs(user_id=user_id)
    return SourcePrefs(
        user_id=user_id,
        calendar_source=_stored_selection(row["calendar_source"]),
        tracker_source=_stored_selection(row["tracker_source"]),
        precedence=_stored_precedence(row["precedence_json"]),
        endpoint_sources=_stored_endpoint_sources(row["endpoint_sources_json"]),
        tracker_priority=_stored_tracker_priority(row["tracker_order_json"]),
        # Same tolerant read as the order beside it: unreadable means "count
        # everything", which is the state an account that never opened the
        # screen is in and nothing that cannot be restated by opening it.
        tracker_retired=_stored_tracker_priority(row["tracker_retired_json"]),
    )


async def save(prefs: SourcePrefs) -> SourcePrefs:
    """Write the whole row, creating it if this account had none.

    Returns what was stored, so a caller that built its argument with `replace`
    does not have to re-read to know what it now holds.
    """
    calendar_source = _selection(prefs.calendar_source, "calendar_source")
    tracker_source = _selection(prefs.tracker_source, "tracker_source")
    precedence = prefs.precedence or {}
    if not isinstance(precedence, dict):
        raise ValueError("precedence must be an object")
    endpoint_sources = prefs.endpoint_sources or {}
    if not isinstance(endpoint_sources, dict):
        raise ValueError("endpoint_sources must be an object")
    # Refused rather than coerced, the same way a bad column value is: an
    # override nobody can satisfy is a bug in the caller, and quietly dropping it
    # would leave a screen showing a choice that was never stored.
    endpoint_sources = {str(key): _selection(value, f"endpoint_sources[{key}]")
                        for key, value in endpoint_sources.items()}
    tracker_priority = _tracker_priority(prefs.tracker_priority)
    tracker_retired = _tracker_retired(prefs.tracker_retired)
    await db.execute(
        "INSERT INTO source_prefs (user_id, calendar_source, tracker_source, "
        "precedence_json, endpoint_sources_json, tracker_order_json, "
        "tracker_retired_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "calendar_source = excluded.calendar_source, "
        "tracker_source = excluded.tracker_source, "
        "precedence_json = excluded.precedence_json, "
        "endpoint_sources_json = excluded.endpoint_sources_json, "
        "tracker_order_json = excluded.tracker_order_json, "
        "tracker_retired_json = excluded.tracker_retired_json",
        (prefs.user_id, calendar_source, tracker_source, json.dumps(precedence),
         json.dumps(endpoint_sources), json.dumps(tracker_priority),
         json.dumps(tracker_retired)),
    )
    return replace(prefs, calendar_source=calendar_source,
                   tracker_source=tracker_source, precedence=precedence,
                   endpoint_sources=endpoint_sources,
                   tracker_priority=tracker_priority,
                   tracker_retired=tracker_retired)
