"""The live computation: a stored roster record plus what the provider says
about it right now, merged into the flat shape the renderers read.

ONE JOB — turn records into shows. It reads (watch history, season detail) and
writes nothing, which is what lets the month payload, the freeze pass and the
rollover bucketing all call it without any of them knowing about the others.
"""
from __future__ import annotations

import asyncio
import logging

from . import counts, discord_fmt
from .. import providers
from ..perftrace import span
from ..sources import prefs as source_prefs
from .store import Bucket, record_key

logger = logging.getLogger(__name__)

# The fields a stored record carries a LAST-KNOWN copy of, and that a live pass
# normally replaces. Named once because the degraded path below falls back to
# exactly this set — a list that drifted would render a mix of live and stale
# numbers with nothing saying which was which.
_LIVE_FIELDS = ("total", "cadence", "premiere", "finale", "started_airing", "finished_airing")


def live_key(rec: dict) -> tuple[str, int]:
    """The (title, season) pair the watch-history lookups are keyed on.

    The flat item key rather than the triple, because these lookups cross a JSON
    boundary (the cached watch state) where a tuple cannot survive as a dict key.
    """
    return (str(record_key(rec)), int(rec["season"]))


def detail_source(rec: dict, settings) -> str | None:
    """Which service is asked how long a season is, or None when none can be.

    DECIDED BY WHICH ID THE RECORD CARRIES, in the registry's declared order, and
    NOT by which services the account has linked. A season's episode total is
    CATALOGUE data — the same for everyone, no token involved — so a title known
    to Trakt is asked of Trakt whether or not this viewer signed in with it, and
    a Simkl-only title is asked of Simkl. Tying it to the linked accounts would
    make a public fact depend on a private one and leave a Simkl-only roster
    showing every season as having no episodes.

    AND THE FIRST SOURCE THAT CAN ACTUALLY BE ASKED, NOT SIMPLY THE FIRST ONE
    NAMED. A record's ids say who KNOWS the title; whether this instance holds
    that service's catalogue credential is a separate fact, and reading only the
    first made a row carrying two ids give up on the second answer it had in
    hand. Blanking one service's client id then emptied almost an entire roster
    on an instance whose other service could have answered most of it. None means
    every source this record names is one this instance cannot ask right now (or
    the record names none at all), which is a state to RENDER — see
    `_merge_unavailable` — rather than a zero to write down.

    ONE SOURCE PER TITLE, NOT BOTH. Paying a second catalogue call per season to
    discover the two services count episodes slightly differently would spend the
    instance's budget on a disagreement nothing renders: what a viewer is shown
    two of is what they have WATCHED (see counts.py), which is a fact about them
    and genuinely differs.

    THROUGH THE CALENDAR'S OWN `detail_source.choose`, which is already this
    question with this answer: the same ids, the same declared order and the same
    per-port catalogue predicate, written for the detail modal on both the
    signed-in and the public calendar. A second implementation here would be a
    second place for "who can describe this title" to drift, and the drift would
    be invisible — each side renders something plausible on its own page.
    """
    # Function-local for the name, not for a cycle: this module's own function is
    # called `detail_source` too, and a module-level import would have to be
    # aliased into something neither name means.
    from ..calendar import detail_source as catalogue_answerer
    chosen = catalogue_answerer.choose(settings, rec.get("ids") or {})
    # A STORED-ONLY ANSWERER IS NOT ONE THIS CAN USE. That one means "nobody can
    # be reached, but the modal can still draw what is on disk" — and a season
    # lookup is a live call, so following it would ask an unconfigured source
    # over the network, which is the whole thing this function exists to stop.
    return str(chosen.source) if chosen and not chosen.stored_only else None


def named_sources(rec: dict) -> list[str]:
    """Every registered source this record carries an id for, in declared order,
    whether or not this instance can ask it anything.

    The pair to `detail_source` above and the reason the two are separate: that
    one answers "who can be asked", this one answers "who KNOWS this title", and
    the difference between them is exactly the sentence a row has to say when it
    could not be refreshed — a service that is absent from the settings reads
    nothing like a service that is down.
    """
    ids = rec.get("ids") or {}
    return [str(source) for source in providers.registered()
            if str(ids.get(str(source)) or "").strip()]


# Why one row's counts are not this pass's. Three states rather than a boolean,
# because they call for different things from the reader and from the operator:
# a service that could not be reached wants another go, one this instance holds
# no credential for wants Settings opened, and a record no registered source has
# an id for wants neither — nothing an operator does will count it.
UNREACHABLE = "unreachable"
NOT_CONFIGURED = "not_configured"
UNANSWERABLE = "unanswerable"


