"""Per-user persistence and rollover for the hidden "distrakt" tracker.

Each distrakt user keeps their OWN independent roster: their own tracked titles,
their own Cleanup/Keepup/Completed/Abandoned buckets built from their own watch
history, and their own pair of Discord posts. Nothing here is shared between
users — every lookup and mutation is scoped by user_id.

THIS FILE IS THE PACKAGE'S PUBLIC SURFACE and nothing else. The work is split by
the reason it changes:

  store.py            the two tables, and the record shape they hold
  live.py             a record plus what the provider says about it right now
  calendar_import.py  the calendar's premieres becoming roster records
  rollover.py         what a new month contains, and when a month freezes
  prefs.py            the per-user network -> emoji map
  backup.py           the JSON export and its inverse

Callers import this package, not its modules: `distrakt.load_month` says what it
does, while `distrakt.store.load_month` would make every caller responsible for
knowing which half of the tracker a name lives in. Inside the package the modules
call each other directly.

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
from .calendar_import import import_premieres, is_calendar_premiere, matches_not_watching
from .live import compute_live_shows, live_key
from .prefs import DEFAULT_EMOJI, get_emoji_prefs, register_networks, set_emoji_prefs
from .rollover import (
    TOTALS_STALE_HOURS,
    WATCHED_RECENCY_DAYS,
    can_initialize,
    drop_seasons_finished_earlier,
    ensure_month,
    is_backfill_blocked,
    is_stale,
    maybe_freeze_prior,
    month_committed,
)
from .store import (
    ADDED_BY_CALENDAR,
    ADDED_BY_HISTORY,
    ADDED_BY_MANUAL,
    ADDED_BY_VALUES,
    ID_COLUMNS,
    IDENTITY_COLUMNS,
    SHOW_COLUMNS,
    UnkeyableRecord,
    add_show,
    frozen_shows,
    list_months,
    load_month,
    months_with_shows,
    new_month_doc,
    normalize_show,
    record_key,
    remove_show,
    row_to_show,
    save_month,
    set_abandoned,
    set_month_movies,
    stamp_refreshed,
)

__all__ = [
    "ADDED_BY_CALENDAR", "ADDED_BY_HISTORY", "ADDED_BY_MANUAL", "ADDED_BY_VALUES",
    "DEFAULT_EMOJI", "EXPORT_SCHEMA", "SUPPORTED_EXPORT_SCHEMAS",
    "ID_COLUMNS", "IDENTITY_COLUMNS", "SHOW_COLUMNS",
    "TOTALS_STALE_HOURS", "WATCHED_RECENCY_DAYS",
    "RestoreError", "UnkeyableRecord",
    "add_show", "can_initialize", "compute_live_shows",
    "drop_seasons_finished_earlier", "ensure_month", "export_user_data",
    "frozen_shows", "get_emoji_prefs", "import_premieres", "is_backfill_blocked",
    "is_calendar_premiere", "is_stale", "list_months", "live_key", "load_month",
    "matches_not_watching", "maybe_freeze_prior", "month_committed",
    "months_with_shows", "new_month_doc", "normalize_show", "record_key",
    "register_networks", "remove_show", "restore_user_data", "row_to_show",
    "save_month", "set_abandoned", "set_emoji_prefs", "set_month_movies",
    "stamp_refreshed",
]
