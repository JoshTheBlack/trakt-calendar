"""How many episodes of a season the viewer has seen, when two services may
have different answers.

ONE RULE, IN ONE PLACE, because it is asked in three: the row on the page, the
number a month is frozen with, and the section a season is bucketed into. The
rule itself is short and the reasoning behind it is the part worth keeping:

  AGREEMENT IS ONE NUMBER. Both services reporting the same watched set for a
  season is the overwhelmingly common case, and saying "6/8" twice with two
  badges beside it would make every ordinary row noisier to buy nothing.

  DISAGREEMENT IS BOTH NUMBERS, EACH LABELLED. Not a union, not an average, not
  a pick. The services genuinely know different things — one saw a play the
  other never received — and every way of collapsing that asserts something
  neither of them said. An average is the worst of them: it is a number nobody
  reported.

  A SEASON ONLY ONE SERVICE KNOWS ABOUT is that service's number with its badge,
  which is the same rule as disagreement with one side missing.

  A SERVICE THAT COULD NOT BE READ IS NOT AGREEMENT. Its absence this pass says
  nothing about what it holds, so the numbers shown are labelled as the ones that
  answered rather than presented as the whole picture.

  A SERVICE THAT SAID "ALL OF IT" WITHOUT SAYING HOW MANY becomes a number here,
  because here is where the season's total is in hand — see ALL_EPISODES.

  A NUMBER MOVING IS NOT THE SAME AS A VERDICT BEING WITHDRAWN. Only crossing the
  season's total either way changes whether a service says the season is
  finished, which is the one thing a completed record actually claims — see
  finished_by and no_longer_finished.

Everything here is PURE and takes the per-source dicts the watch state already
carries, so the rule can be tested without a database, a request or a provider.
"""
from __future__ import annotations

from collections.abc import Mapping

# What the row shows between two services' numbers. A middle dot rather than a
# slash or a comma, because a slash already means "x of y" one character to the
# left and a comma reads as a list of episodes.
SEPARATOR = " · "

# WHAT A SERVICE'S COUNT HOLDS WHEN IT SAID "ALL OF IT" WITHOUT SAYING HOW MANY.
# A service can report a title as finished — every episode watched — while
# itemizing nothing, and then there are no episode numbers to count and no dates
# to keep; there is only the claim. Turning that into a number needs the season's
# TOTAL, which arrives here and nowhere earlier: the watch state holds what each
# service reported, and the total is catalogue data fetched beside it.
#
# SO THE CLAIM TRAVELS AS A COUNT AND IS RESOLVED WHERE "x/y" IS WRITTEN. It rides
# inside the same per-source map every reader already carries rather than in a
# second lookup threaded beside it, because a reader that forgot the second lookup
# would silently drop that service's answer and render a false single-source
# claim — which is the exact defect this exists to remove. Negative because a
# watched count cannot be, so nothing real can collide with it.
ALL_EPISODES = -1


def resolve(per_source: Mapping[str, int] | int | None, total) -> dict[str, int]:
    """Per-source counts with every "all of it" claim turned into a number.

    ONE PLACE, called by everything here, so a claim can never reach a template, a
    frozen month or an announcement post still wearing its sentinel. A caller that
    hands in numbers gets its numbers back unchanged, which is what keeps the
    common path — two services that both itemize — on exactly the code it was on.
    """
    if not isinstance(per_source, Mapping):
        return {}
    y = int(total or 0)
    return {str(source): (y if int(count or 0) == ALL_EPISODES else int(count or 0))
            for source, count in per_source.items()}


def primary_count(per_source: Mapping[str, int] | int | None, order=(), total=0) -> int:
    """The ONE number for callers that can only carry one — a frozen month's
    `watched` column, the announcement post, a bucket comparison.

    It is the PRIMARY source's, which is the first entry of `order` that actually
    reported something (the registry's declared order; see
    providers.for_tracker_ports). Not the highest and not the union: those would
    each be a different number depending on which services happened to answer,
    and a frozen month has to keep meaning the same thing years later. With
    nothing in `order` present, the largest answer is taken, because a caller
    that did not state an order is asking for "how much of this have I seen" and
    the alternative is picking by dictionary iteration.

    A bare number is accepted and returned unchanged, so a caller holding a count
    from somewhere other than the watch state does not have to wrap it.

    `total` is what an "all of it" claim resolves to (see ALL_EPISODES). It
    defaults to nothing rather than being required, because most callers hold
    plain counts — but a caller that has the season's total should pass it, or a
    service that only claimed completeness contributes a zero here.
    """
    if per_source is None:
        return 0
    if not isinstance(per_source, Mapping):
        return int(per_source or 0)
    per_source = resolve(per_source, total)
    for source in order:
        if str(source) in per_source:
            return int(per_source[str(source)] or 0)
    return max((int(v or 0) for v in per_source.values()), default=0)


