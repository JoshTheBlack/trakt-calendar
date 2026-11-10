"""The title store's tables, named once — and the ones it has retired.

ONE ROW PER TITLE, PER SEASON AND PER EPISODE (`TITLES`, `SEASONS`, `EPISODES`),
each holding every service's facts side by side in a column group per service
(app/metadata/groups.py), with `SERVICE_IDS` saying which title each of a
service's own ids names and `ALIASES` the keys a title used to have. Every SQL
string that names one of them names it through here, so the store's tables are
spelled in exactly one place.

THE RETIRED TABLES ARE KEPT, NOT READ. The conversion that built this shape
(app/db.py's MIGRATION_49) copied the per-service tables and left them whole, so
that a later release can check a production copy against them row for row
before it drops them. Nothing in the app reads or writes one: tests/metadata/
test_retired_tables.py fails on any SQL in app/ that names one, and the holder
and retention checks pass over them by this list.
"""
from __future__ import annotations

TITLES = "titles"
SEASONS = "seasons"
EPISODES = "episodes"
SERVICE_IDS = "service_ids"
ALIASES = "title_aliases"

# {table: why it is still in the schema}. A table leaves this list in the
# release that drops it.
RETIRED: dict[str, str] = {
    "title_metadata": ("each service's row for a title, copied into the service groups of "
                       "`titles` and `service_ids`"),
    "season_metadata": "each service's row for a season, copied into `seasons`",
    "episode_metadata": "each service's row for an episode, copied into `episodes`",
    "calendar_airings": "each listing of an episode on a calendar, copied into `episodes`",
    "titles_v47": "the identity table as it stood before the store was one row per title",
}
