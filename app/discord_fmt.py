"""Bucketing state machine + POST 1/POST 2 markdown renderer (BUILD_PLAN §4).

Pure, offline functions — no I/O, no Trakt calls, no persistence. Callers
(app/main.py) merge each show's stored record (app/distrakt.py, identity +
`abandoned`/`abandoned_form`) with its live Trakt-derived fields (app/trakt.py
`fetch_watched_shows` + `fetch_season_detail`) into one flat dict before calling
anything here.

LIVE SHOW SHAPE (one dict per show+season, used throughout this module):
  title (str), season (int), network (str),
  abandoned (bool), abandoned_form (str | None),
  watched (int, "x"), total (int, "y"),
  cadence ("b" | "Sun".."Sat" | None),
  premiere (str "M/D" | None), finale (str "M/D" | None),
  started_airing (bool), finished_airing (bool).

Exact literal formats below are verified against a hand-provided July sample
(not in BUILD_PLAN.txt — pasted directly into the CHAT 4 conversation); see
"CHAT 4 — AS IMPLEMENTED" for the parts of §4 the sample clarified or corrected.
"""
from __future__ import annotations

# Keepup groups Sun..Sat (§4); only weekdays with at least one show get a header.
_WEEKDAY_ORDER = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


_LEADING_ARTICLES = ("the ", "a ", "an ")


def _sort_title(title) -> str:
    """Alphabetical sort key ignoring a leading article (case-insensitive).

    §4's text only says "ignoring a leading 'The'", but the hand-provided July
    sample sorts "A Good Girl's Guide to Murder" under G (between "The Four
    Seasons" and "Half Man") — i.e. leading "A"/"An" are ignored too, standard
    title-alphabetization style, not just "The"."""
    t = (title or "").strip()
    low = t.lower()
    for article in _LEADING_ARTICLES:
        if low.startswith(article):
            return t[len(article):].lower()
    return low


def _season_tag(season) -> str:
    return f"S{int(season or 0):02d}"


def _counts(show: dict) -> str:
    return f"{int(show.get('watched') or 0)}/{int(show.get('total') or 0)}"


def _date_or_unknown(value) -> str:
    return value or "?/?"


def _emoji_for(show: dict, emoji_map: dict, default_emoji: str) -> str:
    return emoji_map.get(show.get("network") or "", default_emoji)


def _premiere_sort_key(show: dict):
    """(month, day, title) — the July sample sorts New/Returning chronologically
    by premiere date, not alphabetically; unknown premieres sort last. Same-day
    ties break alphabetically ignoring "The" (matches the sample's 7/15 and 7/16
    ties)."""
    premiere = show.get("premiere")
    month, day = 99, 99
    if premiere:
        try:
            month, day = (int(p) for p in premiere.split("/", 1))
        except (ValueError, TypeError):
            month, day = 99, 99
    return (month, day, _sort_title(show.get("title")))


# ---------------------------------------------------------------------------
# Bucketing state machine (§4 lifecycle)
# ---------------------------------------------------------------------------

def bucket_of(rec: dict, live: dict) -> str:
    """One of: new, returning, keepup, cleanup, completed, abandoned.

    `rec` carries identity + the manual `abandoned` flag; `live` carries the
    Trakt-derived counts/dates/airing flags. Callers may pass the same merged
    dict for both (see LIVE SHOW SHAPE) — the two-arg split just keeps the
    manual/computed inputs conceptually separate, per the state machine:

      New/Returning --(starts airing)--> Keepup (weekly) OR Cleanup (binge)
      Keepup --(finale airs)--> Cleanup
      any --(season fully watched)--> Completed (auto)
      any --(user abandons)--> Abandoned (manual, checked first — an
      abandon can happen from any other state)
    """
    if rec.get("abandoned"):
        return "abandoned"
    watched = int(live.get("watched") or 0)
    total = int(live.get("total") or 0)
    if total > 0 and watched >= total:
        return "completed"
    if not live.get("started_airing"):
        return "new" if int(rec.get("season") or 1) == 1 else "returning"
    if live.get("cadence") == "b":
        return "cleanup"  # binge goes straight to Cleanup, skipping Keepup
    if live.get("finished_airing"):
        return "cleanup"  # weekly, finale has aired
    return "keepup"  # weekly, still airing


# ---------------------------------------------------------------------------
# Per-bucket line renderers (exact inline forms, §4)
# ---------------------------------------------------------------------------

