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

Everything here is PURE and takes the per-source dicts the watch state already
carries, so the rule can be tested without a database, a request or a provider.
"""
from __future__ import annotations

from collections.abc import Mapping

# What the row shows between two services' numbers. A middle dot rather than a
# slash or a comma, because a slash already means "x of y" one character to the
# left and a comma reads as a list of episodes.
SEPARATOR = " · "


def primary_count(per_source: Mapping[str, int] | int | None, order=()) -> int:
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
    """
    if per_source is None:
        return 0
    if not isinstance(per_source, Mapping):
        return int(per_source or 0)
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
    complete = len(asked) <= 1 or {str(source) for source in asked} <= set(per_source)
    if agreed(per_source) and complete:
        return f"{primary_count(per_source, order)}/{y}"
    names = [str(s) for s in order if str(s) in per_source]
    names += [s for s in sorted(per_source) if s not in names]
    label_of = labels or {}
    return SEPARATOR.join(
        f"{int(per_source[name] or 0)}/{y} ({label_of.get(name, name)})" for name in names)
