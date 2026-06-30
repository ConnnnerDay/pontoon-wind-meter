"""Tests for config.py — run with: pytest test_config.py"""
import os
import pathlib
import textwrap

import pytest

from config import load_config, build_arg_parser, _DEFAULTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_yaml(tmp_path, content: str) -> pathlib.Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

def test_defaults_returned_when_no_file(tmp_path):
    """load_config returns hard-coded defaults when config file is absent."""
    cfg = load_config(config_path=tmp_path / "nonexistent.yaml")
    assert cfg["good_mph"]    == _DEFAULTS["good_mph"]
    assert cfg["caution_mph"] == _DEFAULTS["caution_mph"]
    assert cfg["ndbc_station"] == _DEFAULTS["ndbc_station"]


def test_all_default_keys_present(tmp_path):
    cfg = load_config(config_path=tmp_path / "nonexistent.yaml")
    for key in _DEFAULTS:
        assert key in cfg, f"Missing default key: {key}"


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def test_yaml_overrides_threshold(tmp_path):
    p = make_yaml(tmp_path, """
        thresholds:
          good_mph: 12
          caution_mph: 20
    """)
    cfg = load_config(config_path=p)
    assert cfg["good_mph"]    == 12.0
    assert cfg["caution_mph"] == 20.0


def test_yaml_overrides_location(tmp_path):
    p = make_yaml(tmp_path, """
        location:
          ndbc_station: "41013"
          coops_station: "9999999"
          lat: 35.0
          lon: -76.0
          name: "Test Spot"
    """)
    cfg = load_config(config_path=p)
    assert cfg["ndbc_station"]  == "41013"
    assert cfg["coops_station"] == "9999999"
    assert cfg["lat"]           == 35.0
    assert cfg["lon"]           == -76.0
    assert cfg["location_name"] == "Test Spot"


def test_yaml_partial_override_preserves_defaults(tmp_path):
    """Only specified keys are overridden; others keep their defaults."""
    p = make_yaml(tmp_path, """
        thresholds:
          good_mph: 10
    """)
    cfg = load_config(config_path=p)
    assert cfg["good_mph"]    == 10.0
    assert cfg["caution_mph"] == _DEFAULTS["caution_mph"]   # unchanged
    assert cfg["gauge_max"]   == _DEFAULTS["gauge_max"]     # unchanged


def test_yaml_polling_overrides(tmp_path):
    p = make_yaml(tmp_path, """
        polling:
          poll_interval: 120
          web_port: 9090
    """)
    cfg = load_config(config_path=p)
    assert cfg["poll_interval"] == 120
    assert cfg["web_port"]      == 9090


def test_empty_yaml_returns_defaults(tmp_path):
    p = make_yaml(tmp_path, "")
    cfg = load_config(config_path=p)
    assert cfg["good_mph"] == _DEFAULTS["good_mph"]


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------

def test_env_var_overrides_yaml(tmp_path, monkeypatch):
    p = make_yaml(tmp_path, "thresholds:\n  good_mph: 12\n")
    monkeypatch.setenv("PONTOON_GOOD_MPH", "8")
    cfg = load_config(config_path=p)
    assert cfg["good_mph"] == 8.0


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("PONTOON_CAUTION_MPH", "18")
    cfg = load_config(config_path=pathlib.Path("/nonexistent/config.yaml"))
    assert cfg["caution_mph"] == 18.0


def test_env_var_ndbc_station(monkeypatch):
    monkeypatch.setenv("PONTOON_NDBC_STATION", "41025")
    cfg = load_config(config_path=pathlib.Path("/nonexistent/config.yaml"))
    assert cfg["ndbc_station"] == "41025"


def test_invalid_env_var_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("PONTOON_GOOD_MPH", "not_a_number")
    cfg = load_config(config_path=tmp_path / "nonexistent.yaml")
    assert cfg["good_mph"] == _DEFAULTS["good_mph"]


# ---------------------------------------------------------------------------
# CLI argument overrides
# ---------------------------------------------------------------------------

def test_cli_good_mph_overrides_yaml(tmp_path):
    p = make_yaml(tmp_path, "thresholds:\n  good_mph: 12\n")
    args = build_arg_parser().parse_args(["--good-mph", "9"])
    cfg  = load_config(args=args, config_path=p)
    assert cfg["good_mph"] == 9.0


def test_cli_config_path(tmp_path):
    p = make_yaml(tmp_path, "thresholds:\n  good_mph: 7\n")
    args = build_arg_parser().parse_args(["--config", str(p)])
    cfg  = load_config(args=args)
    assert cfg["good_mph"] == 7.0


def test_cli_overrides_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("PONTOON_GOOD_MPH", "11")
    args = build_arg_parser().parse_args(["--good-mph", "6"])
    cfg  = load_config(args=args, config_path=tmp_path / "nonexistent.yaml")
    assert cfg["good_mph"] == 6.0


# ---------------------------------------------------------------------------
# Priority ordering smoke test
# ---------------------------------------------------------------------------

def test_gauge_max_raised_when_not_above_caution(tmp_path):
    """A gauge_max <= caution_mph (no red zone) is auto-corrected upward."""
    p = make_yaml(tmp_path, """
        thresholds:
          good_mph: 15
          caution_mph: 30
          gauge_max: 30
    """)
    cfg = load_config(config_path=p)
    assert cfg["gauge_max"] > cfg["caution_mph"]


def test_valid_gauge_max_left_untouched(tmp_path):
    p = make_yaml(tmp_path, """
        thresholds:
          caution_mph: 23
          gauge_max: 30
    """)
    cfg = load_config(config_path=p)
    assert cfg["gauge_max"] == 30.0


def test_priority_order(tmp_path, monkeypatch):
    """CLI > env > yaml > default."""
    p = make_yaml(tmp_path, "thresholds:\n  caution_mph: 25\n")
    monkeypatch.setenv("PONTOON_CAUTION_MPH", "22")
    args = build_arg_parser().parse_args(["--caution-mph", "19"])
    cfg  = load_config(args=args, config_path=p)
    assert cfg["caution_mph"] == 19.0