def and_list(names) -> str:
    """Several names in one sentence: "A", "A and B", "A, B, and C".

    WRITTEN FOR ANY NUMBER RATHER THAN FOR TWO, because two is what this instance
    happens to have registered today and nothing about it is a rule — the registry
    is a set a source joins by registering, and a sentence that reads "both" or
    joins on " and " alone starts lying the moment a third one exists. The
    template side has its own copy of this question for source MARKS
    (_source_logo.html's `names`); this is the Python one, and they are separate
    because neither language can call the other's.
    """
    names = [str(name) for name in names if name]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def counting_sources(show: dict) -> list[str]:
    """Every service a row's numbers actually came from, in declared order.

    BOTH HALVES OF "x/y" AND THEY CAN DIFFER. The episode total comes from ONE
    source (see detail_source — paying two catalogue calls to discover they count
    slightly differently would spend the budget on a disagreement nothing
    renders), while the watched count can come from every service the viewer has
    linked. So a row is frequently counted by more services than answered for its
    total, and a reader asking "where did this come from" wants all of them.
    """
    named = {*(show.get("total_by_source") or {}), *(show.get("watched_by_source") or {})}
    return [name for name in source_order() if name in named]


def unasked_sources(show: dict, asked=()) -> list[str]:
    """The services whose number on this row was NOT read this pass, in declared
    order.

    A ROW'S NUMBERS AND THIS PASS'S READS ARE DIFFERENT SETS, which is the thing
    that made a row lie. `watched_by_source` comes off the STORED watch state, so
    a service that has ever been synced goes on contributing a number long after
    its credential was removed — and a row naming it read "up to date, read from
    Trakt and Simkl" over one number nobody had asked for. This is the difference
    between the two sets, and it is what the row has to say out loud.

    Empty when nothing was asked, rather than "everything is stale": a caller that
    did not say what it read (a frozen month, a test) has told us nothing about
    these numbers, and inferring staleness from its silence would put the mark on
    every row of a month that is not waiting on anybody.
    """
    names = {str(name) for name in asked}
    if not names:
        return []
    return [name for name in counting_sources(show) if name not in names]


def fresh_note(show: dict, *, missing=(), asked=(), retired=()) -> str:
    """One row's sentence when its counts ARE this pass's — the tooltip behind
    the mark that says so.

    A ROW SAYS WHETHER ITS NUMBERS ARE CURRENT, WHICHEVER WAY THE ANSWER GOES,
    and the pair matters more than either half: a mark that appears only when
    something is wrong is one a reader has to already know the meaning of, while
    a mark that is always there and changes colour is read at a glance. The words
    are the server's for `unavailable_note`'s reason, and they NAME THE SERVICES,
    which is the question a two-source instance actually raises — a single number
    on a row says nothing about whether one service answered or both agreed.

    `missing` is any service that WAS asked for this account and could not be
    read. Its absence does not make the row's stored numbers wrong, but it does
    make "up to date" untrue: what is on the page is what could be read without
    it, and the mark has to say so.

    `asked` is which services were read at all, and it separates a number that is
    THIS PASS'S from one that is merely the last one taken. A service nobody asked
    — its credential removed since the sync that stored its count — did not fail
    and is not missing, so neither of the two sentences above is true of it: the
    row went on claiming its numbers were "read from" a service that was never
    contacted. Such a number is still worth showing, and it is still that
    service's, but the sentence says which half of the row it applies to.

    `retired` is a service this ACCOUNT has said it no longer counts. It is left
    out of both halves above rather than given a clause of its own: the sentence
    exists to say whether the numbers ON the row are current, and a retired
    service's number is not on the row any more. Saying "Simkl's number is the
    last one read" about a number that is no longer counted would describe a
    staleness that has stopped mattering — the tooltip's per-service lines are
    where the decision is named, once, beside the number it applies to.
    """
    labels = source_labels()
    retired_names = {str(name) for name in retired}
    counted = [name for name in counting_sources(show)
               if name not in retired_names]
    if missing:
        # THE SERVICE THAT WENT QUIET IS NOT ONE OF THE SURVIVORS, obvious as
        # that sounds: its name is still on the row's `total_by_source` when the
        # season lookup was served from cache, so counting it here said "Trakt
        # could not be read, so these counts are Trakt and Simkl's alone" — a
        # sentence that contradicts itself inside its own clause.
        absent = and_list(labels.get(name, name) for name in missing)
        spoke = and_list(labels.get(name, name) for name in counted
                         if name not in set(missing))
        return (f"{absent} could not be read just now, so these counts are "
                + (f"{spoke}'s alone." if spoke else "the last ones read."))
    stored_only = [name for name in unasked_sources(show, asked)
                   if name not in retired_names]
    read_from = and_list(labels.get(name, name) for name in counted
                         if name not in set(stored_only))
    if stored_only:
        # TWO CLAUSES, because the row is two statements: what was read now, and
        # what is being shown from last time. One sentence covering both would
        # have to pick a tense for numbers that do not share one.
        kept = and_list(labels.get(name, name) for name in stored_only)
        tail = (f"{kept}'s number is the last one read." if len(stored_only) == 1
                else f"{kept}'s numbers are the last ones read.")
        return (f"Counts are up to date, read from {read_from}. {tail}"
                if read_from else tail)
    if not read_from:
        return "Counts are up to date."
    return f"Counts are up to date, read from {read_from}."


