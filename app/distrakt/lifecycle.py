"""What a season's record BECOMES as its life proceeds.

ONE JOB — the transitions. store.py owns the tables and answers "what is written
where"; this module answers "what happens next", and it is the only place that
knows the order the two tables are written in when a season moves between them.
A route calls a verb here rather than assembling a transition out of store
primitives, because a transition assembled at a call site is a transition that
exists in as many versions as there are call sites.

THE LIFE OF A SEASON, and the verb that carries each step:

  1. BEFORE IT AIRS       one premiere record on the month it premieres in.
                          Written where the premiere is learned of — the
                          calendar import, and the month's own build — because
                          store.premiere_kind() decides series-vs-season ONCE, at
                          the moment the record is made.
  2. FIRST EPISODE AIRS   follow(): copied onto the viewer's list. The premiere
                          record STAYS; the month's first notice is what it is
                          for, and that notice is about what began that month
                          whatever the title has since become.
  3. IT FINISHES AIRING   follow() again: keepup becomes catchup. The two are
                          states of one row, so this is an update and not a
                          second record.
  4. THE VIEWER FINISHES  finish(): off the list, onto the month the WATCH
                          HISTORY says it was finished in.
  5. THE VIEWER GIVES UP  give_up(): off the list, onto the month the CLOCK says
                          it is. Un-abandoning is take_back(), and the month row
                          does not survive it.
  6. IT TURNS OUT NOT TO
     HAVE BEEN FINISHED   reopen(): a season is completed on the episode count
                          known at the time, and providers learn of more later.
                          The month's verdict is withdrawn, the season goes back
                          onto the viewer's list carrying the came-back marker,
                          and reconcile_history() is what notices — from the
                          plays the watch-history pull has already reported, so
                          there is no detection step and no sweep of its own.

A COMPLETED VERDICT CAN BE QUESTIONED FROM THE OTHER DIRECTION TOO, and that one
is asked rather than acted on. A season is completed on what the services said at
the time, and a verdict never recomputes itself — that is what settling means. But
a service can later stop saying it: somebody sets a season back to unwatched, and
the record goes on asserting a number nobody reports any more. unbacked_verdicts()
notices, reopen() is what the viewer's yes runs, and nothing happens without one.

GIVING UP IS ALSO SOMETHING THE MAIN CALENDAR CAN SAY, and that is a transition
too rather than something a route assembles — reconcile_turn_aways() is give_up
and take_back again, driven by a set of turn-away marks instead of by a button.
Both directions of that mirror move the same record between the same two tables,
so they belong beside each other and not one here and one in a request handler.

WHY 4 AND 5 TAKE THEIR MONTH FROM DIFFERENT PLACES, and it is not an
inconsistency. Finishing a season is something the history can date: the last
episode was watched on a day, and that day names the month, however long ago it
was and whichever month is on screen. Giving up is not in the history at all — it
is an act performed today, so today is when it happened. Dating an abandon from
the month being viewed would let somebody browsing March record a decision they
made in September as March's.

THIS MODULE REACHES FOR NO PROVIDER ITSELF, and the one transition that needs a
provider is handed the means to ask. Keepup becoming catchup in particular is
derived from data already in hand: the season lookup made for every listed season
on every load reports the episode count and the last air date, and a date in the
past means the season has finished airing. That is also what heals a record whose
kind was guessed for it — the first load re-derives every one of them from the
dates. A reopening is the exception, because "how long is this season NOW" is the
one question the records cannot answer and is the whole reason it is reopening; it
takes a SeasonLookup to ask with, so the call still belongs to the caller and a
test can count how many were made.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import NamedTuple

from . import calendar_import, counts, live, store, watch_history
from .store import RecordKind
from ..calendar import state as calendar_state
from ..providers.base import ItemKey

# What a season lookup may correct on a PREMIERE record. `watched` is deliberately
# not among them: a premiere record carries no viewer progress — it is a snapshot
# of the show as it premiered — and that is exactly what makes correcting the rest
# of it safe to do on every load, for ever, including on a month that has frozen.
CATALOGUE_FIELDS = ("total", "cadence", "premiere", "finale",
                    "started_airing", "finished_airing")

# How a reopening asks what a season looks like now: given the stored record and
# the season number, whatever the provider currently says about it — the same
# fields a season lookup reports — or an empty mapping when it could not say.
# A parameter rather than a call wired in here, so this module goes on reaching
# for nothing and the number of lookups a reopening costs is countable from
# outside it.
SeasonLookup = Callable[[dict, int], Awaitable[Mapping]]

# How a settled verdict is asked what its services say about that season NOW:
# given the stored record and the season number, the per-source counts the watch
# state currently holds. A parameter for the same reason SeasonLookup is one —
# this module reaches for nothing itself — and it is deliberately NOT awaitable,
# because everything it can answer from is already in hand by the time a month is
# rendered. A version of it that could fetch would turn drawing a page into a
# provider call per settled row.
LiveCounts = Callable[[dict, int], Mapping[str, int]]


class MonthShape(NamedTuple):
    """The four collections a month's readers draw from, kept apart.

    THEY ARE SEPARATE BECAUSE THEY ANSWER DIFFERENT QUESTIONS. The premieres say
    what BEGAN in the month; the settled records say what the month DECIDED; the
    listed records say what the VIEWER has in hand right now and belong to no
    month at all. They were once one filtered list shared between three readers,
    and the readers came to disagree about which of the three questions they were
    answering.

    The two premiere kinds stay apart for the same reason one level down: they are
    two distinct sections of the month's first notice, and re-splitting one list by
    season number at each render is how the two come to disagree.
    """
    series_premieres: list[dict]
    season_premieres: list[dict]
    settled: list[dict]
    listed: list[dict]

    def premieres(self) -> list[dict]:
        """Both premiere kinds in one list, for a reader that genuinely wants
        every announcement the month made and will not re-split them."""
        return [*self.series_premieres, *self.season_premieres]


class Placement(NamedTuple):
    """Where the tracker currently holds a season. `month` is None when the
    record is the viewer's own, because a viewer record belongs to no month."""
    record: dict
    month: str | None


