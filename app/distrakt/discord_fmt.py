"""Bucketing state machine + POST 1/POST 2 markdown renderer.

Pure, offline functions — no I/O, no Trakt calls, no persistence. Callers
(live.py) merge each show's stored record (store.py, identity +
`abandoned`/`abandoned_form`) with its live Trakt-derived fields
(app/providers/trakt/ `fetch_season_detail` + the watch-history cache) into one
flat dict before calling anything here.

LIVE SHOW SHAPE (one dict per show+season, used throughout this module):
  title (str), season (int), network (str),
  abandoned (bool), abandoned_form (str | None),
  watched (int, "x"), total (int, "y"),
  cadence ("b" | "Sun".."Sat" | None),
  premiere (str "M/D" | None), finale (str "M/D" | None),
  started_airing (bool), finished_airing (bool).

Exact literal formats below are verified against a hand-provided sample of a
real month's posts, since Discord markdown spacing/punctuation is easy to get
subtly wrong from a written spec alone and the sample is what people actually
compare a generated post against. Where the sample disagreed with or went
beyond an earlier written description (e.g. which leading articles get ignored
when sorting — see `_sort_title`), the sample wins: it is the more concrete,
harder-to-misread source of truth.
"""
from __future__ import annotations

# Both vocabularies are named beside the record shape in store.py; this module is
# what DECIDES which bucket a show gets, and what a month in a given standing is
# allowed to say.
from . import store
from .store import Bucket, MonthStanding

# WHICH BUCKETS A MONTH MAY PRESENT, by where that month stands relative to the
# calendar, in the order they render. ONE table, read by the page's row list (the
# payload is filtered through month_view) and by POST 2 below, because the
# question is always the same one — "which month is this?" — and it has to be
# answered once. Answering it per section is exactly how a month that was over
# came to carry a Keepup, and a month that had not started came to carry a
# Cleanup with last month's abandoned title sitting in it.
#
# Cleanup and Keepup are statements about work in hand: what is airing right now,
# and what is waiting to be caught up on. A month that has not begun has neither
# yet — all it can honestly announce is what premieres in it. A month that is
# over settled both when it froze; what survives it is the verdict on each title
# (Completed, Abandoned) plus the films watched during it, and re-listing what
# was mid-flight on the last day reads as work still outstanding on a month
# nobody can do anything about. Only the month actually in progress carries the
# whole set.
#
# Films need no entry: they are not a bucket, and a month that has not happened
# has nothing watched in it to list.
#
# POST 1 IS NOT SUBJECT TO THIS TABLE, in any standing. It answers a different
# question — what PREMIERED in the month — and a premiere belongs to its month
# whatever became of it afterwards. Filtering it through a past month's row
# leaves an announcement of two finished titles and an empty Returning section,
# which is the opposite of what an announcement is for. See render_post1.
MONTH_BUCKETS: dict[MonthStanding, tuple[Bucket, ...]] = {
    MonthStanding.FUTURE: (Bucket.NEW, Bucket.RETURNING, Bucket.COMPLETED, Bucket.ABANDONED),
    MonthStanding.CURRENT: (Bucket.CLEANUP, Bucket.KEEPUP, Bucket.NEW, Bucket.RETURNING,
                            Bucket.COMPLETED, Bucket.ABANDONED),
    MonthStanding.PAST: (Bucket.COMPLETED, Bucket.ABANDONED),
}

# Keepup groups shows by air weekday, Sun..Sat; only weekdays with at least one
# show get a header, so a quiet Tuesday doesn't leave an empty section in the post.
_WEEKDAY_ORDER = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


_LEADING_ARTICLES = ("the ", "a ", "an ")


def _sort_title(title) -> str:
    """Alphabetical sort key ignoring a leading article (case-insensitive).

    Ignores "The"/"A"/"An", not just "The" — standard title-alphabetization
    style. Confirmed against a real sample post, which sorts "A Good Girl's
    Guide to Murder" under G (between "The Four Seasons" and "Half Man"),
    i.e. the leading "A" is stripped there too."""
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


def _premiere_month_day(show: dict) -> tuple[int, int] | None:
    """(month, day) of a show's season premiere from its "M/D" string, or None
    when it has no usable premiere date."""
    premiere = show.get("premiere")
    if not premiere:
        return None
    try:
        month, day = (int(p) for p in str(premiere).split("/", 1))
        return month, day
    except (ValueError, TypeError):
        return None


