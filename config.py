"""
Configuration loader for Pontoon Wind Meter.

Priority (highest → lowest):
  1. CLI arguments  (--good-mph 12, --config myconfig.yml, …)
  2. Environment variables  (PONTOON_GOOD_MPH=12, …)
  3. config.yaml / the file named by --config
  4. Built-in defaults below

Usage::

    from config import build_arg_parser, load_config

    args = build_arg_parser().parse_args()
    cfg  = load_config(args)
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Defaults used for anything config.yaml, env vars, and CLI don't set.
# Keep these in sync with config.yaml.
_DEFAULTS: dict[str, Any] = {
    # location
    "location_name":      "Wrightsville Beach / ICW",
    "ndbc_station":       "41038",
    "coops_station":      "8658120",
    "lat":                34.2108,
    "lon":               -77.5986,
    # thresholds
    "good_mph":           22.0,
    "caution_mph":        30.0,
    "gauge_max":          40.0,
    "stale_minutes":      90.0,
    "wave_good_ft":       2.0,
    "wave_caution_ft":    3.0,
    "wave_chop_dpd":      5.0,
    "wave_swell_dpd":     9.0,
    "temp_cool_f":        65.0,
    "temp_cold_f":        50.0,
    "atmp_warm_f":        80.0,
    "atmp_chilly_f":      62.0,
    "fog_spread_f":       3.0,
    "pres_fall_caution":  1.5,
    # polling / display
    "poll_interval":      300,
    "alerts_interval":    600,
    "frame_rate":         30,
    "web_port":           8080,
    # disk cache
    "cache_enabled":          True,
    "cache_path":             "/home/pizero/.cache/pontoon-meter/latest_snapshot.json",
    "cache_max_age_minutes":  180,
}

def _parse_bool(v: Any) -> bool:
    """Accept YAML booleans and the usual strings ("no", "off", "0", ...)."""
    if isinstance(v, bool):
        return v
    text = str(v).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"not a boolean: {v!r}")


# env-var suffix → (config key, cast function)
_ENV_MAP: dict[str, tuple[str, type]] = {
    "GOOD_MPH":           ("good_mph",           float),
    "CAUTION_MPH":        ("caution_mph",         float),
    "GAUGE_MAX":          ("gauge_max",           float),
    "STALE_MINUTES":      ("stale_minutes",       float),
    "WAVE_GOOD_FT":       ("wave_good_ft",        float),
    "WAVE_CAUTION_FT":    ("wave_caution_ft",     float),
    "WAVE_CHOP_DPD":      ("wave_chop_dpd",       float),
    "WAVE_SWELL_DPD":     ("wave_swell_dpd",      float),
    "TEMP_COOL_F":        ("temp_cool_f",         float),
    "TEMP_COLD_F":        ("temp_cold_f",         float),
    "ATMP_WARM_F":        ("atmp_warm_f",         float),
    "ATMP_CHILLY_F":      ("atmp_chilly_f",       float),
    "FOG_SPREAD_F":       ("fog_spread_f",        float),
    "PRES_FALL_CAUTION":  ("pres_fall_caution",   float),
    "POLL_INTERVAL":      ("poll_interval",       int),
    "ALERTS_INTERVAL":    ("alerts_interval",     int),
    "FRAME_RATE":         ("frame_rate",          int),
    "WEB_PORT":           ("web_port",            int),
    "NDBC_STATION":            ("ndbc_station",          str),
    "COOPS_STATION":           ("coops_station",         str),
    "LAT":                     ("lat",                   float),
    "LON":                     ("lon",                   float),
    "CACHE_ENABLED":           ("cache_enabled",         _parse_bool),
    "CACHE_PATH":              ("cache_path",            str),
    "CACHE_MAX_AGE_MINUTES":   ("cache_max_age_minutes", int),
}
_ENV_PREFIX = "PONTOON_"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        logging.info("No config file at %s — using defaults", path)
        return {}
    if not _YAML_AVAILABLE:
        logging.warning("PyYAML not installed; ignoring %s — install pyyaml to use config.yaml", path)
        return {}
    try:
        with open(path) as fh:
            data = _yaml.safe_load(fh) or {}
    except (OSError, _yaml.YAMLError) as exc:
        logging.error("Config: cannot read %s (%s) — using defaults", path, exc)
        return {}
    if not isinstance(data, dict):
        logging.error("Config: %s is not a YAML mapping — using defaults", path)
        return {}
    return data


# YAML section → {yaml key: (config key, cast)}
_YAML_MAP: dict[str, dict[str, tuple[str, Any]]] = {
    "location": {
        "name":          ("location_name", str),
        "ndbc_station":  ("ndbc_station",  str),
        "coops_station": ("coops_station", str),
        "lat":           ("lat",           float),
        "lon":           ("lon",           float),
    },
    "thresholds": {k: (k, float) for k in (
        "good_mph", "caution_mph", "gauge_max", "stale_minutes",
        "wave_good_ft", "wave_caution_ft", "wave_chop_dpd", "wave_swell_dpd",
        "temp_cool_f", "temp_cold_f", "atmp_warm_f", "atmp_chilly_f",
        "fog_spread_f", "pres_fall_caution",
    )},
    "polling": {k: (k, int) for k in (
        "poll_interval", "alerts_interval", "frame_rate", "web_port",
    )},
    "cache": {
        "enabled":         ("cache_enabled",         _parse_bool),
        "path":            ("cache_path",            str),
        "max_age_minutes": ("cache_max_age_minutes", int),
    },
}


def _flatten_yaml(data: dict) -> dict[str, Any]:
    """Convert nested YAML sections into a flat key→value dict.

    An empty section (``location:`` with nothing under it) is fine. A value
    that can't be converted is logged and skipped, so the default is used.
    """
    flat: dict[str, Any] = {}
    for section, keys in _YAML_MAP.items():
        values = data.get(section) or {}
        if not isinstance(values, dict):
            logging.warning("Config: section %r should be a mapping — ignored", section)
            continue
        for ykey, (ckey, cast) in keys.items():
            if ykey not in values or values[ykey] is None:
                continue
            try:
                flat[ckey] = cast(values[ykey])
            except (TypeError, ValueError):
                logging.warning("Config: %s.%s=%r is invalid — using default %r",
                                section, ykey, values[ykey], _DEFAULTS[ckey])
    return flat


def load_config(
    args: argparse.Namespace | None = None,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return a merged configuration dict.

    Parameters
    ----------
    args:
        Parsed argparse namespace (from :func:`build_arg_parser`).
        Pass ``None`` to skip CLI-layer overrides (useful in tests).
    config_path:
        Explicit path to a YAML file.  If given, it takes precedence over
        the ``--config`` argument and the default ``config.yaml`` location.
    """
    cfg = dict(_DEFAULTS)

    # Determine YAML path
    if config_path is not None:
        yaml_path = Path(config_path)
    elif args is not None and getattr(args, "config", None):
        yaml_path = Path(args.config)
    else:
        yaml_path = _DEFAULT_CONFIG_PATH

    cfg.update(_flatten_yaml(_load_yaml(yaml_path)))

    # Environment variable overrides
    for suffix, (key, cast) in _ENV_MAP.items():
        raw = os.environ.get(_ENV_PREFIX + suffix)
        if raw is not None:
            try:
                cfg[key] = cast(raw)
            except ValueError:
                logging.warning(
                    "Invalid env var %s%s=%r — ignored", _ENV_PREFIX, suffix, raw
                )

    # CLI argument overrides
    if args is not None:
        for suffix, (key, cast) in _ENV_MAP.items():
            val = getattr(args, key, None)
            if val is not None:
                try:
                    cfg[key] = cast(val)
                except (ValueError, TypeError):
                    pass
        if getattr(args, "no_cache", False):
            cfg["cache_enabled"] = False

    _validate_thresholds(cfg)
    return cfg