def unavailable_note(rec: dict, settings, *, asked: str | None) -> str:
    """One row's own sentence for why its counts are the stored ones — the whole
    sentence, composed here, drawn by the browser and nowhere composed twice.

    THE ROW STILL DRAWS ITS NUMBERS, so this is a note ON a row rather than a
    replacement for one: it is the tooltip behind the row's mark, while the page
    says the same thing once, out loud, through `unavailable_notices`. It used to
    be a bare `unavailable` boolean with the words living in JavaScript, which had
    one register for three states and no way to tell an outage from a missing
    credential. Two hardcoded strings in the browser with a flag to choose
    between them would have been the same fault again, so the server says the
    sentence and the client escapes it — the shape `routes._unkeyable_reason`
    already uses for the other refusal a viewer reads.

    `asked` is the source that WAS asked and failed, or None when nobody could be
    asked at all — the caller knows which of the two happened and this function
    cannot work it out afterwards.
    """
    labels = source_labels()
    if asked is not None:
        return f"{labels.get(asked, asked)} could not be reached — these are the last counts read."
    knows = [labels.get(name, name) for name in named_sources(rec)]
    if not knows:
        # No id any registered source issues, so there is nobody to configure
        # that would help — see providers.base.MATCH_SOURCES for how a record
        # comes to be filed under an id no service can be asked by.
        return "No registered service knows this title, so nothing can count it."
    joined = knows[0] if len(knows) == 1 else " and ".join(knows)
    verb = "isn't" if len(knows) == 1 else "aren't"
    return (f"{joined} {verb} configured on this instance, so these are the last "
            f"counts read.")


def unavailable_reason(rec: dict, *, asked: str | None) -> str:
    """Which of the three states above a degraded row is in."""
    if asked is not None:
        return UNREACHABLE
    return NOT_CONFIGURED if named_sources(rec) else UNANSWERABLE


def unavailable_notices(shows, *, unreadable=()) -> list[str]:
    """What the PAGE says about what it could not refresh, as finished sentences.

    ONLY THE TRANSIENT SILENCE IS SAID OUT LOUD, and deciding that is the point
    of this function existing rather than the page enumerating every reason it
    can find. A service that could not be READ is news: it was working, it is not
    now, and the numbers on the page are short of it until it comes back. A
    service this instance holds no credential for is not news and never becomes
    news — it is a standing fact about the instance, most viewers cannot act on
    it (only an administrator can fill a credential in), and an instance that has
    deliberately configured one service would carry a banner about the other for
    ever. That fact belongs on the rows it applies to, where it is a mark and a
    tooltip rather than a permanent bar across the page, and the same goes for a
    title no registered service can look up.

    `unreadable` IS THE OTHER SILENCE, HANDED IN. A service whose HISTORY could
    not be read is the caller's own finding (watch_history.unreadable_sources)
    and this module cannot see it — but to a reader it is the same sentence as a
    service whose EPISODE COUNTS could not be read, so the two are joined here
    rather than rendered as two notices saying one thing. That join used to be
    the browser's, which is why the wording could not be checked against the
    rule that chooses it.
    """
    labels = source_labels()
    down = {str(name) for name in unreadable}
    down |= {show.get("unavailable_source") for show in shows
             if show.get("unavailable")
             and show.get("unavailable_reason") == UNREACHABLE}
    named = [labels.get(name, name) for name in source_order() if name in down]
    if not named:
        return []
    # Worded so it is true whether or not anything else answered. When a second
    # service did, the counts on the page are its alone; when nothing did, they
    # are the last ones written down. Either way the honest statement is that
    # this service is not in them.
    return [and_list(named) + " could not be read just now — the counts below "
            "are only what could be read without it."]


async def season_detail(settings, rec: dict, *, fresh: bool = False, client=None):
    """One record's season summary, from whichever source can answer for it.

    PUBLIC BECAUSE THE ADD ROUTES ASK IT TOO. "How long is this season" is one
    question with one answer, and the manual add used to ask Trakt directly —
    which handed a Simkl-only title `fetch_season_detail(None, ...)` and stored
    it with no episode total at all. A second source-picking rule beside
    `detail_source` would be a second place for "who can answer for this record"
    to drift, so there is one and both the live pass and the add routes call it.
    """
    from ..providers import season as season_rules
    from ..providers.simkl import detail as simkl_detail
    from ..providers.trakt.detail import fetch_season_detail
    ids = rec.get("ids") or {}
    source = detail_source(rec, settings)
    if source == "simkl":
        return await simkl_detail.fetch_season_detail(
            settings, ids.get("simkl"), rec["season"], rec.get("media") or "show")
    if source is None:
        return season_rules.empty_season(int(rec["season"]))
    return await fetch_season_detail(settings, ids.get("trakt"), rec["season"],
                                     fresh=fresh, client=client)