class HistoryQuestions(NamedTuple):
    """What a history pull turned up that only the viewer can settle.

    TWO QUESTIONS, NOT ONE LIST, because they are asked for opposite reasons and
    answering yes to them does opposite things. `unknown` is a season nothing
    anywhere has heard of — saying yes means going and finding out about it.
    `given_up` is a season the viewer deliberately turned away and has been
    watching again — saying yes means withdrawing that verdict, which is a record
    that already exists moving back, and undoing the calendar turn-away that came
    with it.

    Neither is acted on here. Both are things the tracker can see but must not
    decide: quietly re-adding something somebody chose to drop is exactly as wrong
    as quietly ignoring that they have gone back to it.
    """
    unknown: list[watch_history.EpisodePlay]
    given_up: list[watch_history.EpisodePlay]


class UnbackedVerdict(NamedTuple):
    """A settled COMPLETED record whose services no longer stand behind it, and
    everything the viewer needs to be shown to decide what to do about it.

    `sources` NAMES ONLY THE SERVICES THAT WITHDREW, not every service that
    answered, because that is the sentence the viewer is owed: this one said you
    had finished it and now says you have not. `now` is the whole current reading
    beside it, so the two numbers can be shown side by side rather than the viewer
    being told something changed and left to work out what.

    THE RECORD ITSELF TRAVELS UNTOUCHED. Nothing here is a decision — a verdict is
    only ever withdrawn by reopen(), and only ever because somebody said so.
    """
    record: dict
    month: str
    sources: list[str]
    now: dict[str, int]


def listed_kind(show: Mapping) -> RecordKind:
    """Whether a season on the viewer's list is one they are KEEPING UP with or
    one they have to CATCH UP on, from the air dates already in hand.

    A season that has finished airing is a pile to get through; one still going is
    something to keep pace with. A binge release is over the day it lands, so it
    is a catch-up from the start however recent it is — the whole season exists
    and none of it is going to arrive week by week.

    NOTHING THAT HAS NOT STARTED AIRING IS A CATCH-UP, whatever its cadence says.
    A binge season announced for a date still ahead reads as one here — it drops
    all at once, so the cadence is 'b' from the moment the date is known — and it
    would then be offered as a pile to get through weeks before a single episode
    of it exists. There is nothing to catch up ON yet, so it is the waiting state
    or nothing at all; a season that has not begun properly belongs to its month's
    announcement rather than to this list at all (see advance).
    """
    if not show.get("started_airing"):
        return RecordKind.KEEPUP
    if show.get("cadence") == "b":
        return RecordKind.CATCHUP
    return RecordKind.CATCHUP if show.get("finished_airing") else RecordKind.KEEPUP


def is_finished(show: Mapping) -> bool:
    """Whether the viewer has seen every episode the season is known to have.

    `total` of 0 means the lookup could not say how long the season is, which is
    not the same as a season with nothing in it — treating it as finished would
    complete every title the provider was briefly unable to answer about.
    """
    total = int(show.get("total") or 0)
    return total > 0 and int(show.get("watched") or 0) >= total


async def follow(user_id: int, show: dict) -> dict:
    """Take up a season: put it on the viewer's list, or move the one already
    there between keepup and catchup. Returns the stored record.

    ONE VERB FOR BOTH because they are one statement — "this season is the
    viewer's to get through, and here is where it stands". A season is on the
    list at most once, so arriving and moving kind are an insert and an update of
    the same row rather than two different things to remember to do.
    """
    return await store.add_user_record(
        user_id, {**show, "kind": listed_kind(show)})


