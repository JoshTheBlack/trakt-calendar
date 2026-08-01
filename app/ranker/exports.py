"""Turning a board into the thing a user takes away.

One job, in two halves that share a beginning: `consolidate` decides WHICH
titles, in WHAT order, an export is of — and every export goes through it. The
image, the Markdown and the live preview all render the same list, so a grid and
the text block pasted beside it can never disagree about who came fourth.

No SQL, no HTTP, no Pillow. It is handed a board exactly as the data layer reads
one and returns plain values; a caller with a board in hand can consolidate it
without a database connection.

THE ORDERING, and where each part of it is decided:
  - Tiers come first by `rank_priority`, higher above lower.
  - Ties fall back to `sort_order`, then to row id. Those two are NOT re-derived
    here: the data layer's own `ORDER BY sort_order, id` already put the
    categories in that order, and this module's sort is stable, so the tie-break
    is inherited rather than duplicated. The same holds for items within a tier,
    which arrive ordered by `rank_in_category, id`.
  - Isolated tiers keep their own 1..X numbering and stay out of the
    consolidated list unless one is named as the scope outright.
  - Pool titles are never exported. They are the working set, not the ranking.

The ordering has to be TOTAL and STABLE or two exports of an unchanged board
would differ, which is the one thing a "top 25" image must never do.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..media import user_images
from ..providers.base import item_key

logger = logging.getLogger(__name__)

SCOPES = frozenset({"global", "category"})

# The export ceiling. Above this the canvas stops being something a person looks
# at and starts being something a browser struggles to open.
MAX_TOP_X = 100

# How many finished renders are kept per account. Small on purpose: it exists so
# that clicking Download twice, or re-exporting after changing nothing, is free —
# not to be an archive of everything anyone ever made.
MAX_CACHED_RENDERS = 12

# What survives into a download filename. Everything else is dropped, CR and LF
# emphatically included: a filename reaches the client inside a header, and a
# newline in a header is an injection rather than a formatting quirk.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9 ._-]+")
_WHITESPACE = re.compile(r"\s+")
MAX_FILENAME_STEM = 60
FALLBACK_FILENAME = "ranking"


class ExportError(Exception):
    """The export cannot be produced as asked. Written to be shown to the user."""


@dataclass(frozen=True)
class RankedTitle:
    """One title's place in a finished export, carrying what both renderers
    need: the ranking itself, enough of the title to caption it, and the tier it
    came from."""
    rank: int
    media: str
    match_source: str
    match_id: str
    tmdb: int | None
    title: str
    year: int | None
    network: str
    tier_uid: str
    tier_label: str
    colour: str | None

    @property
    def key(self) -> str:
        """This title's flat item key.

        Built by the shared identity helper rather than formatted here: an export
        row addresses the same title the ranker and the tracker do, and a second
        spelling of the separator would come apart silently the day one of them
        changed it.
        """
        return item_key(self.media, self.match_source, self.match_id)


def consolidate(
    board: Mapping[str, Any],
    *,
    scope: str = "global",
    category_uid: str | None = None,
    top_x: int | None = None,
) -> list[RankedTitle]:
    """The ordered list an export is of.

    `board` is the shape the data layer returns: categories in sort order, each
    with its items in rank order, plus a pool this never looks at.

    Ranks are assigned AFTER slicing decisions, so the first title is always 1
    whether the scope is every global tier consolidated or one isolated tier on
    its own.
    """
    if scope not in SCOPES:
        raise ExportError("Export scope must be either the whole board or one tier.")
    categories = list(board.get("categories") or [])

    if scope == "category":
        if not category_uid:
            raise ExportError("Choose which tier to export.")
        chosen = [c for c in categories if c.get("uid") == category_uid]
        if not chosen:
            raise ExportError("That tier is not on this board.")
        contributing = chosen
    else:
        # Isolated tiers are deliberately absent: they hold a list that is scored
        # against itself, and folding one into the global ranking would place
        # its titles against titles they were never compared with.
        contributing = sorted(
            (c for c in categories if not c.get("is_isolated")),
            key=lambda c: -int(c.get("rank_priority") or 0),
        )

    placed = [
        (category, item)
        for category in contributing
        for item in (category.get("items") or [])
    ]
    ranked = [
        RankedTitle(
            rank=position,
            media=str(item.get("media") or "show"),
            match_source=str(item.get("match_source") or ""),
            match_id=str(item.get("match_id") or ""),
            tmdb=item.get("tmdb"),
            title=str(item.get("title") or ""),
            year=item.get("year"),
            network=str(item.get("network") or ""),
            tier_uid=str(category.get("uid") or ""),
            tier_label=str(category.get("label") or ""),
            colour=category.get("colour"),
        )
        for position, (category, item) in enumerate(placed, start=1)
    ]
    if top_x is not None:
        if top_x < 1:
            raise ExportError("A top list needs at least one title.")
        ranked = ranked[:top_x]
    if not ranked:
        raise ExportError("There is nothing tiered to export yet.")
    return ranked


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def to_markdown(
    ranked: list[RankedTitle],
    *,
    title: str = "",
    emojis: Mapping[str, str] | None = None,
    default_emoji: str = "",
) -> str:
    """The same ranking as a pasteable block.

    Tier headings appear only when the export actually spans several tiers —
    a single-tier export is a list, and giving it one heading with everything
    under it just adds a line to delete.

    NUMBERING IS EXPLICIT UNDER HEADINGS. A Markdown ordered list is renumbered
    from 1 by every renderer, so a section that starts at 8 would render as 1;
    grouped output uses bullets with the rank written into the text, and only a
    single flat list uses real ordered-list syntax.

    `emojis` is a network -> emoji map the caller supplies, or nothing. This
    module does not know where such a map might come from, which is what keeps
    the Markdown export working identically for an account that has no such
    preferences at all.
    """
    lines: list[str] = []
    if title:
        lines += [f"# {title}", ""]

    tiers = _grouped_by_tier(ranked)
    grouped = len(tiers) > 1
    for tier_label, titles in tiers:
        if grouped:
            lines += [f"## {tier_label or 'Untitled'}", ""]
        lines += [
            f"- **{entry.rank}.** {_markdown_line(entry, emojis, default_emoji)}"
            if grouped else
            f"{entry.rank}. {_markdown_line(entry, emojis, default_emoji)}"
            for entry in titles
        ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _grouped_by_tier(ranked: list[RankedTitle]) -> list[tuple[str, list[RankedTitle]]]:
    """The ranking split into runs of one tier, in the order it is already in.
    Runs rather than a grouping by key: the consolidated order is the answer,
    and regrouping would be a second, disagreeing ordering."""
    groups: list[tuple[str, list[RankedTitle]]] = []
    for entry in ranked:
        if groups and groups[-1][0] == entry.tier_label:
            groups[-1][1].append(entry)
        else:
            groups.append((entry.tier_label, [entry]))
    return groups


def _markdown_line(entry: RankedTitle, emojis: Mapping[str, str] | None,
                   default_emoji: str) -> str:
    prefix = ""
    if emojis is not None and entry.network:
        prefix = str(emojis.get(entry.network) or default_emoji or "")
        if prefix:
            prefix += " "
    year = f" ({entry.year})" if entry.year else ""
    return f"{prefix}**{entry.title or 'Untitled'}**{year}"


# ---------------------------------------------------------------------------
# the render cache
# ---------------------------------------------------------------------------

def render_key(
    ranked: list[RankedTitle],
    *,
    renderer_version: int,
    columns: int,
    scale: float,
    fmt: str,
    show_titles: bool,
    podium: bool,
    title: str,
    username: str,
    header_image: bytes | None,
) -> str:
    """A hash of everything that can change a single pixel.

    Everything, deliberately: the titles and their order, the tier colours that
    tint their numbers, every layout option, the header line, the bytes of the
    header image, and the renderer's own version. An input the key omits is an
    input a user can change and be handed back the old image for — and the
    renderer version is what stops a layout change serving yesterday's geometry
    to everyone who exported before it.
    """
    payload = {
        "renderer": renderer_version,
        "titles": [
            [entry.rank, entry.media, entry.match_id, entry.tmdb, entry.colour,
             entry.title]
            for entry in ranked
        ],
        "columns": columns,
        "scale": scale,
        "fmt": fmt,
        "show_titles": show_titles,
        "podium": podium,
        "title": title,
        "username": username,
        "header": hashlib.sha256(header_image).hexdigest() if header_image else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def cache_path(user_id: int, year: int, board_uid: str, key: str, fmt: str) -> Path:
    """Where a finished render is kept. Under the account's own directory, so
    deleting the account sweeps every image it ever generated along with
    everything else it owns."""
    return user_images.generated_dir(user_id, year) / f"{board_uid}-{key}.{fmt}"


def cached_render(path: Path) -> bytes | None:
    """A previously rendered file, or None. A cache that cannot be read is a
    cache miss, never an error: the render that would replace it is the same
    render that produced it."""
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


def store_render(path: Path, payload: bytes) -> None:
    """Keep a finished render, then trim the account back to the cap.

    Best-effort throughout. Failing to cache an image the caller is already
    holding is not a reason to fail the export.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except OSError:
        logger.warning("Could not cache a generated grid at %s", path, exc_info=True)
        return
    _trim(path.parent.parent)


def _trim(generated_root: Path) -> None:
    """Keep the newest MAX_CACHED_RENDERS files under an account's generated
    directory, oldest evicted first. Counted across years, because the cap is
    about one account's disk rather than about any one board."""
    try:
        files = sorted(
            (p for p in generated_root.rglob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except OSError:
        return
    for stale in files[MAX_CACHED_RENDERS:]:
        try:
            stale.unlink()
        except OSError:
            continue


# ---------------------------------------------------------------------------
# download naming
# ---------------------------------------------------------------------------

def download_name(parts: Iterable[str], extension: str) -> str:
    """A filename safe to put in a Content-Disposition header.

    Built from the board name and the export title, which are the user's own
    text and therefore untrusted. Anything outside a conservative alphabet is
    dropped rather than escaped, and when nothing survives — a board named
    entirely in a script this filter removes — a constant is used instead of an
    empty name.
    """
    joined = " ".join(part for part in parts if part)
    cleaned = _WHITESPACE.sub(" ", _FILENAME_SAFE.sub(" ", joined)).strip()
    stem = cleaned[:MAX_FILENAME_STEM].strip(" .-_") or FALLBACK_FILENAME
    return f"{stem}.{extension}"