def _new_returning_line(show: dict, emoji_map: dict, default_emoji: str) -> str:
    """> :emoji:`Title SXX (x/y, CAD)` PREM[ - FIN]
    binge: PREM only. weekly: "PREM - FIN". No known cadence (premiere known but
    no air-date pattern yet): CAD omitted, "PREM - FIN" with FIN "?/?" (an edge
    case not in the sample; see AS IMPLEMENTED)."""
    cadence = show.get("cadence")
    cad_part = f", {cadence}" if cadence else ""
    title_part = f"{show.get('title', '')} {_season_tag(show.get('season'))} ({_counts(show)}{cad_part})"
    emoji = _emoji_for(show, emoji_map, default_emoji)
    premiere = _date_or_unknown(show.get("premiere"))
    if cadence == "b":
        return f"> {emoji}`{title_part}` {premiere}"
    finale = _date_or_unknown(show.get("finale"))
    return f"> {emoji}`{title_part}` {premiere} - {finale}"


def _keepup_line(show: dict, emoji_map: dict, default_emoji: str) -> str:
    """> :emoji:`Title SXX (x/y)` FIN — CAD/PREM removed (weekday is the group
    header), FIN stays (possibly "?/?" if the tail isn't fully scheduled yet)."""
    emoji = _emoji_for(show, emoji_map, default_emoji)
    title_part = f"{show.get('title', '')} {_season_tag(show.get('season'))} ({_counts(show)})"
    finale = _date_or_unknown(show.get("finale"))
    return f"> {emoji}`{title_part}` {finale}"


def _cleanup_line(show: dict, emoji_map: dict, default_emoji: str) -> str:
    """> :emoji:`Title SXX (x/y)` — no dates at all."""
    emoji = _emoji_for(show, emoji_map, default_emoji)
    title_part = f"{show.get('title', '')} {_season_tag(show.get('season'))} ({_counts(show)})"
    return f"> {emoji}`{title_part}`"


def _completed_line(show: dict, emoji_map: dict, default_emoji: str) -> str:
    """> :emoji: ~~`Title SXX`~~ — no counts/dates. The emoji is OUTSIDE the
    strikethrough: Discord won't render a custom emoji wrapped in ~~ ~~, so only
    the title is struck."""
    emoji = _emoji_for(show, emoji_map, default_emoji)
    title_part = f"{show.get('title', '')} {_season_tag(show.get('season'))}"
    return f"> {emoji} ~~`{title_part}`~~"


def freeze_form(show: dict) -> str:
    """The backtick-wrapped inline form to snapshot at abandon-time (§4/§5): the
    show's current bucket-appropriate counts form, minus any premiere/finale
    dates — "(x/y, CAD)" if it hasn't started airing yet, else "(x/y)", else
    (fully watched) just the title+season with no counts.

    Deliberately does not call bucket_of / look at `abandoned` — this freezes
    what the state WOULD be right now, independent of the toggle being applied.
    Reused both by app/main.py's abandon endpoint (to freeze `abandoned_form`)
    and by `_abandoned_line` below as the fallback for pre-Chat-4 abandoned
    records where `abandoned_form` is still None.
    """
    title = show.get("title", "")
    season_tag = _season_tag(show.get("season"))
    watched = int(show.get("watched") or 0)
    total = int(show.get("total") or 0)
    if not show.get("started_airing"):
        cadence = show.get("cadence")
        cad_part = f", {cadence}" if cadence else ""
        return f"`{title} {season_tag} ({_counts(show)}{cad_part})`"
    if total > 0 and watched >= total:
        return f"`{title} {season_tag}`"
    return f"`{title} {season_tag} ({_counts(show)})`"


def _abandoned_line(show: dict, emoji_map: dict, default_emoji: str) -> str:
    """> :emoji: ~~`form`~~ — emoji outside the strikethrough (see _completed_line)."""
    emoji = _emoji_for(show, emoji_map, default_emoji)
    form = show.get("abandoned_form") or freeze_form(show)
    return f"> {emoji} ~~{form}~~"


# ---------------------------------------------------------------------------
# Section / post assembly
# ---------------------------------------------------------------------------

def _section(header: str, lines: list[str]) -> str:
    """Mandatory sections (New/Returning/Cleanup/Keepup) always render their
    header, even with zero lines; only Completed/Abandoned are conditionally
    omitted entirely by the caller (§4: "omitting empty optional sections")."""
    return header + ("\n" + "\n".join(lines) if lines else "")