def _validate_thresholds(cfg: dict[str, Any]) -> None:
    """Sanity-check the wind bands in place, logging and correcting bad combos.

    The gauge maps ``caution_mph / gauge_max`` onto the full arc sweep, so if
    ``gauge_max`` does not exceed ``caution_mph`` the red NO-GO zone collapses
    to zero width — a silently broken display.  Likewise the verdict bands only
    make sense when ``good_mph < caution_mph``.  These are easy to trip when
    overriding one knob via env/CLI without the others, so guard against them.
    """
    good    = cfg["good_mph"]
    caution = cfg["caution_mph"]
    gmax    = cfg["gauge_max"]

    if good >= caution:
        logging.warning(
            "Config: good_mph (%g) should be below caution_mph (%g); "
            "the CAUTION band will be empty or inverted.", good, caution,
        )

    if gmax <= caution:
        fixed = round(caution * 1.25, 1)
        logging.warning(
            "Config: gauge_max (%g) must exceed caution_mph (%g) for a visible "
            "red zone — raising gauge_max to %g.", gmax, caution, fixed,
        )
        cfg["gauge_max"] = fixed


def build_arg_parser() -> argparse.ArgumentParser:
    """Return an ArgumentParser for all user-visible knobs."""
    p = argparse.ArgumentParser(
        description="Pontoon Wind Meter — marine conditions display",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--config", metavar="FILE",
        help="Path to YAML config file (default: config.yaml next to this script)",
    )
    p.add_argument("--good-mph",          type=float, metavar="N",    dest="good_mph",          help="GO threshold (mph)")
    p.add_argument("--caution-mph",       type=float, metavar="N",    dest="caution_mph",       help="CAUTION threshold (mph)")
    p.add_argument("--gauge-max",         type=float, metavar="N",    dest="gauge_max",         help="Full-scale gauge value (mph)")
    p.add_argument("--poll-interval",     type=int,   metavar="SEC",  dest="poll_interval",     help="NDBC poll interval (seconds)")
    p.add_argument("--alerts-interval",   type=int,   metavar="SEC",  dest="alerts_interval",   help="Alert poll interval (seconds)")
    p.add_argument("--web-port",          type=int,   metavar="PORT", dest="web_port",           help="HTTP dashboard port")
    p.add_argument("--frame-rate",        type=int,   metavar="FPS",  dest="frame_rate",         help="Display frame rate")
    p.add_argument("--ndbc-station",      type=str,   metavar="ID",   dest="ndbc_station",      help="NDBC station ID")
    p.add_argument("--coops-station",     type=str,   metavar="ID",   dest="coops_station",     help="CO-OPS station ID")
    p.add_argument("--lat",                  type=float, metavar="DEG",                              help="Location latitude")
    p.add_argument("--lon",                  type=float, metavar="DEG",                              help="Location longitude")
    p.add_argument("--cache-path",           type=str,   metavar="FILE", dest="cache_path",           help="Snapshot cache file path")
    p.add_argument("--no-cache",             action="store_true",        dest="no_cache",             help="Disable the disk cache")
    p.add_argument("--cache-max-age",        type=int,   metavar="MIN",  dest="cache_max_age_minutes", help="Cache max age in minutes")
    return p
