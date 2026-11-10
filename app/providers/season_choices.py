"""The seasons a picker offers for one show — the ONE checklist every surface draws
(templates/_season_checklist.html): the tracker's add, the rankings board's add
and a row's own season control, and the calendar's jump.

PURE: it is handed a service's season list (`SeasonsAnswer.seasons`), what the
surface already holds and the viewer's latest finished season, and answers with
the choices to draw. No database, no HTTP, no feature — each route gathers those,
this decides, and every picker therefore offers, greys and suggests a season by
one rule. In providers/ because the season list it reads is a port's answer and
three features draw it.

THE RULE:
  - EVERY SEASON A SERVICE LISTS IS OFFERED, plus any the surface holds, the row
    is, or the viewer finished — the list is a lookup that may have failed or be
    stale, and a picker that could not show where a season already is would be
    unable to say so.
  - A SEASON ALREADY HELD IS OFFERED BUT NOT PICKABLE, said in the surface's own
    words (`held_as`: "on the board", "on your tracker"), or — where the surface
    can send the viewer to it — a link to where it is (`href`).
  - A SEASON THE SURFACE MAY NOT OFFER is listed and refused with its reason
    (`blocked`: the calendar's "your filters hide it").
  - ONE IS SUGGESTED: the latest season the viewer has FINISHED, where the
    tracker knows one; otherwise the latest season that has AIRED, by the
    service's own premiere dates. A season still to premiere is never the
    suggestion — suggesting what nobody has seen is suggesting a guess — but it
    is listed and can be chosen.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

# Season 0 is a show's specials: a real season, but never the one to suggest,
# because "the latest season" of a show means its latest numbered one.
SPECIALS = 0


def season_word(season: int) -> str:
    """One season, as every surface says it: "Season 3", or "Specials" for 0."""
    return "Specials" if season == SPECIALS else f"Season {season}"


@dataclass(frozen=True)
class SeasonOption:
    """One choice in a picker. `season` None is the whole show (only the board's
    row control offers that — a row can be the whole show)."""
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
    # How this surface says a held season is already there.
    held_as: str = "on the board"
    # Why this season cannot be picked here, said beside it; "" when it can.
    blocked: str = ""
    # Where a held season already is, when the surface can link to it.
    href: str = ""
    # What the choice is called when it is not a season of a show — the
    # calendar's one choice for a film is its release.
    name: str = ""
    # How many episodes the service lists for it, said beside it; None unknown.
    episodes: int | None = None

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        return "Whole show" if self.season is None else season_word(self.season)

    @property
    def pickable(self) -> bool:
        return not (self.held or self.blocked)

    @property
    def notes(self) -> tuple[str, ...]:
        """What is said beside the choice, in words rather than only a colour:
        where it already is, why it cannot be picked, whether it is the
        suggestion, and — for a season not aired yet — when it premieres, if
        anybody has said."""
        notes = []
        if self.episodes:
            notes.append(f"{self.episodes} ep" + ("s" if self.episodes != 1 else ""))
        if self.current:
            notes.append("this row")
        elif self.held:
            notes.append(self.held_as)
        if self.blocked:
            notes.append(self.blocked)
        if self.suggested:
            notes.append("suggested")
        if self.season is not None and self.aired is False:
            notes.append(f"airs {self.premiere.day} {self.premiere:%b %Y}"
                         if self.premiere else "not aired yet")
        return tuple(notes)

    @property
    def value(self) -> str:
        """What a form posts for this choice: the number, or "" for the whole
        show — which is what an empty season means everywhere a board row's
        season is read (app/ranker/item_keys.py `valid_season`)."""
        return "" if self.season is None else str(self.season)


def premiere_of(first_aired: Any) -> tuple[date | None, datetime | None]:
    """A service's `first_aired` as (the day, the instant when it states one).

    Trakt states a full ISO instant, Simkl and TMDB a bare day
    (app/providers/base.py `SeasonsAnswer`); anything unreadable is undated
    rather than an error, since a season with no date is still a season that can
    be chosen.
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
    held_as: str = "on the board",
) -> list[SeasonOption]:
    """The picker's options, in order: the whole show, then every season — a
    surface that offers no whole show drops the first (`seasons_only`).

    `listed` is a service's season list (`SeasonsAnswer.seasons`). `held` is
    every season the surface holds for this show, None for the whole show, and it
    includes the row's own `current` one. `finished` is the latest season the
    viewer has finished, or None. `held_as` is how this surface says "already
    here".
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
    options = [SeasonOption(None, held=None in held, current=current is None,
                            held_as=held_as)]
    for number in sorted(numbers):
        entry = by_season.get(number)
        day, _ = premiere_of(entry.get("first_aired")) if entry else (None, None)
        # UNDATED IS NOT "NOT AIRED": a season a service lists with no premiere
        # is one nobody has said anything about, and drawing it as still to come
        # would be a claim the list never made.
        aired = has_aired(entry.get("first_aired"), now) if day else None
        try:
            episodes = int((entry or {}).get("episode_count") or 0) or None
        except (TypeError, ValueError):
            episodes = None
        options.append(SeasonOption(
            number,
            premiere=day,
            # A season the viewer finished has aired whatever the list says.
            aired=True if number == finished else aired,
            held=number in held,
            current=number == current,
            suggested=number == suggestion,
            held_as=held_as,
            episodes=episodes,
        ))
    return options


def seasons_only(options: Iterable[SeasonOption]) -> list[SeasonOption]:
    """`options` without the whole show — what an ADD offers, since an add files
    seasons (a board's whole-show row is added by the result's own row)."""
    return [option for option in options if option.season is not None]


def only(options: Iterable[SeasonOption], season: int) -> list[SeasonOption]:
    """The one choice for a result that IS one season (a service that files each
    season as its own title): asking which would be a question it has already
    answered. Offered even when the list did not name it."""
    kept = [option for option in options if option.season == season]
    return kept or [SeasonOption(season)]


def blocking(options: Iterable[SeasonOption], reasons: Mapping[int, str],
             links: Mapping[int, str] | None = None) -> list[SeasonOption]:
    """`options` with each season's refusal (`reasons`) and the link to where a
    held one already is (`links`) — the calendar's, which knows both only once
    the title is described."""
    links = links or {}
    return [replace(option, blocked=reasons.get(option.season, ""),
                    href=links.get(option.season, ""),
                    held=option.held or option.season in links)
            for option in options]


# The prefix a checklist names each ticked season with: ONE FLAT FIELD PER
# SEASON, `season:<n>`, because json-enc makes a repeated name a list only when
# there is more than one of it — a single tick would arrive as a string and two
# as a list.
FIELD_PREFIX = "season:"


def ticked(data: Mapping[str, Any]) -> list[str]:
    """The seasons a checklist ticked, as the strings it posted, in the order the
    form drew them. Each surface validates them as it files them."""
    return [name[len(FIELD_PREFIX):] for name, value in data.items()
            if str(name).startswith(FIELD_PREFIX) and value]


def _suggestion(by_season: Mapping[int, Mapping[str, Any]], finished: int | None,
                now: datetime) -> int | None:
    """The tracker's latest finished season, else the latest aired one."""
    if finished is not None:
        return finished
    aired = [number for number, entry in by_season.items()
             if number != SPECIALS and has_aired(entry.get("first_aired"), now)]
    return max(aired, default=None)
