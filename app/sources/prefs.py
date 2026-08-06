"""One account's source preferences, read and written.

Backs the `source_prefs` table. Three facts live here:

  - CALENDAR SOURCE and TRACKER SOURCE: which services each half of the app
    asks. Separately, because they are separate decisions — somebody can
    reasonably want every service's calendar and only one service's idea of what
    they have watched. The two halves also read `auto` differently, and
    `admits_calendar` below is where that is written down.
  - PRECEDENCE: when two services fill the same field with different values,
    whose value the viewer sees. Resolved at READ over already-cached data, so
    changing it is instant and invalidates nothing.

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
AUTO = "auto"

# Two services, stated. DELIBERATELY NOT THE SAME VALUE AS `auto`, and the
# difference is what happens when a link lapses: a stated preference for both
# must not silently become single-source because a token expired, so this keeps
# asking and the missing one shows as missing. Collapsing the two would make an
# unlink and a decision indistinguishable afterwards.
BOTH = "both"

# What either column may hold. The service names come from Source rather than
# being spelled again here, so a service the app does not know about cannot be
# stored as a preference for it.
SELECTIONS = frozenset({AUTO, BOTH, *(str(s) for s in Source)})

DEFAULT_SELECTION = AUTO


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
    if selection == AUTO:
        return name in {str(s) for s in linked}
    if selection == BOTH:
        return True
    return selection == name


@dataclass(frozen=True)
class SourcePrefs:
    """One account's whole row, or the defaults when it has none.

    Frozen, and `save` takes a whole one: the three fields are read together on
    every path that wants any of them, and a partial write verb would need a
    "leave this alone" sentinel for each. `dataclasses.replace` is how a caller
    changes one.
    """
    user_id: int
    calendar_source: str = DEFAULT_SELECTION
    tracker_source: str = DEFAULT_SELECTION
    precedence: dict = field(default_factory=dict)

    def admits_calendar(self, source: Source | str) -> bool:
        """Whether this account's calendar reads `source`.

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
        "every source this account has linked". A STATED selection — 'both', or
        one service by name — is still exactly what it says and is honoured
        whatever is linked; this only ever widens the default.
        """
        if self.calendar_source == AUTO:
            return True
        return admits(self.calendar_source, source, ())

    def admits_tracker(self, source: Source | str, linked) -> bool:
        return admits(self.tracker_source, source, linked)


def _selection(value, column: str) -> str:
    """A stored or supplied selection, validated.

    REFUSED RATHER THAN COERCED on the way IN — a preference nobody can satisfy
    is a bug in the caller and silently rewriting it to 'auto' would hide it. On
    the way OUT of the database an unknown value falls back to the default
    instead, because a row written by a newer version of the app must not stop an
    older one from rendering a page.
    """
    text = str(value or "")
    if text not in SELECTIONS:
        raise ValueError(
            f"{column} must be one of {', '.join(sorted(SELECTIONS))}, not {text!r}")
    return text


def _stored_selection(value) -> str:
    text = str(value or "")
    return text if text in SELECTIONS else DEFAULT_SELECTION


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
        "SELECT calendar_source, tracker_source, precedence_json "
        "FROM source_prefs WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return SourcePrefs(user_id=user_id)
    return SourcePrefs(
        user_id=user_id,
        calendar_source=_stored_selection(row["calendar_source"]),
        tracker_source=_stored_selection(row["tracker_source"]),
        precedence=_stored_precedence(row["precedence_json"]),
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
    await db.execute(
        "INSERT INTO source_prefs (user_id, calendar_source, tracker_source, "
        "precedence_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "calendar_source = excluded.calendar_source, "
        "tracker_source = excluded.tracker_source, "
        "precedence_json = excluded.precedence_json",
        (prefs.user_id, calendar_source, tracker_source, json.dumps(precedence)),
    )
    return replace(prefs, calendar_source=calendar_source,
                   tracker_source=tracker_source, precedence=precedence)