def _group_by_bucket(shows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {
        "new": [], "returning": [], "keepup": [], "cleanup": [], "completed": [], "abandoned": [],
    }
    for show in shows:
        groups[bucket_of(show, show)].append(show)
    return groups


def _render_keepup(shows: list[dict], emoji_map: dict, default_emoji: str) -> str:
    by_day: dict[str, list[dict]] = {d: [] for d in _WEEKDAY_ORDER}
    for show in shows:
        day = show.get("cadence")
        if day in by_day:
            by_day[day].append(show)
    lines = ["## **Keepup**"]
    for day in _WEEKDAY_ORDER:
        group = by_day[day]
        if not group:
            continue
        group.sort(key=lambda s: _sort_title(s.get("title")))
        lines.append(f"*{day}*")
        lines.extend(_keepup_line(s, emoji_map, default_emoji) for s in group)
    return "\n".join(lines)


def render_post1(shows: list[dict], emoji_map: dict | None = None, default_emoji: str = ":tv:",
                 link_url: str | None = None) -> str:
    """POST 1 (announcement): **New Shows** + **Returning**, optionally followed
    by a link line pointing at the poster's own public calendar.

    `link_url` is omitted entirely when there is nothing to link to, rather than
    rendered as an empty or broken line. It is wrapped in angle brackets, which
    is Discord's own way of suppressing the link preview card — an announcement
    that already lists a month of shows does not want a second, larger block
    underneath it.
    """
    emoji_map = emoji_map or {}
    groups = _group_by_bucket(shows)
    news = sorted(groups["new"], key=_premiere_sort_key)
    returning = sorted(groups["returning"], key=_premiere_sort_key)
    sections = [
        _section("**New Shows**", [_new_returning_line(s, emoji_map, default_emoji) for s in news]),
        _section("**Returning**", [_new_returning_line(s, emoji_map, default_emoji) for s in returning]),
    ]
    if link_url:
        sections.append(f"**Full calendar:** <{link_url}>")
    return "\n\n".join(sections)


def _movie_line(movie: dict) -> str:
    """> ~~`Title (YYYY)`~~ — struck through, no emoji (per the user's sample)."""
    title = movie.get("title") or ""
    year = movie.get("year")
    label = f"{title} ({year})" if year else title
    return f"> ~~`{label}`~~"


def render_post2(shows: list[dict], emoji_map: dict | None = None, default_emoji: str = ":tv:",
                 movies: list[dict] | None = None) -> str:
    """POST 2 (living tracker): ## Cleanup, ## Keepup, New, Returning,
    [Completed], [Abandoned], [Movies] — the optional sections omitted when empty.

    `movies` is [{title, year, ...}] watched during the month (from the watch-
    history cache); rendered struck-through, alphabetized ignoring a leading
    article, at the very end."""
    emoji_map = emoji_map or {}
    groups = _group_by_bucket(shows)
    cleanup = sorted(groups["cleanup"], key=lambda s: _sort_title(s.get("title")))
    completed = sorted(groups["completed"], key=lambda s: _sort_title(s.get("title")))
    abandoned = sorted(groups["abandoned"], key=lambda s: _sort_title(s.get("title")))
    news = sorted(groups["new"], key=_premiere_sort_key)
    returning = sorted(groups["returning"], key=_premiere_sort_key)

    sections = [
        _section("## **Cleanup**", [_cleanup_line(s, emoji_map, default_emoji) for s in cleanup]),
        _render_keepup(groups["keepup"], emoji_map, default_emoji),
        _section("**New Shows**", [_new_returning_line(s, emoji_map, default_emoji) for s in news]),
        _section("**Returning**", [_new_returning_line(s, emoji_map, default_emoji) for s in returning]),
    ]
    if completed:
        sections.append(_section("**Completed**", [_completed_line(s, emoji_map, default_emoji) for s in completed]))
    if abandoned:
        sections.append(_section("**Abandoned**", [_abandoned_line(s, emoji_map, default_emoji) for s in abandoned]))
    if movies:
        movs = sorted(movies, key=lambda m: _sort_title(m.get("title")))
        sections.append(_section("**Movies**", [_movie_line(m) for m in movs]))
    return "\n\n".join(sections)
