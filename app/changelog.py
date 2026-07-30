"""CHANGELOG.md, parsed once and rendered for the in-app modal.

The file stays exactly where it is — the project root — and is read from there.
There is no second copy of the release notes anywhere in the app, which is the
whole point: the changelog a maintainer edits IS the changelog a user reads.

Because it lives outside app/, the Dockerfile has to copy it in explicitly; see
the COPY line there. Without it the modal renders empty in the container and
nothing else fails, which is exactly the sort of quiet breakage worth naming.

Parsed once per process, never invalidated. The file cannot change under a
running server: images are built by CI on push to main, so a new changelog only
ever arrives with a new container. The container IS the cache boundary, which is
why there is no mtime check here — unlike app/assets.py, which is guarding
against edits to files a long-lived dev server keeps serving.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

# app/changelog.py -> app/ -> the project root, where CHANGELOG.md lives.
CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# Every release heading, e.g. `## 🏷️ [1.1.5] - 2026-07-28`. The version is
# bracketed and the trailing field is free text rather than a date pattern on
# purpose: an in-progress section is dated "Unreleased", and a parser that only
# accepted ISO dates would silently drop the release currently being written —
# the one most worth reading.
_RELEASE_RE = re.compile(
    r"^##\s+(?:\S+\s+)?\[(?P<version>[^\]]+)\]\s*-\s*(?P<date>.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Release:
    """One release: its heading fields, and its body already rendered to HTML."""
    version: str
    date: str
    html: str

    @property
    def is_unreleased(self) -> bool:
        return self.date.strip().lower() == "unreleased"


# `html=False` drops raw HTML in the source rather than passing it through. The
# changelog contains none today, so this costs nothing — and it means a stray
# <script> in a future changelog edit cannot become stored XSS on a signed-in
# page. Cheaper and harder to get wrong than sanitizing the output afterwards.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_md.enable("table")
_md.enable("strikethrough")


def _render(markdown: str) -> str:
    return _md.render(markdown).strip()


def parse(text: str) -> list[Release]:
    """Split the document on its release headings and render each body.

    Anything above the first release heading — the `# Changelog` title and the
    Keep a Changelog line — is dropped: the modal has its own header, so
    repeating it inside would just be noise.
    """
    matches = list(_RELEASE_RE.finditer(text))
    releases = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end]
        releases.append(Release(
            version=match.group("version").strip(),
            date=match.group("date").strip(),
            html=_render(body),
        ))
    return releases


def _load() -> list[Release]:
    try:
        text = CHANGELOG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        # An empty list renders an empty modal rather than a 500. The changelog
        # is a nicety; nothing else on the page should break because it is
        # missing. Logged at warning because in a container it means the
        # Dockerfile's COPY was dropped, which is worth seeing.
        logger.warning("Could not read %s: %s", CHANGELOG_PATH, exc)
        return []
    releases = parse(text)
    if not releases:
        logger.warning("%s parsed to no releases; is the heading format still "
                       "`## [x.y.z] - date`?", CHANGELOG_PATH)
    return releases


_cache: list[Release] | None = None


def releases() -> list[Release]:
    """Every release, newest first as the file itself orders them."""
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def reset_cache() -> None:
    """Drop the parsed copy. For tests, which write their own changelog files."""
    global _cache
    _cache = None


def current_version() -> str:
    """The version to show in the UI: the newest release heading's version.

    That heading's own date field, not this function, is what decides whether
    the release is "Unreleased" — the version number is real and displayed
    either way, so a reader always sees what the running code actually is.
    Empty string if the changelog didn't parse at all (see releases()), so a
    template can render past a missing version the same way it renders past a
    missing changelog.
    """
    rs = releases()
    return rs[0].version if rs else ""
