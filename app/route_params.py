"""Coercions for the handful of request parameters more than one route family
reads: a year, a month, a season number, and the months either side of one.

Its own module because the calendar routes and the tracker routes both ask for
exactly these, and neither is the other's owner — a query-string coercion is not
a calendar fact, and having the tracker import it from the calendar (or the
reverse) would make one route family depend on another for arithmetic. Same
reasoning as app/chrome.py and app/assets.py: a thing every page family needs
belongs beside them, not inside one of them.

Every one of these takes whatever the query string or JSON body actually held —
a string, None, a list, anything — and returns something the rest of the route
can use without checking again. A month or year that cannot be read falls back
to the caller's value (in practice today's) rather than raising, because a
mistyped ?month= in a bookmark should still show a calendar.
"""
from __future__ import annotations


def _as_int(value) -> int | None:
    """`value` as an int, or None when it is not a number at all."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def valid_year(value, fallback: int) -> int:
    """A year from a request, or `fallback`.

    Deliberately unbounded: the calendar and the tracker both navigate freely
    backwards and forwards, and a year with no airings in it renders as an empty
    month, which is the correct answer rather than an error.
    """
    year = _as_int(value)
    return year if year is not None else fallback


def valid_month(value, fallback: int) -> int:
    """A month number from a request, or `fallback` when it is missing or not
    1-12. Range-checked because this reaches calendar.monthrange and a cache
    key, neither of which has any use for month 13."""
    month = _as_int(value)
    return month if month is not None and 1 <= month <= 12 else fallback


def month_given(value) -> bool:
    """Whether `value` names a real month — the question "was a month asked for
    at all", which is distinct from valid_month's "which month do I use". A
    caller that has to tell "no month" from "month 7" cannot get that from a
    value that has already been defaulted."""
    month = _as_int(value)
    return month is not None and 1 <= month <= 12


def season(value) -> int | None:
    """A season number, or None when none was supplied.

    None is a real value to the Trakt detail lookups — it means "whichever
    season is airing" rather than "season 0" — so this does not default.
    """
    return _as_int(value)


def adjacent_months(year: int, month: int) -> dict:
    """The months either side of {year, month}, for a page's prev/next links.

    Returned as a dict rather than two dates because that is what the templates
    read, and because December -> January is a year change the template should
    not have to work out for itself.
    """
    prev_m, prev_y = (12, year - 1) if month == 1 else (month - 1, year)
    next_m, next_y = (1, year + 1) if month == 12 else (month + 1, year)
    return {"prev_month": prev_m, "prev_year": prev_y, "next_month": next_m, "next_year": next_y}
