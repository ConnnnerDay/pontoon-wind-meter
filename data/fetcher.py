"""
Network data fetching: NDBC buoy reads and Weather.gov alert polling.

``data_loop`` is the background thread target that keeps the shared state
dict current.  All other functions are standalone and can be called or
mocked independently in tests.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from urllib.request import urlopen, Request

from ndbc import ms_to_mph, celsius_to_f, m_to_ft, wind_direction, parse_ndbc, obs_age_minutes
from locations import fetch_coops_water_temp


def fetch_ndbc_raw(url: str) -> str:
    """Download the NDBC realtime2 text file, retrying up to 3 times."""
    req = Request(url, headers={"User-Agent": "pontoon-wind-meter/1.0"})
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.read().decode()
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logging.warning("NDBC fetch attempt %d failed: %s", attempt + 1, exc)
                time.sleep(5)
    raise last_exc  # type: ignore[misc]


def fetch_alerts(url: str) -> list[tuple[str, str]]:
    """Fetch active NOAA Weather.gov alerts, returning (event_name, severity) pairs."""
    req = Request(url, headers={
        "User-Agent": "pontoon-wind-meter/1.0",
        "Accept":     "application/geo+json",
    })
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            seen: set[str] = set()
            result: list[tuple[str, str]] = []
            for feat in data.get("features", []):
                props = feat.get("properties", {})
                event = props.get("event", "")
                sev   = props.get("severity", "Unknown")
                if event and event not in seen:
                    seen.add(event)
                    result.append((event.upper(), sev))
            return result
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logging.warning("Alert fetch attempt %d failed: %s", attempt + 1, exc)
                time.sleep(5)
    raise last_exc  # type: ignore[misc]


def data_loop(state: dict, lock: threading.Lock, cfg: dict) -> None:
    """Background thread: refresh NDBC data and NOAA alerts on independent timers.

    Parameters
    ----------
    state:
        Shared mutable dict — must already contain the expected keys.
    lock:
        ``threading.Lock`` guarding ``state``.
    cfg:
        Configuration dict from :func:`config.load_config`.
    """
    ndbc_url      = f"https://www.ndbc.noaa.gov/data/realtime2/{cfg['ndbc_station']}.txt"
    alerts_url    = f"https://api.weather.gov/alerts/active?point={cfg['lat']},{cfg['lon']}"
    coops_station = cfg["coops_station"]
    poll_interval = cfg["poll_interval"]
    alerts_interval = cfg["alerts_interval"]
    last_alert_fetch = 0.0

    while True:
        t0 = time.monotonic()

        try:
            row = parse_ndbc(fetch_ndbc_raw(ndbc_url))
            if row.get("WSPD", "MM") == "MM":
                raise ValueError("WSPD reading unavailable")

            wind     = ms_to_mph(float(row["WSPD"]))
            gust_raw = row.get("GST", "MM")
            gust     = ms_to_mph(float(gust_raw)) if gust_raw != "MM" else wind
            wdir     = wind_direction(row.get("WDIR", "MM"))
            age      = obs_age_minutes(row)

            wtmp_raw = row.get("WTMP", "MM")
            wvht_raw = row.get("WVHT", "MM")
            atmp_raw = row.get("ATMP", "MM")
            dpd_raw  = row.get("DPD",  "MM")
            dewp_raw = row.get("DEWP", "MM")
            pres_raw = row.get("PRES", "MM")

            wtmp = celsius_to_f(float(wtmp_raw)) if wtmp_raw != "MM" else None
            if wtmp is None:
                wtmp = fetch_coops_water_temp(coops_station)
            wvht = m_to_ft(float(wvht_raw))      if wvht_raw != "MM" else None
            atmp = celsius_to_f(float(atmp_raw)) if atmp_raw != "MM" else None
            dewp = celsius_to_f(float(dewp_raw)) if dewp_raw != "MM" else None

            try:
                dpd = float(dpd_raw) if dpd_raw not in ("MM", "99.00") else None
                if dpd is not None and dpd <= 0:
                    dpd = None
            except ValueError:
                dpd = None

            try:
                pres = float(pres_raw) if pres_raw != "MM" else None
            except ValueError:
                pres = None

            with lock:
                new_history = [gust] + state["gust_history"][:5]
                new_pres_history = (
                    [pres] + state["pres_history"][:5]
                    if pres is not None else list(state["pres_history"])
                )
                state.update(
                    wind=wind, gust=gust, wdir=wdir, age=age,
                    wtmp=wtmp, wvht=wvht, atmp=atmp, dpd=dpd,
                    dewp=dewp, pres=pres, pres_history=new_pres_history,
                    gust_history=new_history, error=None,
                )

            logging.info(
                "wind=%.1f mph gust=%.1f mph dir=%s age=%sm wtmp=%s wvht=%s pres=%s dewp=%s",
                wind, gust, wdir, age,
                f"{wtmp:.1f}°F"  if wtmp is not None else "MM",
                f"{wvht:.1f}ft"  if wvht is not None else "MM",
                f"{pres:.1f}hPa" if pres is not None else "MM",
                f"{dewp:.1f}°F"  if dewp is not None else "MM",
            )

        except Exception as exc:
            logging.error("NDBC fetch failed: %s", exc)
            with lock:
                state["error"] = str(exc)

        if time.monotonic() - last_alert_fetch >= alerts_interval:
            try:
                alerts = fetch_alerts(alerts_url)
                with lock:
                    state["alerts"] = alerts
                logging.info(
                    "Active alerts: %s" if alerts else "No active alerts",
                    *([a[0] for a in alerts] if alerts else []),
                )
            except Exception as exc:
                logging.warning("Alert fetch failed: %s", exc)
            last_alert_fetch = time.monotonic()

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, poll_interval - elapsed))