async def correct_premiere(user_id: int, month: str, show: Mapping) -> dict:
    """Write back what a season lookup now says about a premiere record.

    Only CATALOGUE_FIELDS, and never `watched`: see that tuple. The fields it does
    write are the ones the first notice renders — the episode total and the
    premiere/finale dates — and a provider revising them after the fact is
    ordinary. A month that has frozen still takes these, because the notice is a
    statement about the show and not about the viewer, and a wrong episode count
    announced for ever is worse than a month that is not quite immutable.
    """
    return await store.add_month_record(user_id, month, {
        "kind": show["kind"],
        "media": show["media"],
        "match_source": show["match_source"],
        "match_id": show["match_id"],
        "season": int(show["season"]),
        **{field: show[field] for field in CATALOGUE_FIELDS if field in show},
    })


def by_source_of(show: Mapping) -> dict:
    """What each service said about a season, ready to be frozen onto a month.

    Read off a LIVE show, where the numbers came from, rather than re-derived at
    the moment a verdict is written — the two would be a second opinion about the
    same season, taken at a different instant. Empty for a row that never went
    through the live pass, which reads back as "no breakdown was written down".
    """
    return {name: dict(show.get(name) or {})
            for name in ("watched_by_source", "total_by_source")
            if show.get(name)}


async def finish(user_id: int, key: ItemKey, season: int, *, month: str,
                 by_source: dict | None = None) -> dict | None:
    """The viewer finished the season, so it leaves their list and becomes
    `month`'s completed record. None if it was not on the list.

    THE MONTH IS THE CALLER'S TO NAME AND IT COMES FROM THE WATCH HISTORY —
    watch_history.season_completed_map dates the last episode seen, and that date
    names the month. Not the clock: a season finished in July is July's record
    however many months later the tracker is next opened, and reading the clock
    here is how a browse of an old month would have re-dated somebody's viewing.
    """
    return await store.migrate_to_month(user_id, key, season, month=month,
                                        kind=RecordKind.COMPLETED,
                                        by_source=by_source)


async def give_up(user_id: int, show: dict, *, month: str,
                  abandoned_form: str | None = None) -> dict:
    """The viewer gave up on the season, so it leaves their list and
    becomes `month`'s abandoned record. Returns the record written.

    `month` IS THE CLOCK'S CURRENT MONTH, whichever month is on screen — see this
    module's header for why this one and not the viewed month.

    A season that is not on the viewer's list is still given up on, and the record
    is written outright rather than migrated: a premiere nobody ever started
    airing has no listed row to move, and refusing there would make the control
    silently do nothing on exactly the rows it is most often pressed on.
    """
    key, season = store.record_key(show), int(show["season"])
    moved = await store.migrate_to_month(user_id, key, season, month=month,
                                         kind=RecordKind.ABANDONED,
                                         abandoned_form=abandoned_form,
                                         by_source=by_source_of(show))
    if moved is not None:
        return moved
    return await store.add_month_record(user_id, month, {
        **show, "kind": RecordKind.ABANDONED, "abandoned_form": abandoned_form})


async def take_back(user_id: int, key: ItemKey, season: int, *,
                    month: str) -> dict | None:
    """Un-abandon: `month`'s abandoned record migrates back onto the viewer's
    list, under whichever kind the air dates say. None if that month has no such
    record.

    THE MONTH ROW DOES NOT SURVIVE. A month records what it settled, and this one
    no longer settled it — leaving the row behind would have the month go on
    announcing a verdict the viewer has just withdrawn.
    """
    record = await store.find_month_record(
        user_id, month, RecordKind.ABANDONED, key, season)
    if record is None:
        return None
    return await store.migrate_to_user(user_id, key, season, month=month,
                                       from_kind=RecordKind.ABANDONED,
                                       kind=listed_kind(record))


async def reopen(user_id: int, placement: Placement, *,
                 look_up: SeasonLookup) -> dict | None:
    """A season the viewer had finished turned out to have more of it: the
    month's completed verdict is withdrawn and the season goes back onto their
    list, marked as having come back. Returns the listed record, or None if the
    month record had gone in the meantime.

    THE MARKER IS SET AS THE RECORD IS RE-ADDED AND BEFORE THE OLD MONTH ROW
    GOES, which is why it is store.migrate_to_user's `came_back` argument rather
    than a call made after it. That completed record is the only thing that
    remembers the season was ever finished, and it is about to stop existing —
    "completed in July" has turned out not to be true, so July no longer settled
    it. Between a delete and a separate flag write there would be a moment when
    nothing remembered, and a failure in that moment would lose it in silence.
    The flag is the only thing that survives the move, and it is what the page
    draws the marker from.

    ONE LOOKUP, HERE, FOR THE SEASON — never one per episode. The caller reduces
    a history pull to the seasons it touched before any of this runs, so a show
    whose whole nine-episode return was watched in one sitting costs one call,
    and a season nothing was watched of costs none.

    A LOOKUP THAT SAYS NOTHING leaves the counts as the verdict had them, and the
    season may go straight back to being completed on the very next transition.
    That is the honest answer when nothing further is known to exist, and it is
    cheaper than holding a season open on the strength of a failed read.
    """
    record = placement.record
    key, season = store.record_key(record), int(record["season"])
    detail = dict(await look_up(record, season) or {})
    fresh = {**record, **detail, "season": season}
    moved = await store.migrate_to_user(
        user_id, key, season, month=str(placement.month),
        from_kind=RecordKind.COMPLETED, kind=listed_kind(fresh), came_back=True)
    if moved is None or not detail:
        return moved
    # What the lookup now says, written onto the row that is back on the list.
    # This is an update rather than an insert, and `came_back` is not one of the
    # columns an update may touch (see store._UPDATABLE_USER_COLUMNS), so the
    # marker just set survives the second write.
    return await follow(user_id, fresh)


