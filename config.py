"""
Configuration loader for Pontoon Wind Meter.

Priority (highest → lowest):
  1. CLI arguments  (--good-mph 12, --config myconfig.yml, …)
  2. Environment variables  (PONTOON_GOOD_MPH=12, …)
  3. config.yaml / the file named by --config
  4. Built-in defaults  (same values as the original constants)

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

# Built-in defaults — these are the original hard-coded constants so the app
# works identically without a config.yaml present.
_DEFAULTS: dict[str, Any] = {
    # location
    "location_name":      "Wrightsville Beach / ICW",
    "ndbc_station":       "41038",
    "coops_station":      "8658120",
    "lat":                34.2108,
    "lon":               -77.5986,
    # thresholds
    "good_mph":           15.0,
    "caution_mph":        23.0,
    "gauge_max":          30.0,
    "stale_minutes":      90.0,
    "wave_good_ft":       2.0,
    "wave_caution_ft":    3.0,
    "wave_chop_dpd":      5.0,
    "wave_swell_dpd":     9.0,
    "temp_cool_f":        65.0,
    "temp_cold_f":        50.0,
    "atmp_warm_f":        80.0,
    "atmp_chilly_f":      62.0,
    "fog_spread_f":       5.0,
    "pres_fall_caution":  1.5,
    # polling / display
    "poll_interval":      300,
    "alerts_interval":    600,
    "frame_rate":         30,
    "web_port":           8080,
}

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
    "NDBC_STATION":       ("ndbc_station",        str),
    "COOPS_STATION":      ("coops_station",       str),
    "LAT":                ("lat",                 float),
    "LON":                ("lon",                 float),
}
_ENV_PREFIX = "PONTOON_"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        logging.info("No config file at %s — using defaults", path)
        return {}
    if not _YAML_AVAILABLE:
        logging.warning("PyYAML not installed; ignoring %s — install pyyaml to use config.yaml", path)
        return {}
    with open(path) as fh:
        return _yaml.safe_load(fh) or {}


def _flatten_yaml(data: dict) -> dict[str, Any]:
    """Convert nested YAML sections into a flat key→value dict."""
    flat: dict[str, Any] = {}

    loc = data.get("location", {})
    if "name"          in loc: flat["location_name"]  = str(loc["name"])
    if "ndbc_station"  in loc: flat["ndbc_station"]   = str(loc["ndbc_station"])
    if "coops_station" in loc: flat["coops_station"]  = str(loc["coops_station"])
    if "lat"           in loc: flat["lat"]             = float(loc["lat"])
    if "lon"           in loc: flat["lon"]             = float(loc["lon"])

    thr = data.get("thresholds", {})
    for key in (
        "good_mph", "caution_mph", "gauge_max", "stale_minutes",
        "wave_good_ft", "wave_caution_ft", "wave_chop_dpd", "wave_swell_dpd",
        "temp_cool_f", "temp_cold_f", "atmp_warm_f", "atmp_chilly_f",
        "fog_spread_f", "pres_fall_caution",
    ):
        if key in thr:
            flat[key] = float(thr[key])

    pol = data.get("polling", {})
    for key in ("poll_interval", "alerts_interval", "frame_rate", "web_port"):
        if key in pol:
            flat[key] = int(pol[key])

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

    logging.info(
        "Config: location=%s ndbc=%s good_mph=%.0f caution_mph=%.0f",
        cfg["location_name"], cfg["ndbc_station"], cfg["good_mph"], cfg["caution_mph"],
    )
    return cfg


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
    p.add_argument("--lat",               type=float, metavar="DEG",                            help="Location latitude")
    p.add_argument("--lon",               type=float, metavar="DEG",                            help="Location longitude")
    return p