async def network_for(settings, rec: dict) -> str:
    """Who broadcast this title, from whichever source can answer for it — "" when
    none can.

    BESIDE `season_detail` AND PICKING ITS SOURCE THE SAME WAY, because it is the
    same kind of question: a public catalogue fact about a title, decided by which
    ids the record carries and which of those services this instance can actually
    ask (`detail_source`). That is what makes it work on a Simkl-only instance,
    where reaching for Trakt first would answer nothing.

    WHY IT IS A SECOND CALL AND NOT A PARAMETER ON THE ONE ALREADY BEING MADE.
    Asked of both services' docs and then measured against both live, because a
    free ride would have been worth having:

      SIMKL — GET /tv/episodes/{id} takes no `extended` at all. Its own
      conventions page says the parameter "is still accepted for backward
      compatibility, but it's a no-op", and that is exactly what it does:
      identical bytes and identical keys with and without it.

      TRAKT — GET /shows/{id}/seasons/{n} was tried with every `extended` value
      the API has (none, full, metadata, full,metadata, episodes, full,episodes,
      full,images). It answers a list of EPISODES; no value adds the parent show,
      and `network` appears nowhere in any of them.

      THE NEAR MISS WORTH WRITING DOWN: the ALL-seasons call,
      GET /shows/{id}/seasons?extended=full, does carry a `network` key on each
      season object — but it is empty on every show sampled (Severance, Beastars,
      Better Call Saul all null; The Walking Dead ""), while each of those shows
      states a real network at the SHOW level. So it is a field Trakt publishes
      and does not fill, and reading it would have looked right in review and
      returned nothing in production.

    So the network is a fact about the SHOW and has to be asked for as one. The
    season lookup answers how long a season is and when it aired, which is a
    different question, and should not be widened into this one.

    WHO NEEDS IT: a row added from a history prompt. Every other way onto the
    roster arrives with a network already — a search hit carries one, a calendar
    record carries one — so this is the one path with nowhere else to get it, and
    it is paid once, on a click, exactly as the season lookup beside it is.

    THE TWO SERVICES CAN LEGITIMATELY DISAGREE and that is not a defect to
    reconcile here: Simkl answers Beastars with "Fuji TV" (who aired it) and Trakt
    with "Netflix" (who carried it), both true. Whichever source the record's own
    ids and this instance's credentials select is the one that answers, which is
    the same answer the rest of the row is built from.
    """
    from ..providers.simkl import _naming as simkl_naming
    from ..providers.trakt.detail import fetch_show_summary
    ids = rec.get("ids") or {}
    source = detail_source(rec, settings)
    if source == "simkl":
        simkl_id = ids.get("simkl")
        if not simkl_id:
            return ""
        naming = await simkl_naming.fetch(settings, simkl_id)
        # Simkl fills the network in on the SERIES root and leaves it null on
        # every later season-title, so a season added from a prompt has to read
        # it off the series — see network_of_series, which is where that
        # measurement lives.
        return await simkl_naming.network_of_series(settings, simkl_id, naming)
    if source is None:
        return ""
    info = await fetch_show_summary(settings, ids.get("trakt"))
    return str((info or {}).get("network") or "")


async def fetch_season_details(settings, records: list[dict], *, fresh: bool,
                               allow_degrade: bool, sources: list | None = None) -> list:
    """One season lookup per record, in parallel, in the records' own order.

    `allow_degrade` decides what a single failure does: captured as a result the
    caller renders around, or raised so the caller aborts the whole pass. That is
    the only difference, and it is the caller's policy rather than this
    function's — see compute_live_shows.

    A RECORD NO SOURCE CAN BE ASKED ABOUT IS NOT ASKED, AND COMES BACK None. It
    is not a failure and must not be one — `allow_degrade=False` exists to abort
    on a lookup that MIGHT have worked, and a missing credential will not fix
    itself before the retry — but neither is it a season with no episodes. The
    old shape asked anyway, the unconfigured service answered nothing, and a
    zeroed season landed in a frozen month as though it were measured. None says
    "nobody was asked" and every caller renders or stores its record's own
    last-known fields instead.

    `sources` is `detail_source`'s answer per record when the caller has already
    worked it out (compute_live_shows needs it again, to say which service went
    quiet), and is computed here otherwise. One answer either way — asking twice
    is how the row's sentence and the lookup come to disagree.
    """
    from ..providers.trakt.transport import shared_client
    if sources is None:
        sources = [detail_source(rec, settings) for rec in records]
    # The app-wide shared client for the whole fan-out (no per-call client).
    client = shared_client()
    askable = [index for index, source in enumerate(sources) if source]
    answers = await asyncio.gather(*(
        season_detail(settings, records[index], fresh=fresh, client=client)
        for index in askable
    ), return_exceptions=allow_degrade)
    details: list = [None] * len(records)
    for index, answer in zip(askable, answers):
        details[index] = answer
    return details


