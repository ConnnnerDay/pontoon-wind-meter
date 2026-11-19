"""Tests for data/fetcher.py — run with: pytest test_fetcher.py"""
import json
from datetime import datetime, timezone
import threading
import time

import disk_cache
from data.fetcher import _apply_cache, update_histories


def _backdate_saved_at(path: str, seconds_ago: float) -> None:
    """Rewrite the snapshot's _saved_at field to *seconds_ago* in the past."""
    data = json.loads(open(path).read())
    data[disk_cache._TIMESTAMP_KEY] = time.time() - seconds_ago
    open(path, "w").write(json.dumps(data))


def _base_state() -> dict:
    return {
        "wind": None, "gust": None, "wdir": "---", "age": None,
        "wtmp": None, "wvht": None, "atmp": None, "dpd": None, "dewp": None,
        "pres": None, "pres_history": [], "gust_history": [],
        "alerts": [], "error": "boom", "cached": False,
    }


def _cfg(tmp_path, **over) -> dict:
    cfg = {
        "cache_path": str(tmp_path / "snap.json"),
        "cache_max_age_minutes": 180,
    }
    cfg.update(over)
    return cfg


def test_apply_cache_ages_snapshot_by_elapsed_time(tmp_path):
    """A snapshot saved 40 min ago with obs age 5 should display as ~45 min old."""
    cfg = _cfg(tmp_path)
    disk_cache.save_snapshot(cfg["cache_path"], {"wind": 8.0, "gust": 10.0, "age": 5})
    _backdate_saved_at(cfg["cache_path"], 40 * 60)  # saved 40 min ago

    state = _base_state()
    applied = _apply_cache(state, threading.Lock(), cfg)
    assert applied is True
    assert state["cached"] is True
    assert state["error"] is None
    # Original obs age 5 + ~40 min elapsed.
    assert 44 <= state["age"] <= 46


def test_apply_cache_no_age_when_age_missing(tmp_path):
    """If the snapshot has no age, applying the cache must not invent one."""
    cfg = _cfg(tmp_path)
    disk_cache.save_snapshot(cfg["cache_path"], {"wind": 8.0, "gust": 10.0})
    state = _base_state()
    assert _apply_cache(state, threading.Lock(), cfg) is True
    assert state["age"] is None


def test_apply_cache_rejects_stale_snapshot(tmp_path):
    """A snapshot older than cache_max_age_minutes is not applied."""
    cfg = _cfg(tmp_path, cache_max_age_minutes=30)
    disk_cache.save_snapshot(cfg["cache_path"], {"wind": 8.0, "gust": 10.0, "age": 5})
    _backdate_saved_at(cfg["cache_path"], 60 * 60)  # saved 1 h ago

    state = _base_state()
    assert _apply_cache(state, threading.Lock(), cfg) is False
    assert state["cached"] is False


def test_update_histories_skips_repeat_observation():
    state = {"gust_history": [], "pres_history": [], "obs_time": None}
    update_histories(state, 10.0, 1015.0, "2026-06-01 12:00")
    update_histories(state, 10.0, 1015.0, "2026-06-01 12:00")  # same obs, polled again
    assert state["gust_history"] == [10.0]
    assert state["pres_history"] == [1015.0]
    update_histories(state, 14.0, 1014.0, "2026-06-01 12:10")
    assert state["gust_history"] == [14.0, 10.0]
    assert state["pres_history"] == [1014.0, 1015.0]


def test_update_histories_caps_length_and_skips_missing_pressure():
    state = {"gust_history": [], "pres_history": [], "obs_time": None}
    for i in range(10):
        update_histories(state, float(i), None, f"2026-06-01 12:{i:02d}")
    assert state["gust_history"] == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0]
    assert state["pres_history"] == []


def test_parse_alerts_keeps_highest_severity_and_end_time():
    from data.fetcher import parse_alerts
    data = {"features": [
        {"properties": {"event": "Gale Warning", "severity": "Moderate",
                        "expires": "2026-07-01T12:00:00-04:00", "ends": "2026-07-01T18:00:00-04:00"}},
        {"properties": {"event": "Gale Warning", "severity": "Severe",
                        "expires": "2026-07-01T14:00:00-04:00", "ends": None}},
        {"properties": {"event": "Rip Current Statement", "severity": "Minor"}},
        {"properties": {"event": ""}},
    ]}
    alerts = {a[0]: a for a in parse_alerts(data)}
    assert set(alerts) == {"GALE WARNING", "RIP CURRENT STATEMENT"}
    event, sev, until = alerts["GALE WARNING"]
    assert sev == "Severe"
    assert until == datetime(2026, 7, 1, 22, 0, tzinfo=timezone.utc).timestamp()
    assert alerts["RIP CURRENT STATEMENT"][2] is None


def test_drop_expired_alerts():
    from data.fetcher import drop_expired_alerts
    now = 1_000_000.0
    alerts = [("A", "Severe", now - 1), ("B", "Severe", now + 60), ("C", "Minor", None)]
    assert [a[0] for a in drop_expired_alerts(alerts, now)] == ["B", "C"]


def test_alert_floor_accepts_triples():
    from logic import alert_floor
    assert alert_floor([("GALE WARNING", "Severe", None)], {"caution_mph": 30.0}) == 30.5
