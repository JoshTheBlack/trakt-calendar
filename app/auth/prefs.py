"""Per-user view preferences: the `user_prefs` row and the saved timezone.

The timezone lives on `users` rather than `user_prefs`, but it is the same kind
of fact — something the viewer chose about how their calendar is presented — so
it is read and written from here rather than from the account module.
"""
from __future__ import annotations

import json

from .. import db
from ..config import Settings


def insert_user_prefs(conn: db.Connection, user_id: int, settings: Settings,
                      *, seed_filters: bool = False) -> None:
    """SYNCHRONOUS. Seeds a user's view preferences from settings.json's app-wide
    values.

    Those settings.json fields are a SEED, not a live source: once this row
    exists, editing settings.json affects new users only, never this one.

    The genre/country/certification/network FILTERS are excluded from that seed
    unless `seed_filters` is set, and only the first-run onboarding sets it. A
    filter removes shows from someone's calendar without ever telling them a
    filter exists, so it is not something to inherit from an instance's
    configuration — a new account starts seeing everything and narrows it down
    itself. Onboarding is the one exception, because there the settings are the
    operator's own from before this instance had accounts, and their calendar
    has to keep rendering as it did.
    """
    # THE SETTINGS SEED IS ONE ANSWER AND THERE ARE NOW TWO COLUMNS TO PUT IT IN.
    # settings.json's `genres`/`countries` predate the per-medium split and were
    # always applied to whichever calendar was open, so seeding both sides is
    # what preserves the operator's own view through onboarding. That is the
    # opposite of what migration 38 does to an EXISTING account, and deliberately
    # so: the migration is deciding what a filter written for shows meant, while
    # this is copying an instance-wide value that never meant either medium in
    # particular.
    conn.execute(
        "INSERT INTO user_prefs (user_id, endpoint, card_style, day_packing, "
        "hide_not_watching, network_filter_json, tv_genres, tv_countries, "
        "movie_genres, movie_countries, "
        "show_certifications, movie_certifications) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, settings.endpoint, settings.card_style, settings.day_packing,
            int(bool(settings.hide_not_watching)),
            json.dumps(list(settings.network_filter or [])) if seed_filters else "[]",
            (settings.genres or "") if seed_filters else "",
            (settings.countries or "") if seed_filters else "",
            (settings.genres or "") if seed_filters else "",
            (settings.countries or "") if seed_filters else "",
            (settings.show_certifications or "") if seed_filters else "",
            (settings.movie_certifications or "") if seed_filters else "",
        ),
    )
    # THE RELEASE FILTERS ARE NOT SEEDED FROM SETTINGS AT ALL, not even under
    # `seed_filters`, and the reason is that there is nothing to carry over:
    # they have no settings.json field to inherit from, because they narrow ONE
    # viewer's films calendar and were never an instance-wide value. Their
    # column defaults ('' — every market, every format) are what every account
    # starts on, including the operator's own at onboarding.


async def get_user_prefs(user_id: int) -> dict:
    """A user's view preferences, in the shape the calendar read path and the
    per-user pref-write endpoint both use. Falls back to empty/default values if
    the row is somehow missing (it is created alongside every user, but a
    fallback here is cheap and keeps this a total function)."""
    row = await db.fetch_one(
        "SELECT endpoint, card_style, day_packing, hide_not_watching, "
        "network_filter_json, tv_genres, tv_countries, movie_genres, "
        "movie_countries, show_certifications, "
        "movie_certifications, movie_release_countries, movie_release_types, "
        "filters_paused "
        "FROM user_prefs WHERE user_id = ?",
        (user_id,),
    )
    if row is None:
        return {
            "endpoint": None, "card_style": None, "day_packing": None,
            "hide_not_watching": False, "network_filter": [],
            "tv_genres": "", "tv_countries": "",
            "movie_genres": "", "movie_countries": "",
            "show_certifications": "", "movie_certifications": "",
            "movie_release_countries": "", "movie_release_types": "",
            "filters_paused": False,
        }
    return {
        "endpoint": row["endpoint"],
        "card_style": row["card_style"],
        "day_packing": row["day_packing"],
        "hide_not_watching": bool(row["hide_not_watching"]),
        "network_filter": json.loads(row["network_filter_json"] or "[]"),
        "tv_genres": row["tv_genres"] or "",
        "tv_countries": row["tv_countries"] or "",
        "movie_genres": row["movie_genres"] or "",
        "movie_countries": row["movie_countries"] or "",
        "show_certifications": row["show_certifications"] or "",
        "movie_certifications": row["movie_certifications"] or "",
        "movie_release_countries": row["movie_release_countries"] or "",
        "movie_release_types": row["movie_release_types"] or "",
        "filters_paused": bool(row["filters_paused"]),
    }


# Columns a caller may update through update_user_prefs, keyed by the dict key
# it's passed under (network_filter/hide_not_watching need a transform on the
# way into their column; the rest write straight through).
_USER_PREF_FIELDS = frozenset({
    "endpoint", "card_style", "day_packing", "hide_not_watching",
    "network_filter", "tv_genres", "tv_countries", "movie_genres",
    "movie_countries", "show_certifications", "movie_certifications",
    "movie_release_countries", "movie_release_types", "filters_paused",
})


async def update_user_prefs(user_id: int, **fields) -> None:
    """Persist a partial update to one user's view preferences. Unknown keys and
    None values are ignored, so a caller can pass through a request body's dict
    as-is without first stripping out whatever it didn't set."""
    updates = {k: v for k, v in fields.items() if k in _USER_PREF_FIELDS and v is not None}
    if not updates:
        return
    columns: list[str] = []
    params: list = []
    for key, value in updates.items():
        if key in ("hide_not_watching", "filters_paused"):
            columns.append(f"{key} = ?")
            params.append(int(bool(value)))
        elif key == "network_filter":
            columns.append("network_filter_json = ?")
            params.append(json.dumps(list(value)))
        else:
            columns.append(f"{key} = ?")
            params.append(value)
    params.append(user_id)
    await db.execute(f"UPDATE user_prefs SET {', '.join(columns)} WHERE user_id = ?", tuple(params))


async def set_user_timezone(user_id: int, tz: str) -> None:
    """Persist the viewer's saved timezone. Validating that `tz` is a real IANA
    zone is the caller's job (it needs zoneinfo either way, to build the picker)."""
    await db.execute(
        "UPDATE users SET timezone = ?, updated_at = ? WHERE id = ?", (tz, db.now(), user_id),
    )
