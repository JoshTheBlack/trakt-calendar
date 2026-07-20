"""Curated list of canonical IANA time zones for the Settings dropdown.

One primary entry per zone (no legacy aliases like US/Eastern or Etc/GMT+5),
grouped by region and labelled with the current UTC offset.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Grouped so the <select> can use <optgroup>. Order within a group is by offset
# at render time. These are the canonical, commonly-used zones.
CANONICAL: dict[str, list[str]] = {
    "UTC": ["UTC"],
    "Africa": [
        "Africa/Casablanca", "Africa/Lagos", "Africa/Algiers", "Africa/Tunis",
        "Africa/Cairo", "Africa/Johannesburg", "Africa/Nairobi", "Africa/Addis_Ababa",
        "Africa/Khartoum", "Africa/Accra", "Africa/Windhoek",
    ],
    "America": [
        "America/St_Johns", "America/Halifax", "America/New_York", "America/Toronto",
        "America/Havana", "America/Chicago", "America/Mexico_City", "America/Denver",
        "America/Phoenix", "America/Los_Angeles", "America/Vancouver", "America/Anchorage",
        "America/Bogota", "America/Lima", "America/Panama", "America/Caracas",
        "America/Santiago", "America/La_Paz", "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
        "America/Montevideo",
    ],
    "Asia": [
        "Asia/Jerusalem", "Asia/Beirut", "Asia/Baghdad", "Asia/Riyadh", "Asia/Tehran",
        "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata", "Asia/Kathmandu", "Asia/Colombo",
        "Asia/Dhaka", "Asia/Yangon", "Asia/Bangkok", "Asia/Ho_Chi_Minh", "Asia/Jakarta",
        "Asia/Singapore", "Asia/Kuala_Lumpur", "Asia/Hong_Kong", "Asia/Shanghai",
        "Asia/Taipei", "Asia/Manila", "Asia/Seoul", "Asia/Tokyo",
        "Asia/Almaty", "Asia/Tashkent", "Asia/Yekaterinburg",
    ],
    "Atlantic": [
        "Atlantic/Reykjavik", "Atlantic/Azores", "Atlantic/Cape_Verde", "Atlantic/Canary",
    ],
    "Australia": [
        "Australia/Perth", "Australia/Darwin", "Australia/Brisbane", "Australia/Adelaide",
        "Australia/Sydney", "Australia/Melbourne", "Australia/Hobart",
    ],
    "Europe": [
        "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Madrid", "Europe/Paris",
        "Europe/Berlin", "Europe/Amsterdam", "Europe/Brussels", "Europe/Zurich", "Europe/Rome",
        "Europe/Vienna", "Europe/Prague", "Europe/Warsaw", "Europe/Stockholm", "Europe/Oslo",
        "Europe/Copenhagen", "Europe/Helsinki", "Europe/Athens", "Europe/Bucharest",
        "Europe/Istanbul", "Europe/Kyiv", "Europe/Moscow",
    ],
    "Indian": [
        "Indian/Maldives", "Indian/Mauritius",
    ],
    "Pacific": [
        "Pacific/Honolulu", "Pacific/Auckland", "Pacific/Fiji", "Pacific/Guam",
        "Pacific/Port_Moresby", "Pacific/Tongatapu", "Pacific/Chatham",
    ],
}


def _offset(tz: str, now: datetime) -> tuple[int, str]:
    """Return (offset_minutes, '+HH:MM') for a zone at the current instant."""
    try:
        delta = now.astimezone(ZoneInfo(tz)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return (0, "+00:00")
    if delta is None:
        return (0, "+00:00")
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    a = abs(minutes)
    return (minutes, f"{sign}{a // 60:02d}:{a % 60:02d}")


def build_options(current: str) -> list[dict]:
    """Build grouped, offset-labelled options for the timezone <select>.

    Ensures `current` is present and selectable even if it isn't in the curated list.
    """
    now = datetime.now().astimezone()
    known = {tz for zones in CANONICAL.values() for tz in zones}
    groups: list[dict] = []

    for group, zones in CANONICAL.items():
        options = []
        for tz in zones:
            off_min, off_str = _offset(tz, now)
            city = tz.split("/")[-1].replace("_", " ")
            options.append({
                "value": tz,
                "label": f"(UTC{off_str}) {city}" if tz != "UTC" else "UTC (±00:00)",
                "offset": off_min,
                "selected": tz == current,
            })
        options.sort(key=lambda o: (o["offset"], o["value"]))
        groups.append({"group": group, "options": options})

    # If the saved zone isn't in our list, surface it in its own group so it stays selected.
    if current and current not in known:
        off_min, off_str = _offset(current, now)
        groups.insert(0, {"group": "Current", "options": [{
            "value": current,
            "label": f"(UTC{off_str}) {current}",
            "offset": off_min,
            "selected": True,
        }]})
    return groups
