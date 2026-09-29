"""Bucket vocabulary + POST 1/POST 2 markdown renderer.

Pure, offline functions — no I/O, no Trakt calls, no persistence.

TWO KINDS OF CALLER HAND THIS MODULE A SHOW, and they are at different points
in the season's life:

  - live.py, on every load, merges a stored record with what Trakt says about
    it RIGHT NOW into the flat "LIVE SHOW SHAPE" below, and calls `bucket_of`
    to work out where that live-merged season currently stands — nothing has
    decided its bucket yet, because nothing has decided its stored `kind` yet
    either; that happens afterwards, in lifecycle.py, off exactly this answer.
  - routes.py and this module's own renderers, once a season has a stored
    record, read `show["bucket"]` straight off it. A stored record's `kind`
    already says what it is (store.RecordKind, store.BUCKET_OF_KIND), so
    reading it back is a lookup and re-running `bucket_of` on it would be a
    second, independent opinion that the two are not guaranteed to agree with.

LIVE SHOW SHAPE (one dict per show+season, the input `bucket_of` and the line
renderers below read):
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

from typing import NamedTuple

# The render vocabulary is named beside the record shape in store.py; this
# module is what a bucket LOOKS like once rendered.
from .store import Bucket, MonthStanding

# WHICH BUCKETS EACH READER MAY PRESENT, by where a month stands relative to the
# calendar. ONE declaration, not two, because a bucket's MEANING is one fact —
# "Cleanup" names the same set of seasons for the page as it does for the second
# notice — and restating that set a second time for the other reader is exactly
# how the two came to disagree the first time this shipped: they were once one
# filtered list built for whichever reader asked first, and the second reader
# read off a copy of it that nobody kept in step.
#
# WHAT GENUINELY DIFFERS BETWEEN THE TWO READERS is not what a bucket means but
# which buckets they answer with AT ALL. The page also carries what has not
# started airing yet — New and Returning — because a season with nothing else to
# say about it is still worth a viewer seeing sitting on their page. The second
# notice never carries them, in ANY standing: announcing a premiere is the FIRST
# notice's job, in every standing, regardless of the month's — see render_post1 —
# and the second notice exists to answer a different question, what is in hand
# and what got settled. Losing New/Returning from POST 2 is therefore not a
# per-standing filter that happens to always come out empty; New and Returning
# are simply not among the things this notice ever says.
#
# `post2` is a tuple rather than a set because it also states the ORDER its
# sections render in; `page` needs no order because nothing here decides where a
# row sits in the page's own list.
#
# POST 1 IS NOT IN THIS TABLE. It answers a third question — what did this month
# ANNOUNCE — and the answer does not change with where the month stands: a
# premiere belongs to the month it began in, whatever became of it since.
# Filtering it through this table would make an announcement of two finished
# titles and an empty Returning section, which is the opposite of what an
# announcement is for. See render_post1.
class ReaderBuckets(NamedTuple):
    page: frozenset[Bucket]
    post2: tuple[Bucket, ...]


READER_BUCKETS: dict[MonthStanding, ReaderBuckets] = {
    MonthStanding.FUTURE: ReaderBuckets(
        page=frozenset({Bucket.NEW, Bucket.RETURNING}),
        # Nothing has happened yet in a month nobody has reached: no season is in
        # hand to report on and nothing has been settled, so the living-tracker
        # notice has nothing of its own to say. It stays silent rather than
        # repeating the announcement the first notice already made.
        post2=(),
    ),
    MonthStanding.CURRENT: ReaderBuckets(
        page=frozenset({Bucket.CLEANUP, Bucket.KEEPUP, Bucket.NEW, Bucket.RETURNING,
                        Bucket.COMPLETED, Bucket.ABANDONED}),
        post2=(Bucket.CLEANUP, Bucket.KEEPUP, Bucket.COMPLETED, Bucket.ABANDONED),
    ),
    MonthStanding.PAST: ReaderBuckets(
        # A month that is over settled its Cleanup and its Keepup when it froze;
        # what survives is the verdict on each title plus the films watched
        # during it. Re-listing what was still mid-flight on the last day would
        # read as work outstanding on a month nobody can do anything about.
        page=frozenset({Bucket.COMPLETED, Bucket.ABANDONED}),
        post2=(Bucket.COMPLETED, Bucket.ABANDONED),
    ),
}

# WHAT A BUCKET IS CALLED, ONCE, for every reader that names one. The page's
# section heads and the notices' markdown headers were three separate spellings
# of these six words — the enum's own values are lower-case identifiers, the
# notices wrote "## **Cleanup**" inline, and the browser carried a third table —
# so a bucket renamed would have been renamed in one of three places.
#
# THE DECORATION IS NOT IN HERE. "## **Cleanup**" is Discord markdown and
# "Cleanup" is the bucket's name; the notice wraps it where it renders, and the
# page draws it bare. One is a fact about the vocabulary, the other about the
# medium.
BUCKET_LABELS: dict[Bucket, str] = {
    Bucket.CLEANUP: "Cleanup",
    Bucket.KEEPUP: "Keepup",
    Bucket.NEW: "New Shows",
    Bucket.RETURNING: "Returning",
    Bucket.COMPLETED: "Completed",
    Bucket.ABANDONED: "Abandoned",
}

# THE ORDER THE PAGE DRAWS ITS SECTIONS IN. This existed nowhere in Python at
# all — it was a tuple in the browser — which is why the page and the notices
# could not be held to one another by anything but reading both. It is the
# page's own order and deliberately not the notices': READER_BUCKETS.post2
# states theirs, and the two genuinely differ (the notices never carry a
# premiere section at all — see render_post2).
PAGE_BUCKET_ORDER: tuple[Bucket, ...] = (
    Bucket.CLEANUP, Bucket.KEEPUP, Bucket.NEW, Bucket.RETURNING,
    Bucket.COMPLETED, Bucket.ABANDONED,
)

# Keepup groups shows by air weekday, Sun..Sat; only weekdays with at least one
# show get a header, so a quiet Tuesday doesn't leave an empty section in the post.
# PUBLIC because the page groups Keepup the same way and by the same list: a
# second copy of these seven strings in a second language is exactly what the
# browser used to carry.
WEEKDAY_ORDER = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


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


# ---------------------------------------------------------------------------
# Bucketing state machine — for a season that has no stored kind yet
# ---------------------------------------------------------------------------

def bucket_of(rec: dict, live: dict) -> Bucket:
    """Which Bucket this LIVE-MERGED show is in right now.

    Called by live.py on a season fresh off a Trakt merge, BEFORE anything has
    decided what its stored record should say — that decision is what this
    answer feeds into (lifecycle.py). A season that already has a stored `kind`
    should be read from its own `bucket` field instead (store.BUCKET_OF_KIND);
    calling this on one would be a second, independently-derived opinion with no
    guarantee of agreeing with the first.

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
    """Mandatory sections (Cleanup/Keepup, and New/Returning in POST 1) always
    render their header, even with zero lines; only Completed/Abandoned are
    conditionally omitted entirely by the caller when they have nothing in
    them, since unlike the mandatory sections an empty Completed/Abandoned
    isn't itself informative to a reader of the post."""
    return header + ("\n" + "\n".join(lines) if lines else "")


