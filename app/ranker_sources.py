"""Where titles come from, behind seams the rest of the ranker cannot see past.

The ranker takes titles from three kinds of place: a search, a ratings list, and
(optionally) the tracker. Each is a Protocol here with one implementation today,
because a second service is a question of when rather than whether — and the
cost of finding that out late is a provider's name threaded through routes,
templates and stored rows.

THE RULE THIS MODULE EXISTS TO ENFORCE: nothing outside a provider
implementation calls a provider function. Routes ask this module for a source
and get back `TitleRef`s, which say nothing about who produced them. Adding a
service means adding a class here (and, for the tracker seam, in
app/ranker_import.py) — not editing anything downstream.

CREDENTIALS ARE NOT THE SAME FOR ALL THREE, and getting it backwards is the easy
mistake:
  - SEARCH uses the instance's own credential. The rankings grant does not imply
    a linked account of any kind, so a search that reached for the caller's token
    would break for exactly the accounts this feature is designed to serve. What
    it asks for is public catalogue data, so the operator's credential is the
    right one.
  - RATINGS are private to one person and need THEIR token. With no linked
    identity there is no such token, and the action is hidden rather than
    offered and then failed.
"""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import trakt_routes
from .providers.trakt import TraktError, detail as trakt_detail, sync as trakt_sync
from .config import Settings, load_settings
# Re-exported deliberately: `ranker_sources.Media` and `ranker_sources.parse_media`
# are what the ranker's routes and data layer already import, and the vocabulary
# they name is the app's, not this feature's — the calendar and the tracker deal
# in the same two kinds of title. One definition, read from where it lives.
#
# `resolve_identity` comes from there too, and is CALLED here rather than
# re-exported: the tracker keys its own rows on the same waterfall's answer, so a
# second copy would be two definitions of what makes two titles the same one.
from .providers.base import Media, parse_media, resolve_identity  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TitleRef:
    """One title, said in a way that does not name where it came from.

    Everything downstream of a source — the routes, the data layer, the board —
    sees this and only this, which is what keeps a second provider from being a
    change to anything but a source class.

    `ids` carries the WHOLE map a provider knew, not just the id we matched on:
    an id dropped here is one a future cross-service match cannot use, and it
    costs a call already paid for to get back.
    """
    media: Media
    title: str
    ids: Mapping[str, Any] = field(default_factory=dict)
    year: int | None = None
    network: str = ""
    season_count: int | None = None
    episode_count: int | None = None
    runtime: int | None = None

    def identity(self) -> tuple[str, str] | None:
        """(match_source, match_id) by the waterfall, or None when this title
        shares no id we can key on."""
        return resolve_identity(self.ids)

    def as_item(self, **extra: Any) -> dict[str, Any] | None:
        """The mapping the board data layer stores, or None for a title with no
        usable id — which cannot be ranked, because nothing could tell it apart
        from another title with the same name.

        `extra` carries the fields only one caller knows, such as the score a
        ratings seed attaches.
        """
        identity = self.identity()
        if identity is None:
            return None
        match_source, match_id = identity
        return {
            "media": str(self.media),
            "match_source": match_source,
            "match_id": match_id,
            # The artwork key specifically, which is tmdb or nothing: TMDB is
            # what serves the image, so a title matched on tvdb still has no
            # poster to fetch and falls back to the placeholder.
            "tmdb": int_or_none(self.ids.get("tmdb")),
            "ids": dict(self.ids),
            "title": self.title,
            "year": self.year,
            "network": self.network,
            "season_count": self.season_count,
            "episode_count": self.episode_count,
            "runtime": self.runtime,
            **extra,
        }


@dataclass(frozen=True)
class RatedTitle:
    """A title with the score its owner gave it."""
    title: TitleRef
    rating: int
    rated_at: str | None = None


def int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def items_from_refs(refs, **extra: Any) -> list[dict[str, Any]]:
    """Every ref that resolved to an identity, in the shape the data layer takes.

    Titles that resolve to nothing are dropped with a log line rather than
    raising: one obscure entry in a search or an import is not a reason to
    refuse the other nineteen.
    """
    items = []
    for ref in refs:
        item = ref.as_item(**extra)
        if item is None:
            logger.info("ranker: dropping %r — no id in the match waterfall", ref.title)
            continue
        items.append(item)
    return items


class SourceUnavailable(RuntimeError):
    """A source could not answer — it was unreachable, refused the credential,
    or replied with something unreadable.

    PROVIDER-NEUTRAL ON PURPOSE. A route that caught the provider's own
    exception type would name that provider, which is the coupling the seams
    exist to prevent; each implementation translates its own failures into this
    on the way out. `status` carries the upstream code when there was one, so a
    401 can be reported as a credential problem rather than a generic outage.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NoLinkedIdentity(SourceUnavailable):
    """A per-user provider read was attempted for an account with no linked
    identity. Raised rather than returning empty, because "you have rated
    nothing" and "we could not ask" are different answers."""


# ---------------------------------------------------------------------------
# the seams
# ---------------------------------------------------------------------------

