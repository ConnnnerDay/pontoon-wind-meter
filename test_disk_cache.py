"""Tests for disk_cache.py — run with: pytest test_disk_cache.py"""
import json
import os
import time

import pytest

from disk_cache import (
    load_snapshot,
    save_snapshot,
    snapshot_is_fresh,
    CACHE_FIELDS,
    _TIMESTAMP_KEY,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_path(tmp_path, name="snapshot.json") -> str:
    return str(tmp_path / name)


def _sample_snapshot() -> dict:
    return {
        "wind": 8.5, "gust": 11.2, "wdir": "SW",
        "age": 5, "wtmp": 72.0, "wvht": 1.4,
        "atmp": 78.0, "dpd": 9.0, "dewp": 60.0,
        "pres": 1015.2,
        "pres_history": [1015.2, 1015.0, 1014.8],
        "gust_history": [11.2, 10.5, 9.8, 9.1],
    }


# ---------------------------------------------------------------------------
# load_snapshot — missing / invalid / valid
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_none(tmp_path):
    assert load_snapshot(_cache_path(tmp_path)) is None


def test_load_invalid_json_returns_none(tmp_path):
    p = _cache_path(tmp_path)
    open(p, "w").write("{this is not: valid json")
    assert load_snapshot(p) is None


def test_load_empty_file_returns_none(tmp_path):
    p = _cache_path(tmp_path)
    open(p, "w").close()
    assert load_snapshot(p) is None


def test_load_non_dict_json_returns_none(tmp_path):
    p = _cache_path(tmp_path)
    open(p, "w").write("[1, 2, 3]")
    assert load_snapshot(p) is None


def test_load_valid_snapshot(tmp_path):
    p = _cache_path(tmp_path)
    data = {"wind": 12.0, _TIMESTAMP_KEY: time.time()}
    open(p, "w").write(json.dumps(data))
    snap = load_snapshot(p)
    assert snap is not None
    assert snap["wind"] == 12.0


def test_load_preserves_all_fields(tmp_path):
    p = _cache_path(tmp_path)
    snap_in = _sample_snapshot()
    snap_in[_TIMESTAMP_KEY] = time.time()
    open(p, "w").write(json.dumps(snap_in))
    snap_out = load_snapshot(p)
    assert snap_out["gust"]         == pytest.approx(11.2)
    assert snap_out["pres_history"] == [1015.2, 1015.0, 1014.8]
    assert snap_out["gust_history"] == [11.2, 10.5, 9.8, 9.1]


# ---------------------------------------------------------------------------
# save_snapshot — writes valid JSON, atomically
# ---------------------------------------------------------------------------

def test_save_writes_json(tmp_path):
    p = _cache_path(tmp_path)
    save_snapshot(p, _sample_snapshot())
    with open(p) as fh:
        data = json.load(fh)
    assert data["wind"] == pytest.approx(8.5)


def test_save_adds_timestamp(tmp_path):
    p = _cache_path(tmp_path)
    before = time.time()
    save_snapshot(p, _sample_snapshot())
    after = time.time()
    with open(p) as fh:
        data = json.load(fh)
    ts = data[_TIMESTAMP_KEY]
    assert before <= ts <= after


def test_save_creates_parent_directory(tmp_path):
    p = str(tmp_path / "nested" / "dir" / "snap.json")
    save_snapshot(p, {"wind": 5.0})
    assert os.path.isfile(p)


def test_save_atomic_replace(tmp_path):
    p = _cache_path(tmp_path)
    # Write old content
    save_snapshot(p, {"wind": 1.0})
    # Overwrite with new content
    save_snapshot(p, {"wind": 2.0})
    with open(p) as fh:
        data = json.load(fh)
    assert data["wind"] == pytest.approx(2.0)


def test_save_no_tmp_files_left(tmp_path):
    p = _cache_path(tmp_path)
    save_snapshot(p, _sample_snapshot())
    leftover = [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")]
    assert leftover == []


def test_save_does_not_raise_on_bad_directory(tmp_path):
    # Directory is actually a file — save should log and not raise
    fake_dir = str(tmp_path / "not_a_dir")
    open(fake_dir, "w").close()
    bad_path = str(tmp_path / "not_a_dir" / "snap.json")
    save_snapshot(bad_path, {"wind": 5.0})   # should not raise


def test_save_roundtrip(tmp_path):
    """save then load returns the same values."""
    p = _cache_path(tmp_path)
    original = _sample_snapshot()
    save_snapshot(p, original)
    loaded = load_snapshot(p)
    assert loaded is not None
    for key in ("wind", "gust", "wdir", "wtmp", "wvht"):
        assert loaded[key] == pytest.approx(original[key]) if isinstance(original[key], float) else original[key]


# ---------------------------------------------------------------------------
# snapshot_is_fresh
# ---------------------------------------------------------------------------

def test_fresh_snapshot(tmp_path):
    p = _cache_path(tmp_path)
    save_snapshot(p, _sample_snapshot())
    snap = load_snapshot(p)
    assert snapshot_is_fresh(snap, max_age_seconds=3600)


def test_stale_snapshot(tmp_path):
    p = _cache_path(tmp_path)
    old_ts = time.time() - 7200   # 2 hours ago
    data = _sample_snapshot()
    data[_TIMESTAMP_KEY] = old_ts
    open(p, "w").write(json.dumps(data))
    snap = load_snapshot(p)
    assert not snapshot_is_fresh(snap, max_age_seconds=3600)


def test_within_boundary_is_fresh():
    # snapshot saved 59 seconds ago; max_age is 60 seconds → should be fresh
    snap = {_TIMESTAMP_KEY: time.time() - 59}
    assert snapshot_is_fresh(snap, max_age_seconds=60)


def test_missing_timestamp_is_not_fresh():
    assert not snapshot_is_fresh({"wind": 5.0}, max_age_seconds=3600)


def test_malformed_timestamp_is_not_fresh():
    assert not snapshot_is_fresh({_TIMESTAMP_KEY: "not-a-number"}, max_age_seconds=3600)


def test_zero_max_age_is_not_fresh(tmp_path):
    p = _cache_path(tmp_path)
    save_snapshot(p, _sample_snapshot())
    snap = load_snapshot(p)
    # zero seconds — even a just-written snapshot should not be considered fresh
    assert not snapshot_is_fresh(snap, max_age_seconds=0)


# ---------------------------------------------------------------------------
# CACHE_FIELDS constant
# ---------------------------------------------------------------------------

def test_cache_fields_covers_key_observations():
    for key in ("wind", "gust", "wdir", "wtmp", "wvht", "atmp", "pres"):
        assert key in CACHE_FIELDS, f"Expected {key!r} in CACHE_FIELDS"


def test_cache_fields_excludes_alerts_and_error():
    assert "alerts" not in CACHE_FIELDS
    assert "error"  not in CACHE_FIELDS
    assert "cached" not in CACHE_FIELDS