def _group_by_bucket(shows: list[dict]) -> dict[Bucket, list[dict]]:
    """Every bucket gets a list, empty or not, so a caller can read one without
    checking whether anything landed in it.

    Reads `show["bucket"]` rather than deriving it. Every show reaching this
    point is a STORED record — the page's rows and the second notice's input are
    the same list, built from records whose `kind` already decided their bucket
    once, in store.py's row_to_record. Recomputing it here from the show's live
    counts would be a second opinion that the stored one is not guaranteed to
    agree with, which is exactly the class of bug the kind-vs-bucket split
    exists to close.
    """
    groups: dict[Bucket, list[dict]] = {bucket: [] for bucket in Bucket}
    for show in shows:
        groups[Bucket(show["bucket"])].append(show)
    return groups


def _render_keepup(shows: list[dict], emoji_map: dict, default_emoji: str) -> str:
    by_day: dict[str, list[dict]] = {d: [] for d in WEEKDAY_ORDER}
    for show in shows:
        day = show.get("cadence")
        if day in by_day:
            by_day[day].append(show)
    lines = [f"## **{BUCKET_LABELS[Bucket.KEEPUP]}**"]
    for day in WEEKDAY_ORDER:
        group = by_day[day]
        if not group:
            continue
        group.sort(key=lambda s: _sort_title(s.get("title")))
        lines.append(f"*{day}*")
        lines.extend(_keepup_line(s, emoji_map, default_emoji) for s in group)
    return "\n".join(lines)