def _merge_available(rec: dict, detail: dict, watched: dict[str, int], settings,
                     asked=(), *, missing=(), dates=None, retired=frozenset()) -> dict:
    show = {**rec, "key": str(record_key(rec)), "unavailable": False,
            "unavailable_source": "", "unavailable_reason": ""}
    show.update({field: detail[field] for field in _LIVE_FIELDS})
    _apply_counts(show, rec, watched, settings, asked, dates, retired)
    # AFTER the counts, not before: the note names the services the numbers came
    # from and those are what `_apply_counts` has just decided.
    #
    # FRESHNESS IS NOT `not unavailable`, AND IT IS NOT A BOOLEAN. This title's own
    # season lookup answered — that is what brought it here — but the numbers
    # beside it can still be short in two different ways, and a row that renders
    # them identically is telling the reader the same thing about two situations
    # they would act on differently:
    #   STALE   a service was ASKED and could not be read. Something is wrong right
    #           now; refreshing may fix it.
    #   PARTIAL every service asked answered, but a number on this row belongs to a
    #           service nobody asked — its credential is gone, so the number is the
    #           last one taken and no refresh will move it. Nothing is broken; the
    #           row is simply older than it looks in one place.
    # Both are false under a green mark, which is what "up to date" would claim.
    # THE SERVER NAMES THE STATE rather than shipping a flag for the browser to
    # branch on: the vocabulary and the sentence behind it are one decision.
    # A RETIRED SERVICE IS NOT A GAP, AND THIS IS THE WHOLE POINT OF RETIRING ONE.
    # "partial" means a number here belongs to a service nobody asked and no
    # refresh will move it — true of a retired service too, and the reason a
    # migrated account read as permanently degraded: every row it ever touched
    # stayed amber for ever with no way out. Once the account has said it no
    # longer counts that service, its number is not an unanswered question, it is
    # a decision — so the row goes green and the tooltip carries the explanation.
    stored_only = [name for name in unasked_sources(show, asked)
                   if name not in retired]
    show["counts_freshness"] = ("stale" if missing
                                else "partial" if stored_only else "current")
    show["counts_note"] = fresh_note(show, missing=missing, asked=asked,
                                     retired=retired)
    return show


def _merge_unavailable(rec: dict, watched: dict[str, int], settings, asked=(), *,
                       source: str | None, dates=None, retired=frozenset()) -> dict:
    """This one title's totals are not this pass's. Render it from its stored
    record's last-known fields and flag it, rather than presenting a fabricated
    0/0 as real.

    TWO WAYS TO GET HERE AND THE ROW SAYS WHICH. `source` is the service that was
    asked and could not answer — a rate limit, an outage — or None when no source
    this record names could be asked at all, which is a settings problem and not
    an outage. Both render identically otherwise, because the record's last-known
    numbers are the best answer in either case; what differs is
    `unavailable_reason`, which is what lets the page state each cause once, in
    its own words (see unavailable_notices).

    `unavailable_source` IS THE SERVICE THIS ROW WANTED, asked or not. Which one
    it is is decided per record (see detail_source), so the row is the only place
    that knows, and a page full of these otherwise says "unavailable" over and
    over without ever naming anybody. What must NOT be conflated is why it is
    silent, which is why the reason travels beside the name rather than being
    inferred from whether a name is there at all.
    """
    wanted = source or next(iter(named_sources(rec)), "")
    show = {**rec, "key": str(record_key(rec)), "unavailable": True,
            "counts_freshness": "stale",
            "unavailable_source": wanted,
            "unavailable_reason": unavailable_reason(rec, asked=source),
            "counts_note": unavailable_note(rec, settings, asked=source)}
    show.update({
        "total": int(rec.get("total") or 0),
        "cadence": rec.get("cadence"),
        "premiere": rec.get("premiere"),
        "finale": rec.get("finale"),
        "started_airing": bool(rec.get("started_airing")),
        "finished_airing": bool(rec.get("finished_airing")),
    })
    _apply_counts(show, rec, watched, settings, asked, dates, retired)
    return show


def stored_shows(records: list[dict], settings) -> list[dict]:
    """Every record rendered from what it already holds, with NO provider call at
    all — the whole-page version of what one degraded row does.

    FOR THE FALLBACK THAT USED TO DROP THE VIEWER'S LIST ENTIRELY. When a shared
    prerequisite fails there is no per-title answer to be had, and the page used
    to render the month's own records and leave the list off, on the stated
    reasoning that every row on it "would need the season lookup that has just
    failed". That reasoning does not survive a row being able to draw its own
    last-known counts: the seasons somebody is keeping up with are the whole
    point of the page, and having them vanish on a refresh and return on a
    reload is worse than showing the numbers they were showing a moment ago —
    which are real, and are the numbers the row had either way.

    Every row comes back marked not-current, with the sentence saying so, because
    that is exactly what they are.
    """
    shows = []
    for rec in records:
        source = detail_source(rec, settings)
        show = _merge_unavailable(rec, {source or "": int(rec.get("watched") or 0)},
                                  settings, (), source=source)
        show["bucket"] = discord_fmt.bucket_of(show, show)
        # No history was read on this pass, so nothing can say when a season was
        # finished; the stored record's own bucket is what it was last time.
        show.setdefault("completed_on", "")
        shows.append(show)
    return shows


