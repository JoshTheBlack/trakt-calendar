"""What a season's air dates say about it: how many episodes, on what cadence,
when it started and when it ends.

ONE IMPLEMENTATION, FED BY EVERY SOURCE. The rule is about DATES, not about any
service's payload: given one air date per episode (or None for an episode nobody
has dated yet), "is this a weekly show or a binge drop" and "is this season fully
scheduled" have the same answer whoever supplied the dates. Two copies of it
would drift the first time either was touched, and the symptom — one source
calling a season finished and the other not — would look like the services
disagreeing rather than like this app doing two different sums.

Each provider still owns the PARSING, because only it knows which key its
payload spells an air date in and in what format. What it hands over is a plain
list of local calendar dates, in episode order.
"""
from __future__ import annotations

from collections import Counter
from datetime import date

# date.weekday(): Mon=0 .. Sun=6. Explicit map so cadence is locale-independent.
WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _md(day: date | None) -> str | None:
    """A date as 'M/D' with no leading zeros. None stays None so the renderer can
    decide to show '?/?' for a date nobody knows yet."""
    return f"{day.month}/{day.day}" if day else None


def derive_season(air_dates, today: date) -> dict:
    """The cadence/date fields for a season, from one entry per episode.

    `air_dates` carries a `date` for every episode that has one and None for
    every episode that does not, so its LENGTH is the episode total and its
    non-empty values are what is actually scheduled. Passing only the known dates
    would silently make a half-announced season look complete.

    Rules:
      total (y)  = every episode the source currently reports (dated or not).
      premiere   = the first KNOWN air date.
      finale     = the last KNOWN air date, but ONLY once the season is fully
                   scheduled; an unscheduled tail leaves it unknown -> '?/?'.
      cadence    = 'b' when every episode shares one air date (a binge drop),
                   else the weekday abbreviation the airings mostly fall on;
                   None when no air date is known yet.
      started/finished_airing compare premiere/finale against `today`.
    """
    dates = list(air_dates or [])
    total = len(dates)
    known = sorted(d for d in dates if d)
    fully_scheduled = total > 0 and len(known) == total

    premiere_date = known[0] if known else None
    # A finale is only meaningful when nothing is left unscheduled; an
    # unscheduled tail leaves it unknown so the renderer shows "?/?".
    finale_date = known[-1] if (fully_scheduled and known) else None

    if not known:
        cadence = None
    elif fully_scheduled and len(set(known)) == 1:
        cadence = "b"  # binge: every episode shares one air date
    else:
        weekday = Counter(d.weekday() for d in known).most_common(1)[0][0]
        cadence = WEEKDAY_ABBR[weekday]

    return {
        "total": total,
        "cadence": cadence,
        "premiere": _md(premiere_date),
        "finale": _md(finale_date),
        "started_airing": premiere_date is not None and premiere_date <= today,
        "finished_airing": finale_date is not None and finale_date <= today,
        "air_dates": [d.isoformat() for d in known],
    }


def empty_season(season: int) -> dict:
    """What a season nobody could tell us anything about looks like. Beside
    derive_season because a caller that got no episode list still has to hand its
    caller the same keys — a missing one would read as a template bug rather than
    as an unanswered lookup."""
    return {
        "season": season, "total": 0, "cadence": None, "premiere": None,
        "finale": None, "started_airing": False, "finished_airing": False,
        "air_dates": [],
    }