async def finish_if_done(user_id: int, row: dict,
                         completed_on: Mapping[tuple[str, int], str]) -> bool:
    """Settle `row` onto the month its history names, if it is finished and that
    history says when. True when it was settled and has left the viewer's list.

    ITS OWN VERB BECAUSE TWO PASSES REACH IT. advance() runs it for the month under
    way, which is the ordinary case; the held-row drain (unsettled.py) runs it for
    a rebuilt list whose seasons have never been through advance at all, because
    the months they were held on had already ended. Written twice, the two would
    drift on the one question that matters here — whether an undated finish may be
    recorded — and that is a question about somebody's records being wrong rather
    than merely late.

    A SEASON WITH NO DATED LAST EPISODE IS LEFT ON THE LIST. "Finished, month
    unknown" would have to guess a month, and a wrong completed record is worse
    than a season that lingers. The date the history gives names the month, and
    that month is usually not the one being looked at.
    """
    when = str(completed_on.get(live.live_key(row)) or "")
    if not (is_finished(row) and when):
        return False
    await finish(user_id, store.record_key(row), int(row["season"]), month=when[:7],
                 by_source=by_source_of(row))
    return True


async def reconcile_turn_aways(user_id: int, month: str, *,
                               standing: store.MonthStanding) -> None:
    """Bring `month` into line with the viewer's main-calendar turn-away marks:
    a marked title is given up on, and one whose mark has been taken back comes
    back onto the list.

    THE MARKS ARE THE CALENDAR'S HALF OF THE SAME DECISION the Abandon control
    makes here, so this is the other direction of one mirror: giving up on the
    tracker writes a mark (the routes do that as they act), and a mark made over
    on the calendar becomes a verdict the next time the month is read. Run on
    every load rather than at a moment of change because the calendar has no idea
    the tracker exists — nothing over there can call in.

    NOT BOUNDED BY WHERE THE RECORD LIVES. A mark is a statement about the SHOW,
    so a title announced by some earlier month, or sitting on the viewer's own
    list and belonging to no month at all, is given up on just the same. What the
    month decides is only where the resulting verdict is FILED, which is what
    give_up already says: the month under way, because giving up happens today.

    A MONTH THAT HAS NOT BEGUN IS THE ONE EXCEPTION, and there the mark means the
    row simply should not be there: nothing in it has aired, so there is no
    verdict to record and abandoning would invent one. The premiere record is
    removed instead — the same answer the import path reaches by never adding a
    marked title in the first place.

    A MONTH THE CALENDAR HAS PASSED IS LEFT ALONE ENTIRELY. It settled what it
    settled; a decision made afterwards belongs to the month it was made in.

    IN THE STEADY STATE THIS WRITES NOTHING, which it has to, or an ordinary read
    of the page would rewrite records every time. A title already settled this
    month is skipped whatever the marks say, and a record the calendar could not
    name at all (no slug and no source id) is never un-abandoned on the strength
    of a mark it could never have had.
    """
    if standing is store.MonthStanding.PAST:
        return
    marks = await calendar_state.not_watching_ids(user_id)
    premieres = await store.month_records(user_id, month, store.PREMIERE_KINDS)

    if standing is store.MonthStanding.FUTURE:
        for record in premieres:
            if calendar_import.matches_not_watching(record, marks):
                await store.remove_month_record(
                    user_id, month, record["kind"],
                    store.record_key(record), int(record["season"]))
        return

    # Already settled here — completed as well as abandoned. A month says one
    # thing about a season per verdict, and re-running give_up over its own
    # output every load would overwrite the counts the verdict was reached on.
    settled = await store.month_records(user_id, month, store.SETTLED_KINDS)
    decided = {(record["key"], int(record["season"])) for record in settled}
    # The viewer's own list first, and the month's announcements second. A season
    # can be held in both at once, and only the listed copy carries how far
    # through it they got — a verdict reached off the premiere record instead
    # would freeze the abandoned line at nothing watched. `decided` grows as it
    # goes so the second copy is not written over the first.
    for record in (*await store.user_records(user_id), *premieres):
        address = (record["key"], int(record["season"]))
        if address in decided or not calendar_import.matches_not_watching(record, marks):
            continue
        await give_up(user_id, record, month=month)
        decided.add(address)

    for record in settled:
        if (record["kind"] == RecordKind.ABANDONED
                and calendar_import.calendar_mark_id(record)
                and not calendar_import.matches_not_watching(record, marks)):
            await take_back(user_id, store.record_key(record),
                            int(record["season"]), month=month)