def _premiere_sort_key(show: dict):
    """(month, day, title) — the July sample sorts New/Returning chronologically
    by premiere date, not alphabetically; unknown premieres sort last. Same-day
    ties break alphabetically ignoring "The" (matches the sample's 7/15 and 7/16
    ties)."""
    md = _premiere_month_day(show)
    month, day = md if md else (99, 99)
    return (month, day, _sort_title(show.get("title")))


def _month_number(month) -> int | None:
    """The month number from a tracker month given either as an int (7) or a
    'YYYY-MM' string ('2026-07')."""
    if isinstance(month, int):
        return month
    try:
        return store.parse_month_key(str(month))[1]
    except ValueError:
        # Tolerant on purpose, unlike the parse it delegates to: this is a
        # rendering helper and an unreadable month means "no month to sort by",
        # not a failed post.
        return None


def premiered_in_month(show: dict, month_number: int) -> bool:
    """Whether this show's season premiere falls in `month_number` — the test for
    POST 1 membership. A premiere date is what makes a show one of this month's
    announcements, independent of whether it has begun airing yet; a show carried
    over from an earlier month premiered then, so its month differs and it is not
    re-announced."""
    md = _premiere_month_day(show)
    return md is not None and md[0] == month_number


# ---------------------------------------------------------------------------
# Bucketing state machine
# ---------------------------------------------------------------------------

