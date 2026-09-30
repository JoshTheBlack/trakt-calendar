"""Every video the services list for one title, pooled into what the panel plays.

THE ONE EXCEPTION TO "ONE SERVICE DESCRIBES A TITLE". Every other field on the
details panel is the describing service's own (app/providers/detail_source.py),
because two services' descriptions mixed field by field would be a card nobody
wrote. Videos are pooled instead, and the reason is what a wrong answer costs: a
missing or wrong default video is recoverable only when the alternatives are on
screen and say who listed them. Yaga (Trakt 319458) is the case — Trakt sets no
trailer on it, so a panel reading one field from one service showed nothing at
all, while both services list a teaser and a sneak peek.

PURE: lists in, one ordered list out. Asking the services is detail_source's job.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from .base import Video, VideoKind

# What a viewer opening a panel most likely wants to see first: a trailer, then
# a teaser (before a premiere the teaser is often all there is), then anything.
_KIND_RANK = {VideoKind.TRAILER: 0, VideoKind.TEASER: 1}


def _known(video: Video) -> int:
    """How much a service said about a video — its type, whether it is official,
    when it was published. Trakt states all three and Simkl none of them."""
    return ((video.kind is not VideoKind.VIDEO) + (video.official is not None)
            + (video.published is not None))


def _merged(held: Video, other: Video) -> Video:
    """One video two services both list: every service named, and the richer
    service's description of it, since a service that states a type and a date
    knows more about the video than one that only names it."""
    richer = other if _known(other) > _known(held) else held
    sources = held.sources + tuple(s for s in other.sources if s not in held.sources)
    return richer._replace(sources=sources)


def pool(lists: Iterable[Sequence[Video]], describer: str) -> list[Video]:
    """Every video in `lists`, each once, in the order the panel offers them.

    `lists` is each service's own list, in the registry's declared order; the
    same video listed twice — by two services, or twice by one under two titles
    (measured on Trakt's film lists) — is one entry naming every service that
    lists it (`Video.key` says what "the same video" means).

    THE ORDER: trailers, then teasers, then everything else; within a kind, the
    video `describer` lists first, then an official one before one that is not
    or does not say, then the newest where a date is known. What a service
    listed first stays first after that, since nothing else distinguishes them.

    THE FIRST ROW IS WHAT PLAYS, so the first video that can play in the panel's
    own frame (`Video.embeddable`) is lifted to the front: a link out cannot be
    the default when there is something the panel could have played instead.
    """
    merged: dict[tuple[str, str], Video] = {}
    for listed in lists:
        for video in listed:
            held = merged.get(video.key)
            merged[video.key] = video if held is None else _merged(held, video)
    ordered = sorted(merged.values(), key=lambda v: v.published or "", reverse=True)
    ordered.sort(key=lambda v: (_KIND_RANK.get(v.kind, 2),
                                describer not in v.sources,
                                v.official is not True))
    playable = next((v for v in ordered if v.embeddable), None)
    if playable is not None:
        ordered.remove(playable)
        ordered.insert(0, playable)
    return ordered