async def find_season(user_id: int, key: ItemKey, season: int, *,
                      month: str) -> Placement | None:
    """Where this season is held, searched cheapest-and-likeliest first.

    THE ORDER IS THE POINT, and it is the order a watch-history reconciliation has
    to ask in. An episode just seen is nearly always for something the viewer is
    already part-way through, so their own list is asked first — one read, and it
    answers most of the time. Failing that it is something that began this month
    but has not reached the list yet. Only when both come up empty is it worth
    walking back over what earlier months settled, newest first, which is why
    store.walk_settled is a generator: the walk stops at the first match instead
    of loading a viewing life to answer a question about last month.

    None means nothing anywhere knows about this season. That is a real answer and
    not a failure — it is the case where guessing would cost a provider call for
    every unmatched episode of a viewing life, so the caller asks the viewer
    instead.
    """
    listed = await store.find_user_record(user_id, key, season)
    if listed is not None:
        return Placement(listed, None)
    for kind in sorted(store.PREMIERE_KINDS):
        record = await store.find_month_record(user_id, month, kind, key, season)
        if record is not None:
            return Placement(record, month)
    wanted = (str(key), int(season))
    async for past_month, records in store.walk_settled(user_id, before=month):
        for record in records:
            if (record["key"], int(record["season"])) == wanted:
                return Placement(record, past_month)
    return None


async def find_shown_season(user_id: int, key: ItemKey, season: int, *,
                            month: str) -> Placement | None:
    """Where a season the month's PAGE is drawing is held — including a verdict
    `month` has already reached.

    A SECOND QUESTION, NOT A WIDER FIND_SEASON, and the difference is which rows
    each answer is allowed to cover. find_season answers "where could a play that
    has just arrived still land", and its bound on the settled walk is written for
    that: a reconciliation has already set aside everything this month settled
    before it asks, so for that caller those records really are asked about by
    other means. The page has no such step. It draws what the month settled — a
    season finished or given up on this month is a row on screen, with a modal to
    open and ids to open it with — and asking find_season about one gets None,
    because the viewer's list no longer holds it (it left when it settled), the
    premiere step asks only about store.PREMIERE_KINDS, and the walk starts one
    month earlier. Three misses, and the row the page just drew reads as a season
    the tracker has never heard of.

    WIDENING find_season INSTEAD WOULD CHANGE WHAT A PLAY DOES, which is the thing
    that must not move: reconcile_history's own guard is what keeps a season this
    month has already settled from being reopened, and the abandon control would
    start finding a completed verdict and filing a second one beside it, putting
    one season on the page twice under two headings. So the search that exists is
    reused rather than rewritten — this asks it first, then asks the one place it
    deliberately steps over — and nothing that reconciles anything can reach the
    extra read.
    """
    placed = await find_season(user_id, key, season, month=month)
    if placed is not None:
        return placed
    for kind in sorted(store.SETTLED_KINDS):
        record = await store.find_month_record(user_id, month, kind, key, season)
        if record is not None:
            return Placement(record, month)
    return None