def source_order() -> tuple[str, ...]:
    """The registry's declared source order, as bare names. The FIRST entry a
    season actually has a number from is that season's primary — the one number
    a frozen month and the announcement post carry.

    THE DEFAULT, FOR A CALLER WITH NO ACCOUNT IN HAND. Which order an account
    actually wants is that account's to state, and
    `watch_history.tracker_ports` is where the stated one is applied — once,
    centrally, and then passed down as an explicit order argument. Every caller
    on a path that knows whose page it is receives that instead, so this answers
    only where there is genuinely nobody to ask: a shared surface, a pre-warm, a
    default before a preference exists.
    """
    return tuple(str(source) for source in providers.registered())


def unreadable_detail_sources(shows) -> list[str]:
    """The services that could not answer a CATALOGUE question on this pass, in
    the registry's declared order.

    A SECOND WAY FOR A SOURCE TO GO QUIET, and it needs saying out loud because
    it looks nothing like the first. watch_history.unreadable_sources names a
    service whose HISTORY could not be read — one person's watched episodes. This
    names one whose EPISODE COUNTS could not be read, which is public catalogue
    data asked of whichever service the record carries an id for (see
    detail_source) and therefore fails independently of what the viewer linked.
    Both end up in the same banner because to a reader they are the same
    sentence: that service could not be reached, so what you are looking at is
    the other one's numbers.

    Without this, a whole roster whose only source is unreachable renders every
    row flagged unavailable with nothing anywhere on the page naming what to fix.

    A SERVICE THAT WAS NEVER ASKED IS NOT NAMED HERE, which is what
    `unavailable_reason` is read for: a record whose only source has no catalogue
    credential on this instance names that source too (it is the one the row
    wanted), and calling it unreadable would send an operator looking for an
    outage that is not there. It gets its own sentence in `unavailable_notices`.
    """
    down = {show.get("unavailable_source") for show in shows
            if show.get("unavailable") and show.get("unavailable_reason") == UNREACHABLE}
    return [name for name in source_order() if name in down]


def source_labels() -> dict[str, str]:
    """What to call each source on screen, read off the providers themselves so a
    badge can never spell a service differently from the rest of the app."""
    return {str(source): provider.label
            for source, provider in providers.registered().items()}


def _apply_counts(show: dict, rec: dict, watched: dict[str, int], settings,
                  asked=(), dates=None, retired=frozenset()) -> None:
    """Put the watched counts on a live show, in all three forms it is read in.

    `watched` is the primary source's number and is what every existing reader
    already asks for — the bucket rule, the Discord post, a frozen month's column
    — so it keeps meaning exactly what it always did. `watched_by_source` beside
    it is the whole picture, and `counts` is the string the row draws, which is
    one number when the services agree and both when they do not. The label is
    built HERE rather than in the browser because the rule for it is the same
    rule a frozen month is written with, and a copy in JavaScript could not be
    tested against the one that matters.

    `asked` is which services were read for this account, which is what tells a
    season only one of two of them knows about from a season both agree on — see
    counts.counts_label. It is threaded down rather than looked up here because
    it is one answer for the whole pass, not one per row.

    IT IS ALSO THE ORDER THE PRIMARY IS PICKED IN, and those are one fact rather
    than two: `asked` arrives most-trusted first (watch_history.tracker_sources),
    so the service that decides is the first one this ACCOUNT trusts that has a
    number for this season. The registry's order is the fallback for a caller
    that did not say what it asked — a frozen month re-rendered, a test — and
    that is exactly the behaviour every row had before an account could state a
    preference. Reading the registry here regardless is what let a service decide
    from a number it left behind after its link lapsed: it was never asked, no
    refresh would ever move its number, and it still outranked the service the
    viewer actually reads.
    """
    order = tuple(str(source) for source in asked) or source_order()
    total = int(show.get("total") or 0)
    # RESOLVED ONCE, HERE, BEFORE ANY OF THE THREE FORMS IS WRITTEN. A service can
    # report a title finished without itemizing it, and that answer travels as a
    # claim rather than a number because only this side holds the season's total
    # (counts.ALL_EPISODES). This is the point where the total arrives, so it is
    # the point where the claim becomes a count — and everything downstream, the
    # bucket rule and a frozen month's stored breakdown included, sees plain
    # numbers it can compare and store.
    per_source = counts.resolve(watched, total)
    # A RETIRED SERVICE'S NUMBER IS KEPT AND NOT COUNTED, and those are two
    # different things done in two different places on purpose. The account has
    # said it has moved off that service, so its stored numbers must stop deciding
    # anything — the primary count, the label, the bucket rule. They are NOT
    # deleted: the row still shows what that service last said, marked as retired,
    # because a number that vanished with no explanation is the same invisibility
    # the freshness states were built to remove, and because un-retiring has to be
    # able to put it straight back.
    counting = {name: value for name, value in per_source.items()
                if name not in retired}
    # `per_source or watched` so a caller holding one bare number instead of a
    # per-source map still gets it back, exactly as before.
    show["watched"] = counts.primary_count(counting or (watched if not retired else {}),
                                           order, total)
    show["watched_by_source"] = dict(per_source)
    show["retired_sources"] = [name for name in order if name in retired] or [
        name for name in per_source if name in retired]
    # The catalogue half, per source too, so a month frozen today can still say
    # which service's episode count it was measured against. One entry: a
    # season's total comes from one source (see detail_source). A record no
    # source could be asked about names none here — the total on it is its own
    # last-known number and no service is answering for it this pass.
    detail_from = detail_source(rec, settings)
    show["total_by_source"] = {detail_from: total} if detail_from else {}
    show["counts"] = counts.counts_label(counting or (watched if not retired else {}),
                                         total, source_labels(), order, asked)
    # THE SAME NUMBERS, SAID IN FULL, for the row's tooltip. One line has room
    # for the counts and nothing else, so a service nobody asked and a service
    # that agrees look identical in it — see counts.counts_detail, which is where
    # that is spelled out. Composed here beside the label so the two can never
    # name the services differently or disagree about what a number means.
    show["counts_detail"] = counts.counts_detail(
        per_source or watched, total, source_labels(), order, asked, dates,
        linked=asked, retired=retired)


