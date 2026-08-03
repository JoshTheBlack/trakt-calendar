"""Per-user persistence and rollover for the hidden "distrakt" tracker.

Each distrakt user keeps their OWN independent records: their own premieres and
verdicts per month, their own list of what they are keeping up with and catching
up on, and their own pair of Discord notices. Nothing here is shared between
users — every lookup and mutation is scoped by user_id.

THIS FILE IS THE PACKAGE'S PUBLIC SURFACE and nothing else. The work is split by
the reason it changes:

  store.py            the tables, the record shape they hold, and the one
                      mapping from what a record IS to what it renders under
  lifecycle.py        what a season's record BECOMES as its life proceeds, and
                      the order the two tables are written in when it moves
  live.py             a record plus what the provider says about it right now
  watch_history.py    the incremental per-user cache of what has been seen
  calendar_import.py  the calendar's premieres becoming month premiere records
  rollover.py         what a new month contains, and when a month freezes
  backfill.py         months nobody was keeping, rebuilt from watch history
  prefs.py            the per-user network -> emoji map
  backup.py           the JSON export and its inverse
  discord_fmt.py      the two Discord notices
  routes.py           the tracker page and the whole /api/distrakt/* API

lifecycle.py is deliberately NOT re-exported below, for the same reason the
router and the backfill job are not: what it holds are ACTS — a season being
taken up, settled, given up on — rather than facts to read, and a caller that
wants one of those is asking for that module by name (`from .distrakt import
lifecycle`). The names re-exported below are the DATA LAYER — what a caller means when it says
`distrakt.load_month`, versus `distrakt.store.load_month`, which would make every
caller responsible for knowing which half of the tracker a name lives in. The
router and the backfill job are deliberately NOT among them: they are jobs to run
rather than facts to read, and their callers import the module (`from .distrakt
import routes`) precisely because that is what they want. Inside the package the
modules call each other directly.

The provider reads (premieres, season detail, watch history) authenticate with
whatever token is on the `settings` they are handed; `user_id` scopes the STORAGE.
"""
from __future__ import annotations

from .backup import (
    EXPORT_SCHEMA,
    SUPPORTED_EXPORT_SCHEMAS,
    RestoreError,
    export_user_data,
    restore_user_data,
)
from .calendar_import import calendar_mark_id, import_premieres, matches_not_watching
from .live import compute_live_shows, live_key
from .prefs import DEFAULT_EMOJI, get_emoji_prefs, register_networks, set_emoji_prefs
from .rollover import (
    TOTALS_STALE_HOURS,
    WATCHED_RECENCY_DAYS,
    can_initialize,
    ensure_month,
    freeze_eligible,
    is_backfill_blocked,
    is_stale,
    maybe_freeze,
    maybe_freeze_prior,
    month_committed,
)
from .store import (
    ADDED_BY_CALENDAR,
    ADDED_BY_HISTORY,
    ADDED_BY_MANUAL,
    ADDED_BY_VALUES,
    BUCKET_OF_KIND,
    Bucket,
    ID_COLUMNS,
    IDENTITY_COLUMNS,
    MONTH_KINDS,
    MONTH_RECORD_COLUMNS,
    MonthStanding,
    PREMIERE_KINDS,
    RecordKind,
    SETTLED_KINDS,
    USER_KINDS,
    USER_RECORD_COLUMNS,
    UnkeyableRecord,
    add_month_record,
    add_user_record,
    bucket_of_kind,
    dismiss_prompt,
    dismissed_prompts,
    find_month_record,
    find_user_record,
    frozen_shows,
    list_months,
    load_month,
    migrate_to_month,
    migrate_to_user,
    month_first_day,
    month_key,
    month_records,
    month_standing,
    months_with_shows,
    new_month_doc,
    normalize_show,
    parse_month_key,
    premiere_kind,
    record_key,
    remove_month_record,
    remove_season_everywhere,
    remove_user_record,
    row_to_record,
    save_month,
    set_came_back,
    set_month_movies,
    set_user_kind,
    stamp_refreshed,
    user_records,
    walk_settled,
)

__all__ = [
    "ADDED_BY_CALENDAR", "ADDED_BY_HISTORY", "ADDED_BY_MANUAL", "ADDED_BY_VALUES",
    "BUCKET_OF_KIND", "DEFAULT_EMOJI", "EXPORT_SCHEMA", "SUPPORTED_EXPORT_SCHEMAS",
    "Bucket", "ID_COLUMNS", "IDENTITY_COLUMNS", "MONTH_KINDS", "MONTH_RECORD_COLUMNS",
    "MonthStanding", "PREMIERE_KINDS", "RecordKind", "SETTLED_KINDS",
    "USER_KINDS", "USER_RECORD_COLUMNS",
    "TOTALS_STALE_HOURS", "WATCHED_RECENCY_DAYS",
    "RestoreError", "UnkeyableRecord",
    "add_month_record", "add_user_record", "bucket_of_kind", "calendar_mark_id",
    "can_initialize",
    "compute_live_shows", "dismiss_prompt", "dismissed_prompts", "ensure_month",
    "export_user_data", "find_month_record", "find_user_record", "freeze_eligible",
    "frozen_shows", "get_emoji_prefs", "import_premieres", "is_backfill_blocked",
    "is_stale", "list_months", "live_key", "load_month",
    "matches_not_watching", "maybe_freeze", "maybe_freeze_prior",
    "migrate_to_month", "migrate_to_user", "month_committed",
    "month_first_day", "month_key", "month_records", "month_standing",
    "months_with_shows", "new_month_doc", "normalize_show", "parse_month_key",
    "premiere_kind", "record_key", "register_networks", "remove_month_record",
    "remove_season_everywhere", "remove_user_record", "restore_user_data",
    "row_to_record", "save_month", "set_came_back", "set_emoji_prefs",
    "set_month_movies", "set_user_kind", "stamp_refreshed", "user_records",
    "walk_settled",
]