async def reconcile_history(user_id: int,
                            plays: Sequence[watch_history.EpisodePlay], *,
                            month: str,
                            look_up: SeasonLookup) -> HistoryQuestions:
    """Fold what the watch history has just reported back into the records, and
    hand back the plays only the viewer can settle.

    THE SIGNAL IS THE HISTORY PULL ITSELF, so there is no detection step and no
    sweep of its own. `plays` is what the incremental sync just folded in, and in
    the steady state — the activity beacon unmoved, no history fetched — it is
    empty, so this asks nothing and writes nothing. An ordinary load stays a read.

    REDUCED TO SEASONS BEFORE ANYTHING IS LOOKED UP. Nine episodes of one season
    watched in one sitting are one question and not nine, and the answer costs one
    lookup either way. The play kept for each season is the FIRST one seen,
    because the only thing an unmatched play carries onward is what it can say for
    itself — a title and an episode — and any one of them says it.

    A SEASON THIS MONTH HAS ALREADY SETTLED IS LEFT ENTIRELY ALONE, finished or
    given up on. The verdict is current: it was reached this month, from these
    very counts, so there is nothing to withdraw — and the season is plainly
    known, so there is nothing to ask the viewer about either. Without this an
    episode re-watched after finishing a season would come back as something the
    tracker had never heard of.

    ONLY A COMPLETED VERDICT REOPENS BY ITSELF. Anywhere still holding the season
    — the viewer's own list, or this month's premieres — is already the right
    place for it and needs no move at all.

    AN ABANDONED VERDICT IS ASKED ABOUT RATHER THAN REOPENED OR IGNORED. Watching
    something again after giving up on it is a real signal, but it is not proof of
    a changed mind the way finishing an extra episode is proof a season grew: a
    completed record is a claim about the SHOW that the play disproves, while an
    abandoned record is a decision about the VIEWER that only they can withdraw.
    Reopening it silently would also fight the calendar — giving up turns the
    title away there, that mark still stands, and the next turn-away
    reconciliation would give it up again, so the row would flap every time the
    history moved. So it is offered back instead, and saying yes is what clears
    both the verdict and the mark together.

    NOTHING IS LOOKED UP FOR A PLAY THAT MATCHES NOTHING, which is the whole
    reason the answer is a list handed back rather than a decision reached here.
    A season lookup for every unmatched episode of a viewing life is exactly the
    cost that asking the viewer instead exists to avoid, and the play already
    carries everything the asking needs.

    A SEASON THE VIEWER HAS ALREADY DECLINED IS NOT RAISED AGAIN. The refusal is
    read only once something has matched nothing — after the search, not before —
    because declining to ADD a season says nothing about what should happen if the
    tracker later comes to hold it anyway. Filtering earlier would also mean a
    declined season that has since been recorded could never reopen.
    """
    if not plays:
        return HistoryQuestions([], [])
    # One play per season, in the order the history reported them.
    seasons: dict[tuple[str, int], watch_history.EpisodePlay] = {}
    for play in plays:
        seasons.setdefault((str(play.key), int(play.season)), play)

    settled_here = {(record["key"], int(record["season"])) for record
                    in await store.month_records(user_id, month, store.SETTLED_KINDS)}
    # Read at most once, and only if something is actually going to be asked
    # about: the usual pull matches everything it reports to a season already in
    # hand, and a query for a set nothing will be tested against is a query for
    # nothing.
    declined: set[tuple[str, int]] | None = None
    unknown: list[watch_history.EpisodePlay] = []
    given_up: list[watch_history.EpisodePlay] = []
    for address, play in seasons.items():
        if address in settled_here:
            continue
        placed = await find_season(user_id, play.key, play.season, month=month)
        if placed is not None and placed.month is None:
            continue  # already in hand; the counts refresh covers it
        asking = placed is None or placed.record["kind"] == RecordKind.ABANDONED
        if asking:
            if declined is None:
                declined = await store.dismissed_prompts(user_id)
            if address in declined:
                continue
            (unknown if placed is None else given_up).append(play)
        elif placed.record["kind"] == RecordKind.COMPLETED:
            await reopen(user_id, placed, look_up=look_up)
    return HistoryQuestions(unknown, given_up)


async def catch_up_settled(user_id: int, month: str, settled: Sequence[dict],
                           live_counts: LiveCounts) -> int:
    """Let a settled record in an OPEN month learn a count that has gone UP.

    THE CASE, REPORTED FROM A REAL MONTH: a season was finished and settled while
    one service had seen seven of eight; that service then caught up, and the row
    went on reading `8/8 · 7/8` while the panel behind it showed eight ticks from
    both. The two were reading different things — the row its own frozen
    breakdown, the panel the watch state — and the row was simply behind.

    UPWARD ONLY, AND THAT IS THE WHOLE OF THE RULE. A count that has GROWN is a
    service catching up with something the viewer had already done, and there is
    nothing to ask them about it. A count that has SHRUNK is a service withdrawing
    a claim, which is `unbacked_verdicts`' business and must never be written
    silently over a record somebody deliberately settled — see its docstring,
    which requires the completed row to stand unchanged while the question is
    put. So this can only ever move a number toward what the services now
    report, never away from it.

    ONLY THE SERVICES THAT ANSWERED THIS PASS, for the same reason no_longer_finished
    ignores the rest: one that could not be read said nothing, and silence is
    neither a retraction nor a catch-up.

    ONLY WHILE THE MONTH IS OPEN. Its caller applies that; a frozen month answers
    "what did that month decide" and today's watch state cannot make it wrong.

    IT WRITES, rather than patching what the page happens to draw. The breakdown
    is read by the row, by the freeze that will eventually keep it, and by the
    withdrawal check above — a display-only correction would leave those three
    disagreeing, which is the shape of the defect being fixed. Returns how many
    records moved, which is zero on essentially every load.
    """
    moved = 0
    for record in settled:
        if record["kind"] != RecordKind.COMPLETED:
            continue
        recorded = dict(record.get("watched_by_source") or {})
        now = dict(live_counts(record, int(record["season"])) or {})
        caught_up = {
            name: count for name, count in now.items()
            if int(count or 0) > int(recorded.get(name) or 0)
        }
        if not caught_up:
            continue
        merged = {**recorded, **caught_up}
        total = int(record.get("total") or 0)
        await store.add_month_record(user_id, month, {
            **record,
            "watched_by_source": merged,
            # The one number follows the breakdown it is drawn from, by the same
            # rule as everywhere else — see counts.primary_count. Every entry it
            # reads has only risen, so this cannot lower what the month recorded.
            "watched": counts.primary_count(merged, live.source_order(), total),
        })
        moved += 1
    return moved