def render_post1(series_premieres: list[dict], season_premieres: list[dict],
                 emoji_map: dict | None = None, default_emoji: str = ":tv:",
                 link_url: str | None = None) -> str:
    """POST 1 (announcement): **New Shows** (a series' first season) +
    **Returning** (a later season), optionally followed by a link line pointing
    at the poster's own public calendar.

    THE TWO SECTIONS ARE THE TWO RECORD KINDS the month stores its premieres
    under — `series_premieres` and `season_premieres`, store.RecordKind's two
    premiere members — not one list re-split by season number at render time.
    store.premiere_kind() makes that split ONCE, at the moment a premiere record
    is written; re-deriving it here on every render is how the two sections
    would come to disagree with what was actually stored.

    IN EVERY STANDING, and with no filter of its own beyond what its caller
    already handed it: a season the month announced stays announced whatever it
    has since become — airing, finished, even abandoned — because this notice
    answers "what began this month" and that answer does not change with the
    calendar or with the season's later life. GIVE THIS THE MONTH'S OWN PREMIERE
    RECORDS, never rows already filtered through READER_BUCKETS — that table is
    the page's and the second notice's rule for what THEY may say, and applying
    it here would answer this notice's question with the wrong one.

    `link_url` IS BARE, AND THAT IS THE POINT OF IT. It used to be wrapped in
    angle brackets, which is Discord's own way of SUPPRESSING the preview card —
    and the card is the reason to post the link at all, because it carries the
    calendar's own picture. The url is omitted entirely when there is nothing to
    link to, rather than rendered as an empty or broken line.
    """
    emoji_map = emoji_map or {}
    news = sorted(series_premieres, key=_premiere_sort_key)
    returning = sorted(season_premieres, key=_premiere_sort_key)
    sections = [
        _section(f"**{BUCKET_LABELS[Bucket.NEW]}**",
                 [_new_returning_line(s, emoji_map, default_emoji) for s in news]),
        _section(f"**{BUCKET_LABELS[Bucket.RETURNING]}**",
                 [_new_returning_line(s, emoji_map, default_emoji) for s in returning]),
    ]
    if link_url:
        sections.append(f"**Full calendar:** {link_url}")
    return "\n\n".join(sections)


class PageSection(NamedTuple):
    """One heading on the tracker page and what is drawn under it.

    `rows` are the rows directly beneath the heading; `days` are the weekday
    sub-headings Keepup — and only Keepup — splits its rows into, in
    WEEKDAY_ORDER. A section carries one or the other, never both, and the
    template draws whichever it was given rather than asking which bucket this
    is: which buckets group by weekday is a fact about the vocabulary and it is
    settled here, once, beside the notice that already grouped one that way.
    """
    label: str
    rows: list[dict]
    days: list[tuple[str, list[dict]]]


def page_sections(shows: list[dict]) -> list[PageSection]:
    """The tracker page's own list, in the order and the order-within it draws.

    THE PAGE'S ORDER IS PYTHON'S NOW, AND THAT IS THE POINT OF THIS FUNCTION.
    It used to be a bucket order, a label table and two comparison functions in
    the browser, sitting beside the notices' versions of the same three facts
    with nothing holding the two together — and they had drifted: the browser
    matched a leading article followed by ANY whitespace while `_sort_title`
    matches a literal space, and it ordered with `localeCompare` while Python
    orders by code point. So a title separated from its article by a
    non-breaking space, and any accented title, sorted one way on the page and
    another in the notice the page exists to produce. Both now come out of
    `_sort_title` and `_premiere_sort_key`, which is what makes the two agree by
    construction rather than by inspection.

    NEW AND RETURNING GO BY PREMIERE DATE, everything else alphabetically, and
    Keepup by weekday and then alphabetically — the same three rules the
    notices use, read from the same two key functions.

    `shows` HAS ALREADY BEEN FILTERED for what this month's standing may show
    (routes._rows_for reads READER_BUCKETS.page), so this only groups and
    orders. An empty bucket draws no heading at all: on the page a heading with
    nothing under it is a section the reader scans and finds empty, which is a
    different bargain from the notice's, where a missing Cleanup heading would
    read as the month having none.
    """
    groups = _group_by_bucket(shows)
    sections: list[PageSection] = []
    for bucket in PAGE_BUCKET_ORDER:
        rows = groups[bucket]
        if not rows:
            continue
        label = BUCKET_LABELS[bucket]
        if bucket is Bucket.KEEPUP:
            sections.append(PageSection(label, [], _by_weekday(rows)))
            continue
        key = (_premiere_sort_key if bucket in (Bucket.NEW, Bucket.RETURNING)
               else (lambda show: _sort_title(show.get("title"))))
        sections.append(PageSection(label, sorted(rows, key=key), []))
    return sections


