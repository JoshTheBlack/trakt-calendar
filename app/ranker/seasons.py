"""What a show row's season control offers, and which choice it suggests.

PURE: it is handed what the services, the board and (optionally) the viewer's
finished seasons already said, and answers with the options to draw. No database,
no HTTP, no provider — the route gathers those, this decides.

THE CONTROL SETS WHAT ONE BOARD ROW IS: the whole show, or one season of it. So it
offers "Whole show" and every season a service lists, and:
  - EVERY OPTION THE BOARD ALREADY HOLDS FOR THAT SHOW IS OFFERED BUT DISABLED,
    including the row's own current one. A board holds each season of a show at
    most once (the unique index over IFNULL(season, -1)), so an option that would
    collide with another row is never pressable — greyed rather than hidden, so
    the list still reads as the show's seasons and says where the rest already are.
  - ONE OPTION IS SUGGESTED: the latest season the viewer has FINISHED, where the
    tracker knows one; otherwise the latest season that has AIRED, by the
    service's own premiere dates. A season whose premiere is still in the future
    is never the suggestion — suggesting what nobody has seen is suggesting a
    guess — but it is listed and can be chosen.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

# Season 0 is a show's specials: a real season a row may be, but never the one to
# suggest, because "the latest season" of a show means its latest numbered one.
SPECIALS = 0


@dataclass(frozen=True)
class SeasonOption:
    """One choice in the control. `season` None is the whole show."""
    season: int | None
    # The premiere as the service dated it, reduced to a day; None when undated.
    premiere: date | None = None
    # Whether it has aired: True or False when a service listed it, None when no
    # service did (the lookup failed, or the list is stale) — which is "not
    # known", and must never be drawn as "not aired yet".
    aired: bool | None = None
    held: bool = False
    current: bool = False
    suggested: bool = False

    @property
    def label(self) -> str:
        if self.season is None:
            return "Whole show"
        if self.season == SPECIALS:
            return "Specials"
        return f"Season {self.season}"

    @property
    def notes(self) -> tuple[str, ...]:
        """What is said beside the choice, in words rather than only a colour:
        where it already is, whether it is the suggestion, and — for a season not
        aired yet — when it premieres, if anybody has said."""
        notes = []
        if self.current:
            notes.append("this row")
        elif self.held:
            notes.append("on the board")
        if self.suggested:
            notes.append("suggested")
        if self.season is not None and self.aired is False:
            notes.append(f"airs {self.premiere.day} {self.premiere:%b %Y}"
                         if self.premiere else "not aired yet")
        return tuple(notes)

    @property
    def value(self) -> str:
        """What the form posts for this choice: the number, or "" for the whole
        show — which is what an empty season means everywhere a row's season is
        read (app/ranker/item_keys.py `valid_season`)."""
        return "" if self.season is None else str(self.season)


def premiere_of(first_aired: Any) -> tuple[date | None, datetime | None]:
    """A service's `first_aired` as (the day, the instant when it states one).

    Trakt states a full ISO instant, Simkl a bare day (app/providers/base.py
    `SeasonsAnswer`); anything unreadable is undated rather than an error, since
    a season with no date is still a season that can be chosen.
    """
    text = str(first_aired or "").strip()
    if not text:
        return None, None
    if len(text) == 10:
        try:
            return date.fromisoformat(text), None
        except ValueError:
            return None, None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.date(), moment


def has_aired(first_aired: Any, now: datetime) -> bool:
    """Whether a season's premiere has happened by `now` (aware, UTC).

    An instant is compared as an instant. A bare day has aired once that day has
    begun in UTC — a day's premiere may be hours away for some viewers, and a
    suggestion that is early by a few hours on the day it airs is a smaller wrong
    than one that ignores a season a whole timezone has already seen.
    """
    day, moment = premiere_of(first_aired)
    if moment is not None:
        return moment <= now
    return day is not None and day <= now.astimezone(timezone.utc).date()


def season_options(
    listed: Iterable[Mapping[str, Any]],
    *,
    held: Iterable[int | None],
    current: int | None,
    finished: int | None,
    now: datetime,
) -> list[SeasonOption]:
    """The control's options, in order: the whole show, then every season.

    `listed` is a service's season list (`SeasonsAnswer.seasons`). `held` is
    every season the board holds for this show, None for the whole show, and it
    includes the row's own `current` one. `finished` is the latest season the
    viewer has finished, or None.

    A SEASON NO SERVICE LISTED IS STILL OFFERED when this row is it, the board
    holds it, or the viewer finished it: the list is a lookup that may have
    failed or be stale, and a control that could not show where a row already is
    would be unable to say what it is set to.
    """
    held = set(held)
    by_season: dict[int, Mapping[str, Any]] = {}
    for entry in listed:
        try:
            by_season[int(entry["season"])] = entry
        except (KeyError, TypeError, ValueError):
            continue
    numbers = set(by_season) | {s for s in held | {current, finished} if s is not None}

    suggestion = _suggestion(by_season, finished, now)
    options = [SeasonOption(None, held=None in held, current=current is None)]
    for number in sorted(numbers):
        entry = by_season.get(number)
        day, _ = premiere_of(entry.get("first_aired")) if entry else (None, None)
        aired = has_aired(entry.get("first_aired"), now) if entry else None
        options.append(SeasonOption(
            number,
            premiere=day,
            # A season the viewer finished has aired whatever the list says.
            aired=True if number == finished else aired,
            held=number in held,
            current=number == current,
            suggested=number == suggestion,
        ))
    return options


def _suggestion(by_season: Mapping[int, Mapping[str, Any]], finished: int | None,
                now: datetime) -> int | None:
    """The tracker's latest finished season, else the latest aired one."""
    if finished is not None:
        return finished
    aired = [number for number, entry in by_season.items()
             if number != SPECIALS and has_aired(entry.get("first_aired"), now)]
    return max(aired, default=None)