async def unbacked_verdicts(user_id: int, month: str, settled: Sequence[dict],
                            live_counts: LiveCounts) -> list[UnbackedVerdict]:
    """The COMPLETED verdicts among `settled` that the services no longer back.

    THE RECORD IS NOT TOUCHED AND MUST NOT BE. A settled record does not recompute
    itself; that is the whole meaning of settling, and it is why a month that
    froze while two services disagreed can still render the disagreement years
    later. What this notices is that the record has come to assert something no
    service says any more — the case it was written for is a season completed at
    10/10 by both services and then set back to unwatched at both, after which the
    row goes on reading 10/10 while every service reports nothing. The answer is
    not to quietly re-derive it: somebody deliberately finished that season and the
    record of it is theirs. The answer is to SAY so and offer the two moves,
    reopen() and a refusal, which is the same pair the page already offers for a
    season the viewer gave up on and went back to.

    ONLY COMPLETED. An abandoned verdict is a decision about the VIEWER rather than
    a claim about the show, so no number can falsify it — reconcile_history says
    the same thing at more length, and a play arriving for one is already asked
    about there.

    THE MONTH IS THE CALLER'S AND IS ALWAYS THE ONE UNDER WAY. A frozen month is
    history and is governed by what it recorded: its numbers are the answer to
    "what did that month decide", not a live claim about what the services hold
    today, and offering to redo one would let this year's viewing rewrite an
    earlier year's record. The caller is what keeps that true — see
    routes._live_month_payload, which asks only for the month in progress.

    NOTHING IS FETCHED. `live_counts` reads the watch state the month's render has
    already loaded, so a month with no unbacked verdict costs one dictionary
    lookup per settled row and a month with one costs the same.

    A SEASON THE VIEWER HAS ALREADY REFUSED IS NOT RAISED AGAIN, through the very
    refusal the page's other two questions are refused through
    (store.dismiss_prompt). The three questions differ in what they say and agree
    exactly in what NO means: do not put this season back on my list, stop asking.
    Reading the refusal only once something is actually going to be asked keeps an
    ordinary load — every verdict still backed — from querying for a set nothing
    would be tested against.
    """
    questions: list[UnbackedVerdict] = []
    declined: set[tuple[str, int]] | None = None
    for record in settled:
        if record["kind"] != RecordKind.COMPLETED:
            continue
        season = int(record["season"])
        now = dict(live_counts(record, season) or {})
        withdrawn = counts.no_longer_finished(
            record.get("watched_by_source") or {}, now, record.get("total"))
        if not withdrawn:
            continue
        if declined is None:
            declined = await store.dismissed_prompts(user_id)
        if (record["key"], season) in declined:
            continue
        questions.append(UnbackedVerdict(record, month, withdrawn, now))
    return questions


def shape_of(records: list[dict], listed: list[dict] | None = None) -> MonthShape:
    """Sort a month's records into the collections its readers draw from.

    The split is by the record's own KIND rather than by anything re-derived from
    its counts or its season number, which is the whole reason the kind is stored:
    a record says what it is, once, and every reader gets the same answer.
    """
    return MonthShape(
        series_premieres=[r for r in records
                          if r["kind"] == RecordKind.SERIES_PREMIERE],
        season_premieres=[r for r in records
                          if r["kind"] == RecordKind.SEASON_PREMIERE],
        settled=[r for r in records if r["kind"] in store.SETTLED_KINDS],
        listed=list(listed or []),
    )


async def announce(user_id: int, month: str, premieres: list[dict]) -> MonthShape:
    """A month that is not the one under way: correct what its premiere records
    say about the shows, and report what it holds.

    NO VIEWER LIST. What somebody is keeping up with belongs to no month, and a
    month that is over settled its own verdicts while one that has not begun has
    nothing in hand yet — so neither has a list to show, and neither has any
    transition to run. The premiere corrections still happen, because a revised
    episode count is a fact about the show and is true of the month whenever it is
    learned.
    """
    for show in premieres:
        await correct_premiere(user_id, month, show)
    return shape_of(
        premieres + await store.month_records(user_id, month, store.SETTLED_KINDS))


