"""
Location definitions and NOAA CO-OPS water-temperature helper.

Each entry in LOCATIONS describes one named spot:
  ndbc_station  – NDBC buoy or nearshore station ID (primary data source)
  coops_station – NOAA CO-OPS tide-gauge station ID (water-temp fallback)
  lat / lon     – decimal degrees (used for Weather.gov alerts endpoint)
  name          – human-readable display label

CO-OPS water temperature is fetched from the public Tides & Currents API
(no API key required) and cached in memory for COOPS_CACHE_TTL seconds.

API docs: https://api.tidesandcurrents.noaa.gov/api/prod/
"""

from urllib.request import urlopen, Request
import json
import logging
import time

COOPS_CACHE_TTL = 1800  # seconds (30 min) — CO-OPS readings update hourly

LOCATIONS = {
    "wrightsville_beach": {
        "name":          "Wrightsville Beach / ICW",
        "ndbc_station":  "41038",
        "coops_station": "8658120",   # Wilmington, NC
        "lat":           34.2108,
        "lon":          -77.5986,
    },
}

# Active location — change this to switch spots without touching pontoon_meter.py
ACTIVE_LOCATION = LOCATIONS["wrightsville_beach"]

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
