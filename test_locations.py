"""Tests for locations.py (CO-OPS water temp)."""
from datetime import datetime, timezone, timedelta

import locations
from locations import parse_coops_water_temp

NOW = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)


def _resp(t: str, v: str) -> dict:
    return {"data": [{"t": t, "v": v, "f": "0,0,0"}]}


def test_recent_reading_is_used():
    assert parse_coops_water_temp(_resp("2026-07-01 14:54", "82.4"), NOW) == 82.4


def test_old_reading_is_ignored():
    assert parse_coops_water_temp(_resp("2026-06-28 09:00", "79.0"), NOW) is None


def test_blank_or_missing_values():
    assert parse_coops_water_temp(_resp("2026-07-01 14:54", ""), NOW) is None
    assert parse_coops_water_temp({"error": {"message": "No data was found"}}, NOW) is None
    assert parse_coops_water_temp({"data": []}, NOW) is None


def test_fetch_runs_on_first_call_right_after_boot(monkeypatch):
    """Regression: the cache used to default to monotonic time 0, so on a Pi
    with <30 min uptime the first call returned None without fetching."""
    locations._COOPS_CACHE.clear()
    monkeypatch.setattr(locations.time, "monotonic", lambda: 60.0)  # 1 min uptime
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        raise OSError("offline")

    monkeypatch.setattr(locations, "urlopen", fake_urlopen)
    assert locations.fetch_coops_water_temp("8658120") is None
    assert len(calls) == 1
    assert "time_zone=gmt" in calls[0]
    # Second call within the TTL is served from the cache.
    assert locations.fetch_coops_water_temp("8658120") is None
    assert len(calls) == 1