def premieres_in(show: Mapping, month: str) -> bool:
    """Whether this season's premiere date falls in `month`.

    The date the provider gives is "M/D" with no year in it, so the YEAR comes
    from the month being asked about. That is sound for the one question this
    answers — "did this begin in the month I am looking at" — and it is the only
    question asked of it; nothing here tries to date a premiere in the abstract.
    """
    premiere = str(show.get("premiere") or "")
    head, sep, _ = premiere.partition("/")
    if not sep:
        return False
    try:
        return int(head) == store.parse_month_key(month)[1]
    except ValueError:
        return False


async def _announce_instead(user_id: int, month: str, show: Mapping,
                            announced: set) -> bool:
    """Take a season that has NOT started airing off the viewer's list, filing it
    as `month`'s announcement if that is where it belongs. True when it was dealt
    with and must not be listed.

    THE LIST IS WHAT THE VIEWER IS IN THE MIDDLE OF, and a season with nothing
    aired is not something anybody is in the middle of. It reached the list either
    by being carried there before this shape existed — a split that could not tell
    a month's announcements from its work in hand, because the fields that say
    which were only ever written when a month FROZE — or by a route that listed it
    without checking. Either way its place is the announcement.

    THE PREMIERE DATE IS ALREADY IN HAND by the time this runs: the live pass has
    just asked about every one of these seasons, so filing it costs no further
    call. That is what lets a month work this out for itself instead of waiting to
    be re-imported.

    NOTHING HERE MAY MAKE A ROW VANISH. A season this month does not announce and
    whose premiere falls somewhere else is left exactly where it is, because the
    list is the only thing holding it and taking it off would remove it from the
    page altogether rather than move it.
    """
    key, season = store.record_key(show), int(show["season"])
    if live.live_key(show) not in announced:
        if not premieres_in(show, month):
            return False
        await store.add_month_record(user_id, month, {
            **show, "kind": store.premiere_kind(season)})
    await store.remove_user_record(user_id, key, season)
    return True


async def advance(user_id: int, month: str, *, premieres: list[dict],
                  listed: list[dict],
                  completed_on: Mapping[tuple[str, int], str]) -> MonthShape:
    """Run every transition the current facts imply, then report what `month`
    holds and what the viewer holds.

    ONE PASS, DRIVEN BY DATA ALREADY FETCHED. `premieres` and `listed` are the
    month's premiere records and the viewer's own records, each already merged
    with what the provider says about it right now (live.compute_live_shows), and
    `completed_on` is watch_history.season_completed_map. Nothing here fetches
    anything, so the transitions cost the same whether they fire or not.

    THE SETTLED RECORDS ARE RE-READ RATHER THAN ASSEMBLED. A season finished this
    pass lands on the month its history names, which may not be this one, and a
    season finished in an earlier month was already recorded there — so the only
    honest answer to "what did this month settle" is to ask the month, after the
    writes.
    """
    held = {live.live_key(record) for record in listed}
    # What this month has already settled, read BEFORE anything is written. A
    # season the viewer finished or gave up on here is done with, and its premiere
    # record is still sitting on the month announcing it — so without this the
    # premiere would be copied back onto their list on the very next load and the
    # verdict they just reached would be undone by looking at the page.
    decided = {live.live_key(record) for record
               in await store.month_records(user_id, month, store.SETTLED_KINDS)}
    working = list(listed)

    for show in premieres:
        await correct_premiere(user_id, month, show)
        # A premiere that has begun airing is now something the viewer is either
        # keeping up with or behind on, so it is copied onto their list. The
        # premiere record stays where it is: what the month announced does not
        # change because the show started.
        if (show.get("started_airing") and live.live_key(show) not in held
                and live.live_key(show) not in decided):
            # Joined to the working set rather than written here: the loop below
            # follows everything on it, so writing it now would be the same row
            # written twice on the load a season first airs.
            working.append(show)
            held.add(live.live_key(show))

    # What this month announces. A season that has not started airing belongs to
    # its announcement and nowhere else — the viewer's list is what they are in
    # the MIDDLE of, and nothing has aired to be in the middle of. Held on the
    # list as well, the same season renders twice: once under New or Returning
    # from the premiere record, and again under Keepup or Cleanup from this one.
    announced = {live.live_key(record) for record in premieres}

    still_listed: list[dict] = []
    for show in working:
        if not show.get("started_airing") and await _announce_instead(
                user_id, month, show, announced):
            continue
        stored = await follow(user_id, show)
        row = {**show, **stored}
        if await finish_if_done(user_id, row, completed_on):
            continue
        still_listed.append(row)

    # BOTH re-read rather than assembled, and for the same reason: the loops above
    # write. A season finished this pass lands on the month its history names,
    # which may not be this one; a season that turned out not to have aired yet has
    # just become this month's announcement. Asking the month afterwards is the
    # only answer that includes what just happened without reconstructing it.
    return shape_of(
        await store.month_records(
            user_id, month, store.PREMIERE_KINDS | store.SETTLED_KINDS),
        still_listed)