class TitleSearchSource(Protocol):
    """Find titles by name. Public catalogue data — see the credential note in
    the module docstring."""
    async def search(self, query: str, media: Media) -> list[TitleRef]: ...


class RatingsSource(Protocol):
    """One person's own scores, read with their own credential."""
    async def fetch_ratings(self, user_id: int) -> list[RatedTitle]: ...


class FinishedTitlesSource(Protocol):
    """Titles a person has finished watching, for the optional import.

    `media` and `year` are part of the seam rather than filters applied to its
    result because only the provider knows what "finished during 2026" means in
    its own data — for the tracker it is the month a completed record belongs
    to, which is not derivable from a TitleRef at all.
    """
    async def finished_titles(
        self, user_id: int, *, media: Media, year: int | None = None,
    ) -> list[TitleRef]: ...


# ---------------------------------------------------------------------------
# the Trakt implementations
# ---------------------------------------------------------------------------

@contextmanager
def _translating_failures():
    """Turn this provider's own failure type into the neutral one, at the edge
    of the implementation and nowhere else."""
    try:
        yield
    except TraktError as exc:
        raise SourceUnavailable(str(exc), getattr(exc, "status", None)) from exc


class TraktSearchSource:
    """`TitleSearchSource` over Trakt's /search, using the instance credential."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or load_settings()

    @property
    def configured(self) -> bool:
        return bool(self._settings.trakt_configured)

    async def search(self, query: str, media: Media) -> list[TitleRef]:
        with _translating_failures():
            entries = await trakt_detail.search_titles(self._settings, str(media), query)
        return [_ref_from_search(entry, media) for entry in entries]


def _ref_from_search(entry: Mapping[str, Any], media: Media) -> TitleRef:
    """One Trakt search result as a TitleRef.

    Season and episode counts stay None: a search result does not carry them,
    and a lookup per result to fill them in would spend a call apiece on
    information the picker does not show.
    """
    return TitleRef(
        media=media,
        title=entry.get("title") or "",
        ids=dict(entry.get("ids") or {}),
        year=int_or_none(entry.get("year")),
        network=entry.get("network") or "",
        runtime=int_or_none(entry.get("runtime")),
    )


class TraktRatingsSource:
    """`RatingsSource` over Trakt's /sync/ratings, read with the user's own
    token — never the instance credential, which would hand everybody the
    operator's ratings."""

    async def fetch_ratings(self, user_id: int) -> list[RatedTitle]:
        settings = await user_trakt_settings(user_id)
        if settings is None:
            # The caller is expected to have checked availability first; this is
            # the belt to that braces, so a race between unlinking and clicking
            # cannot read with somebody else's credential.
            raise NoLinkedIdentity("No linked account to read ratings from.")
        with _translating_failures():
            entries = await trakt_sync.fetch_ratings(settings)
        rated = []
        for entry in entries:
            media_key = entry.get("type")
            if media_key not in ("show", "movie"):
                # Season and episode ratings exist too; neither is a title a
                # board can hold.
                continue
            media = Media(media_key)
            item = entry.get(media_key) or {}
            ids = trakt_detail.ids_map(item) if isinstance(item, dict) else {}
            score = int_or_none(entry.get("rating"))
            if not ids or score is None:
                continue
            rated.append(RatedTitle(
                title=TitleRef(
                    media=media,
                    title=item.get("title") or "",
                    ids=ids,
                    year=int_or_none(item.get("year")),
                    network=item.get("network") or "",
                    runtime=int_or_none(item.get("runtime")),
                ),
                rating=score,
                rated_at=entry.get("rated_at"),
            ))
        return rated


async def user_trakt_settings(user_id: int) -> Settings | None:
    """The app-wide settings with the Trakt credential swapped for `user_id`'s
    own, or None when they have not linked an account.

    Deliberately not shared with the tracker's equivalent helper: that one exists
    to read somebody's private watch history and answers to the tracker's own
    access level, this one exists to read their ratings and answers to a grant
    that does not imply a linked account at all. The two look alike today; they
    are not the same rule, and merging them would put the tracker's assumption
    (there is always a token) behind a feature where there often is not.
    """
    token = await trakt_routes.access_token_for_user(user_id)
    if not token:
        return None
    return dataclasses.replace(
        load_settings(), trakt_access_token=token, trakt_refresh_token="",
    )


# ---------------------------------------------------------------------------
# what a route asks for
# ---------------------------------------------------------------------------
# Routes call these rather than naming an implementation, which is both the seam
# and what lets a test drive the whole path with a fake source.

def search_source() -> TitleSearchSource:
    """The search seam, credentialled instance-wide."""
    return TraktSearchSource()


def ratings_source() -> RatingsSource:
    """The ratings seam. Resolves the caller's own credential when asked, so
    availability and use cannot disagree about whose token is in play."""
    return TraktRatingsSource()


async def ratings_available(user_id: int) -> bool:
    """Whether to OFFER a ratings seed at all. False hides the action rather
    than showing one that fails: an account with no linked identity has nothing
    to seed from, and a disabled button it can never enable is just noise."""
    return bool(await trakt_routes.access_token_for_user(user_id))
