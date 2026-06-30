"""Tests for data/fetcher.py — run with: pytest test_fetcher.py"""
import json
import threading
import time

import disk_cache
from data.fetcher import _apply_cache


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
