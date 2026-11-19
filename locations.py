"""
NOAA CO-OPS water-temperature lookup, used when the buoy has no WTMP reading.

Results are cached in memory for COOPS_CACHE_TTL seconds. No API key needed.
API docs: https://api.tidesandcurrents.noaa.gov/api/prod/
"""

from urllib.request import urlopen, Request
import json
import logging
import time
from datetime import datetime, timezone

COOPS_CACHE_TTL = 1800  # seconds; water temp changes slowly, no need to hit the API every poll
COOPS_MAX_AGE   = 3 * 3600  # ignore a "latest" reading older than this (sensor down)

_COOPS_CACHE: dict[str, tuple[float, float | None]] = {}  # station → (monotonic time, temp_f)


def parse_coops_water_temp(data: dict, now: datetime | None = None) -> float | None:
    """Pull the newest reading out of a CO-OPS ``time_zone=gmt`` response.

    Returns None when there's no reading, the value is blank, or it's older
    than COOPS_MAX_AGE. ``date=latest`` returns the last value the station
    ever posted, which can be days old if the sensor is down.
    """
    readings = data.get("data") or []
    if not readings:
        return None
    last = readings[-1]
    try:
        temp_f = float(last["v"])
        obs = datetime.strptime(last["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    if (now - obs).total_seconds() > COOPS_MAX_AGE:
        logging.info("CO-OPS reading from %s UTC is too old — ignored", last["t"])
        return None
    return temp_f


def fetch_coops_water_temp(station: str) -> float | None:
    """
    Return the latest water temperature in °F from a NOAA CO-OPS station,
    or None if there's no recent reading.

    Results (including failures) are cached for COOPS_CACHE_TTL seconds so
    the 5-minute poll doesn't hit the API every time.
    """
    hit = _COOPS_CACHE.get(station)
    if hit is not None and time.monotonic() - hit[0] < COOPS_CACHE_TTL:
        return hit[1]

    url = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        f"?station={station}&product=water_temperature"
        "&date=latest&units=english&time_zone=gmt&format=json"
    )
    temp_f = None
    try:
        req = Request(url, headers={"User-Agent": "pontoon-wind-meter/1.0"})
        with urlopen(req, timeout=10) as resp:
            temp_f = parse_coops_water_temp(json.loads(resp.read()))
        if temp_f is not None:
            logging.info("CO-OPS station %s water temp: %.1f°F", station, temp_f)
    except Exception as exc:
        logging.warning("CO-OPS fetch failed for station %s: %s", station, exc)

    _COOPS_CACHE[station] = (time.monotonic(), temp_f)
    return temp_f