def bucket_of(rec: dict, live: dict) -> Bucket:
    """Which Bucket this show is in.

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
        return Bucket.ABANDONED
    watched = int(live.get("watched") or 0)
    total = int(live.get("total") or 0)
    if total > 0 and watched >= total:
        return Bucket.COMPLETED
    if not live.get("started_airing"):
        return Bucket.NEW if int(rec.get("season") or 1) == 1 else Bucket.RETURNING
    if live.get("cadence") == "b":
        return Bucket.CLEANUP  # binge goes straight to Cleanup, skipping Keepup
    if live.get("finished_airing"):
        return Bucket.CLEANUP  # weekly, finale has aired
    return Bucket.KEEPUP  # weekly, still airing


# ---------------------------------------------------------------------------
# Per-bucket line renderers (exact inline forms, verified against a real sample post)
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
    """The backtick-wrapped inline form to snapshot at abandon-time: the show's
    current bucket-appropriate counts form, minus any premiere/finale
    dates — "(x/y, CAD)" if it hasn't started airing yet, else "(x/y)", else
    (fully watched) just the title+season with no counts.

    Deliberately does not call bucket_of / look at `abandoned` — this freezes
    what the state WOULD be right now, independent of the toggle being applied.
    Reused both by routes.py's abandon endpoint (to freeze `abandoned_form`)
    and by `_abandoned_line` below as the fallback for abandoned records
    written before `abandoned_form` existed, where it is still None.
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
    omitted entirely by the caller when they have nothing in them, since
    unlike the mandatory sections an empty Completed/Abandoned isn't itself
    informative to a reader of the post."""
    return header + ("\n" + "\n".join(lines) if lines else "")


def _group_by_bucket(shows: list[dict]) -> dict[Bucket, list[dict]]:
    """Every bucket gets a list, empty or not, so a caller can read one without
    checking whether anything landed in it."""
    groups: dict[Bucket, list[dict]] = {bucket: [] for bucket in Bucket}
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
                 link_url: str | None = None, month=None) -> str:
    """POST 1 (announcement): a SNAPSHOT of this month's premieres — **New Shows**
    (a season 1 premiere) + **Returning** (a later season's premiere) — optionally
    followed by a link line pointing at the poster's own public calendar.

    Membership is by PREMIERE DATE, not by the airing lifecycle. A show announced
    here stays here once its episodes start; only POST 2 moves it on into Keepup /
    Cleanup. It leaves POST 1 only if its premiere date moves out of this month
    (then it belongs to that other month's announcement) or it is abandoned. That
    is the difference from POST 2, whose New/Returning sections DO empty as shows
    begin airing.

    `month` is the tracker month, as 'YYYY-MM' or an int. Without it — a caller
    that only wants the rendering — this falls back to the not-yet-airing
    lifecycle buckets, which is POST 1's pre-snapshot behaviour.

    GIVE THIS THE WHOLE ROSTER, never a roster already put through month_view.
    That filter is the standing rule MONTH_BUCKETS states, and it belongs to the
    page's row list and to POST 2 only: a month's premieres are its premieres in
    every standing, so a title being Completed or Cleanup by now has no bearing
    on whether this month announced it.

    `link_url` is omitted entirely when there is nothing to link to, rather than
    rendered as an empty or broken line. It is wrapped in angle brackets, which
    is Discord's own way of suppressing the link preview card — an announcement
    that already lists a month of shows does not want a second, larger block
    underneath it.
    """
    emoji_map = emoji_map or {}
    month_number = _month_number(month)
    if month_number is None:
        groups = _group_by_bucket(shows)
        news, returning = groups[Bucket.NEW], groups[Bucket.RETURNING]
    else:
        premieres = [
            s for s in shows
            if not s.get("abandoned") and premiered_in_month(s, month_number)
        ]
        news = [s for s in premieres if int(s.get("season") or 1) == 1]
        returning = [s for s in premieres if int(s.get("season") or 1) != 1]
    news = sorted(news, key=_premiere_sort_key)
    returning = sorted(returning, key=_premiere_sort_key)
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


def month_view(shows: list[dict], standing: MonthStanding) -> list[dict]:
    """The roster rows a month in this `standing` presents at all.

    The page's list and POST 2 both draw from this, so a title the post does not
    mention is a title the page does not show either — the two disagreeing about
    what is in a month is the thing this rule exists to stop. Nothing is deleted:
    a row a month may not present is still stored, still exported, and still
    carried forward when the calendar reaches the month it belongs to.

    POST 1 is deliberately outside this: it announces what premiered in the
    month, which no standing changes. See render_post1.
    """
    allowed = set(MONTH_BUCKETS[standing])
    return [s for s in shows if bucket_of(s, s) in allowed]


def render_post2(shows: list[dict], emoji_map: dict | None = None, default_emoji: str = ":tv:",
                 movies: list[dict] | None = None,
                 standing: MonthStanding = MonthStanding.CURRENT) -> str:
    """POST 2 (living tracker) for a month in `standing`: the sections MONTH_BUCKETS
    allows it, in that order, with Completed/Abandoned/Movies omitted when empty
    and the rest carrying their header even so.

    The default is the month in progress — the one that says everything — so a
    caller that only wants the rendering (a test, a preview of the markup) gets the
    full shape without having to state a month, the same way render_post1 falls
    back when it is given no month.

    `movies` is [{title, year, ...}] watched during the month (from the watch-
    history cache); rendered struck-through, alphabetized ignoring a leading
    article, at the very end."""
    emoji_map = emoji_map or {}
    groups = _group_by_bucket(shows)
    cleanup = sorted(groups[Bucket.CLEANUP], key=lambda s: _sort_title(s.get("title")))
    completed = sorted(groups[Bucket.COMPLETED], key=lambda s: _sort_title(s.get("title")))
    abandoned = sorted(groups[Bucket.ABANDONED], key=lambda s: _sort_title(s.get("title")))
    news = sorted(groups[Bucket.NEW], key=_premiere_sort_key)
    returning = sorted(groups[Bucket.RETURNING], key=_premiere_sort_key)

    # Built once each, then picked from by the same table the roster was filtered
    # through — so "does this month have a Keepup" is asked in one place.
    blocks = {
        Bucket.CLEANUP: lambda: _section(
            "## **Cleanup**", [_cleanup_line(s, emoji_map, default_emoji) for s in cleanup]),
        Bucket.KEEPUP: lambda: _render_keepup(groups[Bucket.KEEPUP], emoji_map, default_emoji),
        Bucket.NEW: lambda: _section(
            "**New Shows**", [_new_returning_line(s, emoji_map, default_emoji) for s in news]),
        Bucket.RETURNING: lambda: _section(
            "**Returning**", [_new_returning_line(s, emoji_map, default_emoji) for s in returning]),
        # Unlike the four above, an empty one of these is not informative to a
        # reader of the post, so it is left out entirely rather than headed.
        Bucket.COMPLETED: lambda: _section(
            "**Completed**", [_completed_line(s, emoji_map, default_emoji) for s in completed],
        ) if completed else None,
        Bucket.ABANDONED: lambda: _section(
            "**Abandoned**", [_abandoned_line(s, emoji_map, default_emoji) for s in abandoned],
        ) if abandoned else None,
    }
    sections = [block for block in (blocks[bucket]() for bucket in MONTH_BUCKETS[standing])
                if block is not None]
    if movies:
        movs = sorted(movies, key=lambda m: _sort_title(m.get("title")))
        sections.append(_section("**Movies**", [_movie_line(m) for m in movs]))
    return "\n\n".join(sections)
