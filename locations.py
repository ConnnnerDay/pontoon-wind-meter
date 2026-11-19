"""
NOAA CO-OPS water-temperature lookup, used when the buoy has no WTMP reading.

Results are cached in memory for COOPS_CACHE_TTL seconds. No API key needed.
API docs: https://api.tidesandcurrents.noaa.gov/api/prod/
"""

from urllib.request import urlopen, Request
import json
import logging
import time

COOPS_CACHE_TTL = 1800  # seconds; water temp changes slowly, no need to hit the API every poll

_COOPS_CACHE: dict[str, tuple[float, float | None]] = {}  # station → (timestamp, temp_f)


def fetch_coops_water_temp(station: str) -> float | None:
    """
    Return the latest water temperature in °F from a NOAA CO-OPS station,
    or None if the reading is unavailable.

    Results are cached for COOPS_CACHE_TTL seconds so repeated calls within
    the same polling window do not hit the network.
    """
    cached_at, cached_val = _COOPS_CACHE.get(station, (0.0, None))
    if time.monotonic() - cached_at < COOPS_CACHE_TTL:
        return cached_val

    url = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        f"?station={station}&product=water_temperature"
        "&date=latest&units=english&time_zone=lst&format=json"
    )
    try:
        req = Request(url, headers={"User-Agent": "pontoon-wind-meter/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        readings = data.get("data") or []
        if readings:
            temp_f = float(readings[-1]["v"])
            _COOPS_CACHE[station] = (time.monotonic(), temp_f)
            logging.info("CO-OPS station %s water temp: %.1f°F", station, temp_f)
            return temp_f
    except Exception as exc:
        logging.warning("CO-OPS fetch failed for station %s: %s", station, exc)

    _COOPS_CACHE[station] = (time.monotonic(), None)
    return None
