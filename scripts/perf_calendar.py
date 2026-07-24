"""Repeatable calendar-load measurement.

Seeds a synthetic month sized like a busy "All Episodes" view (~1,100 items
spread across the aligned 7-day windows a month spans), behind the same
app.calendar_cache.fetch_window_raw patch point tests/test_calendar_route.py
uses, so the real read_month -> group -> Jinja render pipeline runs for real
instead of a guess. A small per-window sleep stands in for the Trakt round
trip so a cold (uncached) run still shows the cost of fetching every window
before anything can render.

Reports, for a cold request and a warm one against the same data: server
time-to-response, HTML bytes shipped, and — once GZipMiddleware is on the
response path — the gzip-compressed bytes actually sent over the wire (read
from the response's own Content-Length, not re-compressed here, so it matches
what a browser would receive).

This is a fixed yardstick for calendar-loading changes, not a test: run it
before and after a change to the fetch/assembly/render path and compare the
printed numbers.

    ./.venv/Scripts/python.exe scripts/perf_calendar.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-perf-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, calendar_cache, db  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

ORIGIN = "https://testserver"
YEAR, MONTH = 2026, 7
CARDS_PER_DAY = 37  # 31 days * 37 ~= 1,147, in the same ballpark as the real
                     # "All Episodes" month this harness is modelled on.
PER_WINDOW_FETCH_DELAY_S = 0.4  # stands in for one Trakt window round trip


def _entry(show_id: int, day: date) -> dict:
    return {
        "first_aired": f"{day.isoformat()}T20:00:00Z",
        "episode": {"season": 1, "number": show_id % 20 + 1, "title": f"Episode {show_id}"},
        "show": {
            "title": f"Show {show_id}", "country": "us", "genres": [],
            "ids": {"slug": f"show-{show_id}", "trakt": show_id},
        },
    }


def _month_entries() -> dict[date, list[dict]]:
    """One synthetic entry list per calendar day, evenly filling the month."""
    days_in_month = (date(YEAR, MONTH % 12 + 1, 1) - date(YEAR, MONTH, 1)).days \
        if MONTH != 12 else 31
    by_day: dict[date, list[dict]] = {}
    show_id = 0
    for day_num in range(1, days_in_month + 1):
        day = date(YEAR, MONTH, day_num)
        entries = []
        for _ in range(CARDS_PER_DAY):
            entries.append(_entry(show_id, day))
            show_id += 1
        by_day[day] = entries
    return by_day


def _make_fetch(by_day: dict[date, list[dict]]):
    """A fetch_window_raw stand-in: returns the synthetic entries covering the
    requested 7-day window, after a delay standing in for the Trakt call."""
    async def fetch(endpoint, settings, start):
        await asyncio.sleep(PER_WINDOW_FETCH_DELAY_S)
        out: list[dict] = []
        for offset in range(7):
            day = date.fromordinal(start.toordinal() + offset)
            out.extend(by_day.get(day, []))
        return out
    return AsyncMock(side_effect=fetch)


def main() -> None:
    db.set_db_path(Path(os.environ["TRAKT_DATA_DIR"]) / "perf.db")
    asyncio.run(db.migrate())
    save_settings(Settings(trakt_client_id="perf-client-id", trakt_access_token="perf-access-token"))
    user_id = asyncio.run(auth.create_user(
        username="perf_viewer", password="hunter2hunter2",
        settings=Settings(trakt_client_id="perf-client-id", trakt_access_token="perf-access-token"),
        calendar_approved=True,
    ))
    session_id = asyncio.run(auth.create_session(user_id))

    by_day = _month_entries()
    total_cards = sum(len(v) for v in by_day.values())
    calendar_cache.fetch_window_raw = _make_fetch(by_day)

    client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)
    url = f"/?year={YEAR}&month={MONTH}&endpoint=shows"

    print(f"cards={total_cards} windows~{len(by_day) // 7 + 1}")

    for label in ("cold", "warm"):
        t0 = time.perf_counter()
        resp = client.get(url)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert resp.status_code == 200, resp.text[:500]
        raw_bytes = len(resp.text.encode("utf-8"))
        encoding = resp.headers.get("content-encoding", "none")
        wire_bytes = int(resp.headers.get("content-length", raw_bytes))
        print(
            f"{label:>4}  {elapsed_ms:8.1f} ms  "
            f"html={raw_bytes / 1024:8.1f} KB  "
            f"wire={wire_bytes / 1024:8.1f} KB  encoding={encoding}"
        )

    client.close()
    db.close_thread_connection()


if __name__ == "__main__":
    main()