def _by_weekday(shows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Keepup's rows under their air weekday, Sun..Sat, skipping empty days —
    the same grouping `_render_keepup` writes into the notice, and a row whose
    cadence names no weekday (a binge) is left out of both by the same test.
    """
    by_day: dict[str, list[dict]] = {day: [] for day in WEEKDAY_ORDER}
    for show in shows:
        day = show.get("cadence")
        if day in by_day:
            by_day[day].append(show)
    return [(day, sorted(by_day[day], key=lambda s: _sort_title(s.get("title"))))
            for day in WEEKDAY_ORDER if by_day[day]]


def page_films(movies: list[dict] | None) -> list[dict]:
    """The month's watched films in the order the page lists them: by the day
    they were watched.

    NOT the notice's order, which is alphabetical (see render_post2's
    `**Movies**`), and the difference is deliberate rather than an oversight
    left standing. The notice is a list somebody reads down; the page is a
    record of a month, and a month's films happened in an order.
    """
    return sorted(movies or [], key=lambda movie: str(movie.get("watched_at") or ""))


def _movie_line(movie: dict) -> str:
    """> ~~`Title (YYYY)`~~ — struck through, no emoji (per the user's sample)."""
    title = movie.get("title") or ""
    year = movie.get("year")
    label = f"{title} ({year})" if year else title
    return f"> ~~`{label}`~~"


def render_post2(shows: list[dict], emoji_map: dict | None = None, default_emoji: str = ":tv:",
                 movies: list[dict] | None = None,
                 standing: MonthStanding = MonthStanding.CURRENT) -> str:
    """POST 2 (living tracker) for a month in `standing`: Cleanup, Keepup,
    Completed, Abandoned and Movies — NEVER New/Returning, in any standing. See
    READER_BUCKETS: announcing a premiere is POST 1's job in every standing, so
    this notice does not carry a New/Returning section to filter down to empty,
    it simply never has one.

    `shows` IS THE SAME LIST THE PAGE'S ROW LIST WAS BUILT FROM, including
    whatever premiere rows the page shows and this notice will not — the one
    declaration in READER_BUCKETS is what tells this function which of them to
    render, not a copy of the list pre-trimmed for this one caller. Trimming it
    before it gets here would be growing the second table this design refuses
    to grow: two lists carrying the same fact, free to drift apart the day only
    one of them gets edited.

    The default standing is the month in progress — the one that says
    everything — so a caller that only wants the rendering (a test, a preview of
    the markup) gets the full shape without having to state a month, the same
    way render_post1 needs no month to fall back on.

    `movies` is [{title, year, ...}] watched during the month (from the watch-
    history cache); rendered struck-through, alphabetized ignoring a leading
    article, at the very end."""
    emoji_map = emoji_map or {}
    groups = _group_by_bucket(shows)
    cleanup = sorted(groups[Bucket.CLEANUP], key=lambda s: _sort_title(s.get("title")))
    completed = sorted(groups[Bucket.COMPLETED], key=lambda s: _sort_title(s.get("title")))
    abandoned = sorted(groups[Bucket.ABANDONED], key=lambda s: _sort_title(s.get("title")))

    # Built once each, then picked from by the same table the page's rows were
    # filtered through — so "does this month have a Keepup" is asked in one place.
    blocks = {
        Bucket.CLEANUP: lambda: _section(
            f"## **{BUCKET_LABELS[Bucket.CLEANUP]}**",
            [_cleanup_line(s, emoji_map, default_emoji) for s in cleanup]),
        Bucket.KEEPUP: lambda: _render_keepup(groups[Bucket.KEEPUP], emoji_map, default_emoji),
        # Unlike the two above, an empty one of these is not informative to a
        # reader of the post, so it is left out entirely rather than headed.
        Bucket.COMPLETED: lambda: _section(
            f"**{BUCKET_LABELS[Bucket.COMPLETED]}**",
            [_completed_line(s, emoji_map, default_emoji) for s in completed],
        ) if completed else None,
        Bucket.ABANDONED: lambda: _section(
            f"**{BUCKET_LABELS[Bucket.ABANDONED]}**",
            [_abandoned_line(s, emoji_map, default_emoji) for s in abandoned],
        ) if abandoned else None,
    }
    sections = [block for block in (blocks[bucket]() for bucket in READER_BUCKETS[standing].post2)
                if block is not None]
    if movies:
        movs = sorted(movies, key=lambda m: _sort_title(m.get("title")))
        sections.append(_section("**Movies**", [_movie_line(m) for m in movs]))
    return "\n\n".join(sections)


class PostInputs(NamedTuple):
    """Everything the two posts are rendered from EXCEPT the viewer's emoji map.

    Held apart from the map because the map is the one input that changes on its
    own: a code edited on a row changes every line for that network and nothing
    else, so the posts can be rendered again from these with the new map rather
    than by working the month out again (routes.api_distrakt_emoji).
    """
    series_premieres: list[dict]
    season_premieres: list[dict]
    shows: list[dict]
    movies: list[dict] | None
    standing: MonthStanding
    link_url: str | None


def render_posts(inputs: PostInputs, emoji_map: dict | None,
                 default_emoji: str) -> tuple[str, str]:
    """(POST 1, POST 2) for one month, from what it holds and the viewer's map."""
    return (render_post1(inputs.series_premieres, inputs.season_premieres,
                         emoji_map, default_emoji, link_url=inputs.link_url),
            render_post2(inputs.shows, emoji_map, default_emoji,
                         movies=inputs.movies, standing=inputs.standing))
