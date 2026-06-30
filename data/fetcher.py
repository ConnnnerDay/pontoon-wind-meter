"""
Network data fetching: NDBC buoy reads and Weather.gov alert polling.

``data_loop`` is the background thread target that keeps the shared state
dict current.  All other functions are standalone and can be called or
mocked independently in tests.

Cache behaviour
---------------
* After every successful NDBC parse the observation is written to disk via
  :mod:`disk_cache` (when ``cfg["cache_enabled"]`` is true).
* When the live fetch fails the last cached snapshot is loaded, checked for
  freshness, and applied to state with ``state["cached"] = True``.
* ``state["cached"]`` is ``False`` whenever live data is current.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from urllib.request import urlopen, Request

from ndbc import ms_to_mph, celsius_to_f, m_to_ft, wind_direction, parse_ndbc, obs_age_minutes
from locations import fetch_coops_water_temp
import disk_cache


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


def _apply_cache(state: dict, lock: threading.Lock, cfg: dict) -> bool:
    """Try to load the disk snapshot and apply it to *state*.

    Returns ``True`` if a usable snapshot was found, ``False`` otherwise.
    Logs a warning in every case so the operator can see what happened.
    """
    cache_path  = cfg.get("cache_path", "")
    max_age_sec = int(cfg.get("cache_max_age_minutes", 180)) * 60

    snapshot = disk_cache.load_snapshot(cache_path)
    if snapshot is None:
        logging.warning("Cache: no snapshot on disk — no fallback data available")
        return False

    if not disk_cache.snapshot_is_fresh(snapshot, max_age_sec):
        saved_at = snapshot.get(disk_cache._TIMESTAMP_KEY)
        logging.warning(
            "Cache: snapshot at %s is too old (saved_at=%s, max_age=%dm) — ignoring",
            cache_path, saved_at, cfg.get("cache_max_age_minutes", 180),
        )
        return False

    with lock:
        for key in disk_cache.CACHE_FIELDS:
            if key in snapshot:
                state[key] = snapshot[key]
        # The cached "age" was frozen when the snapshot was written.  Add the
        # wall-clock time elapsed since the save so the freshness bar / STALE
        # flag reflect how old the data actually is now — otherwise hours-old
        # cached data renders as if it were minutes fresh.
        saved_at = snapshot.get(disk_cache._TIMESTAMP_KEY)
        if state.get("age") is not None and saved_at is not None:
            try:
                elapsed_min = max(0.0, (time.time() - float(saved_at)) / 60.0)
                state["age"] = int(state["age"] + elapsed_min)
            except (TypeError, ValueError):
                pass
        state["cached"] = True
        state["error"]  = None

    logging.warning(
        "Cache: live fetch failed — loaded snapshot from %s (wind=%.1f mph gust=%.1f mph)",
        cache_path,
        state.get("wind") or 0.0,
        state.get("gust") or 0.0,
    )
    return True


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
    ndbc_url       = f"https://www.ndbc.noaa.gov/data/realtime2/{cfg['ndbc_station']}.txt"
    alerts_url     = f"https://api.weather.gov/alerts/active?point={cfg['lat']},{cfg['lon']}"
    coops_station  = cfg["coops_station"]
    poll_interval  = cfg["poll_interval"]
    alerts_interval = cfg["alerts_interval"]
    cache_enabled  = cfg.get("cache_enabled", True)
    cache_path     = cfg.get("cache_path", "")
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
                    gust_history=new_history, error=None, cached=False,
                )

            logging.info(
                "wind=%.1f mph gust=%.1f mph dir=%s age=%sm wtmp=%s wvht=%s pres=%s dewp=%s",
                wind, gust, wdir, age,
                f"{wtmp:.1f}°F"  if wtmp is not None else "MM",
                f"{wvht:.1f}ft"  if wvht is not None else "MM",
                f"{pres:.1f}hPa" if pres is not None else "MM",
                f"{dewp:.1f}°F"  if dewp is not None else "MM",
            )

            # Persist a fresh snapshot so we can fall back to it on the next outage
            if cache_enabled and cache_path:
                with lock:
                    snap = {k: state[k] for k in disk_cache.CACHE_FIELDS if k in state}
                disk_cache.save_snapshot(cache_path, snap)

        except Exception as exc:
            logging.error("NDBC live fetch failed: %s", exc)
            applied = False
            if cache_enabled and cache_path:
                applied = _apply_cache(state, lock, cfg)
            if not applied:
                # No fresh reading and the cache couldn't help. Keep aging the
                # last reading by roughly the elapsed poll so the display moves
                # toward STALE instead of freezing on a confident verdict.
                with lock:
                    if state.get("age") is not None:
                        state["age"] += max(1, poll_interval // 60)
                    if not cache_enabled:
                        state["error"] = str(exc)

        if time.monotonic() - last_alert_fetch >= alerts_interval:
            try:
                alerts = fetch_alerts(alerts_url)
                with lock:
                    state["alerts"] = alerts
                if alerts:
                    logging.info("Active alerts: %s", [a[0] for a in alerts])
                else:
                    logging.info("No active alerts")
            except Exception as exc:
                logging.warning("Alert fetch failed: %s", exc)
            last_alert_fetch = time.monotonic()

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, poll_interval - elapsed))