def agreed(per_source: Mapping[str, int] | None) -> bool:
    """Whether every source that answered reported the same count.

    One answer agrees with itself, which is what keeps an account reading a
    single service on exactly the path it was always on.
    """
    if not isinstance(per_source, Mapping) or not per_source:
        return True
    return len(set(int(v or 0) for v in per_source.values())) == 1


def finished_by(per_source: Mapping[str, int] | int | None, total) -> set[str]:
    """The services whose count says the WHOLE season has been seen.

    Its own reader because "how far through is this" and "does this service say it
    is done" are different questions, and only the second one can make a completed
    verdict true or false. A count that moves without crossing the total — three
    episodes becoming seven — says the viewer is getting on with it and says
    nothing at all about a verdict.

    A TOTAL OF ZERO NAMES NOBODY, for the reason lifecycle.is_finished refuses one:
    zero means the lookup could not say how long the season is, not that the season
    is empty, and reading it as "everything" would have every service claim to have
    finished every title a provider was briefly unable to answer about.
    """
    y = int(total or 0)
    if y <= 0:
        return set()
    return {name for name, count in resolve(per_source, y).items() if count >= y}


def no_longer_finished(recorded: Mapping[str, int] | int | None,
                       now: Mapping[str, int] | int | None, total) -> list[str]:
    """The services a settled verdict credits with FINISHING a season and which do
    not say so any more, in name order.

    THIS IS NOT "ANYTHING MOVED", and that is the whole design of it. A verdict is
    a claim that the season was finished; a number that changes without changing
    whether it was finished — a service catching up from three to seven, a second
    service arriving at a total the record never credited it with — leaves the
    claim standing exactly as it was. A question raised on every such move would be
    dismissed reflexively, and a prompt that is always dismissed protects nobody.
    What DOES falsify the claim is a service that the record says finished the
    season now reporting that it did not, which is the shape of somebody setting a
    season back to unwatched at the service.

    A SERVICE THAT SAID NOTHING THIS PASS WITHDRAWS NOTHING. Absence from `now` is
    not a zero — it is a service that was not read, or was read and had no answer —
    and treating it as a retraction would raise the question every time a service
    went quiet. It has to be present and it has to have stopped saying so.

    A RECORD THAT NEVER WROTE DOWN WHICH SERVICE SAID WHAT cannot be checked at
    all, and answers empty. Its `watched` is one number with no service attached
    to it, so there is nothing to test against a per-service reading; guessing at
    which service it came from would be inventing the very attribution the record
    is missing.
    """
    if not isinstance(now, Mapping):
        return []
    y = int(total or 0)
    still = finished_by(now, y)
    return sorted(name for name in finished_by(recorded, y)
                  if name in now and name not in still)


def counts_label(per_source: Mapping[str, int] | int | None, total,
                 labels: Mapping[str, str] | None = None, order=(), asked=()) -> str:
    """The "x/y" a row shows, which is two of them when the services disagree.

    `labels` maps a source name to what to call it on screen (the registry's
    `label`); a source with no entry is shown under its own name rather than
    dropped, because an unlabelled number is still better than a missing one.

    `asked` IS WHICH SERVICES WERE READ FOR THIS ACCOUNT, and it is what tells a
    season only ONE of two services knows about from a season both of them agree
    on. Both arrive here as a single number, and they mean different things: the
    first is one service's claim that the other never made, and it carries that
    service's badge. With one service read — which is what almost every account
    has — there is nothing to distinguish, and every row is a bare number exactly
    as it always was.
    """
    y = int(total or 0)
    if not isinstance(per_source, Mapping) or not per_source:
        return f"{int(per_source or 0)}/{y}"
    # Resolved HERE as well as at every other reader, because this is the one
    # function that is handed both halves of "x/y" by every caller in the app —
    # so a claim that reached a row through some path nobody updated still renders
    # as the number the service meant rather than as a negative one.
    per_source = resolve(per_source, y)
    complete = len(asked) <= 1 or {str(source) for source in asked} <= set(per_source)
    if agreed(per_source) and complete:
        return f"{primary_count(per_source, order)}/{y}"
    names = [str(s) for s in order if str(s) in per_source]
    names += [s for s in sorted(per_source) if s not in names]
    label_of = labels or {}
    return SEPARATOR.join(
        f"{int(per_source[name] or 0)}/{y} ({label_of.get(name, name)})" for name in names)
