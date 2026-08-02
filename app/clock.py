"""What day the app thinks it is, with a development-only override.

Every "has this month finished", "which month is open", "has this aired yet"
question in the app is a question about TODAY, and until this module existed each
one asked `date.today()` for itself. That is fine for correctness and fatal for
testing: there was no way to make a running instance believe the date had
changed, so the tracker's month rollover — the behaviour with the most
month-dependent logic in the app — could never be watched end to end. Several
month bugs shipped because the only way to see one was to wait for the calendar.

So: one function everything calls, and one environment variable that can move it.

WHAT THIS DOES NOT COVER. The override is a DATE, so it moves date questions
only. Timestamps — a `generated_at` field, an export filename's clock stamp —
still read the real clock, because a date carries no hour to give them and a
half-faked timestamp is worse than an honest one. If a future test needs the
whole clock moved, that is a different seam and it should say so.

--- THE OVERRIDE IS A LOADED GUN, SO IT IS BOLTED DOWN -------------------------
A clock a caller can move is a way to unfreeze a month the tracker has already
closed, re-run a rollover that has already happened, or make a month that has not
begun behave as though it had. That is not a hypothetical: `month_standing` and
`month_committed` decide what may still be edited purely from the date.

The protections, and none of them is optional:

  - THE VALUE COMES FROM THE PROCESS ENVIRONMENT AND NOWHERE ELSE. There is no
    query parameter, header, cookie, setting or API that writes it, and nothing
    under app/ assigns into the process environment at all. Setting it requires
    the ability to start the process, which is strictly more access than any
    request has.
  - ABSENT OR UNPARSEABLE MEANS THE REAL CLOCK, with no override path entered at
    all — `today()` returns `date.today()` on the first branch and never looks at
    a stored value, because there is no stored value to look at.
  - IT ANNOUNCES ITSELF AT BOOT, at WARNING, naming the date. An instance running
    on a fake clock says so every time it starts; see `warn_if_overridden()`.
  - THERE IS NO UI. It is not in Settings, it is not rendered anywhere, and it is
    deliberately not an admin-editable value — an admin-editable fake clock would
    be a fake clock an admin account compromise can reach.

READ LIVE RATHER THAN CAPTURED AT IMPORT, matching `config.public_base_url_override`.
It costs one dict lookup per call and it keeps the value in exactly one place —
the environment — instead of a module global that a test could leave dirty for
whatever runs next. Live reading does not widen the reach: a request still has no
way to write the process environment.
"""
from __future__ import annotations

import logging
import os
from datetime import date

logger = logging.getLogger(__name__)

# ISO "YYYY-MM-DD". Named for what it is rather than something neutral like
# TRAKT_TODAY: an operator who finds this set in a deployment should be able to
# tell from the name alone that it is not a real setting.
FAKE_TODAY_ENV = "TRAKT_FAKE_TODAY"


def override() -> date | None:
    """The date `TRAKT_FAKE_TODAY` names, or None when it is unset or unusable.

    A malformed value is logged and ignored rather than raised. This is a
    development seam; refusing to start over a typo in it would turn a debugging
    aid into a way to break a boot, and falling back to the real clock is the
    outcome that cannot surprise anyone.
    """
    raw = os.environ.get(FAKE_TODAY_ENV, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning(
            "Ignoring %s=%r: expected an ISO date like 2026-03-01. Using the real clock.",
            FAKE_TODAY_ENV, raw)
        return None


def today() -> date:
    """Today's date — the real one unless `TRAKT_FAKE_TODAY` says otherwise.

    Call this instead of `date.today()` anywhere the answer decides what the app
    SHOWS or ALLOWS. Functions that already take `today: date | None = None` keep
    that parameter; this is what they should default to.
    """
    return override() or date.today()


def warn_if_overridden() -> None:
    """Say loudly, once at boot, that this instance is not on the real clock.

    Unconditional whenever the variable parses — unlike the base-URL warning next
    door, there is no "the two agree" case that makes it noise. A faked clock is
    never the ordinary state of a deployment, and the one failure mode that
    matters is an operator who set it to test a rollover months ago and has been
    reading a lie ever since.
    """
    fake = override()
    if fake is None:
        return
    logger.warning(
        "%s=%s is in force: this instance believes today is %s, not %s. Month "
        "rollover, what may still be edited, and anything else that depends on "
        "the date will behave accordingly. Unset the variable and restart to go "
        "back to the real clock.",
        FAKE_TODAY_ENV, fake.isoformat(), fake.isoformat(), date.today().isoformat(),
    )