def _log_watched_coverage(records: list[dict], watched_lookup: dict, matched: int) -> None:
    """X/Y diagnostic: distinguishes an EMPTY watched lookup (no progress
    returned) from a NON-empty lookup that simply doesn't line up with the stored
    records (an id/season key mismatch), by printing a small sample of each."""
    logger.info(
        "compute_live_shows: %d record(s), watched-lookup has %d key(s), %d matched",
        len(records), len(watched_lookup), matched,
    )
    if records and matched == 0:
        logger.warning(
            "compute_live_shows: 0/%d records matched a watched count. "
            "sample record keys=%s ; sample watched-lookup=%s",
            len(records), [live_key(r) for r in records[:6]],
            list(watched_lookup.items())[:6],
        )


async def compute_live_shows(user_id: int, records: list[dict], settings, fresh: bool = False,
                             watched_lookup: dict | None = None,
                             allow_degrade: bool = False,
                             completed_lookup: dict | None = None,
                             dates_lookup: dict | None = None,
                             sources_read=(), sources_unread=()) -> list[dict]:
    """Merge each stored record with its live Trakt-derived fields into the flat
    "LIVE SHOW SHAPE" discord_fmt expects (+ computed `bucket`).

    Watched counts (`x`) come from `user_id`'s incremental watch-history cache
    (watch_history.py) — the caller may pass a pre-synced `watched_lookup`
    (avoids re-syncing when it also needs the movies from the same state); if
    omitted we sync here. Totals/dates (`y`, cadence, premiere/finale) come from
    one season call per record; `fresh=True` bypasses the 24h season cache.

    `allow_degrade` decides what a per-title season-detail failure does. Off (the
    default, used by the freeze pass and rollover bucketing) a failure propagates
    so the caller aborts and retries later — never persisting a rate-limited 0/0 as
    a permanent frozen total. On (the live open-month view) a failed title is
    marked `unavailable` and rendered from its LAST-KNOWN stored fields instead: a
    429 on one title must not read as that title genuinely having 0 episodes, and
    it must not sink the rest of the roster. The shared-prerequisite sync (watched
    counts) is NOT degraded here — its failure still propagates for the caller's
    top-level handler, because it can't be pinned on any one title.

    `sources_read` IS WHICH SERVICES WERE ASKED FOR THIS ACCOUNT, and it belongs
    beside `watched_lookup` because it describes the same read: it is what lets a
    row tell "only one of the two services knows this season" from "both of them
    agree", which arrive as the same single number and mean different things (see
    counts.counts_label). A caller that hands in a pre-synced lookup knows what it
    asked and says so; one that leaves the sync to this function does not have to.

    `sources_unread` IS WHICH OF THOSE COULD NOT BE READ, and it is the same
    caller's finding for the same reason — the sync that produced `watched_lookup`
    is where a service goes quiet, and a row cannot tell from the lookup alone
    whether a missing count means "nothing watched" or "nobody could ask". It
    decides only what a row SAYS about itself: the counts are still whatever could
    be read, and the mark stops claiming they are current.

    Every show that comes back carries `key`, whether or not the record handed in
    did: it is what the browser names a row by, and deriving it here means a
    record assembled anywhere is addressable once it has been through this."""
    from . import watch_history
    if not records:
        return []

    # WHO ANSWERS FOR EACH RECORD, RESOLVED ONCE FOR THE WHOLE PASS. The fetch
    # needs it to know which records to skip, and every row that comes back
    # short needs it to say WHICH service went quiet; resolving it twice is how
    # a row's sentence comes to name a service the lookup never asked.
    sources = [detail_source(rec, settings) for rec in records]

    if watched_lookup is None:
        with span("cls.sync+seasons", n=len(records), fresh=fresh):
            state, details = await asyncio.gather(
                watch_history.sync_and_baseline(settings, user_id, records, force=fresh),
                fetch_season_details(settings, records, fresh=fresh,
                                     allow_degrade=allow_degrade, sources=sources),
            )
        # The floor a viewer set on a season they are re-watching is already
        # applied: sync_and_baseline takes the roster and filters the state it
        # returns, so every reader of it agrees. See its own note for why that
        # is there and not here.
        watched_lookup = watch_history.watched_map(state)
        completed_lookup = watch_history.season_completed_map(state)
        # PER SERVICE, unlike completed_lookup beside it, because the tooltip
        # names services and the two answer different questions — see
        # watch_history.season_dates_by_source.
        dates_lookup = watch_history.season_dates_by_source(state)
        # WHICH SERVICES ANSWER FOR THIS ACCOUNT — asked here because this branch
        # has just synced them, so it is one preference read on a path that has
        # already gone to the database. The other branch is handed it by the
        # caller that did the sync (`sources_read`), which keeps a pre-synced
        # call free of storage entirely.
        sources_read = sources_read or await watch_history.tracker_sources(settings, user_id)
    else:
        with span("cls.season_gather", n=len(records), fresh=fresh):
            details = await fetch_season_details(
                settings, records, fresh=fresh, allow_degrade=allow_degrade,
                sources=sources)
    asked = tuple(str(source) for source in sources_read or ())
    # Narrowed to what was actually ASKED for this account: a service nobody
    # asked did not go quiet, and naming it would put a stranger's outage on a
    # row that never wanted it.
    unread = tuple(name for name in asked if name in {str(s) for s in sources_unread})
    # WHICH SERVICES' STORED NUMBERS THIS ACCOUNT HAS RETIRED. Read once for the
    # whole pass rather than threaded down from every caller: it is one fact about
    # the account, this function already has the account in hand, and adding an
    # argument to each of the four callers would be four chances to forget it on
    # the path that matters. See app/sources/prefs.py's `counts_tracker`.
    retired = (await source_prefs.load(user_id)).retired_trackers()

    shows = []
    matched = 0
    unreachable = 0
    unaskable = 0
    for rec, detail, source in zip(records, details, sources):
        key = live_key(rec)
        if key in watched_lookup:
            matched += 1
        # `detail is None` is a record NOBODY COULD BE ASKED ABOUT — it was never
        # fetched (see fetch_season_details) — and an Exception is a service that
        # was asked and failed. They render the same way, from the record's own
        # last-known fields, and differ in the sentence the row draws.
        if detail is None or isinstance(detail, Exception):
            failed = isinstance(detail, Exception)  # allow_degrade path only
            unreachable += failed
            unaskable += not failed
            # No live count for this title: fall back to the number its stored
            # record last settled on, filed under the source that answers for it
            # so the row still renders one number rather than none. The source
            # the record NAMES when none could be asked — filing it under "" put
            # a number outside the declared order, where a row drawing two of
            # them could not have labelled it.
            fallback = {source or next(iter(named_sources(rec)), ""):
                        int(rec.get("watched") or 0)}
            show = _merge_unavailable(rec, watched_lookup.get(key) or fallback,
                                      settings, asked, source=source if failed else None,
                                      dates=(dates_lookup or {}).get(key),
                                      retired=retired)
        else:
            show = _merge_available(rec, detail, watched_lookup.get(key) or {},
                                    settings, asked, missing=unread,
                                    dates=(dates_lookup or {}).get(key),
                                    retired=retired)
        show["bucket"] = discord_fmt.bucket_of(show, show)
        # WHEN the season was finished, and only for a season that IS finished:
        # on a partly-watched season the same date is just "last time I watched
        # something", which must not read as a completion. "" = not finished, or
        # finished on a date the history cache cannot name.
        show["completed_on"] = (
            (completed_lookup or {}).get(key, "")
            if show["bucket"] == Bucket.COMPLETED else ""
        )
        shows.append(show)

    if unreachable:
        logger.warning(
            "compute_live_shows: %d/%d title(s) rate-limited/unreachable this pass; "
            "rendered from last-known totals and flagged unavailable.",
            unreachable, len(records),
        )
    if unaskable:
        # A SEPARATE LINE BECAUSE IT IS A SEPARATE PROBLEM, and the one an
        # operator can actually fix: these titles are known only to services this
        # instance holds no catalogue credential for, so no amount of retrying
        # will count them. Nothing was requested for them either.
        logger.warning(
            "compute_live_shows: %d/%d title(s) name no source this instance can "
            "ask (catalogue credentials missing); rendered from last-known totals.",
            unaskable, len(records),
        )
    _log_watched_coverage(records, watched_lookup, matched)
    return shows
